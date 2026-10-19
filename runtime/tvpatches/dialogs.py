#!/usr/bin/env python3
"""Inline dialogs (ATL_DIALOG_INLINE=1, default with ATL_IS_TELEVISION): Dialogs are drawn inside the main window
(GtkOverlay above the activity stack: dimmed scrim + centred card) instead of a second toplevel window.
On compositors without real window management (webOS: a second toplevel became a blank fullscreen card that took no
input - the FilmPlus "white screen" after choosing a link) and for single-window kiosks this is the only thing
that works. Also:
 - Dialog: cancelable / OnCancelListener / onBackPressed / dispatchKeyEvent (BACK cancels, like Android),
 - window-level keys go to the topmost inline dialog before the Activity,
 - touch/click on the scrim cancels when setCanceledOnTouchOutside(true).
usage: dialogs.py <atl src root>"""
import os, re, sys
R = sys.argv[1]
J = os.path.join(R, "src/api-impl-jni")
A = os.path.join(R, "src/api-impl/android")


def rw(p, f):
    s = open(p).read()
    open(p, "w").write(f(s))


# ---------------------------------------------------------------- main window: overlay above the stack
def mainc(s):
    if "atl-overlay" in s:
        return s
    old = "	gtk_window_set_child(GTK_WINDOW(window), stack);"
    assert old in s
    new = """	GtkWidget *atl_overlay = gtk_overlay_new(); /* inline dialogs (ATL_DIALOG_INLINE) are overlays above the activity stack */
	gtk_overlay_set_child(GTK_OVERLAY(atl_overlay), stack);
	gtk_window_set_child(GTK_WINDOW(window), atl_overlay);
	g_object_set_data(G_OBJECT(window), "atl-stack", stack);
	g_object_set_data(G_OBJECT(window), "atl-overlay", atl_overlay);"""
    return s.replace(old, new, 1)


rw(os.path.join(R, "src/main-executable/main.c"), mainc)

# every user of "the window's child is the stack"
HELPER = """
/* the activity GtkStack (the main window's child is a GtkOverlay holding it) */
GtkStack *atl_main_stack(void);
"""
rw(os.path.join(J, "app/android_app_Activity.h"), lambda s: s if "atl_main_stack" in s else s + HELPER)
rw(os.path.join(J, "app/android_app_Activity.c"), lambda s: s if "GtkStack *atl_main_stack(void)\n{" in s else s.replace(
    "static GList *activity_backlog = NULL;",
    "GtkStack *atl_main_stack(void)\n{\n\treturn GTK_STACK(g_object_get_data(G_OBJECT(window), \"atl-stack\"));\n}\n\nstatic GList *activity_backlog = NULL;", 1))
for f in ("app/android_app_Activity.c", "views/android_view_WindowManagerImpl.c"):
    def fix(s):
        s = s.replace("GTK_STACK(gtk_window_get_child(window))", "atl_main_stack()")
        if '#include "../app/android_app_Activity.h"' not in s and "android_app_Activity.h" not in s:
            s = s.replace('#include "../defines.h"', '#include "../defines.h"\n#include "../app/android_app_Activity.h"', 1)
        return s
    rw(os.path.join(J, f), fix)
for root, _, files in os.walk(J):
    for fn in files:
        if fn.endswith(".c"):
            assert "GTK_STACK(gtk_window_get_child(window))" not in open(os.path.join(root, fn)).read(), fn

