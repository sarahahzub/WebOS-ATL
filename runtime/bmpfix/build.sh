#!/bin/sh
# Build libatlbmpfix.so (aarch64; bounds-checked and sampled JPEG/PNG/WebP bitmap decoding for ATL) inside
# debian:bookworm-slim (glibc 2.36 <= the runtime's 2.43). Run on an aarch64 host with docker.
#   NATIVEHELPER_INC=<art_standalone>/libnativehelper/include/nativehelper sh build.sh   -> out/libatlbmpfix.so
# post-atl.sh installs it from /w/extra/libatlbmpfix.so into the runtime.
set -e
cd "$(dirname "$0")"
INC=${NATIVEHELPER_INC:?set NATIVEHELPER_INC to art_standalone/libnativehelper/include/nativehelper}
mkdir -p out
docker run --rm -v "$PWD":/w -v "$INC":/jni:ro debian:bookworm-slim bash -c '
  set -e
  apt-get update -qq >/dev/null && apt-get install -y -qq gcc libc6-dev libjpeg-dev libwebp-dev libpng-dev zlib1g-dev >/dev/null
  cd /w
  gcc -O2 -Wall -Wextra -Wno-unused-parameter -shared -fPIC -fvisibility=hidden -I/jni -o out/libatlbmpfix.so atlbmpfix.c \
      /usr/lib/aarch64-linux-gnu/libjpeg.a /usr/lib/aarch64-linux-gnu/libwebp.a /usr/lib/aarch64-linux-gnu/libpng16.a /usr/lib/aarch64-linux-gnu/libz.a -lm -ldl -lpthread -Wl,--exclude-libs,ALL -Wl,-z,noexecstack
  strip --strip-unneeded out/libatlbmpfix.so
'
