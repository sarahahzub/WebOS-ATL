#!/usr/bin/env python3
"""atlmgr: webOS ATL manager agent (runs ON THE TV as root, started by init.sh; stdlib only).

1. Payload sync: Homebrew Channel updates the manager app (org.webosatl.manager), whose payload/ folder carries the
   TV programs. atlmgr copies changed files to /media/developer/webosatl/bin and restarts what they belong to
   (atld, apkweb, the wrapper apps' run/xdgshim, itself).
2. Runtime: payload/release.json names the runtime image (url, sha256, size). atlmgr downloads it from GitHub
   Releases with resume, checks the sha256 and swaps it in when no Android app is running.
3. Chromium add-on: when org.webosatl.chromium is installed (Homebrew Channel), downloads its image (payload/addon.json),
   installs its root helper browserd.sh in /media/developer/webosatl/chromium and starts it; removes it all again when
   the add-on app is uninstalled.
4. Local API on http://127.0.0.1:8791/ for the manager app: status, Android apps (settings, icons, uninstall, clear
   data, install from a link), global settings, logs, uninstall everything.

  atlmgr.py                 run the agent
  atlmgr.py runtime-update  download + install the runtime now (prints progress), for scripts
"""
import hashlib, json, os, re, shutil, socket, ssl, subprocess, sys, threading, time, traceback, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from conf import (BASE, BIN, RUNTIME, RUNTIME_IMG, HOME, APKS, LOGS, SETTINGS_F, APPS, PREFIX, MANAGER_ID,  # noqa
                  PAYLOAD, MGR_PORT, APKWEB_PORT, version, ensure_dirs)

ensure_dirs()
RUNTIME_STATE = BASE + "/runtime.json"   # {"sha256", "version"} of the installed runtime image
LOG_F = LOGS + "/atlmgr.log"
REPO = "sarahahzub/WebOS-ATL"
LATEST_URL = "https://raw.githubusercontent.com/%s/main/repo/manifest.json" % REPO  # newest manager app (Homebrew)
DEFAULTS = {"env": {"ATL_DENSITY": "320", "ATL_IS_TELEVISION": "1"}, "dex2oat": "verify", "mgr_lan": False}
# apk.conf keys the manager UI may edit
APP_KEYS = re.compile(r"^(ACTIVITY|ARGS|DPAD|SOFTCURSOR|HOVER|DIALOG_TYPE|SHIM_DEBUG|GSK_RENDERER|WEBVIEW|"
                      r"VIDEO_EXPORT|SHIM_ENV|VDEC_ENV|ENV_[A-Z0-9_]+)$")
# payload file -> what has to happen when it changes
ACTIONS = {"atld.py": "atld", "conf.py": "all", "apkweb.py": "apkweb", "apkinfo.py": "apkweb", "apkcheck.py": "apkweb",
           "low4g.so": None, "atlfix.so": None, "run.sh": "wrappers", "xdgshim": "wrappers", "atlmgr.py": "self"}

_loglock = threading.Lock()
_uplock = threading.Lock()
progress = {"state": "idle", "msg": "", "done": 0, "total": 0, "speed": 0, "time": 0}
latest = {"version": None, "time": 0}


# ---------------------------------------------------------------- utilities
def log(*a):
    with _loglock:
        try:
            if os.path.exists(LOG_F) and os.path.getsize(LOG_F) > (1 << 20):
                os.replace(LOG_F, LOG_F + ".1")
            with open(LOG_F, "a") as f:
                f.write("%s %s\n" % (time.strftime("%F %T"), " ".join(str(x) for x in a)))
        except OSError:
            pass


