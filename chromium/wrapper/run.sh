#!/bin/sh
# bin/run of org.webosatl.chromium (exec'd by SAM, jailed). Same pattern as the Android app wrappers (tv/run.sh):
# owns the window via `xdgshim -1` (LSM only shows a launched app's window if it comes from this process), asks
# browserd (root) to start Chromium on that socket, exits when the browser disconnects, stops it when SAM closes the app.
D=$(cd "$(dirname "$0")/.." && pwd)
ID=$(basename "$D")
S=/tmp/xdg/wayland-$(echo "$ID" | tr . -)
read -r up _ < /proc/uptime; echo "$up run start $ID" >> "$D/run/launch.log" 2>/dev/null
why() { read -r up _ < /proc/uptime; echo "$up $1 (ppid $PPID)" >> "$D/run/exit.log"; }
# write one line to browserd (it holds run/cmd open read-write); opening read-write never blocks, so the app never
# hangs and leaves no blocked writer behind if browserd is not running (then the 90 s watchdog below ends the app)
send() { [ -p "$D/run/cmd" ] && echo "$1" 1<> "$D/run/cmd"; }
mv -f "$D/run/shim.log" "$D/run/shim.log.prev" 2>/dev/null
rm -f "$D/run/ti.state"
# SUBSURF=0: Chromium bubbles drawn as wl_subsurfaces (fullscreen exit hint, ...) stay unmapped: LSM showed them as
# appId-less cards that minimized the browser (drop to Home). CH+/CH- scroll 2 wheel notches per press (Chromium: 2 x 120 px); BACK reaches the app (brwl turns it into browser Back / exit)
env XDG_RUNTIME_DIR=/tmp/xdg XDGSHIM_SUBSURF=0 XDGSHIM_RELAY="$D/run/cmd" XDGSHIM_APPID=$ID XDGSHIM_SCROLL=2 "$D/bin/xdgshim" -1 "$S" 2> "$D/run/shim.log" &
SHIM=$!
trap 'why TERM; send stop; kill $SHIM $WATCH 2>/dev/null; exit 0' TERM
trap 'why INT; send stop; kill $SHIM $WATCH 2>/dev/null; exit 0' INT
trap 'why HUP; send stop; kill $SHIM $WATCH 2>/dev/null; exit 0' HUP
i=0; while [ ! -S "$S" ] && [ $i -lt 100 ]; do sleep 0.02; i=$((i+1)); done
send "start $S" || { why "no $D/run/cmd (browserd never ran)"; kill $SHIM; exit 1; }
# xdgshim writes run/ti.state when the main client (Chromium via brwl) connects; no browser after 90 s: give up
( i=0; while [ ! -f "$D/run/ti.state" ] && [ $i -lt 900 ]; do sleep 0.1; i=$((i+1)); done
  [ -f "$D/run/ti.state" ] || { why "no browser connected in 90 s"; kill $SHIM; } ) &
WATCH=$!
wait $SHIM
why "shim exited rc=$?"
kill $WATCH 2>/dev/null
send stop
