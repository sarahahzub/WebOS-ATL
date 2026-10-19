#!/usr/bin/env python3
"""ATL smoke test bench for the Oracle server (aarch64, same ABI as the TV).

Runs every APK under ATL inside a headless sway (1920x1080, pixman = software rendering like the TV), screenshots it,
and scans the log for the failure classes that matter. Writes a report you can diff between ATL builds.

usage: smoke.py [--atl DIR] [--apks DIR|FILE...] [--out DIR] [--secs N] [--keep-home] [--only SUBSTR] [--microg DIR] [--tv]
  --atl   extracted ATL AppImage dir (contains AppRun)      default ~/atl2/AppDir  (falls back to ~/atl/AppDir)
  --apks  APK files or dirs                                default ~/atl2/apks
  --out   report dir                                       default ~/atl2/smoke-out/<timestamp>
  --secs  seconds to run each app                          default 45
  --microg dir with microG gms.apk (+ vending.apk): installed into each app's ATL data dir (ATL loads GMS in-process)
  --tv    run with the Android TV profile (ATL_IS_TELEVISION=1, density 320) like on the TV
Per-APK options (sdk-int, activity, env) come from <apk>.conf next to the APK, same keys as the TV's apk.conf:
  ARGS=--sdk-int 30   ACTIVITY=pkg/Cls   ENV_ATL_DENSITY=320
Report: <out>/report.md (table), <out>/report.json, <out>/<apk>/{app.log,shot-*.png,missing.txt}
"""
import argparse, json, os, re, shutil, signal, subprocess, sys, time, zipfile

HOME = os.path.expanduser("~")
FAIL_PATTERNS = [  # (kind, regex) - first match per line wins
    ("missing-class", re.compile(r"(NoClassDefFoundError|ClassNotFoundException)[^A-Za-z]*(?:Failed resolution of: )?L?([\w$/.]+)")),
    ("missing-method", re.compile(r"NoSuchMethodError: ?(.*)")),
    ("missing-field", re.compile(r"NoSuchFieldError: ?(.*)")),
    ("unsatisfied-link", re.compile(r"UnsatisfiedLinkError: ?(.*)")),
    ("abstract-method", re.compile(r"AbstractMethodError: ?(.*)")),
    ("native-crash", re.compile(r"(atlfix: FATAL signal.*|Fatal signal \d+.*|JNI DETECTED ERROR.*)")),
    ("java-uncaught", re.compile(r"hacky_uncaught_exception_handler: Thread: (.*)")),
    ("exit", re.compile(r"System\.exit called, status: (\d+)")),
    ("stub", re.compile(r"STUB: ?(.*)")),
    ("atlstub", re.compile(r"ATLSTUB: ?(.*)")),
]
FATAL_KINDS = {"native-crash", "java-uncaught", "exit"}
# the app believes it has no network (ConnectivityManager/NetworkCapabilities answers) - a TV-breaking failure too
OFFLINE = re.compile(r"(?i)(device (is )?offline|no (internet|network) (connection|available)|network (is )?(not available|unavailable)|online=false|UnknownHostException|isNetworkAvailable.*false)")


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw)


