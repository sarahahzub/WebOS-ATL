/*
 * xdgshim: tiny Wayland proxy that gives xdg-shell clients (GTK4 / ATL) a
 * window on the webOS compositor, which only offers wl_shell + wl_webos_shell.
 *
 *   xdgshim [listen-name|/abs/path]   (default: wayland-atl in $XDG_RUNTIME_DIR)
 *   xdgshim -1 /abs/path              single client: connect to the compositor FIRST from this
 *       process, then serve one client inline and exit when it disconnects. Used by the webOS
 *       wrapper app (exec'd by SAM) so the compositor sees the window as coming from the app's
 *       own pid; LSM only shows a launched app's window if it comes from that process.
 *   WAYLAND_DISPLAY=wayland-atl android-translation-layer app.apk
 *
 * Messages are forwarded byte-for-byte (fds included) except:
 *   - a fake "xdg_wm_base" v1 global is advertised next to wl_shell;
 *     binding it binds wl_shell at the same object id.
 *   - xdg_wm_base.get_xdg_surface(S, surf) -> wl_shell.get_shell_surface(S, surf)
 *   - xdg_surface.get_toplevel(T)          -> wl_webos_shell.get_shell_surface(T, surf)
 *       + wl_shell_surface.set_toplevel + set_property appId/window type,
 *       and the shim sends the client xdg_toplevel.configure + xdg_surface.configure.
 *   - positioners/popups become dummy wl_regions (popups are not shown).
 *   - wl_shell_surface.ping is answered by the shim; webos close -> xdg_toplevel.close.
 * wl_webos_shell needs an object id the client won't reuse: the shim takes the id
 * of the client's first finished wl_display.sync callback (it swallows that
 * delete_id, so the client never reuses the id, and binds wl_webos_shell to it).
 *
 * Env: XDGSHIM_APPID (default org.webosatl.app), XDGSHIM_W/H (1920x1080), XDGSHIM_DEBUG=1,
 *      XDGSHIM_NOOPAQUE=1 (drop set_opaque_region: video hole punch), XDGSHIM_ALPHA=1 (log frame pixels),
 *      XDGSHIM_SUBSURF=0 (subsurfaces stay unmapped, like popups).
 */
#define _GNU_SOURCE
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>
#include <sys/prctl.h>
#include <arpa/inet.h>
#include <fcntl.h>

#define MAXID 65536
#define FAKE_NAME 0xFFFF0A71u
enum { T_NONE, T_REG, T_CB, T_COMP, T_XWM, T_XSURF, T_XTOP, T_XPOS, T_XPOP, T_SEAT, T_PTR, T_SURF, T_SUBC, T_KBD, T_SHM, T_POOL, T_BUF, T_BACK, T_FOREIGN, T_EXPORTED, T_IMPORTED,
	T_TIM, T_TI, T_TM };
static uint32_t last_buf[MAXID]; /* last buffer a blocked surface tried to attach */
static uint8_t dbg_att[MAXID];      /* XDGSHIM_DEBUG: first buffer on a non-main surface logged */
static uint8_t allowed_surf[MAXID]; /* surfaces that may show content: toplevels and their subsurfaces */
static uint8_t hidden_surf[MAXID]; /* wl_surface ids of popups: never attach a buffer, so LSM never maps them */

static int dbg;
static int no_subsurf;              /* XDGSHIM_SUBSURF=0: subsurfaces never get a buffer (TV browser: LSM showed Chromium's
                                       bubbles, e.g. the fullscreen exit hint, as appId-less cards that minimized the app) */
static int no_opaque, alpha_dbg, clear_all; /* XDGSHIM_NOOPAQUE=1, XDGSHIM_ALPHA=1 (see main) */
/* XDGSHIM_EXPORT=<file>: export the toplevel as a webOS video element (wl_webos_foreign.export_element) and write
   the window id the compositor assigns to <file>; the NDL helper (atlvdec) binds the hardware video plane to it */
static const char *export_path;
static uint32_t foreign_name, export_type, exported_id;
/* XDGSHIM_IMPORT=<file>: webOS video element window id (written by atlvdec from NDL's exported window); the toplevel
   imports it (wl_webos_foreign.import_element) and attaches a punch-through, so the compositor shows the hardware
   video plane through the app window (what an app's own SDL window gets in moonlight-tv) */
static const char *import_path;
static uint32_t foreign_id, imported_id;
static char imported_wid[128];
static const char *appid;
static int W = 1920, H = 1080;

struct side {
	int fd;
	uint8_t buf[1 << 16];
	size_t len;
	int fds[64];
	int nfds;
};
static struct side cli, srv;
static uint8_t type_[MAXID];
static uint32_t surf_of[MAXID]; /* xdg_surface/toplevel id -> wl_surface id */
static uint32_t xsurf_of[MAXID]; /* toplevel id -> xdg_surface id */
static uint32_t shell_name, webos_name, webos_ver = 1, comp_id, reg_id, webos_id;
static uint32_t serial = 1;
static uint32_t pool[16]; static int npool;      /* spare object ids harvested from finished sync/frame callbacks */
static uint32_t subc_id;                         /* client's wl_subcompositor */

#define D(...) do { if (dbg) fprintf(stderr, "xdgshim: " __VA_ARGS__); } while (0)

static void die(const char *m) { perror(m); exit(1); }

/* --- output with pending fds --- */
static void out(struct side *to, struct side *from, const void *p, size_t n)
{
	struct iovec iov = { (void *)p, n };
	struct msghdr mh = { .msg_iov = &iov, .msg_iovlen = 1 };
	char cbuf[CMSG_SPACE(sizeof(int) * 64)];
	if (from && from->nfds) {
		mh.msg_control = cbuf;
		mh.msg_controllen = CMSG_SPACE(sizeof(int) * from->nfds);
		struct cmsghdr *c = CMSG_FIRSTHDR(&mh);
		c->cmsg_level = SOL_SOCKET; c->cmsg_type = SCM_RIGHTS;
		c->cmsg_len = CMSG_LEN(sizeof(int) * from->nfds);
		memcpy(CMSG_DATA(c), from->fds, sizeof(int) * from->nfds);
	}
	while (n) {
		ssize_t r = sendmsg(to->fd, &mh, MSG_NOSIGNAL);
		if (r < 0) { if (errno == EINTR) continue; fprintf(stderr, "xdgshim: send to %s failed: %s\n", to == &srv ? "compositor" : "client", strerror(errno)); exit(0); }
		if (from && from->nfds) {
			for (int i = 0; i < from->nfds; i++) close(from->fds[i]);
			from->nfds = 0;
			mh.msg_control = NULL; mh.msg_controllen = 0;
		}
		n -= r; iov.iov_base = (char *)iov.iov_base + r; iov.iov_len = n;
	}
}

/* --- message builder --- */
struct mb { uint32_t w[512]; int n; };
static void mb_start(struct mb *m, uint32_t obj, uint16_t op) { m->w[0] = obj; m->w[1] = op; m->n = 2; }
static void mb_u(struct mb *m, uint32_t v) { m->w[m->n++] = v; }
static void mb_s(struct mb *m, const char *s)
{
	uint32_t l = strlen(s) + 1;
	mb_u(m, l);
	memset(&m->w[m->n], 0, (l + 3) & ~3u);
	memcpy(&m->w[m->n], s, l);
	m->n += (l + 3) / 4;
}
static void mb_a(struct mb *m, const uint32_t *a, int cnt)
{
	mb_u(m, cnt * 4);
	for (int i = 0; i < cnt; i++) mb_u(m, a[i]);
}
static void mb_send(struct mb *m, struct side *to)
{
	m->w[1] |= (uint32_t)(m->n * 4) << 16;
	out(to, NULL, m->w, m->n * 4);
}

static const char *str_arg(const uint32_t *a, uint32_t *words)
{
	*words = 1 + (a[0] + 3) / 4;
	return a[0] ? (const char *)(a + 1) : "";
}

static void settype(uint32_t id, int t) { if (id < MAXID) type_[id] = t; }
static int gettype(uint32_t id) { return id < MAXID ? type_[id] : T_NONE; }

static void dummy_region(uint32_t id)
{
	struct mb m;
	if (!comp_id) { fprintf(stderr, "xdgshim: no wl_compositor yet for dummy %u\n", id); return; }
	mb_start(&m, comp_id, 1); mb_u(&m, id); mb_send(&m, &srv); /* wl_compositor.create_region */
}


/* ---------- soft cursor: the shim draws the pointer into the app's frames ----------
 * webOS hides its own cursor on every remote key and re-shows it at the screen centre, so a
 * compositor-drawn cursor can never follow D-pad steps. Instead the shim:
 *   - tracks the app's wl_shm pools/buffers,
 *   - on each commit of a toplevel copies the app's buffer into one of two shim-owned buffers
 *     (releasing the app's buffer straight away), draws an arrow into it, attaches that,
 *   - on cursor moves re-presents the last frame with the arrow moved (old pixels restored).
 * Every toplevel (main window, Compose dialogs / bottom sheets) has its own pair of buffers. The
 * newest live toplevel is "active": it gets the pointer focus and shows the arrow; when it closes the
 * previous one takes over. Apps that render with EGL/wl_drm (non-shm buffers) pass through
 * unchanged and fall back to moving the webOS cursor (netinput), see vptr_move().
 * XDGSHIM_SOFTCURSOR=0 disables all of this. */
#include <sys/mman.h>
#include <time.h>

static int dpad_mouse = 1;
static uint32_t ptr_obj, main_surf, ptr_in;     /* main_surf = wl_surface id of the active toplevel */
static uint32_t px = 960 << 8, py = 540 << 8;  /* wl_fixed */
static uint32_t synth_serial = 0x7f000000;

static int sc_enabled = 1;
static int sc_visible;                 /* arrow shown (after the first D-pad input) */
static int sc_dirty;                   /* arrow moved but the target buffer was busy */
static uint32_t shm_id;                /* client's wl_shm object */
static int in_present;
static int fd_used;                    /* index of next unconsumed fd in cfdq */
static int cfdq[64], ncfdq;            /* dups of the client's fds: out() sends (and closes) them all with the
                                          first forwarded message of a read, before create_pool is parsed */
static double last_dpad, last_click;           /* last_click: text input opens the keyboard only after a click */
static int click_x, click_y;
static void ti_click(void);

