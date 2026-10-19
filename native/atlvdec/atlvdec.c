/* atlvdec: 32-bit webOS helper that feeds compressed video from ATL (64-bit) to the TV's hardware
 * decoder through libNDL_directmedia (API v2, webOS 5+; same calls as ss4s "ndl-webos5").
 *
 *   atlvdec <socket-path>        env: APPID (webOS app id for NDL_DirectMediaInit), ATL_NDL_LIB (override lib)
 *     ATLVDEC_SDL=0|1|2  NDL's impl calls SDL_webOSCreateExportedWindow() and needs a live SDL video device
 *                        and window: 2 (default) SDL_Init(VIDEO) + fullscreen transparent SDL window (Load fails
 *                        with -1 without a window), 1 SDL_Init only, 0 no SDL
 *     ATLVDEC_EARLY=1    set up SDL at startup instead of at the first OPEN (default 1: the wrapper starts the
 *                        helper before the app window, so the app window stays on top and keeps the focus)
 *     ATLVDEC_WINID=<id> NDL_DirectMediaSetWindowId(<id>) before load (exported window owned by someone else)
 *     ATLVDEC_WINID_FILE=<f> same, id read from <f> (xdgshim XDGSHIM_EXPORT=<f> exports the ATL window); also
 *                        makes NDL's SDL_webOSCreateExportedWindow() return that id (ATL-window binding, see below;
 *                        ATLVDEC_OWNWIN=1 = old behaviour) and NDL init waits for the first OPEN
 *     ATLVDEC_LINGER=<s> keep the decoder loaded <s> seconds after CLOSE (default 3) for a codec re-create
 *     ATLVDEC_PACE=0     feed at once instead of at the AVD_DUE time (NDL is in low-delay/game mode on the TV: a
 *                        frame is shown as soon as it is decoded, so the feed time is the display time)
 *     ATLVDEC_SMOOTH=0   feed at ExoPlayer's due times as given (default: steady frame grid, see smooth_due)
 *     ATLVDEC_LEAD_MS=<n> feed this long before the due time (default 15: decode latency)
 *     ATLVDEC_FULL_RECT="x y w h" use this rect when ATL asks for the full plane 0,0 1920x1080 (on the exported
 *                        ATL window a full-size SetArea after a smaller one is not applied by the compositor)
 *
 * Serves one stream at a time (a new connection replaces the current one). Protocol: proto.h.
 * Built with -DNDL_STUB it links ndl_stub.c (logs the calls) instead of dlopen-ing the TV library. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include "proto.h"

/* NDL_directmedia v2 types (layout from ss4s / webos-userland NDL_directmedia_types.h) */
typedef struct {
	struct {
		int width;
		int height;
		int type; /* 1 = H264, 2 = H265 */
		int unknown1;
	} video;
	union {
		int type; /* 0 = no audio */
		char padding[32];
	} audio;
} ndl_info_t;

typedef void (*ndl_load_cb)(int type, long long num, const char *str);
typedef void (*ndl_release_cb)(const char *type);

static int (*p_Init)(const char *appid, ndl_release_cb cb);
static int (*p_Quit)(void);
static const char *(*p_GetError)(void);
static int (*p_Load)(ndl_info_t *info, ndl_load_cb cb);
static int (*p_Unload)(void);
static int (*p_Play)(void *buf, unsigned int size, long long pts);
static int (*p_SetArea)(int x, int y, int w, int h);
static int (*p_GetRenderBufferLength)(int *len);
static int (*p_SetWindowId)(const char *id);

/* SDL (TV /usr/lib/libSDL2-2.0.so.0, the one NDL's impl links) */
static int (*sdl_Init)(uint32_t);
static const char *(*sdl_GetError)(void);
static const char *(*sdl_GetCurrentVideoDriver)(void);
static void *(*sdl_CreateWindow)(const char *, int, int, int, int, uint32_t);
static void *(*sdl_CreateRenderer)(void *, int, uint32_t);
static int (*sdl_SetRenderDrawColor)(void *, uint8_t, uint8_t, uint8_t, uint8_t);
static int (*sdl_SetRenderDrawBlendMode)(void *, int);
static int (*sdl_RenderClear)(void *);
static void (*sdl_RenderPresent)(void *);
static int (*sdl_PollEvent)(void *);
static int sdl_on;

static volatile sig_atomic_t quit;
static int ndl_inited, loaded;
static ndl_info_t info;
static struct timespec load_time;
static unsigned long nfeed, nfail;

static void logf_(const char *fmt, ...)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	fprintf(stderr, "[%ld.%03ld] atlvdec: ", (long)ts.tv_sec, ts.tv_nsec / 1000000);
	va_list ap;
	va_start(ap, fmt);
	vfprintf(stderr, fmt, ap);
	va_end(ap);
	fputc('\n', stderr);
}
#define LOG(...) logf_(__VA_ARGS__)

static const char *err(void)
{
	const char *e = p_GetError ? p_GetError() : NULL;
	return e ? e : "?";
}

