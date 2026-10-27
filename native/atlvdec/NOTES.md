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

## Starfish backend (protocol 2, `sf.c`): written 2026-10-27, not yet run on the TV

**Why.** CSWeb (cloudstream-webos-native) plays through LG's StarfishMediaAPIs. Its playback is smooth, and the
colours are right. ATL with NDL is laggy on the same TV. The two paths differ like this:
- **NDL** runs in low-delay mode. A frame is shown when it is decoded, and the pts NDL got was "ms since load". So
  every bit of jitter on the way showed on screen: ExoPlayer's release times (60 Hz Choreographer, audio clock),
  ART, the socket, the helper's poll loop. The smoothing grid reduced this but couldn't remove it.
- **NDL gets no frame rate.** Native App Kit notes: 24p declared as 60 is presented at 13 fps (judder).
- **NDL gets no colour or HDR data.** HDR10 streams play as SDR.

**How the Starfish backend works.**
- **Load.** The pipeline is loaded video-only in buffer-feed mode (`BUFFERSTREAM`), not in low-delay mode, with the
  real frame rate (`esInfo.videoFps*`, `maxFrameRate`) and `setHdrInfo`.
- **Feeding.** ATL feeds every AU with its content pts as soon as the app queues it (up to ~28 frames ahead).
- **Presenting.** The pipeline presents frames by pts on its own clock.
- **Sync.** `sf.c` maps pts onto the pipeline's timeline and keeps the pipeline's clock on ExoPlayer's due times:
  - **Play** is held until the first due time and issued so that this frame lands on it.
  - **Measuring the offset.** Each due time is compared with when the pipeline shows that frame. The pipeline's
    clock comes from FRAMEREADY (presented pts plus arrival time; the smallest offset of the last 16 is used).
  - **Correcting.** Offsets above 5 ms are removed by shifting the pts of AUs not fed yet, by at most 1/8 frame per
    AU. A steady direction is treated as clock drift and fed in continuously.
  - **Never late.** An AU is never mapped closer than 150 ms to the pipeline's clock. Late frames stay late on
    Starfish, according to the Native App Kit's notes.
  - **Pause.** When the app stops releasing frames (pause, buffering), the pipeline is paused before it gets more than
    one frame past the last released frame. The next due time restarts it in step.
  - **Seek.** Nothing is flushed or reloaded. The new content is spliced 40 ms after what was fed. The clock holds when
    it reaches that point, and the first due time after the seek restarts it.
- **Calling the library.** This follows the Native App Kit's `starfish_backend.cpp` (the code CSWeb plays through),
  ported to C:
  - mangled entry points, and the object constructed in place in a 64 KiB block that is never freed;
  - Feed's `std::string` reply through sret, `getMaxVideoResolution` with a `std::string` by reference;
  - Unload, then UnloadCompleted (type 23), then the destructor.
- **Window.** `windowId` is the ATL window that xdgshim exports (`ATLVDEC_WINID_FILE`). Placement goes through the
  existing `SDL_webOSSetExportedWindow` interposer to xdgshim. Without an exported window the helper uses NDL.
- **Compatibility.**
  - An old helper reads only the first 12 bytes of the new OPEN and replies "ok", so ATL keeps release-time mode.
  - An old ATL sends a 12-byte OPEN and gets NDL.
  - For VP9, ATL first asks with `AVD_HELLO`. An old helper doesn't answer, and ATL uses FFmpeg after 300 ms.

**Checked without a TV.**
- `sftest.py`, with the stub pipeline `sf_stub.c`:
  - 720 frames at 23.976 fps with B-frame reordering, ExoPlayer due times with 5 ms jitter on a clock running 0.2 %
    off, a 2 s pause and a seek;
  - result: |error| p50 2 ms, p95 6.5 ms, no frames dropped, 1 frame ahead during the pause;
  - also passes with no frame rate declared, with 10 ms jitter, and with -0.2 % drift.
- The TV build (armel, glibc ≤ 2.34) in the twin, with the TV's own `libplayerAPIs.so.1`:
  - every entry point resolves;
  - getMaxVideoResolution reports H.264 4096x2176@30, H.265 / VP9 / AV1 4096x2176@60;
  - Load then stops at `LSRegisterPubPriv FAILED`, as NDL does without the luna role. The test binary wasn't at
    the role's exe path, and there is no media server in the container.

**To check on the TV.**
- Picture on the exported ATL window.
- Whether a video-only pipeline without low-delay mode runs its clock (`ATLVDEC_SF_LOWDELAY=1` /
  `ATLVDEC_SF_AUDIOSYNC=1` change the Load flags).
- The sync error and holds in `vdec.log`: the `starfish: ... s:` lines every 10 s.
- Display-latency trim (`ATLVDEC_SF_SYNC_MS`).
- HDR10 with `setHdrInfo`.
- VP9.
- `ATLVDEC_BACKEND=ndl` goes back to the old path.
