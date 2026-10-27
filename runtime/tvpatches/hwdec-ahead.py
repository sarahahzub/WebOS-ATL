#!/usr/bin/env python3
"""On top of hwdec.py + hwdec-surface.py + hwdec-pace.py: protocol 2 of the atlvdec helper (native/atlvdec/proto.h).

 - OPEN carries the stream's frame rate (MediaFormat "frame-rate"), colour (color-standard / color-transfer /
   color-range as ITU-T H.273 code points) and HDR static metadata (hdr-static-info), and asks for ahead mode.
 - If the helper replies "... ahead" (atlvdec's Starfish backend: LG's buffer-feed media pipeline, frames presented
   by pts at the declared frame rate), every AU goes to the helper as soon as the app queues it (FEED with its pts),
   and releaseOutputBuffer only sends AVD_DUE with the release time and that frame's pts; the helper keeps the
   pipeline's clock on those due times (A/V sync stays with the app's audio clock). Up to 28 frames are in flight
   (queueInputBuffer reports full beyond that).
 - Otherwise (old helper, NDL backend, ATL_HWDEC_AHEAD=0) everything stays as before: AUs at release time.
 - VP9 goes to the helper too when it says its decoder takes VP9 (HELLO, a protocol-2 capability query; an old
   helper does not answer, ATL waits 300 ms and uses FFmpeg); VP9 frames are sent as they are (no Annex-B, no csd).
 - The output "token" buffer carries the frame's pts after the token (16 bytes written, size stays 8).
usage: hwdec-ahead.py <atl src root>"""
import os, sys
R = sys.argv[1]
A = os.path.join(R, "src/api-impl/android")
J = os.path.join(R, "src/api-impl-jni")
MC = "media/android_media_MediaCodec.c"


def edit(rel, old, new, base=J, count=1):
    p = os.path.join(base, rel)
    s = open(p).read()
    if new in s:
        return
    assert s.count(old) == count, f"{rel}: anchor found {s.count(old)}x (want {count}): {old[:70]!r}"
    open(p, "w").write(s.replace(old, new))


# ------------------------------------------------------------------ Java
edit("media/MediaFormat.java", "\tpublic int getInteger(String name) {",
     "\tpublic float getFloat(String name) {\n\t\treturn ((Number)map.get(name)).floatValue();\n\t}\n\n"
     "\tpublic int getInteger(String name) {", base=A)
edit("media/MediaCodec.java",
     """			native_set_video_size(native_codec, format.containsKey("width") ? format.getInteger("width") : 0,
			                      format.containsKey("height") ? format.getInteger("height") : 0);
""", """			native_set_video_size(native_codec, format.containsKey("width") ? format.getInteger("width") : 0,
			                      format.containsKey("height") ? format.getInteger("height") : 0);
			native_set_video_info(native_codec, Math.round(number(format, "frame-rate") * 1000), (int)number(format, "color-standard"),
			                      (int)number(format, "color-transfer"), (int)number(format, "color-range"),
			                      format.containsKey("hdr-static-info") ? format.getByteBuffer("hdr-static-info") : null);
""", base=A)
edit("media/MediaCodec.java", "\tpublic void start() {",
     "\t/* a numeric MediaFormat entry (apps set frame-rate as int or float), 0 when absent or not a number */\n"
     "\tprivate static float number(MediaFormat format, String key) {\n"
     "\t\ttry {\n\t\t\treturn format.containsKey(key) ? format.getFloat(key) : 0;\n"
     "\t\t} catch (ClassCastException | NullPointerException e) {\n\t\t\treturn 0;\n\t\t}\n\t}\n\n"
     "\tpublic void start() {", base=A)
edit("media/MediaCodec.java",
     "\tprivate native void native_set_video_size(long codec, int width, int height);\n",
     "\tprivate native void native_set_video_size(long codec, int width, int height);\n"
     "\tprivate native void native_set_video_info(long codec, int fpsMilli, int colorStandard, int colorTransfer, int colorRange, ByteBuffer hdrStaticInfo);\n",
     base=A)

# ------------------------------------------------------------------ native
edit(MC, "\tstruct hwdec *hw; /* ATL_HWDEC: video goes to the atlvdec helper */\n\tint hw_width, hw_height;\n",
     "\tstruct hwdec *hw; /* ATL_HWDEC: video goes to the atlvdec helper */\n\tint hw_width, hw_height;\n"
     "\tint hw_fps_milli, hw_standard, hw_transfer, hw_range; /* MediaFormat, for the helper's OPEN */\n"
     "\tuint8_t hw_hdr[25];\n\tint hw_hdr_len;\n")