static void sdl_pump(void)
{
	uint8_t ev[64];
	if (sdl_on && sdl_PollEvent)
		while (sdl_PollEvent(ev)) {
		}
}

static void sdl_setup(void)
{
	const char *m = getenv("ATLVDEC_SDL");
#ifdef NDL_STUB
	int mode = m && *m ? atoi(m) : 0;
#else
	int mode = m && *m ? atoi(m) : 2;
#endif
	if (!mode)
		return;
	void *h = dlopen("libSDL2-2.0.so.0", RTLD_NOW | RTLD_GLOBAL);
	if (!h) {
		LOG("SDL: dlopen: %s", dlerror());
		return;
	}
	*(void **)&sdl_Init = dlsym(h, "SDL_Init");
	*(void **)&sdl_GetError = dlsym(h, "SDL_GetError");
	*(void **)&sdl_GetCurrentVideoDriver = dlsym(h, "SDL_GetCurrentVideoDriver");
	*(void **)&sdl_CreateWindow = dlsym(h, "SDL_CreateWindow");
	*(void **)&sdl_CreateRenderer = dlsym(h, "SDL_CreateRenderer");
	*(void **)&sdl_SetRenderDrawColor = dlsym(h, "SDL_SetRenderDrawColor");
	*(void **)&sdl_SetRenderDrawBlendMode = dlsym(h, "SDL_SetRenderDrawBlendMode");
	*(void **)&sdl_RenderClear = dlsym(h, "SDL_RenderClear");
	*(void **)&sdl_RenderPresent = dlsym(h, "SDL_RenderPresent");
	*(void **)&sdl_PollEvent = dlsym(h, "SDL_PollEvent");
	if (!sdl_Init || sdl_Init(0x20 /* SDL_INIT_VIDEO */)) {
		LOG("SDL_Init(VIDEO) failed: %s", sdl_GetError ? sdl_GetError() : "?");
		return;
	}
	sdl_on = 1;
	LOG("SDL video up (driver %s)", sdl_GetCurrentVideoDriver ? sdl_GetCurrentVideoDriver() : "?");
	if (mode >= 2 && sdl_CreateWindow) {
		void *w = sdl_CreateWindow("atlvdec", 0x1FFF0000, 0x1FFF0000, 1920, 1080, 0x1 | 0x4 /* FULLSCREEN|SHOWN */);
		void *r = w && sdl_CreateRenderer ? sdl_CreateRenderer(w, -1, 0) : NULL;
		LOG("SDL window %p renderer %p %s", w, r, w ? "" : sdl_GetError());
		if (r) {
			sdl_SetRenderDrawBlendMode(r, 0);
			sdl_SetRenderDrawColor(r, 0, 0, 0, 0);
			sdl_RenderClear(r);
			sdl_RenderPresent(r);
		}
		for (int i = 0; i < 50; i++) {
			sdl_pump();
			usleep(10000);
		}
	}
}

static int sdl_done;

/* NDL's impl calls SDL_webOSCreateExportedWindow()/SDL_webOSSetExportedWindow() (moonlight-tv: on the app's own SDL
 * window). atlvdec exports these symbols (-rdynamic), so NDL binds to them: they log, forward to the TV's libSDL2 and
 * write the video element's window id to ATLVDEC_VIDEO_WINID_FILE; xdgshim (XDGSHIM_IMPORT) imports it into the ATL
 * window with a punch-through. */
static void *sdl_handle(void)
{
	static void *h;
	if (!h)
		h = dlopen("libSDL2-2.0.so.0", RTLD_NOW | RTLD_GLOBAL);
	return h;
}
static void *sdl_sym(const char *s) /* never dlsym(NULL): that finds our own interposer */
{
	void *h = sdl_handle();
	return h ? dlsym(h, s) : NULL;
}
static void publish_winid(const char *id)
{
	const char *f = getenv("ATLVDEC_VIDEO_WINID_FILE");
	if (!f || !*f || !id)
		return;
	char tmp[256];
	snprintf(tmp, sizeof(tmp), "%s.tmp", f);
	FILE *fp = fopen(tmp, "w");
	if (fp) {
		fputs(id, fp);
		fclose(fp);
		chmod(tmp, 0666);
		rename(tmp, f);
	}
}
/* ATL-window binding (default when ATLVDEC_WINID_FILE is set, i.e. run.sh VIDEO_EXPORT=1; ATLVDEC_OWNWIN=1 turns it
 * off): xdgshim exported the visible ATL toplevel as a webOS video element and wrote its id to ATLVDEC_WINID_FILE.
 * NDL's SDL_webOSCreateExportedWindow() gets that id instead of a new element on the helper's own (never shown) SDL
 * window, so the media pipeline puts the video under the ATL window - the moonlight-tv model with the window owned by
 * xdgshim. NDL's SDL_webOSSetExportedWindow(src, dst) for it goes to xdgshim through ATLVDEC_REGION_FILE
 * (default <winid file>.region, "sx sy sw sh dx dy dw dh"), which xdgshim applies with set_exported_window. */
