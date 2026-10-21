"""vmkit recorder: capture the guest framebuffer at a steady rate, stamped on the
host's CLOCK_MONOTONIC, into a lossless PNG sequence plus an ffv1 video.

Run by `vmkit record start` as its own long-lived process (see bin/vmkit-record).

How it works (numbers from the 2026-09-30 recording spike):
  * One persistent connection to the recorder-only QMP socket (qmp-rec.sock;
    a QMP socket serves one client at a time, so holding vmkit's qmp.sock
    would block every other vmkit call).
  * QMP `screendump` format=ppm into /dev/shm, paced on an absolute schedule
    at --fps (default 30). PPM is a memcpy inside QEMU; PNG costs ~30 ms per
    call there and halves the guest's frame rate.
  * Each frame is stamped twice: t_req just before the request, t after the
    reply. The frame's content was captured somewhere in [t_req, t].
  * The raw RGB goes to ONE long-lived ffmpeg child (rawvideo on stdin) that
    writes frames/%06d.png (file number == frame n) and video.mkv (ffv1). The
    PPM is deleted as soon as it is read: 30 fps raw is ~92 MB/s.
  * QEMU's "Display output is not active." placeholder (scanout off: DPMS,
    some lock states) is detected and the frame is marked inactive.
  * A resolution change closes the encoder segment and starts a new one
    (video-001.mkv, ...); PNG numbering continues at n.

Outputs in the run dir:
  frames.jsonl    {"n", "t_req", "t", "inactive"} per frame (ns, CLOCK_MONOTONIC)
  gaps.jsonl      {"t_from", "t_to", "errors", "reason"} per run of screendump
                  errors: no frame exists for [t_from, t_to] (t_from = the first
                  failed request, t_to = the next successful one, or the stop)
  segments.jsonl  {"seg", "first_n", "w", "h", "video"} per encoder segment
  summary.json    fps, interval p50/p95/max, screendump call times, degraded, ...
  recorder.log    gaps over the degraded threshold, errors

A screendump error reply (e.g. no surface during a mode switch) is logged as a
gap, counted in summary.json's screendump_errors, and marks the recording
degraded; the recorder gives up only after --max-error-s seconds (10) of them
in a row (time, not a count: the error reply rate varies), or
when QEMU closes the socket or a reply times out (--qmp-timeout, default 600 s,
so a snapshot/restore during a recording is a long gap, not the end).
  ffmpeg.log      the encoder's stderr

Stop with SIGTERM or SIGINT: the recorder drains the queue, closes ffmpeg,
checks that PNG count == frames.jsonl lines == video packets, and writes
summary.json. Exit status 0 = clean, 1 = the recording hit an error. An
encoder that has not drained --drain-s (10) s after the stop is killed, and
summary.json says encoder_stalled=true, degraded=true (exit 1); that is well
inside record stop's own escalation below, so the recorder still writes it.
`vmkit record stop` SIGKILLs a recorder that outlasts VMKIT_REC_STOP_TIMEOUT
(30 s), with its ffmpeg, and then runs `vmkit-record --run RUN --mark-killed WHY`: the
summary.json it writes (or amends) says killed=true, degraded=true, and the
frames already on disk stay usable.
"""
import argparse
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vmkit.qmp import QMP, QMPClosed, QMPError  # noqa: E402
from vmkit.imageio import _ppm_header  # noqa: E402

now = time.monotonic_ns

FONT_W, FONT_H = 8, 16          # QEMU vgafont16 cell (ui/console.c placeholder)
PLACEHOLDER_FG = 0xAA           # QEMU_PIXMAN_COLOR_GRAY; background is black


