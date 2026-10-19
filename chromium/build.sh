#!/bin/sh
# build.sh [out-dir]: build the Chromium add-on image (browser.sqfs) on an aarch64 Linux host with docker.
#   sh chromium/build.sh   ->  <out-dir>/browser.sqfs (default ~/browser/build-webosatl), plus ghb/ and bundle.json
# Inputs:
#   ~/browser/deb-root           Debian trixie armhf rootfs with the chromium package installed (debootstrap --arch=armhf
#                                trixie + apt-get install chromium; see docs/BUILDING.md)
#   patchelf                     PATCHELF=<path> or ~/browser/venv/bin/patchelf (python3 -m venv ...; pip install patchelf)
#   ../native/xdgshim/xdgshim.c  compiled like the Android app wrappers' copy
# The wrapper app IPK is built by tools/mkipk.py chromium.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
W=${1:-$HOME/browser/build-webosatl}
mkdir -p "$W"; W=$(cd "$W" && pwd)
ROOTFS=${ROOTFS:-$HOME/browser/deb-root}
rm -rf "$W/ghb" "$W/root"; mkdir -p "$W/ghb"
cp "$HERE/wrapper/launch.sh" "$HERE/wrapper/Preferences" "$W/ghb/"
cp -r "$HERE/wrapper/start" "$W/ghb/start"
echo "${VER:-dev}" > "$W/ghb/VERSION"
echo "== compile (armhf, static like xdgshim)"
docker run --rm -v "$W/ghb:/o" -v "$HERE/wrapper:/s:ro" -v "$HERE/../native/xdgshim:/x:ro" debian:bookworm-slim sh -c "set -e
	apt-get update -qq >/dev/null 2>&1; apt-get install -y -qq gcc-arm-linux-gnueabihf libc6-dev-armhf-cross >/dev/null 2>&1
	arm-linux-gnueabihf-gcc -O2 -Wall -static -o /o/brwl /s/brwl.c
	arm-linux-gnueabihf-gcc -O2 -Wall -shared -fPIC -fvisibility=hidden -o /o/gbfix.so /s/gbfix.c -ldl
	arm-linux-gnueabihf-gcc -O2 -Wall -static -o /o/xdgshim /x/xdgshim.c -lm
	arm-linux-gnueabihf-strip /o/brwl /o/xdgshim /o/gbfix.so
	chown -R $(id -u):$(id -g) /o"
echo "== bundle"
GHB_FILES=$W/ghb python3 "$HERE/bundle.py" "$ROOTFS" "$W/root"
echo "== squashfs"
rm -f "$W/browser.sqfs"
nice -n 19 mksquashfs "$W/root" "$W/browser.sqfs" -comp gzip -b 262144 -all-root -noappend -no-xattrs -quiet
ls -l "$W/browser.sqfs" "$W/ghb/xdgshim"
