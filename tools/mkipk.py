#!/usr/bin/env python3
"""mkipk.py: build the webOS ATL packages (stdlib only, reproducible: fixed mtimes).

  mkipk.py manager --version 0.1.0 --native native/out --runtime dist/webosatl-runtime-0.1.0.sqfs \
                   --runtime-version 0.1.0 --tag v0.1.0 [--out dist]
      -> dist/org.webosatl.manager_0.1.0_all.ipk  (web app + payload/ with the TV programs and release.json)
  mkipk.py chromium --version 0.1.0 --image dist/webosatl-chromium-0.1.0.sqfs --tag v0.1.0 [--out dist]
      -> dist/org.webosatl.chromium_0.1.0_arm.ipk (wrapper app; the manager downloads the image named in addon.json)

The runtime image is not inside the IPK (Homebrew Channel downloads IPKs to RAM); release.json points the manager
agent at the GitHub release asset: https://github.com/<repo>/releases/download/<tag>/<runtime file>.
"""
import argparse, gzip, hashlib, io, json, os, sys, tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "sarahahzub/WebOS-ATL"
MTIME = 1760000000  # fixed: same inputs -> same IPK bytes (same sha256 in the repo manifest)
TV_FILES = ["atld.py", "atlmgr.py", "apkweb.py", "apkinfo.py", "apkcheck.py", "conf.py", "env.sh", "run.sh",
            "dex2oat-verify", "init.sh", "install.sh", "uninstall.sh", "ls2-role.sh", "default-icon.png"]
NATIVE_FILES = ["xdgshim", "atlvdec", "atlfix.so", "low4g.so"]


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def tgz(entries):
    """entries: [(path, bytes or None for a dir, mode)]"""
    b = io.BytesIO()
    with gzip.GzipFile(fileobj=b, mode="wb", mtime=MTIME) as gz, tarfile.open(fileobj=gz, mode="w",
                                                                               format=tarfile.GNU_FORMAT) as t:
        for path, data, mode in entries:
            ti = tarfile.TarInfo(path)
            ti.mtime, ti.mode, ti.uid, ti.gid, ti.uname, ti.gname = MTIME, mode, 0, 0, "root", "root"
            if data is None:
                ti.type = tarfile.DIRTYPE
                t.addfile(ti)
            else:
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))
    return b.getvalue()


def ar(members):
    out = io.BytesIO()
    out.write(b"!<arch>\n")
    for name, data in members:
        out.write(("%-16s%-12d%-6d%-6d%-8o%-10d`\n" % (name, MTIME, 0, 0, 0o100644, len(data))).encode())
        out.write(data)
        if len(data) % 2:
            out.write(b"\n")
    return out.getvalue()


def ipk(appid, version, arch, desc, files, out):
    """files: {relative path in the app dir: (bytes, mode)}"""
    base = "./usr/palm/applications/" + appid
    ents = [("./usr", None, 0o755), ("./usr/palm", None, 0o755), ("./usr/palm/applications", None, 0o755),
            (base, None, 0o755)]
    dirs = set()
    for rel in sorted(files):
        parts = rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            d = "/".join(parts[:i])
            if d not in dirs:
                dirs.add(d)
                ents.append((base + "/" + d, None, 0o755))
        data, mode = files[rel]
        ents.append((base + "/" + rel, data, mode))
    size = sum(len(d) for d, _ in files.values()) // 1024 + 1
    control = ("Package: %s\nVersion: %s\nSection: misc\nPriority: optional\nArchitecture: %s\nInstalled-Size: %d\n"
               "Maintainer: webOS ATL <https://github.com/%s>\nDescription: %s\nwebOS-Package-Format-Version: 2\n"
               "webOS-Packager-Version: x.y.x\n") % (appid, version, arch, size, REPO, desc)
    data = ar([("debian-binary", b"2.0\n"), ("control.tar.gz", tgz([("./control", control.encode(), 0o644)])),
               ("data.tar.gz", tgz(ents))])
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out + ".tmp", "wb") as f:
        f.write(data)
    os.replace(out + ".tmp", out)
    print("built %s (%d KB, sha256 %s)" % (out, len(data) >> 10, sha256(out)))
    return out


def read(p):
    with open(p, "rb") as f:
        return f.read()