class Sway:
    def __init__(self, rt):
        self.rt = rt
        os.makedirs(rt, mode=0o700, exist_ok=True)
        cfg = os.path.join(rt, "sway.cfg")
        open(cfg, "w").write("output HEADLESS-1 resolution 1920x1080 bg #202020 solid_color\n"
                             "default_border none\nfocus_follows_mouse no\nseat * hide_cursor 1\n")
        env = dict(os.environ, XDG_RUNTIME_DIR=rt, WLR_BACKENDS="headless", WLR_RENDERER="pixman",
                   WLR_LIBINPUT_NO_DEVICES="1", WLR_HEADLESS_OUTPUTS="1")
        env.pop("WAYLAND_DISPLAY", None); env.pop("DISPLAY", None)
        self.p = subprocess.Popen(["sway", "-c", cfg], env=env, stdout=open(os.path.join(rt, "sway.log"), "w"),
                                  stderr=subprocess.STDOUT, start_new_session=True)
        for _ in range(100):
            socks = [f for f in os.listdir(rt) if f.startswith("wayland-") and not f.endswith(".lock")]
            if socks:
                self.display = socks[0]
                break
            time.sleep(0.1)
        else:
            raise SystemExit("sway did not start: " + open(os.path.join(rt, "sway.log")).read()[-2000:])
        self.env = {"XDG_RUNTIME_DIR": rt, "WAYLAND_DISPLAY": self.display}
        sw = [f for f in os.listdir(rt) if f.startswith("sway-ipc")]
        if sw:
            self.env["SWAYSOCK"] = os.path.join(rt, sw[0])

    def run(self, cmd, **kw):
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, env=dict(os.environ, **self.env), **kw)

    def views(self):
        r = self.run("swaymsg -t get_tree -r")
        try:
            tree = json.loads(r.stdout)
        except ValueError:
            return []
        out = []
        def walk(n):
            if n.get("pid") and n.get("type") in ("con", "floating_con"):
                out.append(n)
            for c in n.get("nodes", []) + n.get("floating_nodes", []):
                walk(c)
        walk(tree)
        return out

    def shot(self, path):
        self.run(f"grim -s 0.5 {path}")
        return os.path.exists(path)

    def stop(self):
        try:
            os.killpg(self.p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def conf_for(apk):
    c = {}
    for f in (apk + ".conf", os.path.splitext(apk)[0] + ".conf"):
        if os.path.exists(f):
            for line in open(f):
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    c[k] = v
    return c


def scan(log):
    hits = []
    for line in log.splitlines():
        for kind, rx in FAIL_PATTERNS:
            m = rx.search(line)
            if m:
                hits.append((kind, (m.group(m.lastindex) if m.lastindex else m.group(0)).strip()[:200]))
                break
    return hits


def rss_kb(pid):
    tot = 0
    for p in [pid] + children(pid):
        try:
            for l in open(f"/proc/{p}/status"):
                if l.startswith("VmRSS"):
                    tot += int(l.split()[1])
        except OSError:
            pass
    return tot


def children(pid):
    r = sh(f"pgrep -P {pid}")
    kids = [int(x) for x in r.stdout.split()]
    for k in list(kids):
        kids += children(k)
    return kids


def install_microg(data, microg):
    """ATL loads <data>/_installed_apks_/<pkg>.apk in-process for com.google.android.gms / com.android.vending"""
    inst = os.path.join(data, "_installed_apks_")
    os.makedirs(inst, exist_ok=True)
    for src, pkg in (("gms.apk", "com.google.android.gms"), ("vending.apk", "com.android.vending")):
        f = os.path.join(microg, src)
        if not os.path.exists(f):
            continue
        dst = os.path.join(inst, pkg + ".apk")
        if not os.path.exists(dst):
            try:
                os.link(f, dst)
            except OSError:
                shutil.copy(f, dst)
        lib = os.path.join(data, pkg + "_", "lib")
        if not os.path.isdir(lib):
            os.makedirs(lib, exist_ok=True)
            with zipfile.ZipFile(f) as z:
                for n in z.namelist():
                    if n.startswith("lib/arm64-v8a/") and n.endswith(".so"):
                        open(os.path.join(lib, os.path.basename(n)), "wb").write(z.read(n))


def run_one(sway, atl, apk, outdir, secs, keep_home, microg=None, tv=False):
    name = os.path.splitext(os.path.basename(apk))[0]
    d = os.path.join(outdir, name)
    os.makedirs(d, exist_ok=True)
    c = conf_for(apk)
    home = os.path.join(HOME, "atl2", "smoke-home", name)
    if not keep_home and os.path.isdir(home):
        # keep the ART boot image cache (slow to rebuild), drop app data
        shutil.rmtree(os.path.join(home, "data"), ignore_errors=True)
    os.makedirs(os.path.join(home, "data"), exist_ok=True)
    if microg or c.get("MICROG"):
        install_microg(os.path.join(home, "data"), microg or c.get("MICROG"))
    env = dict(os.environ, **sway.env, HOME=home, ANDROID_APP_DATA_DIR=os.path.join(home, "data"),
               GSK_RENDERER="cairo", ATLFIX_NOPA="1")
    if tv:
        env.update(ATL_IS_TELEVISION="1", ATL_DENSITY="320")
    env.pop("DISPLAY", None)
    pre = [p for p in (os.path.join(HOME, "atl2", "atlfix.so"),) if os.path.exists(p)]
    if pre:
        env.update(SHARUN_ALLOW_LD_PRELOAD="1", LD_PRELOAD=":".join(pre))
    env.update({k[4:]: v for k, v in c.items() if k.startswith("ENV_")})
    cmd = [os.path.join(atl, "AppRun")] + c.get("ARGS", "").split() + [apk]
    if c.get("ACTIVITY"):
        cmd += ["-l", c["ACTIVITY"]]
    logf = open(os.path.join(d, "app.log"), "w")
    t0 = time.time()
    p = subprocess.Popen(cmd, env=env, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    res = {"apk": name, "cmd": " ".join(cmd), "window_s": None, "alive_s": None, "rc": None, "peak_rss_mb": 0, "shots": []}
    shots_at = sorted({min(10, secs - 1), min(25, secs - 1), secs - 1})
    while time.time() - t0 < secs:
        el = time.time() - t0
        if p.poll() is not None:
            res["rc"] = p.returncode
            res["alive_s"] = round(el, 1)
            break
        res["peak_rss_mb"] = max(res["peak_rss_mb"], rss_kb(p.pid) // 1024)
        if res["window_s"] is None and any(v.get("pid") in [p.pid] + children(p.pid) for v in sway.views()):
            res["window_s"] = round(el, 1)
        if shots_at and el >= shots_at[0]:
            f = os.path.join(d, f"shot-{int(shots_at.pop(0))}s.png")
            if sway.shot(f):
                res["shots"].append(os.path.basename(f))
        time.sleep(0.5)
    if res["rc"] is None:
        res["alive_s"] = secs
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(p.pid, sig)
            except ProcessLookupError:
                break
            time.sleep(1)
        p.wait()
    logf.close()
    log = open(os.path.join(d, "app.log"), errors="replace").read()
    hits = scan(log)
    res["fatal"] = [f"{k}: {v}" for k, v in hits if k in FATAL_KINDS][:5]
    missing = sorted({f"{k}: {v}" for k, v in hits if k.startswith("missing") or k in ("unsatisfied-link", "abstract-method")})
    res["missing"] = missing
    res["stubs_hit"] = len({v for k, v in hits if k == "stub"})
    res["offline"] = sorted({m.group(0)[:60] for m in OFFLINE.finditer(log)})[:5]
    res["gen_stubs_hit"] = sorted({v for k, v in hits if k == "atlstub"})
    open(os.path.join(d, "atlstub.txt"), "w").write("\n".join(res["gen_stubs_hit"]) + "\n")
    open(os.path.join(d, "missing.txt"), "w").write("\n".join(missing) + "\n")
    stubs = sorted({v for k, v in hits if k == "stub"})
    open(os.path.join(d, "stubs.txt"), "w").write("\n".join(stubs) + "\n")
    crashed = res["rc"] not in (None, 0) or bool(res["fatal"])
    res["status"] = ("CRASH" if crashed else "OFFLINE" if res["offline"] else "OK" if res["window_s"] is not None else "NOWINDOW")
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--atl")
    ap.add_argument("--apks", nargs="*")
    ap.add_argument("--out")
    ap.add_argument("--secs", type=int, default=45)
    ap.add_argument("--keep-home", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--microg")
    ap.add_argument("--tv", action="store_true")
    a = ap.parse_args()
    atl = a.atl or next(d for d in (f"{HOME}/atl2/AppDir", f"{HOME}/atl/AppDir") if os.path.exists(f"{d}/AppRun"))
    srcs = a.apks or [f"{HOME}/atl2/apks"]
    apks = []
    for s in srcs:
        apks += sorted(os.path.join(s, f) for f in os.listdir(s) if f.endswith(".apk")) if os.path.isdir(s) else [s]
    if a.only:
        apks = [x for x in apks if a.only in os.path.basename(x)]
    out = a.out or os.path.join(HOME, "atl2", "smoke-out", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out, exist_ok=True)
    sway = Sway(f"/tmp/atl-smoke-{os.getpid()}")
    results = []
    try:
        for apk in apks:
            print(f"== {os.path.basename(apk)}", flush=True)
            r = run_one(sway, atl, apk, out, a.secs, a.keep_home, a.microg, a.tv)
            results.append(r)
            print(f"   {r['status']} window={r['window_s']} alive={r['alive_s']} rc={r['rc']} rss={r['peak_rss_mb']}MB "
                  f"missing={len(r['missing'])} stubs={r['stubs_hit']} {('; '.join(r['fatal'] + r['offline']))[:200]}", flush=True)
    finally:
        sway.stop()
    json.dump({"atl": atl, "results": results}, open(os.path.join(out, "report.json"), "w"), indent=1)
    with open(os.path.join(out, "report.md"), "w") as f:
        f.write(f"# ATL smoke report {time.strftime('%Y-%m-%d %H:%M')}\n\nATL: `{atl}`\n\n")
        f.write("| APK | status | window (s) | alive (s) | rc | peak RSS MB | missing APIs | first fatal |\n|---|---|---|---|---|---|---|---|\n")
        for r in results:
            f.write(f"| {r['apk']} | {r['status']} | {r['window_s']} | {r['alive_s']} | {r['rc']} | {r['peak_rss_mb']} | "
                    f"{len(r['missing'])} | {(r['fatal'][0] if r['fatal'] else '').replace('|', '/')[:120]} |\n")
        f.write("\n## Generated (StubGen) APIs apps called - candidates for real implementations\n\n")
        gagg = {}
        for r in results:
            for m in r.get("gen_stubs_hit", []):
                gagg.setdefault(m, []).append(r["apk"])
        for m, who in sorted(gagg.items(), key=lambda x: -len(x[1]))[:150]:
            f.write(f"- `{m[:160]}` ({len(who)}: {', '.join(who)[:120]})\n")
        f.write("\n## Missing APIs hit at runtime (all apps)\n\n")
        agg = {}
        for r in results:
            for m in r["missing"]:
                agg.setdefault(m, []).append(r["apk"])
        for m, who in sorted(agg.items(), key=lambda x: -len(x[1])):
            f.write(f"- `{m[:160]}` ({len(who)}: {', '.join(who)[:120]})\n")
    ok = sum(r["status"] == "OK" for r in results)
    print(f"\n{ok}/{len(results)} OK  -> {out}/report.md")


if __name__ == "__main__":
    main()