static char atl_wid[128];
static const char *atlwin_file(void)
{
	const char *f = getenv("ATLVDEC_WINID_FILE"), *o = getenv("ATLVDEC_OWNWIN");
	return f && *f && !(o && !strcmp(o, "1")) ? f : NULL;
}
static const char *atlwin_id(int wait_ms)
{
	const char *f = atlwin_file();
	if (!f)
		return NULL;
	for (int t = 0; !atl_wid[0] && t <= wait_ms; t += 100) {
		FILE *fp = fopen(f, "r");
		if (fp) {
			if (!fgets(atl_wid, sizeof(atl_wid), fp))
				atl_wid[0] = 0;
			fclose(fp);
			atl_wid[strcspn(atl_wid, "\r\n")] = 0;
		}
		if (!atl_wid[0])
			usleep(100000);
	}
	return atl_wid[0] ? atl_wid : NULL;
}
static void atlwin_region(const int *src, const int *dst)
{
	const char *f = getenv("ATLVDEC_REGION_FILE");
	char def[256], tmp[300];
	if (!f || !*f) {
		snprintf(def, sizeof(def), "%s.region", atlwin_file());
		f = def;
	}
	snprintf(tmp, sizeof(tmp), "%s.tmp", f);
	FILE *fp = fopen(tmp, "w");
	if (!fp)
		return;
	fprintf(fp, "%d %d %d %d %d %d %d %d\n", src[0], src[1], src[2], src[3], dst[0], dst[1], dst[2], dst[3]);
	fclose(fp);
	chmod(tmp, 0666);
	rename(tmp, f);
}
__attribute__((visibility("default"))) const char *SDL_webOSCreateExportedWindow(int type)
{
	const char *id = atlwin_id(5000);
	if (id) {
		LOG("SDL_webOSCreateExportedWindow(%d) = %s (the ATL window, exported by xdgshim)", type, id);
		return id;
	}
	if (atlwin_file())
		LOG("no ATL window id in %s, using the helper's SDL window", atlwin_file());
	const char *(*real)(int) = sdl_sym("SDL_webOSCreateExportedWindow");
	id = real ? real(type) : NULL;
	LOG("SDL_webOSCreateExportedWindow(%d) = %s", type, id ? id : "(null)");
	publish_winid(id);
	return id;
}
__attribute__((visibility("default"))) void *SDL_webOSSetExportedWindow(const char *id, const int *src, const int *dst)
{
	if (id && atl_wid[0] && !strcmp(id, atl_wid)) {
		static const int full[4] = {0, 0, 1920, 1080};
		atlwin_region(src ? src : full, dst ? dst : full);
		LOG("SDL_webOSSetExportedWindow(%s, src %d,%d %dx%d, dst %d,%d %dx%d) -> xdgshim", id, src ? src[0] : 0, src ? src[1] : 0,
		    src ? src[2] : 1920, src ? src[3] : 1080, dst ? dst[0] : 0, dst ? dst[1] : 0, dst ? dst[2] : 1920, dst ? dst[3] : 1080);
		return (void *)1; /* SDL_TRUE */
	}
	void *(*real)(const char *, const int *, const int *) = sdl_sym("SDL_webOSSetExportedWindow");
	void *r = real ? real(id, src, dst) : NULL;
	LOG("SDL_webOSSetExportedWindow(%s, src %d,%d %dx%d, dst %d,%d %dx%d) = %p", id ? id : "-", src ? src[0] : -1, src ? src[1] : -1,
	    src ? src[2] : -1, src ? src[3] : -1, dst ? dst[0] : -1, dst ? dst[1] : -1, dst ? dst[2] : -1, dst ? dst[3] : -1, r);
	return r;
}
__attribute__((visibility("default"))) void SDL_webOSDestroyExportedWindow(const char *id)
{
	if (id && atl_wid[0] && !strcmp(id, atl_wid)) {
		LOG("SDL_webOSDestroyExportedWindow(%s): the ATL window, kept", id);
		return;
	}
	void (*real)(const char *) = sdl_sym("SDL_webOSDestroyExportedWindow");
	LOG("SDL_webOSDestroyExportedWindow(%s)", id ? id : "-");
	if (real)
		real(id);
}

static int ndl_resolve(char *msg, size_t n)
{
	void *h;
	if (!sdl_done++)
		sdl_setup();
#ifdef NDL_STUB
	h = dlopen(NULL, RTLD_NOW);
#else
	const char *lib = getenv("ATL_NDL_LIB");
	h = dlopen(lib && *lib ? lib : "libNDL_directmedia.so.1", RTLD_NOW | RTLD_GLOBAL);
#endif
	if (!h) {
		snprintf(msg, n, "dlopen: %s", dlerror());
		return -1;
	}
#define R(p, s)                                                \
	if (!(*(void **)&p = dlsym(h, s))) {                       \
		snprintf(msg, n, "missing %s", s);                     \
		return -1;                                             \
	}
	R(p_Init, "NDL_DirectMediaInit");
	R(p_Quit, "NDL_DirectMediaQuit");
	R(p_Load, "NDL_DirectMediaLoad");
	R(p_Unload, "NDL_DirectMediaUnload");
	R(p_Play, "NDL_DirectVideoPlay");
#undef R
	*(void **)&p_GetError = dlsym(h, "NDL_DirectMediaGetError");
	*(void **)&p_SetArea = dlsym(h, "NDL_DirectVideoSetArea");
	*(void **)&p_GetRenderBufferLength = dlsym(h, "NDL_DirectVideoGetRenderBufferLength");
	*(void **)&p_SetWindowId = dlsym(h, "NDL_DirectMediaSetWindowId");
	return 0;
}

