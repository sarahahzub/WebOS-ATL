#!/usr/bin/env python3
"""atld: root helper that runs Android apps (ATL) for their webOS wrapper apps.

Each installed Android app gets a tiny native webOS wrapper app (org.webosatl.app.<slug>, made by apkweb). SAM execs
its bin/run (run.sh), which runs `xdgshim -1` itself so the window comes from the launched app's own process (LSM only
shows that), then writes "start <socket>" into <appdir>/run/cmd ("stop" on close). atld starts or stops
android-translation-layer as root, unjailed, on that socket.

apk.conf in the wrapper dir:  APK=<path>  ACTIVITY=<optional>  ARGS=<optional>  ENV_<NAME>=<value>
Runtime: /media/developer/webosatl/runtime.sqfs, loop-mounted at /media/developer/webosatl/runtime by init.sh.
Defaults (each overridable with ENV_<NAME> in apk.conf): ATL_IS_TELEVISION=1 (Android TV profile, inline dialogs,
remote keys), ATL_DENSITY=320 (960x540 dp like Android TV); dex2oat verify-only (space) unless ARGS has -Xcompiler.
Global defaults (webOS ATL manager -> Settings): settings.json {"env": {NAME: value}, "dex2oat": "verify"|"full"};
apk.conf ENV_<NAME> still wins per app.
Log: /media/developer/webosatl/logs/atld.log

Memory policy (settings.json "memory": {...}, see MEM_DEFAULTS; a 1.5 GB TV has room for one big Android app):
- single_app: starting an Android app first stops every other running one (and closes its wrapper through SAM) and
  closes the background apps in close_apps (browsers, YouTube, Netflix, ...). When the browser or an Android app
  comes to the front by other means, the other Android apps are stopped too.
- idle_stop_min: an Android app that has not been in the foreground for that long is stopped (allow_background exempts it).
- never closed: Home, Live TV, inputs, settings/system apps, the webOS ATL manager, anything in keep.
- oom_score_adj: Android apps (and their WebKit children) are the kernel's first choice under memory pressure, not webOS.
- every start/stop logs MemAvailable, SwapFree and memory pressure (PSI); stop logs the app's peak RSS.
Off switch: settings.json {"memory": {"single_app": false, "idle_stop_min": 0}}.
"""
import json, os, select, signal, socket, struct, subprocess, threading, time

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conf import BASE, BIN, RUNTIME, HOME, LOGS, SETTINGS_F, APPS, PREFIX, MANAGER_ID, ensure_dirs  # noqa: E402

ensure_dirs()
LOG = open(f"{LOGS}/atld.log", "a", buffering=1)
BROWSER = "org.webosatl.chromium"  # optional add-on
running = {}  # appid -> (app Popen,)
_ni = None

MEM_DEFAULTS = {
    "single_app": True,
    "idle_stop_min": 10,
    "allow_background": [],
    "keep": [],
    "close_apps": [BROWSER, "youtube.leanback.v4", "netflix", "amazon", "com.webos.app.browser",
                   "com.disney.disneyplus-prod", "in.startv.hotstar", "spotify-beehive"],
    "oom_score_adj": 500,
}
# never closed, whatever the settings say
NEVER = {"com.webos.app.home", MANAGER_ID, "com.webos.app.livetv",
         "com.webos.app.inputcommon", "com.webos.app.hdmi1", "com.webos.app.hdmi2", "com.webos.app.hdmi3",
         "com.webos.app.hdmi4", "com.palm.app.settings", "com.webos.app.screensaver", "com.webos.app.notificationcenter"}
# apps shown on top of others (the app underneath has not been left); settings.json memory.overlay adds more
OVERLAY = {"com.webos.app.screensaver", "com.webos.app.notificationcenter"}
fg = {"app": None, "ok": False}  # foreground app from SAM (getForegroundAppInfo subscription)
bg_since = {}  # ATL appid -> uptime when it left the foreground
_luna = []  # fire-and-forget luna-send processes (reaped in the main loop)


def netinput_move(dx, dy):
    """Move the webOS pointer (same socket the LG phone remote uses)."""
    global _ni
    msg = ("type:move\ndx:%d\ndy:%d\ndown:0\n\n" % (dx, dy)).encode()
    for _ in range(2):
        try:
            if _ni is None:
                _ni = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                _ni.connect("/tmp/netinput.pointer.sock")
            _ni.sendall(struct.pack(">I", len(msg)) + msg)
            return
        except OSError:
            _ni = None


