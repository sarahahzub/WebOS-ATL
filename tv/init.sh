#!/bin/sh
# init.sh [start|stop|restart|mount|umount|stop-apps|status]: webOS ATL service control (root).
# Boot: Homebrew Channel runs /var/lib/webosbrew/init.d/60-webosatl -> this script (no argument = start).
# start: mount the runtime image, start atld (runs the Android apps), apkweb (phone install page, :8790) and
# atlmgr (manager agent, :8791). Touch /media/developer/webosatl/disable to keep everything off at boot.
. "${WEBOSATL_BASE:-/media/developer/webosatl}/bin/env.sh"
mkdir -p "$LOGS" /tmp/xdg
pids() { # python3 processes running $BIN/<script>
	for p in /proc/[0-9]*; do
		c=$(tr '\0' ' ' 2>/dev/null < "$p/cmdline") || continue
		case "$c" in *python3*" $BIN/$1"*) echo "${p#/proc/}" ;; esac
	done
}
run() { [ -n "$(pids "$1")" ] || setsid python3 "$BIN/$1" >> "$LOGS/${1%.py}.out" 2>&1 < /dev/null & }
mounted() { grep -q " $RUNTIME " /proc/mounts; }
do_mount() {
	[ -f "$RUNTIME_IMG" ] || { echo "no runtime image yet ($RUNTIME_IMG)"; return 1; }
	mounted && return 0
	mkdir -p "$RUNTIME"
	mount -t squashfs -o loop,ro "$RUNTIME_IMG" "$RUNTIME" >> "$LOGS/init.log" 2>&1 || { echo "mounting $RUNTIME_IMG failed" | tee -a "$LOGS/init.log"; return 1; }
}
stop_apps() { # close every Android app (wrappers through SAM, then the ATL processes)
	for a in "$APPS/$PREFIX"*; do
		[ -d "$a" ] && luna-send -n 1 luna://com.webos.applicationManager/closeByAppId "{\"id\":\"$(basename "$a")\"}" >/dev/null 2>&1 < /dev/null
	done
	sleep 1
	for p in /proc/[0-9]*; do
		c=$(tr '\0' ' ' 2>/dev/null < "$p/cmdline") || continue
		case "$c" in "$RUNTIME/"*) kill "${p#/proc/}" 2>/dev/null ;; esac
	done
}
stop_svc() { for s in "$@"; do for p in $(pids "$s"); do kill "$p" 2>/dev/null; done; done; }
case "${1:-start}" in
start)
	[ -f "$BASE/disable" ] && [ -z "$2" ] && exit 0
	echo "$(date '+%F %T') start" >> "$LOGS/init.log"
	# in the background: the boot hook returns at once and never holds up the TV's start-up
	(
		do_mount
		run atld.py; run apkweb.py; run atlmgr.py
		# Chromium add-on (installed by atlmgr): its root helper, busybox sh (one instance, by its pid file)
		[ -f "$BASE/chromium/browserd.sh" ] && { setsid sh "$BASE/chromium/browserd.sh" >> "$BASE/chromium/browserd.log" 2>&1 < /dev/null & }
	) < /dev/null >> "$LOGS/init.log" 2>&1 & ;;
stop)
	stop_apps; stop_svc atld.py apkweb.py atlmgr.py
	[ -f "$BASE/chromium/browserd.pid" ] && kill "$(cat "$BASE/chromium/browserd.pid")" 2>/dev/null ;;
restart) # restart [service.py ...]; default atld + apkweb (atlmgr restarts itself)
	shift; [ $# = 0 ] && set -- atld.py apkweb.py
	stop_svc "$@"; sleep 1; for s in "$@"; do run "$s"; done ;;
mount) do_mount ;;
umount)
	stop_apps
	for i in 1 2 3; do mounted || break; umount "$RUNTIME" 2>/dev/null || umount -l "$RUNTIME"; sleep 1; done
	! mounted ;;
stop-apps) stop_apps ;;
status)
	mounted && echo "runtime: mounted" || echo "runtime: not mounted"
	for s in atld.py apkweb.py atlmgr.py; do echo "$s: $(pids $s)"; done ;;
*) echo "usage: init.sh start|stop|restart [svc.py..]|mount|umount|stop-apps|status"; exit 2 ;;
esac
exit 0
