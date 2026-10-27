#!/usr/bin/env python3
"""Bench test for atlvdec's Starfish backend (sf.c) against the stub pipeline (sf_stub.c), no TV needed.

  python3 native/atlvdec/sftest.py [--keep] [--timeline]     (builds atlvdec-sfstub with the host gcc)
  env SFTEST_DRIFT (app/pipeline clock ratio, 1.002), SFTEST_JITTER_MS (5), SFTEST_FPS_MILLI (23976; 0 = not declared)

Plays the part of ATL + ExoPlayer: OPEN (protocol 2, ahead), AUs fed ahead of playback in decode order with B-frame
reordering, AVD_DUE (due time + pts) ~30 ms before each frame's due time, with the app's clock running 0.2 % off the
pipeline's and 5 ms of jitter, a 2 s pause, a seek, CLOSE. Also checks HELLO and that a protocol-1 OPEN still goes
to NDL. The error is measured against the app's jitter-free timeline (smooth presentation in step with its audio):
pass = p95 within 15 ms, outside the first second after start / resume / seek; none dropped; at most 3 frames run
ahead during the pause."""
import os
import random
import socket
import struct
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
AVD_OPEN, AVD_FEED, AVD_AREA, AVD_FLUSH, AVD_CLOSE, AVD_DUE, AVD_HELLO, AVD_RESULT = 1, 2, 3, 4, 5, 6, 8, 16
FRAME_NS = 1001 * 1000000000 // 24000  # 23.976 fps
DRIFT = float(os.environ.get("SFTEST_DRIFT", "1.002"))  # 2 ms per second: ~20x what two real crystals drift
JITTER_NS = float(os.environ.get("SFTEST_JITTER_MS", "5")) * 1e6
FPS_MILLI = int(os.environ.get("SFTEST_FPS_MILLI", "23976"))  # 0: the app does not say (estimated from its due times)


def build(out):
    cmd = ["gcc", "-O2", "-Wall", "-Wextra", "-Wno-unused-parameter", "-Werror", "-DNDL_STUB", "-DSF_STUB", "-rdynamic", "-o", out,
           os.path.join(HERE, "atlvdec.c"), os.path.join(HERE, "sf.c"), os.path.join(HERE, "ndl_stub.c"),
           os.path.join(HERE, "sf_stub.c"), "-ldl", "-lpthread"]
    subprocess.check_call(cmd)


def send(s, typ, payload=b"", pts=0):
    s.sendall(struct.pack("<IIq", typ, len(payload), pts) + payload)


def result(s):
    s.settimeout(5)
    d = b""
    while len(d) < 16 + 64:
        c = s.recv(16 + 64 - len(d))
        if not c:
            raise RuntimeError("helper closed the connection")
        d += c
    typ, ln, _ = struct.unpack("<IIq", d[:16])
    st = struct.unpack("<i", d[16:20])[0]
    return typ, st, d[20:80].split(b"\0")[0].decode()


def open_v2(codec=1, w=1920, h=1080, fps_milli=23976, ahead=True, transfer=1, hdr=False):
    o = struct.pack("<IIIII", codec, w, h, 1 if ahead else 0, fps_milli)
    o += struct.pack("<BBBBBBBB", transfer, 9 if hdr else 1, 9 if hdr else 1, 0, 1 if hdr else 0, 1 if hdr else 0, 0, 0)
    o += struct.pack("<6H", 35400, 14600, 8500, 39850, 6550, 2300) + struct.pack("<2H", 15635, 16450)
    o += struct.pack("<IIHH", 1000, 50, 1000, 400)
    assert len(o) == 56
    return o


def au(pts_us):
    return b"\x00\x00\x00\x01\x65" + pts_us.to_bytes(8, "little") + b"\x00" * 200


