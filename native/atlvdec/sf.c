/* SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Starfish backend of atlvdec (see sf.h): LG's StarfishMediaAPIs (libplayerAPIs.so.1), reached in C.
 *
 * How the library is called follows the Native App Kit's media/src/starfish_backend.cpp (webos-native-app-kit,
 * GPL-3.0-or-later; which in turn follows PlxNative's src/starfish.c, GPL-3.0-or-later, and Kodi's
 * MediaPipelineWebOS.cpp, GPL-2.0-or-later): the mangled entry points, the object constructed in place in a 64 KiB
 * block (never freed or reused: a late callback carries only its address), the std::string Feed reply through a
 * hidden sret pointer, the BUFFERSTREAM Load payload, the frame-rate envelope, setHdrInfo, getMaxVideoResolution and
 * the Unload -> UnloadCompleted (type 23) -> destructor order. CSWeb (cloudstream-webos-native) plays through that
 * code on the TV. All C++ calls are plain function calls on the ARM EABI (this in r0, sret before it).
 *
 * What is atlvdec's own: ATL is a video-only client whose master clock is the Android app's (ExoPlayer derives its
 * release times from the audio it plays through PulseAudio). The pipeline is loaded without audio and not in
 * low-delay mode, so it presents frames by pts at the declared frame rate (the smooth cadence CSWeb gets); sf.c
 * maps content pts to the pipeline's timeline and keeps the pipeline's clock on the app's due times:
 *   - Play is held until the first due time and issued so that frame lands on it;
 *   - each due time is compared with when the pipeline presents that frame (its clock, from FRAMEREADY events),
 *     and the difference is removed by shifting the pts of AUs not yet fed, a little per AU (never backwards past
 *     the pipeline's clock: late frames stay late on Starfish);
 *   - when the app stops releasing (pause, buffering) the pipeline is paused before it runs ahead of the last
 *     released frame, and the next due time restarts it in step; a seek holds the clock and splices the timeline
 *     (no flush, no reload: Starfish's flush wedges the sink, NDL's reload took ~2 s).
 * Env: ATLVDEC_SF_SYNC_MS (default 0) shows frames that much after their due time (display latency trim),
 *      ATLVDEC_SF_LOWDELAY=1 / ATLVDEC_SF_AUDIOSYNC=1 put those flags in the Load payload (default off),
 *      ATLVDEC_SF_DEBUG=1 logs every due time. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "sf.h"

void avd_log(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
#define LOG(...) avd_log("starfish: " __VA_ARGS__)
/* atlvdec.c: places the exported ATL window's video (forwarded to xdgshim) */
void *SDL_webOSSetExportedWindow(const char *id, const int *src, const int *dst);

#define OBJ_SIZE 65536
#define SPLICE_GAP_US 40000LL   /* new content after a seek lands this far after the last pts fed */
#define START_US 40000LL        /* the first AU of a load goes to 40 ms on the pipeline's timeline (ATLVDEC_SF_START_MS) */
#define NSAMP 16                /* FRAMEREADY samples kept for the clock estimate (they come ~5 per second) */
#define FEED_LEAD_US 150000LL   /* an AU is never mapped closer than this to the pipeline's clock */
#define DEADBAND_NS 5000000LL   /* sync errors below this are left alone */
#define RESYNC_NS 250000000LL   /* video this far early: hold the clock and restart in step */
#define RING 256

typedef void (*sf_event_cb)(int type, long long num, const char *str, void *ctx);

/* libstdc++ std::__cxx11::string (pointer, length, 16-byte local buffer): getMaxVideoResolution takes one by
 * value, i.e. by invisible reference on the ARM EABI; the caller owns (and here never destroys) it */
struct cxx_string {
	char *p;
	size_t len;
	union {
		char buf[16];
		size_t cap;
	} u;
};

static struct {
	void *h;
	void (*ctor)(void *self, const char *uid);
	void (*dtor)(void *self);
	int (*load)(void *self, const char *payload, sf_event_cb cb, void *ctx);
	void (*feed)(void *sret, void *self, const char *payload);
	int (*play)(void *self);
	int (*pause)(void *self);
	int (*unload)(void *self);
	int (*push_eos)(void *self);
	void (*notify_fg)(void *self);
	int (*max_res)(struct cxx_string *codec, int32_t *w, int32_t *h, int32_t *fps); /* optional */
	int (*set_hdr)(void *self, const char *payload);                              /* optional */
} A;
static int api_tried, api_ok;
static char api_why[160], codecs[48];
static struct {
	int known, w, h, fps;
} lim[5]; /* by AVD_CODEC_* */

static const char *codec_name(uint32_t c)
{
	return c == AVD_CODEC_HEVC ? "H265" : c == AVD_CODEC_VP9 ? "VP9" : c == AVD_CODEC_AV1 ? "AV1" : "H264";
}

