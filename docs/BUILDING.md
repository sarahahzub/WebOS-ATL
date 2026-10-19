# Building

Everything is built on an **aarch64 Linux host with docker**. The releases are built on an Oracle Cloud Ampere VM
running Ubuntu 24.04. Allow about 60 GB of disk for the runtime build.

## 1. Native helpers

```sh
sh native/build.sh            # -> native/out/{xdgshim,atlvdec,atlfix.so,low4g.so}
```

## 2. Runtime image

The build scripts expect a work directory `~/atl2` (mounted as `/w` in the `atl2-build` container):

```sh
mkdir -p ~/atl2 && cp -r runtime/build runtime/patches ~/atl2/
# post-atl.sh inputs:
#   ~/atl2/sdk/android.jar          Android SDK platform 35 android.jar (sdkmanager "platforms;android-35")
#   ~/atl2/sdk/asm.jar asm-tree.jar  ObjectWeb ASM 9.x
#   ~/atl2/stubgen/StubGen.class     javac -cp asm.jar:asm-tree.jar runtime/stubgen/StubGen.java
#   ~/atl2/extra/libatlbmpfix.so     sh runtime/bmpfix/build.sh (needs art_standalone's libnativehelper headers)
sh ~/atl2/build/build-oracle.sh all          # hours the first time; logs in ~/atl2/logs/
```

The build produces the AppDir `~/atl2/AppDir`. Then pre-compile the boot image for the TV path and pack it:

```sh
cp -a ~/atl2/AppDir ~/atl2/AppDir-rel
sh runtime/build/bootimage.sh ~/atl2/AppDir-rel dist/webosatl-runtime-<version>.sqfs
```

`bootimage.sh` bind-mounts the AppDir at `/media/developer/webosatl/runtime` and runs a demo app once through
`~/atl2/smoke/smoke.py` (from the smoke test tools). This generates the boot image. Set `ATL_MOUNT` if the TV path
ever changes, because the boot image records it.

To change ATL itself, edit a `runtime/tvpatches/*.py` script or add a new one. Apply it to a git checkout of ATL at
`ATL_COMMIT`, export the series with `git format-patch` into `runtime/patches/android_translation_layer/`, and
rebuild with `sh ~/atl2/build/restart.sh finish`. See `runtime/build/reapply.sh`.

## 3. Chromium add-on (optional)

```sh
# Debian trixie armhf rootfs with Chromium:
sudo debootstrap --arch=armhf trixie ~/browser/deb-root http://deb.debian.org/debian
sudo chroot ~/browser/deb-root apt-get install -y chromium fonts-dejavu-core pulseaudio-utils
python3 -m venv ~/browser/venv && ~/browser/venv/bin/pip install patchelf
VER=<version> sh chromium/build.sh ~/browser/build-webosatl     # -> browser.sqfs
cp ~/browser/build-webosatl/browser.sqfs dist/webosatl-chromium-<version>.sqfs
```

(chroot into a foreign-architecture rootfs needs qemu-user-static and binfmt.)

## 4. Packages and the Homebrew repository

```sh
python3 tools/mkipk.py manager --version 0.1.0 --tag v0.1.0 \
    --runtime dist/webosatl-runtime-0.1.0.sqfs --runtime-version 0.1.0
python3 tools/mkipk.py chromium --version 0.1.0 --tag v0.1.0 --image dist/webosatl-chromium-0.1.0.sqfs
python3 tools/mkrepo.py --tag v0.1.0 dist/org.webosatl.manager_0.1.0_all.ipk dist/org.webosatl.chromium_0.1.0_arm.ipk
```

Upload the IPKs and the `.sqfs` images to the GitHub release `v0.1.0`, then commit `repo/`. If a release does not
change the runtime or the Chromium image, don't upload it again. Pass `--runtime-tag` / `--image-tag` with the
release that already has it. Release versions must be numeric `x.y.z`, because webOS compares them.

`tools/release.sh` runs steps 1 and 4 (and optionally 2 and 3) in one go.

## Tests

```sh
python3 tv/test_atld_memory.py      # atld memory policy, simulated (fake luna-send / SAM), no TV needed
python3 tv/apkinfo.py some.apk      # APK label / package / icon parser
python3 tv/apkcheck.py some.apk --jar <runtime>/lib/java/dex/android_translation_layer/api-impl.jar
```
