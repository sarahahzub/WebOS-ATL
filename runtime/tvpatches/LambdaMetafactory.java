/*
 * Copyright (c) 2012, 2013, Oracle and/or its affiliates. All rights reserved.
 * DO NOT ALTER OR REMOVE COPYRIGHT NOTICES OR THIS FILE HEADER.
 *
 * This code is free software; you can redistribute it and/or modify it
 * under the terms of the GNU General Public License version 2 only, as
 * published by the Free Software Foundation.  Oracle designates this
 * particular file as subject to the "Classpath" exception as provided
 * by Oracle in the LICENSE file that accompanied this code.
 */

package java.lang.invoke;

import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.util.ArrayList;

/**
 * Working LambdaMetafactory for ART (android_translation_layer).
 *
 * Android's libcore ships this class as a stub that returns null: on Android all lambdas are desugared by D8, so it is
 * never called. ATL's libcore, however, is dexed with invoke-custom kept (min-sdk 27), so libcore's own lambdas
 * (java.time.format.DateTimeFormatterBuilder, java.time.chrono.AbstractChronology, java.util.stream, ...) hit this and
 * died with "BootstrapMethodError: Bootstrap method returned null" - every app touching java.time crashed.
 *
 * Implementation: the call site returns a java.lang.reflect.Proxy implementing the functional interface (+ markers);
 * calls of the SAM method (and its bridges) go to implMethod with the captured arguments prepended; Object methods
 * get lambda identity semantics; default methods run their interface implementation.
 */
public class LambdaMetafactory {

	public static final int FLAG_SERIALIZABLE = 1 << 0;

	public static final int FLAG_MARKERS = 1 << 1;

	public static final int FLAG_BRIDGES = 1 << 2;

	private static final Object[] NO_ARGS = new Object[0];

	public static CallSite metafactory(MethodHandles.Lookup caller,
	                                   String invokedName,
	                                   MethodType invokedType,
	                                   MethodType samMethodType,
	                                   MethodHandle implMethod,
	                                   MethodType instantiatedMethodType)
	        throws LambdaConversionException {
		return build(caller, invokedName, invokedType, implMethod, null);
	}

	public static CallSite altMetafactory(MethodHandles.Lookup caller,
	                                      String invokedName,
	                                      MethodType invokedType,
	                                      Object... args)
	        throws LambdaConversionException {
		// args: samMethodType, implMethod, instantiatedMethodType, flags, [markerCount, markers...], [bridgeCount, bridges...]
		MethodHandle impl = (MethodHandle)args[1];
		int flags = (Integer)args[3];
		int i = 4;
		ArrayList<Class<?>> markers = new ArrayList<Class<?>>();
		if ((flags & FLAG_MARKERS) != 0) {
			int n = (Integer)args[i++];
			for (int k = 0; k < n; k++)
				markers.add((Class<?>)args[i++]);
		}
		if ((flags & FLAG_SERIALIZABLE) != 0)
			markers.add(java.io.Serializable.class);
		return build(caller, invokedName, invokedType, impl, markers);
	}