def sh(cmd, timeout=120):
    r = subprocess.run(["sh", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                       timeout=timeout, stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout


def init(*args, timeout=120):
    rc, out = sh("sh %s/init.sh %s" % (BIN, " ".join(args)), timeout)
    log("init.sh", *args, "rc", rc, out.strip()[-300:])
    return out


def luna(uri, payload, timeout=30):
    r = subprocess.run(["luna-send", "-n", "1", "-f", uri, json.dumps(payload)], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, universal_newlines=True, timeout=timeout, stdin=subprocess.DEVNULL)
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"returnValue": False, "raw": r.stdout[:500]}


def procs():
    """[(pid, argv list)] without shelling out to ps/pgrep (no self-matching command lines)."""
    out = []
    for p in os.listdir("/proc"):
        if p.isdigit():
            try:
                with open("/proc/%s/cmdline" % p, "rb") as f:
                    a = [x.decode(errors="replace") for x in f.read().split(b"\0") if x]
                if a:
                    out.append((int(p), a))
            except OSError:
                pass
    return out


def py_running(script):
    me = os.getpid()
    return [p for p, a in procs() if p != me and len(a) > 1 and "python" in os.path.basename(a[0])
            and a[1] == "%s/%s" % (BIN, script)]


def atl_apps_running():
    return [p for p, a in procs() if a[0].startswith(RUNTIME + "/")]


def load_json(p, default):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(p, obj, mode=0o644):
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.chmod(tmp, mode)
    os.replace(tmp, p)


def settings():
    s = json.loads(json.dumps(DEFAULTS))
    for k, v in load_json(SETTINGS_F, {}).items():
        if k == "env" and isinstance(v, dict):
            s["env"].update(v)
        else:
            s[k] = v
    return s


def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def tv_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 53))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return ""


def disk():
    try:
        st = os.statvfs(BASE)
        return {"free_mb": st.f_bavail * st.f_frsize >> 20, "total_mb": st.f_blocks * st.f_frsize >> 20}
    except OSError:
        return {}


def meminfo():
    v = {}
    try:
        for line in open("/proc/meminfo"):
            k, rest = line.split(":", 1)
            v[k] = int(rest.split()[0]) >> 10
    except (OSError, ValueError):
        pass
    return {"total_mb": v.get("MemTotal"), "avail_mb": v.get("MemAvailable"), "swap_free_mb": v.get("SwapFree")}


def conf_read(appdir):
    c = {}
    try:
        for line in open(appdir + "/apk.conf"):
            line = line.rstrip("\n")
            if "=" in line:
                k, v = line.split("=", 1)
                c[k] = v
    except OSError:
        pass
    return c


def conf_write(appdir, c):
    order = ["APK", "PKG", "ACTIVITY", "LEANBACK"]
    keys = [k for k in order if k in c] + sorted(k for k in c if k not in order)
    tmp = appdir + "/apk.conf.tmp"
    with open(tmp, "w") as f:
        f.write("".join("%s=%s\n" % (k, c[k]) for k in keys))
    os.chmod(tmp, 0o644)
    os.replace(tmp, appdir + "/apk.conf")


def urlopen(url, headers=None, timeout=60):
    """GET; if the TV's CA store can't verify the server, retry unverified (downloads are checked by sha256)."""
    req = urllib.request.Request(url, headers=dict({"User-Agent": "webosatl/" + version()}, **(headers or {})))
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context())
    except Exception as e:
        if "CERTIFICATE" in str(e).upper() or isinstance(getattr(e, "reason", None), ssl.SSLError):
            return urllib.request.urlopen(req, timeout=timeout, context=ssl._create_unverified_context())
        raise


def set_progress(**kw):
    progress.update(kw, time=time.time())


# ---------------------------------------------------------------- services
def restart(what):
    if what == "atld":
        return init("restart", "atld.py")
    if what == "apkweb":
        return init("restart", "apkweb.py")
    if what == "wrappers":
        return refresh_wrappers()
    raise ValueError("restart what?")


