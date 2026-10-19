#!/usr/bin/env python3
"""Native android.app.AlertDialog (no setView) looks like an Android TV dialog in inline-dialog mode.
Before, the inline card had no background (small dark GTK text and outlined buttons right on the 50 % scrim, e.g.
FilmPlus "Install a video player") and the title was only the hidden GtkWindow title.
Now (default with ATL_IS_TELEVISION, ATL_ALERT_STYLE=0 turns it off, =1 forces it):
 - the content box is a dark, rounded, opaque card (#2b2e33, light text, 24dp padding, min 280dp wide; the message
   wraps at 50 chars, so the card stays below ~50 % of a 1080p window), sizes scaled by ATL_DENSITY/160;
 - AlertDialog.setTitle() shows a bold title label at the top of the card;
 - the buttons sit in one right-aligned row reading [neutral] [negative] [positive] as on Android (the row is RTL, so
   focus order stays positive first as before); flat accent-coloured text buttons with a visible focus ring;
 - empty message/list rows are hidden.
setView() dialogs (custom content, e.g. FilmPlus' link menu) are put back to exactly the previous plain layout,
and AppCompat dialogs (Dialog.setContentView) never use this code.
usage: alertdialog-style.py <atl src root>"""
import os, sys

root = sys.argv[1]
s = ""


def rep(old, new):
    global s
    assert old in s, old[:70]
    s = s.replace(old, new, 1)


