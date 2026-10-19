#!/usr/bin/env python3
"""android.transition that works without animating: scenes are entered, listeners are told start/end.
ATL's TransitionInflater.inflateTransition() returned null and Scene/TransitionManager were only StubGen
no-ops, so androidx.leanback (BrowseSupportFragment.createHeadersTransition -> TransitionHelper.loadTransition
-> .addTransitionListener) crashed with an NPE (SmartTube: app closes right after start), and Scene enter
actions (leanback shows/hides the headers in them) never ran.
  - TransitionInflater.inflateTransition(): an empty TransitionSet (no animation) instead of null
  - Transition: keeps its listeners; TransitionListener gets the SDK methods (default no-ops)
  - Scene: enter()/exit() run the enter/exit actions (or show the layout of getSceneForLayout)
  - TransitionManager.go()/transitionTo(): exit the old scene, enter the new one, onTransitionStart now and
    onTransitionEnd on the next main-loop turn (as after a finished animation); beginDelayedTransition: nothing
usage: transitions.py <atl src root>"""
import os, sys
R = os.path.join(sys.argv[1], "src/api-impl")
T = os.path.join(R, "android/transition")


def write(name, text):
    open(os.path.join(T, name), "w").write(text)


write("TransitionInflater.java", """package android.transition;

import android.content.Context;
import android.view.ViewGroup;

public class TransitionInflater {

	public static TransitionInflater from(Context context) {
		return new TransitionInflater();
	}

	// no XML parsing: ATL does not animate transitions, but callers expect a transition (leanback adds listeners)
	public Transition inflateTransition(int resourceId) {
		return new TransitionSet();
	}

	public TransitionManager inflateTransitionManager(int resource, ViewGroup sceneRoot) {
		return new TransitionManager();
	}
}
""")

write("Transition.java", """package android.transition;

import android.animation.TimeInterpolator;
import android.view.View;
import java.util.ArrayList;

public class Transition implements Cloneable {

	public interface TransitionListener {
		default void onTransitionStart(Transition transition) {}
		default void onTransitionEnd(Transition transition) {}
		default void onTransitionCancel(Transition transition) {}
		default void onTransitionPause(Transition transition) {}
		default void onTransitionResume(Transition transition) {}
	}

	ArrayList<TransitionListener> listeners = new ArrayList<>();
	private long duration = -1;
	private long startDelay = -1;
	private TimeInterpolator interpolator;

	public Transition clone() {
		Transition t;
		try {
			t = (Transition)super.clone();
		} catch (CloneNotSupportedException e) {
			t = new Transition();
		}
		t.listeners = new ArrayList<>(listeners);
		return t;
	}

	public Transition addListener(TransitionListener listener) {
		if (listener != null)
			listeners.add(listener);
		return this;
	}

	public Transition removeListener(TransitionListener listener) {
		listeners.remove(listener);
		return this;
	}

	void dispatchStart() {
		for (TransitionListener l : new ArrayList<>(listeners))
			l.onTransitionStart(this);
	}

	void dispatchEnd() {
		for (TransitionListener l : new ArrayList<>(listeners))
			l.onTransitionEnd(this);
	}

	public Transition excludeTarget(int targetId, boolean exclude) {
		return this;
	}

	public Transition excludeTarget(View target, boolean exclude) {
		return this;
	}

	public Transition excludeTarget(Class type, boolean exclude) {
		return this;
	}

	public Transition excludeChildren(int targetId, boolean exclude) {
		return this;
	}

	public Transition addTarget(int targetId) {
		return this;
	}

	public Transition addTarget(View target) {
		return this;
	}

	public Transition addTarget(String targetName) {
		return this;
	}

	public Transition addTarget(Class targetType) {
		return this;
	}

	public Transition removeTarget(int targetId) {
		return this;
	}

	public Transition removeTarget(View target) {
		return this;
	}

	public Transition setDuration(long duration) {
		this.duration = duration;
		return this;
	}

	public long getDuration() {
		return duration;
	}

	public Transition setStartDelay(long startDelay) {
		this.startDelay = startDelay;
		return this;
	}

	public long getStartDelay() {
		return startDelay;
	}

	public Transition setInterpolator(TimeInterpolator interpolator) {
		this.interpolator = interpolator;
		return this;
	}

	public TimeInterpolator getInterpolator() {
		return interpolator;
	}
}
""")

