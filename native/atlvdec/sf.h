/* Starfish backend of atlvdec (sf.c): LG's StarfishMediaAPIs (libplayerAPIs.so.1), the buffer-feed media pipeline
 * LG's own apps use. Used in ahead mode (proto.h): ATL feeds AUs with their content pts as soon as the app queues
 * them, the pipeline presents them by pts on its own clock (declared frame rate, no low-delay mode), and sf.c keeps
 * that clock on the app's due times (AVD_DUE). Single-threaded API (atlvdec's main loop); the library's event
 * callback runs on its own thread and only touches sf.c's locked state. */
#pragma once
#include <stddef.h>
#include <stdint.h>

#include "proto.h"

int sf_available(char *why, size_t n);           /* libplayerAPIs + entry points (cached); 0 = usable */
const char *sf_codecs(void);                     /* "h264 hevc vp9 av1" (what this TV's decoder reports) */
int sf_codec_ok(uint32_t codec);                 /* the codec can go to Starfish on this TV */
/* load for this stream; window_id = the exported ATL window (xdgshim). 0 ok, else msg says why */
int sf_open(const struct avd_open *o, const char *window_id, char *msg, size_t n);
int sf_is_loaded(void);
int sf_same_stream(const struct avd_open *o);    /* loaded with this codec + size (linger reuse) */
void sf_reuse(void);                             /* a new client takes over the loaded pipeline: splice + hold */
int sf_feed(const void *buf, uint32_t len, int64_t pts_us); /* 0 accepted, 1 full (retry later), -1 error */
void sf_due(int64_t due_ns, int64_t pts_us);     /* the app released frame pts_us for due_ns (0: not rendered) */
void sf_flush(void);                             /* seek: hold the clock, splice the timeline */
void sf_end_of_stream(void);
void sf_unload(void);
void sf_area(int x, int y, int w, int h);        /* 1920x1080 plane coordinates */
void sf_tick(void);                              /* main loop: scheduled Play, pause watchdog, stats */
int sf_wait_ms(void);                            /* poll timeout sf_tick needs (-1: none) */
