# webOS ATL

Run Android apps on a rooted LG webOS TV. webOS ATL packages
[Android Translation Layer](https://gitlab.com/android_translation_layer) (ATL) for webOS. ATL runs the app's
Java code on ART and maps Android APIs onto GTK/Wayland, without an emulator or an Android VM. Each Android app gets
its own icon in the TV's app list and opens like any other app.

> Experimental. Many apps work and many don't yet. Expect rough edges.

## Requirements

- A **rooted** LG TV with [Homebrew Channel](https://github.com/webosbrew/webos-homebrew-channel)
  (root status "ok" in its settings).
- A 64-bit CPU: `uname -m` must say `aarch64`.
- **600 MB** free in `/media/developer` (the runtime takes about 200 MB, apps and their data take the rest), and
  160 MB more for the optional Chromium add-on.
- **1.2 GB RAM** or more. One big Android app needs 300–700 MB, so only one runs at a time by default.
- Tested only on the **LG 43UR7500 (webOS 23)**. Reports from other models are welcome
  ([compatibility list](docs/COMPAT.md)).

## Safety: read this first

- **Android apps run as root** on your TV, outside the webOS app sandbox. Only install APKs you trust, from their
  official source.
- The phone install page needs the **PIN** shown in the webOS ATL app. The manager's own service only answers the
  manager app, so other devices on your network and web pages open in the TV's browser can't install or remove
  apps.
- webOS ATL does not modify any webOS system file. It only writes:
  - `/media/developer/webosatl/` (programs, runtime, apps' data);
  - one wrapper app per Android app (`org.webosatl.app.*`) in the developer apps folder;
  - the boot hook `/var/lib/webosbrew/init.d/60-webosatl`;
  - a luna-service2 role for its video helper, which is checked before it is installed.
- **Turn it off without uninstalling:** `touch /media/developer/webosatl/disable` (SSH or Telnet from Homebrew
  Channel), then reboot. If the TV ever crashes during start-up, Homebrew Channel's failsafe mode skips all boot
  hooks.
- Downloads always leave at least 150 MB free in `/media/developer`.

## Install

1. In Homebrew Channel open **Settings**, and under extra repositories add:
   ```
   https://raw.githubusercontent.com/sarahahzub/WebOS-ATL/main/repo/apps.json
   ```
2. Go back to the app list and install **webOS ATL**.
3. Open **webOS ATL** and press **Install**. The setup copies its programs to `/media/developer/webosatl`, adds a
   start-up hook and starts the service. It then downloads the Android runtime (about 200 MB) in the background. The
   Status page shows the progress.
4. Install Android apps:
   - from a phone or laptop on the same network: open `http://<tv-ip>:8790/` (the address is shown in the app),
     then pick an `.apk` file or paste a download link;
   - or type a direct `.apk` link on the **Android apps** page.

   The app shows up in the TV's app list a few seconds later.
5. Optional: install **webOS ATL Chromium** from the same repository to get a web browser. webOS ATL downloads its
   image (about 160 MB) by itself.

Updates come through Homebrew Channel like any other homebrew app. The manager applies new programs and runtime
images on its own (a new runtime waits until no Android app is running).

## Using Android apps

- Remote arrows move a pointer and OK clicks. For Android TV apps, set **Remote arrows: Keys** in the app's settings
  (webOS ATL → Android apps → the app). RED switches between pointer and keys while the app is open.
- CH+/CH- scroll. BACK is Android's back button.
- Text fields open the TV keyboard. You can also type from the phone page ("Type on TV").
- Only one Android app runs at a time by default. Opening another one closes the first, and an app left in the
  background is stopped after 10 minutes. Both can be changed in Settings.
- Per-app settings: screen density, Android TV mode, hardware video decoding, remote arrows.

## What does not work (yet)

- Google Play services (Google sign-in, Play Store, push notifications, most DRM). Apps that need them won't run.
- WebView is limited: some pages or captchas inside apps stay blank.
- Hardware video decoding (per app, or for all apps in Settings) works with some players. Others fall back to
  software decoding, which is slow at 1080p.
- Chromium: the three-dot menu does not open, and full-screen video may close the browser.
- Some dialogs don't show the check marks of their selected items.

## Uninstall

In webOS ATL, press **Uninstall webOS ATL** on the Status page. This removes every Android app installed with it,
their data, the runtime and the start-up hook. Then remove **webOS ATL** (and **webOS ATL Chromium**) in Homebrew
Channel.

## How it works

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). In short:

- `runtime.sqfs`: the ATL runtime (ART, ATL, GTK4, Mesa and friends) for aarch64. It is built from pinned upstream
  commits plus the patches in `runtime/patches/` and loop-mounted read-only at `/media/developer/webosatl/runtime`.
- `atld` (root service) starts and stops the Android apps for their webOS wrapper apps (`org.webosatl.app.*`) and
  manages memory.
- `xdgshim` connects the app's Wayland window to the webOS compositor and turns remote keys into pointer, key and
  scroll events. `atlvdec` feeds video to the TV's hardware decoder.
- `apkweb` serves the phone install page. `atlmgr` serves the manager app's API and applies updates.

Building from source: [docs/BUILDING.md](docs/BUILDING.md).

## License

GPL-3.0-or-later (see [LICENSE](LICENSE)), the same as Android Translation Layer. Android apps you install are
subject to their own licenses. webOS ATL is not affiliated with LG, Google or the ATL project.