# ---------------------------------------------------------------- Dialog.c
INLINE_C = r'''
/* ---- inline dialogs ---- */
#include "../app/android_app_Activity.h"
static GList *inline_dialogs; /* GtkWindow* (data holder), topmost first */

static gboolean inline_enabled(void)
{
	static int on = -1;
	if (on < 0) {
		const char *e = getenv("ATL_DIALOG_INLINE");
		on = e ? strcmp(e, "0") != 0 : getenv("ATL_IS_TELEVISION") != NULL;
		if (on) {
			GtkCssProvider *css = gtk_css_provider_new();
			gtk_css_provider_load_from_string(css, ".atl-dialog-scrim { background-color: rgba(0,0,0,0.5); }");
			gtk_style_context_add_provider_for_display(gdk_display_get_default(), GTK_STYLE_PROVIDER(css), GTK_STYLE_PROVIDER_PRIORITY_APPLICATION);
		}
	}
	return on;
}

jobject atl_inline_dialog_top(void)
{
	return inline_dialogs ? g_object_get_data(G_OBJECT(inline_dialogs->data), "atl-jobj") : NULL;
}

static void on_scrim_pressed(GtkGestureClick *g, int n, double x, double y, GtkWindow *dialog)
{
	GtkWidget *scrim = gtk_event_controller_get_widget(GTK_EVENT_CONTROLLER(g));
	GtkWidget *card = g_object_get_data(G_OBJECT(dialog), "atl-inline-card");
	GtkWidget *hit = gtk_widget_pick(scrim, x, y, GTK_PICK_DEFAULT);
	if (hit && card && (hit == card || gtk_widget_is_ancestor(hit, card)))
		return;
	if (!GPOINTER_TO_INT(g_object_get_data(G_OBJECT(dialog), "atl-cancel-outside")))
		return;
	jobject jobj = g_object_get_data(G_OBJECT(dialog), "atl-jobj");
	JNIEnv *env = get_jni_env();
	(*env)->CallVoidMethod(env, jobj, _METHOD(_CLASS(jobj), "cancel", "()V"));
	if ((*env)->ExceptionCheck(env))
		(*env)->ExceptionDescribe(env);
}

static void inline_show(GtkWindow *dialog)
{
	if (g_object_get_data(G_OBJECT(dialog), "atl-inline-scrim"))
		return;
	GtkWidget *overlay = g_object_get_data(G_OBJECT(window), "atl-overlay");
	GtkWidget *child = gtk_window_get_child(dialog);
	GtkWidget *scrim = gtk_box_new(GTK_ORIENTATION_VERTICAL, 0);
	gtk_widget_add_css_class(scrim, "atl-dialog-scrim");
	gtk_widget_set_halign(scrim, GTK_ALIGN_FILL);
	gtk_widget_set_valign(scrim, GTK_ALIGN_FILL);
	GtkWidget *card = gtk_box_new(GTK_ORIENTATION_VERTICAL, 0);
	gtk_widget_set_hexpand(card, TRUE);
	gtk_widget_set_vexpand(card, TRUE);
	gtk_widget_set_halign(card, GTK_ALIGN_CENTER);
	gtk_widget_set_valign(card, GTK_ALIGN_CENTER);
	int w = -1, h = -1;
	gtk_window_get_default_size(dialog, &w, &h);
	gtk_widget_set_size_request(card, w > 0 ? w : -1, h > 0 ? h : -1);
	if (child) {
		g_object_ref(child);
		gtk_window_set_child(dialog, NULL);
		gtk_box_append(GTK_BOX(card), child);
		g_object_unref(child);
	}
	gtk_box_append(GTK_BOX(scrim), card);
	GtkGesture *click = gtk_gesture_click_new();
	g_signal_connect(click, "pressed", G_CALLBACK(on_scrim_pressed), dialog);
	gtk_widget_add_controller(scrim, GTK_EVENT_CONTROLLER(click));
	gtk_overlay_add_overlay(GTK_OVERLAY(overlay), scrim);
	g_object_set_data(G_OBJECT(dialog), "atl-inline-scrim", scrim);
	g_object_set_data(G_OBJECT(dialog), "atl-inline-card", card);
	inline_dialogs = g_list_prepend(inline_dialogs, dialog);
	if (child)
		gtk_widget_child_focus(child, GTK_DIR_TAB_FORWARD);
}

static gboolean inline_close(GtkWindow *dialog)
{
	GtkWidget *scrim = g_object_get_data(G_OBJECT(dialog), "atl-inline-scrim");
	if (!scrim)
		return FALSE;
	GtkWidget *card = g_object_get_data(G_OBJECT(dialog), "atl-inline-card");
	GtkWidget *child = gtk_widget_get_first_child(card);
	if (child) {
		g_object_ref(child);
		gtk_box_remove(GTK_BOX(card), child);
		gtk_window_set_child(dialog, child);
		g_object_unref(child);
	}
	gtk_overlay_remove_overlay(GTK_OVERLAY(g_object_get_data(G_OBJECT(window), "atl-overlay")), scrim);
	g_object_set_data(G_OBJECT(dialog), "atl-inline-scrim", NULL);
	g_object_set_data(G_OBJECT(dialog), "atl-inline-card", NULL);
	inline_dialogs = g_list_remove(inline_dialogs, dialog);
	return TRUE;
}
'''


