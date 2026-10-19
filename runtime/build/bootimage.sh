#!/bin/sh
# bootimage.sh <AppDir-new> <out.sqfs>: ship a prebuilt "speed" ART boot image inside the ATL image, then mksquashfs.
# Without it the TV generates a verify-only (interpreted) boot image per app on first launch (cold CloudStream
# 17.9 -> 7.2 s on the bench). The image records its jar locations, so it is generated with <AppDir-new>
# bind-mounted read-only at the TV path where init.sh mounts the runtime (ATL_MOUNT, default
# /media/developer/webosatl/runtime). ART regenerates a verify-only one if it rejects it.
# Run between `cp -a AppDir AppDir-new` and shipping; ship it together with tv/dex2oat-verify.
set -e
[ $# = 2 ] || { echo "usage: $0 <AppDir-new> <out.sqfs>" >&2; exit 2; }
A=$(cd "$1" && pwd)
OUT=$2
M=${ATL_MOUNT:-/media/developer/webosatl/runtime}
C=$HOME/atl2/smoke-home/$(basename "${DEMO_APK:-demo_app.apk}" .apk)/.cache/art
O=$A/lib/java/dex/art/oat/arm64
sudo mkdir -p $M
if mountpoint -q $M; then
	echo "bootimage: $M is already mounted" >&2
	exit 1
fi
mounted=
cleanup() { [ -z "$mounted" ] || sudo umount $M; }
trap cleanup EXIT
trap 'exit 1' INT TERM HUP
sudo mount --bind "$A" $M
mounted=1
sudo mount -o remount,bind,ro $M
rm -rf "$C"
# no -Xcompiler: ART builds the boot image with its default (speed) filter
# any small APK works (DEMO_APK); smoke.py keeps its HOME (and so the boot image) in ~/atl2/smoke-home/<apk name>
python3 "${SMOKE:-$(dirname "$0")/../smoke/smoke.py}" --atl $M --apks "${DEMO_APK:-$HOME/atl2/apks/demo_app.apk}" --out /tmp/bootimg-gen --secs 90
sudo umount $M
mounted=
mkdir -p "$O"
for f in "$C"/arm64/*@art@oat@boot*; do
	if [ -f "$f" ]; then cp "$f" "$O/${f##*@}"; fi
done
n=$(ls "$O" | grep -c '\.oat$' || true)
if [ "$n" -lt 3 ]; then
	echo "bootimage: only $n boot .oat files in $C/arm64" >&2
	exit 1
fi
rm -rf "$C"
echo "bootimage: $n boot oat files, $(du -sh "$O" | cut -f1) in $O"
mksquashfs "$A" "$OUT" -comp gzip -noappend -all-root -quiet
echo "bootimage: wrote $OUT"
