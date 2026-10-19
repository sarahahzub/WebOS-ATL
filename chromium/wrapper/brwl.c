/*
 * brwl: tiny Wayland proxy between Chromium and xdgshim (webOS ATL Chromium, org.webosatl.chromium).
 *
 *   brwl [-o <oom_score_adj>] <listen-socket-path> <upstream-socket-path>
 *
 * Chromium -> brwl -> xdgshim -1 -> webOS compositor. Messages are forwarded byte-for-byte (fds included) except:
 *   - wl_seat: the TV compositor has v4. Chromium only scrolls on wl_seat v5 (it waits for wl_pointer.frame), so brwl
 *     advertises v5, binds the server at its own version and appends wl_pointer.frame after every pointer event
 *     (axis gets axis_source=wheel + axis_discrete first). xdgshim's CH+/CH- scroll then works in Chromium.
 *     wl_seat.release (v5 only) is dropped. The keyboards of the other wl_seat globals are merged (see try_merge).
 *   - xdg_toplevel.configure: xdgshim sends "fullscreen"; Chromium would go into F11 mode and hide the address bar.
 *     The fullscreen state is rewritten to maximized (BRWL_FULLSCREEN=1 keeps it: kiosk).
 *   - BACK (keys 158 KEY_BACK / 412 KEY_PREVIOUS, what xdgshim passes on with _WEBOS_ACCESS_POLICY_KEYS_BACK):
 *       short press  -> mouse "back" button (BTN_SIDE, Chromium's Back), or close the browser when the title starts with
 *                       BRWL_HOME_TITLE (default "Chromium start", the start page);
 *       held >= BRWL_LONG_MS (default 800 ms) -> close the browser (xdg_toplevel.close).
 *     EXIT (174) closes the browser. Closing the window ends Chromium, then brwl, xdgshim and the wrapper app.
 *   - zwp_text_input_v3: Chromium enables text input for a newly focused field before it sends that field's caret
 *     rectangle, so xdgshim still has the previous field's caret and does not open the LG keyboard for a click on the
 *     address bar after a page field was used. A commit that enables without a caret rectangle gets an empty one
 *     (0,0,0,0) first, which xdgshim treats as "caret unknown": the click decides.
 *   - -o N: OOM guard. Every 2 s every process of brwl's session (Chromium and its children) whose oom_score_adj is
 *     below N is set back to N (Chromium lowers its renderers to 300 when it may), so on memory pressure the kernel
 *     kills the browser before Home or other apps.
 * The browser process is the main client (Chromium children that connect earlier wait for it, see serve()): brwl
 * exits when it disconnects. Later clients are served by forked children.
 * brwl also exits when its parent (launch.sh, i.e. Chromium after exec) dies; if Chromium never connected, brwl first
 * connects to xdgshim and hangs up, so xdgshim and the wrapper app exit at once instead of showing nothing.
 * Env: BRWL_SCALE=N (HiDPI, see `scale`), BRWL_DEBUG=1, BRWL_HOME_TITLE, BRWL_LONG_MS, BRWL_FULLSCREEN=1.
 * Build: arm-linux-gnueabihf-gcc -O2 -Wall -static -o brwl brwl.c  (same toolchain as xdgshim)
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#define MAXID 65536
enum { T_NONE, T_REG, T_SEAT, T_PTR, T_KBD, T_XWM, T_XSURF, T_XTOP, T_TIM, T_TI, T_COMP, T_SURF, T_CB, T_MSEAT, T_MKBD,
       T_OUT, T_REGN, T_SUBC, T_SUBS };
#define K_BACK 158
#define K_PREV 412
#define K_EXIT 174
#define K_LEFT 105
#define BTN_SIDE 0x113 /* mouse "back" button */

static int dbg, keep_fullscreen, long_ms = 800, oom_adj = -1;
static const char *home_title = "Chromium start";
#define D(...) do { if (dbg) { fprintf(stderr, "brwl[%d]: ", (int)getpid()); fprintf(stderr, __VA_ARGS__); } } while (0)

struct side { int fd; uint8_t buf[1 << 16]; size_t len; int fds[64]; int nfds; };
static struct side cli, srv;
static uint8_t type_[MAXID];
static uint32_t seat_name = 0xffffffffu, seat_srv_ver;
static uint8_t seat_emul[MAXID];         /* wl_seat / wl_pointer ids the client sees as v5 */
static uint8_t reg_seats[MAXID];         /* wl_seat globals seen per wl_registry */
static int seat_pick = 2;                /* BRWL_SEAT: index of the wl_seat global the client sees (-1: all). Default 2:
                                          * the LG IR remote (evdev "LGE RCU") arrives on seat #2 of 7; uinput / netinput
                                          * injection on #4 (merged). The last one if there are fewer seats. */