/* an answer larger than the default it gives for any name (1920x1088) names a real decoder */
static int listed(uint32_t c)
{
	return c < 5 && lim[c].known && (long long)lim[c].w * lim[c].h > 1920LL * 1088;
}

int sf_available(char *why, size_t n)
{
	if (!api_tried) {
		api_tried = 1;
#ifdef SF_STUB
		void *h = dlopen(NULL, RTLD_NOW);
#else
		void *h = dlopen("libplayerAPIs.so.1", RTLD_NOW | RTLD_GLOBAL);
#endif
		if (!h) {
			snprintf(api_why, sizeof(api_why), "dlopen libplayerAPIs.so.1: %s", dlerror());
			goto out;
		}
#define R(p, s)                                                     \
	if (!(*(void **)&A.p = dlsym(h, s))) {                          \
		snprintf(api_why, sizeof(api_why), "missing %s", s);        \
		goto out;                                                   \
	}
		R(ctor, "_ZN17StarfishMediaAPIsC1EPKc");
		R(dtor, "_ZN17StarfishMediaAPIsD1Ev");
		R(load, "_ZN17StarfishMediaAPIs4LoadEPKcPFvixS1_PvES2_");
		R(feed, "_ZN17StarfishMediaAPIs4FeedB5cxx11EPKc");
		R(play, "_ZN17StarfishMediaAPIs4PlayEv");
		R(pause, "_ZN17StarfishMediaAPIs5PauseEv");
		R(unload, "_ZN17StarfishMediaAPIs6UnloadEv");
		R(push_eos, "_ZN17StarfishMediaAPIs7pushEOSEv");
		R(notify_fg, "_ZN17StarfishMediaAPIs16notifyForegroundEv");
#undef R
		*(void **)&A.max_res = dlsym(h, "_ZN3smp4util21getMaxVideoResolutionENSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEEPiS7_S7_");
		if (!A.max_res)
			*(void **)&A.max_res = dlsym(h, "_ZN3smp4util21getMaxVideoResolutionERKNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEEPiS8_S8_");
		*(void **)&A.set_hdr = dlsym(h, "_ZN17StarfishMediaAPIs10setHdrInfoEPKc");
		A.h = h;
		api_ok = 1;
		strcpy(codecs, "h264 hevc");
		for (uint32_t c = AVD_CODEC_H264; c <= AVD_CODEC_AV1; c++) {
			if (!A.max_res)
				break;
			struct cxx_string s;
			memset(&s, 0, sizeof(s));
			s.p = s.u.buf;
			s.len = strlen(codec_name(c));
			memcpy(s.u.buf, codec_name(c), s.len + 1);
			int32_t w = 0, hh = 0, f = 0;
			lim[c].known = (A.max_res(&s, &w, &hh, &f) & 0xFF) != 0;
			lim[c].w = w, lim[c].h = hh, lim[c].fps = f;
			LOG("decoder %s: %s %dx%d@%d", codec_name(c), lim[c].known ? "reported" : "not reported", w, hh, f);
		}
		if (listed(AVD_CODEC_VP9))
			strcat(codecs, " vp9");
		if (listed(AVD_CODEC_AV1))
			strcat(codecs, " av1");
		LOG("libplayerAPIs ready (%s; setHdrInfo %s)", codecs, A.set_hdr ? "yes" : "no");
	}
out:
	if (!api_ok && why)
		snprintf(why, n, "%s", api_why);
	return api_ok ? 0 : -1;
}

const char *sf_codecs(void)
{
	return api_ok ? codecs : "";
}

int sf_codec_ok(uint32_t c)
{
	return api_ok && (c == AVD_CODEC_H264 || c == AVD_CODEC_HEVC || ((c == AVD_CODEC_VP9 || c == AVD_CODEC_AV1) && listed(c)));
}

static int64_t mono_ns(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec * 1000000000LL + t.tv_nsec;
}

static int env_on(const char *n)
{
	const char *v = getenv(n);
	return v && !strcmp(v, "1");
}

/* ---------------------------------------------------------------------------------------------- events (any thread) */

static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t cv = PTHREAD_COND_INITIALIZER;
static unsigned ev_epoch;                  /* events with another epoch are dropped */
static int ev_load_completed, ev_unload_completed, ev_first_frame, ev_eos;
static int64_t fr_out_us[NSAMP], fr_at_ns[NSAMP]; /* recent FRAMEREADY: presented pts (pipeline timeline) and when */
static int fr_n, fr_i;
static int64_t fr_valid_after_ns;          /* samples older than the last Play/Pause are ignored */
static long long ev_displayed, ev_dropped, ev_frameready;

