#!/usr/bin/env python3
"""bundle.py <deb-root> <out-root>: assemble the self-contained TV browser tree (becomes browser.sqfs).

Source: the Debian trixie armhf rootfs the phase-2 bench ran Chromium 150 from (~/browser/deb-root).
Contents:
  - every file Chromium mapped or opened during the bench runs (bench/out/chromium/*.json "files"),
  - /usr/lib/chromium (en-US locale only; no Vulkan validation/mock layers),
  - the ELF NEEDED closure of every library/executable in the tree, plus what Chromium dlopen()s
    (libpulse/libasound for audio, NSS modules, GTK3), each with its loader ld-linux-armhf.so.3,
  - fontconfig (conf.d resolved, fonts.conf pointing into the mount), fonts, ALSA config,
  - ghb/: launch.sh, brwl, gbfix.so, Preferences, VERSION, start page, taken from $GHB_FILES (build.sh fills it).
Chromium and chrome_crashpad_handler get PT_INTERP = $GHB_MNT/usr/lib/ld-linux-armhf.so.3 (default
/media/developer/webosatl/chromium/mnt, where the manager mounts browser.sqfs)
(patchelf), so /proc/self/exe stays Chromium (its child processes re-exec it) and the TV's own soft-float loader and
libraries are never used. Writes <out-root>/../bundle.json (file count, sizes, what was added why).
"""
import glob, json, os, shutil, subprocess, sys

SRC, OUT = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
H = os.path.expanduser("~")
MNT = os.environ.get("GHB_MNT", "/media/developer/webosatl/chromium/mnt")
INTERP = MNT + "/usr/lib/ld-linux-armhf.so.3"
PATCHELF = os.environ.get("PATCHELF", H + "/browser/venv/bin/patchelf")
GHB = os.environ["GHB_FILES"]          # dir with launch.sh brwl gbfix.so Preferences start/
LIBDIRS = ["/usr/lib/arm-linux-gnueabihf", "/usr/lib/chromium", "/usr/lib/arm-linux-gnueabihf/pulseaudio",
           "/usr/lib/arm-linux-gnueabihf/nss"]
DLOPEN = ["libpulse.so.0", "libasound.so.2", "libgtk-3.so.0", "libnss3.so", "libsoftokn3.so", "libfreebl3.so",
          "libfreeblpriv3.so", "libnssckbi.so", "libnssdbm3.so", "libxkbcommon.so.0", "libwayland-client.so.0",
          "libwayland-cursor.so.0", "libwayland-egl.so.1", "libgbm.so.1", "libdrm.so.2"]
why = {}


def add(rel, reason):
    """copy SRC+rel (file, symlink chain, or dir) to OUT+rel"""
    rel = "/" + rel.lstrip("/")
    src, dst = SRC + rel, OUT + rel
    if os.path.lexists(dst) or not os.path.lexists(src):
        return os.path.lexists(dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.islink(src):
        t = os.readlink(src)
        os.symlink(t, dst)
        why[rel] = reason
        tgt = t if t.startswith("/") else os.path.normpath(os.path.join(os.path.dirname(rel), t))
        add(tgt, "symlink target of " + rel)
        return True
    if os.path.isdir(src):
        for dp, dn, fn in os.walk(src):
            for f in fn:
                add(os.path.join(dp, f)[len(SRC):], reason)
        return True
    shutil.copy2(src, dst)
    why[rel] = reason
    return True


def real(rel):
    """rel path with every symlink (also in parent dirs, e.g. /lib -> usr/lib) resolved inside SRC"""
    parts, cur = rel.strip("/").split("/"), ""
    for i, p in enumerate(parts):
        nxt = cur + "/" + p
        for _ in range(20):
            s = SRC + nxt
            if not os.path.islink(s):
                break
            t = os.readlink(s)
            nxt = os.path.normpath(t if t.startswith("/") else os.path.join(os.path.dirname(nxt), t))
        cur = nxt
    return cur


def needed(path):
    r = subprocess.run(["readelf", "-dW", path], capture_output=True, text=True)
    return [ln.split("[", 1)[1].split("]", 1)[0] for ln in r.stdout.splitlines() if "(NEEDED)" in ln]


def is_elf(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def find_lib(name):
    for d in LIBDIRS:
        if os.path.lexists(SRC + d + "/" + name):
            return d + "/" + name
    return None


shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT)
# 0. our files (before the NEEDED closure: gbfix.so needs libdl)
os.makedirs(OUT + "/ghb", exist_ok=True)
for f in ("launch.sh", "brwl", "gbfix.so", "Preferences", "VERSION"):
    shutil.copy2(GHB + "/" + f, OUT + "/ghb/" + f)
shutil.copytree(GHB + "/start", OUT + "/ghb/start")
for f in ("launch.sh", "brwl"):
    os.chmod(OUT + "/ghb/" + f, 0o755)
# 1. what Chromium mapped/opened during the bench runs (google/youtube/dash/wikipedia/guardian/github), rootfs-relative,
# one per line in files.txt next to this script
seen = set(l.strip() for l in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "files.txt")) if l.strip())
for f in sorted(seen):
    add(real(f), "mapped during the bench")
    add(f, "mapped during the bench (as named)")
