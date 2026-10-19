#!/usr/bin/env python3
"""Every Context.getSystemService() name/class returns an object instead of null.
ATL implements ~30 managers by hand; for every other SDK service (storagestats, media_session, telecom, nsd, shortcut,
autofill, ...) apps got null and crashed with NPEs. Now the SDK class is instantiated (no-arg ctor, else
Unsafe.allocateInstance - the class's methods are StubGen safe defaults), cached per process.
getSystemService(Class) also no longer dies on classes without public constructors.
usage: services.py <atl src root>"""
import os, sys
R = sys.argv[1]
A = os.path.join(R, "src/api-impl/android")

SERVICES = {
    "accessibility": "android.view.accessibility.AccessibilityManager", "account": "android.accounts.AccountManager",
    "activity": "android.app.ActivityManager", "alarm": "android.app.AlarmManager", "appops": "android.app.AppOpsManager",
    "appwidget": "android.appwidget.AppWidgetManager", "audio": "android.media.AudioManager", "autofill": "android.view.autofill.AutofillManager",
    "batterymanager": "android.os.BatteryManager", "biometric": "android.hardware.biometrics.BiometricManager",
    "blob_store": "android.app.blob.BlobStoreManager", "bluetooth": "android.bluetooth.BluetoothManager",
    "camera": "android.hardware.camera2.CameraManager", "captioning": "android.view.accessibility.CaptioningManager",
    "carrier_config": "android.telephony.CarrierConfigManager", "clipboard": "android.content.ClipboardManager",
    "companiondevice": "android.companion.CompanionDeviceManager", "connectivity": "android.net.ConnectivityManager",
    "consumer_ir": "android.hardware.ConsumerIrManager", "credential": "android.credentials.CredentialManager",
    "crossprofileapps": "android.content.pm.CrossProfileApps", "device_policy": "android.app.admin.DevicePolicyManager",
    "display": "android.hardware.display.DisplayManager", "domain_verification": "android.content.pm.verify.domain.DomainVerificationManager",
    "download": "android.app.DownloadManager", "dropbox": "android.os.DropBoxManager", "euicc": "android.telephony.euicc.EuiccManager",
    "file_integrity": "android.security.FileIntegrityManager", "fingerprint": "android.hardware.fingerprint.FingerprintManager",
    "game": "android.app.GameManager", "grammatical_inflection": "android.app.GrammaticalInflectionManager",
    "hardware_properties": "android.os.HardwarePropertiesManager", "health_connect": "android.health.connect.HealthConnectManager",
    "input": "android.hardware.input.InputManager", "input_method": "android.view.inputmethod.InputMethodManager",
    "ipsec": "android.net.IpSecManager", "jobscheduler": "android.app.job.JobScheduler", "keyguard": "android.app.KeyguardManager",
    "launcherapps": "android.content.pm.LauncherApps", "layout_inflater": "android.view.LayoutInflater", "locale": "android.app.LocaleManager",
    "location": "android.location.LocationManager", "media_communication": "android.media.MediaCommunicationManager",
    "media_metrics": "android.media.metrics.MediaMetricsManager", "media_projection": "android.media.projection.MediaProjectionManager",
    "media_router": "android.media.MediaRouter", "media_session": "android.media.session.MediaSessionManager",
    "midi": "android.media.midi.MidiManager", "netstats": "android.app.usage.NetworkStatsManager", "nfc": "android.nfc.NfcManager",
    "notification": "android.app.NotificationManager", "overlay": "android.content.om.OverlayManager",
    "persistent_data_block": "android.service.persistentdata.PersistentDataBlockManager", "phone": "android.telephony.TelephonyManager",
    "power": "android.os.PowerManager", "print": "android.print.PrintManager", "restrictions": "android.content.RestrictionsManager",
    "role": "android.app.role.RoleManager", "search": "android.app.SearchManager", "security_state": "android.os.SecurityStateManager",
    "sensor": "android.hardware.SensorManager", "shortcut": "android.content.pm.ShortcutManager", "statusbar": "android.app.StatusBarManager",
    "storage": "android.os.storage.StorageManager", "storagestats": "android.app.usage.StorageStatsManager",
    "system_health": "android.os.health.SystemHealthManager", "telecom": "android.telecom.TelecomManager",
    "telephony_subscription_service": "android.telephony.SubscriptionManager", "textclassification": "android.view.textclassifier.TextClassificationManager",
    "textservices": "android.view.textservice.TextServicesManager", "tv_input": "android.media.tv.TvInputManager",
    "tv_interactive_app": "android.media.tv.interactive.TvInteractiveAppManager", "uimode": "android.app.UiModeManager",
    "usagestats": "android.app.usage.UsageStatsManager", "usb": "android.hardware.usb.UsbManager", "user": "android.os.UserManager",
    "vibrator": "android.os.Vibrator", "vibrator_manager": "android.os.VibratorManager", "virtualdevice": "android.companion.virtual.VirtualDeviceManager",
    "vpn_management": "android.net.VpnManager", "wallpaper": "android.app.WallpaperManager", "wifi": "android.net.wifi.WifiManager",
    "wifiaware": "android.net.wifi.aware.WifiAwareManager", "wifip2p": "android.net.wifi.p2p.WifiP2pManager",
    "wifirtt": "android.net.wifi.rtt.WifiRttManager", "window": "android.view.WindowManager", "nsd": "android.net.nsd.NsdManager",
    "translation": "android.view.translation.TranslationManager", "uwb": "android.uwb.UwbManager",
    "contexthub": "android.hardware.location.ContextHubManager", "app_hibernation": "android.apphibernation.AppHibernationManager",
    "performance_hint": "android.os.PerformanceHintManager", "people": "android.app.people.PeopleManager",
    "keystore": "android.security.keystore.KeyStoreManager", "wearable_sensing": "android.app.wearable.WearableSensingManager",
}

