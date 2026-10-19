#!/usr/bin/env python3
"""The real (WebKitGTK) WebView works for in-app challenge pages (Cloudflare/Turnstile captchas). ATL's WebView is a
blank placeholder unless ATL_UGLY_ENABLE_WEBVIEW is set (per app: ENV_ATL_UGLY_ENABLE_WEBVIEW=1 in apk.conf). With it
set, WebKitGTK already rendered and ran the challenge, but the APIs apps use to see the result were stubs, so e.g.
CloudStream's DoraBash "Cloudflare Bypass" dialog waited forever. Now, when the WebView is real:
- WebView.getTitle()/getUrl() return WebKit's title/URI;
- getSettings() is one object per WebView; get/setUserAgentString() read/write WebKit's user agent (cf_clearance is
  bound to the UA the app saves and reuses for its own HTTP requests);
- CookieManager.getCookie(url) returns the cookies of WebKit's default network session ("a=1; b=2", synchronous, also
  from non-UI threads, 3 s timeout); setCookie(url, value) adds/expires a cookie there.
Without ATL_UGLY_ENABLE_WEBVIEW nothing changes (getCookie "" as before, getTitle null).
usage: webview-real.py <atl src root>"""
import os, sys
root = sys.argv[1]


def patch(rel, marker, reps):
    p = os.path.join(root, rel)
    s = open(p).read()
    if marker in s:
        return
    for old, new in reps:
        assert old in s, (rel, old[:60])
        s = s.replace(old, new, 1)
    open(p, "w").write(s)


patch("src/api-impl/android/webkit/WebView.java", "native_getTitle", [
    ("""	private WebViewClient webViewClient;
""", """	private WebViewClient webViewClient;
	private WebSettings settings;
	// WebKitGTK widget behind this view (native code returns a plain View widget without ATL_UGLY_ENABLE_WEBVIEW)
	static final boolean REAL = System.getenv("ATL_UGLY_ENABLE_WEBVIEW") != null;
"""),
    ("""	public WebSettings getSettings() {
		return new WebSettings();
	}
""", """	public WebSettings getSettings() {
		if (settings == null) {
			settings = new WebSettings();
			settings.webview = REAL ? widget : 0;
		}
		return settings;
	}

	public String getTitle() {
		return REAL ? native_getTitle(widget) : null;
	}

	public String getUrl() {
		return REAL ? native_getUrl(widget) : null;
	}
"""),
    ("""	private native void native_loadUrl(long widget, String url);
""", """	private native void native_loadUrl(long widget, String url);
	private static native String native_getTitle(long widget);
	private static native String native_getUrl(long widget);
	static native String native_getUserAgent(long widget);
	static native void native_setUserAgent(long widget, String ua);
"""),
])

patch("src/api-impl/android/webkit/WebSettings.java", "native_getUserAgent", [
    ("""public class WebSettings {
""", """public class WebSettings {

	long webview; // WebKitWebView of the owning WebView, 0 if not a real one
"""),
    ("""	public String getUserAgentString() {
		return "GDPR VIOLATION";
	}

	public void setUserAgentString(String userAgentString) {}
""", """	public String getUserAgentString() {
		if (webview != 0)
			return WebView.native_getUserAgent(webview);
		return "GDPR VIOLATION";
	}

	public void setUserAgentString(String userAgentString) {
		if (webview != 0)
			WebView.native_setUserAgent(webview, userAgentString);
	}
"""),
])

patch("src/api-impl/android/webkit/CookieManager.java", "native_getCookie", [
    ("""	public String getCookie(String url) {
		return "";
	}

	public void setCookie(String url, String value) {}
""", """	public String getCookie(String url) {
		if (WebView.REAL && url != null) {
			String c = native_getCookie(url.contains("://") ? url : "https://" + url);
			return c == null || c.isEmpty() ? null : c;
		}
		return "";
	}

	public void setCookie(String url, String value) {
		if (WebView.REAL && url != null && value != null)
			native_setCookie(url.contains("://") ? url : "https://" + url, value);
	}

	private static native String native_getCookie(String url);
	private static native void native_setCookie(String url, String value);
"""),
])

