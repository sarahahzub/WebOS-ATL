/*
 * StubGen: fill ATL's api-impl with every public Android SDK API it lacks, so apps stop dying on
 * NoClassDefFoundError / NoSuchMethodError / AbstractMethodError for APIs they merely touch.
 *
 *   java -cp asm.jar:asm-tree.jar:. StubGen <android.jar> <hax.jar> <core-all_classes.jar> <out.jar> [report.txt]
 *
 * Inputs: android.jar (SDK platform, API 35; its classes are signature-exact "throw new RuntimeException(Stub!)" stubs),
 *         hax.jar (ATL's compiled api-impl classes, before dx), core-all_classes.jar (libcore: java.*, android.system.*, ...).
 * Output: out.jar = hax.jar with
 *   1. every android.* class missing from ATL+libcore added (taken from android.jar, bodies rewritten to safe defaults),
 *   2. every public/protected method/constructor missing from an existing ATL class added (safe default body),
 *   3. missing SDK interfaces added to ATL classes (+ their missing methods), so casts/instanceof work,
 *   4. missing static fields added (constants keep their value; objects are null).
 * Safe defaults: void -> nothing; boolean/number -> false/0; List/Set/Map/Collection/Iterable -> new empty mutable instance;
 *   arrays -> empty array; same type as the owner (builders, fluent setters) -> this; static factory returning the owner
 *   type -> cached instance (android.atl.ATLServices.forClass); Optional -> empty; anything else -> null.
 *   Each generated body first calls android.atl.ATLStub.hit("Class.method") which logs the first call of each stub
 *   ("ATLSTUB: ..."), so the smoke bench can rank what apps really use. ATL_STUB_LOG=0 silences it.
 * ATL's own code is never replaced; only additions. Abstract methods of NEW classes stay abstract (apps subclass them).
 * Interfaces are completed too: missing abstract methods are added (concrete ATL implementers get safe bodies),
 * default and static interface methods get safe bodies.
 */
import java.io.*;
import java.util.*;
import java.util.jar.*;
import java.util.zip.*;
import org.objectweb.asm.*;
import org.objectweb.asm.tree.*;

public class StubGen implements Opcodes {
	static final Map<String, ClassNode> sdk = new HashMap<>();  // android.jar
	static final Map<String, ClassNode> atl = new HashMap<>();  // hax.jar (mutable, written back)
	static final Set<String> core = new HashSet<>();           // libcore class names
	static final Map<String, ClassNode> coreNodes = new HashMap<>();
	static final Set<String> added = new TreeSet<>(), changed = new TreeSet<>();
	static final List<String> log = new ArrayList<>();
	static int nMethods, nFields, nIfaces, nCtors, skipped, nImpl;
	static final Map<String, List<MethodNode>> ifaceAdded = new HashMap<>();  // interface -> abstract methods StubGen added
	static final String HIT = "android/atl/ATLStub";

