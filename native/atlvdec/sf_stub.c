/* Bench stand-in for libplayerAPIs (StarfishMediaAPIs), linked into atlvdec-stub with -rdynamic (sf.c -DSF_STUB).
 * Models what sf.c relies on: a pipeline clock that starts at 0 on the first Play, stops on Pause and runs on Play;
 * frames presented when the clock reaches their pts (in pts order), late ones (> 20 ms behind) dropped; Feed
 * replies "Ok" / "BufferFull" (more than SFSTUB_CAP frames buffered, default 90); FRAMEREADY (type 0) with the
 * presented pts, sent as a frame is presented, at most every 200 ms, and the sink's displayed/dropped counters (49/48); loadCompleted; UnloadCompleted (23).
 * SFSTUB_LOG=<file>: one line per frame, "P|D <content pts us> <presented CLOCK_MONOTONIC ns>" (D = dropped); the
 * content pts is read from bytes 5..12 of the AU (sftest.py writes it there; it is not in the Feed payload).
 * SFSTUB_RATE=<r>: the pipeline clock runs r times as fast as CLOCK_MONOTONIC (default 1). */
#define _GNU_SOURCE
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

typedef void (*cb_t)(int type, long long num, const char *str, void *ctx);

static pthread_mutex_t m = PTHREAD_MUTEX_INITIALIZER;
static pthread_t th;
static int running, playing, loaded_once;
static cb_t cb;
static void *cb_ctx;
static int64_t q[4096], qc[4096]; /* fed pts (ns, sorted) and the content pts the test put in the AU (us) */
static int qn, cap = 90;
static int64_t base_ns, start_ns; /* pipeline clock: base + (now - start) * rate while playing */
static double rate = 1.0;
static FILE *lf;
static long long n_disp, n_drop;

#define S(...) fprintf(stderr, "  [sf-stub] " __VA_ARGS__)

static int64_t now_ns(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec * 1000000000LL + t.tv_nsec;
}
static int64_t clock_at(int64_t now) { return playing ? base_ns + (int64_t)((now - start_ns) * rate) : base_ns; }

static void *presenter(void *arg)
{
	(void)arg;
	int64_t last_fr = now_ns(), last_pts = 0;
	int sent_loaded = 0;
	while (1) {
		usleep(1000);
		int64_t now = now_ns();
		pthread_mutex_lock(&m);
		if (!running) {
			pthread_mutex_unlock(&m);
			break;
		}
		cb_t c = cb;
		void *x = cb_ctx;
		int64_t clk = clock_at(now);
		int fr = 0;
		while (playing && qn && q[0] <= clk) {
			int64_t pts = q[0], cpts = qc[0];
			memmove(q, q + 1, (qn - 1) * sizeof(*q));
			memmove(qc, qc + 1, (qn - 1) * sizeof(*qc));
			qn--;
			/* the exact instant the clock passed this pts */
			int64_t at = start_ns + (int64_t)((pts - base_ns) / rate);
			int late = clk - pts > 20000000LL && at < now - 20000000LL;
			if (late)
				n_drop++;
			else
				n_disp++;
			if (lf)
				fprintf(lf, "%c %lld %lld\n", late ? 'D' : 'P', (long long)cpts, (long long)(late ? now : at));
			last_pts = pts;
			if (!late && now - last_fr >= 200000000LL)
				fr = 1; /* FRAMEREADY for a frame as it is presented, at most every 200 ms */
		}
		long long d = n_disp, dr = n_drop;
		if (fr) {
			last_fr = now;
			n_disp = n_drop = 0;
		}
		pthread_mutex_unlock(&m);
		if (!sent_loaded && c) {
			sent_loaded = 1;
			usleep(50000);
			c(1, 0, "{\"loadCompleted\":true}", x);
		}
		if (fr && c) {
			usleep(rand() % 2000); /* callbacks arrive a little after the frame */
			c(0, last_pts, NULL, x);
			c(49, d, NULL, x);
			c(48, dr, NULL, x);
		}
	}
	return NULL;
}

