#!/bin/sh
# launch.sh <xdgshim-socket>: starts the TV browser. Lives in browser.sqfs as ghb/launch.sh; browserd.sh (root) runs it
# with setsid, so this shell (then Chromium, after exec) leads its own session.
#   Chromium (armhf, own loader + libs) -> brwl (BACK / scroll / fullscreen fixes, OOM guard) -> xdgshim -1 -> webOS
# Settings: /media/developer/webosatl/chromium/browser.conf (ZOOM, START_URL, OOM_SCORE_ADJ, EXTRA_FLAGS).
M=/media/developer/webosatl/chromium/mnt   # fixed: Chromium's PT_INTERP points at $M/usr/lib/ld-linux-armhf.so.3
B=/media/developer/webosatl/chromium
S=$1
[ -S "$S" ] || { echo "launch.sh: no xdgshim socket '$S'" >&2; exit 1; }
ZOOM=150; SCALE=2; START_URL=; OOM_SCORE_ADJ=1000; EXTRA_FLAGS=
[ -f "$B/browser.conf" ] && . "$B/browser.conf"
echo $$ > "$B/chromium.pid"
echo "$OOM_SCORE_ADJ" > /proc/self/oom_score_adj 2>/dev/null   # inherited by brwl and every Chromium process
P=$B/profile; H=$B/home
mkdir -p "$P/Default" "$H/.cache" "$H/.config" "$B/downloads"
# first run: TV defaults (home page, page zoom, no translate / password prompts, downloads dir).
# Size: SCALE = integer UI scale (brwl tells Chromium the output has scale SCALE; --force-device-scale-factor
# makes Chromium draw a SCALE-times bigger, cropped window here) for the whole UI (toolbar, tabs, menus; 2 = sofa-readable on the 43" TV),
# ZOOM = how big pages look in % of a 1080p desktop; the profile's page zoom is ZOOM / SCALE (150 / 2 = 75 %).
# Only integer SCALE: without wp_viewporter / fractional-scale (the TV has neither) Chromium 150 renders a fractional
# device scale into a wrongly sized, cropped buffer (bench run smoke5).
case "$SCALE" in 1|2|3) ;; *) SCALE=2 ;; esac
case $((ZOOM / SCALE)) in   # Chromium zoom level = log(zoom) / log(1.2) (busybox awk may lack math)
50) Z=-3.801784 ;; 55) Z=-3.279025 ;; 62) Z=-2.577883 ;; 66|67) Z=-2.223901 ;; 75) Z=-1.577883 ;; 87) Z=-0.732395 ;;
100) Z=0 ;; 110) Z=0.522759 ;; 125) Z=1.223901 ;; 175) Z=3.069389 ;; 200) Z=3.801784 ;; *) Z=2.223901 ;;
esac
if [ ! -f "$P/Default/Preferences" ]; then
	sed "s/@ZOOM_LEVEL@/$Z/" "$M/ghb/Preferences" > "$P/Default/Preferences"
	echo "$Z" > "$P/.ghb-zoom"
fi
# ZOOM / SCALE changed since the profile was made (or a profile from before SCALE): new default page zoom
read -r OLDZ < "$P/.ghb-zoom" 2>/dev/null
if [ "$OLDZ" != "$Z" ]; then
	sed -i "s/\"default_zoom_level\": *{ *\"x\": *[-0-9.e]*/\"default_zoom_level\":{\"x\":$Z/" "$P/Default/Preferences"
	echo "$Z" > "$P/.ghb-zoom"
fi
# after a kill: no "restore pages?" bubble
sed -i 's/"exit_type":"Crashed"/"exit_type":"Normal"/; s/"exited_cleanly":false/"exited_cleanly":true/' "$P/Default/Preferences"
rm -f /tmp/xdg/ghb-*.sock   # brwl's sockets of earlier runs (one browser at a time)
L=/tmp/xdg/ghb-$$.sock
BRWL_SCALE=$SCALE "$M/ghb/brwl" -o "$OOM_SCORE_ADJ" "$L" "$S" < /dev/null &
i=0; while [ ! -S "$L" ] && [ $i -lt 100 ]; do sleep 0.05; i=$((i+1)); done
LIB=$M/usr/lib/arm-linux-gnueabihf
# nothing from the TV's (soft-float) /usr/lib may be loaded: every library comes from the bundle
export LD_LIBRARY_PATH=$LIB:$LIB/pulseaudio:$LIB/nss:$M/usr/lib/chromium
export LD_PRELOAD=$M/ghb/gbfix.so   # LG PulseAudio 15 <-> libpulse 17 fix (atlfix.c part 1)
export HOME=$H XDG_CACHE_HOME=$H/.cache XDG_CONFIG_HOME=$H/.config XDG_DATA_DIRS=$M/usr/share
export XDG_RUNTIME_DIR=/tmp/xdg WAYLAND_DISPLAY=$L
export FONTCONFIG_FILE=$M/etc/fonts/fonts.conf XKB_CONFIG_ROOT=$M/usr/share/X11/xkb XLOCALEDIR=$M/usr/share/X11/locale
export GCONV_PATH=$LIB/gconv GSETTINGS_SCHEMA_DIR=$M/usr/share/glib-2.0/schemas GIO_MODULE_DIR=$LIB/gio/modules
export ALSA_CONFIG_PATH=$M/usr/share/alsa/alsa.conf PULSE_SERVER=${PULSE_SERVER:-unix:/var/run/pulse/native}
export DBUS_SESSION_BUS_ADDRESS=disabled: LANG=C.UTF-8 LOCPATH=$M/usr/lib/locale   # not the TV's locale-archive
unset DISPLAY
# memory flags: phase-2 bench winner (bench.md "Exact flags"), minus --disable-sync / --disable-background-networking
# (Google sign-in + sync with the API keys browserd.sh exports from $B/google-keys.env); TV usability: LG keyboard (text-input-v3),
# spatial navigation (RED toggles xdgshim's arrows from pointer to keys), no crash-restore bubble (popups are hidden).
# OzoneBubblesUsePlatformWidgets off: bubbles (the link-URL status bubble on hover, ...) are drawn inside the window;
# as a wl_subsurface the TV compositor (LSM) showed one as an appId-less card that minimized the browser (drop to Home)
exec "$M/usr/lib/chromium/chromium" --ozone-platform=wayland --no-sandbox --disable-gpu --enable-low-end-device-mode \
	--renderer-process-limit=1 --process-per-site \
	--disable-features=Translate,OptimizationHints,MediaRouter,BackForwardCache,Vulkan,OzoneBubblesUsePlatformWidgets \
	--js-flags=--max-old-space-size=160 --disk-cache-size=33554432 --no-first-run --no-default-browser-check \
	--disable-component-update --disable-breakpad \
	--password-store=basic --autoplay-policy=no-user-gesture-required --test-type \
	--enable-wayland-ime --wayland-text-input-version=3 \
	--enable-spatial-navigation --hide-crash-restore-bubble --lang=en-US \
	--user-data-dir="$P" $EXTRA_FLAGS "${START_URL:-file://$M/ghb/start/index.html}" < /dev/null
