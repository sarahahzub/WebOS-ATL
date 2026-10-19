#!/usr/bin/env python3
"""Toasts are visible on the TV: Toast.show() posts a webOS system toast (luna com.webos.notification/createToast).
Upstream show() only posts a libnotify notification over D-Bus, which webOS doesn't have, so every toast was invisible
(CloudStream "Add repository": "Invalid URL" / "No repository found" looked like a dead button). Also real
setText/setDuration/getView, and the text of custom-view toasts (Toast(ctx) + setView, CloudStream's showToast) is taken
from the view's TextViews. ATL runs as root on the TV, so it runs /usr/bin/luna-send itself (clean environment: the TV
userland is 32-bit, ATL's LD_* must not leak). ATL_TOAST=0: old behaviour only.
usage: toast.py <atl src root>"""
import os, sys
p = os.path.join(sys.argv[1], "src/api-impl/android/widget/Toast.java")
s = open(p).read()
if "createToast" in s:
    sys.exit(0)


def rep(old, new):
    global s
    assert old in s, old[:60]
    s = s.replace(old, new, 1)


rep("""	private String text;
""", """	private String text;
	private int duration;
	private View view;
""")
rep("""	public void show() {
		System.out.println("showing toast: " + text);""", """	public void setText(CharSequence s) { text = String.valueOf(s); }

	public void setText(int resId) { text = mContext.getString(resId); }

	public void setDuration(int d) { duration = d; }

	public int getDuration() { return duration; }

	public View getView() { return view; }

	private static void collect(View v, StringBuilder sb) {
		if (v == null || v.getVisibility() != View.VISIBLE)
			return;
		if (v instanceof TextView) {
			CharSequence t = ((TextView)v).getText();
			if (t != null && t.length() > 0)
				sb.append(sb.length() > 0 ? "\\n" : "").append(t);
		} else if (v instanceof android.view.ViewGroup) {
			android.view.ViewGroup g = (android.view.ViewGroup)v;
			for (int i = 0; i < g.getChildCount(); i++)
				collect(g.getChildAt(i), sb);
		}
	}

	private static String json(String t) {
		StringBuilder b = new StringBuilder("\\"");
		for (char c : t.toCharArray()) {
			if (c == '"' || c == '\\\\') b.append('\\\\').append(c);
			else if (c < 0x20) b.append(String.format("\\\\u%04x", (int)c));
			else b.append(c);
		}
		return b.append('"').toString();
	}

	/* webOS system toast (the TV has no notification daemon) */
	private static void tvToast(final String t) {
		if ("0".equals(System.getenv("ATL_TOAST")) || !new java.io.File("/usr/bin/luna-send").exists())
			return;
		new Thread(new Runnable() { public void run() {
			try {
				ProcessBuilder pb = new ProcessBuilder("/usr/bin/luna-send", "-n", "1", "luna://com.webos.notification/createToast",
				                                       "{\\"sourceId\\":\\"tld.ghar.atl\\",\\"message\\":" + json(t.length() > 300 ? t.substring(0, 300) : t) + "}");
				pb.environment().clear();
				pb.environment().put("PATH", "/usr/bin:/bin:/usr/sbin:/sbin");
				pb.redirectErrorStream(true);
				Process pr = pb.start();
				pr.getInputStream().close();
				pr.waitFor();
			} catch (Exception e) {
				System.out.println("toast: luna-send failed: " + e);
			}
		}}, "atl-toast").start();
	}

	public void show() {
		String t = text;
		if ((t == null || t.equals("null")) && view != null) {
			StringBuilder sb = new StringBuilder();
			collect(view, sb);
			t = sb.length() > 0 ? sb.toString() : null;
		}
		System.out.println("showing toast: " + t);
		if (t != null)
			tvToast(t);""")
rep("""	public void setView(View view) {}""", """	public void setView(View view) { this.view = view; }""")
open(p, "w").write(s)