void _ZN17StarfishMediaAPIsC1EPKc(void *self, const char *uid) { (void)self, (void)uid; }
void _ZN17StarfishMediaAPIsD1Ev(void *self) { (void)self; S("destructor\n"); }
void _ZN17StarfishMediaAPIs16notifyForegroundEv(void *self) { (void)self; }

int _ZN17StarfishMediaAPIs4LoadEPKcPFvixS1_PvES2_(void *self, const char *payload, cb_t c, void *ctx)
{
	(void)self;
	S("Load %.400s\n", payload);
	const char *r = getenv("SFSTUB_RATE"), *l = getenv("SFSTUB_LOG"), *cp = getenv("SFSTUB_CAP");
	pthread_mutex_lock(&m);
	rate = r ? atof(r) : 1.0;
	cap = cp ? atoi(cp) : 90;
	if (l && !lf)
		lf = fopen(l, "w");
	if (lf)
		setvbuf(lf, NULL, _IOLBF, 0);
	cb = c, cb_ctx = ctx;
	qn = 0, base_ns = 0, playing = 0, running = 1;
	pthread_mutex_unlock(&m);
	loaded_once = 1;
	pthread_create(&th, NULL, presenter, NULL);
	return 1;
}

void _ZN17StarfishMediaAPIs4FeedB5cxx11EPKc(void *sret, void *self, const char *payload)
{
	(void)self;
	struct {
		char *p;
		size_t len;
		char buf[16];
	} *r = sret;
	long long pts = 0, cpts = -1;
	const char *k = strstr(payload, "\"pts\":");
	if (k)
		pts = atoll(k + 6);
	void *addr = NULL;
	unsigned size = 0;
	if (sscanf(payload, "{\"bufferAddr\":\"%p\",\"bufferSize\":%u", &addr, &size) == 2 && addr && size >= 13)
		memcpy(&cpts, (const char *)addr + 5, 8);
	pthread_mutex_lock(&m);
	const char *ans = "Ok";
	if (qn >= cap || qn >= 4096) {
		ans = "BufferFull";
	} else {
		int i = qn;
		while (i > 0 && q[i - 1] > pts)
			i--;
		memmove(q + i + 1, q + i, (qn - i) * sizeof(*q));
		memmove(qc + i + 1, qc + i, (qn - i) * sizeof(*qc));
		q[i] = pts;
		qc[i] = cpts;
		qn++;
	}
	pthread_mutex_unlock(&m);
	r->p = r->buf;
	r->len = strlen(ans);
	memcpy(r->buf, ans, r->len + 1);
}

int _ZN17StarfishMediaAPIs4PlayEv(void *self)
{
	(void)self;
	pthread_mutex_lock(&m);
	if (!playing) {
		start_ns = now_ns();
		playing = 1;
	}
	pthread_mutex_unlock(&m);
	return 1;
}

int _ZN17StarfishMediaAPIs5PauseEv(void *self)
{
	(void)self;
	pthread_mutex_lock(&m);
	if (playing) {
		base_ns = clock_at(now_ns());
		playing = 0;
	}
	pthread_mutex_unlock(&m);
	return 1;
}

int _ZN17StarfishMediaAPIs6UnloadEv(void *self)
{
	(void)self;
	pthread_mutex_lock(&m);
	running = 0;
	cb_t c = cb;
	void *x = cb_ctx;
	pthread_mutex_unlock(&m);
	pthread_join(th, NULL);
	if (lf)
		fflush(lf);
	if (c)
		c(23, 0, "{\"unloadCompleted\":true}", x);
	return 1;
}

int _ZN17StarfishMediaAPIs7pushEOSEv(void *self) { (void)self; S("pushEOS\n"); return 1; }
int _ZN17StarfishMediaAPIs10setHdrInfoEPKc(void *self, const char *p) { (void)self; S("setHdrInfo %s\n", p); return 1; }

/* getMaxVideoResolution(std::string codec, int *w, int *h, int *fps): a 4K decoder for every codec */
int _ZN3smp4util21getMaxVideoResolutionENSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEEPiS7_S7_(void *str, int32_t *w, int32_t *h, int32_t *f)
{
	(void)str;
	*w = 3840, *h = 2160, *f = 60;
	return 1;
}
