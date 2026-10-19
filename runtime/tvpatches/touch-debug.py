#!/usr/bin/env python3
"""ATL_TOUCH_DEBUG=1: log every pointer event ATL handles (picked widget chain, Java class, phase, action, result)
and every GtkGestureClick release -> performClick. Diagnostics for click routing; off by default.
usage: touch-debug.py <atl src root>"""
import os, sys
R = os.path.join(sys.argv[1], "src/api-impl-jni")


def edit(f, old, new):
    p = os.path.join(R, f)
    s = open(p).read()
    if new in s:
        return
    assert s.count(old) == 1, f + ": " + old[:70]
    open(p, "w").write(s.replace(old, new, 1))


V = "views/android_view_View.c"
edit(V, "static bool action_up_already_handled = false;", """static bool action_up_already_handled = false;

/* ATL_TOUCH_DEBUG=1 */
static int touch_dbg = -1;
static const char *jcls(jobject o)
{
	static char buf[200];
	JNIEnv *env = get_jni_env();
	jclass c = (*env)->GetObjectClass(env, o);
	jstring n = (*env)->CallObjectMethod(env, c, _METHOD(_CLASS(c), "getName", "()Ljava/lang/String;"));
	const char *s = (*env)->GetStringUTFChars(env, n, NULL);
	snprintf(buf, sizeof buf, "%s", s);
	(*env)->ReleaseStringUTFChars(env, n, s);
	(*env)->DeleteLocalRef(env, n);
	(*env)->DeleteLocalRef(env, c);
	return buf;
}
gboolean atl_widget_takes_pointer(GtkWidget *widget);
static void touch_debug_tree(GtkWidget *w, GtkWidget *root, int depth)
{
	graphene_rect_t r;
	if (depth > 7 || !gtk_widget_compute_bounds(w, root, &r))
		return;
	const char *what = WRAPPER_IS_WIDGET(w) && WRAPPER_WIDGET(w)->jobj ? jcls(WRAPPER_WIDGET(w)->jobj) : G_OBJECT_TYPE_NAME(w);
	fprintf(stderr, "ATLTOUCH   %*s%s %.0f,%.0f %.0fx%.0f target=%d vis=%d\\n", depth * 2, "", what, r.origin.x, r.origin.y,
	        r.size.width, r.size.height, atl_widget_takes_pointer(w), gtk_widget_get_visible(w));
	for (GtkWidget *c = gtk_widget_get_first_child(w); c; c = gtk_widget_get_next_sibling(c))
		touch_debug_tree(c, root, depth + 1);
}
static void touch_debug_event(GtkWidget *widget, WrapperWidget *wrapper, GtkPropagationPhase phase, int action, double x, double y, double rx, double ry, gboolean ret)
{
	if (touch_dbg < 0)
		touch_dbg = getenv("ATL_TOUCH_DEBUG") != NULL;
	if (!touch_dbg)
		return;
	fprintf(stderr, "ATLTOUCH %s action=%d at %.0f,%.0f (raw %.0f,%.0f) %s custom=%d -> %d\\n",
	        phase == GTK_PHASE_CAPTURE ? "capture" : phase == GTK_PHASE_BUBBLE ? "bubble" : "target",
	        action, x, y, rx, ry, jcls(wrapper->jobj), wrapper->custom_dispatch_touch, ret);
	if (action == 0 && phase != GTK_PHASE_CAPTURE) {
		GtkWidget *root = GTK_WIDGET(gtk_widget_get_root(widget));
		GtkWidget *hit = root ? gtk_widget_pick(root, rx, ry, GTK_PICK_DEFAULT) : NULL;
		fprintf(stderr, "ATLTOUCH   pick chain at %.0f,%.0f:", rx, ry);
		for (GtkWidget *w = hit; w; w = gtk_widget_get_parent(w)) {
			if (WRAPPER_IS_WIDGET(w) && WRAPPER_WIDGET(w)->jobj)
				fprintf(stderr, " [%s]", jcls(WRAPPER_WIDGET(w)->jobj));
			else
				fprintf(stderr, " %s", G_OBJECT_TYPE_NAME(w));
		}
		fprintf(stderr, "\\n");
		/* subtree of the innermost Java view under the point: what could have been picked */
		WrapperWidget *top = NULL;
		for (GtkWidget *w = hit; w && !top; w = gtk_widget_get_parent(w))
			if (WRAPPER_IS_WIDGET(w) && WRAPPER_WIDGET(w)->jobj) top = WRAPPER_WIDGET(w);
		if (top)
			touch_debug_tree(GTK_WIDGET(top), root, 0);
	}
}""")
edit(V, """	gboolean ret = call_ontouch_callback(wrapper, action, pointers, pointer_indices, phase, timestamp, event);""",
     """	gboolean ret = call_ontouch_callback(wrapper, action, pointers, pointer_indices, phase, timestamp, event);
	touch_debug_event(widget, wrapper, phase, action, x, y, raw_x, raw_y, ret);""")
edit(V, """	jboolean handled = (*env)->CallBooleanMethod(env, wrapper->jobj, handle_cache.view.performClick);
""", """	jboolean handled = (*env)->CallBooleanMethod(env, wrapper->jobj, handle_cache.view.performClick);
	if (getenv("ATL_TOUCH_DEBUG"))
		fprintf(stderr, "ATLTOUCH click gesture on %s -> performClick %d\\n", jcls(wrapper->jobj), handled);
""")
print("touch-debug: ok")