static void on_event(int type, long long num, const char *str, void *ctx)
{
	unsigned e = (unsigned)(uintptr_t)ctx;
	int64_t now = mono_ns();
	pthread_mutex_lock(&mu);
	if (e != ev_epoch) {
		pthread_mutex_unlock(&mu);
		return;
	}
	if (type == 0) { /* FRAMEREADY: num = presented pts in ns */
		ev_frameready++;
		if (now > fr_valid_after_ns) {
			fr_out_us[fr_i] = num / 1000;
			fr_at_ns[fr_i] = now;
			fr_i = (fr_i + 1) % NSAMP;
			if (fr_n < NSAMP)
				fr_n++;
		}
		int first = !ev_first_frame;
		ev_first_frame = 1;
		pthread_mutex_unlock(&mu);
		if (first)
			LOG("first frame presented (pts %.3f s)", num / 1e9);
		return;
	}
	if (type == 48 || type == 49) { /* the sink's dropped / displayed frames per poll (webOS 5+ numbering) */
		if (type == 48)
			ev_dropped += num;
		else
			ev_displayed += num;
		pthread_mutex_unlock(&mu);
		return;
	}
	if (type == 23) {
		ev_unload_completed = 1;
		pthread_cond_broadcast(&cv);
	}
	const char *s = str ? str : "";
	if (strstr(s, "loadCompleted") || strstr(s, "\"loaded\""))
		ev_load_completed = 1;
	if (strstr(s, "endOfStream"))
		ev_eos = 1;
	int refused = type == 18 && !ev_first_frame;
	pthread_mutex_unlock(&mu);
	/* some events (e.g. 44 on webOS 10, about every 0.5-5 s) carry a binary struct, not text: don't log the bytes */
	int text = *s == '{' || *s == '[';  /* JSON may hold UTF-8 */
	if (!text) {
		const unsigned char *p = (const unsigned char *)s;
		while (*p && (p - (const unsigned char *)s) < 300 && ((*p >= 0x20 && *p < 0x7f) || *p == '\n' || *p == '\t'))
			p++;
		text = !*p || (p - (const unsigned char *)s) >= 300;
	}
	LOG("event type=%d num=%lld %.300s%s", type, num, text ? s : "(binary)", refused ? " (pipeline refused the stream)" : "");
}

/* ------------------------------------------------------------------------------------------- state (main thread) */

static void *obj;
static int loaded;
static unsigned epoch;
static struct avd_open cur;
static char wid[128];
static int area[4] = {0, 0, 1920, 1080};

static int playing, replayed;
static int64_t play_at_ns;   /* scheduled Play (0: none) */
static int64_t hold_k_us;    /* the pipeline's clock (timeline us) while it is held */
static int64_t c_guess_ns;   /* when timeline 0 is presented, until FRAMEREADY says */

static int have_shift, splice_pending, realign;
static int64_t hold_at_us; /* after a seek: hold the clock when it reaches this (where the new content starts) */
static int64_t shift_us, max_out_us;
static int64_t corr_left_us, corr_step_us, corr_until_pts; /* pending timeline correction, applied per AU */
/* clock drift between the app (its audio) and the pipeline: corrections per second, fed in continuously */
static double drift;               /* timeline us per content us */
static int64_t drift_t0_ns, drift_sum_us, drift_last_pts;
static unsigned drift_n;
static int64_t frame_us, last_due_pts;
static struct {
	int64_t pts, out;
} ring[RING];
static int ring_n, ring_i;

static int64_t rel_pts, rel_out, rel_ns; /* the last frame the app released */
static unsigned long feeds_total;
static int64_t err_f_ns;
static int err_n;

static struct {
	unsigned long dues, timed, corrections, holds, resyncs, feeds, full, errors;
	int64_t err_abs_sum, err_max;
	int64_t last_ns;
} st;

static int64_t clock_c_ns(void)
{
	int64_t c = c_guess_ns;
	pthread_mutex_lock(&mu);
	for (int i = 0; i < fr_n; i++) {
		int64_t ci = fr_at_ns[i] - fr_out_us[i] * 1000; /* callbacks come after the frame: the smallest is closest */
		if (i == 0 || ci < c)
			c = ci;
	}
	pthread_mutex_unlock(&mu);
	return c;
}

static int clock_samples(void)
{
	pthread_mutex_lock(&mu);
	int n = fr_n;
	pthread_mutex_unlock(&mu);
	return n;
}

static int64_t pos_out_us(int64_t now)
{
	return playing ? (now - clock_c_ns()) / 1000 : hold_k_us;
}

static void samples_reset(int64_t now)
{
	pthread_mutex_lock(&mu);
	fr_n = fr_i = 0;
	fr_valid_after_ns = now + 30000000LL; /* a callback for a frame presented before the Play/Pause */
	pthread_mutex_unlock(&mu);
}

