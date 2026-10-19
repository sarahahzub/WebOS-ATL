#!/bin/sh
# browserd.sh: root helper of the Chromium app org.webosatl.chromium (what atld.py is for the Android apps).
# Started by webOS ATL's init.sh at boot and by the manager (atlmgr) after the add-on is installed; busybox sh, no python, so it
# costs ~0.5 MB while idle. The jailed wrapper (bin/run) owns the window through `xdgshim -1 <sock>` and writes
# "start <sock>" / "stop" lines into <app>/run/cmd. On start, browserd mounts browser.sqfs (loop, ro) at mnt/ if needed
# and runs mnt/ghb/launch.sh <sock> in its own session (Chromium as root, oom_score_adj 1000); stop ends that session
# (SIGTERM to Chromium, SIGKILL to what is left after 4 s).
# Env (bench only): GHB_WRAP=<command prefix for launch.sh>, e.g. a systemd-run memory scope.
B=${WEBOSATL_BASE:-/media/developer/webosatl}/chromium   # (env overrides: tests only; launch.sh paths are fixed)
APP=${WEBOSATL_APPS:-/media/developer/apps/usr/palm/applications}/org.webosatl.chromium
FIFO=$APP/run/cmd
LOG=$B/browserd.log
log() { read -r up _ < /proc/uptime; echo "$up $*" >> "$LOG"; }

# one instance only (pid file, never by process name)
if [ -f "$B/browserd.pid" ]; then
	read -r old < "$B/browserd.pid"
	if [ -n "$old" ] && [ "$old" != $$ ] && grep -q browserd "/proc/$old/cmdline" 2>/dev/null; then exit 0; fi
fi
echo $$ > "$B/browserd.pid"

mounted() { grep -q " $B/mnt squashfs " /proc/mounts; }
ensure_mount() {
	mounted && return 0
	mkdir -p "$B/mnt"
	mount -t squashfs -o loop,ro "$B/browser.sqfs" "$B/mnt" >> "$LOG" 2>&1
}

# pids of every process in session $1
members() {
	for f in /proc/[0-9]*/stat; do
		{ read -r l < "$f"; } 2>/dev/null || continue
		l=${l##*) }
		set -- $l
		[ "$4" = "$sid" ] && { p=${f#/proc/}; echo "${p%/stat}"; }
	done
}

stop_browser() {
	[ -f "$B/chromium.pid" ] || return 0
	read -r sid < "$B/chromium.pid"
	rm -f "$B/chromium.pid"
	[ -n "$sid" ] && [ -d "/proc/$sid" ] || return 0
	kill -TERM "$sid" 2>/dev/null           # Chromium's browser process: saves its profile and ends its children
	i=0; while [ -d "/proc/$sid" ] && [ $i -lt 40 ]; do sleep 0.1; i=$((i+1)); done
	left=$(members)
	[ -n "$left" ] && kill -KILL $left 2>/dev/null
	log "stopped session $sid${left:+ (killed $left)}"
}

start_browser() {
	stop_browser
	ensure_mount || { log "mounting $B/browser.sqfs failed"; return; }
	mkdir -p "$B/logs"
	mv -f "$B/logs/chromium.log" "$B/logs/chromium.log.prev" 2>/dev/null
	# optional Google API keys (sign-in / sync): GOOGLE_API_KEY, GOOGLE_DEFAULT_CLIENT_ID, GOOGLE_DEFAULT_CLIENT_SECRET
	# lines in $B/google-keys.env (root-only file, never in the repo or the package), exported to Chromium only
	( [ -f "$B/google-keys.env" ] && { set -a; . "$B/google-keys.env"; set +a; }
	  exec setsid $GHB_WRAP sh "$B/mnt/ghb/launch.sh" "$1" ) > "$B/logs/chromium.log" 2>&1 < /dev/null &
	log "started launch.sh pid $! on $1"
}

mkdir -p "$APP/run"
chmod 777 "$APP/run"
[ -p "$FIFO" ] || { rm -f "$FIFO"; mkfifo "$FIFO"; }
chmod 666 "$FIFO"
log "== browserd start (pid $$)"
trap 'stop_browser; log "browserd exit"; rm -f "$B/browserd.pid"; exit 0' TERM INT
# the FIFO is held open read-write: never EOF between writers, and a writer never blocks or loses a line
exec 3<> "$FIFO"
while :; do
	if read -r c a <&3; then
		case "$c" in
		start) log "cmd start $a"; case "$a" in /tmp/xdg/wayland-*) start_browser "$a" ;; esac ;;
		stop) log "cmd stop"; stop_browser ;;
		esac
	fi
	[ -p "$FIFO" ] || { log "fifo $FIFO gone, exiting"; rm -f "$B/browserd.pid"; exit 0; }
done