	public static void main(String[] a) throws Exception {
		read(a[0], sdk, null, true);
		read(a[1], atl, null, false);
		read(a[2], coreNodes, core, false);
		String out = a[3];

		// pass 1: missing classes
		for (ClassNode s : new ArrayList<>(sdk.values())) {
			if (!s.name.startsWith("android/") || s.name.startsWith("android/R$") || s.name.equals("android/R")) continue;
			if (!visible(s.access) && !isVisibleInner(s)) continue;
			if (exists(s.name)) continue;
			atl.put(s.name, convertNewClass(s));
			added.add(s.name);
		}
		// pass 2: missing members / interfaces in existing ATL classes
		for (ClassNode s : sdk.values()) {
			if (!s.name.startsWith("android/") || s.name.startsWith("android/R$") || s.name.equals("android/R")) continue;
			ClassNode c = atl.get(s.name);
			if (c == null || added.contains(s.name)) continue;  // libcore-provided or new
			augment(s, c);
		}
		// pass 2b: concrete ATL classes implementing an interface that gained abstract methods get safe bodies
		for (ClassNode c : new ArrayList<>(atl.values())) {
			if ((c.access & (ACC_INTERFACE | ACC_ABSTRACT)) != 0) continue;
			for (Map.Entry<String, List<MethodNode>> e : ifaceAdded.entrySet()) {
				if (!implementsTransitively(c, e.getKey())) continue;
				for (MethodNode im : e.getValue()) {
					if (hasMethod(c.name, im.name, im.desc) || inheritedConcrete(c, im.name, im.desc) || defaultInIfaces(c, im.name, im.desc)) continue;
					MethodNode m = newMethod(im);
					m.access = ACC_PUBLIC;
					m.instructions = defaultBody(c.name, m);
					c.methods.add(m); nImpl++; changed.add(c.name);
					log.add("+impl " + c.name + "." + im.name + im.desc + " (for " + e.getKey() + ")");
				}
			}
		}
		// pass 3: generated constructors must call a super/this constructor that exists
		for (String n : added) fixCtors(atl.get(n));
		atl.put(HIT, makeHitClass());

		write(a[1], out);
		String summary = String.format("StubGen: +%d classes, +%d methods (+%d impls for completed interfaces), +%d ctors, +%d fields, +%d interfaces on %d existing classes, %d members skipped",
		                               added.size(), nMethods, nImpl, nCtors, nFields, nIfaces, changed.size(), skipped);
		System.out.println(summary);
		if (a.length > 4) {
			try (PrintWriter w = new PrintWriter(a[4])) {
				w.println(summary);
				w.println("\n# added classes");
				for (String n : added) w.println(n);
				w.println("\n# changes to existing ATL classes");
				for (String l : log) w.println(l);
			}
		}
	}

	static boolean visible(int acc) { return (acc & (ACC_PUBLIC | ACC_PROTECTED)) != 0; }

	static boolean isVisibleInner(ClassNode s) {
		for (InnerClassNode ic : s.innerClasses)
			if (ic.name.equals(s.name)) return visible(ic.access);
		return false;
	}

	static boolean exists(String n) { return atl.containsKey(n) || core.contains(n); }

	static ClassNode find(String n) {
		ClassNode c = atl.get(n);
		return c != null ? c : coreNodes.get(n);
	}

	// ---------------------------------------------------------------- new classes
	static ClassNode convertNewClass(ClassNode s) {
		ClassNode c = s;  // android.jar nodes are only read, reuse in place
		c.version = V1_8;
		c.nestHostClass = null; c.nestMembers = null; c.permittedSubclasses = null; c.recordComponents = null;
		c.visibleAnnotations = c.invisibleAnnotations = null;
		c.visibleTypeAnnotations = c.invisibleTypeAnnotations = null;
		c.module = null;
		boolean iface = (c.access & ACC_INTERFACE) != 0;
		for (Iterator<MethodNode> it = c.methods.iterator(); it.hasNext();) {
			MethodNode m = it.next();
			clean(m);
			if ((m.access & (ACC_ABSTRACT | ACC_NATIVE)) != 0) continue;   /* interface default/static methods keep a safe body below */
			if (m.name.equals("<clinit>")) { it.remove(); continue; }
			if (m.name.equals("<init>")) continue;  // fixCtors
			m.instructions = defaultBody(c.name, m);
			m.tryCatchBlocks = new ArrayList<>(); m.localVariables = null;
		}
		for (FieldNode f : c.fields) { f.visibleAnnotations = f.invisibleAnnotations = null; }
		return c;
	}

	static void clean(MethodNode m) {
		m.visibleAnnotations = m.invisibleAnnotations = null;
		m.visibleParameterAnnotations = m.invisibleParameterAnnotations = null;
		m.visibleTypeAnnotations = m.invisibleTypeAnnotations = null;
		m.visibleLocalVariableAnnotations = m.invisibleLocalVariableAnnotations = null;
		m.annotationDefault = (m.annotationDefault);
		m.localVariables = null;
		m.parameters = null;
	}

