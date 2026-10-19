#!/usr/bin/env python3
"""apkicon: an APK's real launcher icon, rendered at high resolution, plus the "runs on ATL" badge.

Android launcher icons today are mostly *adaptive icons* (res/mipmap-anydpi-v26/ic_launcher.xml, often renamed to
something like res/BW.xml by R8): a background + foreground layer, each a colour, a bitmap or a *vector drawable*.
apkicon resolves them through resources.arsc (highest density first), converts vector drawables to SVG and
rasterises them with cairosvg at the target size, so icons are crisp at any size instead of a 48 px PNG blown up.

Supported drawables: adaptive-icon, vector (paths, groups, clip-paths, linear/radial/sweep gradients, alpha,
fillType, strokes), inset, layer-list, bitmap, shape (rectangle/oval solid), color, selector/level-list (first item),
plain PNG/WebP/JPEG. Legacy (non-adaptive) icons are centred on a matching background.

Output: a full-bleed square PNG (the launcher applies its own squircle mask), optionally with the badge: a green
Android head in a white disc, bottom-left.

API:  info(apk_path, size=512, badge=True) -> dict(package, label, version_name, version_code, activity, leanback,
                                                 icon_png: bytes, icon_plain_png: bytes, icon_source: str)
CLI:  apkicon.py <apk> [out.png] [size]
"""
import io, math, re, sys, zipfile
import xml.etree.ElementTree as ET
import logging

logging.disable(logging.CRITICAL)
from androguard.core.apk import APK                      # noqa: E402
from androguard.core.axml import AXMLPrinter             # noqa: E402
from PIL import Image, ImageDraw, ImageFilter            # noqa: E402
import cairosvg                                          # noqa: E402

A = "{http://schemas.android.com/apk/res/android}"
DENS = {"ldpi": 120, "mdpi": 160, "tvdpi": 213, "hdpi": 240, "xhdpi": 320, "xxhdpi": 480, "xxxhdpi": 640, "anydpi": 65534,
        "nodpi": 1}


class Res:
    def __init__(self, apk):
        self.apk = apk
        self.arsc = apk.get_android_resources()
        self.pkg = apk.get_package()
        self.files = set(apk.get_files())

    def _configs(self, rid):
        try:
            return self.arsc.get_resolved_res_configs(rid) or []
        except Exception:
            return []

    def resolve(self, ref):
        """'@7f080058' / '@android:color/x' / '#ff00ff' -> ('color', (r,g,b,a)) | ('file', path) | ('dimen', px) | (None,None)"""
        if ref is None:
            return None, None
        ref = str(ref).strip()
        if ref.startswith("#"):
            return "color", parse_color(ref)
        m = re.match(r"^@(?:android:)?([0-9a-fA-F]{8})$", ref)
        if not m:
            m2 = re.match(r"^@\+?(?:android:)?([a-z]+)/(.+)$", ref)
            if m2 and m2.group(1) == "color":
                return "color", ANDROID_COLORS.get(m2.group(2), (0, 0, 0, 255))
            return None, None
        rid = int(m.group(1), 16)
        if rid >> 24 == 0x01:  # framework resource
            if rid in FRAMEWORK_COLORS:
                return "color", FRAMEWORK_COLORS[rid]
            return "color", (255, 255, 255, 255) if (rid >> 16) == 0x0106 else (None, None)
        if not self._configs(rid):  # androguard only "resolves" strings/files; colours/refs need the raw entry
            raw = self._raw(rid)
            if raw is not None:
                return raw
        best, bestd = None, -1
        for cfg, val in self._configs(rid):
            try:
                d = cfg.get_density()
            except Exception:
                d = 0
            d = 65534 if d in (0xfffe,) else (160 if d == 0 else d)
            if d > bestd:
                best, bestd = val, d
        if best is None:
            return None, None
        v = str(best)
        if v.startswith("res/") and v in self.files:
            return "file", v
        if v.startswith("#"):
            return "color", parse_color(v)
        if v.startswith("@"):
            return self.resolve(v)
        if re.match(r"^-?[0-9.]+(dip|dp|px|sp)$", v):
            return "dimen", float(re.sub(r"[a-z]+$", "", v))
        return None, None

    def _raw(self, rid, depth=0):
        try:
            cfgs = self.arsc.get_res_configs(rid)
        except Exception:
            return None
        if not cfgs or depth > 6:
            return None
        ent = cfgs[0][1]
        h = getattr(ent, "item", None) or getattr(ent, "key", None)
        for name in ("holding",):
            h = getattr(ent, name, h)
        if h is None:
            return None
        t = getattr(h, "data_type", None)
        d = getattr(h, "data", None)
        if t is None:
            try:
                t, d = h.get_data_type(), h.get_data()
            except Exception:
                return None
        if t == 0x01:  # reference
            if d >> 24 == 0x01:
                return "color", FRAMEWORK_COLORS.get(d, (255, 255, 255, 255))
            r = self._raw(d, depth + 1)
            if r is not None:
                return r
            return self.resolve("@%08x" % d)
        if t in (0x1c, 0x1d):  # #aarrggbb / #rrggbb
            v = d if t == 0x1c else (d | 0xff000000)
            return "color", ((v >> 16) & 255, (v >> 8) & 255, v & 255, (v >> 24) & 255)
        if t in (0x1e, 0x1f):  # #argb / #rgb
            a, r, g, b = (d >> 12) & 15, (d >> 8) & 15, (d >> 4) & 15, d & 15
            return "color", (r * 17, g * 17, b * 17, a * 17 if t == 0x1e else 255)
        return None

    def xml(self, path):
        data = self.apk.get_file(path)
        try:
            return ET.fromstring(AXMLPrinter(data).get_xml())
        except Exception:
            return None


