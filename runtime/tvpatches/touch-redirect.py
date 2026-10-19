#!/usr/bin/env python3
"""The touch a custom-dispatch overlay declines goes on to the view underneath right away (no lost first click).
touch-passthrough makes an overlay that declined a DOWN transparent for the next touches; this delivers the
declined touch sequence itself: DOWN / MOVE / UP are sent as MotionEvents (view-relative coordinates) to the
views under the point, innermost first, until one takes them; if none takes the UP, the innermost view under the
point gets performClick() (GTK-native widgets such as Button only react to real GTK events, which cannot be
re-injected).
usage: touch-redirect.py <atl src root>"""
import os, sys
R = os.path.join(sys.argv[1], "src/api-impl-jni")


def edit(f, old, new):
    p = os.path.join(R, f)
    s = open(p).read()
    if new in s:
        return
    assert s.count(old) == 1, f + ": " + old[:70]
    open(p, "w").write(s.replace(old, new, 1))


edit("widgets/WrapperWidget.h", "void atl_touch_declined(WrapperWidget *wrapper, double x, double y);",
     "GtkWidget *atl_touch_declined(WrapperWidget *wrapper, double x, double y);")
W = "widgets/WrapperWidget.c"
edit(W, """void atl_touch_declined(WrapperWidget *wrapper, double x, double y)
{
	GtkWidget *w = GTK_WIDGET(wrapper);
	GtkWidget *root = GTK_WIDGET(gtk_widget_get_root(w));
	graphene_point_t p;
	if (!root || !gtk_widget_compute_point(w, root, &GRAPHENE_POINT_INIT(x, y), &p))
		return;""", """GtkWidget *atl_touch_declined(WrapperWidget *wrapper, double x, double y)
{
	GtkWidget *w = GTK_WIDGET(wrapper);
	GtkWidget *root = GTK_WIDGET(gtk_widget_get_root(w));
	graphene_point_t p;
	if (!root || !gtk_widget_compute_point(w, root, &GRAPHENE_POINT_INIT(x, y), &p))
		return NULL;""")
edit(W, """	if (!other) {  /* nothing else would take it: keep the view as the target (and the old decline, if any) */
		wrapper->touch_declined = was;
		wrapper->declined_x = ox;
		wrapper->declined_y = oy;
	}
}""", """	if (!other) {  /* nothing else would take it: keep the view as the target (and the old decline, if any) */
		wrapper->touch_declined = was;
		wrapper->declined_x = ox;
		wrapper->declined_y = oy;
		return NULL;
	}
	return hit;
}""")