static void release_cb(const char *type)
{
	LOG("NDL resource released: %s", type ? type : "?");
	loaded = 0;
}

static void load_cb(int type, long long num, const char *str)
{
	const char *n = type == 0x16 ? "LOADCOMPLETED" : type == 0x17 ? "UNLOADCOMPLETED" : type == 0x1a ? "PLAYING" : "";
	LOG("NDL callback type=%d %s num=%lld str=%.200s", type, n, num, str ? str : "");
}

/* Video area. On the TV only some SetArea calls take effect (observed: the plane stayed at the mini-player
 * rect after a later fullscreen SetArea), so the latest area is re-applied 0.3 s, 1 s and 3 s after each change
 * and after every load; fullscreen is sent as a nudge (1,1,1918,1078) then (0,0,1920,1080) - only with
 * ATLVDEC_AREA_MODE=reapply (that made the picture disappear on the TV). Default "live": one SetArea per change and
 * one after every load. ATLVDEC_AREA=full ignores ATL's rects and always uses fullscreen. */
static struct avd_area area = {0, 0, 1920, 1080};
static int area_set, area_step;
static struct timespec area_t0;
static double since(const struct timespec *t)
{
	struct timespec n;
	clock_gettime(CLOCK_MONOTONIC, &n);
	return (n.tv_sec - t->tv_sec) + (n.tv_nsec - t->tv_nsec) / 1e9;
}
static void area_apply(const char *why)
{
	if (!p_SetArea || !loaded)
		return;
	struct avd_area r = area;
	const char *f = getenv("ATLVDEC_AREA");
	if (f && !strcmp(f, "full"))
		r = (struct avd_area){0, 0, 1920, 1080};
	const char *fr = getenv("ATLVDEC_FULL_RECT");
	struct avd_area s;
	if (r.x == 0 && r.y == 0 && r.w == 1920 && r.h == 1080 && fr && sscanf(fr, "%d %d %d %d", &s.x, &s.y, &s.w, &s.h) == 4 && s.w > 0 && s.h > 0)
		r = s;
	const char *mode = getenv("ATLVDEC_AREA_MODE");
	int nudge = mode && !strcmp(mode, "reapply");
	int full = nudge && r.x == 0 && r.y == 0 && r.w == 1920 && r.h == 1080, rc0 = 0;
	if (full)
		rc0 = p_SetArea(1, 1, 1918, 1078);
	int rc = p_SetArea(r.x, r.y, r.w, r.h);
	LOG("NDL_DirectVideoSetArea(%d, %d, %d, %d) = %d%s (%s)", r.x, r.y, r.w, r.h, rc, full ? (rc0 ? " nudge failed" : " +nudge") : "", why);
}
static void area_changed(void)
{
	const char *mode = getenv("ATLVDEC_AREA_MODE");
	area_set = 1;
	area_step = mode && !strcmp(mode, "reapply") ? 0 : 3; /* default "live": one SetArea per change, no re-apply */
	clock_gettime(CLOCK_MONOTONIC, &area_t0);
	area_apply("new");
}
/* ATLVDEC_AREA_FILE=<f>: "x y w h" written by a test script; applied when it changes (polled every 0.5 s) */
static void area_file_tick(void)
{
	static struct timespec last;
	static char prev[64];
	const char *f = getenv("ATLVDEC_AREA_FILE");
	if (!f || !*f || !loaded || since(&last) < 0.5)
		return;
	clock_gettime(CLOCK_MONOTONIC, &last);
	char b[64] = "";
	FILE *fp = fopen(f, "r");
	if (!fp)
		return;
	if (!fgets(b, sizeof(b), fp))
		b[0] = 0;
	fclose(fp);
	struct avd_area r;
	if (!strcmp(b, prev) || sscanf(b, "%d %d %d %d", &r.x, &r.y, &r.w, &r.h) != 4)
		return;
	strcpy(prev, b);
	int rc = p_SetArea ? p_SetArea(r.x, r.y, r.w, r.h) : -1;
	LOG("NDL_DirectVideoSetArea(%d, %d, %d, %d) = %d (area file)", r.x, r.y, r.w, r.h, rc);
}

static void area_tick(void)
{
	static const double at[] = {0.3, 1.0, 3.0};
	if (!area_set || area_step >= 3 || since(&area_t0) < at[area_step])
		return;
	area_step++;
	area_apply("reapply");
}

