#!/bin/sh
# Build atlvdec on Oracle: armel (soft-float EABI5: the webOS userland is
# soft-float) for the TV (docker debian:bookworm-slim cross gcc; dynamic glibc, only symbols <= GLIBC_2.34; TV has 2.35) and native aarch64 bench tools (stub NDL).
#   sh ~/atl2/gpu/atlvdec/build.sh      -> ~/atl2/gpu/out/{atlvdec,feedtest-arm,atlvdec-stub,feedtest}
set -e
S=$(cd "$(dirname "$0")" && pwd)
O=$S/../out
mkdir -p "$O"
docker run --rm -v "$S":/s -v "$O":/o debian:bookworm-slim sh -c '
set -e
apt-get update -qq >/dev/null 2>&1; apt-get install -y -qq gcc-arm-linux-gnueabi libc6-dev-armel-cross >/dev/null 2>&1
CC="arm-linux-gnueabi-gcc -O2 -Wall -static-libgcc"
$CC -rdynamic -o /o/atlvdec /s/atlvdec.c /s/sf.c -ldl -lpthread
$CC -static -o /o/feedtest-arm /s/feedtest.c
arm-linux-gnueabi-strip /o/atlvdec /o/feedtest-arm
echo "glibc symbols needed by atlvdec:"; arm-linux-gnueabi-objdump -T /o/atlvdec | grep -o "GLIBC_[0-9.]*" | sort -uV | tr "\n" " "; echo
chown $(stat -c %u:%g /o) /o/*'
gcc -O2 -Wall -DNDL_STUB -DSF_STUB -rdynamic -o "$O/atlvdec-stub" "$S/atlvdec.c" "$S/sf.c" "$S/ndl_stub.c" "$S/sf_stub.c" -ldl -lpthread
gcc -O2 -Wall -o "$O/feedtest" "$S/feedtest.c"
file "$O/atlvdec" "$O/feedtest-arm"
