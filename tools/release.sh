#!/bin/sh
# release.sh <version>: build every release asset into dist/ and update repo/ (aarch64 host with docker; see
# docs/BUILDING.md). Upload dist/* to the GitHub release v<version> afterwards (gh release create ...), then commit repo/.
# Env:
#   APPDIR=<AppDir>          build a new runtime image from this ATL AppDir (runtime/build/bootimage.sh)
#   RUNTIME=<file.sqfs> RUNTIME_VERSION=x.y.z RUNTIME_TAG=vX   reuse an already released runtime instead
#   CHROMIUM_IMAGE=<browser.sqfs>   include the Chromium add-on with this image (chromium/build.sh)
#   CHROMIUM_TAG=vX                 ...already uploaded to that release (not copied to dist/)
set -eu
V=${1:?usage: release.sh <version>}
R=$(cd "$(dirname "$0")/.." && pwd)
D=$R/dist; mkdir -p "$D"
echo "== native"; sh "$R/native/build.sh" "$R/native/out" > /dev/null
if [ -n "${APPDIR:-}" ]; then
	echo "== runtime from $APPDIR"
	rm -rf "$APPDIR-rel"; cp -a "$APPDIR" "$APPDIR-rel"
	sh "$R/runtime/build/bootimage.sh" "$APPDIR-rel" "$D/webosatl-runtime-$V.sqfs"
	RUNTIME=$D/webosatl-runtime-$V.sqfs RUNTIME_VERSION=$V RUNTIME_TAG=v$V
fi
: "${RUNTIME:?set APPDIR or RUNTIME}"
python3 "$R/tools/mkipk.py" manager --version "$V" --tag "v$V" --runtime "$RUNTIME" \
	--runtime-version "${RUNTIME_VERSION:-$V}" --runtime-tag "${RUNTIME_TAG:-v$V}" --out "$D"
IPKS="$D/org.webosatl.manager_${V}_all.ipk"
if [ -n "${CHROMIUM_IMAGE:-}" ]; then
	if [ -z "${CHROMIUM_TAG:-}" ]; then cp "$CHROMIUM_IMAGE" "$D/webosatl-chromium-$V.sqfs"; CHROMIUM_IMAGE=$D/webosatl-chromium-$V.sqfs; fi
	python3 "$R/tools/mkipk.py" chromium --version "$V" --tag "v$V" --image "$CHROMIUM_IMAGE" \
		--image-tag "${CHROMIUM_TAG:-v$V}" --out "$D"
	IPKS="$IPKS $D/org.webosatl.chromium_${V}_arm.ipk"
fi
python3 "$R/tools/mkrepo.py" --tag "v$V" $IPKS
( cd "$D" && sha256sum *.ipk *.sqfs 2>/dev/null > SHA256SUMS; cat SHA256SUMS )