# --- native ---
p = os.path.join(root, "src/api-impl-jni/app/android_app_AlertDialog.c")
s = open(p).read()
if "atl-alert-card" not in s:
    rep('''#include <gtk/gtk.h>
#include <jni.h>
''', '''#include <gtk/gtk.h>
#include <jni.h>
#include <stdlib.h>
#include <string.h>
''')
    rep('''G_DEFINE_TYPE(ListEntry, list_entry, G_TYPE_OBJECT)
''', '''G_DEFINE_TYPE(ListEntry, list_entry, G_TYPE_OBJECT)

/* Android-TV-like card for native AlertDialogs (inline-dialog mode); sizes in dp/sp scaled by ATL_DENSITY/160 */
static gboolean alert_style_enabled(void)
{
	static int on = -1;
	if (on < 0) {
		const char *e = getenv("ATL_ALERT_STYLE");
		on = e ? strcmp(e, "0") != 0 : getenv("ATL_IS_TELEVISION") != NULL;
		if (on) {
			const char *d = getenv("ATL_DENSITY");
			int dpi = d && atoi(d) > 0 ? atoi(d) : 160;
#define DP(x) ((int)((x) * dpi / 160.0 + 0.5))
			char *css = g_strdup_printf(
			    ".atl-alert-card { background-color: #2b2e33; color: #e8eaed; border-radius: %dpx; padding: %dpx %dpx %dpx %dpx;"
			    " min-width: %dpx; box-shadow: 0 %dpx %dpx rgba(0,0,0,0.5); }"
			    " .atl-alert-card label { color: #e8eaed; }"
			    " .atl-alert-title { font-size: %dpx; font-weight: bold; margin-bottom: %dpx; }"
			    " .atl-alert-message { font-size: %dpx; color: #c4c7cc; margin-bottom: %dpx; }"
			    " .atl-alert-card listview, .atl-alert-card listview row { background: none; color: #e8eaed; }"
			    " .atl-alert-card listview row { font-size: %dpx; padding: %dpx %dpx; border-radius: %dpx; }"
			    " .atl-alert-card listview row:selected { background-color: transparent; }"
			    " .atl-alert-card listview row:hover, .atl-alert-card listview row:focus { background-color: rgba(255,255,255,0.12); }"
			    " .atl-alert-buttons { margin-top: %dpx; }"
			    " .atl-alert-buttons button { background: none; border: none; box-shadow: none; min-height: 0; min-width: 0;"
			    " padding: %dpx %dpx; border-radius: %dpx; outline: none; }"
			    " .atl-alert-buttons button label { color: #8ab4f8; font-size: %dpx; font-weight: bold; }"
			    " .atl-alert-buttons button:hover, .atl-alert-buttons button:focus { background-color: rgba(138,180,248,0.18); }"
			    " .atl-alert-buttons button:focus { outline: %dpx solid #8ab4f8; outline-offset: 0; }",
			    DP(8), DP(24), DP(24), DP(12), DP(24), DP(280), DP(4), DP(16),
			    DP(20), DP(12), DP(16), DP(12), DP(16), DP(8), DP(8), DP(4),
			    DP(8), DP(8), DP(12), DP(4), DP(14), DP(1) > 1 ? DP(1) : 1);
#undef DP
			GtkCssProvider *provider = gtk_css_provider_new();
			gtk_css_provider_load_from_string(provider, css);
			gtk_style_context_add_provider_for_display(gdk_display_get_default(), GTK_STYLE_PROVIDER(provider), GTK_STYLE_PROVIDER_PRIORITY_APPLICATION);
			g_free(css);
		}
	}
	return on;
}
''')
    rep('''	GtkBox *content_area = GTK_BOX(gtk_box_new(GTK_ORIENTATION_VERTICAL, 1));

	GtkLabel *label = GTK_LABEL(gtk_label_new(NULL));
	gtk_label_set_wrap(label, TRUE);
	gtk_label_set_max_width_chars(label, 50);
	gtk_box_append(content_area, GTK_WIDGET(label));
''', '''	GtkBox *content_area = GTK_BOX(gtk_box_new(GTK_ORIENTATION_VERTICAL, 1));
	/* styled: card + title + button row; setView() puts the plain layout back (see nativeSetView) */
	gboolean styled = alert_style_enabled();
	if (styled)
		gtk_widget_add_css_class(GTK_WIDGET(content_area), "atl-alert-card");

	GtkLabel *title = GTK_LABEL(gtk_label_new(NULL));
	gtk_label_set_wrap(title, TRUE);
	gtk_label_set_max_width_chars(title, 40);
	gtk_label_set_xalign(title, 0);
	gtk_widget_add_css_class(GTK_WIDGET(title), "atl-alert-title");
	gtk_widget_set_visible(GTK_WIDGET(title), FALSE);
	gtk_box_append(content_area, GTK_WIDGET(title));
	g_object_set_data_full(G_OBJECT(dialog), "title", g_object_ref(title), g_object_unref);

	GtkLabel *label = GTK_LABEL(gtk_label_new(NULL));
	gtk_label_set_wrap(label, TRUE);
	gtk_label_set_max_width_chars(label, 50);
	if (styled) {
		gtk_label_set_xalign(label, 0);
		gtk_widget_add_css_class(GTK_WIDGET(label), "atl-alert-message");
		gtk_widget_set_visible(GTK_WIDGET(label), FALSE); /* shown by setMessage */
	}
	gtk_box_append(content_area, GTK_WIDGET(label));
''')
    rep('''	g_signal_connect(list, "activate", G_CALLBACK(activate_cb), NULL);
	gtk_box_append(content_area, GTK_WIDGET(list));
''', '''	g_signal_connect(list, "activate", G_CALLBACK(activate_cb), NULL);
	if (styled)
		gtk_widget_set_visible(GTK_WIDGET(list), FALSE); /* shown by setItems */
	gtk_box_append(content_area, GTK_WIDGET(list));
''')
    rep('''	gtk_box_append(content_area, gtk_widget_get_parent(GTK_WIDGET(_PTR(button_positive_ptr))));
	gtk_box_append(content_area, gtk_widget_get_parent(GTK_WIDGET(_PTR(button_negative_ptr))));
	gtk_box_append(content_area, gtk_widget_get_parent(GTK_WIDGET(_PTR(button_neutral_ptr))));
''', '''	/* positive, negative, neutral (focus order). Styled: one right-aligned RTL row, so it reads
	 * [neutral] [negative] [positive] as on Android while focus still starts at positive */
	GtkWidget *buttons = gtk_box_new(styled ? GTK_ORIENTATION_HORIZONTAL : GTK_ORIENTATION_VERTICAL, 1);
	gtk_box_append(GTK_BOX(buttons), gtk_widget_get_parent(GTK_WIDGET(_PTR(button_positive_ptr))));
	gtk_box_append(GTK_BOX(buttons), gtk_widget_get_parent(GTK_WIDGET(_PTR(button_negative_ptr))));
	gtk_box_append(GTK_BOX(buttons), gtk_widget_get_parent(GTK_WIDGET(_PTR(button_neutral_ptr))));
	if (styled) {
		gtk_box_set_spacing(GTK_BOX(buttons), 8);
		gtk_widget_set_direction(buttons, GTK_TEXT_DIR_RTL);
		gtk_widget_set_halign(buttons, GTK_ALIGN_START); /* START in RTL = right edge */
		gtk_widget_add_css_class(buttons, "atl-alert-buttons");
	}
	gtk_box_append(content_area, buttons);
	g_object_set_data_full(G_OBJECT(dialog), "buttons", g_object_ref(buttons), g_object_unref);
''')
    rep('''	GtkWidget *list = g_object_get_data(G_OBJECT(dialog), "list");
	if (!decor || !list || gtk_widget_get_parent(decor))
		return;
''', '''	GtkWidget *list = g_object_get_data(G_OBJECT(dialog), "list");
	/* custom content: back to the plain (pre-style) layout, the app's view brings its own look */
	g_object_set_data(G_OBJECT(dialog), "atl-custom-view", GINT_TO_POINTER(1));
	GtkWidget *title = g_object_get_data(G_OBJECT(dialog), "title");
	GtkWidget *label = g_object_get_data(G_OBJECT(dialog), "label");
	GtkWidget *buttons = g_object_get_data(G_OBJECT(dialog), "buttons");
	if (title)
		gtk_widget_set_visible(title, FALSE);
	if (label) {
		gtk_widget_remove_css_class(label, "atl-alert-message");
		gtk_label_set_xalign(GTK_LABEL(label), 0.5);
		gtk_widget_set_visible(label, TRUE);
	}
	if (list) {
		gtk_widget_remove_css_class(gtk_widget_get_parent(list), "atl-alert-card");
		gtk_widget_set_visible(list, TRUE);
	}
	if (buttons) {
		gtk_widget_remove_css_class(buttons, "atl-alert-buttons");
		gtk_orientable_set_orientation(GTK_ORIENTABLE(buttons), GTK_ORIENTATION_VERTICAL);
		gtk_box_set_spacing(GTK_BOX(buttons), 1);
		gtk_widget_set_direction(buttons, GTK_TEXT_DIR_NONE);
		gtk_widget_set_halign(buttons, GTK_ALIGN_FILL);
	}
	if (!decor || !list || gtk_widget_get_parent(decor))
		return;
''')
    rep('''	gtk_label_set_text(label, nativeMessage);
	(*env)->ReleaseStringUTFChars(env, message, nativeMessage);
}
''', '''	gtk_label_set_text(label, nativeMessage);
	(*env)->ReleaseStringUTFChars(env, message, nativeMessage);
	gtk_widget_set_visible(GTK_WIDGET(label), TRUE);
}

JNIEXPORT void JNICALL Java_android_app_AlertDialog_nativeSetAlertTitle(JNIEnv *env, jobject this, jlong ptr, jstring title)
{
	GtkWindow *dialog = GTK_WINDOW(_PTR(ptr));
	GtkLabel *label = g_object_get_data(G_OBJECT(dialog), "title");
	if (!label || !title)
		return;
	const char *nativeTitle = (*env)->GetStringUTFChars(env, title, NULL);
	gtk_label_set_text(label, nativeTitle);
	gtk_widget_set_visible(GTK_WIDGET(label), nativeTitle[0] && alert_style_enabled() && !g_object_get_data(G_OBJECT(dialog), "atl-custom-view"));
	(*env)->ReleaseStringUTFChars(env, title, nativeTitle);
}
''')
    rep('''	GtkSelectionModel *model = GTK_SELECTION_MODEL(gtk_single_selection_new(G_LIST_MODEL(store)));
	gtk_list_view_set_model(list, model);
''', '''	GtkSelectionModel *model = GTK_SELECTION_MODEL(gtk_single_selection_new(G_LIST_MODEL(store)));
	gtk_list_view_set_model(list, model);
	gtk_widget_set_visible(GTK_WIDGET(list), TRUE);
''')
    open(p, "w").write(s)

