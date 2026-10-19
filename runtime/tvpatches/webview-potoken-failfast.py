#!/usr/bin/env python3
"""WebView probes from poToken/BotGuard generators fail fast, as on an Android device without a WebView. ATL's WebView
never runs JavaScript, so SmartTube's PoTokenWebView waited forever on an unbounded CountDownLatch and every title
loaded endlessly. CookieManager.getInstance() now throws when a class among the first 10 stack frames names "potoken"
or "botguard" (generalises upstream's NewPipe check, which stays). Other apps' CookieManager use is unchanged;
ATL_UGLY_ENABLE_WEBVIEW turns the check off.
usage: webview-potoken-failfast.py <atl src root>"""
import os, sys
p = os.path.join(sys.argv[1], "src/api-impl/android/webkit/CookieManager.java")
s = open(p).read()
if "botguard" in s:
    sys.exit(0)


def rep(old, new):
    global s
    assert old in s, old[:60]
    s = s.replace(old, new, 1)


rep("""		} catch (ClassNotFoundException e) {
		}
		return new CookieManager();""", """		} catch (ClassNotFoundException e) {
		}
		// WebView probe of a poToken/BotGuard generator (SmartTube, NewPipe forks): fail as on a device without a
		// WebView; ATL's WebView runs no JavaScript, so the generator would wait forever
		if (System.getenv("ATL_UGLY_ENABLE_WEBVIEW") == null) {
			StackTraceElement[] st = new Throwable().getStackTrace();
			for (int i = 0; i < st.length && i < 10; i++) {
				String c = String.valueOf(st[i].getClassName()).toLowerCase();
				if (c.contains("potoken") || c.contains("botguard"))
					throw new RuntimeException("WebView not available (ATL)");
			}
		}
		return new CookieManager();""")
open(p, "w").write(s)