static int load(void)
{
	const char *wid = getenv("ATLVDEC_WINID");
	const char *wf = getenv("ATLVDEC_WINID_FILE"); /* written by xdgshim XDGSHIM_EXPORT (app window exported as video) */
	char wbuf[128] = "";
	if ((!wid || !*wid) && wf && *wf) {
		for (int i = 0; i < 30 && !wbuf[0]; i++) {
			FILE *f = fopen(wf, "r");
			if (f) {
				if (!fgets(wbuf, sizeof(wbuf), f))
					wbuf[0] = 0;
				fclose(f);
			}
			if (!wbuf[0])
				usleep(100000);
		}
		wbuf[strcspn(wbuf, "\r\n")] = 0;
		if (!wbuf[0])
			LOG("no exported window id in %s", wf);
		wid = wbuf;
	}
	if (wid && *wid && p_SetWindowId)
		LOG("NDL_DirectMediaSetWindowId(%s) = %d", wid, p_SetWindowId(wid));
	int rc = p_Load(&info, load_cb);
	LOG("NDL_DirectMediaLoad(video type=%d %dx%d, no audio) = %d%s%s", info.video.type, info.video.width,
	    info.video.height, rc, rc ? " " : "", rc ? err() : "");
	if (rc == 0) {
		loaded = 1;
		clock_gettime(CLOCK_MONOTONIC, &load_time);
		if (area_set)
			area_changed();
	}
	return rc;
}

static int linger_on;
static struct timespec linger_t0;

static void unload(void)
{
	linger_on = 0;
	if (!loaded)
		return;
	loaded = 0;
	int rc = p_Unload();
	LOG("NDL_DirectMediaUnload() = %d (fed %lu, failed %lu)", rc, nfeed, nfail);
}

/* Stream ended (CLOSE / disconnect / replaced). ExoPlayer re-creates the codec when the app swaps or detaches its
 * Surface (SDK < 23 path) and waits only 2 s ("Detaching surface timed out"), but an NDL load takes ~2 s: so the
 * decoder stays loaded for ATLVDEC_LINGER seconds (default 3, 0 = unload at once) and an OPEN with the same codec
 * and size in that time reuses it (replies at once; the new codec's first AU carries csd, the decoder continues). */
static double linger_s(void)
{
	const char *l = getenv("ATLVDEC_LINGER");
	return l && *l ? atof(l) : 3.0;
}
static void unload_later(const char *why)
{
	if (!loaded || linger_s() <= 0) {
		unload();
		memset(&info, 0, sizeof(info));
		return;
	}
	if (linger_on)
		return; /* CLOSE, then the disconnect */
	linger_on = 1;
	clock_gettime(CLOCK_MONOTONIC, &linger_t0);
	LOG("%s: decoder stays loaded for %.1f s", why, linger_s());
}
static void linger_tick(void)
{
	if (linger_on && since(&linger_t0) >= linger_s()) {
		LOG("linger over");
		unload();
		memset(&info, 0, sizeof(info));
	}
}

static long long ms_since_load(void)
{
	struct timespec now;
	clock_gettime(CLOCK_MONOTONIC, &now);
	return (now.tv_sec - load_time.tv_sec) * 1000LL + (now.tv_nsec - load_time.tv_nsec) / 1000000;
}

static int read_full(int fd, void *buf, size_t n)
{
	char *p = buf;
	while (n) {
		ssize_t r = read(fd, p, n);
		if (r < 0 && errno == EINTR)
			continue;
		if (r <= 0)
			return -1;
		p += r;
		n -= r;
	}
	return 0;
}

static int write_full(int fd, const void *buf, size_t n)
{
	const char *p = buf;
	while (n) {
		ssize_t r = send(fd, p, n, MSG_NOSIGNAL);
		if (r < 0 && errno == EINTR)
			continue;
		if (r <= 0)
			return -1;
		p += r;
		n -= r;
	}
	return 0;
}

static void reply(int fd, int status, const char *msg)
{
	struct {
		struct avd_hdr h;
		struct avd_result r;
	} m;
	memset(&m, 0, sizeof(m));
	m.h.type = AVD_RESULT;
	m.h.len = sizeof(m.r);
	m.r.status = status;
	snprintf(m.r.msg, sizeof(m.r.msg), "%s", msg);
	write_full(fd, &m, sizeof(m));
}

