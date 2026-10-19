#!/usr/bin/env python3
"""android.net.Network is AOSP-like: a netId (100 for ATL's one network), toString() is the netId, equals/hashCode by
netId, getNetworkHandle() derived from it. Below API 23 Chromium/Cronet does Integer.parseInt(network.toString()), so
SmartTube (SDK 21) died with NumberFormatException "android.net.Network@..." and a fatal SIGTRAP in jni_android.cc.
usage: network-netid.py <atl src root>"""
import os, sys
p = os.path.join(sys.argv[1], "src/api-impl/android/net/Network.java")
s = open(p).read()
if "netId" in s:
    sys.exit(0)


def rep(old, new):
    global s
    assert old in s, old[:60]
    s = s.replace(old, new, 1)


rep("""public class Network {
	public long getNetworkHandle() {
		return 1L;
	}
}""", """public class Network {
	public final int netId;

	public Network() {
		this(100);
	}

	public Network(int netId) {
		this.netId = netId;
	}

	public long getNetworkHandle() {
		return ((long)netId << 32) | 0xfacadeL;
	}

	@Override
	public boolean equals(Object obj) {
		return obj instanceof Network && ((Network)obj).netId == netId;
	}

	@Override
	public int hashCode() {
		return netId * 11;
	}

	@Override
	public String toString() {
		return Integer.toString(netId);
	}
}""")
open(p, "w").write(s)
