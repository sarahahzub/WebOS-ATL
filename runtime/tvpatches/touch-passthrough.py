#!/usr/bin/env python3
"""Touches/clicks fall through views that do not use them, like on Android.
Upstream: GTK picks the topmost widget under the pointer, so any full-size overlay sibling (an empty FrameLayout,
an empty ComposeView, a GONE child's container) swallowed every click and the real UI underneath never got one.
Android offers a touch to the children top-down and moves on to the next sibling when a child does not take it.
CloudStream's TV layout has two such overlays above its whole UI: nothing was clickable.
Now:
  - a View is only a pick target if it handles pointer input itself (touch/click/long-click/scroll controllers);
    inert containers are transparent (their children are still picked);
  - a view with its own dispatchTouchEvent (ComposeView/AndroidComposeView, ...) that declines a hover or a DOWN
    at a point where another view underneath could take it becomes transparent around that point (24 px), so the
    click goes to the view underneath; it takes touches again as soon as it accepts a hover there or redraws;
  - inline dialogs decide "touch outside" from the card's bounds (no longer from a pick);
  - hover enter without a current event no longer passes NULL to gdk_event_get_position.
usage: touch-passthrough.py <atl src root>"""
import os, sys
R = os.path.join(sys.argv[1], "src/api-impl-jni")


def edit(f, old, new):
    p = os.path.join(R, f)
    s = open(p).read()
    if new in s:
        return
    assert s.count(old) == 1, f + ": " + old[:70]
    open(p, "w").write(s.replace(old, new, 1))


edit("widgets/WrapperWidget.h", "	gboolean custom_dispatch_touch;", """	gboolean custom_dispatch_touch;
	gboolean touch_declined;          /* ATL: transparent for picks around (declined_x, declined_y) */
	double declined_x, declined_y;""")
edit("widgets/WrapperWidget.h", "void _setOnTouchListener(JNIEnv *env, jobject this, GtkWidget *widget);", """void _setOnTouchListener(JNIEnv *env, jobject this, GtkWidget *widget);
gboolean atl_widget_takes_pointer(GtkWidget *widget);
void atl_touch_declined(WrapperWidget *wrapper, double x, double y);""")

edit("widgets/WrapperWidget.c", "static void wrapper_widget_class_init(WrapperWidgetClass *class)\n{", """/* ATL: does this widget handle pointer input itself? (controllers added by ATL carry these keys) */
gboolean atl_widget_takes_pointer(GtkWidget *widget)
{
	return g_object_get_data(G_OBJECT(widget), "on_touch_listener") || g_object_get_data(G_OBJECT(widget), "on_click_listener")
	    || g_object_get_data(G_OBJECT(widget), "on_long_click_listener") || g_object_get_data(G_OBJECT(widget), "atl_pointer_target");
}

#define DECLINE_RADIUS 24
static gboolean wrapper_widget_contains(GtkWidget *widget, double x, double y)
{
	WrapperWidget *wrapper = WRAPPER_WIDGET(widget);
	if (wrapper->touch_declined && x > wrapper->declined_x - DECLINE_RADIUS && x < wrapper->declined_x + DECLINE_RADIUS
	    && y > wrapper->declined_y - DECLINE_RADIUS && y < wrapper->declined_y + DECLINE_RADIUS)
		return FALSE;
	if (!atl_widget_takes_pointer(widget))
		return FALSE;  /* inert view: let the pick fall through to siblings underneath, like Android */
	return GTK_WIDGET_CLASS(wrapper_widget_parent_class)->contains(widget, x, y);
}

/* a custom-dispatch view (e.g. Compose) did not take a hover/DOWN at (x, y) (wrapper coords): if some other view
 * underneath would be picked there, make this one transparent around the point so that view gets the click */
void atl_touch_declined(WrapperWidget *wrapper, double x, double y)
{
	GtkWidget *w = GTK_WIDGET(wrapper);
	GtkWidget *root = GTK_WIDGET(gtk_widget_get_root(w));
	graphene_point_t p;
	if (!root || !gtk_widget_compute_point(w, root, &GRAPHENE_POINT_INIT(x, y), &p))
		return;
	gboolean was = wrapper->touch_declined;
	double ox = wrapper->declined_x, oy = wrapper->declined_y;
	wrapper->touch_declined = TRUE;
	wrapper->declined_x = x;
	wrapper->declined_y = y;
	GtkWidget *hit = gtk_widget_pick(root, p.x, p.y, GTK_PICK_DEFAULT);
	gboolean other = hit && hit != w && !gtk_widget_is_ancestor(hit, w) && !gtk_widget_is_ancestor(w, hit);
	if (!other) {  /* nothing else would take it: keep the view as the target (and the old decline, if any) */
		wrapper->touch_declined = was;
		wrapper->declined_x = ox;
		wrapper->declined_y = oy;
	}
}

static void wrapper_widget_class_init(WrapperWidgetClass *class)
{""")
edit("widgets/WrapperWidget.c", "	widget_class->snapshot = wrapper_widget_snapshot;\n",
     "	widget_class->snapshot = wrapper_widget_snapshot;\n	widget_class->contains = wrapper_widget_contains;\n")
