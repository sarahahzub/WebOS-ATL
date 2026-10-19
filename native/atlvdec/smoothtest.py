#!/usr/bin/env python3
"""Bench test for atlvdec frame smoothing (stub NDL build, no TV): plays a fake 24 fps stream whose due times are what
ATL's ExoPlayer sends on the TV (snapped to a 60 Hz grid that is not the panel's, plus audio-clock jitter), and
measures when atlvdec calls NDL_DirectVideoPlay. Prints the feed-gap spread with ATLVDEC_SMOOTH=0 and =1.
usage: smoothtest.py <atlvdec-stub>"""
import os, random, socket, struct, subprocess, sys, threading, time

STUB = sys.argv[1]
P = 1e9 / 24          # frame period, ns
V = 1e9 / 60          # ATL's fake vsync grid
N = 360


def run(smooth):
    sock = "/tmp/avd-smoothtest-%d" % os.getpid()
    env = dict(os.environ, ATLVDEC_SMOOTH=smooth, ATLVDEC_EARLY="0", ATLVDEC_SDL="0")
    pr = subprocess.Popen([STUB, sock], env=env, stderr=subprocess.PIPE, text=True)
    plays = []

    def reader():
        for line in pr.stderr:
            if "Play(#" in line:
                plays.append(time.monotonic_ns())
    threading.Thread(target=reader, daemon=True).start()
    for _ in range(100):
        if os.path.exists(sock):
            break
        time.sleep(0.02)
    s = socket.socket(socket.AF_UNIX)
    s.connect(sock)
    s.sendall(struct.pack("<IIq", 1, 12, 0) + struct.pack("<III", 1, 1280, 550))
    s.recv(4096)
    rnd = random.Random(7)
    t0 = time.monotonic_ns() + 300_000_000
    grid_phase = 5_300_000
    for i in range(N):
        exact = t0 + i * P + rnd.gauss(0, 6e6)                     # audio-clock jitter
        due = int(round((exact - grid_phase) / V) * V + grid_phase)  # snapped to the fake vsync grid
        send_at = due - rnd.uniform(15e6, 45e6)                      # ExoPlayer releases 15-45 ms early
        w = (send_at - time.monotonic_ns()) / 1e9
        if w > 0:
            time.sleep(w)
        s.sendall(struct.pack("<IIq", 6, 8, 0) + struct.pack("<q", due))
        s.sendall(struct.pack("<IIq", 2, 6, i * 41667) + b"\0\0\0\1\x65\x88")
    time.sleep(0.5)
    s.close()
    pr.terminate()
    pr.wait()
    gaps = [(b - a) / 1e6 for a, b in zip(plays[20:], plays[21:])]
    gaps.sort()
    q = lambda f: gaps[int(f * (len(gaps) - 1))]
    over = sum(1 for g in gaps if g > 50.5) / len(gaps) * 100
    print("ATLVDEC_SMOOTH=%s: %d feeds, gap p5 %.1f  median %.1f  p95 %.1f  max %.1f ms, %.0f%% gaps > 50.5 ms"
          % (smooth, len(plays), q(0.05), q(0.5), q(0.95), gaps[-1], over))
    return q(0.95) - q(0.05)


a = run("0")
b = run("1")
print("spread p95-p5: raw %.1f ms -> smoothed %.1f ms" % (a, b))
sys.exit(0 if b < a / 2 else 1)