ANDROID_COLORS = {"white": (255, 255, 255, 255), "black": (0, 0, 0, 255), "transparent": (0, 0, 0, 0)}
# android.R.color.* by resource id (framework ids are stable): background_dark .. transparent
FRAMEWORK_COLORS = {0x01060000: (0, 0, 0, 255), 0x01060001: (255, 255, 255, 255), 0x01060002: (170, 170, 170, 255),
                    0x01060003: (0, 0, 0, 255), 0x01060004: (190, 190, 190, 255), 0x01060005: (128, 128, 128, 255),
                    0x01060006: (255, 255, 255, 255), 0x01060007: (0, 0, 0, 255), 0x01060008: (255, 255, 255, 255),
                    0x01060009: (0, 0, 0, 255), 0x0106000a: (128, 128, 128, 255), 0x0106000b: (255, 255, 255, 255),
                    0x0106000c: (0, 0, 0, 255), 0x0106000d: (0, 0, 0, 0), 0x01060012: (51, 181, 229, 255),
                    0x01060013: (0, 153, 204, 255), 0x01060014: (153, 204, 0, 255), 0x01060015: (102, 153, 0, 255),
                    0x01060016: (255, 187, 51, 255), 0x01060017: (255, 136, 0, 255), 0x01060018: (255, 68, 68, 255),
                    0x01060019: (204, 0, 0, 255), 0x0106001a: (170, 102, 204, 255), 0x0106001b: (153, 51, 204, 255)}


def parse_color(s):
    s = s.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) == 4:
        s = "".join(c * 2 for c in s)
    if len(s) == 6:
        s = "ff" + s
    try:
        v = int(s, 16)
    except ValueError:
        return (0, 0, 0, 255)
    return ((v >> 16) & 255, (v >> 8) & 255, v & 255, (v >> 24) & 255)


def css(c):
    return "rgb(%d,%d,%d)" % c[:3], c[3] / 255.0


def fnum(v, default=0.0):
    if v is None:
        return default
    try:
        return float(re.sub(r"[a-z%]+$", "", str(v)))
    except ValueError:
        return default


