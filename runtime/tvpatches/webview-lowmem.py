#!/usr/bin/env python3
"""Real WebView (ATL_UGLY_ENABLE_WEBVIEW) fits in the TV's memory. A Cloudflare Turnstile page (CloudStream DoraBash
"Cloudflare Bypass") grew WebKitWebProcess to 0.7-1.2 GB (WebGL/WebAudio fingerprinting + JSC JIT), so webOS
memchute closed the app after ~40 s and the dialog stayed white. Measured on the bench: WebGL/WebAudio/media off and
JSC JIT off = ~250 MB, and Turnstile still completes. So the WebView turns WebGL, WebAudio and media off, and the
WebKit processes start with JSC_useJIT=false (apk.conf ENV_JSC_useJIT=true overrides; ATL_WEBVIEW_FULL=1 keeps
WebKit's defaults).
usage: webview-lowmem.py <atl src root>"""
import os, sys
root = sys.argv[1]
p = os.path.join(root, "src/api-impl-jni/widgets/android_webkit_WebView.c")
s = open(p).read()
if "ATL_WEBVIEW_FULL" not in s:
    old = """	GtkWidget *wrapper = g_object_ref(wrapper_widget_new());
	GtkWidget *webview = webkit_web_view_new();
"""
    new = """	if (!getenv("ATL_WEBVIEW_FULL"))  // TV memory: JSC JIT off in the WebKit processes (inherited env)
		setenv("JSC_useJIT", "false", 0);
	GtkWidget *wrapper = g_object_ref(wrapper_widget_new());
	GtkWidget *webview = webkit_web_view_new();
	if (!getenv("ATL_WEBVIEW_FULL")) {  // WebGL/WebAudio/media fingerprinting made a Turnstile page take ~1 GB
		WebKitSettings *ws = webkit_web_view_get_settings(WEBKIT_WEB_VIEW(webview));
		webkit_settings_set_enable_webgl(ws, FALSE);
		webkit_settings_set_enable_webaudio(ws, FALSE);
		webkit_settings_set_enable_media(ws, FALSE);
	}
"""
    assert old in s
    s = s.replace(old, new, 1)
    open(p, "w").write(s)
