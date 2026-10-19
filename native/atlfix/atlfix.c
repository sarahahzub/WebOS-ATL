/* atlfix.so: LD_PRELOAD helper for the ATL AppImage on LG webOS (aarch64).  Loaded by atld for every app.
 *
 * 1. PulseAudio (pafix): LG's webOS PulseAudio 15 server expects ONE EXTRA trailing boolean in
 *    PA_COMMAND_CREATE_PLAYBACK_STREAM. LG's own libpulse sends it ("... 42 00 30" = proplist end, u32 0,
 *    bool false); upstream libpulse 17 in the AppImage does not, so the server's tagstruct parser fails and it
 *    drops the client ("PulseAudio: Unable to create stream: Connection terminated" -> ALSA EIO -> AudioTrack
 *    writes fail -> ExoPlayer's audio clock never advances -> video frozen on the first frame).
 *    We interpose pa_pstream_send_tagstruct_with_creds() (libpulse -> libpulsecommon via PLT) and append it.
 *    ATLFIX_NOPA=1 disables, ATLFIX_LOG=1 logs.
 * 2. stdout line buffering: ATL logs a lot with printf(); stdout is a file (app.log), so it was block buffered
 *    and the last few KB before a native crash were lost. ATLFIX_STDOUT=full keeps block buffering.
 * 3. Native crash report: SIGSEGV/SIGBUS/SIGILL/SIGFPE/SIGABRT handler that prints thread, fault address,
 *    pc/lr with symbol (dladdr) and a frame-pointer backtrace to stderr, then re-raises with the default action.
 *    ART's libsigchain keeps it as the "user" handler and only calls it for faults ART itself doesn't handle
 *    (implicit null checks / stack overflow stay with ART).  ATLFIX_NOCRASH=1 disables.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <link.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <ucontext.h>
#include <unistd.h>

#define EXPORT __attribute__((visibility("default")))

/* ---------------------------------------------------------------- 1. PulseAudio */
typedef struct pa_pstream pa_pstream;
typedef struct pa_tagstruct pa_tagstruct;
#define PA_COMMAND_CREATE_PLAYBACK_STREAM 3

static void (*real_send)(pa_pstream *, pa_tagstruct *, const void *);
static const uint8_t *(*ts_data)(pa_tagstruct *, size_t *);
static void (*ts_put_boolean)(pa_tagstruct *, int);
static int pa_off = -1, logit;

/* libpulse is usually not in the global scope: ALSA dlopen()s its pulse plugin (and so libpulse/libpulsecommon)
 * RTLD_LOCAL, so dlsym(RTLD_NEXT) finds nothing. Locate the loaded libpulsecommon-*.so by name instead. */
static int find_pulsecommon(struct dl_phdr_info *info, size_t size, void *out)
{
	const char *n = info->dlpi_name ? strrchr(info->dlpi_name, '/') : NULL;
	n = n ? n + 1 : info->dlpi_name;
	if (n && !strncmp(n, "libpulsecommon", 14)) {
		*(void **)out = dlopen(info->dlpi_name, RTLD_NOW | RTLD_NOLOAD);
		return *(void **)out != NULL;
	}
	return 0;
}

static void *pa_sym(void *h, const char *name)
{
	void *s = h ? dlsym(h, name) : NULL;
	return s ? s : dlsym(RTLD_NEXT, name);
}

EXPORT void pa_pstream_send_tagstruct_with_creds(pa_pstream *p, pa_tagstruct *t, const void *creds)
{
	if (!real_send) {
		void *h = NULL;
		dl_iterate_phdr(find_pulsecommon, &h);
		real_send = pa_sym(h, "pa_pstream_send_tagstruct_with_creds");
		ts_data = pa_sym(h, "pa_tagstruct_data");
		ts_put_boolean = pa_sym(h, "pa_tagstruct_put_boolean");
		pa_off = getenv("ATLFIX_NOPA") != NULL || !ts_data || !ts_put_boolean;
		if (logit || !real_send)
			fprintf(stderr, "atlfix: libpulsecommon %p send %p data %p putb %p\n", h, (void *)real_send, (void *)ts_data, (void *)ts_put_boolean);
		if (!real_send)
			abort(); /* cannot happen: we were called from libpulse, so libpulsecommon is loaded */
	}
	if (!pa_off) {
		size_t n = 0;
		const uint8_t *d = ts_data(t, &n);
		/* every command starts with PA_TAG_U32 'L' + big-endian command */
		if (n >= 5 && d[0] == 'L' && ((uint32_t)d[1] << 24 | (uint32_t)d[2] << 16 | (uint32_t)d[3] << 8 | d[4]) == PA_COMMAND_CREATE_PLAYBACK_STREAM) {
			ts_put_boolean(t, 0);
			if (logit)
				fprintf(stderr, "atlfix: CREATE_PLAYBACK_STREAM +bool (%zu bytes)\n", n);
		}
	}
	real_send(p, t, creds);
}

