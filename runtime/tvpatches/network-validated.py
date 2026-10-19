#!/usr/bin/env python3
"""Report the host network like a connected, validated Android network.
Apps (Titu Flix, OkHttp/Cronet/WorkManager users) decide "offline" from:
  caps.hasCapability(INTERNET) && caps.hasCapability(VALIDATED)   and/or   NetworkInfo.isAvailable()/isConnected()
Upstream getNetworkCapabilities() only sets INTERNET+NOT_METERED (no VALIDATED, NOT_SUSPENDED, ...) and
NetworkInfo.isAvailable() is always false -> apps showed "network not available" although requests work.
usage: network-validated.py <atl src root>"""
import os, sys
N = os.path.join(sys.argv[1], "src/api-impl/android/net")


def edit(f, old, new):
    p = os.path.join(N, f)
    s = open(p).read()
    if new in s:
        return
    assert old in s, f + ": " + old[:50]
    open(p, "w").write(s.replace(old, new, 1))


edit("ConnectivityManager.java", """		capabilities.addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET);
""", """		capabilities.addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET);
		// a default route = working connectivity: what Android calls VALIDATED (apps check it before every request)
		for (int c : new int[] {NetworkCapabilities.NET_CAPABILITY_VALIDATED, NetworkCapabilities.NET_CAPABILITY_NOT_SUSPENDED,
		                        NetworkCapabilities.NET_CAPABILITY_NOT_ROAMING, NetworkCapabilities.NET_CAPABILITY_NOT_CONGESTED,
		                        NetworkCapabilities.NET_CAPABILITY_FOREGROUND})
			capabilities.addCapability(c);
""")

p = os.path.join(N, "NetworkInfo.java")
s = open(p).read()
s = s.replace("	public enum DetailedState {}", """	public enum DetailedState {
		IDLE,
		SCANNING,
		CONNECTING,
		AUTHENTICATING,
		OBTAINING_IPADDR,
		CONNECTED,
		SUSPENDED,
		DISCONNECTING,
		DISCONNECTED,
		FAILED,
		BLOCKED,
		VERIFYING_POOR_LINK,
		CAPTIVE_PORTAL_CHECK
	}""", 1)
s = s.replace("""	public boolean isAvailable() {
		return false;
	}""", """	public boolean isAvailable() {
		return state == State.CONNECTED;
	}""", 1)
s = s.replace("""	public String getTypeName() {
		return "UNKNOWN";
	}""", """	public String getTypeName() {
		return state == State.CONNECTED ? "WIFI" : "UNKNOWN";
	}""", 1)
s = s.replace("""	public DetailedState getDetailedState() {
		return null;
	}""", """	public DetailedState getDetailedState() {
		return state == State.CONNECTED ? DetailedState.CONNECTED : DetailedState.DISCONNECTED;
	}""", 1)
assert "return state == State.CONNECTED;\n\t}" in s and "DetailedState.CONNECTED" in s
open(p, "w").write(s)
print("network-validated: ok")