static void do_open(int fd, const struct avd_open *o)
{
	char msg[160] = "";
	if (!p_Play && ndl_resolve(msg, sizeof(msg))) {
		LOG("NDL unavailable: %s", msg);
		reply(fd, -1, msg);
		return;
	}
	if (!ndl_inited) {
		const char *appid = getenv("APPID");
		int rc = p_Init(appid ? appid : "", release_cb);
		LOG("NDL_DirectMediaInit(%s) = %d%s%s", appid ? appid : "", rc, rc ? " " : "", rc ? err() : "");
		if (rc) {
			snprintf(msg, sizeof(msg), "init %d %s", rc, err());
			reply(fd, -2, msg);
			return;
		}
		ndl_inited = 1;
	}
	int type = o->codec == AVD_CODEC_HEVC ? 2 : 1, w = o->width ? (int)o->width : 1920, h = o->height ? (int)o->height : 1080;
	if (loaded && info.video.type == type && info.video.width == w && info.video.height == h) {
		linger_on = 0;
		LOG("OPEN reuses the loaded decoder (fed %lu)", nfeed);
		reply(fd, 0, "ok (reused)");
		return;
	}
	unload();
	memset(&info, 0, sizeof(info));
	info.video.type = o->codec == AVD_CODEC_HEVC ? 2 : 1;
	info.video.width = o->width ? (int)o->width : 1920;
	info.video.height = o->height ? (int)o->height : 1080;
	nfeed = nfail = 0;
	int rc = load();
	if (rc) {
		snprintf(msg, sizeof(msg), "load %d %s", rc, err());
		reply(fd, -3, msg);
		return;
	}
	reply(fd, 0, "ok");
}

static long long mono_ns(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec * 1000000000LL + t.tv_nsec;
}

/* pacing stats, logged every 300 feeds: gaps between feeds, how far ahead of its due time an AU arrived, late feeds */
static struct {
	long long last_ns, gap_max, gap_sum, early_max, early_sum;
	unsigned long n, gaps50, late, nodue;
} st;

static void pace_log(void);
static unsigned long sm_resets, sm_count;
static long long sm_err_sum;
static void smooth_reset(void);
static void do_feed(void *buf, uint32_t len, long long due_ns, long long recv_ns)
{
	if (!loaded) {
		/* resource was taken (e.g. by the TV) or a FLUSH reload failed: try to get it back */
		if (!info.video.type || load())
			return;
	}
	long long pts = ms_since_load(), now = mono_ns();
	int rc = p_Play(buf, len, pts);
	nfeed++;
	if (rc)
		nfail++;
	if (st.last_ns) {
		long long g = now - st.last_ns;
		st.gap_sum += g;
		if (g > st.gap_max)
			st.gap_max = g;
		if (g > 50000000)
			st.gaps50++;
	}
	st.last_ns = now;
	st.n++;
	if (!due_ns)
		st.nodue++;
	else {
		long long e = due_ns - recv_ns;
		st.early_sum += e;
		if (e > st.early_max)
			st.early_max = e;
		if (now - due_ns > 10000000)
			st.late++;
	}
	if (nfeed <= 5 || nfeed % 300 == 0 || (rc && nfail <= 10)) {
		int bl = -1;
		if (p_GetRenderBufferLength)
			p_GetRenderBufferLength(&bl);
		LOG("NDL_DirectVideoPlay(#%lu, %u bytes, pts %lld ms) = %d%s%s renderbuf=%d", nfeed, len, pts, rc,
		    rc ? " " : "", rc ? err() : "", bl);
	}
	if (st.n >= 300)
		pace_log();
}

static void pace_log(void)
{
	if (st.n) {
		unsigned long d = st.n - st.nodue;
		LOG("pacing: %lu feeds, gap avg %.1f max %.1f ms, %lu gaps > 50 ms; due-time early avg %.1f max %.1f ms, %lu late > 10 ms, %lu without due",
		    st.n, st.gap_sum / 1e6 / (st.n > 1 ? st.n - 1 : 1), st.gap_max / 1e6, st.gaps50, d ? st.early_sum / 1e6 / d : 0.0,
		    st.early_max / 1e6, st.late, st.nodue);
		if (sm_count)
			LOG("smoothing: %lu dues, ExoPlayer jitter avg %.1f ms, %lu grid restarts", sm_count, sm_err_sum / 1e6 / sm_count, sm_resets);
		sm_count = sm_resets = 0;
		sm_err_sum = 0;
	}
	long long keep = st.last_ns;
	memset(&st, 0, sizeof(st));
	st.last_ns = keep;
}

/* AUs waiting for their due time (AVD_DUE before the FEEDs of one MediaCodec.releaseOutputBuffer) */
struct pend {
	void *buf;
	uint32_t len;
	long long due, recv;
};
static struct pend *pq;
static int pq_n, pq_cap;
static long long next_due;

/* ATLVDEC_SMOOTH (default 1; 0 = feed at ExoPlayer's due times as given). ExoPlayer snaps each frame's release time
 * to ATL's 60 Hz Choreographer grid, which is not phase-locked to the panel, and derives it from ATL's audio clock,
 * which jitters: due times wander several ms around the frame grid, and in low-delay mode NDL shows a frame on the
 * first refresh after it is decoded, so frames land on random refreshes (uneven, "gappy" motion even at 0 drops).
 * Smoothing feeds on a steady grid instead: frame period = mean of recent due deltas; each due is mapped to the next
 * slot(s) of the grid and the grid's phase follows ExoPlayer slowly (1/16 of the error per frame), which keeps A/V
 * sync. A jump (seek, pause, speed change, > 3 slots or backwards) restarts the grid at the given due. */