static uint32_t kbd_focus;               /* keyboard with focus */
static uint32_t ptr_main, ptr_focus, ptr_x = 960 << 8, ptr_y = 540 << 8;   /* pointer, surface it is over, position */
static uint32_t main_surf;               /* wl_surface of the first toplevel */
static uint32_t xsurf_surf[MAXID];       /* xdg_surface -> wl_surface */
static uint32_t synth_serial = 0x7e000000;
static uint32_t tops[16]; static int ntops;
static char title[512];
static double back_down;                 /* BACK held since (0: not held) */
static int back_fired;
static int ti_enable, ti_rect;           /* zwp_text_input_v3: enable / set_cursor_rectangle since the last commit */
/* seat merge (main client): the TV compositor has 7 wl_seat globals and the remote's keys arrive on one of them (not
 * the first, the only one Chromium binds). brwl binds every other seat itself, with object ids taken from the client's
 * destroyed objects (their delete_id is swallowed, so the client never reuses them), asks for its keyboard and passes
 * its key / modifiers / keymap events on as events of Chromium's own wl_keyboard. */
static int merge_on = 1;
/* HiDPI (BRWL_SCALE=N, integer): Chromium is told the output has scale N, so it lays out a (1920/N)x(1080/N) window
 * (toolbar, menus and pages N times bigger) and draws a 1920x1080 buffer with buffer_scale N. brwl drops
 * set_buffer_scale and converts surface coordinates (x N towards the compositor, / N towards Chromium), so xdgshim and
 * the TV see a plain 1920x1080 scale-1 window (no 4x buffers, no fractional-scale protocol needed). */
static int scale = 1;
#define UP(v) ((v) = (uint32_t)((int32_t)(v) * scale))
#define DOWN(v) ((v) = (uint32_t)((int32_t)(v) / scale))
static uint32_t reg_main, kbd_main, cli_seat_name;
static uint32_t mseat_name[16], mseat_ver[16]; static int nmseat, mseat_done[16];
static uint32_t idpool[32]; static int nidpool;
static volatile sig_atomic_t parent_gone;
static const char *upstream;
static void on_parent_death(int sig) { (void)sig; parent_gone = 1; }

static double now_s(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec / 1e9; }
static uint32_t now_ms(void) { return (uint32_t)(now_s() * 1000); }
static void settype(uint32_t id, int t) { if (id < MAXID) type_[id] = t; }
static int gettype(uint32_t id) { return id < MAXID ? type_[id] : T_NONE; }

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
		if (r < 0) { if (errno == EINTR) continue; D("send failed: %s\n", strerror(errno)); exit(0); }
		if (from && from->nfds) {
			for (int i = 0; i < from->nfds; i++) close(from->fds[i]);
			from->nfds = 0; mh.msg_control = NULL; mh.msg_controllen = 0;
		}
		n -= r; iov.iov_base = (char *)iov.iov_base + r; iov.iov_len = n;
	}
}

static void ev(uint32_t obj, uint16_t op, const uint32_t *args, int n)   /* synthetic event to the client */
{
	uint32_t m[16];
	m[0] = obj; m[1] = (uint32_t)((8 + 4 * n) << 16) | op;
	memcpy(m + 2, args, 4 * n);
	out(&cli, NULL, m, 8 + 4 * n);
}

static const char *str_arg(const uint32_t *a, uint32_t *words)
{
	*words = 1 + (a[0] + 3) / 4;
	return a[0] ? (const char *)(a + 1) : "";
}

static void close_browser(const char *why)
{
	D("close browser (%s), %d toplevel(s)\n", why, ntops);
	fprintf(stderr, "brwl: closing the browser: %s\n", why);
	for (int i = 0; i < ntops; i++) ev(tops[i], 1, NULL, 0);   /* xdg_toplevel.close */
}

/* Back = the mouse "back" button (BTN_SIDE) on Chromium's pointer: independent of the compositor's keymap (a
 * synthesized Alt+Left did not navigate on the bench). If the pointer is not over the window (no D-pad move yet),
 * enter it for the click and leave again. */
