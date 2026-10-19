/* gbfix.so: LD_PRELOAD for the TV browser's (armhf) Chromium on LG webOS: the PulseAudio part of
 * tvlauncher/atl/atlfix/atlfix.c (see its README "1. Audio"), built for 32-bit.
 *
 * LG's webOS PulseAudio 15 server expects one extra trailing boolean in PA_COMMAND_CREATE_PLAYBACK_STREAM. LG's own
 * libpulse sends it; the bundled upstream libpulse 17 does not, so the server drops the client
 * ("Unable to create stream: Connection terminated"). We interpose pa_pstream_send_tagstruct_with_creds() (libpulse ->
 * libpulsecommon via PLT) and append `false`. Chromium dlopen()s libpulse RTLD_LOCAL, so the real symbols are looked up
 * in the loaded libpulsecommon-*.so via dl_iterate_phdr.
 * GBFIX_NOPA=1 disables, GBFIX_LOG=1 logs each patched packet.
 * Build: arm-linux-gnueabihf-gcc -O2 -Wall -shared -fPIC -fvisibility=hidden -o gbfix.so gbfix.c -ldl
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <link.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define EXPORT __attribute__((visibility("default")))
typedef struct pa_pstream pa_pstream;
typedef struct pa_tagstruct pa_tagstruct;
#define PA_COMMAND_CREATE_PLAYBACK_STREAM 3

static void (*real_send)(pa_pstream *, pa_tagstruct *, const void *);
static const uint8_t *(*ts_data)(pa_tagstruct *, size_t *);
static void (*ts_put_boolean)(pa_tagstruct *, int);
static int pa_off = -1, logit;

static int find_pulsecommon(struct dl_phdr_info *info, size_t size, void *out)
{
	const char *n = info->dlpi_name ? strrchr(info->dlpi_name, '/') : NULL;
	(void)size;
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
		logit = getenv("GBFIX_LOG") != NULL;
		pa_off = getenv("GBFIX_NOPA") != NULL || !ts_data || !ts_put_boolean;
		if (logit || !real_send)
			fprintf(stderr, "gbfix: libpulsecommon %p send %p data %p putb %p\n", h, (void *)real_send, (void *)ts_data, (void *)ts_put_boolean);
		if (!real_send)
			abort();
	}
	if (!pa_off) {
		size_t n = 0;
		const uint8_t *d = ts_data(t, &n);
		if (n >= 5 && d[0] == 'L' && ((uint32_t)d[1] << 24 | (uint32_t)d[2] << 16 | (uint32_t)d[3] << 8 | d[4]) == PA_COMMAND_CREATE_PLAYBACK_STREAM) {
			ts_put_boolean(t, 0);
			if (logit)
				fprintf(stderr, "gbfix: CREATE_PLAYBACK_STREAM +bool (%zu bytes)\n", n);
		}
	}
	real_send(p, t, creds);
}
