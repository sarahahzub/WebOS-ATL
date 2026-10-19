#!/usr/bin/env python3
"""Download a test corpus of popular F-Droid apps (arm64-v8a or ABI-independent builds) for the smoke bench.
usage: fetch-corpus.py <outdir> [pkg ...]     (default list below)"""
import json, os, sys, urllib.request, zipfile

DEFAULT = [
    # TV / media
    "org.jellyfin.androidtv", "org.schabi.newpipe", "com.github.libretube", "de.danoeh.antennapod", "org.videolan.vlc",
    # browsers (system WebView based, and Gecko)
    "acr.browser.lightning", "de.baumann.browser", "org.mozilla.fennec_fdroid",
    # Compose apps
    "me.ash.reader", "org.breezyweather", "com.kunzisoft.keepass.libre", "org.koitharu.kotatsu",
    # classic View apps
    "org.fossify.calculator", "org.fossify.notes", "org.fossify.clock", "org.fossify.gallery", "org.fossify.filemanager",
    "org.tasks", "org.fdroid.fdroid", "org.wikipedia", "org.kde.kdeconnect_tp", "com.nextcloud.client",
]


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "atl-smoke/1"}), timeout=120) as r:
        return r.read()


def abis(path):
    with zipfile.ZipFile(path) as z:
        return sorted({n.split("/")[1] for n in z.namelist() if n.startswith("lib/") and n.count("/") >= 2})


def main():
    out = sys.argv[1]
    pkgs = sys.argv[2:] or DEFAULT
    os.makedirs(out, exist_ok=True)
    for p in pkgs:
        dst = os.path.join(out, p + ".apk")
        if os.path.exists(dst):
            print("have", p); continue
        try:
            info = json.loads(get(f"https://f-droid.org/api/v1/packages/{p}"))
        except Exception as e:
            print("FAIL api", p, e); continue
        vcs = [x["versionCode"] for x in info.get("packages", [])]
        sug = info.get("suggestedVersionCode")
        vcs.sort(key=lambda v: (v != sug, -v))
        ok = False
        for vc in vcs[:6]:
            tmp = dst + ".part"
            try:
                open(tmp, "wb").write(get(f"https://f-droid.org/repo/{p}_{vc}.apk"))
            except Exception as e:
                continue
            a = abis(tmp)
            if not a or "arm64-v8a" in a:
                os.replace(tmp, dst); ok = True
                print(f"got {p} vc={vc} abis={a or 'none'} {os.path.getsize(dst) >> 20} MB"); break
            os.remove(tmp)
        if not ok:
            print("FAIL no arm64/universal build", p)


main()