edit("widgets/WrapperWidget.c", """		g_signal_connect(controller, "released", G_CALLBACK(on_click), wrapper->jobj);
		gtk_widget_add_controller(GTK_WIDGET(wrapper), controller);""", """		g_signal_connect(controller, "released", G_CALLBACK(on_click), wrapper->jobj);
		gtk_widget_add_controller(GTK_WIDGET(wrapper), controller);
		g_object_set_data(G_OBJECT(wrapper), "atl_pointer_target", GINT_TO_POINTER(1));""")

V = "views/android_view_View.c"
# JavaWidget: only a target with its own click / long-click controllers
edit(V, """static void java_widget_init(JavaWidget *java_widget) {}
static void java_widget_class_init(JavaWidgetClass *class)
{
	G_OBJECT_CLASS(class)->dispose = java_widget_dispose;
}""", """static void java_widget_init(JavaWidget *java_widget) {}
static gboolean java_widget_contains(GtkWidget *widget, double x, double y)
{
	if (!atl_widget_takes_pointer(widget))
		return FALSE;
	return GTK_WIDGET_CLASS(g_type_class_peek(GTK_TYPE_WIDGET))->contains(widget, x, y);
}
static void java_widget_class_init(JavaWidgetClass *class)
{
	G_OBJECT_CLASS(class)->dispose = java_widget_dispose;
	GTK_WIDGET_CLASS(class)->contains = java_widget_contains;
}""")
# scroll controller makes the wrapper a target (mouse wheel)
edit(V, """		g_signal_connect(controller, "scroll", G_CALLBACK(scroll_cb), wrapper->jobj);
		gtk_widget_add_controller(GTK_WIDGET(wrapper), controller);""", """		g_signal_connect(controller, "scroll", G_CALLBACK(scroll_cb), wrapper->jobj);
		gtk_widget_add_controller(GTK_WIDGET(wrapper), controller);
		g_object_set_data(G_OBJECT(wrapper), "atl_pointer_target", GINT_TO_POINTER(1));""")