def now():
    # /proc/uptime, the clock run.sh stamps run/launch.log with (launch timing; needs TV verification)
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return time.monotonic()


def log(*a):
    LOG.write("%.2f %s\n" % (now(), " ".join(map(str, a))))


def conf(appdir):
    c = {}
    try:
        for line in open(f"{appdir}/apk.conf"):
            if "=" in line:
                k, v = line.strip().split("=", 1)
                c[k] = v
    except OSError:
        pass
    return c


def settings():
    try:
        return json.load(open(SETTINGS_F))
    except (OSError, ValueError):
        return {}


def memset():
    m = dict(MEM_DEFAULTS)
    u = settings().get("memory")
    if isinstance(u, dict):
        m.update(u)
    return m


def meminfo():
    """'avail=..MB swapfree=..MB psi10=..' for the launch/stop log."""
    v = {}
    try:
        for line in open("/proc/meminfo"):
            k, rest = line.split(":", 1)
            if k in ("MemAvailable", "SwapFree", "SwapTotal"):
                v[k] = int(rest.split()[0]) // 1024
    except (OSError, ValueError):
        pass
    psi = "?"
    try:
        some = open("/proc/pressure/memory").readline().split()  # some avg10=.. avg60=.. avg300=.. total=..
        psi = some[1].split("=")[1]
    except (OSError, IndexError):
        pass
    return "avail=%sMB swapfree=%sMB/%sMB psi10=%s" % (v.get("MemAvailable", "?"), v.get("SwapFree", "?"),
                                                         v.get("SwapTotal", "?"), psi)


def peak_rss(pid):
    try:
        for line in open(f"/proc/{pid}/status"):
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return None


def luna(uri, payload, wait=False, timeout=5):
    args = ["luna-send", "-n", "1", uri, json.dumps(payload)]
    try:
        if wait:
            r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
            return json.loads(r.stdout or "{}")
        _luna.append(subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL))
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        log("luna", uri, "failed:", e)
    return {}


def sam_running():
    r = luna("luna://com.webos.applicationManager/running", {}, wait=True)
    return [a.get("id") for a in r.get("running", []) if a.get("id")]


def protected(appid, m):
    return appid in NEVER or appid in (m.get("keep") or [])


def reclaim(keep_id, why):
    """single_app: stop every ATL app except keep_id and close the heavy background apps (close_apps)."""
    m = memset()
    if not m.get("single_app"):
        return
    before = meminfo()
    gone = []
    for other in list(running):
        if other != keep_id and not protected(other, m):
            luna("luna://com.webos.applicationManager/closeByAppId", {"id": other})
            stop(other)
            gone.append(other)
    try:
        sam = sam_running()
    except Exception as e:  # never let SAM trouble block a launch
        log("reclaim: SAM running list failed:", e)
        sam = []
    close = set(m.get("close_apps") or [])
    for a in sam:
        if a == keep_id or protected(a, m) or a in gone:
            continue
        if a in close or (a.startswith(PREFIX) and a not in running):  # heavy apps, orphan ATL wrappers
            luna("luna://com.webos.applicationManager/closeByAppId", {"id": a})
            gone.append(a)
    if gone:
        log("reclaim", why, "for", keep_id, "closed", ",".join(gone), "|", before, "->", meminfo())


