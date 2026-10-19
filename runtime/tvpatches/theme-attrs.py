#!/usr/bin/env python3
"""Theme attributes (?attr/...) inside drawables and colours resolve like on Android.
Upstream: StateListDrawable reads its <item>s with Resources.obtainAttributes (no theme), so a child
<shape android:color="?attr/x"> got theme=null and TypedArray.getColor/getDrawable threw
"Failed to resolve attribute" -> the whole layout failed to inflate (CloudStream's first-run language screen was
black: a ListView selector -> state list -> shape with ?attr colour).
Now: StateListDrawable/ShapeDrawable pass the theme (Drawable.obtainAttributes(r, theme, ...)), and TypedArray
resolves a leftover attribute through its theme; if that is impossible it logs and returns the default
(a missing colour is better than a failed screen).
usage: theme-attrs.py <atl src root>"""
import os, sys
R = os.path.join(sys.argv[1], "src/api-impl/android")


def edit(f, old, new, count=1):
    p = os.path.join(R, f)
    s = open(p).read()
    if new in s:
        return
    assert s.count(old) >= count, f + ": " + old[:70]
    open(p, "w").write(s.replace(old, new, count))


edit("graphics/drawable/StateListDrawable.java",
     "final TypedArray a = r.obtainAttributes(attrs, R.styleable.StateListDrawable);",
     "final TypedArray a = obtainAttributes(r, theme, attrs, R.styleable.StateListDrawable);")
edit("graphics/drawable/StateListDrawable.java",
     "			final TypedArray a = r.obtainAttributes(attrs,",
     "			final TypedArray a = obtainAttributes(r, theme, attrs,")

T = "content/res/TypedArray.java"
# helper
edit(T, """	public Drawable getDrawable(/*@StyleableRes*/ int index) {""", """	/* ATL: an attribute reference left in the data (no theme when it was obtained): resolve it through mTheme.
	 * Returns false (and logs once per attribute) if there is no theme or the theme does not define it. */
	private static java.util.Set<Integer> sUnresolved = new java.util.HashSet<>();
	private boolean resolveThemeAttr(int index, TypedValue value) {
		int attr = value.data;
		if (mTheme != null && mTheme.resolveAttribute(attr, value, true) && value.type != TypedValue.TYPE_ATTRIBUTE)
			return true;
		if (sUnresolved.add(attr))
			android.util.Log.w("TypedArray", "unresolved theme attribute 0x" + Integer.toHexString(attr) + " at index " + index
			                   + (mTheme == null ? " (no theme)" : "") + ": using the default");
		return false;
	}

	public Drawable getDrawable(/*@StyleableRes*/ int index) {""")
edit(T, """			if (value.type == TypedValue.TYPE_ATTRIBUTE) {
				throw new UnsupportedOperationException(
				    "Failed to resolve attribute at index " + index + ": " + value);
			}
			return mResources.loadDrawable(value, value.resourceId, mTheme);""", """			if (value.type == TypedValue.TYPE_ATTRIBUTE && !resolveThemeAttr(index, value))
				return null;
			if (value.type == TypedValue.TYPE_NULL)
				return null;
			return mResources.loadDrawable(value, value.resourceId, mTheme);""")
edit(T, """		} else if (type == TypedValue.TYPE_ATTRIBUTE) {
			final TypedValue value = mValue;
			getValueAt(index, value);
			throw new UnsupportedOperationException(
			    "Failed to resolve attribute at index " + index + ": " + value);
		}

		throw new UnsupportedOperationException("Can't convert to color: type=0x" + Integer.toHexString(type));""", """		} else if (type == TypedValue.TYPE_ATTRIBUTE) {
			final TypedValue value = mValue;
			getValueAt(index, value);
			if (!resolveThemeAttr(index / AssetManager.STYLE_NUM_ENTRIES, value))
				return defValue;
			if (value.type >= TypedValue.TYPE_FIRST_INT && value.type <= TypedValue.TYPE_LAST_INT)
				return value.data;
			if (value.type == TypedValue.TYPE_STRING || value.type == TypedValue.TYPE_REFERENCE) {
				ColorStateList csl = mResources.loadColorStateList(value, value.resourceId, mTheme);
				return csl != null ? csl.getDefaultColor() : defValue;
			}
			return defValue;
		}

		throw new UnsupportedOperationException("Can't convert to color: type=0x" + Integer.toHexString(type));""")
# getComplexColor / getColorStateList
edit(T, """			if (value.type == TypedValue.TYPE_ATTRIBUTE) {
				throw new UnsupportedOperationException(
				    "Failed to resolve attribute at index " + index + ": " + value);
			}
			return mResources.loadComplexColor(value, value.resourceId, mTheme);""", """			if (value.type == TypedValue.TYPE_ATTRIBUTE && !resolveThemeAttr(index, value))
				return null;
			if (value.type >= TypedValue.TYPE_FIRST_COLOR_INT && value.type <= TypedValue.TYPE_LAST_COLOR_INT)
				return ColorStateList.valueOf(value.data);
			return mResources.loadComplexColor(value, value.resourceId, mTheme);""")
edit(T, """			if (value.type == TypedValue.TYPE_ATTRIBUTE) {
				throw new UnsupportedOperationException(
				    "Failed to resolve attribute at index " + index + ": " + value);
			}
			return mResources.loadColorStateList(value, value.resourceId, mTheme);""", """			if (value.type == TypedValue.TYPE_ATTRIBUTE && !resolveThemeAttr(index, value))
				return null;
			if (value.type >= TypedValue.TYPE_FIRST_COLOR_INT && value.type <= TypedValue.TYPE_LAST_COLOR_INT)
				return ColorStateList.valueOf(value.data);
			return mResources.loadColorStateList(value, value.resourceId, mTheme);""")
print("theme-attrs: ok")