static void do_play(int64_t now, const char *why)
{
	play_at_ns = 0;
	int rc = A.play(obj);
	playing = 1;
	c_guess_ns = now - hold_k_us * 1000;
	samples_reset(now);
	LOG("Play -> %d (%s, clock at %.3f s)", rc & 0xFF, why, hold_k_us / 1e6);
}

static void do_pause(int64_t now, const char *why)
{
	play_at_ns = 0;
	if (!playing)
		return;
	hold_k_us = pos_out_us(now);
	int rc = A.pause(obj);
	playing = 0;
	samples_reset(now);
	st.holds++;
	LOG("Pause -> %d (%s, clock held at %.3f s)", rc & 0xFF, why, hold_k_us / 1e6);
}

static int64_t ring_find(int64_t pts)
{
	for (int k = 0, i = ring_i; k < ring_n; k++) {
		i = (i + RING - 1) % RING;
		if (ring[i].pts == pts)
			return ring[i].out;
	}
	return INT64_MIN;
}

static int64_t map_pts(int64_t pts)
{
	int64_t out = ring_find(pts); /* a retried AU (BufferFull) keeps its place */
	if (out != INT64_MIN)
		return out;
	if (!have_shift) {
		have_shift = 1;
		const char *st0 = getenv("ATLVDEC_SF_START_MS");
		shift_us = (st0 && *st0 ? atoll(st0) * 1000 : START_US) - pts;
	} else if (splice_pending) {
		/* after the last AU fed (the pipeline keeps what it has: no flush) and never closer than FEED_LEAD to its clock */
		splice_pending = 0;
		int64_t floor_us = playing ? pos_out_us(mono_ns()) + FEED_LEAD_US : hold_k_us;
		int64_t base = (max_out_us > floor_us ? max_out_us : floor_us) + SPLICE_GAP_US;
		shift_us = base - pts;
		if (playing)
			hold_at_us = base - SPLICE_GAP_US / 2; /* what was fed before plays out, then the clock waits for the app */
		LOG("splice: pts %.3f s -> %.3f s", pts / 1e6, base / 1e6);
	}
	if (corr_left_us) {
		/* faster while far off (after a seek or a late start): frames then come quicker than real time briefly */
		int64_t step = corr_left_us > 100000 || corr_left_us < -100000 ? corr_step_us * 3 : corr_step_us;
		int64_t s = corr_left_us > step ? step : corr_left_us < -step ? -step : corr_left_us;
		shift_us += s;
		corr_left_us -= s;
		if (!corr_left_us)
			corr_until_pts = pts; /* frames from here on show the whole correction */
	}
	if (drift != 0 && drift_last_pts != INT64_MIN && pts > drift_last_pts && pts - drift_last_pts < 1000000)
		shift_us += (int64_t)(drift * (pts - drift_last_pts));
	if (drift_last_pts == INT64_MIN || pts > drift_last_pts)
		drift_last_pts = pts;
	out = pts + shift_us;
	if (playing) { /* never at or behind the pipeline's clock (the frame would be dropped): give up the rest */
		int64_t min_out = pos_out_us(mono_ns()) + FEED_LEAD_US;
		if (out < min_out) {
			shift_us += min_out - out;
			out = min_out;
			corr_left_us = 0;
			corr_until_pts = pts;
		}
	}
	if (out < 0)
		out = 0;
	if (out > max_out_us)
		max_out_us = out;
	ring[ring_i].pts = pts;
	ring[ring_i].out = out;
	ring_i = (ring_i + 1) % RING;
	if (ring_n < RING)
		ring_n++;
	return out;
}

static void state_reset(void)
{
	playing = replayed = realign = 0;
	hold_at_us = 0;
	play_at_ns = hold_k_us = c_guess_ns = 0;
	have_shift = splice_pending = 0;
	shift_us = max_out_us = 0;
	corr_left_us = corr_until_pts = 0;
	corr_step_us = 2000;
	drift = 0;
	drift_t0_ns = drift_sum_us = 0;
	drift_last_pts = INT64_MIN;
	drift_n = 0;
	last_due_pts = INT64_MIN;
	ring_n = ring_i = 0;
	rel_pts = rel_out = rel_ns = 0;
	err_f_ns = 0;
	err_n = 0;
	feeds_total = 0;
	memset(&st, 0, sizeof(st));
	st.last_ns = mono_ns();
}

/* ------------------------------------------------------------------------------------------------------- payloads */

static void json_str(char *o, size_t n, const char *s)
{
	size_t k = 0;
	for (; *s && k + 2 < n; s++) {
		if (*s == '"' || *s == '\\')
			o[k++] = '\\';
		if ((unsigned char)*s >= 0x20)
			o[k++] = *s;
	}
	o[k] = 0;
}

static int rate_class(double fps)
{
	static const int c[] = {24, 25, 30, 50, 60};
	for (int i = 0; i < 5; i++)
		if (fps <= c[i] + 0.01)
			return c[i];
	return (int)(fps + 0.999);
}

