#!/usr/bin/env python3
"""Android TV device profile (ATL_IS_TELEVISION=1), so apps pick their TV layouts, leanback activities and D-pad UX.
 - Configuration: uiMode type TELEVISION (night bits kept; native night-mode updates no longer wipe the type),
   touchscreen NOTOUCH, navigation DPAD, keyboard NOKEYS  -> "-television" resources, UiModeManager.getCurrentModeType().
 - PackageManager.hasSystemFeature: leanback/television true; touchscreen/multitouch false; common TV hardware/software features.
 - Main activity: prefer an activity with category LEANBACK_LAUNCHER (what Android TV launches).
 - Build: manufacturer/model/brand/device from system properties; ATL_PROPS="key=value;key=value" overrides any property.
 - ATL_DEFAULT_SDK_INT=N: Build.VERSION.SDK_INT = max(app minSdk, N) when --sdk-int isn't given (upstream: minSdk).
 - ATL_NO_ANIMATIONS (or TV): no GtkStack slide between activities (software rendering on TV).
usage: tvprofile.py <atl src root>"""
import os, sys
R = sys.argv[1]
A = os.path.join(R, "src/api-impl/android")
J = os.path.join(R, "src/api-impl-jni")


def edit(rel, old, new, base=A, count=1):
    p = os.path.join(base, rel)
    s = open(p).read()
    if new in s:
        return
    assert old in s, f"{rel}: anchor not found: {old[:60]!r}"
    open(p, "w").write(s.replace(old, new, count))


open(os.path.join(A, "atl/ATLTv.java"), "w").write('''package android.atl;

import android.content.res.Configuration;

/** Android TV device profile, enabled with ATL_IS_TELEVISION (any value). */
public final class ATLTv {
	public static final boolean TV = System.getenv("ATL_IS_TELEVISION") != null;

	private ATLTv() {}

	public static void applyConfig(Configuration c) {
		if (!TV || c == null)
			return;
		int night = c.uiMode & Configuration.UI_MODE_NIGHT_MASK;
		if (night == Configuration.UI_MODE_NIGHT_UNDEFINED && !"0".equals(System.getenv("ATL_TV_NIGHT")))
			night = Configuration.UI_MODE_NIGHT_YES; // TV UIs are dark; Android TV reports night mode
		c.uiMode = Configuration.UI_MODE_TYPE_TELEVISION | night;
		c.touchscreen = Configuration.TOUCHSCREEN_NOTOUCH;
		c.navigation = Configuration.NAVIGATION_DPAD;
		c.navigationHidden = Configuration.NAVIGATIONHIDDEN_NO;
		c.keyboard = Configuration.KEYBOARD_NOKEYS;
		c.keyboardHidden = Configuration.KEYBOARDHIDDEN_YES;
		c.hardKeyboardHidden = Configuration.HARDKEYBOARDHIDDEN_YES;
	}

	/** null = not decided here, use ATL's default */
	public static Boolean hasFeature(String name) {
		if (!TV)
			return null;
		switch (name) {
			case "android.software.leanback":
			case "android.hardware.type.television":
			case "android.software.live_tv":
			case "android.hardware.wifi":
			case "android.hardware.ethernet":
			case "android.hardware.audio.output":
			case "android.hardware.screen.landscape":
			case "android.software.webview":
			case "android.software.app_widgets":
			case "android.hardware.hdmi.cec":
				return Boolean.TRUE;
			case "android.hardware.touchscreen":
			case "android.hardware.touchscreen.multitouch":
			case "android.hardware.touchscreen.multitouch.distinct":
			case "android.hardware.touchscreen.multitouch.jazzhand":
			case "android.hardware.faketouch":
			case "android.hardware.screen.portrait":
			case "android.hardware.telephony":
			case "android.hardware.camera":
			case "android.hardware.camera.any":
			case "android.hardware.location.gps":
			case "android.hardware.sensor.accelerometer":
			case "android.hardware.microphone":
			case "android.software.leanback_only":
				return Boolean.FALSE;
			default:
				return null;
		}
	}
}
''')

edit("content/Context.java", """		sys_config = new Configuration();
		native_updateConfig(sys_config);
""", """		sys_config = new Configuration();
		native_updateConfig(sys_config);
		android.atl.ATLTv.applyConfig(sys_config);
""")

edit("content/pm/PackageManager.java", """	public boolean hasSystemFeature(String name) {
		switch (name) {""", """	public boolean hasSystemFeature(String name) {
		Boolean tv = android.atl.ATLTv.hasFeature(name);
		if (tv != null)
			return tv;
		switch (name) {""")