	private static CallSite build(MethodHandles.Lookup caller, String samName, MethodType invokedType,
	                              MethodHandle impl, ArrayList<Class<?>> markers) throws LambdaConversionException {
		Class<?> iface = invokedType.returnType();
		if (!iface.isInterface())
			throw new LambdaConversionException(iface.getName() + " is not an interface");
		ArrayList<Class<?>> all = new ArrayList<Class<?>>();
		all.add(iface);
		if (markers != null)
			for (Class<?> m : markers)
				if (m.isInterface() && !all.contains(m))
					all.add(m);
		ClassLoader loader = caller.lookupClass().getClassLoader();
		if (loader == null)
			loader = iface.getClassLoader();
		Factory f = new Factory(samName, iface, impl, all.toArray(new Class<?>[all.size()]), loader);
		int n = invokedType.parameterCount();
		try {
			if (n == 0) { // non-capturing: one shared instance, like the JDK
				f.single = f.create(NO_ARGS);
				MethodHandle single = MethodHandles.Lookup.IMPL_LOOKUP.findVirtual(Factory.class, "single", MethodType.methodType(Object.class));
				return new ConstantCallSite(single.bindTo(f).asType(invokedType));
			}
			MethodHandle create = MethodHandles.Lookup.IMPL_LOOKUP.findVirtual(Factory.class, "create",
			                                                          MethodType.methodType(Object.class, Object[].class));
			MethodHandle target = create.bindTo(f).asCollector(Object[].class, n).asType(invokedType);
			return new ConstantCallSite(target);
		} catch (ReflectiveOperationException | RuntimeException e) {
			LambdaConversionException lce = new LambdaConversionException("cannot link lambda for " + iface.getName() + ": " + e);
			lce.initCause(e);
			throw lce;
		}
	}

	static final class Factory {
		final String samName;
		final Class<?> iface;
		final MethodHandle impl;
		final Class<?>[] ifaces;
		final ClassLoader loader;
		Object single;

		Factory(String samName, Class<?> iface, MethodHandle impl, Class<?>[] ifaces, ClassLoader loader) {
			this.samName = samName;
			this.iface = iface;
			this.impl = impl;
			this.ifaces = ifaces;
			this.loader = loader;
		}

		Object single() {
			return single;
		}

		Object create(Object[] captured) {
			return Proxy.newProxyInstance(loader, ifaces, new Handler(this, captured));
		}
	}

	static final class Handler implements InvocationHandler {
		private final Factory f;
		private final Object[] captured;

		Handler(Factory f, Object[] captured) {
			this.f = f;
			this.captured = captured;
		}

		@Override
		public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
			String name = method.getName();
			Class<?> decl = method.getDeclaringClass();
			if (decl == Object.class) {
				if (name.equals("equals"))
					return proxy == args[0];
				if (name.equals("hashCode"))
					return System.identityHashCode(proxy);
				if (name.equals("toString"))
					return f.iface.getName() + "$$Lambda@" + Integer.toHexString(System.identityHashCode(proxy));
			}
			if (method.isDefault()) {
				MethodHandle special = new MethodHandles.Lookup(decl).unreflectSpecial(method, decl).bindTo(proxy);
				return special.invokeWithArguments(args == null ? NO_ARGS : args);
			}
			if (!name.equals(f.samName))
				throw new AbstractMethodError(decl.getName() + "." + name + " in lambda for " + f.iface.getName());
			int na = args == null ? 0 : args.length;
			Object[] all;
			if (captured.length == 0) {
				all = args == null ? NO_ARGS : args;
			} else {
				all = new Object[captured.length + na];
				System.arraycopy(captured, 0, all, 0, captured.length);
				if (na > 0)
					System.arraycopy(args, 0, all, captured.length, na);
			}
			Object r = f.impl.invokeWithArguments(all);
			return convert(r, method.getReturnType());
		}

		/* impl's return value -> what the (possibly bridged / widening) SAM signature promises; Proxy unboxes */
		private static Object convert(Object v, Class<?> rt) {
			if (rt == void.class)
				return null;
			if (v == null || !rt.isPrimitive())
				return v;
			if (v instanceof Character && rt != char.class)
				v = Integer.valueOf(((Character)v).charValue());
			if (!(v instanceof Number))
				return v;
			Number n = (Number)v;
			if (rt == int.class)
				return n.intValue();
			if (rt == long.class)
				return n.longValue();
			if (rt == double.class)
				return n.doubleValue();
			if (rt == float.class)
				return n.floatValue();
			if (rt == short.class)
				return n.shortValue();
			if (rt == byte.class)
				return n.byteValue();
			if (rt == char.class)
				return (char)n.intValue();
			return v;
		}
	}
}
