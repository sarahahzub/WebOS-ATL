#!/bin/sh
# bin/run of an ATL wrapper app (exec'd by SAM, jailed). Owns the window via `xdgshim -1`, so LSM
# sees it coming from the launched app's own process; asks atld (root) to start the APK on that
# socket; exits when the Android app disconnects; stops the APK when SAM closes the app.
D=$(cd "$(dirname "$0")/.." && pwd)
ID=$(basename "$D")
# launch timing (needs TV verification): same clock as atld's log (/proc/uptime)
echo "$(cut -d' ' -f1 /proc/uptime) run start $ID $(awk '/MemAvailable/{a=$2}/SwapFree/{s=$2}END{printf "avail=%dMB swapfree=%dMB", a/1024, s/1024}' /proc/meminfo 2>/dev/null)" >> "$D/run/launch.log" 2>/dev/null
S=/tmp/xdg/wayland-$(echo "$ID" | tr . -)
send() { [ -p "$D/run/cmd" ] && echo "$1" > "$D/run/cmd"; }
# optional per-app settings from apk.conf: DIALOG_TYPE=<webOS window type for dialogs/sheets>, SHIM_DEBUG=1
DT=$(sed -n 's/^DIALOG_TYPE=//p' "$D/apk.conf" 2>/dev/null | head -1)
SD=$(sed -n 's/^SHIM_DEBUG=//p' "$D/apk.conf" 2>/dev/null | head -1)
HV=$(sed -n 's/^HOVER=//p' "$D/apk.conf" 2>/dev/null | head -1)
# DPAD=keys: remote arrows are sent as key events (Android TV / leanback apps) instead of moving the pointer
# SOFTCURSOR=0: no drawn pointer
DP=$(sed -n 's/^DPAD=//p' "$D/apk.conf" 2>/dev/null | head -1)
SC=$(sed -n 's/^SOFTCURSOR=//p' "$D/apk.conf" 2>/dev/null | head -1)
# SHIM_ENV="XDGSHIM_NOOPAQUE=1 ...": extra xdgshim env
SE=$(sed -n 's/^SHIM_ENV=//p' "$D/apk.conf" 2>/dev/null | head -1)
mv -f "$D/run/shim.log" "$D/run/shim.log.prev" 2>/dev/null
# hardware video decode (apk.conf ENV_ATL_HWDEC=1 or settings.json env ATL_HWDEC=1): the 32-bit atlvdec helper
# (libNDL_directmedia) runs in this app's process tree; atld points ATL at its socket (ATL_HWDEC_SOCK)
HW=$(sed -n 's/^ENV_ATL_HWDEC=//p' "$D/apk.conf" 2>/dev/null | head -1)
[ -z "$HW" ] && grep -q '"ATL_HWDEC": *"1"' /media/developer/webosatl/settings.json 2>/dev/null && HW=1
VD=/media/developer/webosatl/bin/atlvdec  # fixed path: its luna-service2 role (ls2-role.sh) names this exe
VE=$(sed -n 's/^VDEC_ENV=//p' "$D/apk.conf" 2>/dev/null | head -1)  # optional extra helper env (ATLVDEC_*)
VDEC=
# VIDEO_EXPORT=1 (apk.conf): xdgshim exports the app window as the webOS video element, atlvdec binds NDL to it
# instead of its own SDL window (LSM does not show a second window of the app)
VX=$(sed -n 's/^VIDEO_EXPORT=//p' "$D/apk.conf" 2>/dev/null | head -1)
# hwdec on (per app or global): the NewPipe-proven setup is the default - window export + no opaque region (ATL only
# punches the transparent hole while the hardware decoder is really active; FFmpeg fallback draws normally).
# Per-app opt-out: apk.conf ENV_ATL_HWDEC=0; keep hwdec but not the window binding: VIDEO_EXPORT=0
if [ "$HW" = 1 ]; then
	[ -z "$VX" ] && VX=1
	case " $SE " in *XDGSHIM_NOOPAQUE=*) ;; *) SE="$SE XDGSHIM_NOOPAQUE=1" ;; esac
fi
WF=/tmp/xdg/atlvdec-$(echo "$ID" | tr . -).winid
if [ "$HW" = 1 ] && [ "$VX" = 1 ]; then
	rm -f "$WF"; SE="$SE XDGSHIM_EXPORT=$WF"; VE="ATLVDEC_SDL=${VXSDL:-1} ATLVDEC_WINID_FILE=$WF $VE"
fi
if [ "$HW" = 1 ] && [ -x "$VD" ]; then
	mv -f "$D/run/vdec.log" "$D/run/vdec.log.prev" 2>/dev/null
	# its SDL window is a second webOS surface of this appId: without KEYS_BACK on it too, LSM shows its own
	# "Do you want to exit the app?" on BACK and the key never reaches the app (needs ATL with tvpatches/back-dispatch.py:
	# older ATL aborts on the first BACK that reaches a Compose view)
	env APPID=$ID SDL_WEBOS_ACCESS_POLICY_KEYS_BACK=true $VE "$VD" "/tmp/xdg/atlvdec-$(echo "$ID" | tr . -)" 2> "$D/run/vdec.log" &
	VDEC=$!
fi
env XDG_RUNTIME_DIR=/tmp/xdg XDGSHIM_RELAY="$D/run/cmd" XDGSHIM_APPID=$ID ${DT:+XDGSHIM_DIALOG_TYPE=$DT} ${SD:+XDGSHIM_DEBUG=1} ${HV:+XDGSHIM_HOVER=$HV} ${DP:+XDGSHIM_DPAD=$DP} ${SC:+XDGSHIM_SOFTCURSOR=$SC} $SE "$D/bin/xdgshim" -1 "$S" 2> "$D/run/shim.log" &
SHIM=$!
why() { echo "$(cut -d' ' -f1 /proc/uptime) $1 (ppid $PPID)" >> "$D/run/exit.log"; }
trap 'why TERM; send stop; kill $SHIM $VDEC 2>/dev/null; exit 0' TERM
trap 'why INT; send stop; kill $SHIM $VDEC 2>/dev/null; exit 0' INT
trap 'why HUP; send stop; kill $SHIM $VDEC 2>/dev/null; exit 0' HUP
i=0; while [ ! -S "$S" ] && [ $i -lt 100 ]; do sleep 0.02; i=$((i+1)); done
send "start $S"
wait $SHIM
why "shim exited rc=$?"
[ -n "$VDEC" ] && kill $VDEC 2>/dev/null
send stop