static void rate_fraction(uint32_t milli, long long *num, long long *den)
{
	static const struct {
		uint32_t milli;
		long long num, den;
	} k[] = {{23976, 24000, 1001}, {29970, 30000, 1001}, {59940, 60000, 1001}, {119880, 120000, 1001}};
	for (unsigned i = 0; i < sizeof(k) / sizeof(k[0]); i++)
		if (milli + 5 >= k[i].milli && milli <= k[i].milli + 5) {
			*num = k[i].num, *den = k[i].den;
			return;
		}
	if (milli % 1000 == 0) {
		*num = milli / 1000, *den = 1;
		return;
	}
	*num = milli, *den = 1000;
}

static void build_load(char *p, size_t n, const struct avd_open *o)
{
	char app[160], win[160];
	json_str(app, sizeof(app), getenv("APPID") ? getenv("APPID") : "");
	json_str(win, sizeof(win), wid);
	int fhd = o->codec == AVD_CODEC_H264 && o->width && o->height && o->width <= 1920 && o->height <= 1088;
	int mw = fhd ? 1920 : 3840, mh = fhd ? 1080 : 2160; /* webOS 10.3.1 refuses H.264 declared as 4K */
	if (listed(o->codec)) {
		if (lim[o->codec].w < mw)
			mw = lim[o->codec].w;
		if (lim[o->codec].h < mh)
			mh = lim[o->codec].h;
	}
	double fps = o->fps_milli / 1000.0;
	int mfps = o->codec == AVD_CODEC_H264 && fps > 0 ? rate_class(fps) : 60;
	char es[160] = "{\"pauseAtDecodeTime\":false,\"ptsToDecode\":0,\"seperatedPTS\":true";
	if (o->fps_milli) {
		long long num, den;
		rate_fraction(o->fps_milli, &num, &den);
		snprintf(es + strlen(es), sizeof(es) - strlen(es), ",\"videoFpsValue\":%lld,\"videoFpsScale\":%lld", num, den);
	}
	strcat(es, "}");
	snprintf(p, n,
	         "{\"args\":[{\"mediaTransportType\":\"BUFFERSTREAM\",\"option\":{\"appId\":\"%s\"%s%s%s,"
	         "\"externalStreamingInfo\":{\"contents\":{\"codec\":{\"video\":\"%s\"},\"esInfo\":%s,\"format\":\"RAW\",\"provider\":\"webosatl\"},"
	         "\"streamQualityInfo\":true,\"streamQualityInfoNonFlushable\":true,\"audioSync\":%s,\"restartStreaming\":false,"
	         "\"bufferingCtrInfo\":{\"bufferMaxLevel\":0,\"bufferMinLevel\":0,\"preBufferByte\":0,\"qBufferLevelAudio\":0,\"qBufferLevelVideo\":0,"
	         "\"srcBufferLevelAudio\":{\"minimum\":1,\"maximum\":32768},\"srcBufferLevelVideo\":{\"minimum\":1,\"maximum\":8388608}}},"
	         "\"needAudio\":false,\"queryPosition\":false,\"lowDelayMode\":%s,\"transmission\":{\"contentsType\":\"LIVE\"},"
	         "\"adaptiveStreaming\":{\"audioOnly\":false%s,\"maxWidth\":%d,\"maxHeight\":%d,\"maxFrameRate\":%d}}}]}",
	         app, win[0] ? ",\"windowId\":\"" : "", win, win[0] ? "\"" : "", codec_name(o->codec), es,
	         env_on("ATLVDEC_SF_AUDIOSYNC") ? "true" : "false", env_on("ATLVDEC_SF_LOWDELAY") ? "true" : "false",
	         o->fps_milli ? ",\"adaptiveResolution\":true" : "", mw, mh, mfps);
}

/* setHdrInfo for HDR10 / HLG (Kodi's payload; SEI units: chromaticity 1/50000 = MediaFormat's 0.00002, luminance
 * 1/10000 cd/m2, primaries in G, B, R order). Skipped without any SEI value: some TVs crash on an HDR message with
 * none, and the decoder still reads what the bitstream carries. Returns 0 when there is nothing to declare. */
