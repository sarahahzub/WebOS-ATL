#!/usr/bin/env python3
"""Simulation test for atld's memory policy (no TV needed): fake luna-send (SAM running list, closeByAppId log,
getForegroundAppInfo subscription fed from a file), fake AppRun (sleep), temp app dirs with run/cmd FIFOs.
usage: python3 test_atld_memory.py  (exit 0 = all scenarios pass)"""
import json, os, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
T = tempfile.mkdtemp(prefix="atldtest-")
ATL, APPS, BIN = f"{T}/atl", f"{T}/apps", f"{T}/bin"
for d in (ATL, f"{ATL}/runtime", APPS, BIN, f"{T}/xdg"):
    os.makedirs(d)
open(f"{ATL}/runtime/AppRun", "w").write("#!/bin/sh\nexec sleep 600\n")
os.chmod(f"{ATL}/runtime/AppRun", 0o755)
open(f"{T}/fake.apk", "w").write("x")
# fake luna-send: closeByAppId -> log + drop from running list; running -> list; -i subscription -> tail fg file
open(f"{BIN}/luna-send", "w").write(f"""#!/usr/bin/env python3
import json, sys, time, os, fcntl
T = {T!r}
lk = open(T + "/sam.lock", "a")
fcntl.flock(lk, fcntl.LOCK_EX)
a = sys.argv[1:]
if a[0] == "-i":
    fcntl.flock(lk, fcntl.LOCK_UN)
    seen = 0
    while True:
        try:
            lines = open(T + "/fg").read().splitlines()
        except OSError:
            lines = []
        for l in lines[seen:]:
            print(json.dumps({{"appId": l, "returnValue": True}}), flush=True)
        seen = len(lines)
        time.sleep(0.1)
uri, payload = a[2], json.loads(a[3])
run = T + "/sam.json"
r = json.load(open(run)) if os.path.exists(run) else []
if uri.endswith("/running"):
    print(json.dumps({{"running": [{{"id": i}} for i in r], "returnValue": True}}))
elif uri.endswith("/closeByAppId"):
    open(T + "/closed", "a").write(payload["id"] + "\\n")
    json.dump([i for i in r if i != payload["id"]], open(run + ".tmp", "w"))
    os.replace(run + ".tmp", run)
    print('{{"returnValue": true}}')
""")
os.chmod(f"{BIN}/luna-send", 0o755)

src = open(f"{HERE}/atld.py").read()
src = src.replace('"/tmp/xdg/wayland-"', f'{T + "/xdg/wayland-"!r}')
open(f"{T}/atld.py", "w").write(src)
shutil.copy(f"{HERE}/conf.py", f"{T}/conf.py")


def mkapp(i):
    d = f"{APPS}/org.webosatl.app.{i}"
    os.makedirs(d)
    open(f"{d}/apk.conf", "w").write(f"APK={T}/fake.apk\n")


for i in ("a", "b", "c", "music"):
    mkapp(i)
json.dump({"memory": {"idle_stop_min": 0.05, "allow_background": ["org.webosatl.app.music"],
                      "keep": ["com.example.doorbell"], "overlay": ["com.example.doorbell"]}}, open(f"{ATL}/settings.json", "w"))
json.dump(["com.webos.app.home", "com.example.doorbell", "org.webosatl.chromium", "youtube.leanback.v4", "com.webos.app.livetv"],
          open(f"{T}/sam.json", "w"))
open(f"{T}/fg", "w").write("com.webos.app.home\n")

env = dict(os.environ, PATH=f"{BIN}:" + os.environ["PATH"], WEBOSATL_BASE=ATL, WEBOSATL_APPS=APPS)
d = subprocess.Popen([sys.executable, f"{T}/atld.py"], env=env, cwd=T)
time.sleep(3)


def cmd(app, line):
    with open(f"{APPS}/org.webosatl.app.{app}/run/cmd", "w") as f:
        f.write(line + "\n")


