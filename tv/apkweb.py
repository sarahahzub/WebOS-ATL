#!/usr/bin/env python3
"""apkweb: install Android APKs on the TV from a phone/laptop browser (runs ON THE TV as root).

  http://<tv-ip>:8790/   upload an .apk (or give a download link) -> wrapper app org.webosatl.app.<slug> is built
                         and installed; it shows up in the TV's app list. Lists/removes installed Android apps.
                         Needs the PIN shown in the webOS ATL app (Android apps run as root: nobody else on the
                         network, and no web page open in the TV's browser, may install them). The PIN is kept in a
                         SameSite=Strict cookie; 10 wrong PINs lock the page for a minute.
                         "Type on TV": while an Android text field is focused on the TV (xdgshim writes
                         <app>/run/ti.state), text typed/pasted here goes into it (xdgshim FIFO <app>/run/ti.in).
Stdlib only. Started at boot by init.sh (Homebrew Channel init.d hook 60-webosatl).
"""
import hmac, html, io, json, os, re, secrets, shutil, struct, subprocess, sys, tarfile, threading, time, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conf import BASE, BIN, APKS, APPS, LOGS, PREFIX, APKWEB_PORT, ensure_dirs  # noqa: E402

ensure_dirs()
PORT = int(os.environ.get("APKWEB_PORT", str(APKWEB_PORT)))
LOCK = threading.Lock()
LOG = open(f"{LOGS}/apkweb.log", "a", buffering=1)
PIN_F = BASE + "/apkweb.pin"
PUBLIC = {"/manifest.json", "/sw.js", "/icon.png", "/pin"}  # everything else needs the PIN cookie
_fails = []  # times of wrong PIN attempts (rate limit)


def pin():
    """6-digit PIN of the phone page (created once, root-only file; the manager app shows it)"""
    try:
        p = open(PIN_F).read().strip()
        if re.fullmatch(r"\d{6}", p):
            return p
    except OSError:
        pass
    p = "%06d" % secrets.randbelow(1000000)
    fd = os.open(PIN_F + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(p + "\n")
    os.replace(PIN_F + ".tmp", PIN_F)
    return p
# per-package apk.conf defaults written at every (re)install: Android package -> {apk.conf key: value}
PKG_DEFAULTS = {
    # SmartTube (F-Droid / stable / beta builds): at SDK 21 Cronet's property shim calls dlopen("libc.so", RTLD_NOLOAD),
    # which bionic_translation can't resolve -> SIGTRAP ~20 s in; at SDK 28 it takes another path and plays
    "app.smarttube.fdroid": {"ARGS": "--sdk-int 28"},
    "com.teamsmart.videomanager.tv": {"ARGS": "--sdk-int 28"},
    "com.liskovsoft.smarttubetv.beta": {"ARGS": "--sdk-int 28"},
}


def known_pkg(manifest):
    """PKG_DEFAULTS package named in a binary AndroidManifest.xml (exact string-pool entry, UTF-16 or UTF-8), or None.
    Fallback for when apkinfo can't read the package."""
    for p in PKG_DEFAULTS:
        if (struct.pack("<H", len(p)) + p.encode("utf-16-le") + b"\0\0" in manifest
                or bytes((len(p), len(p))) + p.encode() + b"\0" in manifest):
            return p
    return None


def log(*a):
    LOG.write("%s %s\n" % (time.strftime("%H:%M:%S"), " ".join(map(str, a))))


def luna(uri, payload):
    r = subprocess.run(["luna-send", "-n", "1", "-f", uri, json.dumps(payload)], capture_output=True, text=True, timeout=60)
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"raw": r.stdout + r.stderr}


def slug(s):
    s = re.sub(r"[^a-z0-9]+", "", s.lower())
    return (s or "app")[:40]


def ar_archive(members):
    out = io.BytesIO()
    out.write(b"!<arch>\n")
    for name, data in members:
        hdr = "%-16s%-12d%-6d%-6d%-8o%-10d`\n" % (name, int(time.time()), 0, 0, 0o100644, len(data))
        out.write(hdr.encode())
        out.write(data)
        if len(data) % 2:
            out.write(b"\n")
    return out.getvalue()