static void pframe(void) { if (ptr_main < MAXID && seat_emul[ptr_main]) ev(ptr_main, 5, NULL, 0); }

static void browser_back(void)
{
	if (!ptr_main || !main_surf) { D("back: no pointer (%u) / surface (%u)\n", ptr_main, main_surf); return; }
	int synth_enter = !ptr_focus;
	if (synth_enter) { uint32_t e[4] = { synth_serial++, main_surf, ptr_x, ptr_y }; ev(ptr_main, 0, e, 4); pframe(); }
	/* Chromium handles the back button only over the web contents: over the tab strip / address bar (after a click
	 * there) it does nothing. Click below the toolbar (y >= 300 px), then put the pointer back. */
	uint32_t below = (300 / scale) << 8;
	int moved = ptr_y < below;
	if (moved) { uint32_t mo[3] = { now_ms(), ptr_x, below }; ev(ptr_main, 2, mo, 3); pframe(); }
	uint32_t bt[4] = { synth_serial++, now_ms(), BTN_SIDE, 1 };
	ev(ptr_main, 3, bt, 4); pframe();
	bt[0] = synth_serial++; bt[1] = now_ms(); bt[3] = 0;
	ev(ptr_main, 3, bt, 4); pframe();
	if (moved) { uint32_t mo[3] = { now_ms(), ptr_x, ptr_y }; ev(ptr_main, 2, mo, 3); pframe(); }
	if (synth_enter) { uint32_t l[2] = { synth_serial++, main_surf }; ev(ptr_main, 1, l, 2); pframe(); }
	D("back -> BTN_SIDE (title '%s'%s)\n", title, synth_enter ? ", synthetic enter" : "");
}

static void back_tick(void)
{
	if (back_down > 0 && !back_fired && (now_s() - back_down) * 1000 >= long_ms) {
		back_fired = 1;
		close_browser("BACK held");
	}
}

/* client -> server; returns 1 to forward */
static struct { uint32_t id, name, ver; int shown; } regs[8]; static int nregs;   /* BRWL_SEAT per wl_registry */
static int reg_slot(uint32_t id)
{
	for (int i = 0; i < nregs; i++) if (regs[i].id == id) return i;
	if (nregs == 8) return -1;
	regs[nregs].id = id; return nregs++;
}
static void ev_raw(const uint32_t *m, size_t n) { out(&cli, NULL, m, n); }

static void try_merge(void)
{
	if (!merge_on || !reg_main || !kbd_main) return;
	for (int i = 0; i < nmseat && nidpool >= 2; i++) {
		if (mseat_done[i] || mseat_name[i] == cli_seat_name) continue;
		uint32_t sid = idpool[--nidpool], kid = idpool[--nidpool];
		uint32_t b[8] = { reg_main, (32u << 16) | 0, mseat_name[i], 8, 0, 0, mseat_ver[i] > 4 ? 4 : mseat_ver[i], sid };
		memcpy(b + 4, "wl_seat", 8);
		out(&srv, NULL, b, sizeof b);
		uint32_t g[3] = { sid, (12u << 16) | 1, kid };                 /* wl_seat.get_keyboard */
		out(&srv, NULL, g, sizeof g);
		settype(sid, T_MSEAT); settype(kid, T_MKBD); mseat_done[i] = 1;
		fprintf(stderr, "brwl: merged wl_seat global %u (seat %u, keyboard %u -> %u)\n", mseat_name[i], sid, kid, kbd_main);
	}
}