	/** android.jar constructors: [aload_0, push args..., invokespecial super/this.<init>, new RuntimeException...athrow].
	 *  Keep the prefix up to the first <init> call on `this`, then return; fall back to super() if that target is missing. */
	static void fixCtors(ClassNode c) {
		if ((c.access & ACC_INTERFACE) != 0) return;
		for (MethodNode m : c.methods) {
			if (!m.name.equals("<init>")) continue;
			InsnList keep = new InsnList();
			boolean ok = false;
			for (AbstractInsnNode i = m.instructions.getFirst(); i != null; i = i.getNext()) {
				if (i instanceof LabelNode || i instanceof LineNumberNode || i instanceof FrameNode) continue;
				if (i.getOpcode() == NEW || i.getOpcode() == ATHROW) break;
				keep.add(i.clone(new HashMap<>()));
				if (i.getOpcode() == INVOKESPECIAL && ((MethodInsnNode) i).name.equals("<init>")) {
					MethodInsnNode mi = (MethodInsnNode) i;
					ok = hasMethod(mi.owner, "<init>", mi.desc);
					break;
				}
			}
			if (!ok) {
				keep = new InsnList();
				String sup = superCtorTarget(c);
				if (sup == null) { log.add("WARN no usable super ctor for " + c.name + m.desc); keep.add(new VarInsnNode(ALOAD, 0)); keep.add(new MethodInsnNode(INVOKESPECIAL, "java/lang/Object", "<init>", "()V", false)); c.superName = c.superName; }
				else { keep.add(new VarInsnNode(ALOAD, 0)); keep.add(new MethodInsnNode(INVOKESPECIAL, c.superName, "<init>", sup, false)); }
			}
			keep.add(hitCall(c.name, m));
			keep.add(new InsnNode(RETURN));
			m.instructions = keep;
			m.tryCatchBlocks = new ArrayList<>(); m.localVariables = null;
		}
	}

	/** descriptor of a callable super constructor using only default args; only "()V" supported for safety */
	static String superCtorTarget(ClassNode c) {
		return hasMethod(c.superName, "<init>", "()V") ? "()V" : null;
	}

	static boolean hasMethod(String owner, String name, String desc) {
		ClassNode o = find(owner);
		if (o == null) return false;
		for (MethodNode m : o.methods)
			if (m.name.equals(name) && m.desc.equals(desc)) return true;
		return false;
	}

	// ---------------------------------------------------------------- existing ATL classes
	static void augment(ClassNode s, ClassNode c) {
		boolean iface = (c.access & ACC_INTERFACE) != 0;
		// interfaces the SDK class implements but ATL's doesn't
		if (!iface)
			for (String in : s.interfaces)
				if (!c.interfaces.contains(in) && (exists(in) || added.contains(in)) && !implementsTransitively(c, in)) {
					c.interfaces.add(in); nIfaces++; changed.add(c.name);
					log.add("+iface " + c.name + " implements " + in);
					ClassNode ifn = find(in) != null ? find(in) : sdk.get(in);
					if (ifn != null) addInterfaceMethods(c, ifn, new HashSet<>());
				}
		Set<String> have = new HashSet<>();
		for (MethodNode m : c.methods) have.add(m.name + m.desc);
		for (MethodNode sm : s.methods) {
			if (!visible(sm.access) || sm.name.equals("<clinit>")) continue;
			if (have.contains(sm.name + sm.desc) || inheritedConcrete(c, sm.name, sm.desc)) continue;
			if (iface) {
				if (sm.name.equals("<init>")) continue;
				MethodNode m = newMethod(sm);
				if ((sm.access & ACC_ABSTRACT) != 0) {          /* abstract: implementers that predate it get a default body (pass 2b) */
					m.access = (sm.access & ~ACC_NATIVE) | ACC_ABSTRACT;
					m.instructions = new InsnList();
					ifaceAdded.computeIfAbsent(c.name, k -> new ArrayList<>()).add(m);
				} else {                                       /* default or static interface method */
					m.access = sm.access & ~(ACC_ABSTRACT | ACC_NATIVE);
					m.instructions = defaultBody(c.name, m);
				}
				c.methods.add(m); nMethods++; have.add(sm.name + sm.desc); changed.add(c.name);
				log.add("+iface method " + c.name + "." + sm.name + sm.desc);
				continue;
			}
			if (sm.name.equals("<init>")) {
				MethodNode m = newMethod(sm);
				InsnList b = new InsnList();
				if (hasMethod(c.name, "<init>", "()V")) {
					b.add(new VarInsnNode(ALOAD, 0)); b.add(new MethodInsnNode(INVOKESPECIAL, c.name, "<init>", "()V", false));
				} else if (c.superName != null && hasMethod(c.superName, "<init>", "()V")) {
					b.add(new VarInsnNode(ALOAD, 0)); b.add(new MethodInsnNode(INVOKESPECIAL, c.superName, "<init>", "()V", false));
				} else { log.add("skip ctor " + c.name + sm.desc + " (no no-arg ctor to chain)"); skipped++; continue; }
				b.add(hitCall(c.name, m)); b.add(new InsnNode(RETURN));
				m.instructions = b; c.methods.add(m); nCtors++;
			} else {
				MethodNode m = newMethod(sm);
				m.access &= ~(ACC_ABSTRACT | ACC_NATIVE);
				m.instructions = defaultBody(c.name, m);
				c.methods.add(m); nMethods++;
			}
			have.add(sm.name + sm.desc); changed.add(c.name);
			log.add("+method " + c.name + "." + sm.name + sm.desc);
		}
		Set<String> haveF = new HashSet<>();
		for (FieldNode f : c.fields) haveF.add(f.name);
		for (FieldNode sf : s.fields) {
			if (!visible(sf.access) || haveF.contains(sf.name) || fieldInherited(c, sf.name)) continue;
			if ((sf.access & ACC_STATIC) == 0) {  // instance field layout is ATL's business
				log.add("skip instance field " + c.name + "." + sf.name); skipped++; continue;
			}
			FieldNode f = new FieldNode(sf.access & ~ACC_FINAL | ((sf.value != null) ? ACC_FINAL : 0), sf.name, sf.desc, sf.signature, sf.value);
			c.fields.add(f); nFields++; changed.add(c.name);
			log.add("+field " + c.name + "." + sf.name);
		}
	}