def refresh_wrappers():
    n = 0
    for d in os.listdir(APPS):
        if d.startswith(PREFIX) and os.path.isdir("%s/%s/bin" % (APPS, d)):
            for src, dst in (("run.sh", "run"), ("xdgshim", "xdgshim")):
                if os.path.exists("%s/%s" % (BIN, src)):
                    t = "%s/%s/bin/%s" % (APPS, d, dst)
                    shutil.copyfile("%s/%s" % (BIN, src), t + ".tmp")
                    os.chmod(t + ".tmp", 0o755)
                    os.replace(t + ".tmp", t)
            n += 1
    log("refreshed", n, "wrappers")
    return "refreshed %d wrappers" % n


def reexec():
    time.sleep(2)
    log("restarting atlmgr")
    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])


# ---------------------------------------------------------------- payload sync (manager app updated by Homebrew)
def sync_payload():
    """copy changed payload files to BIN and run their actions; returns the list of changed files"""
    if not os.path.isdir(PAYLOAD):
        return []
    changed = []
    for n in sorted(os.listdir(PAYLOAD)):
        src, dst = "%s/%s" % (PAYLOAD, n), "%s/%s" % (BIN, n)
        if not os.path.isfile(src):
            continue
        if os.path.exists(dst) and os.path.getsize(src) == os.path.getsize(dst) and sha_file(src) == sha_file(dst):
            continue
        shutil.copyfile(src, dst + ".tmp")
        os.chmod(dst + ".tmp", 0o644 if n.endswith((".json", ".png", ".html")) else 0o755)
        os.replace(dst + ".tmp", dst)
        changed.append(n)
    if not changed:
        return []
    log("payload sync:", ", ".join(changed))
    acts = {ACTIONS.get(n) for n in changed}
    if "all" in acts:
        acts |= {"atld", "apkweb", "self"}
    if "wrappers" in acts:
        refresh_wrappers()
    if "atld" in acts:
        init("restart", "atld.py")
    if "apkweb" in acts:
        init("restart", "apkweb.py")
    if "ls2-role.sh" in changed or "atlvdec" in changed:
        sh("sh %s/ls2-role.sh force" % BIN, 60)
    if "self" in acts:
        threading.Thread(target=reexec, daemon=True).start()
    return changed


# ---------------------------------------------------------------- runtime image
def release():
    return load_json(BIN + "/release.json", {})


def runtime_wanted():
    return release().get("runtime") or {}


def runtime_state():
    return load_json(RUNTIME_STATE, {})


def runtime_ok():
    w = runtime_wanted()
    return bool(w.get("sha256")) and runtime_state().get("sha256") == w["sha256"] and os.path.exists(RUNTIME_IMG)


def download(url, dest, sha, size, cb=None):
    """resumable download to dest (dest.part while incomplete); True when the sha256 matches"""
    part = dest + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if have > size:
        os.remove(part)
        have = 0
    if have < size:
        r = urlopen(url, {"Range": "bytes=%d-" % have} if have else None)
        if have and r.status != 206:  # server ignored the range: start over
            have = 0
        with r, open(part, "ab" if have else "wb") as f:
            t0, got = time.time(), 0
            while True:
                c = r.read(1 << 18)
                if not c:
                    break
                f.write(c)
                got += len(c)
                if cb:
                    cb(have + got, size, got / max(0.001, time.time() - t0))
    if os.path.getsize(part) != size:
        raise IOError("download incomplete: %d of %d bytes" % (os.path.getsize(part), size))
    if sha_file(part) != sha:
        os.remove(part)
        return False
    os.replace(part, dest)
    return True


def runtime_update(force=False, out=None):
    """download + install the runtime named in release.json. force: close running Android apps instead of waiting.
    Returns (ok, message)."""
    if not _uplock.acquire(blocking=False):
        return False, "already running"
    try:
        return _runtime_update(force, out)
    except Exception as e:
        log("runtime update failed", traceback.format_exc())
        set_progress(state="error", msg="runtime download failed: %s" % e)
        return False, "runtime update failed: %s" % e
    finally:
        _uplock.release()