static int on_request(uint32_t *m, uint32_t size)
{
	uint32_t obj = m[0], op = m[1] & 0xffff, *a = m + 2;
	int t = gettype(obj);
	if (obj == 1 && op == 1) { settype(a[0], T_REG); if (!reg_main) reg_main = a[0]; return 1; }   /* get_registry */
	if (obj == 1 && op == 0) { settype(a[0], T_CB); return 1; }        /* wl_display.sync: id harvestable */
	if (t == T_REG && op == 0) {                                        /* bind(name, iface, ver, id) */
		uint32_t w;
		const char *iface = str_arg(a + 1, &w);
		uint32_t *ver = &a[1 + w], id = a[2 + w];
		D("bind %s v%u -> %u\n", iface, *ver, id);
		if (!strcmp(iface, "wl_seat")) {   /* by interface: the TV compositor may advertise more than one wl_seat */
			settype(id, T_SEAT); if (!cli_seat_name) cli_seat_name = a[0];
			if (seat_srv_ver && *ver > seat_srv_ver) { if (id < MAXID) seat_emul[id] = 1; D("wl_seat %u: client v%u, server v%u\n", id, *ver, seat_srv_ver); *ver = seat_srv_ver; }
		} else if (!strcmp(iface, "xdg_wm_base")) settype(id, T_XWM);
		else if (!strcmp(iface, "wl_compositor")) settype(id, T_COMP);
		else if (!strcmp(iface, "wl_output")) settype(id, T_OUT);
		else if (!strcmp(iface, "wl_subcompositor")) settype(id, T_SUBC);
		else if (!strcmp(iface, "zwp_text_input_manager_v3")) settype(id, T_TIM);
		(void)size;
		return 1;
	}
	switch (t) {
	case T_COMP:
		if (op == 0) settype(a[0], T_SURF);                         /* create_surface */
		if (op == 1) settype(a[0], T_REGN);                         /* create_region */
		return 1;
	case T_SURF:
		if (op == 3) settype(a[0], T_CB);                           /* frame */
		if (scale > 1) {
			if (op == 1) { UP(a[1]); UP(a[2]); }                    /* attach(buffer, x, y) */
			else if (op == 2) { UP(a[0]); UP(a[1]); UP(a[2]); UP(a[3]); }   /* damage */
			else if (op == 8) return 0;                             /* set_buffer_scale: the TV sees scale 1 */
		}
		return 1;
	case T_REGN:
		if (scale > 1 && (op == 1 || op == 2)) { UP(a[0]); UP(a[1]); UP(a[2]); UP(a[3]); }   /* add / subtract */
		return 1;
	case T_SUBC:
		if (op == 1) settype(a[0], T_SUBS);                         /* get_subsurface */
		return 1;
	case T_SUBS:
		if (scale > 1 && op == 1) { UP(a[0]); UP(a[1]); }           /* set_position */
		return 1;
	case T_SEAT:
		if (op == 1 && !kbd_main) { kbd_main = a[0]; out(&srv, &cli, m, size); try_merge(); settype(a[0], T_KBD); D("wl_seat %u: get_keyboard %u (main)\n", obj, a[0]); return 0; }
		if (op == 0) { settype(a[0], T_PTR); if (a[0] < MAXID && obj < MAXID) seat_emul[a[0]] = seat_emul[obj]; if (!ptr_main) ptr_main = a[0]; }
		if (op == 1) settype(a[0], T_KBD);
		if (op <= 1) D("wl_seat %u: get_%s %u\n", obj, op ? "keyboard" : "pointer", a[0]);
		if (op == 3 && obj < MAXID && seat_emul[obj]) return 0;     /* wl_seat.release (v5): server has v4 */
		return 1;
	case T_TIM:
		if (op == 1) settype(a[0], T_TI);                           /* get_text_input(id, seat) */
		return 1;
	case T_TI:
		if (op == 1) ti_enable = 1;                                 /* enable */
		else if (op == 6) { ti_rect = 1; if (scale > 1) { UP(a[0]); UP(a[1]); UP(a[2]); UP(a[3]); } }   /* set_cursor_rectangle */
		else if (op == 7) {                                         /* commit */
			if (ti_enable && !ti_rect) {
				uint32_t r[6] = { obj, (24u << 16) | 6, 0, 0, 0, 0 };
				out(&srv, NULL, r, sizeof r);
				D("text input enabled without a caret: empty caret rectangle sent\n");
			}
			ti_enable = ti_rect = 0;
		}
		return 1;
	case T_XWM:
		if (op == 2) { settype(a[0], T_XSURF); if (a[0] < MAXID) xsurf_surf[a[0]] = a[1]; }   /* get_xdg_surface */
		return 1;
	case T_XSURF:
		if (scale > 1 && op == 3) { UP(a[0]); UP(a[1]); UP(a[2]); UP(a[3]); }   /* set_window_geometry */
		if (op == 1) {                                              /* get_toplevel */
			settype(a[0], T_XTOP); if (ntops < 16) tops[ntops++] = a[0];
			D("toplevel %u\n", a[0]);
			if (!main_surf && obj < MAXID) main_surf = xsurf_surf[obj];
		}
		return 1;
	case T_XTOP:
		if (scale > 1 && (op == 7 || op == 8)) { UP(a[0]); UP(a[1]); }   /* set_max_size / set_min_size */
		if (op == 0) {                                              /* destroy */
			for (int i = 0; i < ntops; i++) if (tops[i] == obj) { tops[i] = tops[--ntops]; break; }
			settype(obj, T_NONE);
		} else if (op == 2) {                                       /* set_title */
			uint32_t w;
			snprintf(title, sizeof title, "%s", str_arg(a, &w));
			D("title '%s'\n", title);
		}
		return 1;
	}
	return 1;
}

