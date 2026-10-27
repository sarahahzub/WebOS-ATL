/* atlvdec wire protocol (ATL 64-bit <-> atlvdec 32-bit), unix stream socket, little-endian.
 * Same layout on armhf and aarch64 (explicit sizes, pts_us aligned at offset 8).
 *
 * Two feeding modes, chosen by the helper's OPEN reply:
 *   NDL / release-time (protocol 1, every helper): ATL sends an AU when the app releases the frame that needs it
 *     (MediaCodec.releaseOutputBuffer), preceded by AVD_DUE; the helper shows it at once (NDL low-delay mode).
 *   Starfish / ahead (protocol 2, reply message contains " ahead"): ATL sends every AU as soon as the app queues it
 *     (FEED with its content pts) and only AVD_DUE (due time + the frame's pts in the header) on release; the
 *     helper's decoder schedules frames by pts on its own clock and the helper keeps that clock on the app's due
 *     times. ATL asks for it with avd_open.flags AVD_OPEN_AHEAD; an old helper ignores the extra OPEN bytes and
 *     replies "ok" (release-time mode), an old ATL sends a 12-byte OPEN and gets release-time mode. */
#pragma once
#include <stdint.h>

struct avd_hdr {
	uint32_t type;
	uint32_t len; /* payload bytes following the header */
	int64_t pts_us;
};

enum {
	AVD_OPEN = 1,   /* ATL->helper: struct avd_open (12 bytes in protocol 1, sizeof(struct avd_open) in 2); reply AVD_RESULT */
	AVD_FEED = 2,   /* ATL->helper: one access unit (Annex-B for H.264/HEVC, one frame for VP9/AV1); hdr.pts_us = its pts */
	AVD_AREA = 3,   /* ATL->helper: struct avd_area (1920x1080 plane coordinates) */
	AVD_FLUSH = 4,  /* ATL->helper: seek (NDL: unload + reload; Starfish: the timeline is spliced, nothing reloads) */
	AVD_CLOSE = 5,  /* ATL->helper: stream ends */
	AVD_DUE = 6,    /* ATL->helper: int64 CLOCK_MONOTONIC ns at which the released frame should be on screen (0 = not
	                   rendered / no time); MediaCodec.releaseOutputBuffer(index, releaseTimeNs), ExoPlayer releases up to
	                   50 ms early. hdr.pts_us = that frame's pts in ahead mode (0 in release-time mode) */
	AVD_HELLO = 8,  /* ATL->helper (protocol 2): capability query, reply AVD_RESULT msg "atlvdec 2 <backend> <codecs...>".
	                   A protocol-1 helper does not reply: ATL waits at most ~300 ms, and only sends it for VP9 */
	AVD_RESULT = 16, /* helper->ATL: struct avd_result */
	AVD_EVENT = 17,  /* helper->ATL: int32 kind (async, informational) */
};

enum { AVD_CODEC_H264 = 1, AVD_CODEC_HEVC = 2, AVD_CODEC_VP9 = 3, AVD_CODEC_AV1 = 4 };
enum { AVD_OPEN_AHEAD = 1 }; /* avd_open.flags: ATL can feed ahead (sends AUs at queue time, pts on AVD_DUE) */

/* Protocol 1 stops after `height` (12 bytes). The rest is protocol 2; colour fields are ITU-T H.273 code points
 * (0 = unknown); HDR static metadata (SMPTE ST 2086 + CTA-861.3) as MediaFormat KEY_HDR_STATIC_INFO gives it. */
struct avd_open {
	uint32_t codec;
	uint32_t width;
	uint32_t height;
	/* protocol 2 */
	uint32_t flags;
	uint32_t fps_milli;  /* frame rate x 1000 (23976 = 23.976 fps), 0 = unknown */
	uint8_t transfer;    /* 1 BT.709, 16 PQ (HDR10), 18 HLG */
	uint8_t primaries;   /* 1 BT.709, 9 BT.2020 */
	uint8_t matrix;      /* 1 BT.709, 9 BT.2020 non-constant */
	uint8_t full_range;  /* 1 full, 0 limited / unknown */
	uint8_t has_mastering, has_light_level, pad[2];
	uint16_t primaries_xy[3][2]; /* R, G, B chromaticity in 0.00002 units */
	uint16_t white_xy[2];
	uint32_t max_luminance;      /* cd/m2 */
	uint32_t min_luminance;      /* 0.0001 cd/m2 */
	uint16_t max_cll, max_fall;  /* cd/m2 */
};

#define AVD_OPEN_V1_LEN 12u

struct avd_area {
	int32_t x, y, w, h;
};

struct avd_result {
	int32_t status; /* 0 = ok */
	char msg[60];
};

#define AVD_MAX_PAYLOAD (16u << 20)

_Static_assert(sizeof(struct avd_hdr) == 16, "avd_hdr layout");
_Static_assert(sizeof(struct avd_open) == 56, "avd_open layout");
_Static_assert(sizeof(struct avd_result) == 64, "avd_result layout");