def _runtime_update(force, out):
    w = runtime_wanted()
    if not w.get("url") or not w.get("sha256"):
        return False, "release.json names no runtime"
    if runtime_ok():
        if not os.path.exists(RUNTIME + "/AppRun"):
            init("mount")
        set_progress(state="idle", msg="runtime %s installed" % w.get("version", ""))
        return True, "runtime up to date"
    dl = RUNTIME_IMG + ".new"
    size = int(w.get("size") or 0)
    if not (os.path.exists(dl) and os.path.getsize(dl) == size and sha_file(dl) == w["sha256"]):
        have = os.path.getsize(dl + ".part") if os.path.exists(dl + ".part") else 0
        need = (size - have) >> 20
        free = disk().get("free_mb", 0)
        if need + 50 > free:
            raise RuntimeError("not enough space: the runtime needs %d MB more, %d MB free" % (need + 50, free))
        last = [0]

        def cb(done, total, speed):
            msg = "downloading runtime %d / %d MB (%.1f MB/s)" % (done >> 20, total >> 20, speed / 1048576)
            set_progress(state="downloading", msg=msg, done=done, total=total, speed=speed)
            if out and time.time() - last[0] > 2:
                last[0] = time.time()
                out(msg)
        log("runtime download", w["url"])
        set_progress(state="downloading", msg="downloading runtime", done=have, total=size, speed=0)
        for attempt in range(6):
            try:
                if download(w["url"], dl, w["sha256"], size, cb):
                    break
                log("runtime sha256 mismatch, attempt", attempt)
            except Exception as e:
                log("runtime download attempt", attempt, repr(e))
                if out:
                    out("download interrupted (%s), retrying" % e)
                time.sleep(min(5 * (attempt + 1), 30))
        else:
            raise RuntimeError("runtime download failed")
    if atl_apps_running() and not force:
        set_progress(state="pending", msg="new runtime downloaded; it is installed when no Android app is running")
        return True, "runtime downloaded, waiting for Android apps to close"
    set_progress(state="installing", msg="installing runtime")
    if out:
        out("installing runtime")
    init("umount", timeout=90)
    os.replace(dl, RUNTIME_IMG)
    os.chmod(RUNTIME_IMG, 0o644)
    shutil.rmtree(HOME + "/.cache/art", ignore_errors=True)  # compiled boot image belongs to the old runtime
    save_json(RUNTIME_STATE, {"sha256": w["sha256"], "version": w.get("version", ""), "time": int(time.time())})
    init("mount")
    ok = os.path.exists(RUNTIME + "/AppRun")
    msg = "runtime %s installed%s" % (w.get("version", ""), "" if ok else " but mounting it failed")
    set_progress(state="idle" if ok else "error", msg=msg, done=0, total=0, speed=0)
    log(msg)
    return ok, msg


# ---------------------------------------------------------------- Chromium add-on
CHROMIUM_ID = "org.webosatl.chromium"
CB = BASE + "/chromium"
_gone = [0]  # consecutive checks the add-on app was missing (an update reinstalls it: don't wipe on one miss)


def _pid_alive(pidfile, word):
    try:
        pid = open(pidfile).read().strip()
        return pid if pid and word in open("/proc/%s/cmdline" % pid).read() else None
    except (OSError, ValueError):
        return None


