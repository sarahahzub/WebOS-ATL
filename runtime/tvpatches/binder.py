#!/usr/bin/env python3
"""In-process Binder semantics (needed for microG and every app-internal AIDL service).
ATL runs everything in one process, but Binder.transact() returned false and queryLocalInterface() returned null, so
every AIDL proxy call (GMS client -> microG, app -> its own bound service) silently failed.
 - transact() dispatches to onTransact() on the same Parcel objects (positions rewound), exceptions are written to the
   reply like Binder.execTransact does; oneway calls run synchronously.
 - queryLocalInterface() returns the owner for a matching descriptor (same-classloader fast path, as on Android).
 - onTransact() default answers INTERFACE_TRANSACTION / PING_TRANSACTION.
 - IBinder gains linkToDeath/unlinkToDeath/pingBinder/isBinderAlive/dump/dumpAsync (+ constants) - GMS calls them.
usage: binder.py <atl src root>"""
import os, re, sys
R = sys.argv[1]
A = os.path.join(R, "src/api-impl/android/os")

open(os.path.join(A, "IBinder.java"), "w").write('''package android.os;

import java.io.FileDescriptor;

public interface IBinder {
	int FIRST_CALL_TRANSACTION = 0x00000001;
	int LAST_CALL_TRANSACTION = 0x00ffffff;
	int PING_TRANSACTION = ('_' << 24) | ('P' << 16) | ('N' << 8) | 'G';
	int DUMP_TRANSACTION = ('_' << 24) | ('D' << 16) | ('M' << 8) | 'P';
	int SHELL_COMMAND_TRANSACTION = ('_' << 24) | ('C' << 16) | ('M' << 8) | 'D';
	int INTERFACE_TRANSACTION = ('_' << 24) | ('N' << 16) | ('T' << 8) | 'F';
	int TWEET_TRANSACTION = ('_' << 24) | ('T' << 16) | ('W' << 8) | 'T';
	int LIKE_TRANSACTION = ('_' << 24) | ('L' << 16) | ('I' << 8) | 'K';
	int FLAG_ONEWAY = 0x00000001;
	int FLAG_CLEAR_BUF = 0x00000020;

	public interface DeathRecipient {
		public void binderDied();
	}

	public IInterface queryLocalInterface(String descriptor);

	/* (no "throws RemoteException": ATL code calls these without catching; apps compiled against android.jar catch anyway) */
	public boolean transact(int code, Parcel data, Parcel reply, int flags);

	public String getInterfaceDescriptor();

	/* everything is in-process and never dies */
	public default boolean pingBinder() { return true; }

	public default boolean isBinderAlive() { return true; }

	public default void linkToDeath(DeathRecipient recipient, int flags) {}

	public default boolean unlinkToDeath(DeathRecipient recipient, int flags) { return true; }

	public default void dump(FileDescriptor fd, String[] args) {}

	public default void dumpAsync(FileDescriptor fd, String[] args) {}
}
''')
p = os.path.join(A, "Binder.java")
s = open(p).read()
s = s.replace("import android.content.Context;", "import android.content.Context;\nimport java.io.FileDescriptor;\nimport java.io.PrintWriter;", 1)
s = s.replace("""	@Override
	public IInterface queryLocalInterface(String descriptor) { return null; }

	@Override
	public boolean transact(int code, Parcel data, Parcel reply, int flags) { return false; }
""", """	public Binder() {}

	public Binder(String descriptor) {
		mDescriptor = descriptor;
	}

	@Override
	public IInterface queryLocalInterface(String descriptor) {
		if (mDescriptor != null && mDescriptor.equals(descriptor))
			return mOwner;
		return null;
	}

	/* In-process transaction: what Binder.execTransact does on Android, minus the kernel. */
	@Override
	public boolean transact(int code, Parcel data, Parcel reply, int flags) {
		if (data != null)
			data.setDataPosition(0);
		boolean handled;
		try {
			handled = onTransact(code, data, reply, flags);
		} catch (RemoteException | RuntimeException e) {
			if (reply == null || (flags & FLAG_ONEWAY) != 0) {
				if (e instanceof RuntimeException)
					throw (RuntimeException)e;
				android.util.Log.w("Binder", "oneway transaction " + code + " failed", e);
				return true;
			}
			reply.setDataPosition(0);
			reply.setDataSize(0);
			try {
				reply.writeException(e);
			} catch (Exception unwritable) {
				if (e instanceof RuntimeException)
					throw (RuntimeException)e;
				throw new RuntimeException(e);
			}
			handled = true;
		}
		if (reply != null)
			reply.setDataPosition(0);
		return handled;
	}

	protected boolean onTransact(int code, Parcel data, Parcel reply, int flags) throws RemoteException {
		if (code == INTERFACE_TRANSACTION) {
			if (reply != null)
				reply.writeString(getInterfaceDescriptor());
			return true;
		}
		if (code == PING_TRANSACTION || code == DUMP_TRANSACTION)
			return true;
		return false;
	}

	protected void dump(FileDescriptor fd, PrintWriter fout, String[] args) {}

	public static final boolean isProxy(IInterface iface) { return false; }

	public static final int getCallingUserHandle() { return 0; }

	public static final long getCallingUidOrThrow() { return getCallingUid(); }

	public static final boolean isDirectlyHandlingTransaction() { return false; }

	public static final void joinThreadPool() {}

	public static void setCallingWorkSourceUid(int workSource) {}

	public static int getCallingWorkSourceUid() { return getCallingUid(); }
""", 1)
assert "In-process transaction" in s, "Binder.java changed upstream"
open(p, "w").write(s)

print("binder: ok")
