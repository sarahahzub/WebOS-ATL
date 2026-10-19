#!/usr/bin/env python3
"""wolfJSSE: drop protocols this wolfSSL build lacks instead of throwing, like Android's Conscrypt.
Apps still call SSLSocket.setEnabledProtocols({"TLSv1", "TLSv1.1", "TLSv1.2"}) (SmartTube's Tls12SocketFactory,
old OkHttp ConnectionSpecs); wolfSSL is built without TLS 1.0/1.1, so WolfSSLEngineHelper.setProtocols threw
"IllegalArgumentException: Unsupported protocol: TLSv1" and every request failed (SmartTube: empty rows + error).
Now known-but-unavailable names are ignored; unknown names, or nothing usable left, still throw.
usage: wolfssl-protocols.py <art_standalone src root | dir containing it>   (~/atl2/src/art_standalone)"""
import os, sys
REL = "external/wolfssljni/src/java/com/wolfssl/provider/jsse/WolfSSLEngineHelper.java"
root = sys.argv[1]
cands = [os.path.join(root, REL), os.path.join(root, "art_standalone", REL), os.path.join(root, "src/art_standalone", REL)]
p = next((c for c in cands if os.path.exists(c)), None)
assert p, "WolfSSLEngineHelper.java not found under " + root
s = open(p).read()
OLD = """        /* sanitize protocol array for unsupported strings */
        List<String> supported = Arrays.asList(getAllProtocols());
        for (int i = 0; i < p.length; i++) {
            if (!supported.contains(p[i])) {
                throw new IllegalArgumentException("Unsupported protocol: " +
                    p[i]);
            }
        }
"""
NEW = """        /* sanitize protocol array: like Conscrypt, silently drop known
         * protocols this wolfSSL build lacks (apps still ask for TLSv1) */
        List<String> supported = Arrays.asList(getAllProtocols());
        List<String> known = Arrays.asList("SSLv2Hello", "SSLv3", "TLSv1",
            "TLSv1.1", "TLSv1.2", "TLSv1.3");
        java.util.ArrayList<String> usable = new java.util.ArrayList<String>();
        for (int i = 0; i < p.length; i++) {
            if (supported.contains(p[i])) {
                if (!usable.contains(p[i]))
                    usable.add(p[i]);
            } else if (!known.contains(p[i])) {
                throw new IllegalArgumentException("Unsupported protocol: " +
                    p[i]);
            }
        }
        if (usable.isEmpty()) {
            throw new IllegalArgumentException("Unsupported protocol: " +
                p[0]);
        }
        p = usable.toArray(new String[0]);
"""
if NEW not in s:
    assert OLD in s, p + ": setProtocols sanitize loop"
    s = s.replace(OLD, NEW, 1)
    open(p, "w").write(s)
print("wolfssl-protocols: ok")