def tgz(entries):
    """entries: list of (path, bytes or None for dir, mode)"""
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w:gz") as t:
        for path, data, mode in entries:
            ti = tarfile.TarInfo(path)
            ti.mtime = int(time.time())
            ti.mode = mode
            if data is None:
                ti.type = tarfile.DIRTYPE
                t.addfile(ti)
            else:
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))
    return b.getvalue()


def apk_info(apk_path):
    """label, package, Android TV flag and a PNG launcher icon (apkinfo.py, stdlib); None if the APK can't be parsed"""
    try:
        import apkinfo
        t = time.time()
        r = apkinfo.info(apk_path)
        log("apkinfo", r.get("package"), r.get("label"), r.get("icon_source"), "%.1f s" % (time.time() - t))
        return r
    except Exception as e:
        log("apkinfo failed", repr(e))
        return None


def ensure_vdec_role():
    """luna-service2 role for the hardware video decode helper (cloned from a wrapper app's role, see ls2-role.sh)"""
    try:
        r = subprocess.run(["sh", f"{BIN}/ls2-role.sh"], capture_output=True, text=True, timeout=60)
        log("ls2-role:", (r.stdout + r.stderr).strip()[-200:])
    except Exception as e:
        log("ls2-role failed", repr(e))


def build_and_install(apk_path, title, activity, info_=None):
    with zipfile.ZipFile(apk_path) as z:
        if "classes.dex" not in z.namelist():
            raise ValueError("not an Android APK (no classes.dex)")
        manifest = z.read("AndroidManifest.xml") if "AndroidManifest.xml" in z.namelist() else b""
    hi = info_ if info_ is not None else apk_info(apk_path)
    icon = (hi or {}).get("icon_png")
    if hi and not title:
        title = (hi.get("label") or "App")[:60]
    title = title or "App"
    name = slug(title)
    appid = PREFIX + name
    final_apk = f"{APKS}/{name}.apk"
    if os.path.abspath(apk_path) != final_apk:
        shutil.move(apk_path, final_apk)
    if not icon:
        icon = open(f"{BIN}/default-icon.png", "rb").read()
    ver = "1.0.%d" % (int(time.time()) % 100000 // 10)
    base = f"./usr/palm/applications/{appid}"
    info = {"id": appid, "version": ver, "vendor": "webOS ATL", "type": "native", "main": "bin/run",
            "title": title, "icon": "icon.png", "largeIcon": "icon.png", "iconColor": "#1b1d22",
            "noSplashOnLaunch": True, "disableBackHistoryAPI": True}
    conf = f"APK={final_apk}\n" + (f"ACTIVITY={activity}\n" if activity else "")
    pkg = (hi or {}).get("package") or known_pkg(manifest)
    if hi or pkg:
        conf += "PKG=%s\n" % (pkg or "")
        if hi and hi.get("leanback"):  # Android TV app; DPAD=keys (arrows as keys, not pointer) can be set in the manager
            conf += "LEANBACK=1\n"
    conf += "".join(f"{k}={v}\n" for k, v in PKG_DEFAULTS.get(pkg, {}).items())
    files = [("./usr", None, 0o755), ("./usr/palm", None, 0o755), ("./usr/palm/applications", None, 0o755),
             (base, None, 0o755), (base + "/bin", None, 0o755), (base + "/run", None, 0o777),
             (base + "/appinfo.json", json.dumps(info).encode(), 0o644),
             (base + "/apk.conf", conf.encode(), 0o644),
             (base + "/icon.png", icon, 0o644),
             (base + "/bin/run", open(f"{BIN}/run.sh", "rb").read(), 0o755),
             (base + "/bin/xdgshim", open(f"{BIN}/xdgshim", "rb").read(), 0o755)]
    control = ("Package: %s\nVersion: %s\nSection: misc\nPriority: optional\nArchitecture: arm\n"
               "Installed-Size: 600000\nMaintainer: N/A <nobody@example.com>\nDescription: webOS ATL: Android app %s\n"
               "webOS-Package-Format-Version: 2\nwebOS-Packager-Version: x.y.x\n") % (appid, ver, title)
    data_tgz = tgz(files)
    ipk = ar_archive([("debian-binary", b"2.0\n"), ("control.tar.gz", tgz([("./control", control.encode(), 0o644)])),
                      ("data.tar.gz", data_tgz)])
    p = f"/tmp/{appid}.ipk"
    open(p, "wb").write(ipk)
    r = luna("luna://com.webos.appInstallService/dev/install", {"id": appid, "ipkUrl": p, "subscribe": False})
    time.sleep(3)
    # an already-installed id is not replaced by the installer: unpack in place as well
    with tarfile.open(fileobj=io.BytesIO(data_tgz), mode="r:gz") as t:
        t.extractall(APPS[:-len("/usr/palm/applications")])
    os.chmod(f"{APPS}/{appid}/run", 0o777)
    os.remove(p)
    log("installed", appid, final_apk, r)
    threading.Thread(target=ensure_vdec_role, daemon=True).start()
    threading.Thread(target=compat_report, args=(name, final_apk), daemon=True).start()
    return appid


def install_url(url, title="", activity="", progress=None):
    """download an APK (http/https) into APKS and install it; progress(done_bytes, total_bytes) is optional"""
    import urllib.request
    if not re.match(r"https?://", url):
        raise ValueError("give an http(s) link to an .apk")
    os.makedirs(APKS, exist_ok=True)
    tmp = f"{APKS}/.download-{os.getpid()}-{threading.get_ident()}.apk"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "webosatl"})
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            free = shutil.disk_usage(BASE).free
            if total and total * 2 + (150 << 20) > free:
                raise ValueError("Not enough space on TV (%d MB free)" % (free >> 20))
            done = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if done > 400 * 1024 * 1024:
                    raise ValueError("APK too big")
                if progress:
                    progress(done, total)
        log("downloaded", url, os.path.getsize(tmp))
        return build_and_install(tmp, title, activity)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def compat_report(name, apk):
    """which Android APIs does the app reference that ATL lacks? written to apks/<name>.compat.txt"""
    try:
        import apkcheck
        res = apkcheck.check(apk)
        lines = [apkcheck.summary(res, 12), ""]
        lines += ["method %s.%s" % (c, m) for c, ms in res["missing_methods"].items() for m in ms]
        lines += ["class  " + c for c in res["missing_classes"]]
        open(f"{APKS}/{name}.compat.txt", "w").write("\n".join(lines) + "\n")
    except Exception as e:
        open(f"{APKS}/{name}.compat.txt", "w").write("compatibility check failed: %s\n" % e)
        log("compat failed", name, repr(e))


