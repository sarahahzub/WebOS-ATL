/* atlvdec wire protocol (ATL 64-bit <-> atlvdec 32-bit), unix stream socket, little-endian.
 * Same layout on armhf and aarch64 (explicit sizes, pts_us aligned at offset 8). */
#pragma once
#include <stdint.h>

struct avd_hdr {
	uint32_t type;
	uint32_t len; /* payload bytes following the header */
	int64_t pts_us;
};

enum {
	AVD_OPEN = 1,   /* ATL->helper: struct avd_open; helper replies AVD_RESULT */
	AVD_FEED = 2,   /* ATL->helper: one Annex-B access unit */
	AVD_AREA = 3,   /* ATL->helper: struct avd_area (1920x1080 plane coordinates) */
	AVD_FLUSH = 4,  /* ATL->helper: seek: unload + reload */
	AVD_CLOSE = 5,  /* ATL->helper: stream ends */
	AVD_DUE = 6,    /* ATL->helper: int64 CLOCK_MONOTONIC ns at which the next FEEDs should be on screen (0 = now);
	                   MediaCodec.releaseOutputBuffer(index, releaseTimeNs) - ExoPlayer releases up to 50 ms early */
	AVD_RESULT = 16, /* helper->ATL: struct avd_result */
	AVD_EVENT = 17,  /* helper->ATL: int32 kind (async, informational) */
};

enum { AVD_CODEC_H264 = 1, AVD_CODEC_HEVC = 2 };

struct avd_open {
	uint32_t codec;
	uint32_t width;
	uint32_t height;
};

struct avd_area {
	int32_t x, y, w, h;
};

struct avd_result {
	int32_t status; /* 0 = ok */
	char msg[60];
};

#define AVD_MAX_PAYLOAD (16u << 20)