	static void addInterfaceMethods(ClassNode c, ClassNode ifn, Set<String> seen) {
		if (!seen.add(ifn.name)) return;
		for (MethodNode im : ifn.methods) {
			if ((im.access & ACC_STATIC) != 0 || im.name.startsWith("<")) continue;
			if (hasMethod(c.name, im.name, im.desc) || inheritedConcrete(c, im.name, im.desc)) continue;
			MethodNode m = newMethod(im);
			m.access = ACC_PUBLIC;
			m.instructions = defaultBody(c.name, m);
			c.methods.add(m); nMethods++;
			log.add("+method " + c.name + "." + im.name + im.desc + " (for " + ifn.name + ")");
		}
		for (String sup : ifn.interfaces) {
			ClassNode n = find(sup) != null ? find(sup) : sdk.get(sup);
			if (n != null) addInterfaceMethods(c, n, seen);
		}
	}

	/* a default method for name+desc in any interface c implements (then no body is needed) */
	static boolean defaultInIfaces(ClassNode c, String name, String desc) {
		for (ClassNode k = c; k != null; k = k.superName == null ? null : find(k.superName))
			for (String i : k.interfaces)
				if (defaultIn(i, name, desc, new HashSet<>())) return true;
		return false;
	}

	static boolean defaultIn(String i, String name, String desc, Set<String> seen) {
		if (!seen.add(i)) return false;
		ClassNode n = find(i);
		if (n == null) return false;
		for (MethodNode m : n.methods)
			if (m.name.equals(name) && m.desc.equals(desc) && (m.access & ACC_ABSTRACT) == 0 && (m.access & ACC_STATIC) == 0) return true;
		for (String s : n.interfaces)
			if (defaultIn(s, name, desc, seen)) return true;
		return false;
	}

	static boolean implementsTransitively(ClassNode c, String in) {
		for (ClassNode k = c; k != null; k = k.superName == null ? null : find(k.superName))
			for (String i : k.interfaces)
				if (i.equals(in) || extendsIface(i, in, new HashSet<>())) return true;
		return false;
	}