def sam_add(*ids):
    import fcntl
    with open(f"{T}/sam.lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        r = json.load(open(f"{T}/sam.json"))
        json.dump(r + list(ids), open(f"{T}/sam.json.tmp", "w"))
        os.replace(f"{T}/sam.json.tmp", f"{T}/sam.json")


def fg(app):
    open(f"{T}/fg", "a").write(app + "\n")


def closed():
    try:
        return open(f"{T}/closed").read().split()
    except OSError:
        return []


def log():
    return open(f"{ATL}/logs/atld.log").read()


def alive(app):
    ps = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout
    return log().count(f"started org.webosatl.app.{app}") > log().count(f"stopped org.webosatl.app.{app}")


fails = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)


try:
    # 1: launch A -> Chrome and YouTube (background) are closed, Home/Cam/LiveTV are not
    sam_add("org.webosatl.app.a")
    cmd("a", f"start {T}/xdg/wayland-a")
    time.sleep(2)
    fg("org.webosatl.app.a")
    time.sleep(2)
    c = closed()
    check("launch closes Chrome + YouTube", "org.webosatl.chromium" in c and "youtube.leanback.v4" in c)
    check("launch keeps Home, doorbell (keep), Live TV", not {"com.webos.app.home", "com.example.doorbell", "com.webos.app.livetv"} & set(c))
    check("A running", alive("a"))
    # 2: launch B -> A stopped and its wrapper closed
    sam_add("org.webosatl.app.b")
    cmd("b", f"start {T}/xdg/wayland-b")
    time.sleep(3)
    fg("org.webosatl.app.b")
    time.sleep(1)
    check("launch B stops A", not alive("a") and alive("b"))
    check("A wrapper closed via SAM", "org.webosatl.app.a" in closed())
    check("start logs memory", "avail=" in log() and "psi10=" in log())
    check("stop logs peak RSS", "peak_rss=" in log())
    # 3: doorbell camera on top of B for longer than idle -> B keeps running
    fg("com.example.doorbell")
    time.sleep(5)
    check("doorbell (keep) overlay does not idle-stop B", alive("b"))
    # 4: back to Home -> B idle-stopped after idle_stop_min (3 s)
    fg("org.webosatl.app.b")
    time.sleep(1)
    fg("com.webos.app.home")
    time.sleep(6)
    check("B idle-stopped in background", not alive("b") and "idle stop org.webosatl.app.b" in log())
    # 5: allow_background app is not idle-stopped
    sam_add("org.webosatl.app.music")
    cmd("music", f"start {T}/xdg/wayland-music")
    time.sleep(2)
    fg("org.webosatl.app.music")
    time.sleep(1)
    fg("com.webos.app.home")
    time.sleep(6)
    check("allow_background app survives idle", alive("music"))
    # 6: Chrome comes to the front -> ATL apps stopped
    sam_add("org.webosatl.chromium")
    fg("org.webosatl.chromium")
    time.sleep(3)
    check("Chrome in front stops ATL apps", not alive("music"))
    check("Chrome itself not closed when it is in front", closed().count("org.webosatl.chromium") == 1)
    # 7: single_app off -> nothing closed
    json.dump({"memory": {"single_app": False, "idle_stop_min": 0}}, open(f"{ATL}/settings.json", "w"))
    n = len(closed())
    sam_add("org.webosatl.app.a", "org.webosatl.app.c")
    cmd("a", f"start {T}/xdg/wayland-a")
    time.sleep(2)
    cmd("c", f"start {T}/xdg/wayland-c")
    time.sleep(2)
    check("single_app off keeps both", alive("a") and alive("c") and len(closed()) == n)
    check("atld still running", d.poll() is None)
finally:
    d.kill()
    subprocess.run(["pkill", "-f", f"{T}/atl/v2/AppRun"], stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-f", "sleep 600"], stderr=subprocess.DEVNULL)
    if fails:
        print(log())
    shutil.rmtree(T, ignore_errors=True)
print("ALL OK" if not fails else "%d FAILED" % len(fails))
sys.exit(1 if fails else 0)
