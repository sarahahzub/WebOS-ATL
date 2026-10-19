#!/bin/sh
# ATL v2 build, run INSIDE the atl2-build container (ghcr.io/pkgforge-dev/archlinux, aarch64, /w = ~/atl2 on Oracle).
# Derived from pkgforge-dev/android_translation_layer-AppImage get-dependencies.sh + make-appimage.sh, with:
#   - pinned upstream commits (versions.env)
#   - our patch series applied from /w/patches/<component>/*.patch (git apply, numeric order)
#   - resumable stages (/w/stamps/<stage>)
# usage: build-in-container.sh [setup|deps|vixl|wolfssl|bionic|art|opensles|atl|post|appimage|all|artinc|rebuild]
#   rebuild = artinc + atl (fresh, with the current patch series) + post + appimage  (default all)
set -eu
. /w/build/versions.env
W=/w; SRC=$W/src; LOG=$W/logs; ST=$W/stamps
mkdir -p "$SRC" "$LOG" "$ST"
export JAVA_HOME=/usr/lib/jvm/java-8-openjdk
export PATH="$JAVA_HOME/bin:$PATH"
export JAVA_TOOL_OPTIONS=-Xmx4096M
J=$(nproc)
GL=https://gitlab.com/android_translation_layer
PK=$SRC/pkgforge

dl() { # <out> <url> : resumable download with stall detection
	rm -f "$1"
	for i in 1 2 3 4 5; do
		curl -fsSL --connect-timeout 20 --speed-limit 10000 --speed-time 30 --max-time 900 -o "$1" "$2" && return 0
		echo "   download retry $i: $2"; sleep 5
	done
	return 1
}

stage() { [ -f "$ST/$1" ] && { echo "== $1: done (stamp)"; return 1; }; echo "== $1: start $(date -u +%T)"; return 0; }
done_() { date -u +%s > "$ST/$1"; echo "== $1: done $(date -u +%T)"; }