	static boolean extendsIface(String i, String target, Set<String> seen) {
		if (!seen.add(i)) return false;
		ClassNode n = find(i);
		if (n == null) return false;
		for (String s : n.interfaces)
			if (s.equals(target) || extendsIface(s, target, seen)) return true;
		return false;
	}

	static boolean inheritedConcrete(ClassNode c, String name, String desc) {
		if (name.equals("<init>")) return false;
		for (ClassNode k = c.superName == null ? null : find(c.superName); k != null; k = k.superName == null ? null : find(k.superName))
			for (MethodNode m : k.methods)
				if (m.name.equals(name) && m.desc.equals(desc) && (m.access & ACC_PRIVATE) == 0) return true;
		return false;
	}

	static boolean fieldInherited(ClassNode c, String name) {
		for (ClassNode k = c.superName == null ? null : find(c.superName); k != null; k = k.superName == null ? null : find(k.superName))
			for (FieldNode f : k.fields)
				if (f.name.equals(name)) return true;
		return false;
	}

	static MethodNode newMethod(MethodNode sm) {
		MethodNode m = new MethodNode(sm.access & ~(ACC_ABSTRACT | ACC_NATIVE | ACC_SYNCHRONIZED), sm.name, sm.desc, sm.signature,
		                              sm.exceptions == null ? null : sm.exceptions.toArray(new String[0]));
		return m;
	}

	// ---------------------------------------------------------------- default bodies
	static final Map<String, String> EMPTY_COLL = new HashMap<>();
	static {
		EMPTY_COLL.put("java/util/List", "java/util/ArrayList");
		EMPTY_COLL.put("java/util/ArrayList", "java/util/ArrayList");
		EMPTY_COLL.put("java/util/Collection", "java/util/ArrayList");
		EMPTY_COLL.put("java/lang/Iterable", "java/util/ArrayList");
		EMPTY_COLL.put("java/util/Set", "java/util/HashSet");
		EMPTY_COLL.put("java/util/HashSet", "java/util/HashSet");
		EMPTY_COLL.put("java/util/Map", "java/util/HashMap");
		EMPTY_COLL.put("java/util/HashMap", "java/util/HashMap");
	}

	static InsnList defaultBody(String owner, MethodNode m) {
		InsnList b = new InsnList();
		b.add(hitCall(owner, m));
		Type r = Type.getReturnType(m.desc);
		switch (r.getSort()) {
			case Type.VOID: b.add(new InsnNode(RETURN)); break;
			case Type.BOOLEAN: case Type.CHAR: case Type.BYTE: case Type.SHORT: case Type.INT: b.add(new InsnNode(ICONST_0)); b.add(new InsnNode(IRETURN)); break;
			case Type.LONG: b.add(new InsnNode(LCONST_0)); b.add(new InsnNode(LRETURN)); break;
			case Type.FLOAT: b.add(new InsnNode(FCONST_0)); b.add(new InsnNode(FRETURN)); break;
			case Type.DOUBLE: b.add(new InsnNode(DCONST_0)); b.add(new InsnNode(DRETURN)); break;
			case Type.ARRAY: {
				b.add(new InsnNode(ICONST_0));
				Type el = r.getElementType();
				if (r.getDimensions() == 1 && el.getSort() < Type.ARRAY) b.add(new IntInsnNode(NEWARRAY, primArrayCode(el)));
				else b.add(new TypeInsnNode(ANEWARRAY, Type.getType(r.getDescriptor().substring(1)).getInternalName()));
				b.add(new InsnNode(ARETURN)); break;
			}
			default: {
				String rn = r.getInternalName();
				String coll = EMPTY_COLL.get(rn);
				if (coll != null) {
					b.add(new TypeInsnNode(NEW, coll)); b.add(new InsnNode(DUP));
					b.add(new MethodInsnNode(INVOKESPECIAL, coll, "<init>", "()V", false));
				} else if ((m.access & ACC_STATIC) == 0 && rn.equals(owner)) {
					b.add(new VarInsnNode(ALOAD, 0));  // fluent setter / builder
				} else if ((m.access & ACC_STATIC) != 0 && rn.equals(owner)) {
					// static factory (X.get(Context), X.getInstance(), X.from(...)): a cached instance, not null
					b.add(new LdcInsnNode(Type.getObjectType(owner)));
					b.add(new MethodInsnNode(INVOKESTATIC, "android/atl/ATLServices", "forClass", "(Ljava/lang/Class;)Ljava/lang/Object;", false));
					b.add(new TypeInsnNode(CHECKCAST, owner));
				} else if (rn.equals("java/util/Optional")) {
					b.add(new MethodInsnNode(INVOKESTATIC, "java/util/Optional", "empty", "()Ljava/util/Optional;", false));
				} else {
					b.add(new InsnNode(ACONST_NULL));
				}
				b.add(new InsnNode(ARETURN));
			}
		}
		return b;
	}

