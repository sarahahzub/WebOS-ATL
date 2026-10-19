#!/bin/sh
# restart.sh [stage]: stop a running ATL v2 build (host + container) and start it again in the background.
W=$HOME/atl2
for p in $(pgrep -f "sh $W/build/build-oracle.sh|sh build/build-oracle.sh"); do kill "$p" 2>/dev/null; done
sudo docker exec atl2-build sh -c 'for p in $(pgrep -f build-in-container.sh) $(pgrep curl); do kill $p; done' 2>/dev/null
sleep 2
cd "$W" && setsid nohup sh "$W/build/build-oracle.sh" "${1:-all}" > "$W/logs-all.out" 2>&1 < /dev/null &
echo "restarted (${1:-all}); log: $W/logs-all.out"