apply_patches() { # <component> <dir>
	for p in $(ls "$W/patches/$1"/*.patch 2>/dev/null | sort); do
		echo "   patch $1: $(basename "$p")"
		git -C "$2" apply --whitespace=nowarn "$p"
	done
}

clone() { # <repo> <commit> <dir>
	[ -d "$3/.git" ] || git clone -q "$GL/$1" "$3"
	git -C "$3" fetch -q origin
	git -C "$3" checkout -q -f "$2"
	git -C "$3" clean -q -fdx -e build -e out
}

s_setup() {
	stage setup || return 0
	sh "$W/build/anylinux-setup.sh"
	[ -d "$PK/.git" ] || git clone -q https://github.com/pkgforge-dev/android_translation_layer-AppImage "$PK"
	git -C "$PK" checkout -q "$PKGFORGE_COMMIT"
	done_ setup
}

s_deps() {
	stage deps || return 0
	pacman -Syu --noconfirm --needed alsa-lib cmake elfutils glib2 glib2-devel graphene gtk4 gtk4-layer-shell \
		jdk8-openjdk jre-openjdk libbsd libcap libglvnd libgudev libpng libportal libsndfile libunwind lz4 meson \
		openxr openssl pipewire-audio sdl2 sqlite valgrind vulkan-headers vulkan-icd-loader wayland-protocols \
		webkitgtk-6.0 xz zlib zip libjpeg-turbo libwebp
	get-debloated-pkgs --add-common --prefer-nano ffmpeg-mini
	done_ deps
}

s_vixl() {
	stage vixl || return 0
	cd "$SRC"; [ -f vixl-$VIXL_VERSION/SConstruct ] || { rm -rf vixl-$VIXL_VERSION; dl vixl.tgz https://github.com/Linaro/vixl/archive/refs/tags/$VIXL_VERSION.tar.gz; tar xzf vixl.tgz; }
	cd vixl-$VIXL_VERSION
	[ -f meson.build ] || patch -p1 < "$PK/patches/vixl-meson-support.patch"
	meson setup build --wipe --prefix=/usr --libdir=lib --buildtype=release -Dsimulator=none
	meson compile -C build; meson install -C build
	done_ vixl
}

s_wolfssl() {
	stage wolfssl || return 0
	cd "$SRC"; D=wolfssl-$WOLFSSL_VERSION-stable
	[ -f $D/configure.ac ] || { rm -rf $D; dl wolfssl.tgz https://github.com/wolfSSL/wolfssl/archive/refs/tags/v$WOLFSSL_VERSION-stable.tar.gz; tar xzf wolfssl.tgz; }
	cd $D; ./autogen.sh
	./configure --prefix=/usr --libdir=/usr/lib --sysconfdir=/etc --mandir=/usr/share/man --localstatedir=/var \
		--enable-shared --enable-static --enable-reproducible-build --disable-opensslall --disable-opensslextra \
		--enable-aescbc-length-checks --enable-curve25519 --enable-ed25519 --enable-ed25519-stream --disable-oldtls \
		--enable-base64encode --enable-tlsx --enable-scrypt --disable-examples --enable-keygen --enable-wolfssh --enable-jni
	make -j"$J"; make install
	done_ wolfssl
}

meson_comp() { # <stage> <repo> <commit> [meson args]
	n=$1 r=$2 c=$3; shift 3
	stage "$n" || return 0
	clone "$r" "$c" "$SRC/$r"
	cd "$SRC/$r"
	if [ "$r" = android_translation_layer ]; then # pkgforge's .desktop path fix (irrelevant on the TV); stale vs. newer upstream
		patch -p1 --forward --dry-run < "$PK/patches/atl-desktop-entry-binary-path.patch" >/dev/null 2>&1 \
			&& patch -p1 --forward < "$PK/patches/atl-desktop-entry-binary-path.patch" || echo "   skip pkgforge atl-desktop-entry-binary-path.patch (does not apply)"
	fi
	apply_patches "$r" "$SRC/$r"
	meson setup build --wipe --prefix=/usr --libdir=lib --buildtype=release "$@"
	meson compile -C build; meson install -C build
	git rev-parse --short HEAD > "$W/out-$r.version" 2>/dev/null || true
	done_ "$n"
}

s_art() {
	stage art || return 0
	clone art_standalone "$ART_COMMIT" "$SRC/art_standalone"
	cd "$SRC/art_standalone"
	for p in art-addr2line-path art-dx-heap art-wolfssljni-host-cacerts art-ipv4-fixes; do patch -p1 < "$PK/patches/$p.patch"; done
	apply_patches art_standalone "$SRC/art_standalone"
	make ____PREFIX=/usr ____LIBDIR=lib -j"$J"
	make ____PREFIX=/usr ____INSTALL_ETC=/etc ____LIBDIR=lib install
	done_ art
}

s_artinc() { # apply not-yet-applied art patches to the existing tree and rebuild incrementally (keeps out/)
	cd "$SRC/art_standalone"
	for p in $(ls "$W/patches/art_standalone"/*.patch 2>/dev/null | sort); do
		if git apply --check -R "$p" >/dev/null 2>&1; then echo "   art patch already in: $(basename "$p")"; else echo "   art patch: $(basename "$p")"; git apply --whitespace=nowarn "$p"; fi
	done
	make ____PREFIX=/usr ____LIBDIR=lib -j"$J"
	make ____PREFIX=/usr ____INSTALL_ETC=/etc ____LIBDIR=lib install
	date -u +%s > "$ST/art"
	echo "== artinc: done $(date -u +%T)"
}

s_appimage() {
	rm -f "$ST/appimage"; stage appimage || true
	cd "$W"; rm -rf AppDir dist
	mkdir -p AppDir   # quick-sharun wants one top-level .desktop + icon
	printf '[Desktop Entry]\nType=Application\nName=Android Translation Layer\nExec=android-translation-layer\nIcon=android-translation-layer\nCategories=Utility;\nTerminal=false\n' > AppDir/android-translation-layer.desktop
	cp /w/build/atl-icon.png AppDir/android-translation-layer.png
	cp /w/build/atl-icon.png AppDir/.DirIcon
	export ARCH=aarch64 OUTPATH=$W/dist DEPLOY_OPENGL=1 DEPLOY_GSTREAMER=1 DEPLOY_PIPEWIRE=1
	export UPINFO=""
	quick-sharun /usr/bin/android-translation-layer /usr/bin/addr2line /usr/bin/dex2oat /usr/bin/dalvikvm /usr/bin/dx \
		/usr/lib/libOpenSLES.so* /usr/lib/java /usr/lib/art /usr/share/atl
	echo 'ATL_APP_LAUNCHER=$APPIMAGE' >> ./AppDir/.env
	quick-sharun --make-appimage
	ls -la "$W/dist"
	done_ appimage
}

what=${1:-all}
case $what in
	setup) s_setup;; deps) s_deps;; vixl) s_vixl;; wolfssl) s_wolfssl;;
	bionic) meson_comp bionic bionic_translation "$BIONIC_COMMIT";;
	art) s_art;;
	opensles) meson_comp opensles libopensles-standalone "$OPENSLES_COMMIT";;
	atl) meson_comp atl android_translation_layer "$ATL_COMMIT";;
	appimage) s_appimage;;
	all)
		s_setup; s_deps; s_vixl; s_wolfssl
		meson_comp bionic bionic_translation "$BIONIC_COMMIT"
		s_art
		meson_comp opensles libopensles-standalone "$OPENSLES_COMMIT"
		meson_comp atl android_translation_layer "$ATL_COMMIT"
		sh /w/build/post-atl.sh
		s_appimage;;
	post) sh /w/build/post-atl.sh;;
	artinc) s_artinc;;
	finish) rm -f "$ST/atl"; meson_comp atl android_translation_layer "$ATL_COMMIT"; sh /w/build/post-atl.sh; s_appimage;;
	rebuild) s_artinc; rm -f "$ST/atl"; meson_comp atl android_translation_layer "$ATL_COMMIT"; sh /w/build/post-atl.sh; s_appimage;;
	*) echo "unknown stage $what"; exit 2;;
esac