# 2. Chromium's install dir
for dp, dn, fn in os.walk(SRC + "/usr/lib/chromium"):
    for f in fn:
        rel = os.path.join(dp, f)[len(SRC):]
        if "/locales/" in rel and not rel.endswith("/en-US.pak"):
            continue
        if "VkLayer_khronos_validation" in rel or "VkICD_mock" in rel:
            continue
        add(rel, "chromium install dir")
# 3. loader + dlopen()ed libs + NEEDED closure
add("/usr/lib/ld-linux-armhf.so.3", "loader")
add(real("/usr/lib/ld-linux-armhf.so.3"), "loader")
for n in DLOPEN:
    p = find_lib(n)
    if p:
        add(p, "dlopen by chromium")
        add(real(p), "dlopen by chromium")
missing = set()
changed = True
while changed:
    changed = False
    for dp, dn, fn in os.walk(OUT):
        for f in fn:
            p = os.path.join(dp, f)
            if os.path.islink(p) or not is_elf(p):
                continue
            for n in needed(p):
                q = find_lib(n)
                if not q:
                    missing.add(n)
                    continue
                if not os.path.lexists(OUT + q):
                    add(q, "NEEDED by " + p[len(OUT):])
                    add(real(q), "NEEDED by " + p[len(OUT):])
                    changed = True
# 4. data: fonts, fontconfig, ALSA, GTK/GIO schemas
# (xkb/compose: Chromium compiles a default keymap from XKB rules at start; opened and closed at once, so the bench's
# map/fd sampling never saw it, and without it Chromium crashes)
for d in ("/usr/share/fonts", "/usr/share/alsa", "/usr/share/glib-2.0/schemas", "/usr/share/X11/xkb",
          "/usr/share/X11/locale", "/usr/lib/locale/C.utf8"):
    add(d, "data")
os.makedirs(OUT + "/etc/fonts/conf.d", exist_ok=True)
for c in glob.glob(SRC + "/etc/fonts/conf.d/*.conf"):
    t = SRC + real(c[len(SRC):])
    if os.path.exists(t):
        shutil.copy2(t, OUT + "/etc/fonts/conf.d/" + os.path.basename(c))
with open(OUT + "/etc/fonts/fonts.conf", "w") as f:
    f.write('<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">\n<fontconfig>\n'
            f'  <dir>{MNT}/usr/share/fonts</dir>\n  <cachedir prefix="xdg">fontconfig</cachedir>\n'
            f'  <include ignore_missing="yes">{MNT}/etc/fonts/conf.d</include>\n</fontconfig>\n')
# 5. own loader for the executables
for exe in ("/usr/lib/chromium/chromium", "/usr/lib/chromium/chrome_crashpad_handler"):
    subprocess.run([PATCHELF, "--set-interpreter", INTERP, OUT + exe], check=True)
    r = subprocess.run(["readelf", "-lW", OUT + exe], capture_output=True, text=True).stdout
    assert INTERP in r, exe + ": interpreter not set"
nfiles = sum(len(fn) for _, _, fn in os.walk(OUT))
size = int(subprocess.run(["du", "-sm", OUT], capture_output=True, text=True).stdout.split()[0])
res = {"files": nfiles, "size_mb": size, "interp": INTERP, "missing_needed": sorted(missing),
       "mapped_in_bench": len(seen), "dlopen": DLOPEN}
json.dump(dict(res, why=why), open(os.path.dirname(OUT) + "/bundle.json", "w"), indent=1)
print(json.dumps(res))
