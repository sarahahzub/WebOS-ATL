#!/usr/bin/env python3
"""View reads android:alpha from XML layouts (as on Android). ATL ignored it, so views declared transparent in their
layout were drawn fully opaque. CloudStream adds `extra_brightness_overlay` (a full-screen white View with
android:alpha="0", raised by its "extra brightness" gesture) into the player's content frame: under ATL it covered the
video with white (software and hardware decoding alike), with only the strip below it visible.
usage: view-xml-alpha.py <atl src root>"""
import os, sys
p = os.path.join(sys.argv[1], "src/api-impl/android/view/View.java")
s = open(p).read()
if "View_alpha" in s:
    sys.exit(0)
old = """		if (a.hasValue(com.android.internal.R.styleable.View_visibility)) {
			visibility = VISIBILITY_FLAGS[a.getInt(com.android.internal.R.styleable.View_visibility, 0)];
			native_setVisibility(widget, visibility, alpha, atl_enabled);
		}
"""
new = """		if (a.hasValue(com.android.internal.R.styleable.View_alpha)) // tv: XML android:alpha (CloudStream brightness overlay)
			alpha = Math.max(0f, Math.min(1f, a.getFloat(com.android.internal.R.styleable.View_alpha, 1f)));
		if (a.hasValue(com.android.internal.R.styleable.View_visibility))
			visibility = VISIBILITY_FLAGS[a.getInt(com.android.internal.R.styleable.View_visibility, 0)];
		if (a.hasValue(com.android.internal.R.styleable.View_visibility) || alpha != 1f)
			native_setVisibility(widget, visibility, alpha, atl_enabled);
"""
assert old in s, "anchor"
s = s.replace(old, new, 1)
open(p, "w").write(s)
