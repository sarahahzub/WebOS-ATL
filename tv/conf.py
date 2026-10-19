"""webOS ATL: paths and ids shared by the TV-side programs (atld, atlmgr, apkweb). Shell scripts use env.sh."""
import json, os

BASE = os.environ.get("WEBOSATL_BASE", "/media/developer/webosatl")  # everything webOS ATL owns (env: tests)
BIN = BASE + "/bin"                          # programs (copied from the manager app's payload by install.sh)
RUNTIME_IMG = BASE + "/runtime.sqfs"         # ATL runtime image (downloaded)
RUNTIME = BASE + "/runtime"                  # where it is mounted (read-only); the boot image records this path
HOME = BASE + "/home"                        # Android apps' HOME (data under home/data)
APKS = BASE + "/apks"
LOGS = BASE + "/logs"
SETTINGS_F = BASE + "/settings.json"
APPS = os.environ.get("WEBOSATL_APPS", "/media/developer/apps/usr/palm/applications")
PREFIX = "org.webosatl.app."                 # webOS id of an installed Android app: PREFIX + slug
MANAGER_ID = "org.webosatl.manager"
MANAGER_DIR = APPS + "/" + MANAGER_ID
PAYLOAD = MANAGER_DIR + "/payload"
MGR_PORT = 8791
APKWEB_PORT = 8790


def version():
    try:
        return json.load(open(BIN + "/release.json")).get("version", "?")
    except (OSError, ValueError):
        return "?"


def ensure_dirs():
    for d in (BASE, BIN, RUNTIME, HOME, HOME + "/data", APKS, LOGS):
        os.makedirs(d, exist_ok=True)