edit(MC, "enum { AVD_OPEN = 1, AVD_FEED = 2, AVD_AREA = 3, AVD_FLUSH = 4, AVD_CLOSE = 5, AVD_DUE = 6, AVD_RESULT = 16 };",
     "enum { AVD_OPEN = 1, AVD_FEED = 2, AVD_AREA = 3, AVD_FLUSH = 4, AVD_CLOSE = 5, AVD_DUE = 6, AVD_HELLO = 8, AVD_RESULT = 16 };\n\n"
     "/* protocol 2 OPEN (native/atlvdec/proto.h struct avd_open, 56 bytes); a protocol-1 helper reads the first 12 */\n"
     "struct avd_open2 {\n\tuint32_t codec, width, height, flags, fps_milli;\n"
     "\tuint8_t transfer, primaries, matrix, full_range, has_mastering, has_light_level, pad[2];\n"
     "\tuint16_t primaries_xy[3][2], white_xy[2];\n\tuint32_t max_luminance, min_luminance;\n\tuint16_t max_cll, max_fall;\n};\n"
     "_Static_assert(sizeof(struct avd_open2) == 56, \"avd_open layout\");\n")
edit(MC, "\tgboolean hevc;\n\tSurfaceViewWidget *widget;", "\tgboolean hevc;\n\tgboolean vp9;   /* frames as they are: no Annex-B parsing, no csd */\n"
     "\tgboolean ahead; /* protocol-2 helper: AUs at queue time, AVD_DUE with the frame's pts at release */\n\tSurfaceViewWidget *widget;")

# helper reply with a timeout; MediaFormat colour -> H.273
edit(MC, "/* configure: returns TRUE if the hardware path is used */", """/* the helper's AVD_RESULT within timeout_ms: status (msg filled), or -1000 without a reply */
static int hw_result(int fd, int timeout_ms, char *msg, size_t n)
{
	struct {
		struct avd_hdr h;
		int32_t status;
		char msg[60];
	} r = {0};
	size_t got = 0;
	gint64 end = g_get_monotonic_time() + timeout_ms * 1000LL;
	while (got < sizeof(r)) {
		int left = (int)((end - g_get_monotonic_time()) / 1000);
		struct pollfd p = {fd, POLLIN, 0};
		if (left <= 0 || poll(&p, 1, left) <= 0)
			break;
		ssize_t k = read(fd, (char *)&r + got, sizeof(r) - got);
		if (k <= 0)
			break;
		got += k;
	}
	r.msg[sizeof(r.msg) - 1] = 0;
	snprintf(msg, n, "%s", got < sizeof(r) ? "no reply" : r.msg);
	return got < sizeof(r) || r.h.type != AVD_RESULT ? -1000 : r.status;
}

/* MediaFormat COLOR_STANDARD / COLOR_TRANSFER / COLOR_RANGE / HDR_STATIC_INFO -> the helper's OPEN (ITU-T H.273) */
static void hw_colour(const struct ATL_codec_context *ctx, struct avd_open2 *o)
{
	switch (ctx->hw_standard) {
		case 1: o->primaries = 1, o->matrix = 1; break; /* BT709 */
		case 2: o->primaries = 5, o->matrix = 5; break; /* BT601_PAL */
		case 4: o->primaries = 6, o->matrix = 6; break; /* BT601_NTSC */
		case 6: o->primaries = 9, o->matrix = 9; break; /* BT2020 */
	}
	switch (ctx->hw_transfer) {
		case 1: o->transfer = 8; break;  /* LINEAR */
		case 3: o->transfer = 1; break;  /* SDR_VIDEO */
		case 6: o->transfer = 16; break; /* ST2084 (PQ, HDR10) */
		case 7: o->transfer = 18; break; /* HLG */
	}
	o->full_range = ctx->hw_range == 1;
	if (ctx->hw_hdr_len == 25 && ctx->hw_hdr[0] == 0) { /* type 0: ST 2086 + CTA-861.3, little-endian uint16 */
		uint16_t v[12];
		memcpy(v, ctx->hw_hdr + 1, sizeof(v));
		for (int i = 0; i < 3; i++)
			o->primaries_xy[i][0] = v[2 * i], o->primaries_xy[i][1] = v[2 * i + 1];
		o->white_xy[0] = v[6], o->white_xy[1] = v[7];
		o->max_luminance = v[8], o->min_luminance = v[9];
		o->max_cll = v[10], o->max_fall = v[11];
		o->has_mastering = v[8] || v[0] || v[6];
		o->has_light_level = v[10] || v[11];
	}
}

/* configure: returns TRUE if the hardware path is used */""")