static int build_hdr(char *p, size_t n, const struct avd_open *o)
{
	const char *type = o->transfer == 16 ? "HDR10" : o->transfer == 18 ? "HLG" : NULL;
	if (!type || !(o->has_mastering || o->has_light_level))
		return 0;
	char sei[512] = "";
	size_t k = 0;
#define ADD(key, v) k += snprintf(sei + k, sizeof(sei) - k, "%s\"%s\":%lld", k ? "," : "", key, (long long)(v))
	if (o->has_mastering) {
		static const int order[3] = {1, 2, 0}; /* G, B, R from R, G, B */
		char key[24];
		for (int i = 0; i < 3; i++) {
			snprintf(key, sizeof(key), "displayPrimariesX%d", i);
			ADD(key, o->primaries_xy[order[i]][0]);
			snprintf(key, sizeof(key), "displayPrimariesY%d", i);
			ADD(key, o->primaries_xy[order[i]][1]);
		}
		ADD("whitePointX", o->white_xy[0]);
		ADD("whitePointY", o->white_xy[1]);
		ADD("minDisplayMasteringLuminance", o->min_luminance);
		ADD("maxDisplayMasteringLuminance", (long long)o->max_luminance * 10000);
	}
	if (o->has_light_level) {
		ADD("maxContentLightLevel", o->max_cll);
		ADD("maxPicAverageLightLevel", o->max_fall);
	}
#undef ADD
	snprintf(p, n,
	         "{\"hdrType\":\"%s\",\"sei\":{%s},\"vui\":{\"transferCharacteristics\":%u,\"colorPrimaries\":%u,\"matrixCoeffs\":%u,"
	         "\"videoFullRangeFlag\":%s}}",
	         type, sei, o->transfer, o->primaries, o->matrix, o->full_range ? "true" : "false");
	return 1;
}

static void place(void)
{
	if (!loaded || !wid[0])
		return;
	int src[4] = {0, 0, cur.width ? (int)cur.width : 1920, cur.height ? (int)cur.height : 1080};
	SDL_webOSSetExportedWindow(wid, src, area);
}

/* ---------------------------------------------------------------------------------------------------------- API */

int sf_open(const struct avd_open *o, const char *window_id, char *msg, size_t n)
{
	if (sf_available(msg, n))
		return -1;
	if (!sf_codec_ok(o->codec)) {
		snprintf(msg, n, "codec %s not reported by this TV", codec_name(o->codec));
		return -1;
	}
	sf_unload();
	void *ob = NULL;
	if (posix_memalign(&ob, 16, OBJ_SIZE)) {
		snprintf(msg, n, "out of memory");
		return -1;
	}
	memset(ob, 0, OBJ_SIZE);
	snprintf(wid, sizeof(wid), "%s", window_id ? window_id : "");
	char payload[2048];
	build_load(payload, sizeof(payload), o);
	LOG("Load %s", payload);
	pthread_mutex_lock(&mu);
	ev_epoch = ++epoch;
	ev_load_completed = ev_unload_completed = ev_first_frame = ev_eos = 0;
	fr_n = fr_i = 0;
	ev_displayed = ev_dropped = ev_frameready = 0;
	pthread_mutex_unlock(&mu);
	A.ctor(ob, NULL); /* uid NULL: the pipeline registers on the pre-authorised media namespace (PlxNative) */
	A.notify_fg(ob);
	int64_t t = mono_ns();
	int ok = A.load(ob, payload, on_event, (void *)(uintptr_t)epoch) & 0xFF;
	LOG("Load -> %d (%.1f ms)", ok, (mono_ns() - t) / 1e6);
	if (!ok) { /* the block stays allocated: callbacks may still name it */
		pthread_mutex_lock(&mu);
		ev_epoch = 0;
		pthread_mutex_unlock(&mu);
		snprintf(msg, n, "Starfish Load refused the stream");
		return -1;
	}
	char hdr[1024];
	if (build_hdr(hdr, sizeof(hdr), o)) {
		if (A.set_hdr)
			LOG("setHdrInfo %s -> %d", hdr, A.set_hdr(ob, hdr) & 0xFF);
		else
			LOG("no setHdrInfo on this firmware: HDR metadata stays in the bitstream only");
	}
	obj = ob;
	loaded = 1;
	cur = *o;
	state_reset();
	frame_us = o->fps_milli ? 1000000000LL / o->fps_milli : 0;
	if (frame_us)
		corr_step_us = frame_us / 8;
	place();
	snprintf(msg, n, "ok starfish ahead");
	return 0;
}

int sf_is_loaded(void)
{
	return loaded;
}

int sf_same_stream(const struct avd_open *o)
{
	return loaded && o->codec == cur.codec && o->width == cur.width && o->height == cur.height;
}

void sf_reuse(void)
{
	LOG("new stream on the loaded pipeline: timeline spliced");
	sf_flush();
}

