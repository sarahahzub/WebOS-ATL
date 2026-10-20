# Compatibility

## TVs

| Model | webOS | RAM | Result |
|---|---|---|---|
| LG 43UR7500 | webOS 23 (8.x) | 1.2 GB | works (the development TV) |

Send a report (issue template "TV report") with `uname -m`, the webOS version, your RAM and what happened.

## Apps

Tested on the 43UR7500. "Works" means it starts, can be used with the remote, and plays sound and video if it has
any.

| App | Result | Notes |
|---|---|---|
| NewPipe | works | turn on hardware video decode for smooth 1080p |
| SmartTube | works | `--sdk-int 28` is set automatically |
| Gravity Defied | works | |
| Offline Currency Converter | works | |
| Apps that need Google Play services | no | no Google sign-in, Play Store or Firebase push |
| Apps with DRM video (Widevine) | no | |

webOS ATL writes a compatibility report for every installed APK. It lists the Android classes and methods the app
uses that ATL lacks. Find it under Android apps → the app → App log, or open `http://<tv-ip>:8790/` → compat.
