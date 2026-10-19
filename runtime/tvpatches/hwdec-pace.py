#!/usr/bin/env python3
"""On top of hwdec.py + hwdec-surface.py: frame pacing in the hardware decode path (ATL_HWDEC=1).
NDL runs in low-delay (game) mode on the TV: a frame is shown as soon as it is decoded, so the time an AU reaches the
helper is its display time. ExoPlayer (SDK >= 21) calls releaseOutputBuffer(index, releaseTimeNs) up to 50 ms before
the frame is due; the hw path fed at once and ignored the time, so frames were shown 0-50 ms early with jitter
(playback looked laggy). Now releaseOutputBuffer sends AVD_DUE (releaseTimeNs, CLOCK_MONOTONIC; 0 for render=false
or no time) before the AUs it feeds, and atlvdec holds them until then. Logs render/drop/timed counts every 600
releases (ExoPlayer drops = it was late, i.e. ATL starved).
usage: hwdec-pace.py <atl src root>"""
import os, sys
R = sys.argv[1]
J = os.path.join(R, "src/api-impl-jni")
MC = "media/android_media_MediaCodec.c"


def edit(rel, old, new, base=J, count=1):
    p = os.path.join(base, rel)
    s = open(p).read()
    if new in s:
        return
    assert old in s, f"{rel}: anchor not found: {old[:60]!r}"
    open(p, "w").write(s.replace(old, new, count))


edit(MC, "enum { AVD_OPEN = 1, AVD_FEED = 2, AVD_AREA = 3, AVD_FLUSH = 4, AVD_CLOSE = 5, AVD_RESULT = 16 };",
     "enum { AVD_OPEN = 1, AVD_FEED = 2, AVD_AREA = 3, AVD_FLUSH = 4, AVD_CLOSE = 5, AVD_DUE = 6, AVD_RESULT = 16 };")
edit(MC, "\tgint64 last_area;\n\tunsigned long fed;\n};",
     "\tgint64 last_area;\n\tunsigned long fed;\n"
     "\tunsigned long n_rel, n_drop, n_timed; /* releaseOutputBuffer calls: all, render=false, with a release time */\n"
     "\tint64_t early_sum, early_max;         /* how far ahead of now the release times were (ns) */\n};")
edit(MC, """static void hwdec_release(JNIEnv *env, struct hwdec *hw, jobject buffer)
{
	jarray array_ref;
	jbyte *array;
	int64_t token;
	uint8_t *raw = get_nio_buffer(env, buffer, &array_ref, &array);
	memcpy(&token, raw, sizeof(token));
	release_nio_buffer(env, array_ref, array);
	int n = 0;
""", """static void hwdec_release(JNIEnv *env, struct hwdec *hw, jobject buffer, jboolean render, jlong release_ns)
{
	jarray array_ref;
	jbyte *array;
	int64_t token;
	uint8_t *raw = get_nio_buffer(env, buffer, &array_ref, &array);
	memcpy(&token, raw, sizeof(token));
	release_nio_buffer(env, array_ref, array);
	int64_t due = render && release_ns > 0 ? release_ns : 0;
	hw->n_rel++;
	if (!render)
		hw->n_drop++;
	if (due) {
		int64_t e = due - g_get_monotonic_time() * 1000;
		hw->n_timed++;
		hw->early_sum += e;
		if (e > hw->early_max)
			hw->early_max = e;
	}
	if (hw->n_rel % 600 == 0) {
		fprintf(stderr, "ATL_HWDEC: releases %lu: %lu dropped (render=false), %lu timed, release time ahead avg %.1f max %.1f ms\\n",
		        hw->n_rel, hw->n_drop, hw->n_timed, hw->n_timed ? hw->early_sum / 1e6 / hw->n_timed : 0.0, hw->early_max / 1e6);
		hw->n_drop = hw->n_timed = 0;
		hw->early_sum = hw->early_max = 0;
	}
	if (hw->n_aus)
		hw_send(hw, AVD_DUE, &due, sizeof(due), NULL, 0, 0);
	int n = 0;
""")
edit(MC, """	if (ctx->hw) {
		hwdec_release(env, ctx->hw, buffer);
		return;
	}""", """	if (ctx->hw) {
		hwdec_release(env, ctx->hw, buffer, render, presentationTimeNs);
		return;
	}""")
print("hwdec-pace: ok")