static struct { int fd; uint8_t *map; size_t size; } spool[MAXID];
static struct { uint32_t pool, off, w, h, stride, fmt; } sbuf[MAXID];

#define CUR_W 24
#define CUR_H 34
static const char *cur_art[17] = {
	"X           ", "XX          ", "XoX         ", "XooX        ", "XoooX       ", "XooooX      ",
	"XoooooX     ", "XooooooX    ", "XoooooooX   ", "XooooooooX  ", "XoooooXXXXX ", "XooXooX     ",
	"XoX XooX    ", "XX  XooX    ", "X    XooX   ", "     XooX   ", "      XX    " };

struct backbuf { uint32_t id; uint8_t *map; int busy; int cx, cy, cvalid; uint32_t save[CUR_W * CUR_H]; };
#define MAXSC 6
struct scs {                           /* one per toplevel */
	int used; unsigned seq;
	uint32_t surf;                     /* wl_surface id */
	int mode;                          /* 0 unknown, 1 shm composited, 2 pass-through */
	uint32_t pending;                  /* app buffer attached since the last commit */
	struct backbuf back[2];
	uint8_t *base; size_t total;       /* memfd mapping holding both buffers */
	uint32_t pool_id;
	int ready, w, h, stride, latest;
};
static struct scs scs[MAXSC];
static struct scs *act;                /* topmost live toplevel */
static unsigned sc_seq;

static int pump(struct side *from, struct side *to, int is_client);
static struct side fdcarrier;

static double now_s(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec / 1e9; }
static uint32_t now_ms(void) { return (uint32_t)(now_s() * 1000); }

static struct scs *sc_find(uint32_t surf)
{
	if (!surf) return NULL;
	for (int i = 0; i < MAXSC; i++) if (scs[i].used && scs[i].surf == surf) return &scs[i];
	return NULL;
}
static struct scs *sc_topmost(void)
{
	struct scs *best = NULL;
	for (int i = 0; i < MAXSC; i++) if (scs[i].used && (!best || scs[i].seq > best->seq)) best = &scs[i];
	return best;
}

static void sc_draw(struct scs *s, uint8_t *base, struct backbuf *bk)
{
	int x0 = (int)(px >> 8), y0 = (int)(py >> 8);
	bk->cx = x0; bk->cy = y0; bk->cvalid = 1;
	for (int y = 0; y < CUR_H; y++) {
		int yy = y0 + y;
		if (yy < 0 || yy >= s->h) continue;
		uint32_t *row = (uint32_t *)(base + (size_t)yy * s->stride);
		for (int x = 0; x < CUR_W; x++) {
			int xx = x0 + x;
			if (xx < 0 || xx >= s->w) continue;
			bk->save[y * CUR_W + x] = row[xx];
			char c = cur_art[y / 2][x / 2];
			if (c == 'X') row[xx] = 0xFF000000u;
			else if (c == 'o') row[xx] = 0xFFFFFFFFu;
		}
	}
}
static void sc_restore(struct scs *s, uint8_t *base, struct backbuf *bk)
{
	for (int y = 0; y < CUR_H; y++) {
		int yy = bk->cy + y;
		if (yy < 0 || yy >= s->h) continue;
		uint32_t *row = (uint32_t *)(base + (size_t)yy * s->stride);
		for (int x = 0; x < CUR_W; x++) {
			int xx = bk->cx + x;
			if (xx < 0 || xx >= s->w) continue;
			row[xx] = bk->save[y * CUR_W + x];
		}
	}
}

/* two shim-owned buffers per toplevel: one memfd, a wl_shm_pool + 2 wl_buffers created on the
 * compositor connection with object ids harvested from the client's finished sync callbacks */
