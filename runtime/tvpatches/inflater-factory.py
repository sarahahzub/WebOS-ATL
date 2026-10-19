#!/usr/bin/env python3
"""LayoutInflater factories behave like Android's.
Upstream: LayoutInflater.Factory is an empty interface and is never consulted, Factory2 does not extend it, and the
Activity is only asked through its 4-argument onCreateView(parent, name, ctx, attrs), which returns null; the
3-argument Activity.onCreateView(name, ctx, attrs) is never called. Android calls the Activity as the inflater's
"private factory" for every view, and Activity.onCreateView(parent, ...) forwards to onCreateView(name, ...).
Apps that do work in an onCreateView override (CloudStream initialises its view models there) crashed
("lateinit property viewModel has not been initialized"); Factory-only libraries were ignored.
Now: factories (Factory2) -> legacy Factory -> private factory (the Activity behind the inflater's context, also
through ContextWrappers) -> built-in classes, and Activity.onCreateView(parent, ...) -> onCreateView(name, ...).
usage: inflater-factory.py <atl src root>"""
import os, sys
R = os.path.join(sys.argv[1], "src/api-impl/android")


def edit(f, old, new):
    p = os.path.join(R, f)
    s = open(p).read()
    if new in s:
        return
    assert old in s, f + ": " + old[:60]
    open(p, "w").write(s.replace(old, new, 1))


edit("view/LayoutInflater.java", """	public interface Factory {
	}

	public interface Factory2 {
		public View onCreateView(View parent, String name, Context context, AttributeSet attrs);
	}
""", """	public interface Factory {
		public View onCreateView(String name, Context context, AttributeSet attrs);
	}

	public interface Factory2 extends Factory {
		public View onCreateView(View parent, String name, Context context, AttributeSet attrs);
	}

	private Factory2 mPrivateFactory;

	/* hidden API in Android: the Activity is the private factory of its window's inflater */
	public void setPrivateFactory(Factory2 factory) {
		mPrivateFactory = factory;
	}

	private Factory2 privateFactory() {
		if (mPrivateFactory != null)
			return mPrivateFactory;
		Context c = context;
		for (int i = 0; c != null && i < 16; i++) {
			if (c instanceof android.app.Activity)
				return (Factory2)c;
			if (!(c instanceof android.content.ContextWrapper))
				break;
			c = ((android.content.ContextWrapper)c).getBaseContext();
		}
		return context instanceof Factory2 ? (Factory2)context : null;
	}
""")
edit("view/LayoutInflater.java", """		this.factories.addAll(original.factories);
		this.mFactory = original.mFactory;
		this.mFactory2 = original.mFactory2;
""", """		this.factories.addAll(original.factories);
		this.mFactory = original.mFactory;
		this.mFactory2 = original.mFactory2;
		this.mPrivateFactory = original.mPrivateFactory;
""")
edit("view/LayoutInflater.java", """		inflater.mFactory = mFactory;
		inflater.mFactory2 = mFactory2;
		return inflater;""", """		inflater.mFactory = mFactory;
		inflater.mFactory2 = mFactory2;
		inflater.mPrivateFactory = mPrivateFactory;
		return inflater;""")
edit("view/LayoutInflater.java", """		if (view == null && context instanceof Factory2) {
			view = ((Factory2)context).onCreateView(parent, name, context, attrs);
		}""", """		if (view == null && mFactory != null && !(mFactory instanceof Factory2 && factories.contains(mFactory))) {
			view = mFactory.onCreateView(name, context, attrs);
		}
		if (view == null) {
			Factory2 pf = privateFactory();
			if (pf != null)
				view = pf.onCreateView(parent, name, context, attrs);
		}""")

edit("app/Activity.java", """	public View onCreateView(View parent, String name, Context context, AttributeSet attrs) {
		return null;
	}""", """	public View onCreateView(View parent, String name, Context context, AttributeSet attrs) {
		if (!"fragment".equals(name))
			return onCreateView(name, context, attrs);
		return null;
	}

	public View onCreateView(String name, Context context, AttributeSet attrs) {
		return null;
	}""")
print("inflater-factory: ok")