# leanback launcher first
edit("app/Activity.java", """		if (className == null) {
			for (PackageParser.Activity activity : ATLLoadedApp.getPrimaryApplication().pkg.activities) {""",
     """		if (className == null && uri == null && android.atl.ATLTv.TV) {
			for (PackageParser.Activity activity : ATLLoadedApp.getPrimaryApplication().pkg.activities) {
				if (!activity.info.enabled)
					continue;
				for (PackageParser.IntentInfo intent : activity.intents) {
					if (intent.hasCategory("android.intent.category.LEANBACK_LAUNCHER") && intent.hasAction("android.intent.action.MAIN")) {
						className = activity.info.targetActivity != null ? activity.info.targetActivity : activity.className;
						break;
					}
				}
				if (className != null)
					break;
			}
		}
		if (className == null) {
			for (PackageParser.Activity activity : ATLLoadedApp.getPrimaryApplication().pkg.activities) {""")
# the generic loop ran even when className was set by the leanback pass: guard its else branch
edit("app/Activity.java", """		} else {
			className = className.replace('/', '.');
		}
		if (className == null) {
			if (uri != null)""", """		} else if (className.indexOf('/') >= 0) {
			className = className.replace('/', '.');
		}
		if (className == null) {
			if (uri != null)""")

# Build properties
edit("os/Build.java", """	public static final String MANUFACTURER = "HTC" /* HTC picked at random; getString("ro.product.manufacturer")*/;""",
     """	public static final String MANUFACTURER = SystemProperties.get("ro.product.manufacturer", "HTC"); /* upstream default: HTC, picked at random */""")
edit("os/SystemProperties.java", """		properties.put("ro.build.type", "user");""", """		properties.put("ro.build.type", "user");
		if (System.getenv("ATL_IS_TELEVISION") != null) {
			properties.put("ro.product.manufacturer", "LGE");
			properties.put("ro.product.brand", "lge");
			properties.put("ro.product.model", "LG webOS TV");
			properties.put("ro.product.device", "webos_tv");
			properties.put("ro.product.name", "webos_tv");
			properties.put("ro.hardware", "webos");
			properties.put("ro.build.characteristics", "tv");
		}
		String extra = System.getenv("ATL_PROPS"); // "key=value;key=value"
		if (extra != null)
			for (String kv : extra.split(";")) {
				int i = kv.indexOf('=');
				if (i > 0)
					properties.put(kv.substring(0, i).trim(), kv.substring(i + 1));
			}""")

# SDK_INT default
edit("os/Build.java", """					SDK_INT_tmp = EarlyPackageParser.parseMinSdkInt(mainApk, SDK_INT_tmp);
					// Always ensure the SDK int is at least gingerbread for older applications.
					SDK_INT_tmp = Math.max(SDK_INT_tmp, Build.VERSION_CODES.GINGERBREAD);""",
     """					SDK_INT_tmp = EarlyPackageParser.parseMinSdkInt(mainApk, SDK_INT_tmp);
					// Always ensure the SDK int is at least gingerbread for older applications.
					SDK_INT_tmp = Math.max(SDK_INT_tmp, Build.VERSION_CODES.GINGERBREAD);
					String def = System.getenv("ATL_DEFAULT_SDK_INT"); // pretend at least this Android version
					if (def != null) {
						try {
							SDK_INT_tmp = Math.max(SDK_INT_tmp, Integer.parseInt(def.trim()));
						} catch (NumberFormatException e) {
						}
					}""")

# native: night-mode updates keep the ui mode type
p = os.path.join(J, "content/android_content_Context.c")
s = open(p).read()
for night in ("0x20", "0x10", "0x00"):
    s = s.replace(f'_SET_INT_FIELD(configuration, "uiMode", /*', f'_SET_INT_FIELD(configuration, "uiMode", (_GET_INT_FIELD(configuration, "uiMode") & 0x0f) | /*', 1)
open(p, "w").write(s)
assert s.count('(_GET_INT_FIELD(configuration, "uiMode") & 0x0f) |') == 3, "Context.c night-mode anchors changed"

# no activity slide animation on TV / when asked
p = os.path.join(R, "src/main-executable/main.c")
s = open(p).read()
old = "	gtk_stack_set_transition_type(GTK_STACK(stack), GTK_STACK_TRANSITION_TYPE_SLIDE_LEFT_RIGHT);"
new = ("	gtk_stack_set_transition_type(GTK_STACK(stack), (getenv(\"ATL_NO_ANIMATIONS\") || getenv(\"ATL_IS_TELEVISION\")) ? GTK_STACK_TRANSITION_TYPE_NONE\n"
       "	                                                                                                             : GTK_STACK_TRANSITION_TYPE_SLIDE_LEFT_RIGHT);")
if new not in s:
    assert old in s, "main.c stack transition anchor changed"
    s = s.replace(old, new, 1)
open(p, "w").write(s)

# register the new class with meson
p = os.path.join(R, "src/api-impl/meson.build")
s = open(p).read()
if "'android/atl/ATLTv.java'" not in s:
    anchor = "\t'android/atl/ATLToggles.java',"
    assert anchor in s, "meson anchor"
    s = s.replace(anchor, "\t'android/atl/ATLToggles.java',\n\t'android/atl/ATLTv.java',", 1)
    open(p, "w").write(s)
print("tvprofile: ok")