V = "views/android_view_View.c"
edit(V, "static bool action_up_already_handled = false;", """static bool action_up_already_handled = false;

/* ---- redirect of a touch sequence declined by a custom-dispatch overlay (touch-redirect) ---- */
static GtkWidget *redir_hit;          /* widget under the declined DOWN (ref) */
static WrapperWidget *redir_from;     /* the overlay that declined it */
static WrapperWidget *redir_taker;    /* view that took the DOWN, gets the rest of the sequence */
static int redir_token;               /* stands in for the GdkEvent of redirected MotionEvents */
static double redir_down_x, redir_down_y;
static WrapperWidget *redir_clicker;  /* clickable view that took the DOWN (Android: clickable views consume touches) */

static gboolean view_clickable(WrapperWidget *o)
{
	GtkWidget *child = o->child;
	return (child && (g_object_get_data(G_OBJECT(child), "on_click_listener") || g_object_get_data(G_OBJECT(child), "on_long_click_listener")))
	    || g_object_get_data(G_OBJECT(o), "atl_click_override");
}

static const char *jcls(jobject o);  /* touch-debug */

static WrapperWidget *owner_wrapper(GtkWidget *w)
{
	for (; w; w = gtk_widget_get_parent(w))
		if (WRAPPER_IS_WIDGET(w) && WRAPPER_WIDGET(w)->jobj)
			return WRAPPER_WIDGET(w);
	return NULL;
}

static bool call_ontouch_callback(WrapperWidget *wrapper, int action, struct pointer pointers[MAX_POINTERS], GPtrArray *pointer_indices, GtkPropagationPhase phase, guint32 timestamp, GdkEvent *event);

static gboolean redir_send(WrapperWidget *o, GtkWidget *from, double fx, double fy, int action, guint32 time)
{
	graphene_point_t p;
	if (!gtk_widget_compute_point(from, GTK_WIDGET(o), &GRAPHENE_POINT_INIT(fx, fy), &p))
		return FALSE;
	struct pointer ptr[MAX_POINTERS] = {};
	ptr[1].id = 1;
	ptr[1].index = 0;
	ptr[1].coord_x = p.x;
	ptr[1].coord_y = p.y;
	ptr[1].raw_x = p.x;
	ptr[1].raw_y = p.y;
	GPtrArray *idx = g_ptr_array_new();
	g_ptr_array_add(idx, &ptr[1]);
	gboolean r = call_ontouch_callback(o, action, ptr, idx, GTK_PHASE_BUBBLE, time, (GdkEvent *)&redir_token);
	g_ptr_array_free(idx, TRUE);
	return r;
}

/* deepest Java view under a root point that handles input, searched by bounds and ignoring ancestor sizes: ATL
 * sometimes allocates a container 0x0 while its children are laid out and drawn (NavigationRailFrameLayout), so
 * GTK's pick never reaches them although Android would dispatch to them */
static WrapperWidget *deep_target(GtkWidget *w, GtkWidget *root, double rx, double ry, int depth)
{
	WrapperWidget *best = NULL;
	if (depth > 40 || !gtk_widget_get_visible(w))
		return NULL;
	for (GtkWidget *c = gtk_widget_get_last_child(w); c && !best; c = gtk_widget_get_prev_sibling(c))
		best = deep_target(c, root, rx, ry, depth + 1);
	if (best)
		return best;
	if (WRAPPER_IS_WIDGET(w) && WRAPPER_WIDGET(w)->jobj) {
		WrapperWidget *o = WRAPPER_WIDGET(w);
		graphene_rect_t r;
		if ((view_clickable(o) || g_object_get_data(G_OBJECT(o), "on_touch_listener")) && gtk_widget_compute_bounds(w, root, &r)
		    && r.size.width > 0 && r.size.height > 0 && graphene_rect_contains_point(&r, &GRAPHENE_POINT_INIT(rx, ry)))
			return o;
	}
	return NULL;
}

/* deliver one event of the redirected sequence; (fx, fy) are coordinates in `from` */
static void redir_deliver(GtkWidget *from, double fx, double fy, int action, guint32 time)
{
	JNIEnv *env = get_jni_env();
	if (action == ACTION_DOWN) {
		redir_taker = NULL;
		redir_clicker = NULL;
		redir_down_x = fx;
		redir_down_y = fy;
		{	/* a view inside the picked one that GTK could not reach (0x0 container): it gets the touch */
			GtkWidget *root = GTK_WIDGET(gtk_widget_get_root(from));
			WrapperWidget *ow = owner_wrapper(redir_hit);
			graphene_point_t rp;
			if (root && ow && gtk_widget_compute_point(from, root, &GRAPHENE_POINT_INIT(fx, fy), &rp)) {
				WrapperWidget *d = deep_target(GTK_WIDGET(ow), root, rp.x, rp.y, 0);
				if (d && d != ow) {
					g_object_unref(redir_hit);
					redir_hit = g_object_ref(GTK_WIDGET(d));
				}
			}
		}
		for (WrapperWidget *o = owner_wrapper(redir_hit); o; o = owner_wrapper(gtk_widget_get_parent(GTK_WIDGET(o)))) {
			if (o == redir_from)
				break;
			if (view_clickable(o) && !g_object_get_data(G_OBJECT(o), "on_touch_listener")) {
				redir_clicker = o;  /* plain clickable view: it takes the sequence, a tap = performClick */
				break;
			}
			if (!g_object_get_data(G_OBJECT(o), "on_touch_listener"))
				continue;
			if (redir_send(o, from, fx, fy, action, time)) {
				redir_taker = o;
				break;
			}
			if (view_clickable(o)) {  /* Android: a clickable view consumes the touch even if onTouchEvent is not overridden */
				redir_clicker = o;
				break;
			}
		}
		if (getenv("ATL_TOUCH_DEBUG"))
			fprintf(stderr, "ATLTOUCH redirect DOWN -> %s %s\\n", redir_taker ? "taken by" : redir_clicker ? "clickable" : "no taker",
			        redir_taker ? jcls(redir_taker->jobj) : redir_clicker ? jcls(redir_clicker->jobj) : "");
		return;
	}
	if (redir_taker) {
		redir_send(redir_taker, from, fx, fy, action, time);
		return;
	}
	if (action == ACTION_UP) {
		/* nobody took the DOWN: a tap (moved < 40 px) clicks what is under the point - a GTK-native button
		 * through its own "clicked" signal, any other view through performClick() */
		double dx = fx - redir_down_x, dy = fy - redir_down_y;
		if (dx * dx + dy * dy > 40 * 40)
			return;
		WrapperWidget *o = redir_clicker ? redir_clicker : owner_wrapper(redir_hit);
		GtkWidget *btn = NULL;
		for (GtkWidget *w = redir_hit; w && w != GTK_WIDGET(o); w = gtk_widget_get_parent(w))
			if (GTK_IS_BUTTON(w)) { btn = w; break; }
		jboolean r = 0;
		if (btn) {
			g_signal_emit_by_name(btn, "clicked");
			r = 1;
		} else if (o) {
			r = (*env)->CallBooleanMethod(env, o->jobj, handle_cache.view.performClick);
			if ((*env)->ExceptionCheck(env))
				(*env)->ExceptionDescribe(env);
		}
		if (getenv("ATL_TOUCH_DEBUG"))
			fprintf(stderr, "ATLTOUCH redirect UP -> %s %s\\n", btn ? "GtkButton clicked" : "performClick", r ? "handled" : "not handled");
	}
}

static void redir_end(void)
{
	if (redir_hit)
		g_object_unref(redir_hit);
	redir_hit = NULL;
	redir_from = NULL;
	redir_taker = NULL;
	redir_clicker = NULL;
}""")
# during a redirected sequence, the overlay's controllers hand every event over and stop GTK propagation
edit(V, """	if (event == canceled_event && cancel_triggerer != wrapper) {
		action = ACTION_CANCEL;
	}
""", """	if (event == canceled_event && cancel_triggerer != wrapper) {
		action = ACTION_CANCEL;
	}

	if (redir_from == wrapper && action != ACTION_DOWN) {
		redir_deliver(widget, x, y, action, timestamp);
		if (event_type == GDK_BUTTON_RELEASE || event_type == GDK_TOUCH_END) {
			if (pointers[id].id)
				remove_pointer_fast(pointer_indices, &pointers[id]);
			redir_end();
		}
		return TRUE;
	}
""")
edit(V, """	if (!ret && action == ACTION_DOWN && phase == GTK_PHASE_BUBBLE && wrapper->custom_dispatch_touch)
		atl_touch_declined(wrapper, x, y);""", """	if (!ret && action == ACTION_DOWN && phase == GTK_PHASE_BUBBLE && wrapper->custom_dispatch_touch) {
		GtkWidget *hit = atl_touch_declined(wrapper, x, y);
		if (hit) {  /* someone else is under the point: the whole sequence goes there */
			redir_end();
			redir_hit = g_object_ref(hit);
			redir_from = wrapper;
			redir_deliver(widget, x, y, ACTION_DOWN, timestamp);
			ret = TRUE;
		}
	}""")
