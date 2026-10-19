# atlvdec — findings on the LG 43UR7500 (webOS 23)

- **Toolchain:** armel soft-float (`arm-linux-gnueabi`, interpreter `/lib/ld-linux.so.3`); the webOS userland is
  soft-float. Dynamic glibc, symbols ≤ GLIBC_2.34 (TV 2.35). `build.sh` uses debian:bookworm-slim.
- **luna-service2 role required:** NDL (uMediaClient) registers `com.webos.media.client.*` etc.; without a role for
  the executable `NDL_DirectMediaLoad` aborts with `LSRegisterPubPriv FAILED`. ATL wrapper apps only have a role for
  `bin/run` (a shell script), so `ls2-role.sh install` (run once on the TV as root) clones one for
  `/media/developer/webosatl/bin/atlvdec` (`org.webosatl.atlvdec`). Remove with `ls2-role.sh remove`.
- **SDL window required:** NDL's impl calls `SDL_webOSCreateExportedWindow()`. With only `SDL_Init(VIDEO)`,
  `NDL_DirectMediaLoad` returns -1. With a fullscreen transparent SDL window (TV `/usr/lib/libSDL2-2.0.so.0`,
  `ATLVDEC_SDL=2`, default) Load returns 0, callbacks 22 (LOADCOMPLETED) and 26 (PLAYING), `NDL_DirectVideoPlay` rc 0.
- **Window order:** the wrapper (`run.sh`) starts atlvdec before xdgshim; atlvdec creates its SDL window at startup
  (`ATLVDEC_EARLY`, default on). The ATL window still shows on top (screenshot `shot-hw1.png`).
  The SDL window is a second surface of the appId: it needs `SDL_WEBOS_ACCESS_POLICY_KEYS_BACK=true` too,
  otherwise LSM shows its own "exit app?" prompt on BACK (run.sh sets it).
- **Timing:** OPEN → Load done ≈ 2.1 s on the first load in a session (NDL init done at startup); ATL waits ≤ 10 s.
- The jailed app process tree (uid 5630) can execute `/media/developer/atl/gpu/atlvdec` and use `/tmp/xdg`.
- NDL runs a GStreamer (starfish/omx) pipeline inside the helper process.
- **Picture (iteration 2):** NDL's impl creates its video element with `SDL_webOSCreateExportedWindow()` on the helper's
  own SDL window, which LSM never shows (the ATL window is the app's card), so the plane stayed black. ATL-window
  binding (`run.sh` VIDEO_EXPORT=1 → `ATLVDEC_WINID_FILE`): xdgshim exports the ATL toplevel (`XDGSHIM_EXPORT`), atlvdec's
  interposed `SDL_webOSCreateExportedWindow()` returns that id, `SDL_webOSSetExportedWindow(src, dst)` goes to xdgshim via
  `<winid>.region` (`set_exported_window`). No second SDL window (`ATLVDEC_SDL=1`). Bench: `benchtest.sh`.
- **Codec re-create:** ExoPlayer re-creates the codec on a Surface change (SDK < 23) and waits only 2 s; a CLOSE keeps
  the decoder loaded `ATLVDEC_LINGER` s (default 3) and a same-codec/size OPEN reuses it (no ~2 s load).