static int sc_setup(struct scs *s, uint32_t w, uint32_t h, uint32_t stride, uint32_t fmt)
{
	if (npool < 3 || !shm_id) return 0;
	size_t one = (size_t)stride * h, total = one * 2;
	int fd = memfd_create("xdgshim", MFD_CLOEXEC);
	if (fd < 0 || ftruncate(fd, total) < 0) { if (fd >= 0) close(fd); return 0; }
	uint8_t *map = mmap(NULL, total, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	if (map == MAP_FAILED) { close(fd); return 0; }
	uint32_t pid = pool[--npool];
	uint32_t msg[4] = { shm_id, (16u << 16) | 0, pid, (uint32_t)total };
	memset(&fdcarrier, 0, sizeof fdcarrier);
	fdcarrier.fds[0] = fd; fdcarrier.nfds = 1;
	out(&srv, &fdcarrier, msg, 16);                 /* wl_shm.create_pool(new id, fd, size); closes fd */
	struct mb b;
	settype(pid, T_BACK);                           /* so its delete_id gives the id back */
	for (int i = 0; i < 2; i++) {
		s->back[i].id = pool[--npool]; s->back[i].map = map + one * i; s->back[i].busy = 0; s->back[i].cvalid = 0;
		settype(s->back[i].id, T_BACK);
		mb_start(&b, pid, 0); mb_u(&b, s->back[i].id); mb_u(&b, (uint32_t)(one * i)); mb_u(&b, w); mb_u(&b, h);
		mb_u(&b, stride); mb_u(&b, fmt); mb_send(&b, &srv);
	}
	mb_start(&b, pid, 1); mb_send(&b, &srv);        /* wl_shm_pool.destroy: buffers stay valid */
	s->base = map; s->total = total; s->pool_id = pid;
	s->w = w; s->h = h; s->stride = stride; s->ready = 1; s->latest = -1;
	D("soft cursor buffers for wl_surface %u: %ux%u stride %u\n", s->surf, w, h, stride);
	return 1;
}

static void sc_wait_free(struct scs *s, int i)
{
	for (int n = 0; n < 60 && s->back[i].busy; n++) {  /* up to ~300 ms for the compositor's release */
		struct pollfd p = { srv.fd, POLLIN, 0 };
		if (poll(&p, 1, 5) > 0 && pump(&srv, &cli, 0) < 0) exit(0);
	}
	s->back[i].busy = 0;
}

/* app_bid != 0: new frame from that app buffer; 0: re-present the last frame (arrow moved) */
static void sc_present(struct scs *s, uint32_t app_bid)
{
	struct mb b;
	if (in_present) return;
	in_present = 1;
	int src = s->latest, tgt = src < 0 ? 0 : 1 - src;
	sc_wait_free(s, tgt);
	size_t sz = (size_t)s->stride * s->h;
	uint8_t *dst = s->back[tgt].map;
	if (app_bid) {
		memcpy(dst, spool[sbuf[app_bid].pool].map + sbuf[app_bid].off, sz);
		mb_start(&b, app_bid, 0); mb_send(&b, &cli);   /* wl_buffer.release: we have our own copy */
	} else {
		memcpy(dst, s->back[src].map, sz);
		if (s->back[src].cvalid) sc_restore(s, dst, &s->back[src]);
	}
	if (clear_all) memset(dst, 0, sz);                 /* XDGSHIM_CLEAR=1 (video tests): whole window transparent */
	s->back[tgt].cvalid = 0;
	if (s == act && sc_visible && dpad_mouse) sc_draw(s, dst, &s->back[tgt]);
	mb_start(&b, s->surf, 1); mb_u(&b, s->back[tgt].id); mb_u(&b, 0); mb_u(&b, 0); mb_send(&b, &srv);
	mb_start(&b, s->surf, 2); mb_u(&b, 0); mb_u(&b, 0); mb_u(&b, s->w); mb_u(&b, s->h); mb_send(&b, &srv);
	mb_start(&b, s->surf, 6); mb_send(&b, &srv);
	s->back[tgt].busy = 1; s->latest = tgt; sc_dirty = 0;
	in_present = 0;
}

/* the app committed toplevel `s` with s->pending attached */
static void sc_commit(struct scs *s)
{
	struct mb b;
	uint32_t bid = s->pending; s->pending = 0;
	if (sbuf[bid].fmt > 1 || sbuf[bid].off + (size_t)sbuf[bid].stride * sbuf[bid].h > spool[sbuf[bid].pool].size) goto passthru;
	if (!s->ready && !sc_setup(s, sbuf[bid].w, sbuf[bid].h, sbuf[bid].stride, sbuf[bid].fmt)) {
		static int ndefer;                             /* retried on every later commit */
		if (ndefer++ % 100 == 0) D("soft cursor setup deferred (spare ids %d, wl_shm %u), retrying on next commit\n", npool, shm_id);
		goto passthru;
	}
	if (sbuf[bid].w != (uint32_t)s->w || sbuf[bid].h != (uint32_t)s->h || sbuf[bid].stride != (uint32_t)s->stride) goto passthru;
	sc_present(s, bid);
	return;
passthru:                                              /* show the app's own buffer, no arrow */
	mb_start(&b, s->surf, 1); mb_u(&b, bid); mb_u(&b, 0); mb_u(&b, 0); mb_send(&b, &srv);
	mb_start(&b, s->surf, 2); mb_u(&b, 0); mb_u(&b, 0); mb_u(&b, 0x7fffffff); mb_u(&b, 0x7fffffff); mb_send(&b, &srv);
	mb_start(&b, s->surf, 6); mb_send(&b, &srv);
	s->latest = -1;
}

/* re-present `s` (arrow added/moved/removed) if it has a frame and a free buffer */
static void sc_refresh(struct scs *s)
{
	if (!s || s->mode != 1 || !s->ready || s->latest < 0) { if (s == act) sc_dirty = 0; return; }
	if (s->back[1 - s->latest].busy) { if (s == act) sc_dirty = 1; return; }   /* retried from dpad_tick / on release */
	sc_present(s, 0);
}
static void sc_cursor_refresh(void) { sc_refresh(act); }

/* pointer focus moves to toplevel `s` (new window opened, or the one above it closed) */
static void sc_activate(struct scs *s)
{
	struct mb m;
	struct scs *old = act;
	if (old == s) return;
	if (ptr_obj && ptr_in && main_surf) {                          /* wl_pointer.leave(serial, surface) */
		mb_start(&m, ptr_obj, 1); mb_u(&m, synth_serial++); mb_u(&m, main_surf); mb_send(&m, &cli);
	}
	ptr_in = 0;                                                    /* re-entered lazily on the next D-pad input */
	act = s; main_surf = s ? s->surf : 0;
	D("active toplevel -> wl_surface %u\n", main_surf);
	if (old && old->used) sc_refresh(old);                         /* erase the arrow there */
	if (s) sc_refresh(s);
}
static struct scs *sc_add(uint32_t surf)
{
	struct scs *s = sc_find(surf);
	if (s) return s;
	for (int i = 0; i < MAXSC; i++) if (!scs[i].used) {
		memset(&scs[i], 0, sizeof scs[i]);
		scs[i].used = 1; scs[i].seq = ++sc_seq; scs[i].surf = surf; scs[i].latest = -1;
		return &scs[i];
	}
	return NULL;
}
/* toplevel destroyed: free its buffers (ids come back through delete_id) */
static void sc_remove(uint32_t surf)
{
	struct scs *s = sc_find(surf);
	if (!s) return;
	struct mb b;
	if (s->ready) {
		for (int i = 0; i < 2; i++) { mb_start(&b, s->back[i].id, 0); mb_send(&b, &srv); }   /* wl_buffer.destroy */
		munmap(s->base, s->total);
	}
	int was_active = s == act;
	s->used = 0;
	if (was_active) { act = NULL; sc_activate(sc_topmost()); }
}

/* ---------- D-pad mouse: simple IR remote -> pointer/touch ----------
 * XDGSHIM_DPAD=mouse (default) | keys.  Arrows move a virtual pointer: the shim sends wl_pointer
 * enter/motion/button to the app itself and draws the arrow (soft cursor above). One press = 24 px,
 * held = accelerating. OK = left button press/release (hold OK + arrows = drag / long-press).
 * CH+/CH- (and PgUp/PgDn) = scroll. RED toggles mouse <-> keys. Other keys pass through. */
#define K_UP 103
#define K_DOWN 108
#define K_LEFT 105
#define K_RIGHT 106
static int held[4];            /* up down left right */
static double held_since[4], last_tick;
static int ok_down, ni_fd = -1;
static int hover_mode = 1;   /* XDGSHIM_HOVER=0: the app only sees the pointer during a click (enter, press, release, leave) */
static double ok_down_at, ok_release_at;   /* OK held at least MIN_TAP: some toolkits drop 1-3 ms taps */
#define MIN_TAP 0.09

/* Pointer moves for the webOS cursor (only used when the app can't be drawn on, e.g. EGL apps). The
 * jailed wrapper can't open /tmp/netinput.pointer.sock (EACCES), so with XDGSHIM_RELAY=<fifo> moves
 * are written as "move dx dy" lines to run/cmd and the root helper atld forwards them. */
static const char *relay;
static int relay_fd = -1;
static void ni_send(const char *txt)
{
	if (ni_fd < 0) {
		struct sockaddr_un sa = { .sun_family = AF_UNIX };
		strcpy(sa.sun_path, "/tmp/netinput.pointer.sock");
		ni_fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
		if (connect(ni_fd, (void *)&sa, sizeof sa) < 0) { D("netinput connect failed: %s\n", strerror(errno)); close(ni_fd); ni_fd = -1; return; }
	}
	uint32_t n = strlen(txt), be = htonl(n);
	if (send(ni_fd, &be, 4, MSG_NOSIGNAL) != 4 || send(ni_fd, txt, n, MSG_NOSIGNAL) != (ssize_t)n) { close(ni_fd); ni_fd = -1; }
}
static void ni_move(int dx, int dy)
{
	char b[96];
	if (relay) {
		if (relay_fd < 0) relay_fd = open(relay, O_WRONLY | O_NONBLOCK | O_CLOEXEC);
		int n = snprintf(b, sizeof b, "move %d %d\n", dx, dy);
		if (relay_fd >= 0 && write(relay_fd, b, n) != n) { close(relay_fd); relay_fd = -1; }
		return;
	}
	snprintf(b, sizeof b, "type:move\ndx:%d\ndy:%d\ndown:0\n\n", dx, dy);
	ni_send(b);
}
static void ptr_ensure_enter(void)
{
	struct mb m;
	if (!ptr_obj || !main_surf || ptr_in) return;
	mb_start(&m, ptr_obj, 0); mb_u(&m, synth_serial++); mb_u(&m, main_surf); mb_u(&m, px); mb_u(&m, py); mb_send(&m, &cli);
	ptr_in = 1;
}
static void vptr_move(int dx, int dy)
{
	struct mb m;
	int ox = (int)(px >> 8), oy = (int)(py >> 8);
	int x = ox + dx, y = oy + dy;
	x = x < 0 ? 0 : x >= W ? W - 1 : x; y = y < 0 ? 0 : y >= H ? H - 1 : y;
	px = (uint32_t)x << 8; py = (uint32_t)y << 8;
	last_dpad = now_s();
	if (ptr_obj && (hover_mode || ok_down)) {
		if (!ptr_in) ptr_ensure_enter();
		else { mb_start(&m, ptr_obj, 2); mb_u(&m, now_ms()); mb_u(&m, px); mb_u(&m, py); mb_send(&m, &cli); }
	}
	if (sc_enabled && act && act->mode != 2) { sc_visible = 1; sc_cursor_refresh(); }
	else if (x != ox || y != oy) ni_move(x - ox, y - oy);   /* EGL app: move the webOS cursor instead */
}
static void ptr_button(int down)
{
	struct mb m;
	if (!ptr_obj) return;
	last_dpad = now_s();
	if (down) { last_click = last_dpad; click_x = (int)(px >> 8); click_y = (int)(py >> 8); ti_click(); } /* text input: a click opens the keyboard */
	if (!ptr_in && ptr_obj && main_surf) ptr_ensure_enter();    /* enter exactly where the click happens */
	else if (down && !hover_mode) { mb_start(&m, ptr_obj, 2); mb_u(&m, now_ms()); mb_u(&m, px); mb_u(&m, py); mb_send(&m, &cli); }
	if (sc_enabled && act && act->mode != 2 && !sc_visible) { sc_visible = 1; sc_cursor_refresh(); }
	mb_start(&m, ptr_obj, 3); mb_u(&m, synth_serial++); mb_u(&m, now_ms()); mb_u(&m, 0x110); mb_u(&m, down); mb_send(&m, &cli);
	if (!down && !hover_mode && main_surf) {                       /* release: pointer leaves again */
		mb_start(&m, ptr_obj, 1); mb_u(&m, synth_serial++); mb_u(&m, main_surf); mb_send(&m, &cli);
		ptr_in = 0;
	}
	D("dpad button %d at %u,%u\n", down, px >> 8, py >> 8);
}
static void ptr_scroll(int dir)
{
	struct mb m;
	if (!ptr_obj) return;
	ptr_ensure_enter();
	/* one press = 3 wheel notches (45 px in GTK terms); XDGSHIM_SCROLL=<notches> overrides */
	static int notches = -1;
	if (notches < 0) notches = getenv("XDGSHIM_SCROLL") ? atoi(getenv("XDGSHIM_SCROLL")) : 3;
	mb_start(&m, ptr_obj, 4); mb_u(&m, now_ms()); mb_u(&m, 0); mb_u(&m, (uint32_t)(dir * 15 * notches * 256)); mb_send(&m, &cli);
	D("scroll %d x%d at %u,%u\n", dir, notches, px >> 8, py >> 8);
}
static int dpad_active(void) { return held[0] || held[1] || held[2] || held[3] || sc_dirty || ok_release_at > 0; }
static void dpad_tick(void)
{
	double t = now_s();
	if (sc_dirty) sc_cursor_refresh();
	if (ok_release_at > 0 && t >= ok_release_at) { ok_release_at = 0; ptr_button(0); }
	if (!(held[0] || held[1] || held[2] || held[3])) { last_tick = t; return; }
	if (t - last_tick < 0.016) return;
	last_tick = t;
	int d[2] = { 0, 0 };
	for (int i = 0; i < 4; i++) if (held[i]) {
		double h = t - held_since[i];
		int v = h < 0.5 ? 0 : h < 0.9 ? 8 : h < 1.6 ? 18 : 30;  /* held: accelerate after a key-repeat-like delay */
		d[i < 2 ? 1 : 0] += (i == 0 || i == 2) ? -v : v;
	}
	if (d[0] || d[1]) vptr_move(d[0], d[1]);
}
/* returns 1 if the key was consumed */
static int dpad_key(uint32_t key, uint32_t state)
{
	if (dbg) fprintf(stderr, "xdgshim: key %u %s\n", key, state ? "down" : "up");
	if (key == 398 && state) {                                  /* RED: toggle mouse / keys */
		dpad_mouse = !dpad_mouse; D("dpad mode %s\n", dpad_mouse ? "mouse" : "keys");
		memset(held, 0, sizeof held);
		if (sc_enabled && act && act->mode == 1) { if (!dpad_mouse) sc_visible = 0; sc_cursor_refresh(); }
		return 1;
	}
	/* CH+ / CH- (and PgUp / PgDn): scroll at the pointer in every mode (key repeat keeps scrolling) */
	if (key == 402 || key == 104) { if (state) ptr_scroll(-1); return 1; }
	if (key == 403 || key == 109) { if (state) ptr_scroll(1); return 1; }
	if (!dpad_mouse) return 0;
	int idx = key == K_UP ? 0 : key == K_DOWN ? 1 : key == K_LEFT ? 2 : key == K_RIGHT ? 3 : -1;
	if (idx >= 0) {
		if (state && !held[idx]) { held[idx] = 1; held_since[idx] = now_s(); vptr_move(idx == 2 ? -24 : idx == 3 ? 24 : 0, idx == 0 ? -24 : idx == 1 ? 24 : 0); /* one press = 24 px */ }
		if (!state) held[idx] = 0;
		return 1;
	}
	if (key == 28 || key == 352 || key == 96) {
		if (state && !ok_down) { ok_down = 1; ok_down_at = now_s(); ok_release_at = 0; ptr_button(1); }
		else if (!state && ok_down) {
			ok_down = 0;
			double wait = ok_down_at + MIN_TAP - now_s();
			if (wait > 0) ok_release_at = now_s() + wait;      /* release later, from dpad_tick */
			else ptr_button(0);
		}
		return 1;
	}
	return 0;
}

static void try_export(void)
{
	struct mb m;
	if (!export_path || exported_id || !foreign_name || !reg_id || !comp_id || !main_surf || npool < 3) return;
	uint32_t fid = pool[--npool], eid = pool[--npool], rid = pool[--npool];
	settype(fid, T_FOREIGN); settype(eid, T_EXPORTED); settype(rid, T_NONE);
	mb_start(&m, reg_id, 0); mb_u(&m, foreign_name); mb_s(&m, "wl_webos_foreign"); mb_u(&m, 1); mb_u(&m, fid); mb_send(&m, &srv);
	mb_start(&m, fid, 1); mb_u(&m, eid); mb_u(&m, main_surf); mb_u(&m, export_type); mb_send(&m, &srv); /* export_element */
	mb_start(&m, comp_id, 1); mb_u(&m, rid); mb_send(&m, &srv);                                   /* create_region */
	mb_start(&m, rid, 1); mb_u(&m, 0); mb_u(&m, 0); mb_u(&m, W); mb_u(&m, H); mb_send(&m, &srv);  /* region.add */
	mb_start(&m, eid, 1); mb_u(&m, rid); mb_u(&m, rid); mb_send(&m, &srv);                        /* set_exported_window */
	exported_id = eid;
	fprintf(stderr, "xdgshim: exported wl_surface %u as webOS element type %u (foreign %u, exported %u)\n", main_surf, export_type, fid, eid);
}

/* <export file>.region "sx sy sw sh dx dy dw dh" (atlvdec: NDL's SDL_webOSSetExportedWindow for the exported ATL
   window) -> set_exported_window(source, destination) with two temporary regions (ids back to the pool on delete_id) */
static void try_region(void)
{
	static double last;
	static char prev[128];
	struct mb m;
	if (!export_path || !exported_id || !comp_id || now_s() - last < 0.25) return;
	last = now_s();
	char path[512], b[128] = "";
	snprintf(path, sizeof path, "%s.region", export_path);
	FILE *f = fopen(path, "r");
	if (!f) return;
	if (!fgets(b, sizeof b, f)) b[0] = 0;
	fclose(f);
	int r[8];
	if (!strcmp(b, prev) || sscanf(b, "%d %d %d %d %d %d %d %d", &r[0], &r[1], &r[2], &r[3], &r[4], &r[5], &r[6], &r[7]) != 8 || npool < 2) return;
	snprintf(prev, sizeof prev, "%s", b);
	uint32_t rid[2];
	rid[0] = pool[--npool]; rid[1] = pool[--npool];
	for (int i = 0; i < 2; i++) {
		settype(rid[i], T_BACK);
		mb_start(&m, comp_id, 1); mb_u(&m, rid[i]); mb_send(&m, &srv);                                                 /* create_region */
		mb_start(&m, rid[i], 1); mb_u(&m, r[4 * i]); mb_u(&m, r[4 * i + 1]); mb_u(&m, r[4 * i + 2]); mb_u(&m, r[4 * i + 3]); mb_send(&m, &srv); /* add */
	}
	mb_start(&m, exported_id, 1); mb_u(&m, rid[0]); mb_u(&m, rid[1]); mb_send(&m, &srv);                              /* set_exported_window */
	for (int i = 0; i < 2; i++) { mb_start(&m, rid[i], 0); mb_send(&m, &srv); }                                       /* region.destroy */
	fprintf(stderr, "xdgshim: exported window region src %d,%d %dx%d dst %d,%d %dx%d\n", r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]);
}