def remove(appid):
    if not appid.startswith(PREFIX) or "/" in appid:
        raise ValueError("bad id")
    luna("luna://com.webos.applicationManager/closeByAppId", {"id": appid})
    conf = {}
    try:
        for line in open(f"{APPS}/{appid}/apk.conf"):
            if "=" in line:
                k, v = line.strip().split("=", 1)
                conf[k] = v
    except OSError:
        pass
    luna("luna://com.webos.appInstallService/dev/remove", {"id": appid})
    time.sleep(2)
    shutil.rmtree(f"{APPS}/{appid}", ignore_errors=True)
    apk = conf.get("APK", "")
    if apk.startswith(APKS + "/") and os.path.exists(apk):
        os.remove(apk)
    log("removed", appid)


def installed():
    out = []
    for d in sorted(os.listdir(APPS)):
        if d.startswith(PREFIX):
            try:
                t = json.load(open(f"{APPS}/{d}/appinfo.json")).get("title", d)
            except (OSError, ValueError):
                t = d
            out.append((d, t))
    return out


def shim_alive(appid):
    """the app's xdgshim holds run/ti.in open (pids differ inside the jail): a non-blocking writer open fails without it"""
    try:
        os.close(os.open(f"{APPS}/{appid}/run/ti.in", os.O_WRONLY | os.O_NONBLOCK))
        return True
    except OSError:
        return False