def is_placeholder(buf, off, w, h):
    """True if the RGB frame at buf[off:] is QEMU's placeholder surface: black,
    with one line of gray (0xaa) vgafont text on the centered text row.

    QEMU draws it with qemu_create_placeholder_surface(): row
    (h/16 - 1)/2, column (w/8 - len)/2, fg gray, bg black, at the previous
    surface's resolution. Only C-speed bytes operations here (30 fps path)."""
    stride = w * 3
    if w < FONT_W or h < FONT_H:
        return False
    y0 = ((h // FONT_H - 1) // 2) * FONT_H
    a, b, end = off + y0 * stride, off + (y0 + FONT_H) * stride, off + h * stride
    if len(buf) < end:
        return False
    if buf.count(0, off, a) != a - off or buf.count(0, b, end) != end - b:
        return False
    band = buf[a:b]
    if band.translate(None, b"\x00\xaa") or band.count(PLACEHOLDER_FG) == 0:
        return False
    first, last = stride, -1
    for r in range(FONT_H):
        row = band[r * stride:(r + 1) * stride]
        i = row.find(b"\xaa")
        if i >= 0:
            first = min(first, i // 3)
            last = max(last, row.rfind(b"\xaa") // 3)
    # centered text: margins equal within a glyph cell or two
    return abs(first - (w - 1 - last)) <= 2 * FONT_W


def pct(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p / 100.0
    lo = int(k); hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


class Encoder:
    """One ffmpeg child per resolution segment."""

    def __init__(self, run, fps, ffmpeg, log):
        self.run, self.fps, self.ffmpeg, self.log = run, fps, ffmpeg, log
        self.proc = None; self.wh = None; self.seg = -1; self.segments = []
        os.makedirs(os.path.join(run, "frames"), exist_ok=True)

    def start(self, w, h, first_n):
        self.close()
        self.seg += 1
        video = "video.mkv" if self.seg == 0 else f"video-{self.seg:03d}.mkv"
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-framerate", str(self.fps),
               "-i", "pipe:0",
               "-map", "0", "-fps_mode", "passthrough", "-c:v", "png",
               "-start_number", str(first_n), os.path.join(self.run, "frames", "%06d.png"),
               "-map", "0", "-fps_mode", "passthrough", "-c:v", "ffv1",
               os.path.join(self.run, video)]
        err = open(os.path.join(self.run, "ffmpeg.log"), "ab")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err)
        err.close()
        self.wh = (w, h)
        seg = {"seg": self.seg, "first_n": first_n, "w": w, "h": h, "video": video}
        self.segments.append(seg)
        with open(os.path.join(self.run, "segments.jsonl"), "a") as f:
            f.write(json.dumps(seg) + "\n")
        if self.seg:
            self.log(f"resolution change at n={first_n}: {w}x{h}; new encoder segment {video}")

    def write(self, view):
        self.proc.stdin.write(view)       # BufferedWriter loops over partial writes

    def close(self):
        if self.proc is None:
            return
        p = self.proc      # kept until it has exited, so kill() can reach a stalled one
        try:
            p.stdin.close()
        except BrokenPipeError:
            pass
        rc = p.wait()
        self.proc = None
        if rc != 0:
            raise RuntimeError(f"ffmpeg exited {rc} (see ffmpeg.log)")

    def kill(self):
        p = self.proc
        if p is not None:
            p.kill()


class Recorder:
    def __init__(self, a):
        self.a = a
        self.run = a.run
        self.stop = threading.Event()
        self.error = None
        self.q = queue.Queue(maxsize=a.queue)
        self.logf = open(os.path.join(self.run, "recorder.log"), "a", buffering=1)
        self.enc = Encoder(self.run, a.fps, a.ffmpeg, self.log)
        self.ts = []; self.calls = []; self.inactive = 0; self.errors = 0
        self.put_wait_ns = 0; self.max_q = 0
        self.stalled = False; self.drain_by = None

    def log(self, msg):
        self.logf.write(f"{time.strftime('%H:%M:%S')} {msg}\n")

    def fail(self, msg):
        if self.error is None:
            self.error = msg
            self.log("ERROR " + msg)
        self.stop.set()

    def drain_left(self):
        """Seconds left for the encoder to drain, counted from the first time it is asked after stop."""
        if self.drain_by is None:
            self.drain_by = time.monotonic() + self.a.drain_s
        return max(0.0, self.drain_by - time.monotonic())

    def writer(self):
        try:
            while True:
                item = self.q.get()
                if item is None:
                    break
                n, w, h, buf, off = item
                if self.enc.wh != (w, h):
                    self.enc.start(w, h, n)
                self.enc.write(memoryview(buf)[off:off + w * h * 3])
            self.enc.close()
        except Exception as e:   # BrokenPipe, ffmpeg exit, ...
            self.fail(f"encoder: {e}")
            try:                 # unblock the capture loop
                while True:
                    self.q.get_nowait()
            except queue.Empty:
                pass

    def capture(self):
        a = self.a
        period = int(1e9 / a.fps)
        limit_ns = int(a.degraded_ms * 1e6)
        shm = os.path.join(a.shm, f"vmkit-rec.{os.getpid()}.ppm")
        try:
            qmp = QMP(a.sock, timeout=a.qmp_timeout, stop=self.stop)
        except QMPError as e:
            return None if self.stop.is_set() else self.fail(f"qmp: {e}")
        gpu = getattr(a, "gpu", None)
        sel = {"device": gpu, "head": getattr(a, "head", 0)} if gpu else {}    # several heads: record one
        frames = open(os.path.join(self.run, "frames.jsonl"), "a", buffering=1)
        n = 0; t_next = now(); prev_t = None; was_inactive = False; consec = 0
        gap = None      # the open run of errors: {"t_from", "errors", "reason"}

        def end_gap(t_to):
            with open(os.path.join(self.run, "gaps.jsonl"), "a") as g:
                g.write(json.dumps({"t_from": gap["t_from"], "t_to": t_to, "errors": gap["errors"],
                                    "reason": gap["reason"]}) + "\n")
        try:
            while not self.stop.is_set():
                t_req = now()
                try:
                    qmp.cmd("screendump", {"filename": shm, "format": "ppm", **sel})
                except QMPClosed as e:
                    if not self.stop.is_set():
                        self.fail(f"screendump: {e}")
                    break
                except QMPError as e:   # an error reply (e.g. no surface mid mode-switch): a gap
                    if self.stop.is_set():
                        break
                    self.errors += 1; consec += 1
                    gap = gap or {"t_from": t_req, "errors": 0}
                    gap["errors"] += 1; gap["reason"] = f"screendump error: {e}"
                    self.log(f"gap n={n} screendump error ({consec} in a row): {e}")
                    if now() - gap["t_from"] >= a.max_error_s * 1e9:
                        self.fail(f"screendump errors for {a.max_error_s:g} s ({consec} in a row), last: {e}")
                        break
                    self.stop.wait(period / 1e9)
                    t_next = now()
                    continue
                except OSError as e:    # timeout: a late reply would desync the stream
                    if not self.stop.is_set():
                        self.fail(f"screendump: {e}")
                    break
                consec = 0
                if gap:
                    end_gap(t_req); gap = None
                t = now()
                try:
                    with open(shm, "rb") as f:
                        buf = f.read()
                finally:
                    try:
                        os.unlink(shm)
                    except OSError:
                        pass
                w, h, maxval, off = _ppm_header(buf[:64])
                if maxval != 255 or len(buf) < off + w * h * 3:
                    self.fail(f"bad PPM from screendump ({len(buf)} bytes, {w}x{h} max {maxval})")
                    break
                inactive = is_placeholder(buf, off, w, h)
                if inactive != was_inactive:
                    self.log(f"n={n} display output {'inactive (QEMU placeholder)' if inactive else 'active again'}")
                    was_inactive = inactive
                self.inactive += inactive
                frames.write(json.dumps({"n": n, "t_req": t_req, "t": t, "inactive": inactive}) + "\n")
                self.ts.append(t); self.calls.append(t - t_req)
                if prev_t is not None and t - prev_t > limit_ns:
                    self.log(f"gap n={n} interval={(t - prev_t) / 1e6:.1f}ms call={(t - t_req) / 1e6:.1f}ms")
                prev_t = t
                tp = now()
                while self.error is None:   # enqueue even when stopping: frames.jsonl has the line
                    try:
                        self.q.put((n, w, h, buf, off), timeout=0.5)
                        break
                    except queue.Full:
                        if self.error:
                            break
                        if self.stop.is_set() and not self.drain_left():
                            self.stalled = True
                            break
                self.put_wait_ns += now() - tp
                self.max_q = max(self.max_q, self.q.qsize())
                if self.error or self.stalled:
                    break
                n += 1
                # absolute schedule; after a stall, re-anchor instead of bursting
                t_next += period
                late = now() - t_next
                if late > period:
                    t_next = now()
                elif late < 0:
                    self.stop.wait(-late / 1e9)
        finally:
            if gap:
                end_gap(now())
            frames.close()
            qmp.close()
            try:
                os.unlink(shm)
            except OSError:
                pass

    def verify(self, n):
        """PNG count == frames.jsonl lines == video packets (per segment)."""
        out = {"frames": n}
        pngs = sum(1 for f in os.listdir(os.path.join(self.run, "frames")) if f.endswith(".png"))
        out["pngs"] = pngs
        probe = shutil.which("ffprobe")
        packets = None
        if probe:
            packets = 0
            for s in self.enc.segments:
                r = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-count_packets",
                                    "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0",
                                    os.path.join(self.run, s["video"])], capture_output=True, text=True)
                try:
                    packets += int(r.stdout.strip().split()[0])
                except (ValueError, IndexError):
                    packets = None
                    break
        out["video_packets"] = packets
        out["ok"] = pngs == n and (packets is None or packets == n)
        return out

    def summary(self):
        a = self.a
        ts = self.ts
        iv = sorted((ts[i] - ts[i - 1]) / 1e6 for i in range(1, len(ts)))
        calls = sorted(c / 1e6 for c in self.calls)
        dur = (ts[-1] - ts[0]) / 1e9 if len(ts) > 1 else 0.0
        r2 = lambda v: None if v is None else round(v, 2)  # noqa: E731
        S = {
            "frames": len(ts),
            "target_fps": a.fps,
            "duration_s": round(dur, 3),
            "fps": round((len(ts) - 1) / dur, 2) if dur > 0 else None,
            "interval_ms": {"p50": r2(pct(iv, 50)), "p95": r2(pct(iv, 95)), "max": r2(iv[-1] if iv else None)},
            "screendump_call_ms": {"p50": r2(pct(calls, 50)), "p95": r2(pct(calls, 95)),
                                   "max": r2(calls[-1] if calls else None)},
            "degraded_threshold_ms": a.degraded_ms,
            "gaps_over_threshold": sum(1 for v in iv if v > a.degraded_ms),
            "inactive_frames": self.inactive,
            "screendump_errors": self.errors,
            "encoder_backpressure_ms": round(self.put_wait_ns / 1e6, 1),
            "encoder_queue_max": self.max_q,
            "segments": self.enc.segments,
            "nice": os.getpriority(os.PRIO_PROCESS, 0),
        }
        S["encoder_stalled"] = self.stalled
        S["degraded"] = bool((iv and iv[-1] > a.degraded_ms) or self.errors or self.error or self.stalled)
        return S

    def main(self):
        wt = threading.Thread(target=self.writer, name="encoder", daemon=True)
        wt.start()
        try:
            self.capture()
        except Exception as e:   # bad PPM header, shm unreadable, ...: still write summary.json
            self.fail(f"capture: {type(e).__name__}: {e}")
        n = len(self.ts)
        if not self.stalled:
            try:
                self.q.put(None, timeout=self.drain_left())
            except queue.Full:
                self.stalled = True
        if not self.stalled:
            wt.join(self.drain_left())
            self.stalled = wt.is_alive()
        if self.stalled:   # the frames on disk stand; the summary says the rest is missing
            self.fail(f"encoder stalled: not drained {self.a.drain_s:g} s after stop; ffmpeg killed")
            self.enc.kill()
            wt.join(5)
        S = self.summary()
        try:
            S["integrity"] = self.verify(n)
            if not S["integrity"]["ok"]:
                self.fail(f"integrity: {S['integrity']}")
        except OSError as e:
            self.fail(f"verify: {e}")
        S["error"] = self.error
        S["degraded"] = S["degraded"] or bool(self.error)
        with open(os.path.join(self.run, "summary.json"), "w") as f:
            json.dump(S, f, indent=1)
        self.log(f"stopped: {n} frames, fps {S['fps']}, interval max {S['interval_ms']['max']} ms, "
                 f"degraded={S['degraded']}")
        return 1 if self.error else 0


def mark_killed(run, why):
    """Write (or amend) summary.json for a recorder that was SIGKILLed: frames.jsonl
    and the PNGs on disk stand; the video and the integrity check do not."""
    path = os.path.join(run, "summary.json")
    try:
        with open(path) as f:
            S = json.load(f)
    except (OSError, ValueError):
        rows = []
        try:
            with open(os.path.join(run, "frames.jsonl")) as f:
                for line in f:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:      # a line cut short by the kill
                        pass
        except OSError:
            pass
        ts = [r["t"] for r in rows]
        iv = sorted((ts[i] - ts[i - 1]) / 1e6 for i in range(1, len(ts)))
        calls = sorted((r["t"] - r["t_req"]) / 1e6 for r in rows)
        dur = (ts[-1] - ts[0]) / 1e9 if len(ts) > 1 else 0.0
        r2 = lambda v: None if v is None else round(v, 2)  # noqa: E731
        S = {"frames": len(rows), "target_fps": None, "duration_s": round(dur, 3),
             "fps": round((len(ts) - 1) / dur, 2) if dur > 0 else None,
             "interval_ms": {"p50": r2(pct(iv, 50)), "p95": r2(pct(iv, 95)), "max": r2(iv[-1] if iv else None)},
             "screendump_call_ms": {"p50": r2(pct(calls, 50)), "p95": r2(pct(calls, 95)),
                                    "max": r2(calls[-1] if calls else None)},
             "inactive_frames": sum(1 for r in rows if r.get("inactive"))}
    S["killed"] = True
    S["degraded"] = True
    S["error"] = S.get("error") or why
    with open(path + ".tmp", "w") as f:
        json.dump(S, f, indent=1)
    os.replace(path + ".tmp", path)
    return S


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vmkit-record", description=__doc__.split("\n")[0])
    ap.add_argument("--sock", help="recorder QMP socket (qmp-rec.sock); required to record")
    ap.add_argument("--run", required=True, help="run dir (created)")
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--gpu", help="QEMU id of the GPU device, to record one head of several")
    ap.add_argument("--head", type=int, default=0, help="head of --gpu to record")
    ap.add_argument("--shm", default="/dev/shm" if os.access("/dev/shm", os.W_OK) else None,
                    help="scratch dir for the PPM (tmpfs)")
    ap.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg")
    ap.add_argument("--degraded-ms", type=float, default=50.0,
                    help="an interval over this marks the recording degraded")
    ap.add_argument("--queue", type=int, default=16, help="frames buffered for the encoder")
    ap.add_argument("--qmp-timeout", type=float, default=600.0,
                    help="seconds for one screendump reply; a savevm/loadvm on the other socket "
                         "holds QEMU's main loop that long (same default as VMKIT_SNAPSHOT_TIMEOUT)")
    ap.add_argument("--drain-s", type=float, default=10.0,
                    help="after stop, give the encoder this long to drain, then kill it and flag "
                         "encoder_stalled (under record stop's 30 s KILL escalation)")
    ap.add_argument("--max-error-s", type=float, default=10.0,
                    help="give up after this many seconds of screendump error replies in a row")
    ap.add_argument("--mark-killed", metavar="WHY",
                    help="don't record: mark RUN's recording as killed (summary.json) and exit")
    a = ap.parse_args(argv)
    if a.mark_killed:
        mark_killed(a.run, a.mark_killed)
        return 0
    if not a.sock:
        ap.error("--sock is required")
    os.makedirs(a.run, exist_ok=True)
    a.shm = a.shm or a.run
    R = Recorder(a)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: R.stop.set())
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    R.log(f"start pid={os.getpid()} fps={a.fps} sock={a.sock}")
    return R.main()


if __name__ == "__main__":
    sys.exit(main())