write("TransitionSet.java", """package android.transition;

import java.util.ArrayList;

public class TransitionSet extends Transition {

	public static final int ORDERING_TOGETHER = 0;
	public static final int ORDERING_SEQUENTIAL = 1;

	private ArrayList<Transition> transitions = new ArrayList<>();
	private int ordering = ORDERING_TOGETHER;

	public TransitionSet addTransition(Transition transition) {
		if (transition != null)
			transitions.add(transition);
		return this;
	}

	public TransitionSet removeTransition(Transition transition) {
		transitions.remove(transition);
		return this;
	}

	public int getTransitionCount() {
		return transitions.size();
	}

	public Transition getTransitionAt(int index) {
		return index < 0 || index >= transitions.size() ? null : transitions.get(index);
	}

	public TransitionSet setOrdering(int ordering) {
		this.ordering = ordering;
		return this;
	}

	public int getOrdering() {
		return ordering;
	}

	@Override
	public TransitionSet clone() {
		TransitionSet t = (TransitionSet)super.clone();
		t.transitions = new ArrayList<>(transitions);
		return t;
	}
}
""")

write("Scene.java", """package android.transition;

import android.content.Context;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;

public final class Scene {

	private ViewGroup sceneRoot;
	private View layout;
	private int layoutId = -1;
	private Context context;
	private Runnable enterAction, exitAction;

	public Scene(ViewGroup sceneRoot) {
		this.sceneRoot = sceneRoot;
	}

	public Scene(ViewGroup sceneRoot, View layout) {
		this.sceneRoot = sceneRoot;
		this.layout = layout;
	}

	private Scene(ViewGroup sceneRoot, int layoutId, Context context) {
		this.sceneRoot = sceneRoot;
		this.layoutId = layoutId;
		this.context = context;
	}

	public static Scene getSceneForLayout(ViewGroup sceneRoot, int layoutId, Context context) {
		return new Scene(sceneRoot, layoutId, context);
	}

	public ViewGroup getSceneRoot() {
		return sceneRoot;
	}

	public void setEnterAction(Runnable action) {
		enterAction = action;
	}

	public void setExitAction(Runnable action) {
		exitAction = action;
	}

	public void enter() {
		if (sceneRoot != null && (layoutId > 0 || layout != null)) {
			sceneRoot.removeAllViews();
			if (layoutId > 0)
				LayoutInflater.from(context != null ? context : sceneRoot.getContext()).inflate(layoutId, sceneRoot);
			else
				sceneRoot.addView(layout);
		}
		if (enterAction != null)
			enterAction.run();
		if (sceneRoot != null)
			TransitionManager.current.put(sceneRoot, this);
	}

	public void exit() {
		if (sceneRoot != null && TransitionManager.current.get(sceneRoot) == this && exitAction != null)
			exitAction.run();
	}
}
""")

write("TransitionManager.java", """package android.transition;

import android.os.Handler;
import android.os.Looper;
import android.view.ViewGroup;
import java.util.HashMap;
import java.util.WeakHashMap;

// no animations: scene changes are applied at once, listeners still see start and (on the next loop turn) end
public class TransitionManager {

	static final WeakHashMap<ViewGroup, Scene> current = new WeakHashMap<>();
	private final HashMap<Scene, Transition> sceneTransitions = new HashMap<>();

	public static void go(Scene scene) {
		go(scene, null);
	}

	public static void go(Scene scene, Transition transition) {
		if (scene == null)
			return;
		ViewGroup root = scene.getSceneRoot();
		Scene old = root != null ? current.get(root) : null;
		if (old != null)
			old.exit();
		if (transition != null)
			transition.dispatchStart();
		scene.enter();
		if (transition != null) {
			final Transition t = transition;
			new Handler(Looper.getMainLooper()).post(new Runnable() {
				public void run() {
					t.dispatchEnd();
				}
			});
		}
	}

	public static void beginDelayedTransition(ViewGroup sceneRoot) {}

	public static void beginDelayedTransition(ViewGroup sceneRoot, Transition transition) {}

	public static void endTransitions(ViewGroup sceneRoot) {}

	public void setTransition(Scene scene, Transition transition) {
		sceneTransitions.put(scene, transition);
	}

	public void setTransition(Scene fromScene, Scene toScene, Transition transition) {
		sceneTransitions.put(toScene, transition);
	}

	public void transitionTo(Scene scene) {
		go(scene, sceneTransitions.get(scene));
	}
}
""")

m = os.path.join(R, "meson.build")
s = open(m).read()
for f in ("Scene.java", "TransitionManager.java"):
    line = "\t'android/transition/%s',\n" % f
    if line not in s:
        anchor = "\t'android/transition/Transition.java',\n"
        assert anchor in s, "meson.build: transition anchor"
        s = s.replace(anchor, anchor + line, 1) if f == "Scene.java" else \
            s.replace("\t'android/transition/TransitionInflater.java',\n", "\t'android/transition/TransitionInflater.java',\n" + line, 1)
open(m, "w").write(s)
assert "'android/transition/Scene.java'" in s and "'android/transition/TransitionManager.java'" in s
print("transitions: ok")
