#!/usr/bin/env python3
"""ConnectivityManager callbacks that apps can actually use.
 - NetworkCallback was a non-static inner class: apps compiled against the SDK call `new NetworkCallback()` (no outer
   instance) -> NoSuchMethodError. Now `public static class` with the full SDK callback set.
 - register{,Default}NetworkCallback / requestNetwork deliver onAvailable(network) + onCapabilitiesChanged on the
   given handler (or main looper) when the host is online, instead of a null Network from native code (Kotlin apps
   abort on that) or nothing at all.
usage: connectivity.py <atl src root>"""
import os, re, sys
p = os.path.join(sys.argv[1], "src/api-impl/android/net/ConnectivityManager.java")
s = open(p).read()
if "deliverAvailable" in s:
    print("connectivity: already patched"); sys.exit(0)

s = s.replace("import android.os.Handler;", "import android.os.Handler;\nimport android.os.Looper;", 1)
s = re.sub(r"\tpublic class NetworkCallback \{.*?\n\t\}\n", """	public static class NetworkCallback {
		public NetworkCallback() {}
		public NetworkCallback(int flags) {}
		public void onPreCheck(Network network) {}
		public void onAvailable(Network network) {}
		public void onLosing(Network network, int maxMsToLive) {}
		public void onLost(Network network) {}
		public void onUnavailable() {}
		public void onCapabilitiesChanged(Network network, NetworkCapabilities networkCapabilities) {}
		public void onLinkPropertiesChanged(Network network, LinkProperties linkProperties) {}
		public void onBlockedStatusChanged(Network network, boolean blocked) {}
	}
""", s, count=1, flags=re.S)
assert "public static class NetworkCallback" in s

# drop every existing register*/request* variant, then add ours
s = re.sub(r"\tpublic (native )?void registerNetworkCallback\(NetworkRequest request, NetworkCallback callback\)[^\n]*\n", "", s)
s = re.sub(r"\tpublic void registerDefaultNetworkCallback\(NetworkCallback cb(, Handler hdl)?\) \{\}\n\n?", "", s)
i = s.rindex("}")
s = s[:i] + """
	public void registerNetworkCallback(NetworkRequest request, NetworkCallback callback) { deliverAvailable(callback, null); }

	public void registerNetworkCallback(NetworkRequest request, NetworkCallback callback, Handler handler) { deliverAvailable(callback, handler); }

	public void registerDefaultNetworkCallback(NetworkCallback callback) { deliverAvailable(callback, null); }

	public void registerDefaultNetworkCallback(NetworkCallback callback, Handler handler) { deliverAvailable(callback, handler); }

	public void registerBestMatchingNetworkCallback(NetworkRequest request, NetworkCallback callback, Handler handler) { deliverAvailable(callback, handler); }

	public void requestNetwork(NetworkRequest request, NetworkCallback callback) { deliverAvailable(callback, null); }

	public void requestNetwork(NetworkRequest request, NetworkCallback callback, Handler handler) { deliverAvailable(callback, handler); }

	public void requestNetwork(NetworkRequest request, NetworkCallback callback, int timeoutMs) { deliverAvailable(callback, null); }

	public void requestNetwork(NetworkRequest request, NetworkCallback callback, Handler handler, int timeoutMs) { deliverAvailable(callback, handler); }

	public void releaseNetworkRequest(android.app.PendingIntent operation) {}

	/* One network (the host's), always up while the host has a default route: announce it like Android does right
	   after registration, asynchronously on the callback's handler. */
	private void deliverAvailable(final NetworkCallback callback, Handler handler) {
		if (callback == null)
			return;
		final Network network = getActiveNetwork();
		final boolean online = hostOnline();
		(handler != null ? handler : new Handler(Looper.getMainLooper())).post(new Runnable() {
			@Override
			public void run() {
				if (online) {
					callback.onAvailable(network);
					NetworkCapabilities caps = getNetworkCapabilities(network);
					if (caps != null)
						callback.onCapabilitiesChanged(network, caps);
					callback.onBlockedStatusChanged(network, false);
				} else {
					callback.onUnavailable();
				}
			}
		});
	}
""" + s[i:]
open(p, "w").write(s)
print("connectivity: ok")