# ------------------------------------------------------------------ vector drawable -> SVG
class VecConv:
    def __init__(self, res):
        self.res = res
        self.defs = []
        self.n = 0

    def paint(self, el, attr, alpha_attr):
        """returns (svg paint string, opacity) for fillColor/strokeColor, incl. gradient resources"""
        v = el.get(A + attr)
        op = fnum(el.get(A + alpha_attr), 1.0)
        if v is None:
            # inline <aapt:attr name="android:fillColor"><gradient/></aapt:attr> is compiled to a resource; nothing here
            return None, op
        kind, val = self.res.resolve(v) if not v.startswith("#") else ("color", parse_color(v))
        if kind == "color":
            c, a = css(val)
            return c, op * a
        if kind == "file":
            g = self.res.xml(val)
            if g is not None:
                if g.tag == "gradient":
                    return "url(#%s)" % self.gradient(g), op
                if g.tag == "selector":  # color state list: default item
                    for it in g:
                        k2, v2 = self.res.resolve(it.get(A + "color"))
                        if k2 == "color":
                            c, a = css(v2)
                            return c, op * a * fnum(it.get(A + "alpha"), 1.0)
        return None, op

    def gradient(self, g):
        self.n += 1
        gid = "g%d" % self.n
        t = g.get(A + "type", "0")
        stops = []
        items = [it for it in g if it.tag == "item"]
        if items:
            for it in items:
                k, c = self.res.resolve(it.get(A + "color"))
                stops.append((fnum(it.get(A + "offset")), c if k == "color" else (0, 0, 0, 255)))
        else:
            for key, off in (("startColor", 0), ("centerColor", .5), ("endColor", 1)):
                if g.get(A + key):
                    k, c = self.res.resolve(g.get(A + key))
                    stops.append((off, c if k == "color" else parse_color(g.get(A + key))))
        st = "".join('<stop offset="%g" stop-color="rgb(%d,%d,%d)" stop-opacity="%g"/>' % (o, *c[:3], c[3] / 255) for o, c in stops)
        if t in ("1", "radial"):
            self.defs.append('<radialGradient id="%s" gradientUnits="userSpaceOnUse" cx="%g" cy="%g" r="%g">%s</radialGradient>' % (
                gid, fnum(g.get(A + "centerX")), fnum(g.get(A + "centerY")), fnum(g.get(A + "gradientRadius"), 1), st))
        else:  # linear (sweep approximated as linear)
            self.defs.append('<linearGradient id="%s" gradientUnits="userSpaceOnUse" x1="%g" y1="%g" x2="%g" y2="%g">%s</linearGradient>' % (
                gid, fnum(g.get(A + "startX")), fnum(g.get(A + "startY")), fnum(g.get(A + "endX")), fnum(g.get(A + "endY")), st))
        return gid

    def node(self, el):
        out = []
        clip = None
        for ch in el:
            tag = ch.tag
            if tag == "path":
                d = ch.get(A + "pathData")
                if not d:
                    continue
                fill, fo = self.paint(ch, "fillColor", "fillAlpha")
                stroke, so = self.paint(ch, "strokeColor", "strokeAlpha")
                rule = "evenodd" if ch.get(A + "fillType") in ("1", "evenOdd") else "nonzero"
                attrs = ['d="%s"' % d.replace('"', ""), 'fill="%s"' % (fill or "none"), 'fill-opacity="%g"' % fo, 'fill-rule="%s"' % rule]
                if stroke:
                    caps = {"0": "butt", "1": "round", "2": "square"}
                    joins = {"0": "miter", "1": "round", "2": "bevel"}
                    attrs += ['stroke="%s"' % stroke, 'stroke-opacity="%g"' % so, 'stroke-width="%g"' % fnum(ch.get(A + "strokeWidth"), 1),
                              'stroke-linecap="%s"' % caps.get(ch.get(A + "strokeLineCap", "0"), "butt"),
                              'stroke-linejoin="%s"' % joins.get(ch.get(A + "strokeLineJoin", "0"), "miter")]
                if clip:
                    attrs.append('clip-path="url(#%s)"' % clip)
                out.append("<path %s/>" % " ".join(attrs))
            elif tag == "group":
                tx, ty = fnum(ch.get(A + "translateX")), fnum(ch.get(A + "translateY"))
                sx, sy = fnum(ch.get(A + "scaleX"), 1), fnum(ch.get(A + "scaleY"), 1)
                rot, px, py = fnum(ch.get(A + "rotation")), fnum(ch.get(A + "pivotX")), fnum(ch.get(A + "pivotY"))
                # Android: T(translate+pivot) * R * S * T(-pivot)
                tr = "translate(%g %g) rotate(%g) scale(%g %g) translate(%g %g)" % (tx + px, ty + py, rot, sx, sy, -px, -py)
                inner = self.node(ch)
                g = '<g transform="%s"%s>%s</g>' % (tr, ' clip-path="url(#%s)"' % clip if clip else "", inner)
                out.append(g)
            elif tag == "clip-path":
                d = ch.get(A + "pathData")
                if d:
                    self.n += 1
                    clip = "c%d" % self.n
                    self.defs.append('<clipPath id="%s"><path d="%s"/></clipPath>' % (clip, d))
        return "".join(out)

    def svg(self, vec, px):
        vw, vh = fnum(vec.get(A + "viewportWidth"), 24), fnum(vec.get(A + "viewportHeight"), 24)
        body = self.node(vec)
        alpha = fnum(vec.get(A + "alpha"), 1)
        tint = None
        if vec.get(A + "tint"):
            k, c = self.res.resolve(vec.get(A + "tint"))
            tint = c if k == "color" else None
        s = ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %g %g"><defs>%s</defs><g opacity="%g">%s</g></svg>'
             % (px, px, vw, vh, "".join(self.defs), alpha, body))
        return s, tint


