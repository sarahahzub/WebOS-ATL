#!/usr/bin/env python3
"""Dialog(context, themeResId) applies the theme, as on Android: a non-zero theme wraps the context in a
ContextThemeWrapper, so the dialog's window background, colours and inflated views come from the dialog theme.
ATL ignored themeResId, so dialogs took the activity theme: FilmPlus' link menu (AlertDialog.Builder(this,
R.style.Dialog_Dark), white text) was drawn on the light activity windowBackground - white on white.
usage: dialog-theme.py <atl src root>"""
import os, sys
p = os.path.join(sys.argv[1], "src/api-impl/android/app/Dialog.java")
s = open(p).read()
if "new Window(this.context, this)" in s:
    sys.exit(0)


def rep(old, new):
    global s
    assert old in s, old[:60]
    s = s.replace(old, new, 1)


rep("""	public Dialog(Context context, int themeResId) {
		this.context = context;
		window = new Window(context, this);
""", """	public Dialog(Context context, int themeResId) {
		// as Android: an explicit dialog theme wraps the context (the window uses it too)
		this.context = themeResId != 0 ? new android.view.ContextThemeWrapper(context, themeResId) : context;
		window = new Window(this.context, this);
""")
open(p, "w").write(s)
