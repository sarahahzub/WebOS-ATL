#!/usr/bin/env python3
"""apkcheck: which Android framework APIs does an APK use that ATL does not implement?

  python3 apkcheck.py app.apk [--jar api-impl.jar] [--json]

Reads the APK's dex files and ATL's api-impl.jar (the Java half of ATL), lists every referenced
`android.*` class that api-impl lacks and every referenced method (name + signature) that the
class, or its android.* superclasses/interfaces, lacks. A missing item only matters if the app
executes it, but missing classes/methods are exactly what makes an app crash on start
(NoClassDefFoundError / NoSuchMethodError), so this predicts "app X will not open".
Standard library only; also used by apkweb.py after an upload.
"""
import json, os, struct, sys, zipfile

DEFAULT_JARS = [
    "/media/developer/webosatl/runtime/lib/java/dex/android_translation_layer/api-impl.jar",
]
OBJECT_METHODS = {"<init>", "<clinit>", "hashCode", "equals", "toString", "getClass", "notify", "notifyAll", "wait", "finalize", "clone"}


def uleb(b, o):
    r = s = 0
    while True:
        x = b[o]; o += 1
        r |= (x & 0x7F) << s
        if not x & 0x80:
            return r, o
        s += 7


class Dex:
    def __init__(self, data):
        self.d = data
        (self.str_n, self.str_off, self.type_n, self.type_off, self.proto_n, self.proto_off,
         self.field_n, self.field_off, self.meth_n, self.meth_off, self.cdef_n, self.cdef_off) = struct.unpack_from("<12I", data, 0x38)
        self._s = {}

    def string(self, i):
        if i in self._s:
            return self._s[i]
        off = struct.unpack_from("<I", self.d, self.str_off + 4 * i)[0]
        _, o = uleb(self.d, off)
        e = self.d.index(b"\0", o)
        v = self.d[o:e].decode("utf-8", "replace")
        self._s[i] = v
        return v

    def type(self, i):
        return self.string(struct.unpack_from("<I", self.d, self.type_off + 4 * i)[0])

    def type_list(self, off):
        if not off:
            return []
        n = struct.unpack_from("<I", self.d, off)[0]
        return [self.type(struct.unpack_from("<H", self.d, off + 4 + 2 * k)[0]) for k in range(n)]

    def proto(self, i):
        _, ret, params = struct.unpack_from("<III", self.d, self.proto_off + 12 * i)
        return "(" + "".join(self.type_list(params)) + ")" + self.type(ret)

    def method(self, i):
        c, p, n = struct.unpack_from("<HHI", self.d, self.meth_off + 8 * i)
        return self.type(c), self.string(n), self.proto(p)

    def classes(self):
        """yield (descriptor, superclass, interfaces, [(name, proto)]) for every class defined here"""
        for k in range(self.cdef_n):
            cls, _acc, sup, ifs, _src, _ann, data_off, _sv = struct.unpack_from("<8I", self.d, self.cdef_off + 32 * k)
            methods = []
            if data_off:
                sf, inf, dm, vm = [0] * 4
                o = data_off
                sf, o = uleb(self.d, o); inf, o = uleb(self.d, o); dm, o = uleb(self.d, o); vm, o = uleb(self.d, o)
                for _ in range(sf + inf):
                    _, o = uleb(self.d, o); _, o = uleb(self.d, o)
                idx = 0
                for n in range(dm + vm):
                    diff, o = uleb(self.d, o); _, o = uleb(self.d, o); _, o = uleb(self.d, o)
                    if n == dm:
                        idx = 0
                    idx += diff
                    _c, name, proto = self.method(idx)
                    methods.append((name, proto))
            yield (self.type(cls), self.type(sup) if sup != 0xFFFFFFFF else None, self.type_list(ifs), methods)

    def referenced_methods(self):
        for i in range(self.meth_n):
            yield self.method(i)

    def referenced_types(self):
        for i in range(self.type_n):
            yield self.type(i)


def dexes_of(path):
    z = zipfile.ZipFile(path)
    return [Dex(z.read(n)) for n in sorted(z.namelist()) if n.startswith("classes") and n.endswith(".dex")]


def load_atl(jar):
    cls = {}
    for dx in dexes_of(jar):
        for desc, sup, ifs, methods in dx.classes():
            cls[desc] = (sup, ifs, set(methods))
    return cls


def find_method(cls, desc, name, proto):
    """True if ATL's class (or a parent) defines it. A parent outside android.* (other than
    java.lang.Object) comes from the JDK, which we cannot inspect here: assume it has the method."""
    if name in OBJECT_METHODS or desc == "Ljava/lang/Object;":
        return name in OBJECT_METHODS
    if not desc.startswith("Landroid/"):
        return True
    if desc not in cls:
        return False
    sup, ifs, ms = cls[desc]
    if (name, proto) in ms:
        return True
    for p in ([sup] if sup else []) + ifs:
        if p and find_method(cls, p, name, proto):
            return True
    return False


def check(apk, jar=None):
    jar = jar or next((j for j in DEFAULT_JARS if os.path.exists(j)), None)
    if not jar:
        raise SystemExit("api-impl.jar not found (use --jar)")
    atl = load_atl(jar)
    app_dexes = dexes_of(apk)
    defined = set()
    for dx in app_dexes:
        for desc, *_ in dx.classes():
            defined.add(desc)
    missing_classes, missing_methods = set(), {}
    for dx in app_dexes:
        for t in dx.referenced_types():
            if t.startswith("Landroid/") and t not in defined and t not in atl:
                missing_classes.add(t)
        for c, n, p in dx.referenced_methods():
            if c.startswith("Landroid/") and c not in defined and c in atl and not find_method(atl, c, n, p):
                missing_methods.setdefault(c, set()).add(n + p)
    return {
        "missing_classes": sorted(c[1:-1].replace("/", ".") for c in missing_classes),
        "missing_methods": {c[1:-1].replace("/", "."): sorted(v) for c, v in sorted(missing_methods.items())},
    }


def summary(r, limit=6):
    mc, mm = r["missing_classes"], r["missing_methods"]
    n = sum(len(v) for v in mm.values())
    if not mc and not n:
        return "Compatibility check: every Android class and method it references exists in ATL."
    parts = ["Compatibility check: %d Android classes and %d methods this app references are missing in ATL" % (len(mc), n)]
    if mc:
        parts.append("classes: " + ", ".join(c.split(".")[-1] for c in mc[:limit]) + (" …" if len(mc) > limit else ""))
    if mm:
        flat = ["%s.%s" % (c.split(".")[-1], m.split("(")[0]) for c, v in mm.items() for m in v]
        parts.append("methods: " + ", ".join(flat[:limit]) + (" …" if len(flat) > limit else ""))
    parts.append("(only a problem if the app runs them; a crash on start usually names one of these)")
    return "\n".join(parts)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    jar = sys.argv[sys.argv.index("--jar") + 1] if "--jar" in sys.argv else None
    if jar and jar in args:
        args.remove(jar)
    res = check(args[0], jar)
    if "--json" in sys.argv:
        print(json.dumps(res, indent=1))
    else:
        print(summary(res, 40))
        for c, ms in res["missing_methods"].items():
            for m in ms:
                print("  method %s.%s" % (c, m))
        for c in res["missing_classes"]:
            print("  class  %s" % c)
