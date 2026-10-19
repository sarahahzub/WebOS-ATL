#!/bin/sh
# post-atl.sh: run INSIDE the atl2-build container after the atl stage, before appimage.
#  1. StubGen: fill api-impl with every public SDK API ATL lacks (safe defaults + ATLSTUB log), re-dex, install.
#  2. libatlbmpfix.so (bounds/sampled bitmap decoding) into ATL's natives dir.
# Inputs: /w/sdk/android.jar /w/sdk/asm*.jar /w/stubgen/StubGen*.class /w/extra/libatlbmpfix.so
set -eu
B=/w/src/android_translation_layer/build
HAX=$B/src/api-impl/hax.jar
DEX=/usr/lib/java/dex/android_translation_layer
[ -f "$HAX" ] || { echo "no $HAX (run the atl stage first)"; exit 1; }
cd /w/stubgen
JAVA=$(ls -d /usr/lib/jvm/java-*-openjdk 2>/dev/null | grep -v java-8 | sort -V | tail -1)/bin/java
"$JAVA" -Xmx3g -cp /w/sdk/asm.jar:/w/sdk/asm-tree.jar:. StubGen /w/sdk/android.jar "$HAX" /usr/lib/java/core-all_classes.jar /tmp/hax-full.jar /w/logs/stubgen-report.txt
rm -f /tmp/api-impl-full.jar
dx --dex --output=/tmp/api-impl-full.jar /tmp/hax-full.jar
install -m 644 /tmp/api-impl-full.jar "$DEX/api-impl.jar"
ls -la "$DEX/api-impl.jar"
if [ -f /w/extra/libatlbmpfix.so ]; then
	mkdir -p "$DEX/natives"
	install -m 755 /w/extra/libatlbmpfix.so "$DEX/natives/libatlbmpfix.so"
fi
ls -la "$DEX" "$DEX/natives" 2>/dev/null
echo "post-atl: done"