static void try_import(void)
{
	static double last;
	struct mb m;
	if (!import_path || !reg_id || !foreign_name || !main_surf || now_s() - last < 0.5) return;
	last = now_s();
	char wid[128] = "";
	FILE *f = fopen(import_path, "r");
	if (!f) return;
	if (!fgets(wid, sizeof wid, f)) wid[0] = 0;
	fclose(f);
	wid[strcspn(wid, "\r\n")] = 0;
	if (!wid[0] || !strcmp(wid, imported_wid)) return;
	if (npool < (foreign_id ? 1 : 2)) return;
	if (!foreign_id) {
		foreign_id = pool[--npool]; settype(foreign_id, T_FOREIGN);
		mb_start(&m, reg_id, 0); mb_u(&m, foreign_name); mb_s(&m, "wl_webos_foreign"); mb_u(&m, 1); mb_u(&m, foreign_id); mb_send(&m, &srv);
	}
	if (imported_id) { mb_start(&m, imported_id, 3); mb_send(&m, &srv); mb_start(&m, imported_id, 1); mb_send(&m, &srv); } /* detach, destroy */
	imported_id = pool[--npool]; settype(imported_id, T_IMPORTED);
	snprintf(imported_wid, sizeof imported_wid, "%s", wid);
	mb_start(&m, foreign_id, 2); mb_u(&m, imported_id); mb_s(&m, wid); mb_u(&m, 0); mb_send(&m, &srv);     /* import_element(id, window_id, VIDEO_OBJECT) */
	const char *ctx = getenv("XDGSHIM_PUNCH_CTX");
	mb_start(&m, imported_id, 2); mb_s(&m, ctx ? ctx : ""); mb_send(&m, &srv);                            /* attach_punchthrough */
	if (getenv("XDGSHIM_IMPORT_SURFACE")) { mb_start(&m, imported_id, 4); mb_u(&m, main_surf); mb_send(&m, &srv); }
	fprintf(stderr, "xdgshim: imported video element %s into wl_surface %u (imported %u), punch-through attached\n", wid, main_surf, imported_id);
}

/* ---------- text input: GTK's zwp_text_input_v3 -> webOS text_model (the LG on-screen keyboard) ----------
 * The compositor has no zwp_text_input_manager_v3, so GTK never asks for a keyboard. The shim advertises a fake
 * one (v1) next to text_model_factory. Binding it binds text_model_factory at the same id and get_text_input(id)
 * becomes create_text_model(id): the server must see every client id in order (a gap is "invalid arguments"),
 * so the text_input and our text_model share one id. text_model has no destructor, so a destroyed text_input's
 * id is never given back (no delete_id). Requests are translated (text.xml of webOS TV 9/10: activate=1
 * deactivate=2 set_content_type=5 commit=8 show_input_panel=11 hide_input_panel=12):
 *   enable+commit right after a click -> text_model.activate + show_input_panel (focus without a click, e.g. a
 *   dialog's first field, only marks the field focused), disable+commit -> hide_input_panel + deactivate;
 *   text_model commit_string / preedit_string / delete_surrounding_text -> the same text_input events + done;
 *   keysym: printable -> commit_string, BackSpace/Return/arrows/... -> wl_keyboard keys (Return also hides).
 * Phone typing (main client only): <prefix>.state is rewritten on every change (focused, shown, purpose,
 * cursor, text as hex); <prefix>.in is a FIFO of "<replace 0|1> <enter 0|1> <hex utf-8>" lines that go into
 * the focused field. prefix = XDGSHIM_TI, default <dir of XDGSHIM_RELAY>/ti. XDGSHIM_TEXTINPUT=0: all off. */
#define FAKE_TI 0xFFFF0A72u
static int ti_enabled = 1;
static uint32_t tmf_name, tmf_obj, tm_id, tm_serial; /* compositor: text_model_factory global + object (= manager id), current text_model */
static int tm_active, tm_shown;
static double tm_closed_at;                          /* BACK that closed the keyboard is not passed on */
static double tm_idle_at;                            /* panel hidden by the compositor (BACK): deactivate then */
static uint32_t ti_id, ti_seat, ti_serial;           /* client: newest zwp_text_input_v3, its seat, commits so far */
static int ti_entered, ti_on, ti_pend_on = -1, ti_dirty;
static char ti_text[4100]; static uint32_t ti_cursor, ti_hint, ti_purpose;
static int ti_rect[4];                               /* caret rectangle (surface coordinates) */
static uint32_t kbd_focus, kbd_surf;                 /* client wl_keyboard holding the focus, and its surface */
static int kbd_swallowed;                            /* keyboard leave hidden from the client while the VKB is up */
static char ti_prefix[400]; static int ti_fifo = -1;

static void ti_state(void)
{
	char p[420], tmp[430];
	if (!ti_prefix[0]) return;
	snprintf(p, sizeof p, "%s.state", ti_prefix); snprintf(tmp, sizeof tmp, "%s.tmp", p);
	FILE *f = fopen(tmp, "w");
	if (!f) return;
	fprintf(f, "focused=%d\nshown=%d\npurpose=%u\nhint=%u\ncursor=%u\npid=%d\ntext=", ti_on && ti_id, tm_shown, ti_purpose, ti_hint, ti_cursor, (int)getpid());
	for (const unsigned char *c = (const unsigned char *)ti_text; *c; c++) fprintf(f, "%02x", *c);
	fputs("\n", f); fclose(f); chmod(tmp, 0666); rename(tmp, p);
}
static void ti_done(void) { struct mb b; mb_start(&b, ti_id, 5); mb_u(&b, ti_serial); mb_send(&b, &cli); }
static void ti_commit_str(const char *s) { struct mb b; mb_start(&b, ti_id, 3); mb_s(&b, s); mb_send(&b, &cli); }
static void ti_key(uint32_t key, uint32_t state)       /* synthetic wl_keyboard.key to the focused keyboard */
{
	struct mb b;
	if (!kbd_focus) return;
	mb_start(&b, kbd_focus, 3); mb_u(&b, synth_serial++); mb_u(&b, now_ms()); mb_u(&b, key); mb_u(&b, state); mb_send(&b, &cli);
}
static void tm_hide(void)
{
	struct mb b;
	if (!tm_id || !tm_active) return;
	mb_start(&b, tm_id, 12); mb_send(&b, &srv);                         /* hide_input_panel */
	mb_start(&b, tm_id, 2); mb_u(&b, ti_seat); mb_send(&b, &srv);        /* deactivate(seat) */
	tm_active = tm_shown = 0;
	tm_id = 0;                      /* the compositor destroys a text_model after deactivate ("invalid object" on reuse) */
	D("text: keyboard hidden\n");
}
static void tm_show(void)
{
	struct mb b;
	uint32_t surf = kbd_surf ? kbd_surf : main_surf;
	if (!tm_id && tmf_obj && npool > 0) {                               /* a fresh text_model for every activation */
		tm_id = pool[--npool]; settype(tm_id, T_TM); tm_active = 0;
		mb_start(&b, tmf_obj, 0); mb_u(&b, tm_id); mb_send(&b, &srv);    /* create_text_model */
	}
	if (!tm_id || !ti_seat || !surf) { D("text: no text_model (seat %u, surf %u)\n", ti_seat, surf); return; }
	mb_start(&b, tm_id, 5); mb_u(&b, ti_hint); mb_u(&b, ti_purpose > 8 ? (ti_purpose == 9 ? 2 : ti_purpose - 1) : ti_purpose); mb_send(&b, &srv);
	if (!tm_active) { mb_start(&b, tm_id, 1); mb_u(&b, ++tm_serial); mb_u(&b, ti_seat); mb_u(&b, surf); mb_send(&b, &srv); } /* activate */
	mb_start(&b, tm_id, 8); mb_send(&b, &srv);                           /* commit */
	mb_start(&b, tm_id, 11); mb_send(&b, &srv);                          /* show_input_panel */
	tm_active = tm_shown = 1; tm_idle_at = 0;
	D("text: keyboard shown (seat %u, wl_surface %u, purpose %u)\n", ti_seat, surf, ti_purpose);
}
/* the compositor hid the panel itself (BACK on the keyboard) but the text_model stays active and keeps the remote's
   keys (arrows/OK went to the hidden keyboard, OK reopened it): deactivate unless it came back within 0.4 s */
