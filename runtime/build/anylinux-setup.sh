#!/bin/sh
# copy of https://github.com/pkgforge-dev/anylinux-setup-action/blob/main/setup.sh (provides quick-sharun, get-debloated-pkgs)
# installs the packaging dependencies and deploys the anylinux tools
# runs on the archlinux container natively and inside foreign arch containers
# that are driven through qemu user emulation (binfmt handlers must exist)

set -e

ARCH=${ARCH:-$(uname -m)}
ANYLINUX_TOOLS_DIR=${ANYLINUX_TOOLS_DIR:-/usr/local/bin}

# the port mirrors occasionally drop connections mid transaction
pacman_retry() {
	n=0
	while ! pacman "$@"; do
		n=$((n + 1))
		[ "$n" -lt 3 ] || return 1
		sleep 5
	done
}

_get_anylinux_tool() {
	echo "DOWNLOADING '$2' to '$1'"

	_count=0
	while [ "$_count" -lt 3 ]; do
		if wget --retry-connrefused --tries=3 -O "$@"; then
			return 0
		else
			_status=$?
			>&2 echo "'wget $*' exited with $_status"
			>&2 echo "Trying again..."
		fi
		_count=$((_count + 1))
		sleep 10
	done

	>&2 echo "ERROR: Failed to download 3 times!"
	return 1
}

echo "Installing basic packaging dependencies..."
echo "---------------------------------------------------------------"

pacman-key --init

# archlinux-keyring is not packaged on the powerpc port, its keys ship in the image
case "$ARCH" in
	ppc64|ppc64le) :;;
	*) pacman_retry -Syy --noconfirm archlinux-keyring;;
esac

pacman_retry -Syu --needed --noconfirm \
	7zip \
	base-devel \
	freetype2 \
	git \
	jq \
	libx11 \
	libxrandr \
	libxss \
	nspr \
	nss \
	nss-mdns \
	patchelf \
	pulseaudio \
	pulseaudio-alsa \
	unzip \
	wget \
	xorg-server-xvfb \
	zsync

mkdir -p "$ANYLINUX_TOOLS_DIR"

QUICK_SHARUN=${QUICK_SHARUN:-https://raw.githubusercontent.com/pkgforge-dev/Anylinux-AppImages/refs/heads/main/useful-tools/quick-sharun.sh}
DEBLOATED_PACKAGES=${DEBLOATED_PACKAGES:-https://raw.githubusercontent.com/pkgforge-dev/Anylinux-AppImages/refs/heads/main/useful-tools/get-debloated-pkgs.sh}
MAKE_AUR_PACKAGE=${MAKE_AUR_PACKAGE:-https://raw.githubusercontent.com/pkgforge-dev/Anylinux-AppImages/refs/heads/main/useful-tools/make-aur-package.sh}

_get_anylinux_tool "$ANYLINUX_TOOLS_DIR"/quick-sharun "$QUICK_SHARUN"
_get_anylinux_tool "$ANYLINUX_TOOLS_DIR"/get-debloated-pkgs "$DEBLOATED_PACKAGES"
_get_anylinux_tool "$ANYLINUX_TOOLS_DIR"/make-aur-package "$MAKE_AUR_PACKAGE"

chmod +x \
	"$ANYLINUX_TOOLS_DIR"/quick-sharun \
	"$ANYLINUX_TOOLS_DIR"/get-debloated-pkgs \
	"$ANYLINUX_TOOLS_DIR"/make-aur-package

echo "SHA256SUMS:"
sha256sum \
	"$ANYLINUX_TOOLS_DIR"/quick-sharun \
	"$ANYLINUX_TOOLS_DIR"/get-debloated-pkgs \
	"$ANYLINUX_TOOLS_DIR"/make-aur-package

echo "CONTAINER IS READY! Tools installed in $ANYLINUX_TOOLS_DIR."