edit(MC, """	gboolean hevc = !strcmp(name, "hevc");
	if (!hevc && strcmp(name, "h264"))
		return FALSE;""", """	gboolean hevc = !strcmp(name, "hevc"), vp9 = !strcmp(name, "vp9");
	if (!hevc && !vp9 && strcmp(name, "h264"))
		return FALSE;""")
# OPEN v2 (+ HELLO for VP9), reply parsing through hw_result
edit(MC, """	struct {
		struct avd_hdr h;
		uint32_t codec, width, height;
	} o = {{AVD_OPEN, 12, 0}, hevc ? 2 : 1, ctx->hw_width, ctx->hw_height};
	struct {
		struct avd_hdr h;
		int32_t status;
		char msg[60];
	} r = {0};
	size_t got = 0;
	if (hw_write_all(fd, &o, sizeof(o.h) + 12) == 0) { /* not sizeof(o): padded to 32 */
		gint64 end = g_get_monotonic_time() + 10000000; /* NDL load takes ~2 s on the TV */
		while (got < sizeof(r)) {
			int left = (int)((end - g_get_monotonic_time()) / 1000);
			struct pollfd p = {fd, POLLIN, 0};
			if (left <= 0 || poll(&p, 1, left) <= 0)
				break;
			ssize_t n = read(fd, (char *)&r + got, sizeof(r) - got);
			if (n <= 0)
				break;
			got += n;
		}
	}
	if (got < sizeof(r) || r.h.type != AVD_RESULT || r.status != 0) {
		r.msg[sizeof(r.msg) - 1] = 0;
		fprintf(stderr, "ATL_HWDEC: helper OPEN failed (%s), using FFmpeg\\n", got < sizeof(r) ? "no reply" : r.msg);
		close(fd);
		return FALSE;
	}""", """	char rmsg[64];
	if (vp9) { /* only a protocol-2 helper whose decoder takes VP9 (an old one would load an H.264 decoder for it) */
		struct avd_hdr hh = {AVD_HELLO, 0, 0};
		int st = hw_write_all(fd, &hh, sizeof(hh)) ? -1000 : hw_result(fd, 300, rmsg, sizeof(rmsg));
		if (st != 0 || !strstr(rmsg, " starfish ") || !strstr(rmsg, "vp9")) {
			fprintf(stderr, "ATL_HWDEC: helper has no VP9 decoder (%s), using FFmpeg\\n", st == -1000 ? "no reply: old helper" : rmsg);
			close(fd);
			return FALSE;
		}
	}
	const char *ah = getenv("ATL_HWDEC_AHEAD");
	struct {
		struct avd_hdr h;
		struct avd_open2 o;
	} o;
	memset(&o, 0, sizeof(o));
	o.h.type = AVD_OPEN;
	o.h.len = sizeof(o.o);
	o.o.codec = hevc ? 2 : vp9 ? 3 : 1;
	o.o.width = ctx->hw_width;
	o.o.height = ctx->hw_height;
	o.o.flags = ah && !strcmp(ah, "0") ? 0 : 1 /* AVD_OPEN_AHEAD */;
	o.o.fps_milli = ctx->hw_fps_milli;
	hw_colour(ctx, &o.o);
	/* NDL load takes ~2 s on the TV */
	int status = hw_write_all(fd, &o, sizeof(o)) ? -1000 : hw_result(fd, 10000, rmsg, sizeof(rmsg));
	if (status != 0) {
		fprintf(stderr, "ATL_HWDEC: helper OPEN failed (%s), using FFmpeg\\n", rmsg);
		close(fd);
		return FALSE;
	}""")
edit(MC, "\thw->hevc = hevc;\n", "\thw->hevc = hevc;\n\thw->vp9 = vp9;\n\thw->ahead = strstr(rmsg, \" ahead\") != NULL;\n")
edit(MC, """	fprintf(stderr, "ATL_HWDEC: hardware decode active: %s %dx%d via %s\\n", name, ctx->hw_width, ctx->hw_height, path);""",
     """	fprintf(stderr, "ATL_HWDEC: hardware decode active: %s %dx%d %.3f fps via %s (%s: %s)\\n", name, ctx->hw_width, ctx->hw_height,
	        ctx->hw_fps_milli / 1000.0, path, hw->ahead ? "AUs at queue time" : "AUs at release time", rmsg);""")

# queue: VP9 frames as they are; ahead mode sends at once
edit(MC, """	if (hw->n_aus >= HW_MAX_AUS || hw->bytes > HW_MAX_BYTES)
		return 1; /* full: Java retries on the next dequeueInputBuffer */""", """	if (hw->ahead ? hw->n_win >= HW_MAX_AUS + HW_WINDOW : (hw->n_aus >= HW_MAX_AUS || hw->bytes > HW_MAX_BYTES))
		return 1; /* full: Java retries on the next dequeueInputBuffer */""")