def connect(path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    for _ in range(100):
        try:
            s.connect(path)
            return s
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("helper did not listen")


def scenario(s, log):
    """returns {pts_us: due_ns} and the windows to exclude, plus pause info"""
    rnd = random.Random(7)
    dues, exclude = {}, []
    order = []  # decode order: I P B B per group of 4
    n = 720
    for g in range(0, n, 4):
        order += [g, g + 3, g + 1, g + 2]
    pts_of = lambda k, base: base + k * FRAME_NS // 1000
    base = 3_000_000  # content starts at 3 s (the pipeline timeline starts at 0.1 s)
    fed = 0
    t0 = time.monotonic_ns() + 400_000_000
    exclude.append((t0, t0 + 1_000_000_000))
    pause_at, pause_ns = 240, 2_000_000_000
    seek_at = 480
    seek_base = 100_000_000
    shift_ns = 0  # app clock offset added by the pause / seek
    pause_win = None
    k = 0
    while k < n:
        # feed ahead (decode order), up to 24 frames past the next release
        while fed < len(order) and order[fed] <= k + 24 and (k >= seek_at or order[fed] < seek_at):
            idx = order[fed]
            send(s, AVD_FEED, au(pts_of(idx, base if idx < seek_at else seek_base)), pts_of(idx, base if idx < seek_at else seek_base))
            fed += 1
        ideal = t0 + shift_ns + int(k * FRAME_NS * DRIFT)  # the app's own clock (audio position)
        due = ideal + int(max(-2.4 * JITTER_NS, min(2.4 * JITTER_NS, rnd.gauss(0, JITTER_NS))))  # what it sends (60 Hz snap, audio jitter)
        rel = due - 30_000_000
        while time.monotonic_ns() < rel:
            time.sleep(0.001)
        pts = pts_of(k, base if k < seek_at else seek_base)
        send(s, AVD_DUE, struct.pack("<q", due), pts)
        dues[pts] = ideal
        k += 1
        if k == pause_at:
            last = pts
            p0 = time.monotonic_ns()
            time.sleep(pause_ns / 1e9)
            shift_ns += time.monotonic_ns() - p0
            pause_win = (p0, p0 + pause_ns, last)
            exclude.append((p0, p0 + pause_ns + 1_000_000_000))
        if k == seek_at:
            send(s, AVD_FLUSH)
            p0 = time.monotonic_ns()
            # the app rebuffers 300 ms, then plays the new position
            while fed < len(order) and order[fed] <= k + 24:
                idx = order[fed]
                send(s, AVD_FEED, au(pts_of(idx, seek_base)), pts_of(idx, seek_base))
                fed += 1
            time.sleep(0.3)
            shift_ns += time.monotonic_ns() - p0 + 300_000_000
            exclude.append((p0, p0 + 1_600_000_000))
    time.sleep(0.5)
    send(s, AVD_CLOSE)
    return dues, exclude, pause_win


def main():
    keep = "--keep" in sys.argv
    tmp = tempfile.mkdtemp(prefix="sftest-")
    exe = os.path.join(tmp, "atlvdec-sfstub")
    build(exe)
    sock, logf, winf = os.path.join(tmp, "vdec.sock"), os.path.join(tmp, "present.log"), os.path.join(tmp, "winid")
    open(winf, "w").write("stub_window_1\n")
    env = dict(os.environ, APPID="org.webosatl.app.test", ATLVDEC_WINID_FILE=winf, ATLVDEC_SDL="0", ATLVDEC_EARLY="0",
               SFSTUB_LOG=logf, ATLVDEC_LINGER="0")
    err = open(os.path.join(tmp, "atlvdec.log"), "w")
    p = subprocess.Popen([exe, sock], env=env, stderr=err)
    ok = True
    try:
        s = connect(sock)
        send(s, AVD_HELLO)
        typ, st, msg = result(s)
        print("HELLO ->", st, msg)
        ok &= st == 0 and msg.startswith("atlvdec 2 starfish") and "vp9" in msg
        send(s, AVD_OPEN, open_v2(fps_milli=FPS_MILLI, transfer=16, hdr=True))
        typ, st, msg = result(s)
        print("OPEN v2 ->", st, msg)
        ok &= st == 0 and "ahead" in msg
        send(s, AVD_AREA, struct.pack("<4i", 0, 0, 1920, 1080))
        dues, exclude, pause_win = scenario(s, logf)
        s.close()
        # a protocol-1 client (12-byte OPEN) still gets NDL in release-time mode
        s = connect(sock)
        send(s, AVD_OPEN, struct.pack("<III", 1, 1920, 1080))
        typ, st, msg = result(s)
        print("OPEN v1 ->", st, msg)
        ok &= st == 0 and "ahead" not in msg
        s.close()
        time.sleep(0.5)
    finally:
        p.terminate()
        p.wait(5)
        err.close()
    pres = []
    for line in open(logf):
        c, pts, at = line.split()
        pres.append((c, int(pts), int(at)))
    errs, drops, ahead = [], 0, 0
    for c, pts, at in pres:
        if c == "D":
            drops += 1
            continue
        if pts not in dues:
            continue
        if pause_win and pause_win[0] < at < pause_win[1] and pts > pause_win[2] + 2 * FRAME_NS // 1000:
            ahead += 1
        if any(a <= at <= b for a, b in exclude):
            continue
        errs.append((at - dues[pts]) / 1e6)
    if "--timeline" in sys.argv:  # mean / max error per second of playback (all frames, nothing excluded)
        t_first = min(at for c, pts, at in pres)
        buckets = {}
        for c, pts, at in pres:
            if c == "P" and pts in dues:
                buckets.setdefault((at - t_first) // 1_000_000_000, []).append((at - dues[pts]) / 1e6)
        print("  t(s)  n   mean    max|e|")
        for b in sorted(buckets):
            v = buckets[b]
            print("  %3d  %3d  %+6.1f  %6.1f" % (b, len(v), sum(v) / len(v), max(abs(x) for x in v)))
    errs_abs = sorted(abs(e) for e in errs)
    p50 = errs_abs[len(errs_abs) // 2] if errs_abs else 99
    p95 = errs_abs[int(len(errs_abs) * 0.95)] if errs_abs else 99
    mean = sum(errs) / len(errs) if errs else 99
    print("presented %d frames (%d measured): |error| p50 %.1f ms p95 %.1f ms, mean %+.1f ms; dropped %d; ran ahead in pause %d"
          % (len(pres) - drops, len(errs), p50, p95, mean, drops, ahead))
    sflog = [l.strip() for l in open(os.path.join(tmp, "atlvdec.log")) if "starfish:" in l]
    for l in sflog:
        if any(w in l for w in ("Play", "Pause", "splice", "s: ", "setHdrInfo", "Load ->", "Unload")):
            print("   ", l[:200])
    ok &= len(errs) > 400 and p95 < 15 and drops == 0 and ahead <= 3
    print("PASS" if ok else "FAIL", "(logs in %s)" % tmp if keep or not ok else "")
    if ok and not keep:
        import shutil
        shutil.rmtree(tmp)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