static void tm_idle_check(void)
{
	if (!tm_idle_at || now_s() < tm_idle_at) return;
	tm_idle_at = 0;
	if (tm_active && !tm_shown) { D("text: panel closed by the compositor, deactivating\n"); tm_hide(); ti_state(); }
}
static void ti_apply(void)                                               /* client text_input.commit */
{
	if (ti_pend_on >= 0) D("text: %s (%.1f s after a click at y %d, caret y %d h %d)\n", ti_pend_on ? "enable" : "disable", now_s() - last_click, click_y, ti_rect[1], ti_rect[3]);
	if (ti_pend_on == 1) {
		ti_on = 1;
		/* clicked on the field: open the keyboard (a click that only opened a dialog whose field takes the focus
		   is elsewhere: the caret is more than 80 px above/below the click) */
		int cy = ti_rect[1] + ti_rect[3] / 2;
		if (now_s() - last_click < 1.5 && (!ti_rect[3] || abs(cy - click_y) < 80)) tm_show();
	} else if (ti_pend_on == 0) {
		ti_on = 0; tm_hide();
	}
	ti_pend_on = -1;
	ti_state();
	ti_dirty = 0;
}
/* a click on the already focused field (GTK asks for the keyboard again only for touch) */
static void ti_click(void)
{
	if (!ti_id || !ti_on || tm_shown || !ti_rect[3]) return;
	if (abs(ti_rect[1] + ti_rect[3] / 2 - click_y) < 40 && click_x > ti_rect[0] - 200 && click_x < ti_rect[0] + 1200) {
		D("text: click on the focused field\n");
		tm_show(); ti_state();
	}
}
static void ti_enter(void)
{
	struct mb b;
	if (!ti_id || ti_entered || !kbd_surf) return;
	mb_start(&b, ti_id, 0); mb_u(&b, kbd_surf); mb_send(&b, &cli);       /* text_input.enter(surface) */
	ti_entered = 1;
}
static int utf8_put(char *o, uint32_t u)
{
	if (u < 0x80) { o[0] = u; return 1; }
	if (u < 0x800) { o[0] = 0xc0 | u >> 6; o[1] = 0x80 | (u & 63); return 2; }
	if (u < 0x10000) { o[0] = 0xe0 | u >> 12; o[1] = 0x80 | (u >> 6 & 63); o[2] = 0x80 | (u & 63); return 3; }
	o[0] = 0xf0 | u >> 18; o[1] = 0x80 | (u >> 12 & 63); o[2] = 0x80 | (u >> 6 & 63); o[3] = 0x80 | (u & 63); return 4;
}
/* text_model events (keyboard -> app) */
static void tm_event(uint32_t op, const uint32_t *a)
{
	struct mb b;
	uint32_t w;
	if (!ti_id) return;
	if (op == 0) {                                                       /* commit_string(serial, text) */
		const char *s = str_arg(a + 1, &w);
		D("text: commit_string '%s'\n", s);
		if (!strcmp(s, "\n") || !strcmp(s, "\r")) { ti_key(28, 1); ti_key(28, 0); tm_hide(); ti_state(); return; }
		ti_commit_str(s); ti_done();
	} else if (op == 1) {                                                /* preedit_string(serial, text, commit) */
		const char *s = str_arg(a + 1, &w);
		mb_start(&b, ti_id, 2); mb_s(&b, s); mb_u(&b, strlen(s)); mb_u(&b, strlen(s)); mb_send(&b, &cli);
		ti_done();
	} else if (op == 2) {                                                /* delete_surrounding_text(serial, index, length) */
		int32_t idx = (int32_t)a[1]; uint32_t len = a[2];
		uint32_t before = idx < 0 ? (uint32_t)-idx : 0, after = len > before ? len - before : 0;
		D("text: delete_surrounding %d %u\n", idx, len);
		mb_start(&b, ti_id, 4); mb_u(&b, before); mb_u(&b, after); mb_send(&b, &cli);
		ti_done();
	} else if (op == 7) {                                                /* keysym(serial, time, sym, state, modifiers) */
		uint32_t sym = a[2], st = a[3], key = 0;
		D("text: keysym 0x%x state %u\n", sym, st);
		switch (sym) {
		case 0xff08: key = 14; break;  case 0xff0d: case 0xff8d: key = 28; break;  case 0xff09: key = 15; break;
		case 0xff1b: key = 1; break;   case 0xffff: key = 111; break; case 0xff50: key = 102; break; case 0xff57: key = 107; break;
		case 0xff51: case 0xff52: case 0xff53: case 0xff54: return;  /* arrows: the remote left the keyboard's edge */
		}
		if (key) { ti_key(key, st); if (key == 28 && !st) { tm_hide(); ti_state(); } return; }
		uint32_t u = (sym >= 0x20 && sym <= 0xff) ? sym : (sym & 0xff000000) == 0x01000000 ? sym & 0xffffff : 0;
		if (u && st) { char s[8] = { 0 }; utf8_put(s, u); ti_commit_str(s); ti_done(); }
	} else if (op == 10) {                                               /* input_panel_state(state) */
		D("text: input_panel_state %u\n", a[0]);
		if (!!a[0] != tm_shown) { tm_shown = !!a[0]; ti_state(); }
		if (!a[0]) tm_closed_at = now_s(), tm_idle_at = now_s() + 0.4;
		else tm_idle_at = 0;
	} else if (op == 9) {                                                /* leave: this text_model is done, the next show makes a new one */
		D("text: text_model %u left\n", tm_id);
		tm_id = 0; tm_active = tm_shown = 0; tm_closed_at = now_s(); ti_state();
	} else if (op == 11) D("text: input_panel_rect %d,%d %ux%u\n", (int)a[0], (int)a[1], a[2], a[3]);
	else D("text: text_model event %u\n", op);
}
/* phone typing: "<replace> <enter> <hex>" lines from <prefix>.in */
static void ti_fifo_read(void)
{
	static char line[20000]; static size_t ll;
	char buf[4096];
	ssize_t r = read(ti_fifo, buf, sizeof buf);
	for (ssize_t i = 0; i < r; i++) {
		if (buf[i] != '\n') { if (ll < sizeof line - 1) line[ll++] = buf[i]; continue; }
		line[ll] = 0; ll = 0;
		int rep = 0, ent = 0, n = 0;
		if (sscanf(line, "%d %d %n", &rep, &ent, &n) < 2) continue;
		static char txt[10000]; size_t tl = 0;
		for (const char *h = line + n; h[0] && h[1] && tl < sizeof txt - 1; h += 2) { unsigned v; if (sscanf(h, "%2x", &v) != 1) break; txt[tl++] = v; }
		txt[tl] = 0;
		D("text: phone input replace=%d enter=%d %zu bytes (field %s)\n", rep, ent, tl, ti_on ? "focused" : "not focused");
		if (!ti_id || !ti_on) continue;
		struct mb b;
		size_t cur = strlen(ti_text);
		if (rep && cur) { mb_start(&b, ti_id, 4); mb_u(&b, ti_cursor > cur ? cur : ti_cursor); mb_u(&b, ti_cursor > cur ? 0 : cur - ti_cursor); mb_send(&b, &cli); }
		if (rep && cur) ti_done();
		for (size_t o = 0; o < tl;) {                                    /* <= 1000 bytes per message, on UTF-8 boundaries */
			size_t e = o + 1000 < tl ? o + 1000 : tl;
			while (e < tl && ((unsigned char)txt[e] & 0xc0) == 0x80) e--;
			char c = txt[e]; txt[e] = 0; ti_commit_str(txt + o); ti_done(); txt[e] = c; o = e;
		}
		if (ent) { ti_key(28, 1); ti_key(28, 0); tm_hide(); ti_state(); }
	}
}
static void ti_open(void)
{
	const char *p = getenv("XDGSHIM_TI");
	if (!ti_enabled) return;
	if (p && *p) snprintf(ti_prefix, sizeof ti_prefix, "%s", p);
	else if (relay && strrchr(relay, '/')) snprintf(ti_prefix, sizeof ti_prefix, "%.*s/ti", (int)(strrchr(relay, '/') - relay), relay);
	if (!ti_prefix[0]) return;
	char f[420]; snprintf(f, sizeof f, "%s.in", ti_prefix);
	struct stat st;
	if (stat(f, &st) < 0 || !S_ISFIFO(st.st_mode)) { unlink(f); mkfifo(f, 0666); }
	chmod(f, 0666);
	ti_fifo = open(f, O_RDWR | O_NONBLOCK | O_CLOEXEC);
	ti_state();
	D("text: phone typing on %s (fd %d)\n", f, ti_fifo);
}
/* client requests on the fake objects; never forwarded */
static void ti_request(int t, uint32_t obj, uint32_t op, const uint32_t *a)
{
	struct mb b;
	uint32_t w;
	if (op == 0 && (t == T_TIM || t == T_TI)) {                         /* destroy: the server objects stay (ids kept) */
		if (obj == ti_id) { tm_hide(); ti_id = 0; ti_on = 0; ti_state(); }
		return;                                                          /* type kept: its events are dropped */
	}
	if (t == T_TIM) {                                                    /* get_text_input(id, seat) -> create_text_model(id) */
		if (op != 1) return;
		mb_start(&b, obj, 0); mb_u(&b, a[0]); mb_send(&b, &srv);
		if (ti_id && ti_id != a[0]) tm_hide();
		tmf_obj = obj; settype(a[0], T_TI); ti_id = tm_id = a[0]; ti_seat = a[1]; ti_serial = 0; ti_entered = 0; ti_on = 0; tm_active = tm_shown = 0;
		D("text: zwp_text_input_v3 %u on seat %u\n", ti_id, ti_seat);
		ti_enter();
		return;
	}
	if (obj != ti_id) return;
	switch (op) {
	case 1: ti_pend_on = 1; break;                                       /* enable */
	case 2: ti_pend_on = 0; break;                                       /* disable */
	case 3: {                                                            /* set_surrounding_text(text, cursor, anchor) */
		const char *s = str_arg(a, &w);
		size_t sl = strnlen(s, sizeof ti_text - 1); memcpy(ti_text, s, sl); ti_text[sl] = 0; ti_cursor = a[w]; ti_dirty = 1;
		break; }
	case 5: ti_hint = a[0]; ti_purpose = a[1]; break;                    /* set_content_type(hint, purpose) */
	case 6: memcpy(ti_rect, a, sizeof ti_rect); break;                   /* set_cursor_rectangle(x, y, w, h) */
	case 7: ti_serial++; ti_apply(); break;                              /* commit */
	}
}