int sf_feed(const void *buf, uint32_t len, int64_t pts_us)
{
	if (!loaded)
		return -1;
	int64_t out = map_pts(pts_us);
	char json[160];
	snprintf(json, sizeof(json), "{\"bufferAddr\":\"%p\",\"bufferSize\":%u,\"pts\":%lld,\"esData\":1}", buf, len, (long long)out * 1000);
	_Alignas(8) unsigned char reply[32];
	memset(reply, 0, sizeof(reply));
	A.feed(reply, obj, json);
	const char *s = *(const char *const *)reply;
	if (s && strstr(s, "BufferFull")) {
		st.full++;
		return 1;
	}
	if (s && strstr(s, "Ok")) {
		st.feeds++;
		if (++feeds_total <= 3)
			LOG("Feed #%lu: %u bytes, pts %.3f s -> %.3f s", feeds_total, len, pts_us / 1e6, out / 1e6);
		return 0;
	}
	if (++st.errors <= 10)
		LOG("Feed reply \"%.80s\"", s ? s : "(null)");
	return -1;
}

/* Play so that the frame at timeline `out` lands on `due` (the clock is held at hold_k_us) */
static void plan_play(int64_t due, int64_t out, int64_t now)
{
	int64_t t = due - (out - hold_k_us) * 1000;
	if (t <= now + 2000000) {
		do_play(now, "due now");
		int64_t late = now - t; /* this frame comes that much after its due time: pull the timeline in */
		if (late > DEADBAND_NS) {
			corr_left_us -= late / 1000;
			corr_until_pts = INT64_MAX;
		}
	} else if (!play_at_ns) {
		play_at_ns = t;
	}
}

void sf_due(int64_t due, int64_t pts)
{
	if (!loaded)
		return;
	int64_t now = mono_ns(), out = ring_find(pts);
	if (out == INT64_MIN)
		out = pts + shift_us;
	if (!cur.fps_milli && last_due_pts != INT64_MIN && pts > last_due_pts && pts - last_due_pts < 200000) {
		int64_t d = pts - last_due_pts; /* no frame rate from the app: the spacing of released frames */
		frame_us = frame_us ? (frame_us * 7 + d) / 8 : d;
		corr_step_us = frame_us / 8 > 500 ? frame_us / 8 : 500;
	}
	last_due_pts = pts;
	rel_pts = pts, rel_out = out, rel_ns = now;
	st.dues++;
	if (!due)
		return; /* not rendered (the app was late): the pipeline shows it anyway */
	int64_t sync = getenv("ATLVDEC_SF_SYNC_MS") ? atoll(getenv("ATLVDEC_SF_SYNC_MS")) * 1000000LL : 0;
	due += sync;
	if (!playing) {
		plan_play(due, out, now);
		return;
	}
	int64_t err = clock_c_ns() + out * 1000 - due; /* > 0: the pipeline shows this frame after its due time */
	st.timed++;
	st.err_abs_sum += err < 0 ? -err : err;
	if ((err < 0 ? -err : err) > st.err_max)
		st.err_max = err < 0 ? -err : err;
	if (realign) { /* first due time after a seek: new content early -> hold until it is due; late -> catch up */
		realign = 0;
		hold_at_us = 0;
		err_n = 0;
		LOG("after the seek: new content %+.1f ms off its due time", err / 1e6);
		if (err < -DEADBAND_NS) {
			do_pause(now, "new content is early");
			plan_play(due, out, now);
		} else if (err > DEADBAND_NS) {
			corr_left_us = -err / 1000;
			corr_until_pts = INT64_MAX;
		}
		return;
	}
	if (pts < corr_until_pts)
		return; /* fed before the last correction took effect: says nothing about it */
	err_f_ns = err_n ? err_f_ns + (err - err_f_ns) / 8 : err;
	err_n++;
	if (env_on("ATLVDEC_SF_DEBUG"))
		LOG("due pts %.3f s: error %+.1f ms (filtered %+.1f)", pts / 1e6, err / 1e6, err_f_ns / 1e6);
	if (err_f_ns < -RESYNC_NS && clock_samples() >= 2) { /* far ahead of the app (it stalled without stopping): hold and restart in step */
		st.resyncs++;
		do_pause(now, "video ahead of the app");
		plan_play(due, out, now);
		err_n = 0;
		return;
	}
	if (err_n >= 8 && clock_samples() >= 2 && !corr_left_us && pts >= corr_until_pts && (err_f_ns > DEADBAND_NS || err_f_ns < -DEADBAND_NS)) {
		corr_left_us = -err_f_ns / 1000;
		corr_until_pts = INT64_MAX; /* set when the correction is fed through */
		st.corrections++;
		err_n = 0;
		/* the same direction again and again is drift: feed its rate in continuously (bounded to 0.5 %) */
		if (!drift_n)
			drift_t0_ns = now;
		drift_sum_us += corr_left_us;
		if (++drift_n >= 3 && now - drift_t0_ns > 3000000000LL) {
			drift = (double)drift_sum_us * 1000.0 / (double)(now - drift_t0_ns) + drift;
			drift = drift > 0.005 ? 0.005 : drift < -0.005 ? -0.005 : drift;
			drift_n = 0;
			drift_sum_us = 0;
			LOG("clock drift: %+.0f ppm", drift * 1e6);
		}
	}
}

