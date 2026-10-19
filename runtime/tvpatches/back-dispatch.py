#!/usr/bin/env python3
"""Remote BACK reaches Compose apps' BackHandler / OnBackPressedDispatcher instead of crashing them.
TituFlix (Jetpack Compose): the first key that reaches AndroidComposeView.dispatchKeyEvent (BACK; arrows/OK are
pointer moves/clicks in the shim's mouse mode) threw UninitializedPropertyAccessException "lateinit property
layoutDirection" (FocusOwnerImpl.moveFocus(Exit)): Android sets it from View.onRtlPropertiesChanged() when it
resolves RTL during measure, ATL never called it. The exception stayed pending (ART's ExceptionDescribe does not
clear it) and the window key controller's next JNI call aborted the runtime.
 - View: onRtlPropertiesChanged(getLayoutDirection()) once, on the first measure(); getKeyDispatcherState().
 - Window.superDispatchKeyEvent(): androidx KeyEventDispatcher calls it on SDK_INT < 28 (ComponentActivity
   .dispatchKeyEvent); ATL views already had the key through GTK, so it returns false and the key goes on to
   KeyEvent.dispatch(activity) -> onKeyUp(BACK) -> onBackPressed() -> OnBackPressedDispatcher (Navigation).
 - Key controllers (WrapperWidget, window -> Activity): a Java exception is described and cleared, and the key
   counts as not consumed so it still bubbles to the Activity.
usage: back-dispatch.py <atl src root>   (idempotent; apply after keys.py)"""
import os, sys
R = sys.argv[1]
A = os.path.join(R, "src/api-impl/android")
J = os.path.join(R, "src/api-impl-jni")


def rw(p, f):
    s = open(p).read()
    t = f(s)
    if t != s:
        open(p, "w").write(t)


def view(s):
    if "atl_rtl_resolved" in s:
        return s
    old = """	public final void measure(int widthMeasureSpec, int heightMeasureSpec) {
"""
    assert s.count(old) == 1
    new = """	private boolean atl_rtl_resolved;

	/* Android resolves RTL properties during measure and tells the view; Compose needs it (FocusOwner.layoutDirection) */
	public void onRtlPropertiesChanged(int layoutDirection) {}

	public KeyEvent.DispatcherState getKeyDispatcherState() {
		return null;
	}

	public final void measure(int widthMeasureSpec, int heightMeasureSpec) {
		if (!atl_rtl_resolved) {
			atl_rtl_resolved = true;
			onRtlPropertiesChanged(getLayoutDirection());
		}
"""
    s = s.replace(old, new, 1)
    assert "public void onRtlPropertiesChanged(" in s and s.count("public void onRtlPropertiesChanged(") == 1
    assert s.count("getKeyDispatcherState()") == 1
    return s


def window(s):
    if "superDispatchKeyEvent" in s:
        return s
    anchor = """	public boolean hasFeature(int featureId) {"""
    assert anchor in s
    new = """	/* androidx KeyEventDispatcher (SDK_INT < 28): the view hierarchy already had the key through GTK */
	public boolean superDispatchKeyEvent(KeyEvent event) {
		return false;
	}

"""
    return s.replace(anchor, new + anchor, 1)


DESC = "\tif ((*env)->ExceptionCheck(env))\n\t\t(*env)->ExceptionDescribe(env);\n"
CLEAR = """	if ((*env)->ExceptionCheck(env)) { /* ART keeps it pending after Describe: the next JNI call would abort */
		(*env)->ExceptionDescribe(env);
		(*env)->ExceptionClear(env);
		ret = FALSE; /* not consumed: the key bubbles on (window -> Activity: BACK -> onBackPressed) */
	}
"""


def wrapper(s):
    if "ret = FALSE; /* not consumed" in s:
        return s
    old = "\tgboolean ret = (*env)->CallBooleanMethod(env, wrapper->jobj, handle_cache.view.dispatchKeyEvent, key_event);\n" + DESC
    assert s.count(old) == 2, s.count(old)
    return s.replace(old, old.replace(DESC, CLEAR))


def activity_c(s):
    if "ret = FALSE; /* not consumed" in s:
        return s
    for who in ("target", "activity_current"):  # dialogs.py retargets activity_key() to an inline dialog
        old = "\tgboolean ret = (*env)->CallBooleanMethod(env, %s, dispatch, key_event);\n" % who + DESC
        if s.count(old) == 1:
            return s.replace(old, old.replace(DESC, CLEAR))
    raise SystemExit("back-dispatch: activity_key() not found (apply keys.py first)")


rw(os.path.join(A, "view/View.java"), view)
rw(os.path.join(A, "view/Window.java"), window)
rw(os.path.join(J, "widgets/WrapperWidget.c"), wrapper)
rw(os.path.join(J, "app/android_app_Activity.c"), activity_c)
print("back-dispatch: ok")