# ------------------------------------------------------------------ drawable -> PIL image
def render(res, ref, size, depth=0):
    """render drawable reference/file at size x size, RGBA; None if unsupported"""
    if depth > 8:
        return None
    kind, val = (("file", ref) if isinstance(ref, str) and ref.startswith("res/") else res.resolve(ref))
    if kind == "color":
        return Image.new("RGBA", (size, size), val)
    if kind != "file":
        return None
    if re.search(r"\.(png|webp|jpe?g)$", val, re.I):
        try:
            im = Image.open(io.BytesIO(res.apk.get_file(val))).convert("RGBA")
            return im.resize((size, size), Image.LANCZOS)
        except Exception:
            return None
    root = res.xml(val)
    if root is None:
        return None
    return render_xml(res, root, size, depth)


def render_xml(res, root, size, depth):
    tag = root.tag
    if tag == "vector":
        vc = VecConv(res)
        svg, tint = vc.svg(root, size)
        try:
            im = Image.open(io.BytesIO(cairosvg.svg2png(bytestring=svg.encode(), output_width=size, output_height=size))).convert("RGBA")
        except Exception:
            return None
        if tint:
            solid = Image.new("RGBA", im.size, tint[:3] + (255,))
            solid.putalpha(im.getchannel("A"))
            im = solid
        return im
    if tag == "adaptive-icon":
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        for layer in ("background", "foreground"):
            el = root.find(layer)
            if el is None:
                continue
            ref = el.get(A + "drawable")
            im = render(res, ref, size, depth + 1) if ref else (render_xml(res, el[0], size, depth + 1) if len(el) else None)
            if im is not None:
                canvas.alpha_composite(im)
        # visible part of an adaptive icon: the central 72dp of 108dp (launchers mask inside it)
        c = size * 18 // 108
        return canvas.crop((c, c, size - c, size - c)).resize((size, size), Image.LANCZOS)
    if tag == "inset":
        ref = root.get(A + "drawable")
        frac = []
        for k in ("insetLeft", "insetTop", "insetRight", "insetBottom"):
            v = root.get(A + k) or root.get(A + "inset") or "0"
            if v.endswith("%"):
                frac.append(fnum(v) / 100)
            else:
                kd, dv = res.resolve(v) if v.startswith("@") else ("dimen", fnum(v))
                frac.append((dv if kd == "dimen" else 0) / 108.0)  # foreground layers are 108dp
        l, t, r, b = [int(round(f * size)) for f in frac]
        w, h = max(1, size - l - r), max(1, size - t - b)
        inner = render(res, ref, max(w, h), depth + 1) if ref else (render_xml(res, root[0], max(w, h), depth + 1) if len(root) else None)
        if inner is None:
            return None
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.alpha_composite(inner.resize((w, h), Image.LANCZOS), (l, t))
        return canvas
    if tag == "layer-list":
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        for it in root:
            if it.tag != "item":
                continue
            ref = it.get(A + "drawable")
            ins = [int(fnum(it.get(A + k)) / 108.0 * size) for k in ("left", "top", "right", "bottom")]
            w, h = max(1, size - ins[0] - ins[2]), max(1, size - ins[1] - ins[3])
            im = render(res, ref, max(w, h), depth + 1) if ref else (render_xml(res, it[0], max(w, h), depth + 1) if len(it) else None)
            if im is not None:
                canvas.alpha_composite(im.resize((w, h), Image.LANCZOS), (ins[0], ins[1]))
        return canvas
    if tag == "bitmap":
        return render(res, root.get(A + "src"), size, depth + 1)
    if tag in ("selector", "level-list", "animation-list", "transition"):
        for it in root:
            ref = it.get(A + "drawable")
            if ref:
                return render(res, ref, size, depth + 1)
            if len(it):
                return render_xml(res, it[0], size, depth + 1)
        return None
    if tag == "shape":
        solid = root.find("solid")
        grad = root.find("gradient")
        if solid is None and grad is not None:
            cols = []
            for key in ("startColor", "centerColor", "endColor"):
                if grad.get(A + key):
                    k, c = res.resolve(grad.get(A + key))
                    if k == "color":
                        cols.append(c)
            if not cols:
                return None
            if len(cols) == 1:
                cols.append(cols[0])
            ang = fnum(grad.get(A + "angle"), 0) % 360
            g = Image.new("RGBA", (size, 1))
            for x in range(size):
                t = x / max(1, size - 1) * (len(cols) - 1)
                i = min(int(t), len(cols) - 2)
                f = t - i
                g.putpixel((x, 0), tuple(int(cols[i][j] + (cols[i + 1][j] - cols[i][j]) * f) for j in range(4)))
            g = g.resize((size, size))
            # Android angle: 0 = left->right, 90 = bottom->top (counter-clockwise)
            return g.rotate(ang, resample=Image.BICUBIC, expand=False, fillcolor=cols[-1])
        if solid is None:
            return None
        k, c = res.resolve(solid.get(A + "color"))
        if k != "color":
            return None
        im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        if root.get(A + "shape") in ("1", "oval"):
            d.ellipse((0, 0, size - 1, size - 1), fill=c)
        else:
            d.rectangle((0, 0, size, size), fill=c)
        return im
    if tag == "color":
        k, c = res.resolve(root.get(A + "color"))
        return Image.new("RGBA", (size, size), c) if k == "color" else None
    return None