/* server -> client; returns 1 to forward */
static int on_event(uint32_t *m, uint32_t size)
{
	uint32_t obj = m[0], op = m[1] & 0xffff, *a = m + 2;
	int t = gettype(obj);
	if (obj == 1 && op == 1) {                                          /* delete_id */
		/* any id the client freed (callbacks, regions, buffers, pools ...): the server has destroyed the object */
		if (merge_on && a[0] < MAXID && gettype(a[0]) != T_MSEAT && gettype(a[0]) != T_MKBD
		    && nidpool < 32 && nidpool < 2 * (nmseat - 1)) {
			D("id %u kept for the seat merge (%d)\n", a[0], nidpool + 1);
			idpool[nidpool++] = a[0]; settype(a[0], T_NONE); try_merge(); return 0;   /* kept for the seat merge */
		}
		if (a[0] < MAXID) { type_[a[0]] = T_NONE; seat_emul[a[0]] = 0; }
		return 1;
	}
	if (t == T_CB && op == 0 && seat_pick >= 0) {                     /* callback done: registry burst is over */
		for (int r = 0; r < nregs; r++) if (!regs[r].shown && regs[r].name) {
			regs[r].shown = 1;
			uint32_t g[7] = { regs[r].id, (28u << 16) | 0, regs[r].name, 8, 0, 0, regs[r].ver < 5 ? 5 : regs[r].ver };
			memcpy(g + 4, "wl_seat", 8);
			if (regs[r].ver < 5) { seat_name = regs[r].name; seat_srv_ver = regs[r].ver; }
			ev_raw(g, sizeof g);
			D("no wl_seat #%d: global %u shown instead\n", seat_pick, regs[r].name);
		}
	}
	if (t == T_MSEAT) return 0;                                         /* capabilities / name of a merged seat */
	if (t == T_MKBD) {                                                  /* merged keyboard -> Chromium's keyboard */
		if (op == 1 || op == 2 || op == 5) return 0;                    /* enter / leave / repeat_info */
		m[0] = obj = kbd_main; t = T_KBD;                               /* keymap (fd) / key / modifiers */
		if (op == 3) D("merged key %u %u\n", a[2], a[3]);
	}
	if (t == T_REG && op == 0) {                                        /* global(name, iface, ver) */
		uint32_t w;
		const char *iface = str_arg(a + 1, &w);
		if (!strcmp(iface, "wl_seat") && nmseat < 16) {
			int k = 0; while (k < nmseat && mseat_name[k] != a[0]) k++;
			if (k == nmseat) { mseat_name[nmseat] = a[0]; mseat_ver[nmseat++] = a[1 + w]; }
		}
		if (!strcmp(iface, "wl_seat") && seat_pick >= 0 && obj < MAXID) {
			int k = reg_seats[obj]++, r = reg_slot(obj);
			if (k != seat_pick) {                   /* hidden; the last one is shown later if there is no #seat_pick */
				if (r >= 0 && !regs[r].shown) { regs[r].name = a[0]; regs[r].ver = a[1 + w]; }
				D("wl_seat global %u (#%d) hidden\n", a[0], k); return 0;
			}
			if (r >= 0) regs[r].shown = 1;
			D("wl_seat global %u (#%d) shown\n", a[0], k);
		}
		if (!strcmp(iface, "wl_seat") && a[1 + w] < 5) {
			seat_name = a[0]; seat_srv_ver = a[1 + w];
			a[1 + w] = 5;
			D("wl_seat v%u advertised as v5\n", seat_srv_ver);
		}
		return 1;
	}
	switch (t) {
	case T_OUT:
		if (scale > 1 && op == 3) { a[0] = scale; return 1; }       /* scale */
		if (scale > 1 && op == 2) { uint32_t sc = scale; ev(obj, 3, &sc, 1); }   /* done: scale first */
		return 1;
	case T_PTR:
		if (scale > 1 && op == 0) { DOWN(a[2]); DOWN(a[3]); }      /* enter: surface coords for Chromium */
		if (scale > 1 && op == 2) { DOWN(a[1]); DOWN(a[2]); }      /* motion */
		if (op == 0) { ptr_focus = a[1]; ptr_x = a[2]; ptr_y = a[3]; }          /* enter(serial, surface, x, y) */
		else if (op == 1) ptr_focus = 0;                                         /* leave */
		else if (op == 2) { ptr_x = a[1]; ptr_y = a[2]; }                        /* motion(time, x, y) */
		if (obj >= MAXID || !seat_emul[obj] || op > 4) return 1;
		if (op == 4) {                                              /* axis(time, axis, value) */
			uint32_t src = 0;
			ev(obj, 6, &src, 1);                                    /* axis_source wheel */
			int32_t v = (int32_t)a[2], d = v / (15 * 256);
			if (d) { uint32_t dd[2] = { a[1], (uint32_t)d }; ev(obj, 8, dd, 2); }   /* axis_discrete */
		}
		out(&cli, &srv, m, size);
		ev(obj, 5, NULL, 0);                                        /* frame */
		return 0;
	case T_KBD:
		if (op == 1 || op == 3) D("wl_keyboard %u: %s %u\n", obj, op == 1 ? "enter" : "key", op == 3 ? a[2] : a[1]);
		if (op == 1) kbd_focus = obj;                               /* enter */
		else if (op == 2 && obj == kbd_focus) kbd_focus = 0;        /* leave */
		else if (op == 3) {                                         /* key(serial, time, key, state) */
			uint32_t key = a[2], st = a[3];
			if (key == K_BACK || key == K_PREV) {
				if (st) { if (back_down == 0) { back_down = now_s(); back_fired = 0; } }
				else if (back_down > 0) {
					if (!back_fired) {
						if (home_title[0] && !strncmp(title, home_title, strlen(home_title))) close_browser("BACK on the start page");
						else browser_back();
					}
					back_down = 0; back_fired = 0;
				}
				return 0;
			}
			if (key == K_EXIT) { if (st) close_browser("EXIT"); return 0; }
		}
		return 1;
	case T_XTOP:
		if (op == 0 && scale > 1) { DOWN(a[0]); DOWN(a[1]); }      /* configure size in Chromium's units */
		if (op == 0 && !keep_fullscreen) {                          /* configure(w, h, states[]) */
			uint32_t n = a[2] / 4;
			for (uint32_t i = 0; i < n && 3 + i < size / 4 - 2; i++) if (a[3 + i] == 2) a[3 + i] = 1;  /* fullscreen -> maximized */
		}
		return 1;
	}
	return 1;
}