	static int primArrayCode(Type t) {
		switch (t.getSort()) {
			case Type.BOOLEAN: return T_BOOLEAN; case Type.CHAR: return T_CHAR; case Type.BYTE: return T_BYTE;
			case Type.SHORT: return T_SHORT; case Type.INT: return T_INT; case Type.LONG: return T_LONG;
			case Type.FLOAT: return T_FLOAT; default: return T_DOUBLE;
		}
	}

	static InsnList hitCall(String owner, MethodNode m) {
		InsnList b = new InsnList();
		b.add(new LdcInsnNode(owner.replace('/', '.') + "." + m.name + m.desc));
		b.add(new MethodInsnNode(INVOKESTATIC, HIT, "hit", "(Ljava/lang/String;)V", false));
		return b;
	}

	/** android.atl.ATLStub: logs the first call of every generated stub ("ATLSTUB: <Class.method(desc)>") */
	static ClassNode makeHitClass() {
		ClassNode c = new ClassNode();
		c.version = V1_8; c.access = ACC_PUBLIC | ACC_FINAL | ACC_SUPER; c.name = HIT; c.superName = "java/lang/Object";
		c.fields.add(new FieldNode(ACC_PRIVATE | ACC_STATIC | ACC_FINAL, "seen", "Ljava/util/Set;", null, null));
		c.fields.add(new FieldNode(ACC_PRIVATE | ACC_STATIC | ACC_FINAL, "quiet", "Z", null, null));
		MethodNode cl = new MethodNode(ACC_STATIC, "<clinit>", "()V", null, null);
		InsnList i = cl.instructions;
		i.add(new TypeInsnNode(NEW, "java/util/HashSet")); i.add(new InsnNode(DUP));
		i.add(new MethodInsnNode(INVOKESPECIAL, "java/util/HashSet", "<init>", "()V", false));
		i.add(new MethodInsnNode(INVOKESTATIC, "java/util/Collections", "synchronizedSet", "(Ljava/util/Set;)Ljava/util/Set;", false));
		i.add(new FieldInsnNode(PUTSTATIC, HIT, "seen", "Ljava/util/Set;"));
		i.add(new LdcInsnNode("0")); i.add(new LdcInsnNode("ATL_STUB_LOG"));
		i.add(new MethodInsnNode(INVOKESTATIC, "java/lang/System", "getenv", "(Ljava/lang/String;)Ljava/lang/String;", false));
		i.add(new MethodInsnNode(INVOKEVIRTUAL, "java/lang/String", "equals", "(Ljava/lang/Object;)Z", false));
		i.add(new FieldInsnNode(PUTSTATIC, HIT, "quiet", "Z"));
		i.add(new InsnNode(RETURN));
		c.methods.add(cl);
		MethodNode init = new MethodNode(ACC_PRIVATE, "<init>", "()V", null, null);
		init.instructions.add(new VarInsnNode(ALOAD, 0));
		init.instructions.add(new MethodInsnNode(INVOKESPECIAL, "java/lang/Object", "<init>", "()V", false));
		init.instructions.add(new InsnNode(RETURN));
		c.methods.add(init);
		// public static void hit(String s) { if (quiet || !seen.add(s)) return; System.err.println("ATLSTUB: " + s); }
		MethodNode h = new MethodNode(ACC_PUBLIC | ACC_STATIC, "hit", "(Ljava/lang/String;)V", null, null);
		InsnList x = h.instructions;
		LabelNode ret = new LabelNode();
		x.add(new FieldInsnNode(GETSTATIC, HIT, "quiet", "Z"));
		x.add(new JumpInsnNode(IFNE, ret));
		x.add(new FieldInsnNode(GETSTATIC, HIT, "seen", "Ljava/util/Set;"));
		x.add(new VarInsnNode(ALOAD, 0));
		x.add(new MethodInsnNode(INVOKEINTERFACE, "java/util/Set", "add", "(Ljava/lang/Object;)Z", true));
		x.add(new JumpInsnNode(IFEQ, ret));
		x.add(new FieldInsnNode(GETSTATIC, "java/lang/System", "err", "Ljava/io/PrintStream;"));
		x.add(new TypeInsnNode(NEW, "java/lang/StringBuilder")); x.add(new InsnNode(DUP));
		x.add(new LdcInsnNode("ATLSTUB: "));
		x.add(new MethodInsnNode(INVOKESPECIAL, "java/lang/StringBuilder", "<init>", "(Ljava/lang/String;)V", false));
		x.add(new VarInsnNode(ALOAD, 0));
		x.add(new MethodInsnNode(INVOKEVIRTUAL, "java/lang/StringBuilder", "append", "(Ljava/lang/String;)Ljava/lang/StringBuilder;", false));
		x.add(new MethodInsnNode(INVOKEVIRTUAL, "java/lang/StringBuilder", "toString", "()Ljava/lang/String;", false));
		x.add(new MethodInsnNode(INVOKEVIRTUAL, "java/io/PrintStream", "println", "(Ljava/lang/String;)V", false));
		x.add(ret);
		x.add(new FrameNode(F_SAME, 0, null, 0, null));
		x.add(new InsnNode(RETURN));
		c.methods.add(h);
		return c;
	}