patch("src/api-impl-jni/widgets/android_webkit_WebView.c", "native_1getCookie", [
    ("""JNIEXPORT void JNICALL Java_android_webkit_WebView_native_1loadUrl(""", r"""JNIEXPORT jstring JNICALL Java_android_webkit_WebView_native_1getTitle(JNIEnv *env, jclass cls, jlong widget_ptr)
{
	const char *t = webkit_web_view_get_title(WEBKIT_WEB_VIEW(_PTR(widget_ptr)));
	return t ? _JSTRING(t) : NULL;
}

JNIEXPORT jstring JNICALL Java_android_webkit_WebView_native_1getUrl(JNIEnv *env, jclass cls, jlong widget_ptr)
{
	const char *u = webkit_web_view_get_uri(WEBKIT_WEB_VIEW(_PTR(widget_ptr)));
	return u ? _JSTRING(u) : NULL;
}

JNIEXPORT jstring JNICALL Java_android_webkit_WebView_native_1getUserAgent(JNIEnv *env, jclass cls, jlong widget_ptr)
{
	const char *ua = webkit_settings_get_user_agent(webkit_web_view_get_settings(WEBKIT_WEB_VIEW(_PTR(widget_ptr))));
	return ua ? _JSTRING(ua) : NULL;
}

JNIEXPORT void JNICALL Java_android_webkit_WebView_native_1setUserAgent(JNIEnv *env, jclass cls, jlong widget_ptr, jstring ua)
{
	webkit_settings_set_user_agent(webkit_web_view_get_settings(WEBKIT_WEB_VIEW(_PTR(widget_ptr))), ua ? _CSTRING(ua) : NULL);
}

/* CookieManager: WebKit's cookie API is async and main-thread only; Android's getCookie is synchronous and may be called
 * from any thread. On the main thread spin the default context, elsewhere hand the request over and wait. */
struct cookie_req {
	char *uri;
	char *value; // setCookie: cookie string, NULL for getCookie
	GString *out;
	gboolean done;
	GMutex lock;
	GCond cond;
};

static void cookie_req_finish(struct cookie_req *r)
{
	g_mutex_lock(&r->lock);
	r->done = TRUE;
	g_cond_broadcast(&r->cond);
	g_mutex_unlock(&r->lock);
	g_main_context_wakeup(NULL);
}

static void got_cookies(GObject *src, GAsyncResult *res, gpointer data)
{
	struct cookie_req *r = data;
	GList *list = webkit_cookie_manager_get_cookies_finish(WEBKIT_COOKIE_MANAGER(src), res, NULL);
	for (GList *l = list; l; l = l->next) {
		SoupCookie *c = l->data;
		g_string_append_printf(r->out, "%s%s=%s", r->out->len ? "; " : "", soup_cookie_get_name(c), soup_cookie_get_value(c));
	}
	g_list_free_full(list, (GDestroyNotify)soup_cookie_free);
	cookie_req_finish(r);
}

static void added_cookie(GObject *src, GAsyncResult *res, gpointer data)
{
	webkit_cookie_manager_add_cookie_finish(WEBKIT_COOKIE_MANAGER(src), res, NULL);
	cookie_req_finish(data);
}

static gboolean cookie_req_start(gpointer data)
{
	struct cookie_req *r = data;
	WebKitCookieManager *cm = webkit_network_session_get_cookie_manager(webkit_network_session_get_default());
	if (!r->value) {
		webkit_cookie_manager_get_cookies(cm, r->uri, NULL, got_cookies, r);
		return G_SOURCE_REMOVE;
	}
	GUri *guri = g_uri_parse(r->uri, G_URI_FLAGS_NONE, NULL);
	SoupCookie *c = guri ? soup_cookie_parse(r->value, guri) : NULL;
	if (guri)
		g_uri_unref(guri);
	if (!c) {
		cookie_req_finish(r);
		return G_SOURCE_REMOVE;
	}
	webkit_cookie_manager_add_cookie(cm, c, NULL, added_cookie, r);
	soup_cookie_free(c);
	return G_SOURCE_REMOVE;
}

static gboolean cookie_req_timeout(gpointer data)
{
	return G_SOURCE_REMOVE; // only wakes up g_main_context_iteration
}

static char *cookie_req_run(const char *uri, const char *value)
{
	struct cookie_req *r = g_new0(struct cookie_req, 1);
	r->uri = g_strdup(uri);
	r->value = g_strdup(value);
	r->out = g_string_new(NULL);
	g_mutex_init(&r->lock);
	g_cond_init(&r->cond);
	gint64 end = g_get_monotonic_time() + 3 * G_USEC_PER_SEC;
	gboolean ok;
	if (g_main_context_is_owner(g_main_context_default())) {
		cookie_req_start(r);
		guint t = g_timeout_add(100, cookie_req_timeout, NULL);
		while (!r->done && g_get_monotonic_time() < end) {
			g_source_remove(t);
			t = g_timeout_add(100, cookie_req_timeout, NULL);
			g_main_context_iteration(NULL, TRUE);
		}
		g_source_remove(t);
		ok = r->done;
	} else {
		g_main_context_invoke(NULL, cookie_req_start, r);
		g_mutex_lock(&r->lock);
		while (!r->done && g_cond_wait_until(&r->cond, &r->lock, end))
			;
		ok = r->done;
		g_mutex_unlock(&r->lock);
	}
	if (!ok) // the callback still owns r: leak it rather than free it under the callback
		return NULL;
	char *s = g_string_free(r->out, FALSE);
	g_free(r->uri);
	g_free(r->value);
	g_mutex_clear(&r->lock);
	g_cond_clear(&r->cond);
	g_free(r);
	return s;
}

JNIEXPORT jstring JNICALL Java_android_webkit_CookieManager_native_1getCookie(JNIEnv *env, jclass cls, jstring url)
{
	char *s = cookie_req_run(_CSTRING(url), NULL);
	jstring ret = s ? _JSTRING(s) : NULL;
	g_free(s);
	return ret;
}

JNIEXPORT void JNICALL Java_android_webkit_CookieManager_native_1setCookie(JNIEnv *env, jclass cls, jstring url, jstring value)
{
	g_free(cookie_req_run(_CSTRING(url), _CSTRING(value)));
}

JNIEXPORT void JNICALL Java_android_webkit_WebView_native_1loadUrl("""),
])
