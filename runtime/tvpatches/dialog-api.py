#!/usr/bin/env python3
"""Framework dialogs show their content. AlertDialog.Builder.setView() was a no-op, so FilmPlus' link menu
(Builder + setView(ListView) + show) was an empty card on the scrim ("dims then nothing"). Now the view goes into the
dialog's decor (Window.setContentView) and a new native nativeSetView puts the decor into the AlertDialog content box
after the list, so title, message, items and buttons stay (nativeConstruct keeps a ref to the decor it detaches).
AlertDialog height is wrap-content as on Android. Also Builder.setView(int), setOnDismissListener, setOnKeyListener,
setSingleChoiceItems/setAdapter (-> items with labels), getContext; Dialog.getLayoutInflater() (StubGen returned null:
CloudStream's home-provider picker aborted on an NPE) and Dialog.setOnKeyListener (called first in dispatchKeyEvent).
usage: dialog-api.py <atl src root>"""
import os, sys

root = sys.argv[1]
s = ""


def rep(old, new):
    global s
    assert old in s, old[:60]
    s = s.replace(old, new, 1)


# --- native: keep the decor, nativeSetView ---
p = os.path.join(root, "src/api-impl-jni/app/android_app_AlertDialog.c")
s = open(p).read()
if "nativeSetView" not in s:
    rep("""	gtk_window_set_child(dialog, GTK_WIDGET(content_area));
}
""", """	/* the decor wrapper set by Dialog.nativeInit; AlertDialog.setView() puts it back after the list */
	GtkWidget *decor = gtk_window_get_child(dialog);
	if (decor)
		g_object_set_data_full(G_OBJECT(dialog), "atl-decor", g_object_ref(decor), g_object_unref);

	gtk_window_set_child(dialog, GTK_WIDGET(content_area));
}

JNIEXPORT void JNICALL Java_android_app_AlertDialog_nativeSetView(JNIEnv *env, jobject this, jlong ptr)
{
	GtkWindow *dialog = GTK_WINDOW(_PTR(ptr));
	GtkWidget *decor = g_object_get_data(G_OBJECT(dialog), "atl-decor");
	GtkWidget *list = g_object_get_data(G_OBJECT(dialog), "list");
	if (!decor || !list || gtk_widget_get_parent(decor))
		return;
	gtk_box_insert_child_after(GTK_BOX(gtk_widget_get_parent(list)), decor, list);
	gtk_widget_set_vexpand(decor, TRUE);
}
""")
    open(p, "w").write(s)

# --- AlertDialog.java ---
p = os.path.join(root, "src/api-impl/android/app/AlertDialog.java")
s = open(p).read()
if "nativeSetView" not in s:
    rep("""import android.content.DialogInterface;
import android.view.View;
import android.widget.Button;
""", """import android.content.DialogInterface;
import android.view.LayoutInflater;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.ListAdapter;
""")
    rep("""	private native void nativeSetItems(long ptr, String[] items, DialogInterface.OnClickListener listener);
""", """	private native void nativeSetItems(long ptr, String[] items, DialogInterface.OnClickListener listener);
	private native void nativeSetView(long ptr);
""")
    rep("""	public void setView(View view) {
		setContentView(view);
	}
""", """	public void setView(View view) {
		// the view goes into the decor, and the decor into the content box after the list (title/message/buttons stay)
		getWindow().setContentView(view);
		nativeSetView(nativePtr);
		getWindow().getAttributes().height = WindowManager.LayoutParams.WRAP_CONTENT;
	}
""")
    rep("""		public AlertDialog.Builder setView(View view) {
			return this;
		}
""", """		public AlertDialog.Builder setView(View view) {
			dialog.setView(view);
			return this;
		}

		public AlertDialog.Builder setView(int layoutResId) {
			dialog.setView(LayoutInflater.from(dialog.getContext()).inflate(layoutResId, null));
			return this;
		}

		public Context getContext() {
			return dialog.getContext();
		}

		public Builder setOnDismissListener(OnDismissListener onDismissListener) {
			dialog.setOnDismissListener(onDismissListener);
			return this;
		}

		public Builder setOnKeyListener(OnKeyListener onKeyListener) {
			dialog.setOnKeyListener(onKeyListener);
			return this;
		}

		public AlertDialog.Builder setSingleChoiceItems(CharSequence[] items, int checkedItem, final DialogInterface.OnClickListener listener) {
			return setItems(items, listener);
		}

		public AlertDialog.Builder setSingleChoiceItems(int itemsId, int checkedItem, final DialogInterface.OnClickListener listener) {
			return setItems(itemsId, listener);
		}

		public AlertDialog.Builder setAdapter(ListAdapter adapter, final DialogInterface.OnClickListener listener) {
			CharSequence[] items = new CharSequence[adapter.getCount()];
			for (int i = 0; i < items.length; i++)
				items[i] = String.valueOf(adapter.getItem(i));
			return setItems(items, listener);
		}
""")
    open(p, "w").write(s)

# --- Dialog.java ---
p = os.path.join(root, "src/api-impl/android/app/Dialog.java")
s = open(p).read()
if "getLayoutInflater" not in s:
    rep("""	private OnShowListener onShowListener;
""", """	private OnShowListener onShowListener;
	private OnKeyListener onKeyListener;
""")
    rep("""	public boolean dispatchKeyEvent(KeyEvent event) {
""", """	public boolean dispatchKeyEvent(KeyEvent event) {
		if (onKeyListener != null && onKeyListener.onKey(this, event.getKeyCode(), event))
			return true;
""")
    rep("""	public void setOnKeyListener(OnKeyListener onKeyListener) {}
""", """	public void setOnKeyListener(OnKeyListener onKeyListener) {
		this.onKeyListener = onKeyListener;
	}

	public LayoutInflater getLayoutInflater() {
		return LayoutInflater.from(context);
	}
""")
    open(p, "w").write(s)