	// ---------------------------------------------------------------- jar io
	static void read(String jar, Map<String, ClassNode> into, Set<String> names, boolean full) throws IOException {
		try (ZipFile z = new ZipFile(jar)) {
			for (Enumeration<? extends ZipEntry> e = z.entries(); e.hasMoreElements();) {
				ZipEntry ze = e.nextElement();
				if (!ze.getName().endsWith(".class") || ze.getName().startsWith("META-INF")) continue;
				try (InputStream in = z.getInputStream(ze)) {
					ClassReader cr = new ClassReader(in);
					ClassNode n = new ClassNode();
					cr.accept(n, full ? 0 : ClassReader.SKIP_DEBUG);
					into.put(n.name, n);
					if (names != null) names.add(n.name);
				}
			}
		}
	}

	static void write(String orig, String out) throws IOException {
		Set<String> written = new HashSet<>();
		try (ZipFile z = new ZipFile(orig); JarOutputStream jo = new JarOutputStream(new FileOutputStream(out))) {
			for (Enumeration<? extends ZipEntry> e = z.entries(); e.hasMoreElements();) {
				ZipEntry ze = e.nextElement();
				String n = ze.getName();
				if (n.endsWith(".class")) {
					String cn = n.substring(0, n.length() - 6);
					ClassNode c = atl.get(cn);
					if (c != null && (changed.contains(cn))) { put(jo, n, bytes(c)); written.add(cn); continue; }
				}
				if (ze.isDirectory()) continue;
				jo.putNextEntry(new ZipEntry(n));
				try (InputStream in = z.getInputStream(ze)) { in.transferTo(jo); }
				jo.closeEntry();
				if (n.endsWith(".class")) written.add(n.substring(0, n.length() - 6));
			}
			for (Map.Entry<String, ClassNode> en : atl.entrySet())
				if (!written.contains(en.getKey())) put(jo, en.getKey() + ".class", bytes(en.getValue()));
		}
	}

	static void put(JarOutputStream jo, String n, byte[] b) throws IOException {
		jo.putNextEntry(new ZipEntry(n)); jo.write(b); jo.closeEntry();
	}

	static byte[] bytes(ClassNode c) {
		if (c.version > V1_8 || c.version < V1_1) c.version = V1_8;
		ClassWriter w = new ClassWriter(ClassWriter.COMPUTE_MAXS);
		c.accept(w);
		return w.toByteArray();
	}
}