edit(MC, "\tsize_t pre = (hw->need_csd ? hw->csd_len : 0) + hw->prefix_len;",
     "\tif (hw->vp9)\n\t\thw->need_csd = FALSE; /* VP9 codec-private data is not part of the frames */\n"
     "\tsize_t pre = (hw->need_csd ? hw->csd_len : 0) + hw->prefix_len;")
edit(MC, "\tif (!hw_has_vcl(au + pre, len, hw->hevc)) { /* config only: goes in front of the next picture */",
     "\tif (!hw->vp9 && !hw_has_vcl(au + pre, len, hw->hevc)) { /* config only: goes in front of the next picture */")
edit(MC, """	hw->aus[hw->n_aus++] = (struct hw_au){au, pre + len, pts, hw->next_idx};
	hw->bytes += pre + len;""", """	if (hw->ahead) { /* the helper's pipeline schedules it by pts */
		hw_send(hw, AVD_FEED, au, pre + len, NULL, 0, pts);
		if (hw->fed++ < 3 || hw->fed % 600 == 0)
			fprintf(stderr, "ATL_HWDEC: fed AU #%lu ahead (%zu bytes, pts %" PRId64 ")\\n", hw->fed, pre + len, (int64_t)pts);
		free(au);
	} else {
		hw->aus[hw->n_aus++] = (struct hw_au){au, pre + len, pts, hw->next_idx};
		hw->bytes += pre + len;
	}""")
# dequeue: the token carries the frame's pts
edit(MC, "\tint64_t token;\n\thwdec_check_area(hw);", "\tint64_t token, tok_pts = 0;\n\thwdec_check_area(hw);")
edit(MC, "\t\ttoken = hw->win[m].idx;\n", "\t\ttoken = hw->win[m].idx;\n\t\ttok_pts = hw->win[m].pts;\n")
edit(MC, "\tmemcpy(raw, &token, sizeof(token));\n", "\tmemcpy(raw, &token, sizeof(token));\n\tmemcpy(raw + 8, &tok_pts, sizeof(tok_pts)); /* output buffers are 8 KiB */\n")
# release: ahead mode only reports the due time
edit(MC, "\tmemcpy(&token, raw, sizeof(token));\n\trelease_nio_buffer(env, array_ref, array);\n\tint64_t due = render",
     "\tmemcpy(&token, raw, sizeof(token));\n\tint64_t tok_pts;\n\tmemcpy(&tok_pts, raw + 8, sizeof(tok_pts));\n"
     "\trelease_nio_buffer(env, array_ref, array);\n\tint64_t due = render")
edit(MC, "\tif (hw->n_aus)\n\t\thw_send(hw, AVD_DUE, &due, sizeof(due), NULL, 0, 0);\n",
     "\tif (hw->ahead) {\n\t\tif (token != INT64_MAX)\n\t\t\thw_send(hw, AVD_DUE, &due, sizeof(due), NULL, 0, tok_pts);\n\t\treturn;\n\t}\n"
     "\tif (hw->n_aus)\n\t\thw_send(hw, AVD_DUE, &due, sizeof(due), NULL, 0, 0);\n")
# MediaFormat details for OPEN
edit(MC, "JNIEXPORT void JNICALL Java_android_media_MediaCodec_native_1set_1output_1surface",
     """JNIEXPORT void JNICALL Java_android_media_MediaCodec_native_1set_1video_1info(JNIEnv *env, jobject this, jlong codec, jint fps_milli,
                                                                             jint standard, jint transfer, jint range, jobject hdr)
{
	struct ATL_codec_context *ctx = _PTR(codec);
	ctx->hw_fps_milli = fps_milli > 0 ? fps_milli : 0;
	ctx->hw_standard = standard;
	ctx->hw_transfer = transfer;
	ctx->hw_range = range;
	ctx->hw_hdr_len = 0;
	if (hdr && get_nio_buffer_size(env, hdr) >= 25) {
		jarray array_ref;
		jbyte *array;
		uint8_t *p = get_nio_buffer(env, hdr, &array_ref, &array);
		if (p) {
			memcpy(ctx->hw_hdr, p, 25);
			ctx->hw_hdr_len = 25;
		}
		release_nio_buffer(env, array_ref, array);
	}
}

JNIEXPORT void JNICALL Java_android_media_MediaCodec_native_1set_1output_1surface""")
print("hwdec-ahead: ok")