/* seek: the clock keeps running (what the pipeline holds plays out; the app is rebuffering meanwhile), the new
 * content is spliced after it, and the first due time aligns it (sf_due) */
void sf_flush(void)
{
	if (!loaded)
		return;
	splice_pending = 1;
	realign = playing;
	hold_at_us = 0;
	play_at_ns = 0;
	ring_n = ring_i = 0; /* pts after a seek may repeat old ones */
	corr_left_us = 0;
	corr_until_pts = 0;
	err_n = 0;
	rel_ns = 0;
	last_due_pts = INT64_MIN;
}

void sf_end_of_stream(void)
{
	if (loaded)
		LOG("pushEOS -> %d", A.push_eos(obj) & 0xFF);
}

static void stats(int64_t now, int force)
{
	if (!force && now - st.last_ns < 10000000000LL)
		return;
	long long disp, drop, fr;
	pthread_mutex_lock(&mu);
	disp = ev_displayed, drop = ev_dropped, fr = ev_frameready;
	ev_displayed = ev_dropped = ev_frameready = 0;
	pthread_mutex_unlock(&mu);
	LOG("%.0f s: %lu feeds (%lu BufferFull, %lu errors), %lu dues, sync error avg %.1f max %.1f ms, %lu corrections, %lu holds, "
	    "%lu resyncs; sink %lld displayed %lld dropped, %lld FRAMEREADY; frame %.1f ms",
	    (now - st.last_ns) / 1e9, st.feeds, st.full, st.errors, st.dues, st.timed ? st.err_abs_sum / 1e6 / st.timed : 0.0,
	    st.err_max / 1e6, st.corrections, st.holds, st.resyncs, disp, drop, fr, frame_us / 1e3);
	st.feeds = st.full = st.errors = st.dues = st.timed = st.corrections = st.holds = st.resyncs = 0;
	st.err_abs_sum = st.err_max = 0;
	st.last_ns = now;
}

void sf_tick(void)
{
	if (!loaded)
		return;
	int64_t now = mono_ns();
	if (play_at_ns && now >= play_at_ns)
		do_play(now, "scheduled");
	if (playing && hold_at_us && pos_out_us(now) >= hold_at_us) {
		hold_at_us = 0;
		realign = 0; /* the first due time after the seek starts the clock (plan_play) */
		do_pause(now, "seek: the frames from before it are shown");
	}
	pthread_mutex_lock(&mu);
	int lc = ev_load_completed;
	pthread_mutex_unlock(&mu);
	if (lc && playing && !replayed) { /* a Play before loadCompleted is queued; issued once more after it (kit) */
		replayed = 1;
		LOG("Play after loadCompleted -> %d", A.play(obj) & 0xFF);
	}
	if (playing && rel_ns && !realign) { /* the app stopped releasing: hold before the pipeline runs past its last frame */
		int64_t fu = frame_us ? frame_us : 33367, pos = pos_out_us(now);
		int64_t quiet = 5 * fu * 1000 / 2 > 100000000LL ? 5 * fu * 1000 / 2 : 100000000LL;
		if (pos > rel_out + fu && now - rel_ns > quiet)
			do_pause(now, "the app stopped releasing frames");
	}
	stats(now, 0);
}

int sf_wait_ms(void)
{
	if (!loaded)
		return -1;
	if (play_at_ns) {
		int64_t w = (play_at_ns - mono_ns()) / 1000000;
		return w < 0 ? 0 : w > 20 ? 20 : (int)w;
	}
	return playing ? 20 : 200;
}

void sf_area(int x, int y, int w, int h)
{
	area[0] = x, area[1] = y, area[2] = w, area[3] = h;
	place();
}

void sf_unload(void)
{
	if (!loaded)
		return;
	stats(mono_ns(), 1);
	loaded = 0;
	playing = 0;
	play_at_ns = 0;
	int64_t t = mono_ns();
	int rc = A.unload(obj) & 0xFF;
	/* the destructor gives the decoders back (a Load after an Unload without it waited forever on webOS 6.5.3),
	 * and runs only after UnloadCompleted */
	struct timespec dl;
	clock_gettime(CLOCK_REALTIME, &dl);
	dl.tv_sec += 3;
	pthread_mutex_lock(&mu);
	while (!ev_unload_completed)
		if (pthread_cond_timedwait(&cv, &mu, &dl) == ETIMEDOUT)
			break;
	int done = ev_unload_completed;
	ev_epoch = 0; /* later callbacks are dropped */
	pthread_mutex_unlock(&mu);
	if (done)
		A.dtor(obj);
	LOG("Unload -> %d, %s (%.1f ms)", rc, done ? "destroyed" : "no UnloadCompleted: object kept", (mono_ns() - t) / 1e6);
	obj = NULL; /* the block itself is never freed */
}
