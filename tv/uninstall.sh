#!/bin/sh
# uninstall.sh [--keep-data]: remove webOS ATL from the TV (root): every installed Android app, the Chromium add-on,
# the runtime, the boot hook and /media/developer/webosatl. --keep-data keeps the APKs and app data (apks/, home/).
# The manager app itself stays; remove it in Homebrew Channel.
P=$(cd "$(dirname "$0")" && pwd)
. "$P/env.sh"
[ "$(id -u)" = 0 ] || { echo "ERROR needs root"; exit 1; }
# never delete anything outside our own paths, whatever env.sh says
case "$BASE" in /*/webosatl) ;; *) echo "ERROR refusing: BASE='$BASE'"; exit 1 ;; esac
case "$PREFIX" in org.webosatl.app.) ;; *) echo "ERROR refusing: PREFIX='$PREFIX'"; exit 1 ;; esac
case "$APPS" in /*/usr/palm/applications) ;; *) echo "ERROR refusing: APPS='$APPS'"; exit 1 ;; esac
case "$HOOK" in */init.d/60-webosatl) ;; *) echo "ERROR refusing: HOOK='$HOOK'"; exit 1 ;; esac
echo "stopping"
[ -f "$BIN/init.sh" ] && sh "$BIN/init.sh" stop
rm -f "$HOOK"
for a in "$APPS/$PREFIX"* "$APPS/org.webosatl.chromium"; do
	[ -d "$a" ] || continue
	id=$(basename "$a"); echo "removing $id"
	luna-send -n 1 luna://com.webos.applicationManager/closeByAppId "{\"id\":\"$id\"}" >/dev/null 2>&1 < /dev/null
	luna-send -n 1 luna://com.webos.appInstallService/dev/remove "{\"id\":\"$id\"}" >/dev/null 2>&1 < /dev/null
done
sleep 3
rm -rf "$APPS/$PREFIX"* "$APPS/org.webosatl.chromium"
[ -f "$BIN/ls2-role.sh" ] && sh "$BIN/ls2-role.sh" remove
[ -f "$BASE/chromium/browserd.pid" ] && kill "$(cat "$BASE/chromium/browserd.pid")" 2>/dev/null
for m in "$RUNTIME" "$BASE/chromium/mnt"; do
	grep -q " $m " /proc/mounts && { umount "$m" 2>/dev/null || umount -l "$m"; }
done
if grep -q " $BASE/" /proc/mounts; then  # something still mounted inside: never rm -rf through a mount
	echo "ERROR still mounted under $BASE:"; grep " $BASE/" /proc/mounts; exit 1
fi
if [ "$1" = --keep-data ]; then
	find "$BASE" -mindepth 1 -maxdepth 1 ! -name apks ! -name home -exec rm -rf {} +
else
	rm -rf "$BASE"
fi
echo "OK removed"