def textfield():
    """the focused Android text field on the TV (newest run/ti.state with focused=1 and a live shim), or None"""
    best = None
    for d in os.listdir(APPS):
        f = f"{APPS}/{d}/run/ti.state"
        if not d.startswith(PREFIX) or not os.path.exists(f):
            continue
        try:
            st = dict(l.split("=", 1) for l in open(f).read().splitlines() if "=" in l)
            m = os.path.getmtime(f)
        except (OSError, ValueError):
            continue
        if st.get("focused") != "1" or not shim_alive(d):
            continue
        if not best or m > best["mtime"]:
            try:
                text = bytes.fromhex(st.get("text", "")).decode("utf-8", "replace")
            except ValueError:
                text = ""
            best = {"app": d, "mtime": m, "text": text, "shown": st.get("shown") == "1",
                    "password": st.get("purpose") == "8" or (int(st.get("hint", "0") or 0) & 64) != 0}
    if best:
        best["title"] = dict(installed()).get(best["app"], best["app"])
        if best["password"]:
            best["text"] = ""
    return best


def type_text(text, replace, enter):
    tf = textfield()
    if not tf:
        return "No text field is focused on the TV"
    fd = os.open(f"{APPS}/{tf['app']}/run/ti.in", os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(fd, ("%d %d %s\n" % (bool(replace), bool(enter), text.encode().hex())).encode())
    finally:
        os.close(fd)
    return "Sent to %s" % tf["title"]


SHOT_LOCK = threading.Lock()  # one capture at a time (own lock: an install must not block screenshots)


def screenshot(w, h):
    """PNG bytes of the TV's current screen (webOS capture service, DISPLAY = UI + video planes); RuntimeError on failure"""
    p = "/tmp/apkweb-shot-%d-%s.png" % (os.getpid(), os.urandom(6).hex())
    req = {"path": p, "method": "DISPLAY", "format": "PNG", "width": w, "height": h}
    with SHOT_LOCK:
        try:
            try:
                r = subprocess.run(["luna-send", "-n", "1", "luna://com.webos.service.capture/executeOneShot", json.dumps(req)],
                                   capture_output=True, text=True, timeout=10)
            except subprocess.TimeoutExpired:
                raise RuntimeError("capture timed out after 10 s")
            out = (r.stdout + r.stderr).strip()
            try:
                rep = json.loads(r.stdout[r.stdout.index("{"):])
            except ValueError:
                rep = {}
            if r.returncode != 0 or rep.get("returnValue") is False or re.search(r'"returnValue"\s*:\s*false', out):
                raise RuntimeError("capture failed (rc %d): %s" % (r.returncode, (rep.get("errorText") or out)[:300]))
            try:
                data = open(p, "rb").read()
            except OSError:
                data = b""
            if not data:
                raise RuntimeError("capture produced no image: %s" % out[:300])
            return data
        finally:
            if os.path.exists(p):
                os.remove(p)


PIN_PAGE = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>webOS ATL</title><style>body{font:16px system-ui,sans-serif;background:#14161c;color:#eee;margin:0;padding:20px;max-width:420px;margin:auto}
input{width:100%;box-sizing:border-box;padding:12px;border-radius:8px;border:1px solid #333a46;background:#14161c;color:#eee;font-size:22px;letter-spacing:6px;text-align:center}
button{background:#3ddc84;color:#0b3d1f;border:0;border-radius:10px;padding:12px;font-size:16px;font-weight:600;width:100%;margin-top:14px}
p{color:#9aa3b2}</style></head><body><h1>webOS ATL</h1>
<p>Enter the PIN shown on the TV in the webOS ATL app (Status page).</p>
<input id=p inputmode=numeric maxlength=6 autocomplete=off><button id=b>Continue</button><p id=m></p>
<script>b.onclick=()=>fetch('/pin',{method:'POST',body:JSON.stringify({pin:p.value.trim()})}).then(r=>r.text().then(t=>{if(r.ok)location.reload();else m.textContent=t}));
p.onkeydown=e=>{if(e.key=='Enter')b.click()};p.focus();</script></body></html>"""

PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=theme-color content="#14161c"><link rel=manifest href=/manifest.json><link rel=icon href=/icon.png>
<title>webOS ATL</title><style>
body{font:16px system-ui,sans-serif;background:#14161c;color:#eee;margin:0;padding:20px;max-width:560px;margin:auto}
h1{font-size:22px;margin:4px 0 18px}.card{background:#1f232c;border-radius:14px;padding:16px;margin-bottom:16px}
label{display:block;font-size:13px;color:#9aa3b2;margin:10px 0 4px}input{width:100%;box-sizing:border-box;padding:10px;
border-radius:8px;border:1px solid #333a46;background:#14161c;color:#eee;font-size:16px}
button{background:#3ddc84;color:#0b3d1f;border:0;border-radius:10px;padding:12px 16px;font-size:16px;font-weight:600;width:100%;margin-top:14px}
.row{display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid #2a2f3a}.row:last-child{border:0}
.row img{width:40px;height:40px;border-radius:10px}.row span{flex:1}.row button{width:auto;margin:0;padding:8px 12px;background:#3a2328;color:#ff8a8a}
#tf textarea{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #333a46;background:#14161c;
color:#eee;font-size:16px;min-height:70px}#tf .opt{display:flex;gap:18px;margin-top:8px;font-size:14px}#tf .opt label{display:inline;margin:0;color:#ccc}
#tf input[type=checkbox]{width:auto}#tfcur{font-size:13px;color:#9aa3b2;word-break:break-all;margin-top:6px}
#st{margin-top:12px;font-size:14px;color:#9aa3b2;white-space:pre-wrap}progress{width:100%;margin-top:10px}
#shot select{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #333a46;background:#14161c;color:#eee;font-size:16px}
#shot .bar{display:flex;gap:10px}#shot .bar button:disabled{opacity:.5}#shdl{background:#2a3446;color:#cfe0ff}
#shimg{display:block;width:100%;height:auto;border-radius:8px;margin-top:12px;background:#000;-webkit-touch-callout:default}#shimg[hidden]{display:none}
#shst{font-size:14px;color:#9aa3b2;margin-top:8px;white-space:pre-wrap}#shst.err{color:#ff8a8a}
</style></head><body><h1>webOS ATL: install Android apps</h1>
<div class=card id=tf><b>Type on TV</b><div id=tfnone style="font-size:14px;color:#9aa3b2;margin-top:6px">No text field focused on the TV.
Click a text field in an Android app and it shows up here.</div><div id=tfbox hidden><div id=tfapp style="font-size:13px;color:#3ddc84;margin-top:6px"></div>
<div id=tfcur></div><div style="font-size:12px;color:#9aa3b2;margin-top:4px">Text goes into the field that has the cursor on the TV: click the right field there first (a dialog focuses its first field).</div><label>Text</label><textarea id=tft placeholder="type or paste, e.g. a long URL"></textarea>
<div class=opt><label><input type=checkbox id=tfr checked> replace field text</label><label><input type=checkbox id=tfe> press Enter</label></div>
<button id=tfs>Send to TV</button><div id=tfst style="font-size:14px;color:#9aa3b2;margin-top:8px"></div></div></div>
<div class=card id=shot><b>TV screenshot</b><label>Size</label><select id=shsz><option value="1920x1080" selected>1080p (1920×1080)</option>
<option value="3840x2160">4K (3840×2160)</option><option value="960x540">540p (960×540)</option></select>
<div class=bar><button id=shb>Take screenshot</button><button id=shdl hidden>Download</button></div>
<div id=shst></div><img id=shimg hidden alt="TV screenshot">
<div style="font-size:12px;color:#9aa3b2;margin-top:8px">Long-press the picture to save it, or tap Download. Hardware-decoded or DRM video may show black (a webOS capture limit).</div></div>
<div class=card><label>APK file</label><input id=f type=file accept=".apk,application/vnd.android.package-archive">
<label>…or a direct download link to an .apk</label><input id=u type=url placeholder="https://…/app.apk">
<label>Name on TV</label><input id=t placeholder="the app's own name">
<label>Activity (optional, e.g. org/happysanta/gd/GDActivity)</label><input id=a>
<button id=go>Install on TV</button><progress id=p value=0 max=1 hidden></progress><div id=st></div></div>
<div class=card><b>Installed</b>__LIST__</div>
<script>
if('serviceWorker'in navigator)navigator.serviceWorker.register('/sw.js');
let tfon=false;function tfpoll(){fetch('/textfield').then(r=>r.json()).then(j=>{const on=!!j.app;
 tfnone.hidden=on;tfbox.hidden=!on;if(on){tfapp.textContent=j.title+(j.shown?' (TV keyboard open)':'');
 tfcur.textContent=j.password?'(password field)':j.text?'In field: '+j.text:'Field is empty'}
 if(on&&!tfon)tft.focus();tfon=on}).catch(()=>{}).finally(()=>setTimeout(tfpoll,1000))}tfpoll();
tfs.onclick=()=>{tfs.disabled=true;fetch('/type',{method:'POST',body:JSON.stringify({text:tft.value,replace:tfr.checked,enter:tfe.checked})})
 .then(r=>r.text()).then(m=>{tfst.textContent=m;if(/^Sent/.test(m))tft.value=''}).catch(()=>tfst.textContent='Failed').finally(()=>tfs.disabled=false)};
tft.onkeydown=e=>{if(e.key=='Enter'&&!e.shiftKey){e.preventDefault();tfs.click()}};
let shBlob=null,shUrl='',shName='tv.png',shQ='';function shMsg(m,err){shst.textContent=m;shst.className=err?'err':''}
shb.onclick=()=>{const [w,h]=shsz.value.split('x');shQ='w='+w+'&h='+h;shb.disabled=true;shb.textContent='Capturing…';shMsg('Capturing the TV screen…');
 fetch('/screenshot?'+shQ+'&t='+Date.now(),{cache:'no-store'}).then(r=>{if(!r.ok)return r.text().then(m=>{throw new Error(m||('HTTP '+r.status))});
  const m=/filename="([^"]+)"/.exec(r.headers.get('Content-Disposition')||'');shName=m?m[1]:'tv-'+Date.now()+'.png';return r.blob()})
 .then(b=>{if(shUrl)URL.revokeObjectURL(shUrl);shBlob=b;shUrl=URL.createObjectURL(b);shimg.src=shUrl;shimg.hidden=false;shdl.hidden=false;
  shMsg(shName+' ('+Math.round(b.size/1024)+' KB)')}).catch(e=>shMsg(/^Screenshot/.test(e.message)?e.message:'Screenshot failed: '+e.message,true))
 .finally(()=>{shb.disabled=false;shb.textContent='Take screenshot'})};
function shDownload(){const a=document.createElement('a');if(shUrl&&'download'in a){a.href=shUrl;a.download=shName;document.body.appendChild(a);a.click();a.remove()}
 else location.href='/screenshot?'+shQ+'&dl=1&t='+Date.now()}
shdl.onclick=()=>{let f=null;try{f=new File([shBlob],shName,{type:'image/png'})}catch(e){}
 if(f&&navigator.canShare&&navigator.canShare({files:[f]}))navigator.share({files:[f]}).catch(e=>{if(e.name!='AbortError')shDownload()});else shDownload()};
go.onclick=()=>{const file=f.files[0];if(!file&&u.value){go.disabled=true;p.hidden=true;st.textContent='Downloading and installing on TV…';
 fetch('/fetch',{method:'POST',body:JSON.stringify({url:u.value,title:t.value,activity:a.value})}).then(r=>r.text().then(m=>{st.textContent=m;if(r.ok)setTimeout(()=>location.reload(),1500)}))
 .catch(()=>st.textContent='Failed').finally(()=>go.disabled=false);return}
 if(!file){st.textContent='Pick an APK (or give a link) first';return}
 const x=new XMLHttpRequest();x.open('POST','/upload');x.setRequestHeader('X-Title',encodeURIComponent(t.value));
 x.setRequestHeader('X-Activity',encodeURIComponent(a.value));p.hidden=false;go.disabled=true;st.textContent='Uploading…';
 x.upload.onprogress=e=>{if(e.lengthComputable){p.value=e.loaded/e.total;if(e.loaded==e.total)st.textContent='Installing on TV…'}};
 x.onload=()=>{go.disabled=false;st.textContent=x.responseText;if(x.status==200)setTimeout(()=>location.reload(),1500)};
 x.onerror=()=>{go.disabled=false;st.textContent='Upload failed'};x.send(file)};
document.querySelectorAll('[data-rm]').forEach(b=>b.onclick=()=>{if(!confirm('Remove '+b.dataset.t+'?'))return;
 b.disabled=true;fetch('/remove?id='+b.dataset.rm,{method:'POST'}).then(r=>r.text()).then(m=>{st.textContent=m;location.reload()})});
</script></body></html>"""

MANIFEST = {"name": "webOS ATL", "short_name": "webOS ATL", "start_url": "/", "display": "standalone",
            "background_color": "#14161c", "theme_color": "#14161c",
            "icons": [{"src": "/icon.png", "sizes": "192x192", "type": "image/png"}]}
SW = "self.addEventListener('fetch',e=>{});"


class H(BaseHTTPRequestHandler):
    def authed(self):
        c = self.headers.get("Cookie", "")
        m = re.search(r"(?:^|;\s*)atlpin=(\d{6})", c)
        return bool(m) and hmac.compare_digest(m.group(1), pin())

    def gate(self, path):
        """True if the request may go on; otherwise the PIN page (for /) or 403 was sent"""
        if path in PUBLIC or self.authed():
            return True
        if path == "/" and self.command == "GET":
            self.send(200, PIN_PAGE, "text/html; charset=utf-8")
        else:
            self.send(403, "PIN needed: open http://<tv-ip>:%d/ and enter the PIN from the webOS ATL app" % PORT)
        return False

    def send(self, code, body, ctype="text/plain; charset=utf-8", headers=()):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?")[0]
        if not self.gate(path):
            return
        if path == "/":
            rows = "".join('<div class=row><img src="/appicon?id=%s"><span>%s <a href="/compat?id=%s" style="font-size:12px;color:#9aa3b2">compat</a></span><button data-rm="%s" data-t="%s">Remove</button></div>'
                           % (i, html.escape(t), i, i, html.escape(t)) for i, t in installed()) or "<div class=row>None yet</div>"
            self.send(200, PAGE.replace("__LIST__", rows), "text/html; charset=utf-8")
        elif path == "/textfield":
            tf = textfield() or {}
            tf.pop("mtime", None)
            self.send(200, json.dumps(tf), "application/json")
        elif path == "/screenshot":
            from urllib.parse import parse_qs
            q = parse_qs(self.path.partition("?")[2])
            try:
                w = min(max(int(q.get("w", ["1920"])[0]), 160), 3840)
                h = min(max(int(q.get("h", ["1080"])[0]), 90), 2160)
            except ValueError:
                return self.send(400, "w and h must be integers")
            try:
                png = screenshot(w, h)
            except Exception as e:
                log("screenshot failed", repr(e))
                return self.send(500, "Screenshot failed: %s" % e)
            name = time.strftime("tv-%Y%m%d-%H%M%S.png")
            disp = "attachment" if q.get("dl", [""])[0] == "1" else "inline"
            self.send(200, png, "image/png", [("Content-Disposition", '%s; filename="%s"' % (disp, name))])
        elif path == "/manifest.json":
            self.send(200, json.dumps(MANIFEST), "application/manifest+json")
        elif path == "/sw.js":
            self.send(200, SW, "text/javascript")
        elif path == "/icon.png":
            self.send(200, open(f"{BIN}/default-icon.png", "rb").read(), "image/png")
        elif path == "/compat":
            i = self.path.split("id=")[-1]
            f = f"{APKS}/{i[len(PREFIX):]}.compat.txt"
            if i.startswith(PREFIX) and "/" not in i and os.path.exists(f):
                self.send(200, open(f).read())
            else:
                self.send(404, "no report yet (it is written a few seconds after install)")
        elif path == "/appicon":
            i = self.path.split("id=")[-1]
            p = f"{APPS}/{i}/icon.png"
            if i.startswith(PREFIX) and "/" not in i and os.path.exists(p):
                self.send(200, open(p, "rb").read(), "image/png")
            else:
                self.send(404, "no icon")
        else:
            self.send(404, "not found")

    def do_POST(self):
        from urllib.parse import unquote
        path = self.path.split("?")[0]
        if not self.gate(path):
            return
        try:
            if path == "/pin":
                now = time.time()
                _fails[:] = [t for t in _fails if now - t < 60]
                if len(_fails) >= 10:
                    return self.send(429, "Too many wrong PINs, wait a minute")
                n = int(self.headers.get("Content-Length", "0"))
                got = str(json.loads(self.rfile.read(min(n, 1024)) or b"{}").get("pin", "")) if n > 0 else ""
                if not re.fullmatch(r"\d{6}", got) or not hmac.compare_digest(got, pin()):
                    _fails.append(now)
                    log("wrong PIN from", self.client_address[0])
                    return self.send(403, "Wrong PIN")
                return self.send(200, "ok", headers=[("Set-Cookie", "atlpin=%s; Max-Age=31536000; Path=/; "
                                                                     "SameSite=Strict; HttpOnly" % got)])
            if path == "/upload":
                n = int(self.headers.get("Content-Length", "0"))
                if n <= 0 or n > 400 * 1024 * 1024:
                    return self.send(400, "APK missing or too big")
                free = shutil.disk_usage(BASE).free
                if n * 2 + (150 << 20) > free:  # keep 150 MB free
                    return self.send(507, "Not enough space on TV (%d MB free)" % (free >> 20))
                title = unquote(self.headers.get("X-Title", "")).strip()[:60]  # empty: the app's own label
                act = unquote(self.headers.get("X-Activity", "")).strip()
                if act and not re.fullmatch(r"[A-Za-z0-9_$/.]+", act):
                    return self.send(400, "bad activity")
                os.makedirs(APKS, exist_ok=True)
                tmp = f"{APKS}/.upload-{os.getpid()}-{threading.get_ident()}.apk"
                with open(tmp, "wb") as f:
                    left = n
                    while left:
                        chunk = self.rfile.read(min(1 << 20, left))
                        if not chunk:
                            break
                        f.write(chunk)
                        left -= len(chunk)
                with LOCK:
                    try:
                        appid = build_and_install(tmp, title, act)
                    finally:
                        if os.path.exists(tmp):
                            os.remove(tmp)
                self.send(200, "Installed %s. It is in the TV's app list now." % appid)
            elif path == "/fetch":
                n = int(self.headers.get("Content-Length", "0"))
                if n <= 0 or n > 64 * 1024:
                    return self.send(400, "bad request")
                d = json.loads(self.rfile.read(n))
                act = str(d.get("activity") or "").strip()
                if act and not re.fullmatch(r"[A-Za-z0-9_$/.]+", act):
                    return self.send(400, "bad activity")
                with LOCK:
                    appid = install_url(str(d.get("url", "")).strip(), str(d.get("title") or "").strip()[:60], act)
                self.send(200, "Installed %s. It is in the TV's app list now." % appid)
            elif path == "/type":
                n = int(self.headers.get("Content-Length", "0"))
                if n <= 0 or n > 64 * 1024:
                    return self.send(400, "text missing or too long")
                d = json.loads(self.rfile.read(n))
                self.send(200, type_text(str(d.get("text", "")), d.get("replace"), d.get("enter")))
            elif path == "/remove":
                appid = self.path.split("id=")[-1]
                with LOCK:
                    remove(appid)
                self.send(200, "Removed " + appid)
            else:
                self.send(404, "not found")
        except Exception as e:
            log("error", path, repr(e))
            self.send(500, "Failed: %s" % e)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    pin()
    log("== apkweb on port", PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
