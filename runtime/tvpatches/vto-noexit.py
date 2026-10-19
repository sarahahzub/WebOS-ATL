#!/usr/bin/env python3
"""ViewTreeObserver global-layout / pre-draw listeners never kill the app.
ATL hooks these listeners to the window's GdkFrameClock. A dialog window that is not realized yet has no frame clock,
so g_signal_connect(NULL, ...) failed ("invalid (NULL) pointer instance"), the handle stayed 0, and the later
"remove listener" call hit the "invalid state" branch, which called exit(1). CloudStream died like this when a plugin's
BottomSheetDialog opened (AllMovieLand in the plugin list); exit() then tore ICU down under a running OkHttp thread
(SIGSEGV in RegexMatcher, the crash seen in app.log). Now:
- no frame clock on the view's window -> use the main window's clock (same display, same frame timing);
- disconnect looks the handler up on either clock;
- an unexpected state only logs a warning.
usage: vto-noexit.py <atl src root>"""
import os, sys
root = sys.argv[1]
p = os.path.join(root, "src/api-impl-jni/views/android_view_ViewTreeObserver.c")
s = open(p).read()
if "vto_clock" in s:
    sys.exit(0)

helper = """void _gdb_force_java_stack_trace(void);

/* tv: a not-yet-realized (dialog) window has no frame clock; fall back to the main window's clock */
static GdkFrameClock *vto_clock(GtkWidget *w)
{
	GdkFrameClock *c = w ? gtk_widget_get_frame_clock(w) : NULL;
	if (!c && window)
		c = gtk_widget_get_frame_clock(window);
	return c;
}

static void vto_disconnect(GtkWidget *w, gulong h)
{
	GdkFrameClock *c = w ? gtk_widget_get_frame_clock(w) : NULL;
	if (c && g_signal_handler_is_connected(c, h)) {
		g_signal_handler_disconnect(c, h);
		return;
	}
	c = window ? gtk_widget_get_frame_clock(window) : NULL;
	if (c && g_signal_handler_is_connected(c, h))
		g_signal_handler_disconnect(c, h);
}
"""
old = "void _gdb_force_java_stack_trace(void);\n"
assert old in s
s = s.replace(old, helper, 1)

for name in ("global_1layout", "predraw"):
    sig = "onGlobalLayout_signal_handle" if name == "global_1layout" else "onPreDraw_signal_handle"
    cb = "on_global_layout_callback" if name == "global_1layout" else "on_pre_draw_callback"
    ev = "layout" if name == "global_1layout" else "paint"
    a = "\tGdkFrameClock *clock = gtk_widget_get_frame_clock(window);\n\n\tif (have_listeners && !signal_handle) {\n"
    i = s.index("native_1set_1have_1" + name + "_1listeners(JNIEnv")
    j = s.index(a, i)
    s = s[:j] + "\tGdkFrameClock *clock = vto_clock(window);\n\n\tif (have_listeners && !signal_handle) {\n\t\tif (!clock) {\n\t\t\tfprintf(stderr, \"ViewTreeObserver: no frame clock yet, " + name + " listener not hooked\\n\");\n\t\t\treturn;\n\t\t}\n" + s[j + len(a):]
    d = "\t\tg_signal_handler_disconnect(G_OBJECT(clock), signal_handle);\n\t\t_SET_LONG_FIELD(this, \"" + sig + "\", 0);\n"
    k = s.index(d, i)
    s = s[:k] + "\t\tvto_disconnect(window, signal_handle);\n\t\t_SET_LONG_FIELD(this, \"" + sig + "\", 0);\n" + s[k + len(d):]
    e = "signal_handle);\n\t\texit(1);\n"
    m = s.index(e, i)
    s = s[:m] + "signal_handle);\n" + s[m + len(e):]
    assert cb and ev

open(p, "w").write(s)