static int pump(struct side *from, struct side *to, int is_client)
{
	char cbuf[CMSG_SPACE(sizeof(int) * 32)];
	struct iovec iov = { from->buf + from->len, sizeof from->buf - from->len };
	struct msghdr mh = { .msg_iov = &iov, .msg_iovlen = 1, .msg_control = cbuf, .msg_controllen = sizeof cbuf };
	ssize_t r = recvmsg(from->fd, &mh, MSG_CMSG_CLOEXEC);
	if (r <= 0) return r < 0 && errno == EINTR ? 0 : -1;
	for (struct cmsghdr *c = CMSG_FIRSTHDR(&mh); c; c = CMSG_NXTHDR(&mh, c))
		if (c->cmsg_level == SOL_SOCKET && c->cmsg_type == SCM_RIGHTS) {
			int n = (c->cmsg_len - CMSG_LEN(0)) / sizeof(int);
			if (from->nfds + n > 64) { fprintf(stderr, "brwl: too many fds\n"); return -1; }
			memcpy(from->fds + from->nfds, CMSG_DATA(c), n * sizeof(int));
			from->nfds += n;
		}
	from->len += r;
	size_t off = 0;
	while (from->len - off >= 8) {
		uint32_t hdr[2];
		memcpy(hdr, from->buf + off, 8);
		uint32_t size = hdr[1] >> 16;
		if (size < 8 || size & 3) { fprintf(stderr, "brwl: bad message size\n"); return -1; }
		if (from->len - off < size) break;
		uint32_t msg[16384];
		memcpy(msg, from->buf + off, size);
		int fwd = is_client ? on_request(msg, size) : on_event(msg, size);
		if (fwd) out(to, from, msg, size);
		off += size;
	}
	memmove(from->buf, from->buf + off, from->len - off);
	from->len -= off;
	return 0;
}

