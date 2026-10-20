#!/usr/bin/env python3
"""mkrepo.py --tag v0.1.0 dist/org.webosatl.manager_0.1.0_all.ipk [dist/org.webosatl.chromium_0.1.0_arm.ipk]

Writes the Homebrew Channel repository served from GitHub (raw.githubusercontent.com):
  repo/apps.json                 package list (Homebrew Channel -> Settings -> add the apps.json URL)
  repo/manifests/<id>.json       per-package manifest: version, ipkUrl (GitHub release asset), sha256, size
  repo/<id>.html                 full description shown on the package page
"""
import argparse, hashlib, io, json, os, re, tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "sarahahzub/WebOS-ATL"
RAW = "https://raw.githubusercontent.com/%s/main" % REPO
PKGS = {
    "org.webosatl.manager": {
        "title": "webOS ATL",
        "icon": RAW + "/manager/icon-512.png",
        "short": "Run Android apps on a rooted webOS TV (Android Translation Layer). Needs root and an aarch64 TV.",
        "desc": """<p><b>webOS ATL</b> runs Android apps natively on rooted LG webOS TVs, using
<a href="https://gitlab.com/android_translation_layer">Android Translation Layer</a> (no emulator, no Android VM).</p>
<p>Requirements: rooted TV with Homebrew Channel, 64-bit CPU (<code>uname -m</code> = aarch64), 600 MB free space,
1.2 GB RAM. Tested on LG 43UR7500 (webOS 23); other models are untested.</p>
<p>After installing, open <b>webOS ATL</b> and press <b>Install</b>: it sets up its service and downloads the Android
runtime (about 200 MB). Then install APKs from a phone or laptop browser at <code>http://&lt;tv-ip&gt;:8790/</code>,
or from a download link in the app. Each Android app gets its own icon in the TV's app list.</p>
<p>Many apps work, many do not yet (no Google Play services, limited WebView and media support). See the project
page for the compatibility list and known issues.</p>""",
    },
    "org.webosatl.chromium": {
        "title": "webOS ATL Chromium",
        "icon": RAW + "/chromium/wrapper/icon.png",
        "short": "Chromium web browser for webOS TVs (optional add-on, needs webOS ATL installed and set up).",
        "desc": """<p>A desktop Chromium build for the TV, driven with the remote: arrows move a pointer, OK clicks,
CH+/CH- scroll, BACK goes back. Text fields open the TV keyboard.</p>
<p>Needs <b>webOS ATL</b>: after this app is installed, the webOS ATL service downloads the browser image (about
160 MB) within a minute. Open webOS ATL to see the progress.</p>
<p>Known issues: the three-dot menu does not open, and full-screen video may close the browser.</p>""",
    },
}


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def control(ipk):
    data = open(ipk, "rb").read()
    i = 8
    while i + 60 <= len(data):
        name = data[i:i + 16].decode().strip().rstrip("/")
        size = int(data[i + 48:i + 58].decode().strip())
        if name == "control.tar.gz":
            with tarfile.open(fileobj=io.BytesIO(data[i + 60:i + 60 + size]), mode="r:gz") as t:
                c = t.extractfile("./control").read().decode()
            return dict(re.findall(r"^([A-Za-z-]+):\s*(.*)$", c, re.M))
        i += 60 + size + (size & 1)
    raise ValueError(ipk + ": no control.tar.gz")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tag", required=True)
    p.add_argument("ipks", nargs="+")
    a = p.parse_args()
    os.makedirs(ROOT + "/repo/manifests", exist_ok=True)
    old = {}
    try:
        old = {x["id"]: x for x in json.load(open(ROOT + "/repo/apps.json"))["packages"]}
    except (OSError, ValueError, KeyError):
        pass
    for ipk in a.ipks:
        c = control(ipk)
        appid, ver = c["Package"], c["Version"]
        meta = PKGS[appid]
        man = {"id": appid, "version": ver, "type": "native" if appid.endswith("chromium") else "web",
               "title": meta["title"], "appDescription": meta["short"], "iconUri": meta["icon"],
               "sourceUrl": "https://github.com/" + REPO, "rootRequired": True,
               "ipkUrl": "https://github.com/%s/releases/download/%s/%s" % (REPO, a.tag, os.path.basename(ipk)),
               "ipkHash": {"sha256": sha256(ipk)}, "ipkSize": os.path.getsize(ipk)}
        json.dump(man, open(ROOT + "/repo/manifests/%s.json" % appid, "w"), indent=1)
        open(ROOT + "/repo/%s.html" % appid, "w").write(meta["desc"] + "\n")
        old[appid] = {"id": appid, "title": meta["title"], "iconUri": meta["icon"],
                      "manifestUrl": RAW + "/repo/manifests/%s.json" % appid, "manifest": man, "pool": "main",
                      "shortDescription": meta["short"], "fullDescriptionUrl": RAW + "/repo/%s.html" % appid}
        print("repo: %s %s" % (appid, ver))
    pk = [old[k] for k in sorted(old, key=lambda k: (k != "org.webosatl.manager", k))]
    n = len(pk)
    json.dump({"paging": {"page": 1, "count": n, "maxPage": 1, "itemsTotal": n, "prevUrl": None, "nextUrl": None},
               "packages": pk}, open(ROOT + "/repo/apps.json", "w"), indent=1)
    print("wrote repo/apps.json (%d packages)" % n)


if __name__ == "__main__":
    main()