# button padding: ATL's View sets the Android padding as a per-widget provider on the GtkButton, which wins over ours,
# so the label gets the 8x12dp instead; the row spacing follows the density too
s = open(p).read()
if "button label { margin" not in s:
    rep('''static gboolean alert_style_enabled(void)
{
''', '''static int alert_dpi = 160;

static gboolean alert_style_enabled(void)
{
''')
    rep('''			int dpi = d && atoi(d) > 0 ? atoi(d) : 160;
''', '''			int dpi = d && atoi(d) > 0 ? atoi(d) : 160;
			alert_dpi = dpi;
''')
    rep('''#undef DP
			GtkCssProvider *provider = gtk_css_provider_new();
			gtk_css_provider_load_from_string(provider, css);
''', '''			char *css2 = g_strdup_printf("%s .atl-alert-buttons button label { margin: %dpx %dpx; }", css, DP(8), DP(12));
			g_free(css);
			css = css2;
#undef DP
			GtkCssProvider *provider = gtk_css_provider_new();
			gtk_css_provider_load_from_string(provider, css);
''')
    rep('''		gtk_box_set_spacing(GTK_BOX(buttons), 8);
''', '''		gtk_box_set_spacing(GTK_BOX(buttons), alert_dpi * 8 / 160);
''')
    open(p, "w").write(s)

# --- AlertDialog.java: setTitle also fills the card's title label ---
p = os.path.join(root, "src/api-impl/android/app/AlertDialog.java")
s = open(p).read()
if "nativeSetAlertTitle" not in s:
    rep('''	private native void nativeSetView(long ptr);
''', '''	private native void nativeSetView(long ptr);
	private native void nativeSetAlertTitle(long ptr, String title);
''')
    rep('''	public void setMessage(CharSequence message) {
''', '''	@Override
	public void setTitle(CharSequence title) {
		super.setTitle(title);
		if (buttons != null) // not during Dialog's constructor
			nativeSetAlertTitle(nativePtr, title == null ? "" : String.valueOf(title));
	}

	@Override
	public void setTitle(int titleId) {
		setTitle(getContext().getText(titleId));
	}

	public void setMessage(CharSequence message) {
''')
    open(p, "w").write(s)