static void make_toplevel(uint32_t top, uint32_t xs)
{
	struct mb m;
	uint32_t surf = surf_of[xs];
	surf_of[top] = surf; xsurf_of[top] = xs;
	{ struct scs *ns = sc_add(surf); if (sc_enabled && ns) sc_activate(ns); else if (!main_surf) main_surf = surf; }
	mb_start(&m, xs, 3); mb_send(&m, &srv); /* wl_shell_surface.set_toplevel */
	if (webos_id) {
		mb_start(&m, webos_id, 1); mb_u(&m, top); mb_u(&m, surf); mb_send(&m, &srv);
		mb_start(&m, top, 2); mb_s(&m, "appId"); mb_s(&m, appid); mb_send(&m, &srv);
		try_export();
		{	/* first toplevel = the app card; later ones (dialogs, sheets) use XDGSHIM_DIALOG_TYPE if set */
			static int ntop;
			const char *wt = "_WEBOS_WINDOW_TYPE_CARD";
			if (ntop++ > 0 && getenv("XDGSHIM_DIALOG_TYPE")) wt = getenv("XDGSHIM_DIALOG_TYPE");
			mb_start(&m, top, 2); mb_s(&m, "_WEBOS_WINDOW_TYPE"); mb_s(&m, wt); mb_send(&m, &srv);
			D("toplevel window type %s\n", wt);
		}
		/* BACK goes to the app (ATL maps it to onBackPressed) instead of webOS's "exit app?" prompt */
		mb_start(&m, top, 2); mb_s(&m, "_WEBOS_ACCESS_POLICY_KEYS_BACK"); mb_s(&m, "true"); mb_send(&m, &srv);
	} else {
		fprintf(stderr, "xdgshim: toplevel %u before wl_webos_shell was bound; no webos surface\n", top);
		dummy_region(top);
	}
	/* xdg_toplevel.configure(w, h, [fullscreen=2, activated=4]) then xdg_surface.configure */
	uint32_t st[2] = { 2, 4 };
	mb_start(&m, top, 0); mb_u(&m, W); mb_u(&m, H); mb_a(&m, st, 2); mb_send(&m, &cli);
	mb_start(&m, xs, 0); mb_u(&m, serial++); mb_send(&m, &cli);
	D("toplevel %u (xdg_surface %u, wl_surface %u) appId=%s\n", top, xs, surf, appid);
}

/* client -> server. return 1 if forwarded verbatim. */
static int on_request(const uint32_t *m, uint32_t size)
{
	uint32_t obj = m[0], op = m[1] & 0xffff;
	const uint32_t *a = m + 2;
	struct mb b;
	int t = gettype(obj);

	if (obj == 1 && op == 0) { settype(a[0], T_CB); return 1; }           /* wl_display.sync */
	if (obj == 1 && op == 1) { settype(a[0], T_REG); reg_id = a[0]; return 1; } /* get_registry */
	if (t == T_REG && op == 0) {                                          /* wl_registry.bind */
		uint32_t name = a[0], w;
		const char *iface = str_arg(a + 1, &w);
		uint32_t ver = a[1 + w], id = a[2 + w];
		if (name == FAKE_NAME) {
			settype(id, T_XWM);
			mb_start(&b, obj, 0); mb_u(&b, shell_name); mb_s(&b, "wl_shell"); mb_u(&b, 1); mb_u(&b, id);
			mb_send(&b, &srv);
			D("xdg_wm_base bound as wl_shell id %u\n", id);
			return 0;
		}
		if (name == FAKE_TI) {                                            /* -> text_model_factory at the same id */
			settype(id, T_TIM);
			mb_start(&b, obj, 0); mb_u(&b, tmf_name); mb_s(&b, "text_model_factory"); mb_u(&b, 1); mb_u(&b, id); mb_send(&b, &srv);
			D("text: zwp_text_input_manager_v3 bound as text_model_factory %u\n", id);
			return 0;
		}
		if (!strcmp(iface, "wl_compositor")) { comp_id = id; settype(id, T_COMP); }
		if (!strcmp(iface, "wl_seat")) settype(id, T_SEAT);
		if (!strcmp(iface, "wl_shm")) { settype(id, T_SHM); shm_id = id; }
		if (!strcmp(iface, "wl_subcompositor")) { settype(id, T_SUBC); subc_id = id; }
		(void)ver;
		return 1;
	}
	/* XDGSHIM_NOOPAQUE=1: drop wl_surface.set_opaque_region so the compositor blends the window's alpha
	   (video plane below a transparent hole punched by ATL) */
	if (no_opaque && t == T_SURF && op == 4) { D("dropped set_opaque_region on wl_surface %u\n", obj); return 0; }
	/* XDGSHIM_ALPHA=1: log format and centre/corner pixels of attached shm frames (every 2 s) */
	if (alpha_dbg && t == T_SURF && op == 1 && a[0] && a[0] < MAXID && sbuf[a[0]].pool && spool[sbuf[a[0]].pool].map) {
		static time_t last;
		time_t now = time(NULL);
		uint32_t bid = a[0];
		if (now - last >= 2 && sbuf[bid].off + (size_t)sbuf[bid].stride * sbuf[bid].h <= spool[sbuf[bid].pool].size) {
			last = now;
			const uint8_t *px = spool[sbuf[bid].pool].map + sbuf[bid].off;
			uint32_t w = sbuf[bid].w, h = sbuf[bid].h, st = sbuf[bid].stride, c, k;
			memcpy(&c, px + (size_t)st * (h / 2) + (w / 2) * 4, 4);
			const char *xy = getenv("XDGSHIM_ALPHA_XY"); /* "x,y" probe point, default top-left */
			uint32_t px_x = 10, px_y = 0;
			if (xy) sscanf(xy, "%u,%u", &px_x, &px_y);
			if (px_x >= w) px_x = w - 1;
			if (px_y >= h) px_y = h - 1;
			memcpy(&k, px + (size_t)st * px_y + px_x * 4, 4);
			fprintf(stderr, "xdgshim: surf %u buf %u fmt %u %ux%u centre %08x probe %08x\n", obj, bid, sbuf[bid].fmt, w, h, c, k);
		}
	}
	/* toplevel surfaces: composite the soft cursor into shm frames (see softcur.inc) */
	struct scs *ss = (sc_enabled && t == T_SURF) ? sc_find(obj) : NULL;
	if (ss) {
		if (op == 1) {                                                /* attach(buffer, x, y) */
			uint32_t bid = a[0];
			if (!bid) { ss->pending = 0; return 1; }
			if (bid < MAXID && sbuf[bid].pool && spool[sbuf[bid].pool].map) { ss->mode = 1; ss->pending = bid; return 0; }
			if (ss->mode != 2) D("wl_surface %u: buffer %u is not shm (pool %u), no soft cursor\n", obj, bid, bid < MAXID ? sbuf[bid].pool : 0);
			ss->mode = 2; ss->pending = 0; return 1;                 /* EGL / unknown buffer: untouched */
		}
		if (ss->mode == 1 && (op == 2 || op == 9)) return 0;        /* damage: we damage the whole frame */
		if (ss->mode == 1 && op == 6 && ss->pending) { sc_commit(ss); return 0; }
		if (op == 0) sc_remove(obj);                                 /* wl_surface.destroy: forwarded below */
	}
	/* wl_surface.attach(buffer, x, y) on a popup surface: attach NULL instead. LSM would show any
	   mapped wl_shell_surface as its own appId-less card, hiding the app (and SAM then raises Home). */
	if (t == T_SURF && op == 1 && size == 20 && (hidden_surf[obj] || !allowed_surf[obj])) {
		last_buf[obj] = a[0];
		mb_start(&b, obj, 1); mb_u(&b, 0); mb_u(&b, 0); mb_u(&b, 0); mb_send(&b, &srv);
		return 0;
	}
	if (dbg && t == T_SURF && op == 1 && size == 20 && a[0] && obj != main_surf && obj < MAXID && !dbg_att[obj]) {
		dbg_att[obj] = 1; D("%.2f first buffer on wl_surface %u (not the main one)\n", now_s(), obj);
	}
	if (t == T_SURF && op == 3) settype(a[0], T_CB);                 /* wl_surface.frame: id harvestable too */
	if (t == T_TIM || t == T_TI) { ti_request(t, obj, op, a); return 0; }
	switch (t) {
	case T_COMP:
		if (op == 0) { settype(a[0], T_SURF); allowed_surf[a[0]] = 0; hidden_surf[a[0]] = 0; dbg_att[a[0]] = 0; D("%.2f create_surface %u\n", now_s(), a[0]); } /* create_surface */
		return 1;
	case T_SUBC:
		if (op == 1 && a[1] < MAXID) allowed_surf[a[1]] = 1;          /* get_subsurface(id, surface, parent) */
		if (op == 1 && no_subsurf && a[1] < MAXID) { hidden_surf[a[1]] = 1; allowed_surf[a[1]] = 0; } /* XDGSHIM_SUBSURF=0 */
		if (op == 1) D("%.2f subsurface %u: wl_surface %u on %u\n", now_s(), a[0], a[1], a[2]);
		return 1;
	case T_SEAT:
		if (op == 0) { settype(a[0], T_PTR); if (!ptr_obj) ptr_obj = a[0]; } /* get_pointer */
		if (op == 1) settype(a[0], T_KBD);                           /* get_keyboard */
		return 1;
	case T_PTR:
		/* set_cursor: the shim draws the arrow itself; a client cursor surface mapped by LSM would
		   become an appId-less card that minimizes the app. Drop it and keep that surface unmapped. */
		if (op == 0) { if (a[1] && a[1] < MAXID) hidden_surf[a[1]] = 1; return 0; }
		return 1;
	case T_SHM:
		if (op == 0 && ncfdq > fd_used) {                            /* create_pool(id, fd, size) */
			int fd = cfdq[fd_used++];
			uint32_t pid = a[0], sz = a[1];
			settype(pid, T_POOL);
			if (pid < MAXID) {
				if (spool[pid].map) munmap(spool[pid].map, spool[pid].size);
				if (spool[pid].fd > 0) close(spool[pid].fd);
				void *mp = mmap(NULL, sz, PROT_READ, MAP_SHARED, fd, 0);
				spool[pid].map = mp == MAP_FAILED ? NULL : mp;
				spool[pid].size = sz; spool[pid].fd = dup(fd);
			}
		}
		return 1;
	case T_POOL:
		if (op == 0 && a[0] < MAXID) {                               /* create_buffer(id, off, w, h, stride, fmt) */
			settype(a[0], T_BUF);
			sbuf[a[0]].pool = obj; sbuf[a[0]].off = a[1]; sbuf[a[0]].w = a[2]; sbuf[a[0]].h = a[3];
			sbuf[a[0]].stride = a[4]; sbuf[a[0]].fmt = a[5];
		} else if (op == 2 && obj < MAXID && spool[obj].fd > 0) {   /* resize(size) */
			if (spool[obj].map) munmap(spool[obj].map, spool[obj].size);
			void *mp = mmap(NULL, a[0], PROT_READ, MAP_SHARED, spool[obj].fd, 0);
			spool[obj].map = mp == MAP_FAILED ? NULL : mp; spool[obj].size = a[0];
		}
		return 1;
	case T_BUF:
		if (op == 0 && obj < MAXID) memset(&sbuf[obj], 0, sizeof sbuf[obj]);   /* destroy */
		return 1;
	case T_XWM:
		if (op == 1) { settype(a[0], T_XPOS); dummy_region(a[0]); }   /* create_positioner */
		else if (op == 2) {                                          /* get_xdg_surface */
			settype(a[0], T_XSURF); surf_of[a[0]] = a[1]; D("%.2f xdg_surface %u on wl_surface %u\n", now_s(), a[0], a[1]);
			if (a[1] < MAXID) allowed_surf[a[1]] = 1;
			mb_start(&b, obj, 0); mb_u(&b, a[0]); mb_u(&b, a[1]); mb_send(&b, &srv);
		}
		return 0; /* destroy, pong: drop */
	case T_XSURF:
		if (op == 1) { settype(a[0], T_XTOP); make_toplevel(a[0], obj); }
		else if (op == 2) {                                          /* get_popup: keep its surface unmapped */
			settype(a[0], T_XPOP); dummy_region(a[0]);
			if (surf_of[obj] < MAXID) { hidden_surf[surf_of[obj]] = 1; allowed_surf[surf_of[obj]] = 0; }
			D("popup %u on wl_surface %u hidden\n", a[0], surf_of[obj]);
		}
		return 0;
	case T_XTOP:
		if (op != 2) D("%.2f xdg_toplevel %u request %u (dropped)\n", now_s(), obj, op);
		if (op == 2 && size > 12) {                                   /* set_title */
			uint32_t w;
			mb_start(&b, xsurf_of[obj], 8); mb_s(&b, str_arg(a, &w)); mb_send(&b, &srv);
		}
		return 0;
	case T_XPOS:
	case T_XPOP:
		if (op == 0) { mb_start(&b, obj, 0); mb_send(&b, &srv); }  /* destroy -> wl_region.destroy */
		return 0;
	}
	return 1;
}

