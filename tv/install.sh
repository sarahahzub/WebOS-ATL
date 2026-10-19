#!/bin/sh
# install.sh: install or repair webOS ATL (run as root; the manager app runs it through Homebrew Channel).
# Copies the programs from the manager app's payload/ to /media/developer/webosatl/bin, adds the boot hook and
# starts the services. The manager agent (atlmgr) then downloads the ~200 MB runtime image from GitHub Releases.
# Output: progress lines, last line "OK ..." or "ERROR ...".
P=$(cd "$(dirname "$0")" && pwd)
. "$P/env.sh"
fail() { echo "ERROR $*"; exit 1; }
[ "$(id -u)" = 0 ] || fail "needs root (rooted TV with Homebrew Channel)"
[ "$(uname -m)" = aarch64 ] || fail "needs a 64-bit (aarch64) TV, this one is $(uname -m)"
command -v python3 >/dev/null 2>&1 || fail "python3 not found on this TV"
command -v luna-send >/dev/null 2>&1 || fail "luna-send not found (is this webOS?)"
grep -q squashfs /proc/filesystems || modprobe squashfs 2>/dev/null; grep -q squashfs /proc/filesystems || fail "kernel has no squashfs support"
mkdir -p "$BIN" "$LOGS" || fail "cannot create $BASE"
free=$(df -Pm /media/developer | awk 'NR==2{print $4}')
if [ ! -f "$RUNTIME_IMG" ] && [ "${free:-0}" -lt 600 ]; then fail "needs 600 MB free in /media/developer, only ${free} MB"; fi
mem=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
echo "TV: $(uname -m), ${mem} MB RAM, ${free} MB free"
[ "${mem:-0}" -lt 1400 ] && echo "WARNING: less than 1.5 GB RAM; big apps may be closed by the system"
[ -d /media/developer/atl ] && echo "NOTE: an older ATL install is in /media/developer/atl (it uses the same ports; remove it)"
echo "copying programs"
for f in "$P"/*; do
	n=$(basename "$f")
	cp -f "$f" "$BIN/.$n.tmp" && mv -f "$BIN/.$n.tmp" "$BIN/$n" || fail "copy $n failed"
done
chmod 755 "$BIN"/*.sh "$BIN"/*.py "$BIN/dex2oat-verify" "$BIN/xdgshim" "$BIN/atlvdec" 2>/dev/null
[ -f "$BASE/settings.json" ] || echo '{}' > "$BASE/settings.json"
rm -f "$BASE/disable"
echo "adding boot hook"
mkdir -p "$INITD" || fail "no $INITD (Homebrew Channel missing?)"
printf '#!/bin/sh\n# webOS ATL: start at boot (see %s)\n[ -f %s ] && exec sh %s start\n' "$BIN/init.sh" "$BIN/init.sh" "$BIN/init.sh" > "$HOOK.tmp" && chmod 755 "$HOOK.tmp" && mv -f "$HOOK.tmp" "$HOOK" || fail "cannot write $HOOK"
echo "starting services"
sh "$BIN/init.sh" restart atld.py apkweb.py atlmgr.py
sleep 2
sh "$BIN/init.sh" status
echo "OK installed $(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$BIN/release.json" | head -1)"