/* -o: keep every process of our session at oom_score_adj >= oom_adj */
static void oom_guard(void)
{
	static double last;
	if (oom_adj < 0 || now_s() - last < 2.0) return;
	last = now_s();
	pid_t me = getsid(0);
	DIR *d = opendir("/proc");
	if (!d) return;
	struct dirent *e;
	while ((e = readdir(d))) {
		if (e->d_name[0] < '1' || e->d_name[0] > '9') continue;
		char p[300], b[512];
		snprintf(p, sizeof p, "/proc/%s/stat", e->d_name);
		int fd = open(p, O_RDONLY | O_CLOEXEC);
		if (fd < 0) continue;
		ssize_t n = read(fd, b, sizeof b - 1);
		close(fd);
		if (n <= 0) continue;
		b[n] = 0;
		char *q = strrchr(b, ')');
		int sid = 0, ppid, pgrp; char st;
		if (!q || sscanf(q + 2, "%c %d %d %d", &st, &ppid, &pgrp, &sid) != 4 || sid != me) continue;
		snprintf(p, sizeof p, "/proc/%s/oom_score_adj", e->d_name);
		fd = open(p, O_RDWR | O_CLOEXEC);
		if (fd < 0) continue;
		n = read(fd, b, 16);
		if (n > 0) {
			b[n] = 0;
			if (atoi(b) < oom_adj) {
				int l = snprintf(b, sizeof b, "%d\n", oom_adj);
				if (pwrite(fd, b, l, 0) == l) D("oom_score_adj of %s raised to %d\n", e->d_name, oom_adj);
			}
		}
		close(fd);
	}
	closedir(d);
}

static int connect_up(const char *path)
{
	struct sockaddr_un sa = { .sun_family = AF_UNIX };
	snprintf(sa.sun_path, sizeof sa.sun_path, "%s", path);
	int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	if (fd < 0 || connect(fd, (void *)&sa, sizeof sa) < 0) { perror("brwl: connect upstream"); exit(1); }
	return fd;
}

static void serve(int cfd, const char *up, int lfd);

/* pid and Chromium process type of a client ("browser" when its command line has no --type=) */
static const char *peer(int fd, int *pid)
{
	static char ty[64];
	struct ucred cr; socklen_t n = sizeof cr;
	*pid = getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &cr, &n) ? 0 : cr.pid;
	snprintf(ty, sizeof ty, "browser");
	char path[64], buf[4096];
	snprintf(path, sizeof path, "/proc/%d/cmdline", *pid);
	int f = open(path, O_RDONLY | O_CLOEXEC);
	if (f < 0) return ty;
	ssize_t r = read(f, buf, sizeof buf - 1); close(f);
	for (ssize_t i = 0; i < r; i += strlen(buf + i) + 1) {
		buf[r] = 0;
		if (!strncmp(buf + i, "--type=", 7)) { snprintf(ty, sizeof ty, "%s", buf + i + 7); break; }
	}
	return ty;
}

static void extra(int c2, const char *up, int lfd)                    /* a later client: served by its own process */
{
	int pid; const char *ty = peer(c2, &pid);
	pid_t k = fork();
	if (k == 0) {
		prctl(PR_SET_PDEATHSIG, SIGTERM);
		close(lfd); close(cli.fd); close(srv.fd);
		memset(type_, 0, sizeof type_); memset(seat_emul, 0, sizeof seat_emul); memset(reg_seats, 0, sizeof reg_seats);
		cli.len = srv.len = 0; ntops = 0; kbd_focus = 0; seat_name = 0xffffffffu;
		ptr_main = ptr_focus = main_surf = 0;
		oom_adj = -1; merge_on = 0; nregs = 0;
		serve(c2, up, -1);
	}
	close(c2);
	fprintf(stderr, "brwl: extra client pid %d (%s), served by %d\n", pid, ty, (int)k);
}

/* Chromium children (e.g. the GPU process) may connect before the browser process. xdgshim -1 shows only the windows
 * of its FIRST client (the others are served by a forked process, and LSM gives no keyboard focus to their windows), so
 * those early clients wait here until the browser process has connected (at most 10 s). */
static int pend[8], npend;
static double pend_since;

