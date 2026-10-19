#!/usr/bin/env python3
"""apkinfo: package, label, launcher activity, Android TV flag and a PNG launcher icon of an APK.

Standard library only (runs on the TV). Parses the binary AndroidManifest.xml and resources.arsc itself.
Icon: the highest-density PNG of the app's icon resource; for adaptive icons (XML) the PNG behind the foreground
layer, else a legacy PNG of the same resource, else the biggest launcher-looking PNG in the APK. Vector-only icons
give None (the caller uses a default icon).

  python3 apkinfo.py app.apk [icon-out.png]
"""
import json, struct, sys, zipfile

A_NAME, A_LABEL, A_ICON, A_DRAWABLE, A_ROUNDICON = 0x01010003, 0x01010001, 0x01010002, 0x01010199, 0x0101052C
MAIN = "android.intent.action.MAIN"
LAUNCHER = "android.intent.category.LAUNCHER"
LEANBACK = "android.intent.category.LEANBACK_LAUNCHER"


def u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def string_pool(b, o):
    """ResStringPool at offset o -> list of str"""
    hsz, size = u16(b, o + 2), u32(b, o + 4)
    n, flags, sstart = u32(b, o + 8), u32(b, o + 16), u32(b, o + 20)
    utf8 = flags & 0x100
    out = []
    for i in range(n):
        p = o + sstart + u32(b, o + hsz + 4 * i)
        try:
            if utf8:
                ln = b[p]
                p += 2 if ln & 0x80 else 1  # utf-16 length
                ln = b[p]
                if ln & 0x80:
                    ln = ((ln & 0x7F) << 8) | b[p + 1]
                    p += 2
                else:
                    p += 1
                out.append(b[p:p + ln].decode("utf-8", "replace"))
            else:
                ln = u16(b, p)
                p += 2
                if ln & 0x8000:
                    ln = ((ln & 0x7FFF) << 16) | u16(b, p)
                    p += 2
                out.append(b[p:p + 2 * ln].decode("utf-16-le", "replace"))
        except (IndexError, struct.error):
            out.append("")
    return out, size


