#!/usr/bin/env python3
"""TV remote keys -> Android KeyEvents, and BACK/MENU/media keys that actually reach the Activity.
 - atl_map_key_code(keyval, keycode): maps Linux evdev codes of remote keys (keycode - 8 under Wayland) first:
   BACK (158, 412, ESC on TV), OK/SELECT (352) -> DPAD_CENTER, MENU, INFO, colour keys, CH+/-, media keys, captions,
   digits; then ATL's keyval table. Used by WrapperWidget's key controllers.
 - A window-level key controller (bubble phase = only keys no widget consumed) sends KeyEvents to the current
   Activity's dispatchKeyEvent(). Activity now implements KeyEvent.Callback like Android: dispatchKeyEvent ->
   onKeyDown/onKeyUp, and BACK up -> onBackPressed() (androidx OnBackPressedDispatcher, fragments, finish()).
usage: keys.py <atl src root>"""
import os, sys
R = sys.argv[1]
J = os.path.join(R, "src/api-impl-jni")
A = os.path.join(R, "src/api-impl/android")


def rw(p, f):
    s = open(p).read()
    t = f(s)
    open(p, "w").write(t)
    return t


# ---------------------------------------------------------------- native key map (WrapperWidget.c)
TVMAP = r'''
/* ---- TV remote: Linux evdev codes (Wayland keycode = evdev + 8) -> Android KeyEvent codes ---- */
static int map_tv_evdev(guint evdev)
{
	switch (evdev) {
		case 158: /* KEY_BACK */
		case 412: /* KEY_PREVIOUS: webOS remote Back */
			return 4;   /* KEYCODE_BACK */
		case 352: /* KEY_OK */
		case 353: /* KEY_SELECT */
			return 23;  /* KEYCODE_DPAD_CENTER */
		case 139: /* KEY_MENU */
		case 127: /* KEY_COMPOSE */
			return 82;  /* KEYCODE_MENU */
		case 358: /* KEY_INFO */
			return 165; /* KEYCODE_INFO */
		case 398: return 183; /* KEY_RED -> PROG_RED */
		case 399: return 184; /* GREEN */
		case 400: return 185; /* YELLOW */
		case 401: return 186; /* BLUE */
		case 402: return 166; /* KEY_CHANNELUP -> CHANNEL_UP */
		case 403: return 167; /* KEY_CHANNELDOWN */
		case 164: return 85;  /* KEY_PLAYPAUSE -> MEDIA_PLAY_PAUSE */
		case 207: return 126; /* KEY_PLAY -> MEDIA_PLAY */
		case 200: return 126; /* KEY_PLAYCD */
		case 119: return 127; /* KEY_PAUSE -> MEDIA_PAUSE */
		case 201: return 127; /* KEY_PAUSECD */
		case 128: /* KEY_STOP */
		case 166: return 86;  /* KEY_STOPCD -> MEDIA_STOP */
		case 163: return 87;  /* KEY_NEXTSONG -> MEDIA_NEXT */
		case 165: return 88;  /* KEY_PREVIOUSSONG -> MEDIA_PREVIOUS */
		case 168: return 89;  /* KEY_REWIND -> MEDIA_REWIND */
		case 208: return 90;  /* KEY_FASTFORWARD -> MEDIA_FAST_FORWARD */
		case 167: return 130; /* KEY_RECORD -> MEDIA_RECORD */
		case 370: return 175; /* KEY_SUBTITLE -> CAPTIONS */
		case 392: return 222; /* KEY_AUDIO -> MEDIA_AUDIO_TRACK */
		case 365: return 172; /* KEY_EPG -> GUIDE */
		case 377: return 170; /* KEY_TV -> TV */
		case 388: return 233; /* KEY_TEXT -> TV_TELETEXT */
		case 113: return 164; /* KEY_MUTE -> VOLUME_MUTE */
		case 115: return 24;  /* KEY_VOLUMEUP */
		case 114: return 25;  /* KEY_VOLUMEDOWN */
		case 28:  /* KEY_ENTER: LG remote OK arrives as ENTER on some models; leave ENTER */
		default:
			return 0;
	}
}

int atl_map_key_code(guint keyval, guint keycode)
{
	static int tv = -1;
	if (tv < 0)
		tv = getenv("ATL_IS_TELEVISION") != NULL;
	if (keycode >= 8) {
		int k = map_tv_evdev(keycode - 8);
		if (k)
			return k;
	}
	if (keyval == GDK_KEY_Back || (tv && keyval == GDK_KEY_Escape))
		return 4; /* KEYCODE_BACK */
	if (keyval == GDK_KEY_Menu)
		return 82;
	if (keyval == GDK_KEY_AudioPlay)
		return 126;
	if (keyval == GDK_KEY_AudioPause)
		return 127;
	if (keyval == GDK_KEY_AudioStop)
		return 86;
	if (keyval == GDK_KEY_AudioNext)
		return 87;
	if (keyval == GDK_KEY_AudioPrev)
		return 88;
	if (keyval == GDK_KEY_AudioRewind)
		return 89;
	if (keyval == GDK_KEY_AudioForward)
		return 90;
	return map_key_code(keyval);
}
'''