def fg_watch():
    """Track the foreground app (SAM). Restarts the subscription if luna-send dies."""
    while True:
        try:
            p = subprocess.Popen(["luna-send", "-i", "luna://com.webos.applicationManager/getForegroundAppInfo",
                                  '{"subscribe":true}'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            _sub[:] = [p]
            for line in p.stdout:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    a = json.loads(line).get("appId")
                except ValueError:
                    continue
                if a is None:
                    continue
                with _fglock:
                    fg["ok"] = True
                    if a != fg["app"]:
                        fg["app"] = a
                        _fgq.append(a)
            p.wait()
        except OSError as e:
            log("fg_watch:", e)
        fg["ok"] = False
        time.sleep(10)


_sub = []  # the running getForegroundAppInfo subscription (ended with atld, see on_term)
_fglock = threading.Lock()
_fgq = []  # foreground changes, handled in the main loop


def memory_tick():
    """Main loop: foreground changes and idle stops."""
    with _fglock:
        changes, _fgq[:] = list(_fgq), []
    t = now()
    m = memset()
    for a in changes:
        if a in OVERLAY or a in (m.get("overlay") or []):  # screensaver / notification on top: the app underneath has not been left
            continue
        bg_since.pop(a, None)
        for other in running:
            if other != a:
                bg_since.setdefault(other, t)
        if a in running or a == BROWSER:  # a big app came to the front: free memory for it
            reclaim(a, "foreground")
    idle = float(m.get("idle_stop_min") or 0)
    if not idle or not fg["ok"]:
        return
    for appid in list(running):
        s = bg_since.get(appid)
        if s is not None and appid != fg["app"] and fg["app"] not in OVERLAY and fg["app"] not in (m.get("overlay") or []) and t - s > idle * 60 \
                and appid not in (m.get("allow_background") or []) and not protected(appid, m):
            log("idle stop", appid, "in background %.0f min" % ((t - s) / 60))
            luna("luna://com.webos.applicationManager/closeByAppId", {"id": appid})
            stop(appid)
    for p in list(_luna):
        if p.poll() is not None:
            _luna.remove(p)


def kill_orphans(appid):
    """An ATL app started by an earlier atld (atld restarted by an update) is not in `running`: find it by its APK."""
    apk = conf(f"{APPS}/{appid}").get("APK")
    if not apk:
        return
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            cmd = open(f"/proc/{d}/cmdline", "rb").read().split(b"\0")
        except OSError:
            continue
        if cmd and cmd[0].endswith(b"/AppRun") and apk.encode() in cmd:
            pid = int(d)
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
                time.sleep(0.5)
            log("stopped orphan", appid, "pid", pid, meminfo())


def boost_vdec(appid, secs=20):
    """The wrapper (jailed, cannot raise priority) starts the hardware-decode helper atlvdec; atld (root) gives it and
    its threads nice -10, so a busy TV feeds frames on time (late feeds = a frame shown one refresh late)."""
    tag = ("/tmp/xdg/atlvdec-" + appid.replace(".", "-")).encode()
    end = time.time() + secs
    while time.time() < end:
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                cmd = open(f"/proc/{d}/cmdline", "rb").read().split(b"\0")
            except OSError:
                continue
            if len(cmd) > 1 and cmd[0].endswith(b"/atlvdec") and cmd[1] == tag:
                n = 0
                for t in os.listdir(f"/proc/{d}/task"):
                    try:
                        os.setpriority(os.PRIO_PROCESS, int(t), -10)
                        n += 1
                    except OSError:
                        pass
                log("atlvdec", appid, "pid", d, "nice -10 for", n, "threads")
                return
        time.sleep(1)


def stop(appid):
    procs = running.pop(appid, None)
    bg_since.pop(appid, None)
    if not procs:
        kill_orphans(appid)
        return
    peak = peak_rss(procs[-1].pid)
    rc = procs[-1].poll()          # already gone before we were asked to stop it? then how did it end
    if rc is not None:
        log("app had exited before stop:", appid, "rc", rc, "(killed by signal %d)" % -rc if rc < 0 else "")
    for p in reversed(procs):
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    time.sleep(0.5)
    for p in procs:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()
    log("stopped", appid, "peak_rss=%sMB" % peak if peak else "", meminfo())


def start(appid, appdir, sock):
    stop(appid)
    try:
        reclaim(appid, "launch")
    except Exception as e:
        log("reclaim failed:", e)
    c = conf(appdir)
    apk = c.get("APK")
    if not apk or not os.path.exists(apk):
        log("start", appid, "missing APK", apk)
        return
    env = dict(os.environ, XDG_RUNTIME_DIR="/tmp/xdg")
    data = f"{HOME}/data"
    os.makedirs(data, exist_ok=True)
    # low4g.so: ART low-4GB allocator fix; atlfix.so: libpulse 17 <-> LG PulseAudio 15 protocol fix (audio),
    # line-buffered stdout, native crash backtrace (atlfix/atlfix.c)
    preload = ":".join(p for p in (f"{BIN}/low4g.so", f"{BIN}/atlfix.so") if os.path.exists(p))
    env.update(WAYLAND_DISPLAY=sock, HOME=HOME, ANDROID_APP_DATA_DIR=data,
               GSK_RENDERER=c.get("GSK_RENDERER", "cairo"),
               PULSE_SERVER="unix:/var/run/pulse/native",
               SHARUN_ALLOW_LD_PRELOAD="1", LD_PRELOAD=preload)
    env.update({k[4:]: v for k, v in c.items() if k.startswith("ENV_")})
    root = RUNTIME
    if not os.path.exists(f"{root}/AppRun"):
        log("start", appid, "runtime not mounted at", root)
        return
    args = c.get("ARGS", "").split()
    st = settings()
    tv = {"ATL_IS_TELEVISION": "1", "ATL_DENSITY": "320"}
    tv.update({str(k): str(v) for k, v in (st.get("env") or {}).items() if v != ""})
    for k, v in tv.items():
        if "ENV_" + k not in c:
            env[k] = v
    if st.get("dex2oat", "verify") == "verify" and not any(a.startswith("-Xcompiler:") for a in args) \
            and os.path.exists(f"{BIN}/dex2oat-verify"):
        args = ["-X", f"-Xcompiler:{BIN}/dex2oat-verify"] + args
    if env.get("ATL_HWDEC") == "1" and not env.get("ATL_HWDEC_SOCK"):  # atlvdec started by the wrapper (run.sh)
        env["ATL_HWDEC_SOCK"] = "/tmp/xdg/atlvdec-" + appid.replace(".", "-")
    cmd = [f"{root}/AppRun"] + args + [apk]
    if c.get("ACTIVITY"):
        cmd += ["-l", c["ACTIVITY"]]
    hook = f"{root}/bin/01-path-mapping-hardcoded.hook"
    if os.path.exists(hook):  # quick-sharun /tmp/SH1 etc. -> image lib/bin/share (WebKit helper processes);
        subprocess.run(["sh", hook], env=dict(os.environ, APPDIR=root),  # the sharun AppRun skips it under atld
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    try:  # keep the previous run's log: crashes are often noticed after the relaunch
        os.replace(f"{appdir}/app.log", f"{appdir}/app.log.prev")
    except OSError:
        pass
    app = subprocess.Popen(cmd, env=env, cwd=BASE, start_new_session=True,
                           stdout=open(f"{appdir}/app.log", "w"), stderr=subprocess.STDOUT)
    running[appid] = (app,)
    adj = memset().get("oom_score_adj")
    if adj not in (None, "", 0):
        try:  # inherited by the WebKit/helper processes ATL forks later
            with open(f"/proc/{app.pid}/oom_score_adj", "w") as f:
                f.write(str(int(adj)))
        except (OSError, ValueError):
            pass
    log("started", appid, "pid", app.pid, meminfo(), " ".join(cmd))
    if env.get("ATL_HWDEC") == "1":
        threading.Thread(target=boost_vdec, args=(appid,), daemon=True).start()


def fifos():
    out = {}
    for d in os.listdir(APPS):
        if d.startswith(PREFIX):
            f = f"{APPS}/{d}/run/cmd"
            os.makedirs(os.path.dirname(f), exist_ok=True)
            if not os.path.exists(f):
                os.mkfifo(f)
            os.chmod(f, 0o666)
            os.chmod(os.path.dirname(f), 0o777)
            out[d] = f
    return out


def on_term(sig, frame):
    """init.sh restart/stop: end the luna-send subscription too (it would outlive atld); Android apps keep running
    (own sessions) and the next atld finds them by their APK (kill_orphans)"""
    for p in _sub:
        try:
            p.kill()
        except OSError:
            pass
    log("== atld exit (signal %d)" % sig)
    os._exit(0)


def main():
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    log("== atld start")
    threading.Thread(target=fg_watch, daemon=True).start()
    fds = {}
    while True:
        try:
            memory_tick()
        except Exception as e:  # the memory policy must never take atld down
            log("memory_tick:", e)
        for appid, f in fifos().items():
            if appid not in fds.values():
                fd = os.open(f, os.O_RDONLY | os.O_NONBLOCK)
                fds[fd] = appid
        r, _, _ = select.select(list(fds), [], [], 2.0)
        for fd in r:
            appid = fds[fd]
            data = os.read(fd, 4096).decode(errors="replace")
            if not data:  # writer closed: reopen so select doesn't spin
                os.close(fd)
                del fds[fd]
                continue
            for line in data.splitlines():
                cmd = line.split()
                if not cmd:
                    continue
                if cmd[0] == "move" and len(cmd) == 3:
                    netinput_move(int(cmd[1]), int(cmd[2]))
                    continue
                log("cmd", appid, line)
                if cmd[0] == "start" and len(cmd) > 1 and cmd[1].startswith("/tmp/xdg/wayland-"):
                    start(appid, f"{APPS}/{appid}", cmd[1])
                elif cmd[0] == "stop":
                    stop(appid)
        for appid, procs in list(running.items()):
            app = procs[-1]
            if app.poll() is not None:
                rc = app.returncode
                log("app exited", appid, "rc", rc, "(signal %d)" % -rc if rc < 0 else "")
                stop(appid)


if __name__ == "__main__":
    main()