static long long sm_hist[16], sm_raw, sm_sched;
static int sm_n, sm_i;

static int smooth_on(void)
{
	const char *s = getenv("ATLVDEC_SMOOTH");
	return !(s && !strcmp(s, "0"));
}

/* ATLVDEC_SMOOTH_DELAY_MS (default 20): show frames this much after ExoPlayer's due time, so a frame released only
 * just before its due time is still waiting when its grid slot comes (ExoPlayer releases 15-50 ms early); below
 * lip-sync perception (TVs already add ~20-40 ms of their own) */
static long long smooth_delay_ns(void)
{
	const char *s = getenv("ATLVDEC_SMOOTH_DELAY_MS");
	return (s && *s ? atoll(s) : 20) * 1000000LL;
}

static void smooth_reset(void)
{
	sm_raw = sm_sched = 0;
	sm_n = sm_i = 0;
}

static long long smooth_due(long long d)
{
	if (!smooth_on() || !d)
		return d;
	if (sm_raw && d > sm_raw && d - sm_raw < 200000000LL) {
		sm_hist[sm_i] = d - sm_raw;
		sm_i = (sm_i + 1) % 16;
		if (sm_n < 16)
			sm_n++;
	}
	sm_raw = d;
	if (sm_n < 8 || !sm_sched) {
		sm_sched = d;
		return d + smooth_delay_ns();
	}
	long long p = 0;
	for (int i = 0; i < sm_n; i++)
		p += sm_hist[i];
	p /= sm_n;
	if (p <= 0) {
		sm_sched = d;
		return d;
	}
	long long k = (d - sm_sched + p / 2) / p; /* grid slots since the last scheduled frame */
	if (d < sm_sched || k > 3) {
		sm_resets++;
		smooth_reset();
		sm_raw = d;
		return d;
	}
	if (k < 1)
		k = 1;
	long long pred = sm_sched + k * p, err = d - pred;
	sm_sched = pred + err / 16;
	sm_count++;
	sm_err_sum += err < 0 ? -err : err;
	return sm_sched + smooth_delay_ns();
}

static int pace_on(void)
{
	const char *p = getenv("ATLVDEC_PACE");
	return !(p && !strcmp(p, "0"));
}
static long long lead_ns(void)
{
	const char *l = getenv("ATLVDEC_LEAD_MS");
	return (l && *l ? atoll(l) : 15) * 1000000LL;
}
static void pq_drop(void)
{
	for (int i = 0; i < pq_n; i++)
		free(pq[i].buf);
	pq_n = 0;
}
/* feed what is due; returns ms until the next AU is due (-1: none waiting) */
static int pq_run(void)
{
	int i = 0;
	long long now = mono_ns();
	while (i < pq_n && pq[i].due - lead_ns() <= now) {
		do_feed(pq[i].buf, pq[i].len, pq[i].due, pq[i].recv);
		free(pq[i].buf);
		i++;
		now = mono_ns();
	}
	if (i) {
		pq_n -= i;
		memmove(pq, pq + i, pq_n * sizeof(*pq));
	}
	if (!pq_n)
		return -1;
	long long w = pq[0].due - lead_ns() - now;
	return w <= 0 ? 0 : (int)(w / 1000000) + 1;
}
static void feed_msg(void *buf, uint32_t len)
{
	long long now = mono_ns(), due = next_due;
	if (due && due - now > 200000000LL)
		due = now + 200000000LL; /* ExoPlayer releases <= 50 ms early; never hold an AU longer than 0.2 s */
	if (!pace_on() || ((!due || due - lead_ns() <= now) && !pq_n)) {
		do_feed(buf, len, due, now);
		return;
	}
	if (pq_n && due < pq[pq_n - 1].due)
		due = pq[pq_n - 1].due; /* keep decode order */
	if (pq_n == pq_cap) {
		pq_cap = pq_cap ? pq_cap * 2 : 16;
		pq = realloc(pq, pq_cap * sizeof(*pq));
	}
	void *c = malloc(len ? len : 1);
	memcpy(c, buf, len);
	pq[pq_n++] = (struct pend){c, len, due, now};
	pq_run();
}

static void on_term(int s)
{
	(void)s;
	quit = 1;
}