# ------------------------------------------------------------------ icon selection
def icon_candidates(apk, res):
    """icon resource references, best first: activity-alias/activity icon of the launcher, application icon/roundIcon"""
    out = []
    try:
        man = apk.get_android_manifest_xml()
        app = man.find("application")
        for el in list(app.iter("activity")) + list(app.iter("activity-alias")):
            if el.get(A + "enabled") in ("false", "0"):   # e.g. F-Droid's hidden "calculator" disguise launcher
                continue
            for f in el.iter("intent-filter"):
                cats = {c.get(A + "name") for c in f.iter("category")}
                if cats & {"android.intent.category.LEANBACK_LAUNCHER"}:
                    for k in ("banner", "icon"):
                        pass  # banners are wide 16:9 art; we want the square icon
                if cats & {"android.intent.category.LAUNCHER", "android.intent.category.LEANBACK_LAUNCHER"} and el.get(A + "icon"):
                    out.append(el.get(A + "icon"))
        for k in ("icon", "roundIcon"):
            if app.get(A + k):
                out.append(app.get(A + k))
    except Exception:
        pass
    return out


def best_png(apk):
    """legacy fallback: biggest launcher-looking PNG"""
    best, bsz = None, 0
    for n in apk.get_files():
        if not re.search(r"\.(png|webp)$", n) or not n.startswith("res/"):
            continue
        if not re.search(r"(ic_launcher|app_icon|/icon\.|launcher)", n) or re.search(r"(foreground|background|monochrome|notification)", n):
            continue
        try:
            w = Image.open(io.BytesIO(apk.get_file(n))).size[0]
        except Exception:
            continue
        if w > bsz:
            best, bsz = n, w
    return best