entries = "\n".join(f'\t\tm.put("{k}", "{v}");' for k, v in sorted(SERVICES.items()))
open(os.path.join(A, "atl/ATLServices.java"), "w").write('''package android.atl;

import android.util.Slog;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.HashMap;
import java.util.Map;

/** Fallback for system services ATL does not implement by hand: an instance of the SDK class (StubGen defaults). */
public final class ATLServices {
	private static final String TAG = "ATLServices";
	private static final Map<String, String> names = new HashMap<>();
	private static final Map<String, Object> cache = new HashMap<>();
	private static Object unsafe;
	private static Method allocateInstance;

	static {
		Map<String, String> m = names;
''' + entries + '''
	}

	private ATLServices() {}

	public static String classNameFor(String service) {
		return names.get(service);
	}

	public static String serviceNameFor(Class<?> cls) {
		for (Map.Entry<String, String> e : names.entrySet())
			if (e.getValue().equals(cls.getName()))
				return e.getKey();
		return null;
	}

	public static synchronized Object forName(String service) {
		String cn = names.get(service);
		if (cn == null)
			return null;
		Object o = cache.get(cn);
		if (o != null)
			return o;
		try {
			o = forClass(Class.forName(cn));
		} catch (ClassNotFoundException e) {
			Slog.w(TAG, "no class " + cn + " for service " + service);
			return null;
		}
		return o;
	}

	public static synchronized Object forClass(Class<?> cls) {
		Object o = cache.get(cls.getName());
		if (o != null)
			return o;
		try {
			Constructor<?> c = cls.getDeclaredConstructor();
			c.setAccessible(true);
			o = c.newInstance();
		} catch (Throwable t) {
			o = allocate(cls);
		}
		if (o != null) {
			cache.put(cls.getName(), o);
			Slog.i(TAG, "stub system service " + cls.getName());
		}
		return o;
	}

	private static Object allocate(Class<?> cls) {
		try {
			if (allocateInstance == null) {
				Class<?> u = Class.forName("sun.misc.Unsafe");
				Field f;
				try {
					f = u.getDeclaredField("theUnsafe");
				} catch (NoSuchFieldException e) {
					f = u.getDeclaredField("THE_ONE");
				}
				f.setAccessible(true);
				unsafe = f.get(null);
				allocateInstance = u.getMethod("allocateInstance", Class.class);
			}
			return allocateInstance.invoke(unsafe, cls);
		} catch (Throwable t) {
			Slog.w(TAG, "cannot instantiate " + cls.getName() + ": " + t);
			return null;
		}
	}
}
''')

p = os.path.join(A, "app/ContextImpl.java")
s = open(p).read()
old = """			default:
				Slog.e(TAG, "!!!!!!! getSystemService: case >" + name + "< is not implemented yet");
				return null;"""
assert old in s
s = s.replace(old, """			default:
				Object stub = android.atl.ATLServices.forName(name);
				if (stub == null)
					Slog.e(TAG, "!!!!!!! getSystemService: case >" + name + "< is not implemented yet");
				return stub;""", 1)
old = "		return serviceClass.getConstructors()[0].newInstance();"
assert old in s
s = s.replace(old, """		String name = android.atl.ATLServices.serviceNameFor(serviceClass);
		if (name != null) {
			Object o = getSystemService(name);
			if (o != null && serviceClass.isInstance(o))
				return o;
		}
		if (serviceClass.getConstructors().length > 0) {
			try {
				return serviceClass.getConstructors()[0].newInstance();
			} catch (ReflectiveOperationException | IllegalArgumentException e) {
			}
		}
		return android.atl.ATLServices.forClass(serviceClass);""", 1)
if "getSystemServiceName(Class" not in s:
    i = s.index("	@Override\n	public Object getSystemService(Class<?> serviceClass)")
    s = s[:i] + """	public String getSystemServiceName(Class<?> serviceClass) {
		return android.atl.ATLServices.serviceNameFor(serviceClass);
	}

""" + s[i:]
open(p, "w").write(s)

p = os.path.join(R, "src/api-impl/meson.build")
s = open(p).read()
if "'android/atl/ATLServices.java'" not in s:
    anchor = "\t'android/atl/ATLSigHelper.java',"
    assert anchor in s
    s = s.replace(anchor, "\t'android/atl/ATLServices.java',\n" + anchor, 1)
    open(p, "w").write(s)
print("services: ok")