def wrapper(s):
    if "atl_map_key_code" in s:
        return s
    anchor = "#define META_SHIFT_ON"
    assert anchor in s
    s = s.replace(anchor, TVMAP + "\n" + anchor, 1)
    s = s.replace("ACTION_DOWN, map_key_code(keyval), 0, map_meta_state(state));", "ACTION_DOWN, atl_map_key_code(keyval, keycode), 0, map_meta_state(state));")
    s = s.replace("ACTION_UP, map_key_code(keyval), 0, map_meta_state(state));", "ACTION_UP, atl_map_key_code(keyval, keycode), 0, map_meta_state(state));")
    assert s.count("atl_map_key_code(keyval, keycode)") == 2
    # make the meta-state helper reusable from Activity.c
    s = s.replace("static int map_meta_state(GdkModifierType state)", "int atl_map_meta_state(GdkModifierType state);\nstatic int map_meta_state(GdkModifierType state)", 1)
    s += "\nint atl_map_meta_state(GdkModifierType state)\n{\n\treturn map_meta_state(state);\n}\n"
    return s


rw(os.path.join(J, "widgets/WrapperWidget.c"), wrapper)

# ---------------------------------------------------------------- window-level controller -> current Activity
ACT_C = r'''

/* ---- keys no widget consumed go to the current Activity (Android: PhoneWindow -> Activity.dispatchKeyEvent) ---- */
int atl_map_key_code(guint keyval, guint keycode);
int atl_map_meta_state(GdkModifierType state);

static gboolean activity_key(guint keyval, guint keycode, GdkModifierType state, int action)
{
	if (!activity_current)
		return FALSE;
	JNIEnv *env = get_jni_env();
	static jlong down_time;
	jlong now = g_get_monotonic_time() / 1000;
	if (action == 0)
		down_time = now;
	int code = atl_map_key_code(keyval, keycode);
	if (!code)
		return FALSE;
	jobject key_event = (*env)->NewObject(env, handle_cache.key_event.class, handle_cache.key_event.constructor, down_time, now, action, code, 0, atl_map_meta_state(state));
	_SET_INT_FIELD(key_event, "unicodeValue", gdk_keyval_to_unicode(keyval));
	jmethodID dispatch = _METHOD(_CLASS(activity_current), "dispatchKeyEvent", "(Landroid/view/KeyEvent;)Z");
	gboolean ret = (*env)->CallBooleanMethod(env, activity_current, dispatch, key_event);
	if ((*env)->ExceptionCheck(env))
		(*env)->ExceptionDescribe(env);
	(*env)->DeleteLocalRef(env, key_event);
	return ret;
}

static gboolean on_window_key_pressed(GtkEventControllerKey *c, guint keyval, guint keycode, GdkModifierType state, gpointer d)
{
	return activity_key(keyval, keycode, state, 0 /* ACTION_DOWN */);
}

static gboolean on_window_key_released(GtkEventControllerKey *c, guint keyval, guint keycode, GdkModifierType state, gpointer d)
{
	return activity_key(keyval, keycode, state, 1 /* ACTION_UP */);
}

void activity_install_key_controller(GtkWindow *w)
{
	GtkEventController *controller = gtk_event_controller_key_new();
	gtk_event_controller_set_propagation_phase(controller, GTK_PHASE_BUBBLE);
	g_signal_connect(controller, "key-pressed", G_CALLBACK(on_window_key_pressed), NULL);
	g_signal_connect(controller, "key-released", G_CALLBACK(on_window_key_released), NULL);
	gtk_widget_add_controller(GTK_WIDGET(w), controller);
}
'''
rw(os.path.join(J, "app/android_app_Activity.c"), lambda s: s if "activity_install_key_controller" in s else s + ACT_C)
rw(os.path.join(J, "app/android_app_Activity.h"),
   lambda s: s if "activity_install_key_controller" in s else s + "#include <gtk/gtk.h>\nvoid activity_install_key_controller(GtkWindow *w);\n")


def mainc(s):
    if "activity_install_key_controller" in s:
        return s
    anchor = "	gtk_window_set_child(GTK_WINDOW(window), stack);"
    assert anchor in s
    return s.replace(anchor, anchor + "\n	activity_install_key_controller(GTK_WINDOW(window));", 1)


rw(os.path.join(R, "src/main-executable/main.c"), mainc)

# ---------------------------------------------------------------- Activity implements KeyEvent.Callback
def activity(s):
    if "onKeyMultiple(int keyCode" in s:
        return s
    s = s.replace("public class Activity extends ContextThemeWrapper implements Window.Callback, LayoutInflater.Factory2 {",
                  "public class Activity extends ContextThemeWrapper implements Window.Callback, LayoutInflater.Factory2, KeyEvent.Callback {", 1)
    old = """	public boolean dispatchKeyEvent(KeyEvent event) {
		return false;
	}"""
    assert old in s
    new = """	/* Android: Window -> view hierarchy -> KeyEvent.dispatch(this). In ATL the view hierarchy already had the key
	 * through GTK (WrapperWidget controllers), so this only sees keys no view consumed. */
	public boolean dispatchKeyEvent(KeyEvent event) {
		onUserInteraction();
		return event.dispatch(this, null, this);
	}

	public void onUserInteraction() {}

	public boolean onKeyDown(int keyCode, KeyEvent event) {
		if (keyCode == KeyEvent.KEYCODE_BACK) {
			event.startTracking();
			return true;
		}
		return false;
	}

	public boolean onKeyUp(int keyCode, KeyEvent event) {
		if (keyCode == KeyEvent.KEYCODE_BACK && !event.isCanceled()) {
			onBackPressed();
			return true;
		}
		return false;
	}

	public boolean onKeyLongPress(int keyCode, KeyEvent event) {
		return false;
	}

	public boolean onKeyMultiple(int keyCode, int repeatCount, KeyEvent event) {
		return false;
	}

	public boolean onKeyShortcut(int keyCode, KeyEvent event) {
		return false;
	}"""
    return s.replace(old, new, 1)


rw(os.path.join(A, "app/Activity.java"), activity)
print("keys: ok")
