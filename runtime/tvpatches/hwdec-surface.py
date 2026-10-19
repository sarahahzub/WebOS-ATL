#!/usr/bin/env python3
"""On top of hwdec.py: MediaCodec.setOutputSurface in the hardware path (ATL_HWDEC=1).
ExoPlayer (SDK >= 23) calls setOutputSurface when the app swaps or detaches its Surface (mini-player <-> fullscreen,
surface destroyed) and waits <= 2 s for it ("Detaching surface timed out" -> the player drops the codec). Upstream
setOutputSurface is empty. Now, with a hardware codec: the hole punch and the video AREA move to the new SurfaceView
(none for a placeholder Surface) on the main thread, the call returns at once, the decoder keeps going.
The FFmpeg path is unchanged (no-op). The SDK < 23 path (codec re-created) is covered by atlvdec's ATLVDEC_LINGER
(an OPEN right after CLOSE reuses the loaded decoder, so configure does not block for the ~2 s NDL load).
usage: hwdec-surface.py <atl src root>"""
import os, sys
R = sys.argv[1]
A = os.path.join(R, "src/api-impl/android")
J = os.path.join(R, "src/api-impl-jni")


def edit(rel, old, new, base=A, count=1):
    p = os.path.join(base, rel)
    s = open(p).read()
    if new in s:
        return
    assert old in s, f"{rel}: anchor not found: {old[:60]!r}"
    open(p, "w").write(s.replace(old, new, count))


edit("media/MediaCodec.java",
     "\tprivate native void native_set_video_size(long codec, int width, int height);\n",
     "\tprivate native void native_set_video_size(long codec, int width, int height);\n"
     "\tprivate native void native_set_output_surface(long codec, Surface surface);\n")
edit("media/MediaCodec.java", "\tpublic void setOutputSurface(Surface surface) {\n\t}",
     "\tpublic void setOutputSurface(Surface surface) {\n"
     "\t\tif (native_codec != 0)\n\t\t\tnative_set_output_surface(native_codec, surface);\n\t}")

MC = "media/android_media_MediaCodec.c"
# the area update tolerates "no SurfaceView"
edit(MC, """	struct hwdec *hw = p;
	GtkWidget *w = GTK_WIDGET(hw->widget);
	if (!hw->closed && !hw->dead) {
		if (!hw->widget->hole_punch) {""", """	struct hwdec *hw = p;
	if (!hw->closed && !hw->dead && hw->widget) {
		GtkWidget *w = GTK_WIDGET(hw->widget);
		if (!hw->widget->hole_punch) {""", base=J)
edit(MC, """	ctx->hw_width = width;
	ctx->hw_height = height;
}
""", """	ctx->hw_width = width;
	ctx->hw_height = height;
}

struct hw_retarget {
	struct hwdec *hw;
	SurfaceViewWidget *widget; /* NULL: no SurfaceView (detached / placeholder Surface) */
};

/* main thread: setOutputSurface - hole punch and video area move to the new SurfaceView (the decoder keeps going) */
static gboolean hwdec_retarget_idle(gpointer p)
{
	struct hw_retarget *rt = p;
	struct hwdec *hw = rt->hw;
	if (hw->widget != rt->widget) {
		if (hw->widget) {
			if (hw->widget->hole_punch) {
				hw->widget->hole_punch = FALSE;
				gtk_widget_queue_draw(GTK_WIDGET(hw->widget));
			}
			g_object_unref(hw->widget);
		}
		hw->widget = rt->widget; /* takes the reference */
		rt->widget = NULL;
		memset(hw->area, 0, sizeof(hw->area)); /* AREA is sent again for the new rect */
		fprintf(stderr, "ATL_HWDEC: output surface changed (%s)\\n", hw->widget ? "new SurfaceView" : "no SurfaceView");
	}
	if (rt->widget)
		g_object_unref(rt->widget);
	free(rt);
	return hwdec_area_idle(hw); /* drops the reference taken in setOutputSurface */
}

JNIEXPORT void JNICALL Java_android_media_MediaCodec_native_1set_1output_1surface(JNIEnv *env, jobject this, jlong codec, jobject surface_obj)
{
	struct ATL_codec_context *ctx = _PTR(codec);
	if (!ctx->hw)
		return; /* FFmpeg path: unchanged (no-op) */
	jlong wp = surface_obj ? _GET_LONG_FIELD(surface_obj, "widget") : 0;
	GtkWidget *child = wp ? gtk_widget_get_first_child(_PTR(wp)) : NULL;
	struct hw_retarget *rt = calloc(1, sizeof(*rt));
	rt->hw = ctx->hw;
	rt->widget = child && SURFACE_VIEW_IS_WIDGET(child) ? g_object_ref(SURFACE_VIEW_WIDGET(child)) : NULL;
	g_atomic_int_inc(&ctx->hw->refs);
	ctx->hw->last_area = g_get_monotonic_time(); /* the retarget does this area update */
	fprintf(stderr, "ATL_HWDEC: setOutputSurface(%s)\\n", rt->widget ? "SurfaceView" : "no SurfaceView");
	g_idle_add(hwdec_retarget_idle, rt);
}
""", base=J)
print("hwdec-surface: ok")