/* server -> client. return 1 if forwarded verbatim. */
static int on_event(const uint32_t *m, uint32_t size)
{
	uint32_t obj = m[0], op = m[1] & 0xffff;
	const uint32_t *a = m + 2;
	struct mb b;
	int t = gettype(obj);
	(void)size;

	if (obj == 1 && op == 0) {                                            /* wl_display.error */
		uint32_t w; fprintf(stderr, "xdgshim: COMPOSITOR ERROR obj %u code %u: %s\n", a[0], a[1], str_arg(a + 2, &w));
		return 1;
	}
	if (obj == 1 && op == 1) {                                            /* wl_display.delete_id */
		uint32_t id = a[0];
		if (gettype(id) == T_TI) return 0;               /* text_model behind a live client text_input: keep the client's id */
		if (gettype(id) == T_TM) { if (id == tm_id) tm_id = 0, tm_active = tm_shown = 0; settype(id, T_BACK); }
		if (gettype(id) == T_BACK) {                      /* id of one of our own objects: back to the spare pool */
			settype(id, T_NONE); if (npool < 16) pool[npool++] = id;
			return 0;
		}
		if (gettype(id) == T_CB && webos_id && npool < 16) {         /* keep a few spare ids for the shim */
			pool[npool++] = id; settype(id, T_NONE);
			try_export();
			return 0;
		}
		if (gettype(id) == T_CB && !webos_id && webos_name && reg_id) {
			webos_id = id; settype(id, T_NONE);
			mb_start(&b, reg_id, 0); mb_u(&b, webos_name); mb_s(&b, "wl_webos_shell");
			mb_u(&b, webos_ver); mb_u(&b, id); mb_send(&b, &srv);
			D("wl_webos_shell bound at harvested id %u\n", id);
			return 0;
		}
		if (id < MAXID) { type_[id] = T_NONE; hidden_surf[id] = 0; }
		return 1;
	}
	if (t == T_REG && op == 0) {                                          /* wl_registry.global */
		uint32_t w;
		const char *iface = str_arg(a + 1, &w);
		if (!strcmp(iface, "wl_webos_shell")) { webos_name = a[0]; webos_ver = a[1 + w] > 1 ? 1 : a[1 + w]; }
		if (!strcmp(iface, "wl_webos_foreign")) foreign_name = a[0];
		if (!strcmp(iface, "text_model_factory") && ti_enabled) {
			tmf_name = a[0];
			out(&cli, &srv, m, size);
			mb_start(&b, obj, 0); mb_u(&b, FAKE_TI); mb_s(&b, "zwp_text_input_manager_v3"); mb_u(&b, 1); mb_send(&b, &cli);
			return 0;
		}
		if (!strcmp(iface, "wl_shell")) {
			shell_name = a[0];
			out(&cli, &srv, m, size);
			mb_start(&b, obj, 0); mb_u(&b, FAKE_NAME); mb_s(&b, "xdg_wm_base"); mb_u(&b, 1); mb_send(&b, &cli);
			return 0;
		}
		return 1;
	}
	if (t == T_EXPORTED) {                                                /* window_id_assigned(window_id, type) */
		uint32_t w;
		const char *wid = str_arg(a, &w);
		fprintf(stderr, "xdgshim: exported window id %s (type %u)\n", wid, a[w]);
		if (op == 0 && export_path) {
			char tmp[160]; snprintf(tmp, sizeof tmp, "%s.tmp", export_path);
			FILE *f = fopen(tmp, "w");
			if (f) { fputs(wid, f); fclose(f); chmod(tmp, 0666); rename(tmp, export_path); }
		}
		return 0;
	}
	if (t == T_IMPORTED) {
		uint32_t w;
		if (op == 0) fprintf(stderr, "xdgshim: imported: destination_region_changed %u %u\n", a[0], a[1]);
		else if (op == 1 || op == 2) fprintf(stderr, "xdgshim: imported: punchthrough_%s %s\n", op == 1 ? "attached" : "detached", str_arg(a, &w));
		else fprintf(stderr, "xdgshim: imported: event %u\n", op);
		return 0;
	}
	if (t == T_FOREIGN || t == T_TIM) return 0;
	if (t == T_TI || t == T_TM) { if (obj == tm_id) tm_event(op, a); return 0; }
	switch (t) {
	case T_BACK:                                                              /* release of one of our buffers */
		if (op == 0) {
			for (int k = 0; k < MAXSC; k++) for (int i = 0; i < 2; i++) if (scs[k].used && scs[k].back[i].id == obj) scs[k].back[i].busy = 0;
			if (sc_dirty && !in_present) sc_cursor_refresh();
		}
		return 0;
	case T_PTR: {
		if (op == 3 && a[3]) { last_click = now_s(); click_x = (int)(px >> 8); click_y = (int)(py >> 8); ti_click(); } /* button pressed (text input) */
		int recent = dpad_mouse && now_s() - last_dpad < 3.0;               /* D-pad in use: our position wins */
		if (op == 0) {                                                       /* enter(serial, surf, x, y) */
			ptr_obj = obj;
			if (recent) {
				if (ptr_in) return 0;
				ptr_in = 1;
				uint32_t e[6]; memcpy(e, m, 24); e[3] = main_surf; e[4] = px; e[5] = py; out(&cli, &srv, e, 24);
				return 0;
			}
			ptr_in = 1; px = a[2]; py = a[3];
			if (sc_visible) { sc_visible = 0; sc_cursor_refresh(); }          /* a real pointer: hide our arrow */
		}
		else if (op == 1) { if (recent) return 0; if (obj == ptr_obj) ptr_in = 0; }   /* leave */
		else if (op == 2 && obj == ptr_obj) {                               /* motion(time, x, y) */
			if (recent) return 0;
			px = a[1]; py = a[2];
		}
		return 1;
	}
	case T_KBD:
		if (op == 1) {                                                      /* enter(serial, surface, keys) */
			if (kbd_swallowed && kbd_focus == obj) { kbd_swallowed = 0; D("text: keyboard enter swallowed (client still focused)\n"); return 0; }
			kbd_focus = obj; kbd_surf = a[1]; ti_enter(); D("%.2f keyboard %u enter wl_surface %u\n", now_s(), obj, a[1]);
		}
		if (op == 2 && tm_active && obj == kbd_focus) {                     /* leave while the LG keyboard is up: keep GTK focused */
			kbd_swallowed = 1; D("text: keyboard leave swallowed (VKB up)\n"); return 0;
		}
		if (op == 2 && obj == kbd_focus) kbd_swallowed = 0;
		if (op == 3 && dpad_key(a[2], a[3])) return 0;                    /* key(serial, time, key, state) */
		/* LG-private codes above KEY_MAX (1198/1199 come with every Magic Remote pointer move): GTK maps them to
		   keysyms whose "unicode" is not valid UTF-8 and types that into a focused text field (ART aborts in NewStringUTF) */
		if (op == 3 && a[2] > 0x2ff) return 0;
		if (op == 3 && (a[2] == 412 || a[2] == 158) && now_s() - tm_closed_at < 0.8) { D("text: BACK closed the keyboard, not passed on\n"); return 0; }
		return 1;
	case T_XSURF: /* real object: wl_shell_surface */
		if (op == 0) { mb_start(&b, obj, 0); mb_u(&b, a[0]); mb_send(&b, &srv); } /* ping -> pong */
		return 0;
	case T_XTOP: /* real object: wl_webos_shell_surface */
		D("%.2f webos surface %u event %u (%u)\n", now_s(), obj, op, op < 2 ? a[0] : 0);
		if (op == 2) { mb_start(&b, obj, 1); mb_send(&b, &cli); }          /* close -> xdg_toplevel.close */
		return 0;
	case T_XWM: case T_XPOS: case T_XPOP:
		return 0;
	}
	return 1;
}