# hover result -> decline learning
edit(V, """static void call_hover_callback(jobject this, int action, float x, float y, float raw_x, float raw_y)
{
	JNIEnv *env = get_jni_env();
	jobject motion_event = (*env)->NewObject(env, handle_cache.motion_event.class, handle_cache.motion_event.constructor_single, SOURCE_CLASS_POINTER, action, 0, x, y, x, y);
	(*env)->CallBooleanMethod(env, this, handle_cache.view.dispatchHoverEvent, motion_event);
	if ((*env)->ExceptionCheck(env))
		(*env)->ExceptionDescribe(env);
}""", """static gboolean call_hover_callback(jobject this, int action, float x, float y, float raw_x, float raw_y)
{
	JNIEnv *env = get_jni_env();
	jobject motion_event = (*env)->NewObject(env, handle_cache.motion_event.class, handle_cache.motion_event.constructor_single, SOURCE_CLASS_POINTER, action, 0, x, y, x, y);
	jboolean ret = (*env)->CallBooleanMethod(env, this, handle_cache.view.dispatchHoverEvent, motion_event);
	if ((*env)->ExceptionCheck(env))
		(*env)->ExceptionDescribe(env);
	(*env)->DeleteLocalRef(env, motion_event);
	return ret;
}

static void hover_result(WrapperWidget *wrapper, gboolean taken, double x, double y)
{
	if (!wrapper->custom_dispatch_touch)
		return;
	if (taken)
		wrapper->touch_declined = FALSE;
	else
		atl_touch_declined(wrapper, x, y);
}""")
edit(V, """	GdkEvent *event = gtk_event_controller_get_current_event(GTK_EVENT_CONTROLLER(controller));
	gdk_event_get_position(event, &raw_x, &raw_y);
	wrapper->hover_exit_pending = FALSE;
	wrapper->hover_x = x;
	wrapper->hover_y = y;
	call_hover_callback(wrapper->jobj, ACTION_HOVER_ENTER, x, y, raw_x, raw_y);""", """	GdkEvent *event = gtk_event_controller_get_current_event(GTK_EVENT_CONTROLLER(controller));
	raw_x = x, raw_y = y;
	if (event)
		gdk_event_get_position(event, &raw_x, &raw_y);
	wrapper->hover_exit_pending = FALSE;
	wrapper->hover_x = x;
	wrapper->hover_y = y;
	hover_result(wrapper, call_hover_callback(wrapper->jobj, ACTION_HOVER_ENTER, x, y, raw_x, raw_y), x, y);""")
edit(V, """	GdkEvent *event = gtk_event_controller_get_current_event(GTK_EVENT_CONTROLLER(controller));
	gdk_event_get_position(event, &raw_x, &raw_y);
	wrapper->hover_x = x;
	wrapper->hover_y = y;
	call_hover_callback(wrapper->jobj, ACTION_HOVER_MOVE, x, y, raw_x, raw_y);""", """	GdkEvent *event = gtk_event_controller_get_current_event(GTK_EVENT_CONTROLLER(controller));
	raw_x = x, raw_y = y;
	if (event)
		gdk_event_get_position(event, &raw_x, &raw_y);
	wrapper->hover_x = x;
	wrapper->hover_y = y;
	hover_result(wrapper, call_hover_callback(wrapper->jobj, ACTION_HOVER_MOVE, x, y, raw_x, raw_y), x, y);""")
# declined DOWN -> decline learning
edit(V, """	gboolean ret = call_ontouch_callback(wrapper, action, pointers, pointer_indices, phase, timestamp, event);""",
     """	gboolean ret = call_ontouch_callback(wrapper, action, pointers, pointer_indices, phase, timestamp, event);
	if (!ret && action == ACTION_DOWN && phase == GTK_PHASE_BUBBLE && wrapper->custom_dispatch_touch)
		atl_touch_declined(wrapper, x, y);""")
# redraw -> may take touches again
edit(V, """	GtkWidget *widget = GTK_WIDGET(_PTR(widget_ptr));

	wrapper_widget_queue_draw(WRAPPER_WIDGET(gtk_widget_get_parent(widget)));""", """	GtkWidget *widget = GTK_WIDGET(_PTR(widget_ptr));

	WRAPPER_WIDGET(gtk_widget_get_parent(widget))->touch_declined = FALSE;
	wrapper_widget_queue_draw(WRAPPER_WIDGET(gtk_widget_get_parent(widget)));""")

D = "app/android_app_Dialog.c"
edit(D, """	GtkWidget *hit = gtk_widget_pick(scrim, x, y, GTK_PICK_DEFAULT);
	if (hit && card && (hit == card || gtk_widget_is_ancestor(hit, card)))
		return;""", """	graphene_rect_t b;
	if (card && gtk_widget_compute_bounds(card, scrim, &b) && graphene_rect_contains_point(&b, &GRAPHENE_POINT_INIT(x, y)))
		return;""")
edit("views/android_view_ViewGroup.c", """	GtkWidget *picked_child = gtk_widget_pick(widget, x, y, GTK_PICK_DEFAULT);
""", """	GtkWidget *picked_child = gtk_widget_pick(widget, x, y, GTK_PICK_DEFAULT);
	if (!picked_child)  /* only inert (transparent) views at this point: offer it to this view group */
		picked_child = widget;
""")
print("touch-passthrough: ok")