/* ---------------------------------------------------------------- 3. crash report */
static int safe_read(uintptr_t addr, void *out, size_t n)
{
	struct iovec l = { out, n }, r = { (void *)addr, n };
	return syscall(SYS_process_vm_readv, getpid(), &l, 1, &r, 1, 0) == (ssize_t)n;
}

static void put(const char *s) { (void)!write(2, s, strlen(s)); }

static void sym(const char *tag, uintptr_t a)
{
	char b[512];
	Dl_info i;
	if (a && dladdr((void *)a, &i) && i.dli_fname) {
		const char *f = strrchr(i.dli_fname, '/');
		snprintf(b, sizeof b, "atlfix:   %s %#lx %s+%#lx (%s+%#lx)\n", tag, (unsigned long)a, f ? f + 1 : i.dli_fname,
		         (unsigned long)(a - (uintptr_t)i.dli_fbase), i.dli_sname ? i.dli_sname : "?",
		         i.dli_saddr ? (unsigned long)(a - (uintptr_t)i.dli_saddr) : 0UL);
	} else {
		snprintf(b, sizeof b, "atlfix:   %s %#lx ?\n", tag, (unsigned long)a);
	}
	put(b);
}

static void crash(int sig, siginfo_t *si, void *ucv)
{
	char b[256], name[17] = { 0 };
	prctl(PR_GET_NAME, name);
	snprintf(b, sizeof b, "\natlfix: FATAL signal %d (%s) code %d addr %p tid %ld \"%s\"\n", sig, strsignal(sig), si->si_code,
	         si->si_addr, (long)syscall(SYS_gettid), name);
	put(b);
#ifdef __aarch64__
	ucontext_t *uc = ucv;
	uintptr_t pc = uc->uc_mcontext.pc, lr = uc->uc_mcontext.regs[30], fp = uc->uc_mcontext.regs[29];
	sym("pc", pc);
	sym("lr", lr);
	for (int i = 0; i < 40 && fp; i++) {
		uintptr_t f[2];
		if ((fp & 7) || !safe_read(fp, f, sizeof f))
			break;
		snprintf(b, sizeof b, "#%02d", i);
		sym(b, f[1]);
		if (f[0] <= fp)
			break;
		fp = f[0];
	}
	for (int r = 0; r < 31; r += 4) {
		snprintf(b, sizeof b, "atlfix:   x%-2d %016llx %016llx %016llx %016llx\n", r, uc->uc_mcontext.regs[r],
		         r + 1 < 31 ? uc->uc_mcontext.regs[r + 1] : 0, r + 2 < 31 ? uc->uc_mcontext.regs[r + 2] : 0,
		         r + 3 < 31 ? uc->uc_mcontext.regs[r + 3] : 0);
		put(b);
	}
#endif
	/* default action, bypassing libsigchain's sigaction interposition */
	struct { void *h; unsigned long fl; void *res; uint64_t mask; } k = { SIG_DFL, 0, 0, 0 };
	syscall(SYS_rt_sigaction, sig, &k, NULL, 8);
	syscall(SYS_tgkill, getpid(), syscall(SYS_gettid), sig);
}

__attribute__((constructor)) static void init(void)
{
	logit = getenv("ATLFIX_LOG") != NULL;
	const char *so = getenv("ATLFIX_STDOUT");
	if (!so || strcmp(so, "full"))
		setvbuf(stdout, NULL, _IOLBF, 0);
	/* only arm the crash report in the real app process (AppRun's helpers are short-lived shells) */
	if (!getenv("ATLFIX_NOCRASH")) {
		struct sigaction sa;
		memset(&sa, 0, sizeof sa);
		sa.sa_sigaction = crash;
		sa.sa_flags = SA_SIGINFO | SA_ONSTACK;
		static const int sigs[] = { SIGSEGV, SIGBUS, SIGILL, SIGFPE, SIGABRT };
		for (unsigned i = 0; i < sizeof sigs / sizeof *sigs; i++)
			sigaction(sigs[i], &sa, NULL);
		if (getenv("ATLFIX_SELFTEST"))
			*(volatile int *)(uintptr_t)atoi(getenv("ATLFIX_SELFTEST")) = 1;
	}
}