# scroll: a wheel / CH+- scroll the overlay does not use goes to the scrollable view underneath
edit(V, """		g_signal_connect(controller, "scroll", G_CALLBACK(scroll_cb), wrapper->jobj);
		gtk_widget_add_controller(GTK_WIDGET(wrapper), controller);
		g_object_set_data(G_OBJECT(wrapper), "atl_pointer_target", GINT_TO_POINTER(1));""", """		g_signal_connect(controller, "scroll", G_CALLBACK(scroll_cb), wrapper->jobj);
		gtk_widget_add_controller(GTK_WIDGET(wrapper), controller);
		g_object_set_data(G_OBJECT(wrapper), "atl_pointer_target", GINT_TO_POINTER(1));
		g_object_set_data(G_OBJECT(wrapper), "atl_scroll_target", GINT_TO_POINTER(1));""")
edit(V, """	gboolean ret = (*env)->CallBooleanMethod(env, this, handle_cache.view.dispatchGenericMotionEvent, motion_event);
	if ((*env)->ExceptionCheck(env))
		(*env)->ExceptionDescribe(env);
""", """	gboolean ret = (*env)->CallBooleanMethod(env, this, handle_cache.view.dispatchGenericMotionEvent, motion_event);
	if ((*env)->ExceptionCheck(env))
		(*env)->ExceptionDescribe(env);
	if (!ret && wrapper->custom_dispatch_touch) {
		GtkWidget *hit = atl_touch_declined(wrapper, x, y);
		for (WrapperWidget *o = owner_wrapper(hit); o && o != wrapper && !ret; o = owner_wrapper(gtk_widget_get_parent(GTK_WIDGET(o)))) {
			if (!g_object_get_data(G_OBJECT(o), "atl_scroll_target"))
				continue;
			graphene_point_t p;
			if (!gtk_widget_compute_point(widget, GTK_WIDGET(o), &GRAPHENE_POINT_INIT(x, y), &p))
				continue;
			jobject ev2 = (*env)->NewObject(env, handle_cache.motion_event.class, handle_cache.motion_event.constructor_scroll,
			                                SOURCE_CLASS_POINTER, ACTION_SCROLL, gdk_event_get_time(event), p.x, p.y, raw_x, raw_y, dx, -dy);
			ret = (*env)->CallBooleanMethod(env, o->jobj, handle_cache.view.dispatchGenericMotionEvent, ev2);
			if ((*env)->ExceptionCheck(env))
				(*env)->ExceptionDescribe(env);
			(*env)->DeleteLocalRef(env, ev2);
			if (getenv("ATL_TOUCH_DEBUG"))
				fprintf(stderr, "ATLTOUCH scroll %.2f redirected -> %s %d\\n", dy, jcls(o->jobj), ret);
		}
	}
""")
edit("widgets/WrapperWidget.c", """		g_object_set_data(G_OBJECT(wrapper), "atl_pointer_target", GINT_TO_POINTER(1));""", """		g_object_set_data(G_OBJECT(wrapper), "atl_pointer_target", GINT_TO_POINTER(1));
		g_object_set_data(G_OBJECT(wrapper), "atl_click_override", GINT_TO_POINTER(1));""")
edit("widgets/WrapperWidget.c", """		wrapper->background = gtk_picture_new();
""", """		wrapper->background = gtk_picture_new();
		gtk_widget_set_can_target(wrapper->background, FALSE);  /* drawing only: never a click target */
""")
print("touch-redirect: ok")