def dialogc(s):
    if "inline_show" in s:
        return s
    s = s.replace("/* main app window */\nextern GtkWindow *window;", "/* main app window */\nextern GtkWindow *window;\n#include <string.h>\n" + INLINE_C, 1)
    assert "inline_show" in s
    # remember the java object on the GtkWindow
    s = s.replace('	g_signal_connect(GTK_WINDOW(dialog), "close-request", G_CALLBACK(on_close_request), _REF(this));',
                  '	jobject ref = _REF(this);\n	g_object_set_data(G_OBJECT(dialog), "atl-jobj", ref);\n	g_signal_connect(GTK_WINDOW(dialog), "close-request", G_CALLBACK(on_close_request), ref);', 1)
    # content view while shown inline
    s = s.replace("""	GtkWidget *widget = GTK_WIDGET(_PTR(widget_ptr));

	gtk_window_set_child(dialog, gtk_widget_get_parent(widget));""", """	GtkWidget *widget = GTK_WIDGET(_PTR(widget_ptr));
	GtkWidget *card = g_object_get_data(G_OBJECT(dialog), "atl-inline-card");
	if (card) {
		GtkWidget *old = gtk_widget_get_first_child(card);
		if (old)
			gtk_box_remove(GTK_BOX(card), old);
		gtk_box_append(GTK_BOX(card), gtk_widget_get_parent(widget));
		return;
	}

	gtk_window_set_child(dialog, gtk_widget_get_parent(widget));""", 1)
    s = s.replace("""	GtkWindow *dialog = GTK_WINDOW(_PTR(ptr));
	gtk_window_present(dialog);""", """	GtkWindow *dialog = GTK_WINDOW(_PTR(ptr));
	if (inline_enabled()) {
		inline_show(dialog);
		return;
	}
	gtk_window_present(dialog);""", 1)
    s = s.replace("""	remove_touch_outside(dialog);
	gtk_window_close(dialog);""", """	remove_touch_outside(dialog);
	if (inline_close(dialog))
		return;
	gtk_window_close(dialog);""", 1)
    s = s.replace("""	GtkWindow *dialog = GTK_WINDOW(_PTR(ptr));
	return gtk_widget_is_visible(GTK_WIDGET(dialog));""", """	GtkWindow *dialog = GTK_WINDOW(_PTR(ptr));
	if (g_object_get_data(G_OBJECT(dialog), "atl-inline-scrim"))
		return TRUE;
	return gtk_widget_is_visible(GTK_WIDGET(dialog));""", 1)
    s = s.replace("""	GtkWindow *dialog = GTK_WINDOW(_PTR(ptr));
	remove_touch_outside(dialog);
	if (!cancel)
		return;""", """	GtkWindow *dialog = GTK_WINDOW(_PTR(ptr));
	g_object_set_data(G_OBJECT(dialog), "atl-cancel-outside", GINT_TO_POINTER(cancel ? 1 : 0));
	remove_touch_outside(dialog);
	if (!cancel || inline_enabled())
		return;""", 1)
    for k in ("inline_enabled()) {\n\t\tinline_show", "if (inline_close(dialog))", "atl-cancel-outside\", GINT_TO_POINTER", "gtk_box_append(GTK_BOX(card), gtk_widget_get_parent(widget));"):
        assert k in s, k
    return s


rw(os.path.join(J, "app/android_app_Dialog.c"), dialogc)