def axml(b):
    """binary XML -> list of (event, tag, attrs) with attrs {name_or_resid: (type, data, raw str)}"""
    strings, resmap, ev = [], [], []
    o = u16(b, 2)
    while o + 8 <= len(b):
        t, hsz, size = u16(b, o), u16(b, o + 2), u32(b, o + 4)
        if size < 8:
            break
        if t == 0x0001:
            strings, _ = string_pool(b, o)
        elif t == 0x0180:
            resmap = [u32(b, o + 8 + 4 * i) for i in range((size - 8) // 4)]
        elif t == 0x0102:
            name = u32(b, o + 16 + 4)
            astart, asize, acount = u16(b, o + 16 + 8), u16(b, o + 16 + 10), u16(b, o + 16 + 12)
            attrs = {}
            for i in range(acount):
                p = o + 16 + astart + i * asize
                an, raw, dtype, data = u32(b, p + 4), u32(b, p + 8), b[p + 15], u32(b, p + 16)
                rs = strings[raw] if raw < len(strings) else None
                key = resmap[an] if an < len(resmap) and resmap[an] else (strings[an] if an < len(strings) else an)
                v = (dtype, data, rs)
                attrs[key] = v
                if an < len(strings):
                    attrs.setdefault(strings[an], v)
            ev.append(("start", strings[name] if name < len(strings) else "", attrs))
        elif t == 0x0103:
            name = u32(b, o + 16 + 4)
            ev.append(("end", strings[name] if name < len(strings) else "", {}))
        o += size
    return ev, strings


class Arsc:
    """resources.arsc: res id -> [(config dict, (type, data))], global strings"""

    def __init__(self, b):
        self.entries = {}
        self.strings = []
        o = u16(b, 2)
        while o + 8 <= len(b):
            t, size = u16(b, o), u32(b, o + 4)
            if t == 0x0001:
                self.strings, _ = string_pool(b, o)
            elif t == 0x0200:
                self._package(b, o, size)
            if size < 8:
                break
            o += size

    def _package(self, b, o, psize):
        pid = u32(b, o + 8)
        hsz = u16(b, o + 2)
        p = o + hsz
        end = o + psize
        while p + 8 <= end:
            t, h, size = u16(b, p), u16(b, p + 2), u32(b, p + 4)
            if size < 8:
                break
            if t == 0x0201:
                self._type(b, p, h, pid)
            p += size

    def _type(self, b, p, h, pid):
        tid, flags = b[p + 8], b[p + 9]
        count, estart = u32(b, p + 12), u32(b, p + 16)
        c = p + 20
        csize = u32(b, c)
        cfg = {"lang": b[c + 8:c + 10].rstrip(b"\0").decode("latin-1") if csize >= 12 else "",
               "density": u16(b, c + 14) if csize >= 16 else 0}
        offs = []
        if flags & 0x01:  # sparse: (index u16, offset/4 u16)
            for i in range(count):
                idx, off = u16(b, p + h + 4 * i), u16(b, p + h + 4 * i + 2)
                offs.append((idx, off * 4))
        elif flags & 0x02:  # offset16
            for i in range(count):
                off = u16(b, p + h + 2 * i)
                if off != 0xFFFF:
                    offs.append((i, off * 4))
        else:
            for i in range(count):
                off = u32(b, p + h + 4 * i)
                if off != 0xFFFFFFFF:
                    offs.append((i, off))
        for idx, off in offs:
            e = p + estart + off
            try:
                esize, eflags = u16(b, e), u16(b, e + 2)
                if eflags & 0x0001:  # complex (bag): not needed
                    continue
                if eflags & 0x0008:  # compact entry: data in the key field
                    val = (eflags >> 8, u32(b, e + 4))
                else:
                    v = e + esize
                    val = (b[v + 3], u32(b, v + 4))
            except (IndexError, struct.error):
                continue
            rid = (pid << 24) | (tid << 16) | idx
            self.entries.setdefault(rid, []).append((cfg, val))

    def values(self, rid, depth=0):
        """[(cfg, (type, data))] following references"""
        out = []
        for cfg, (t, d) in self.entries.get(rid, []):
            if t == 0x01 and depth < 5 and d != rid:
                out += [(cfg, v) for _, v in self.values(d, depth + 1)]
            else:
                out.append((cfg, (t, d)))
        return out

    def string(self, rid):
        vals = [(c, v) for c, v in self.values(rid) if v[0] == 0x03]
        vals.sort(key=lambda cv: (cv[0]["lang"] not in ("", "en"), cv[0]["lang"] != ""))
        return self.strings[vals[0][1][1]] if vals and vals[0][1][1] < len(self.strings) else None

    def files(self, rid):
        """[(density, path)] best density first (anydpi xml last among same)"""
        out = []
        for c, (t, d) in self.values(rid):
            if t == 0x03 and d < len(self.strings):
                out.append((c["density"], self.strings[d]))
        dscore = lambda den: -1 if den in (0xFFFE, 0xFFFF) else den  # anydpi / nodpi
        out.sort(key=lambda dp: (dp[1].endswith(".png"), dscore(dp[0])), reverse=True)
        return out


def info(apk):
    r = {"package": None, "label": None, "activity": None, "leanback": False, "icon_png": None, "icon_source": None}
    with zipfile.ZipFile(apk) as z:
        names = set(z.namelist())
        ev, _ = axml(z.read("AndroidManifest.xml"))
        arsc = Arsc(z.read("resources.arsc")) if "resources.arsc" in names else None
        app_label = app_icon = None
        stack, cur_act, filt = [], None, None
        acts = []
        for kind, tag, at in ev:
            if kind == "start":
                stack.append(tag)
                if tag == "manifest":
                    v = at.get("package")
                    r["package"] = v[2] if v else None
                elif tag == "application":
                    app_label, app_icon = at.get(A_LABEL) or at.get("label"), at.get(A_ICON) or at.get("icon")
                elif tag in ("activity", "activity-alias"):
                    n = at.get(A_NAME) or at.get("name")
                    cur_act = {"name": n[2] if n else None, "main": False, "launcher": False, "leanback": False}
                    acts.append(cur_act)
                elif tag == "intent-filter":
                    filt = set()
                elif tag in ("action", "category") and filt is not None:
                    n = at.get(A_NAME) or at.get("name")
                    if n and n[2]:
                        filt.add(n[2])
                elif tag == "uses-feature":
                    n = at.get(A_NAME) or at.get("name")
                    if n and n[2] == "android.software.leanback":
                        r["leanback"] = r["leanback"] or None  # declares TV support; LEANBACK_LAUNCHER decides
            else:
                if tag == "intent-filter" and filt is not None and cur_act is not None:
                    if MAIN in filt:
                        cur_act["main"] = True
                        cur_act["launcher"] |= LAUNCHER in filt
                        cur_act["leanback"] |= LEANBACK in filt
                    filt = None
                elif tag in ("activity", "activity-alias"):
                    cur_act = None
                if stack:
                    stack.pop()
        pick = [a for a in acts if a["leanback"]] or [a for a in acts if a["launcher"]] or [a for a in acts if a["main"]]
        r["leanback"] = any(a["leanback"] for a in acts)
        if pick and pick[0]["name"]:
            n = pick[0]["name"]
            if n.startswith(".") and r["package"]:
                n = r["package"] + n
            r["activity"] = n.replace(".", "/") if "/" not in n else n
        if app_label:
            t, d, raw = app_label
            if t == 0x03 and raw:
                r["label"] = raw
            elif t == 0x01 and arsc:
                r["label"] = arsc.string(d)
        r["icon_png"], r["icon_source"] = _icon(z, names, arsc, app_icon)
    return r


def _png(z, path):
    try:
        d = z.read(path)
    except KeyError:
        return None
    return d if d[:8] == b"\x89PNG\r\n\x1a\n" else None


def _icon(z, names, arsc, ref):
    if arsc and ref and ref[0] == 0x01:
        files = arsc.files(ref[1])
        for _, p in files:  # PNGs first, best density first
            d = _png(z, p) if p.endswith(".png") else None
            if d:
                return d, p
        for _, p in files:  # adaptive icon: foreground layer
            if p.endswith(".xml") and p in names:
                try:
                    ev, _ = axml(z.read(p))
                except Exception:
                    continue
                for kind, tag, at in ev:
                    if kind == "start" and tag in ("foreground", "background"):
                        v = at.get(A_DRAWABLE) or at.get("drawable")
                        if v and v[0] == 0x01:
                            for _, fp in arsc.files(v[1]):
                                d = _png(z, fp) if fp.endswith(".png") else None
                                if d:
                                    return d, fp + " (adaptive " + tag + ")"
    best, size = None, 0
    for n in names:
        ln = n.lower()
        if ln.startswith("res/") and ln.endswith(".png") and "ic_launcher" in ln and "background" not in ln:
            s = z.getinfo(n).file_size
            if size < s < 400000:
                best, size = n, s
    if best:
        return z.read(best), best + " (guess)"
    return None, None


if __name__ == "__main__":
    i = info(sys.argv[1])
    png = i.pop("icon_png")
    if len(sys.argv) > 2 and png:
        open(sys.argv[2], "wb").write(png)
    i["icon_bytes"] = len(png) if png else 0
    print(json.dumps(i, indent=1))
