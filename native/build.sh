#!/bin/sh
# build.sh [out-dir]: build the TV-side native helpers (run on an aarch64 Linux host with docker, e.g. an Oracle
# Ampere VM). Output (default native/out):
#   xdgshim    32-bit ARM, static        Wayland shim between the Android app and webOS LSM (window, remote keys, pointer)
#   atlvdec    32-bit ARM soft-float     hardware video decode helper (LG libNDL_directmedia), dynamic glibc <= 2.34
#   atlfix.so  aarch64, LD_PRELOAD       libpulse protocol fix, line-buffered stdout, crash backtraces
#   low4g.so   aarch64, LD_PRELOAD       ART low-4GB allocator fix (startup hang on 39-bit VA kernels)
set -e
S=$(cd "$(dirname "$0")" && pwd)
O=$(mkdir -p "${1:-$S/out}" && cd "${1:-$S/out}" && pwd)
[ "$(uname -m)" = aarch64 ] || { echo "build.sh: run on aarch64 (atlfix.so and low4g.so are built natively)" >&2; exit 1; }
docker run --rm -v "$S":/s:ro -v "$O":/o debian:bookworm-slim sh -c '
set -e
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq gcc-arm-linux-gnueabihf libc6-dev-armhf-cross gcc-arm-linux-gnueabi libc6-dev-armel-cross >/dev/null 2>&1
arm-linux-gnueabihf-gcc -O2 -Wall -static -o /o/xdgshim /s/xdgshim/xdgshim.c -lm
arm-linux-gnueabihf-strip /o/xdgshim
arm-linux-gnueabi-gcc -O2 -Wall -static-libgcc -rdynamic -o /o/atlvdec /s/atlvdec/atlvdec.c -ldl
arm-linux-gnueabi-strip /o/atlvdec
echo "glibc symbols needed by atlvdec:"; arm-linux-gnueabi-objdump -T /o/atlvdec | grep -o "GLIBC_[0-9.]*" | sort -uV | tr "\n" " "; echo
chown $(stat -c %u:%g /o) /o/*'
gcc -O2 -Wall -fno-omit-frame-pointer -shared -fPIC -fvisibility=hidden -o "$O/atlfix.so" "$S/atlfix/atlfix.c" -ldl
gcc -O2 -Wall -shared -fPIC -o "$O/low4g.so" "$S/low4g/low4g.c" -ldl
strip "$O/atlfix.so" "$O/low4g.so" 2>/dev/null || true
file "$O"/* | sed "s#$O/##"