# ---------------------------------------------------------------- keys: topmost inline dialog first
def actc(s):
    if "atl_inline_dialog_top" in s:
        return s
    old = """	jmethodID dispatch = _METHOD(_CLASS(activity_current), "dispatchKeyEvent", "(Landroid/view/KeyEvent;)Z");
	gboolean ret = (*env)->CallBooleanMethod(env, activity_current, dispatch, key_event);"""
    assert old in s, "keys.py must be applied first"
    new = """	jobject target = atl_inline_dialog_top(); /* a dialog shown inside the window gets keys first (BACK cancels it) */
	if (!target)
		target = activity_current;
	jmethodID dispatch = _METHOD(_CLASS(target), "dispatchKeyEvent", "(Landroid/view/KeyEvent;)Z");
	gboolean ret = (*env)->CallBooleanMethod(env, target, dispatch, key_event);"""
    s = s.replace(old, new, 1)
    s = s.replace("int atl_map_key_code(guint keyval, guint keycode);", "int atl_map_key_code(guint keyval, guint keycode);\njobject atl_inline_dialog_top(void);", 1)
    return s


rw(os.path.join(J, "app/android_app_Activity.c"), actc)

# ---------------------------------------------------------------- Java Dialog: cancel/back/keys
def dialogj(s):
    if "onBackPressed()" in s:
        return s
    s = s.replace("	public void setCancelable(boolean cancelable) {}", """	private boolean cancelable = true;
	private OnCancelListener onCancelListener;

	public void setCancelable(boolean cancelable) {
		this.cancelable = cancelable;
	}""", 1)
    s = s.replace("	public void setOnCancelListener(OnCancelListener onCancelListener) {}", """	public void setOnCancelListener(OnCancelListener onCancelListener) {
		this.onCancelListener = onCancelListener;
	}

	public void onBackPressed() {
		if (cancelable)
			cancel();
	}

	public boolean dispatchKeyEvent(KeyEvent event) {
		if (event.getKeyCode() == KeyEvent.KEYCODE_BACK || event.getKeyCode() == KeyEvent.KEYCODE_ESCAPE) {
			if (event.getAction() == KeyEvent.ACTION_UP && !event.isCanceled())
				onBackPressed();
			return true;
		}
		if (event.getAction() == KeyEvent.ACTION_DOWN)
			return onKeyDown(event.getKeyCode(), event);
		if (event.getAction() == KeyEvent.ACTION_UP)
			return onKeyUp(event.getKeyCode(), event);
		return false;
	}

	public boolean onKeyDown(int keyCode, KeyEvent event) {
		return false;
	}

	public boolean onKeyUp(int keyCode, KeyEvent event) {
		return false;
	}""", 1)
    old = """	public void cancel() {
		dismiss();
	}"""
    assert old in s
    s = s.replace(old, """	public void cancel() {
		if (onCancelListener != null)
			onCancelListener.onCancel(this);
		dismiss();
	}""", 1)
    if "import android.view.KeyEvent;" not in s:
        s = s.replace("package android.app;\n", "package android.app;\n\nimport android.view.KeyEvent;\n", 1)
    assert "private boolean cancelable" in s and "onCancelListener.onCancel" in s
    return s


rw(os.path.join(A, "app/Dialog.java"), dialogj)


def alertj(s):
    s = s.replace("""		public AlertDialog.Builder setCancelable(boolean cancelable) {
			return this;
		}""", """		public AlertDialog.Builder setCancelable(boolean cancelable) {
			dialog.setCancelable(cancelable);
			return this;
		}""", 1)
    s = s.replace("""		public Builder setOnCancelListener(OnCancelListener onCancelListener) {
			return this;
		}""", """		public Builder setOnCancelListener(OnCancelListener onCancelListener) {
			dialog.setOnCancelListener(onCancelListener);
			return this;
		}""", 1)
    return s


rw(os.path.join(A, "app/AlertDialog.java"), alertj)


def dlgiface(s):
    s = s.replace("""	public interface OnCancelListener {
	}""", """	public interface OnCancelListener {
		void onCancel(DialogInterface dialog);
	}""", 1)
    s = s.replace("""	public interface OnMultiChoiceClickListener {
	}""", """	public interface OnMultiChoiceClickListener {
		void onClick(DialogInterface dialog, int which, boolean isChecked);
	}""", 1)
    s = s.replace("""	public interface OnKeyListener {
	}""", """	public interface OnKeyListener {
		boolean onKey(DialogInterface dialog, int keyCode, android.view.KeyEvent event);
	}""", 1)
    return s


rw(os.path.join(R, "src/api-impl/android/content/DialogInterface.java"), dlgiface)
print("dialogs: ok")