static int pump(struct side *from, struct side *to, int is_client)
{
	char cbuf[CMSG_SPACE(sizeof(int) * 32)];
	struct iovec iov = { from->buf + from->len, sizeof from->buf - from->len };
	struct msghdr mh = { .msg_iov = &iov, .msg_iovlen = 1, .msg_control = cbuf, .msg_controllen = sizeof cbuf };
	ssize_t r = recvmsg(from->fd, &mh, MSG_CMSG_CLOEXEC);
	if (r <= 0) return r == 0 || errno != EINTR ? -1 : 0;
	for (struct cmsghdr *c = CMSG_FIRSTHDR(&mh); c; c = CMSG_NXTHDR(&mh, c))
		if (c->cmsg_level == SOL_SOCKET && c->cmsg_type == SCM_RIGHTS) {
			int n = (c->cmsg_len - CMSG_LEN(0)) / sizeof(int);
			memcpy(from->fds + from->nfds, CMSG_DATA(c), n * sizeof(int));
			from->nfds += n;
			for (int i = 0; is_client && i < n && ncfdq < 64; i++) cfdq[ncfdq++] = dup(((int *)CMSG_DATA(c))[i]);
		}
	from->len += r;
	size_t off = 0;
	while (from->len - off >= 8) {
		uint32_t hdr[2];
		memcpy(hdr, from->buf + off, 8);
		uint32_t size = hdr[1] >> 16;
		if (size < 8) { fprintf(stderr, "xdgshim: bad message size\n"); return -1; }
		if (from->len - off < size) break;
		uint32_t msg[16384];
		memcpy(msg, from->buf + off, size);
		int fwd = is_client ? on_request(msg, size) : on_event(msg, size);
		if (fwd) out(to, from, msg, size);
		off += size;
	}
	memmove(from->buf, from->buf + off, from->len - off);
	from->len -= off;
	if (is_client && !from->len) {                     /* no partial message left: its fds are done */
		for (int i = 0; i < ncfdq; i++) close(cfdq[i]);
		ncfdq = fd_used = 0;
	}
	return 0;
}

static void connect_srv(const char *target)
{
	struct sockaddr_un sa = { .sun_family = AF_UNIX };
	snprintf(sa.sun_path, sizeof sa.sun_path, "%s", target);
	srv.fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	if (connect(srv.fd, (void *)&sa, sizeof sa) < 0) die("connect compositor");
}

static void serve(int cfd, const char *target)
{
	if (target) connect_srv(target);
	cli.fd = cfd;
	if (!target) ti_open();                            /* phone typing: main (SAM-launched) client only */
	struct pollfd p[3] = { { cli.fd, POLLIN }, { srv.fd, POLLIN }, { ti_fifo, POLLIN } };
	for (;;) {
		if (poll(p, ti_fifo >= 0 ? 3 : 2, dpad_active() ? 16 : tm_idle_at ? 100 : export_path ? 250 : import_path ? 500 : -1) < 0) { if (errno == EINTR) continue; die("poll"); }
		dpad_tick();
		tm_idle_check();
		if (p[2].fd >= 0 && (p[2].revents & POLLIN)) ti_fifo_read();
		try_import();
		try_region();
		if (p[0].revents && pump(&cli, &srv, 1) < 0) { fprintf(stderr, "xdgshim: client closed\n"); break; }
		if (p[1].revents && pump(&srv, &cli, 0) < 0) { fprintf(stderr, "xdgshim: compositor closed\n"); break; }
	}
	ti_on = 0; ti_state();
	exit(0);
}

int main(int argc, char **argv)
{
	const char *rt = getenv("XDG_RUNTIME_DIR");
	const char *real = getenv("XDGSHIM_TARGET");
	int once = argc > 1 && !strcmp(argv[1], "-1");
	if (once) { argv++; argc--; }
	const char *name = argc > 1 ? argv[1] : "wayland-atl";
	char lpath[108], tpath[108];
	dbg = getenv("XDGSHIM_DEBUG") != NULL;
	import_path = getenv("XDGSHIM_IMPORT") && *getenv("XDGSHIM_IMPORT") ? getenv("XDGSHIM_IMPORT") : NULL;
		export_path = getenv("XDGSHIM_EXPORT") && *getenv("XDGSHIM_EXPORT") ? getenv("XDGSHIM_EXPORT") : NULL;
	export_type = getenv("XDGSHIM_EXPORT_TYPE") ? atoi(getenv("XDGSHIM_EXPORT_TYPE")) : 0;
	if (export_path) unlink(export_path);
		no_opaque = getenv("XDGSHIM_NOOPAQUE") && !strcmp(getenv("XDGSHIM_NOOPAQUE"), "1");
	clear_all = getenv("XDGSHIM_CLEAR") && !strcmp(getenv("XDGSHIM_CLEAR"), "1");
		alpha_dbg = getenv("XDGSHIM_ALPHA") && !strcmp(getenv("XDGSHIM_ALPHA"), "1");
	relay = getenv("XDGSHIM_RELAY");
	no_subsurf = getenv("XDGSHIM_SUBSURF") && !strcmp(getenv("XDGSHIM_SUBSURF"), "0");
	hover_mode = !(getenv("XDGSHIM_HOVER") && !strcmp(getenv("XDGSHIM_HOVER"), "0"));
	ti_enabled = !(getenv("XDGSHIM_TEXTINPUT") && !strcmp(getenv("XDGSHIM_TEXTINPUT"), "0"));
	sc_enabled = !(getenv("XDGSHIM_SOFTCURSOR") && !strcmp(getenv("XDGSHIM_SOFTCURSOR"), "0"));
	dpad_mouse = !(getenv("XDGSHIM_DPAD") && !strcmp(getenv("XDGSHIM_DPAD"), "keys"));
	appid = getenv("XDGSHIM_APPID") ? getenv("XDGSHIM_APPID") : "org.webosatl.app";
	if (getenv("XDGSHIM_W")) W = atoi(getenv("XDGSHIM_W"));
	if (getenv("XDGSHIM_H")) H = atoi(getenv("XDGSHIM_H"));
	if (!rt) rt = "/tmp/xdg";
	if (name[0] == '/') snprintf(lpath, sizeof lpath, "%s", name);
	else snprintf(lpath, sizeof lpath, "%s/%s", rt, name);
	snprintf(tpath, sizeof tpath, "%s/%s", rt, real ? real : "wayland-0");
	signal(SIGCHLD, SIG_IGN);
	int l = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	struct sockaddr_un sa = { .sun_family = AF_UNIX };
	snprintf(sa.sun_path, sizeof sa.sun_path, "%s", lpath);
	unlink(lpath);
	if (bind(l, (void *)&sa, sizeof sa) < 0 || listen(l, 8) < 0) die("listen");
	chmod(lpath, 0777);
	fprintf(stderr, "xdgshim: %s -> %s (appId %s, %dx%d%s)\n", lpath, tpath, appid, W, H, once ? ", single" : "");
	if (once) {
		connect_srv(tpath);
		int c;
		while ((c = accept4(l, NULL, NULL, SOCK_CLOEXEC)) < 0 && errno == EINTR) ;
		if (c < 0) die("accept");
		/* later connections (e.g. ATL's off-screen GdkDisplay for Bitmap rendering) are served by a
		   forked listener; only the first client's windows come from this (SAM-launched) process */
		pid_t lp = fork();
		if (lp == 0) {
			prctl(PR_SET_PDEATHSIG, SIGTERM);
			close(c); close(srv.fd);
			for (;;) {
				int c2 = accept4(l, NULL, NULL, SOCK_CLOEXEC);
				if (c2 < 0) { if (errno == EINTR) continue; _exit(0); }
				D("extra client\n");
				if (fork() == 0) { close(l); prctl(PR_SET_PDEATHSIG, SIGTERM); serve(c2, tpath); }
				close(c2);
			}
		}
		close(l);
		serve(c, NULL);
	}
	for (;;) {
		int c = accept4(l, NULL, NULL, SOCK_CLOEXEC);
		if (c < 0) { if (errno == EINTR) continue; die("accept"); }
		if (fork() == 0) { close(l); serve(c, tpath); }
		close(c);
	}
}