static void serve(int cfd, const char *up, int lfd)
{
	cli.fd = cfd;
	srv.fd = connect_up(up);
	if (lfd >= 0) {
		int pid; const char *ty = peer(cfd, &pid);
		fprintf(stderr, "brwl: main client pid %d (%s)\n", pid, ty);
		for (int i = 0; i < npend; i++) extra(pend[i], up, lfd);
		npend = 0;
	}
	for (;;) {
		struct pollfd p[3] = { { cli.fd, POLLIN, 0 }, { srv.fd, POLLIN, 0 }, { lfd, POLLIN, 0 } };
		int to = back_down > 0 && !back_fired ? 50 : oom_adj >= 0 ? 2000 : -1;
		if (parent_gone && lfd >= 0) { fprintf(stderr, "brwl: browser process gone\n"); exit(0); }
		if (poll(p, lfd >= 0 ? 3 : 2, to) < 0) { if (errno == EINTR) continue; perror("brwl: poll"); exit(1); }
		back_tick();
		oom_guard();
		if (lfd >= 0 && (p[2].revents & POLLIN)) {                  /* extra client: own process */
			int c2 = accept4(lfd, NULL, NULL, SOCK_CLOEXEC);
			if (c2 >= 0) extra(c2, up, lfd);
		}
		if (p[0].revents && pump(&cli, &srv, 1) < 0) { D("client closed\n"); break; }
		if (p[1].revents && pump(&srv, &cli, 0) < 0) { fprintf(stderr, "brwl: upstream closed\n"); break; }
	}
	exit(0);
}

int main(int argc, char **argv)
{
	int i = 1;
	if (argc > 2 && !strcmp(argv[1], "-o")) { oom_adj = atoi(argv[2]); i = 3; }
	if (argc - i != 2) { fprintf(stderr, "usage: brwl [-o oom_score_adj] <listen-path> <upstream-path>\n"); return 2; }
	dbg = getenv("BRWL_DEBUG") && *getenv("BRWL_DEBUG") == '1';
	keep_fullscreen = getenv("BRWL_FULLSCREEN") && *getenv("BRWL_FULLSCREEN") == '1';
	if (getenv("BRWL_HOME_TITLE")) home_title = getenv("BRWL_HOME_TITLE");
	if (getenv("BRWL_SCALE") && atoi(getenv("BRWL_SCALE")) > 1) scale = atoi(getenv("BRWL_SCALE"));
	if (getenv("BRWL_SEAT")) seat_pick = atoi(getenv("BRWL_SEAT"));
	if (getenv("BRWL_LONG_MS")) long_ms = atoi(getenv("BRWL_LONG_MS"));
	signal(SIGCHLD, SIG_IGN);
	signal(SIGPIPE, SIG_IGN);
	const char *lp = argv[i], *up = argv[i + 1];
	int l = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	struct sockaddr_un sa = { .sun_family = AF_UNIX };
	snprintf(sa.sun_path, sizeof sa.sun_path, "%s", lp);
	unlink(lp);
	if (bind(l, (void *)&sa, sizeof sa) < 0 || listen(l, 8) < 0) { perror("brwl: listen"); return 1; }
	chmod(lp, 0700);
	fprintf(stderr, "brwl: %s -> %s (oom guard %d)\n", lp, up, oom_adj);
	upstream = up;
	signal(SIGUSR1, on_parent_death);
	prctl(PR_SET_PDEATHSIG, SIGUSR1);
	if (getppid() == 1) parent_gone = 1;
	for (;;) {                                                       /* wait for the main client, guarding OOM */
		struct pollfd p = { l, POLLIN, 0 };
		if (parent_gone) {                                           /* Chromium died before connecting */
			fprintf(stderr, "brwl: browser exited before connecting; closing the window\n");
			close(connect_up(upstream));
			return 0;
		}
		int r = poll(&p, 1, npend ? 500 : oom_adj >= 0 ? 2000 : -1);
		oom_guard();
		if (r > 0) {
			int c = accept4(l, NULL, NULL, SOCK_CLOEXEC);
			if (c < 0) continue;
			int pid;
			if (strcmp(peer(c, &pid), "browser") && npend < 8) {
				if (!npend) pend_since = now_s();
				pend[npend++] = c;
				fprintf(stderr, "brwl: client pid %d connected before the browser process: waits\n", pid);
				continue;
			}
			serve(c, up, l);
		}
		if (npend && now_s() - pend_since > 10) { int c = pend[0]; memmove(pend, pend + 1, --npend * sizeof *pend); serve(c, up, l); }
	}
}