def full_bleed(im, size):
    """legacy icons have their own shape/transparency: centre them on a background in their dominant edge colour"""
    im = im.convert("RGBA")
    a = im.getchannel("A")
    if a.getextrema()[0] > 250:  # already opaque square art
        return im.resize((size, size), Image.LANCZOS)
    small = im.resize((32, 32))
    px = [small.getpixel((x, y)) for x in range(32) for y in range(32) if small.getpixel((x, y))[3] > 200]
    if px:
        px.sort(key=lambda p: sum(p[:3]))
        r, g, b = [sum(p[i] for p in px) // len(px) for i in range(3)]
        lum = 0.3 * r + 0.59 * g + 0.11 * b
        bg = (r // 4, g // 4, b // 4, 255) if lum > 140 else (min(255, r + 200) // 3 + 160, min(255, g + 200) // 3 + 160, min(255, b + 200) // 3 + 160, 255)
        bg = (24, 26, 32, 255) if lum > 90 else (236, 238, 242, 255)
    else:
        bg = (24, 26, 32, 255)
    canvas = Image.new("RGBA", (size, size), bg)
    inner = int(size * 0.80)
    canvas.alpha_composite(im.resize((inner, inner), Image.LANCZOS), ((size - inner) // 2, (size - inner) // 2))
    return canvas


# ------------------------------------------------------------------ badge
def badge(icon):
    """green Android head in a white disc, bottom-left (~30 % of the icon), drawn 4x and downsampled"""
    S = icon.size[0]
    d = int(S * 0.30)
    k = 4
    D = d * k
    b = Image.new("RGBA", (D, D), (0, 0, 0, 0))
    g = ImageDraw.Draw(b)
    g.ellipse((0, 0, D - 1, D - 1), fill=(255, 255, 255, 255))
    green = (61, 220, 132, 255)
    # head: upper half-disc
    cx, cy, r = D / 2, D * 0.60, D * 0.30
    g.pieslice((cx - r, cy - r, cx + r, cy + r), 180, 360, fill=green)
    # antennae
    w = D * 0.035
    for sgn in (-1, 1):
        x0, y0 = cx + sgn * r * 0.45, cy - r * 0.80
        x1, y1 = cx + sgn * r * 0.75, cy - r * 1.25
        g.line((x0, y0, x1, y1), fill=green, width=int(w * 2))
        g.ellipse((x1 - w, y1 - w, x1 + w, y1 + w), fill=green)
    # eyes
    er = D * 0.035
    for sgn in (-1, 1):
        ex, ey = cx + sgn * r * 0.42, cy - r * 0.40
        g.ellipse((ex - er, ey - er, ex + er, ey + er), fill=(255, 255, 255, 255))
    b = b.resize((d, d), Image.LANCZOS)
    # soft shadow
    sh = Image.new("RGBA", (d + 16, d + 16), (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse((8, 10, d + 8, d + 10), fill=(0, 0, 0, 110))
    sh = sh.filter(ImageFilter.GaussianBlur(5))
    out = icon.copy()
    m = int(S * 0.06)
    out.alpha_composite(sh, (m - 8, S - d - m - 8))
    out.alpha_composite(b, (m, S - d - m))
    return out


# ------------------------------------------------------------------ main entry
def info(apk_path, size=512, with_badge=True):
    apk = APK(apk_path)
    res = Res(apk)
    icon, src = None, ""
    for ref in icon_candidates(apk, res):
        im = render(res, ref, size)
        if im is not None and im.getchannel("A").getextrema()[1] > 0:
            kind, val = res.resolve(ref)
            src = val if kind == "file" else str(ref)
            root = res.xml(val) if kind == "file" and val.endswith(".xml") else None
            icon = im if root is not None and root.tag == "adaptive-icon" else full_bleed(im, size)
            break
    if icon is None:
        p = best_png(apk)
        if p:
            icon, src = full_bleed(Image.open(io.BytesIO(apk.get_file(p))), size), p
    if icon is None:
        icon, src = Image.new("RGBA", (size, size), (37, 41, 50, 255)), "none"
    icon = icon.convert("RGBA")
    plain = io.BytesIO(); icon.save(plain, "PNG", optimize=True)
    out = io.BytesIO(); (badge(icon) if with_badge else icon).save(out, "PNG", optimize=True)
    lean = False
    try:
        man = apk.get_android_manifest_xml()
        lean = any(c.get(A + "name") == "android.intent.category.LEANBACK_LAUNCHER" for c in man.iter("category"))
    except Exception:
        pass
    try:
        act = apk.get_main_activity()
    except Exception:
        act = None
    # dominant colour for the tile background behind (rare) transparent pixels
    sm = icon.resize((8, 8)).convert("RGB")
    r, g, b = [sum(sm.getpixel((x, y))[i] for x in range(8) for y in range(8)) // 64 for i in range(3)]
    return {"package": apk.get_package(), "label": apk.get_app_name() or apk.get_package(),
            "version_name": apk.get_androidversion_name(), "version_code": apk.get_androidversion_code(),
            "activity": act, "leanback": lean, "icon_source": src, "color": "#%02x%02x%02x" % (r, g, b),
            "icon_png": out.getvalue(), "icon_plain_png": plain.getvalue()}


if __name__ == "__main__":
    r = info(sys.argv[1], int(sys.argv[3]) if len(sys.argv) > 3 else 512)
    if len(sys.argv) > 2:
        open(sys.argv[2], "wb").write(r["icon_png"])
    print({k: v for k, v in r.items() if not k.endswith("_png")})