def manager(a):
    rt = {"version": a.runtime_version, "file": os.path.basename(a.runtime),
          "url": "https://github.com/%s/releases/download/%s/%s" % (REPO, a.runtime_tag or a.tag,
                                                                     os.path.basename(a.runtime)),
          "sha256": sha256(a.runtime), "size": os.path.getsize(a.runtime)}
    release = {"version": a.version, "tag": a.tag, "repo": REPO, "runtime": rt}
    m = ROOT + "/manager"
    info = {"id": "org.webosatl.manager", "version": a.version, "vendor": "webOS ATL", "type": "web",
            "main": "index.html", "title": "webOS ATL", "icon": "icon.png", "largeIcon": "largeIcon.png",
            "bgColor": "#101218", "iconColor": "#151821", "resolution": "1920x1080", "disableBackHistoryAPI": True,
            "appDescription": "Android apps on webOS"}
    files = {"appinfo.json": (json.dumps(info, indent=1).encode(), 0o644),
             "index.html": (read(m + "/index.html"), 0o644),
             "icon.png": (read(m + "/icon.png"), 0o644), "largeIcon.png": (read(m + "/largeIcon.png"), 0o644),
             "payload/release.json": (json.dumps(release, indent=1).encode(), 0o644)}
    for n in TV_FILES:
        files["payload/" + n] = (read(ROOT + "/tv/" + n), 0o644 if n.endswith(".png") else 0o755)
    for n in NATIVE_FILES:
        files["payload/" + n] = (read(os.path.join(a.native, n)), 0o755)
    return ipk("org.webosatl.manager", a.version, "all", "webOS ATL: Android apps on webOS (manager and runtime installer)",
               files, os.path.join(a.out, "org.webosatl.manager_%s_all.ipk" % a.version))


def chromium(a):
    c = ROOT + "/chromium/wrapper"
    img = {"file": os.path.basename(a.image), "sha256": sha256(a.image), "size": os.path.getsize(a.image),
           "url": "https://github.com/%s/releases/download/%s/%s" % (REPO, a.image_tag or a.tag, os.path.basename(a.image))}
    info = {"id": "org.webosatl.chromium", "version": a.version, "vendor": "webOS ATL", "type": "native",
            "main": "bin/run", "title": "Chromium", "icon": "icon.png", "largeIcon": "icon.png", "iconColor": "#1d2433",
            "noSplashOnLaunch": True, "disableBackHistoryAPI": True}
    files = {"appinfo.json": (json.dumps(info, indent=1).encode(), 0o644), "icon.png": (read(c + "/icon.png"), 0o644),
             "bin/run": (read(c + "/run.sh"), 0o755), "bin/xdgshim": (read(os.path.join(a.native, "xdgshim")), 0o755),
             "run/.keep": (b"", 0o644),
             "payload/addon.json": (json.dumps({"version": a.version, "image": img}, indent=1).encode(), 0o644),
             "payload/browserd.sh": (read(c + "/browserd.sh"), 0o755),
             "payload/browser.conf": (read(c + "/browser.conf"), 0o644)}
    return ipk("org.webosatl.chromium", a.version, "arm", "webOS ATL Chromium: web browser add-on (needs webOS ATL)",
               files, os.path.join(a.out, "org.webosatl.chromium_%s_arm.ipk" % a.version))


def main():
    p = argparse.ArgumentParser()
    sp = p.add_subparsers(dest="what")
    m = sp.add_parser("manager")
    m.add_argument("--version", required=True)
    m.add_argument("--tag", required=True, help="GitHub release tag the assets are uploaded to")
    m.add_argument("--native", default=ROOT + "/native/out")
    m.add_argument("--runtime", required=True, help="runtime .sqfs (built by runtime/build/bootimage.sh)")
    m.add_argument("--runtime-version", required=True)
    m.add_argument("--runtime-tag", help="release tag holding the runtime if it is not re-uploaded (default --tag)")
    m.add_argument("--out", default=ROOT + "/dist")
    c = sp.add_parser("chromium")
    c.add_argument("--version", required=True)
    c.add_argument("--tag", required=True)
    c.add_argument("--image", required=True, help="browser.sqfs built by chromium/build.sh")
    c.add_argument("--image-tag", help="release tag holding the image if it is not re-uploaded (default --tag)")
    c.add_argument("--native", default=ROOT + "/native/out")
    c.add_argument("--out", default=ROOT + "/dist")
    a = p.parse_args()
    if a.what == "manager":
        manager(a)
    elif a.what == "chromium":
        chromium(a)
    else:
        p.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
