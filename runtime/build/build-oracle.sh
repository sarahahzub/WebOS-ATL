#!/bin/sh
# Run on the Oracle server (aarch64). Creates/starts the atl2-build container and runs a build stage in it.
#   sh build-oracle.sh [stage]        stage = setup|deps|vixl|wolfssl|bionic|art|opensles|atl|appimage|all (default all)
#   sh build-oracle.sh reset <stage>  forget a stage stamp so it rebuilds
# Logs: ~/atl2/logs/<stage>.log ; output AppImage: ~/atl2/dist/
set -eu
W=$HOME/atl2
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$W/logs" "$W/build" "$W/patches"
[ "$HERE" = "$W/build" ] || cp -f "$HERE"/*.sh "$HERE"/versions.env "$W/build/"
if [ "${1:-}" = reset ]; then rm -f "$W/stamps/$2"; echo "reset $2"; exit 0; fi
if ! sudo docker ps -a --format '{{.Names}}' | grep -qx atl2-build; then
	sudo docker run -d --name atl2-build --platform linux/arm64 -v "$W":/w ghcr.io/pkgforge-dev/archlinux:latest sleep infinity
fi
sudo docker start atl2-build >/dev/null
S=${1:-all}
sudo docker exec atl2-build sh /w/build/build-in-container.sh "$S" 2>&1 | tee "$W/logs/$S.log"