def browserd(action):
    pid = _pid_alive(CB + "/browserd.pid", "browserd")
    if action == "stop" or action == "restart":
        if pid:
            os.kill(int(pid), 15)
            time.sleep(1)
        pid = None
    if action in ("start", "restart") and not pid and os.path.exists(CB + "/browserd.sh"):
        subprocess.Popen("mkdir -p /tmp/xdg; exec sh %s/browserd.sh >> %s/browserd.log 2>&1" % (CB, CB), shell=True,
                         start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        log("browserd started")


def chromium_state():
    app = os.path.isdir("%s/%s" % (APPS, CHROMIUM_ID))
    a = load_json("%s/%s/payload/addon.json" % (APPS, CHROMIUM_ID), {}) if app else {}
    have = load_json(CB + "/image.json", {})
    want = (a.get("image") or {}).get("sha256")
    return {"installed": app, "version": a.get("version"), "ready": bool(want) and have.get("sha256") == want
            and os.path.exists(CB + "/browser.sqfs"), "running": bool(_pid_alive(CB + "/chromium.pid", "")),
            "browserd": bool(_pid_alive(CB + "/browserd.pid", "browserd"))}


def sync_chromium(force=False):
    app = "%s/%s" % (APPS, CHROMIUM_ID)
    if not os.path.isdir(app):
        if os.path.exists(CB + "/image.json"):
            _gone[0] += 1
            if _gone[0] >= 3:  # removed in Homebrew Channel: remove its image, helper and profile too
                log("Chromium add-on uninstalled: removing", CB)
                browserd("stop")
                sh("pkill -f %s/mnt/ 2>/dev/null; sleep 1; umount %s/mnt 2>/dev/null || umount -l %s/mnt 2>/dev/null"
                   % (CB, CB, CB), 60)
                shutil.rmtree(CB, ignore_errors=True)
                _gone[0] = 0
        return
    _gone[0] = 0
    a = load_json(app + "/payload/addon.json", {})
    img = a.get("image") or {}
    if not img.get("sha256"):
        return
    os.makedirs(CB, exist_ok=True)
    restart_d = False
    for n in ("browserd.sh", "browser.conf"):
        src, dst = "%s/payload/%s" % (app, n), "%s/%s" % (CB, n)
        if not os.path.exists(src) or (n == "browser.conf" and os.path.exists(dst)):
            continue  # browser.conf: the user's edits survive updates
        if not os.path.exists(dst) or sha_file(src) != sha_file(dst):
            shutil.copyfile(src, dst + ".tmp")
            os.chmod(dst + ".tmp", 0o755 if n.endswith(".sh") else 0o644)
            os.replace(dst + ".tmp", dst)
            restart_d = restart_d or n == "browserd.sh"
    if load_json(CB + "/image.json", {}).get("sha256") != img["sha256"] or not os.path.exists(CB + "/browser.sqfs"):
        if not _uplock.acquire(blocking=False):
            return
        try:
            dl = CB + "/browser.sqfs.new"
            size = int(img.get("size") or 0)
            if not (os.path.exists(dl) and os.path.getsize(dl) == size and sha_file(dl) == img["sha256"]):
                have = os.path.getsize(dl + ".part") if os.path.exists(dl + ".part") else 0
                free = disk().get("free_mb", 0)
                if ((size - have) >> 20) + 50 > free:
                    set_progress(state="error", msg="Chromium needs %d MB free, %d MB free"
                                 % (((size - have) >> 20) + 50, free))
                    return
                log("Chromium image download", img.get("url"))

                def cb(done, total, speed):
                    set_progress(state="downloading", done=done, total=total, speed=speed,
                                 msg="downloading Chromium %d / %d MB (%.1f MB/s)" % (done >> 20, total >> 20,
                                                                                    speed / 1048576))
                for attempt in range(6):
                    try:
                        if download(img["url"], dl, img["sha256"], size, cb):
                            break
                    except Exception as e:
                        log("Chromium download attempt", attempt, repr(e))
                        time.sleep(min(5 * (attempt + 1), 30))
                else:
                    set_progress(state="error", msg="Chromium download failed")
                    return
            if _pid_alive(CB + "/chromium.pid", "") and not force:
                set_progress(state="pending", msg="new Chromium downloaded; installed when Chromium is closed")
                return
            browserd("stop")
            sh("umount %s/mnt 2>/dev/null || umount -l %s/mnt 2>/dev/null; true" % (CB, CB), 60)
            os.replace(dl, CB + "/browser.sqfs")
            save_json(CB + "/image.json", {"sha256": img["sha256"], "version": a.get("version", "")})
            set_progress(state="idle", msg="Chromium %s installed" % a.get("version", ""), done=0, total=0, speed=0)
            log("Chromium image installed", a.get("version"))
            restart_d = True
        finally:
            _uplock.release()
    browserd("restart" if restart_d else "start")


def check_latest():
    """newest manager version published in the Homebrew repo (for the UI's 'update available' hint)"""
    try:
        with urlopen(LATEST_URL, timeout=30) as r:
            latest.update(version=json.loads(r.read().decode()).get("version"), time=time.time())
    except Exception as e:
        log("latest check failed", repr(e)[:200])
        latest["time"] = time.time()


def update_loop():
    time.sleep(5)
    while True:
        try:
            sync_payload()
        except Exception:
            log("payload sync failed", traceback.format_exc())
        if not runtime_ok() or progress.get("state") == "pending":
            runtime_update()
        try:
            sync_chromium()
        except Exception:
            log("Chromium sync failed", traceback.format_exc())
        if time.time() - latest["time"] > 6 * 3600:
            check_latest()
        time.sleep(60)


# ---------------------------------------------------------------- apps
def du(p):
    n = 0
    for r, _, fs in os.walk(p):
        for f in fs:
            try:
                n += os.lstat(os.path.join(r, f)).st_size
            except OSError:
                pass
    return n


def apps():
    out = []
    running = " ".join(" ".join(a) for _, a in procs() if a[0].startswith(RUNTIME + "/"))
    for d in sorted(os.listdir(APPS)):
        if not d.startswith(PREFIX):
            continue
        ad = "%s/%s" % (APPS, d)
        c = conf_read(ad)
        info = load_json(ad + "/appinfo.json", {})
        apk = c.get("APK", "")
        data = "%s/data/%s_" % (HOME, os.path.basename(apk)) if apk else ""
        out.append({"id": d, "title": info.get("title", d), "version": info.get("version", ""), "conf": c,
                    "apk": apk, "apk_mb": round(os.path.getsize(apk) / 1048576, 1) if apk and os.path.exists(apk) else 0,
                    "data_mb": round(du(data) / 1048576, 1) if data else 0, "running": bool(apk) and apk in running,
                    "compat": os.path.exists(apk[:-4] + ".compat.txt") if apk else False})
    return out


def appdir(appid):
    if not appid.startswith(PREFIX) or "/" in appid or not os.path.isdir("%s/%s" % (APPS, appid)):
        raise ValueError("unknown Android app " + appid)
    return "%s/%s" % (APPS, appid)


def app_set(appid, changes):
    ad = appdir(appid)
    c = conf_read(ad)
    for k, v in changes.items():
        if not APP_KEYS.match(k):
            raise ValueError("key not editable: " + k)
        if v is None or v == "":
            c.pop(k, None)
        else:
            v = str(v)
            if "\n" in v:
                raise ValueError("bad value")
            c[k] = v
    conf_write(ad, c)
    log("app-set", appid, json.dumps(changes))
    return c


def clear_data(appid):
    ad = appdir(appid)
    apk = conf_read(ad).get("APK", "")
    luna("luna://com.webos.applicationManager/closeByAppId", {"id": appid})
    time.sleep(1)
    if apk:
        shutil.rmtree("%s/data/%s_" % (HOME, os.path.basename(apk)), ignore_errors=True)
    log("clear-data", appid)
    return "data cleared"


def uninstall(appid):
    appdir(appid)
    import apkweb
    apkweb.remove(appid)
    return "uninstalled " + appid


def install_url(url, title=""):
    import apkweb
    set_progress(state="downloading", msg="downloading APK", done=0, total=0, speed=0)

    def cb(done, total):
        set_progress(state="downloading", msg="downloading APK %d MB" % (done >> 20), done=done, total=total)
    try:
        appid = apkweb.install_url(url, title, "", cb)
    except Exception as e:
        set_progress(state="error", msg="install failed: %s" % e)
        raise
    set_progress(state="idle", msg="installed " + appid, done=0, total=0)
    return appid


def reicon(which="all"):
    import apkinfo
    ids = [a["id"] for a in apps()] if which == "all" else [which]
    out = []
    for appid in ids:
        try:
            ad = appdir(appid)
            c = conf_read(ad)
            apk = c.get("APK", "")
            if not os.path.exists(apk):
                out.append("%s: APK missing" % appid)
                continue
            r = apkinfo.info(apk)
            if not r.get("icon_png"):
                out.append("%s: no PNG icon in the APK" % appid)
                continue
            tmp = ad + "/icon.png.tmp"
            open(tmp, "wb").write(r["icon_png"])
            os.chmod(tmp, 0o644)
            os.replace(tmp, ad + "/icon.png")
            ch = False
            if r.get("package") and c.get("PKG") != r["package"]:
                c["PKG"] = r["package"]
                ch = True
            if r.get("leanback") and c.get("LEANBACK") != "1":
                c["LEANBACK"] = "1"
                ch = True
            if ch:
                conf_write(ad, c)
            out.append("%s: %s (%s)" % (appid, r.get("label"), r.get("icon_source")))
        except Exception as e:
            out.append("%s: %s" % (appid, e))
    log("reicon", "; ".join(out))
    return "\n".join(out) + "\n(webOS may show the new icon after a restart)"


def uninstall_all(keep_data=False):
    """runs uninstall.sh detached (it stops atlmgr too)"""
    log("uninstall everything", "keep data" if keep_data else "")
    src = BIN + "/uninstall.sh"
    tmp = "/tmp/webosatl-uninstall.sh"
    shutil.copyfile(src, tmp)
    subprocess.Popen("cp %s/env.sh /tmp/env.sh; sh %s %s > /tmp/webosatl-uninstall.log 2>&1"
                     % (BIN, tmp, "--keep-data" if keep_data else ""), shell=True, start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "uninstalling"


# ---------------------------------------------------------------- status / logs
def status():
    rel = release()
    rs = runtime_state()
    return {"version": rel.get("version", "?"), "payload_version": load_json(PAYLOAD + "/release.json", {}).get("version"),
            "latest": latest.get("version"), "time": time.time(), "ip": tv_ip(), "disk": disk(), "mem": meminfo(),
            "uptime": float(open("/proc/uptime").read().split()[0]),
            "runtime": {"mounted": os.path.exists(RUNTIME + "/AppRun"), "version": rs.get("version", ""),
                        "sha": rs.get("sha256", "")[:12], "wanted": (runtime_wanted().get("version") or ""),
                        "ok": runtime_ok()},
            "atld": bool(py_running("atld.py")), "apkweb": bool(py_running("apkweb.py")),
            "apkweb_url": "http://%s:%d/" % (tv_ip() or "<tv-ip>", APKWEB_PORT),
            "browser": chromium_state(),
            "progress": progress, "settings": settings(), "repo": "https://github.com/" + REPO}


def tail(path, n=64 << 10):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            sz = f.tell()
            f.seek(max(0, sz - n))
            return f.read().decode(errors="replace")
    except OSError as e:
        return str(e)


LOG_FILES = {"mgr": LOG_F, "atld": LOGS + "/atld.log", "apkweb": LOGS + "/apkweb.log", "init": LOGS + "/init.log"}


def log_path(name, appid=""):
    if name in LOG_FILES:
        return LOG_FILES[name]
    if name in ("app", "app.prev", "shim", "vdec", "compat"):
        ad = appdir(appid)
        if name == "compat":
            return conf_read(ad).get("APK", "x.apk")[:-4] + ".compat.txt"
        return {"app": ad + "/app.log", "app.prev": ad + "/app.log.prev", "shim": ad + "/run/shim.log",
                "vdec": ad + "/run/vdec.log"}[name]
    raise ValueError("unknown log " + name)


# ---------------------------------------------------------------- local API for the manager app
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def out(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(b)

    def q(self):
        from urllib.parse import parse_qs, urlparse
        u = urlparse(self.path)
        return u.path, {k: v[0] for k, v in parse_qs(u.query).items()}

    def do_OPTIONS(self):
        self.out(204, b"")

    def do_GET(self):
        path, q = self.q()
        try:
            if path == "/api/status":
                return self.out(200, status())
            if path == "/api/apps":
                return self.out(200, apps())
            if path == "/api/settings":
                return self.out(200, settings())
            if path == "/api/progress":
                return self.out(200, progress)
            if path == "/api/log":
                return self.out(200, tail(log_path(q.get("name", "mgr"), q.get("id", ""))), "text/plain; charset=utf-8")
            if path == "/api/icon":
                with open(appdir(q.get("id", "")) + "/icon.png", "rb") as f:
                    return self.out(200, f.read(), "image/png")
            self.out(404, {"error": "not found"})
        except Exception as e:
            self.out(500, {"error": str(e)})

    def do_POST(self):
        path, q = self.q()
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            i = q.get("id", "")
            if path == "/api/update":  # sync payload + runtime now; force=1 closes running Android apps
                def go():
                    sync_payload()
                    runtime_update(force=q.get("force") == "1")
                    sync_chromium(force=q.get("force") == "1")
                    check_latest()
                threading.Thread(target=go, daemon=True).start()
                return self.out(200, {"ok": True, "msg": "checking"})
            if path == "/api/settings":
                s = load_json(SETTINGS_F, {})
                for k, v in body.items():
                    if k in ("env", "memory") and isinstance(v, dict):  # merge, so one key doesn't wipe the others
                        e = dict(s.get(k) or {})
                        e.update(v)
                        s[k] = {a: b for a, b in e.items() if b not in (None, "")}
                    else:
                        s[k] = v
                save_json(SETTINGS_F, s)
                log("settings", json.dumps(body))
                return self.out(200, settings())
            if path == "/api/app":
                return self.out(200, {"ok": True, "conf": app_set(i, body)})
            if path == "/api/uninstall":
                return self.out(200, {"ok": True, "msg": uninstall(i)})
            if path == "/api/clear":
                return self.out(200, {"ok": True, "msg": clear_data(i)})
            if path == "/api/reicon":
                return self.out(200, {"ok": True, "msg": reicon(i or "all")})
            if path == "/api/install-url":
                return self.out(200, {"ok": True, "msg": "installed " + install_url(str(body.get("url", "")).strip(),
                                                                                str(body.get("title") or "")[:60])})
            if path == "/api/launch":
                appdir(i)
                return self.out(200, luna("luna://com.webos.applicationManager/launch", {"id": i}))
            if path == "/api/stop":
                appdir(i)
                return self.out(200, luna("luna://com.webos.applicationManager/closeByAppId", {"id": i}))
            if path == "/api/restart":
                return self.out(200, {"ok": True, "msg": restart(q.get("what", ""))})
            if path == "/api/uninstall-all":
                return self.out(200, {"ok": True, "msg": uninstall_all(q.get("keep") == "1")})
            self.out(404, {"error": "not found"})
        except Exception as e:
            log("api", path, traceback.format_exc())
            self.out(500, {"error": str(e)})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    if sys.argv[1:2] == ["runtime-update"]:
        ok, msg = runtime_update(force="--force" in sys.argv, out=lambda m: print(m, flush=True))
        print(("OK " if ok else "ERROR ") + msg)
        sys.exit(0 if ok else 1)
    log("== atlmgr %s start (pid %d)" % (version(), os.getpid()))
    try:
        srv = Server(("0.0.0.0" if settings().get("mgr_lan") else "127.0.0.1", MGR_PORT), H)
    except OSError as e:
        log("port %d busy (%s): another atlmgr running? exiting" % (MGR_PORT, e))
        return
    threading.Thread(target=update_loop, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