int main(int argc, char **argv)
{
	if (argc < 2) {
		fprintf(stderr, "usage: atlvdec <socket-path>\n");
		return 2;
	}
	setvbuf(stderr, NULL, _IOLBF, 0);
	struct sigaction sa = {.sa_handler = on_term};
	sigaction(SIGTERM, &sa, NULL);
	sigaction(SIGINT, &sa, NULL);
	sigaction(SIGHUP, &sa, NULL);
	signal(SIGPIPE, SIG_IGN);

	struct sockaddr_un a = {.sun_family = AF_UNIX};
	snprintf(a.sun_path, sizeof(a.sun_path), "%s", argv[1]);
	int ls = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	unlink(a.sun_path);
	if (ls < 0 || bind(ls, (struct sockaddr *)&a, sizeof(a)) || listen(ls, 4)) {
		LOG("cannot listen on %s: %s", a.sun_path, strerror(errno));
		return 1;
	}
	chmod(a.sun_path, 0777);
	LOG("listening on %s (appid %s)", a.sun_path, getenv("APPID") ? getenv("APPID") : "-");
	const char *early = getenv("ATLVDEC_EARLY");
#ifndef NDL_STUB
	if (!early || strcmp(early, "0"))
#else
	if (early && !strcmp(early, "1"))
#endif
		if (!sdl_done++) {
			sdl_setup();
			/* NDL init now too (~0.5 s), so OPEN only has to load */
			char msg[160];
			/* ATL-window binding: NDL init exports the window, so it waits for the ATL window (first OPEN) */
			if (getenv("APPID") && !atlwin_file() && !ndl_resolve(msg, sizeof(msg))) {
				int rc = p_Init(getenv("APPID"), release_cb);
				LOG("NDL_DirectMediaInit(%s) = %d%s%s", getenv("APPID"), rc, rc ? " " : "", rc ? err() : "");
				ndl_inited = rc == 0;
			}
		}

	int cl = -1;
	size_t cap = 0;
	char *buf = NULL;
	while (!quit) {
		struct pollfd p[2] = {{ls, POLLIN, 0}, {cl, POLLIN, 0}};
		int to = sdl_on || (area_set && area_step < 3) || getenv("ATLVDEC_AREA_FILE") ? 20 : linger_on ? 100 : -1;
		int pw = pq_run();
		if (pw >= 0 && (to < 0 || pw < to))
			to = pw;
		int n = poll(p, cl >= 0 ? 2 : 1, to);
		pq_run();
		linger_tick();
		area_tick();
		area_file_tick();
		sdl_pump();
		if (n < 0) {
			if (errno == EINTR)
				continue;
			break;
		}
		if (p[0].revents & POLLIN) {
			int c = accept4(ls, NULL, NULL, SOCK_CLOEXEC);
			if (c >= 0) {
				if (cl >= 0) {
					pq_drop();
					next_due = 0;
					unload_later("new client replaces the current stream");
					close(cl);
				}
				cl = c;
				LOG("client connected");
			}
			continue;
		}
		if (cl < 0 || !(p[1].revents & (POLLIN | POLLHUP | POLLERR)))
			continue;
		struct avd_hdr h;
		if (read_full(cl, &h, sizeof(h)) || h.len > AVD_MAX_PAYLOAD) {
			LOG("client disconnected");
			pq_drop();
			pace_log();
			st.last_ns = 0;
			next_due = 0;
			unload_later("disconnect");
			close(cl);
			cl = -1;
			continue;
		}
		if (h.len + 64 > cap) {
			cap = h.len + 65536;
			buf = realloc(buf, cap);
		}
		if (h.len && read_full(cl, buf, h.len)) {
			LOG("client disconnected (short message)");
			unload();
			close(cl);
			cl = -1;
			continue;
		}
		memset(buf + h.len, 0, 64);
		switch (h.type) {
			case AVD_OPEN: {
				struct avd_open o = {0};
				memcpy(&o, buf, h.len < sizeof(o) ? h.len : sizeof(o));
				LOG("OPEN codec=%s %ux%u", o.codec == AVD_CODEC_HEVC ? "hevc" : "h264", o.width, o.height);
				pq_drop();
				pace_log();
				st.last_ns = 0;
				next_due = 0;
				do_open(cl, &o);
				break;
			}
			case AVD_FEED:
				feed_msg(buf, h.len);
				break;
			case AVD_DUE: {
				int64_t d = 0;
				memcpy(&d, buf, h.len < sizeof(d) ? h.len : sizeof(d));
				next_due = smooth_due(d);
				break;
			}
			case AVD_AREA: {
				struct avd_area r = {0};
				memcpy(&r, buf, h.len < sizeof(r) ? h.len : sizeof(r));
				const char *am = getenv("ATLVDEC_AREA");
				if (am && !strcmp(am, "none"))
					LOG("AREA %d,%d %dx%d from ATL ignored (ATLVDEC_AREA=none)", r.x, r.y, r.w, r.h);
				else if (r.w > 0 && r.h > 0) {
					area = r;
					area_changed();
				}
				break;
			}
			case AVD_FLUSH:
				LOG("FLUSH");
				pq_drop();
				next_due = 0;
				smooth_reset();
				unload();
				load();
				break;
			case AVD_CLOSE:
				LOG("CLOSE");
				for (int i = 0; i < pq_n; i++) /* end of stream: the last AUs at once */
					do_feed(pq[i].buf, pq[i].len, pq[i].due, pq[i].recv);
				pq_drop();
				pace_log();
				st.last_ns = 0;
				next_due = 0;
				unload_later("CLOSE");
				break;
			default:
				LOG("unknown message %u", h.type);
		}
	}
	unload();
	if (ndl_inited) {
		int rc = p_Quit();
		LOG("NDL_DirectMediaQuit() = %d", rc);
	}
	unlink(a.sun_path);
	LOG("exit");
	return 0;
}
