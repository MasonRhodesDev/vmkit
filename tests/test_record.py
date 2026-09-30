import argparse
import json
import os
import queue
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from vmkit import record
from vmkit.record import is_placeholder, pct

from .fakeqmp import FakeQMP

MSG = "Display output is not active."


def frame(w, h, text_cells=None, fg=b"\xaa\xaa\xaa", row=None, col=None, noise=None):
    """Black RGB frame with 'glyph' pixels in the text row, like QEMU's placeholder."""
    buf = bytearray(w * h * 3)
    n = len(MSG) if text_cells is None else text_cells
    r = ((h // 16 - 1) // 2) if row is None else row
    c = ((w // 8 - n) // 2) if col is None else col
    for y in range(r * 16 + 3, r * 16 + 13):
        for x in range(c * 8 + 1, (c + n) * 8 - 1):
            if (x + y) % 3:
                buf[(y * w + x) * 3:(y * w + x) * 3 + 3] = fg
    if noise:
        x, y = noise
        buf[(y * w + x) * 3] = 7
    return b"P6\n%d %d\n255\n" % (w, h) + bytes(buf)


def check(buf):
    hdr_end = buf.index(b"255\n") + 4
    w, h = map(int, buf.split(b"\n")[1].split())
    return is_placeholder(buf, hdr_end, w, h)


class Placeholder(unittest.TestCase):
    def test_detects_placeholder_various_sizes(self):
        for w, h in [(640, 480), (1280, 800), (1024, 768), (1920, 1080)]:
            self.assertTrue(check(frame(w, h)), (w, h))

    def test_all_black_is_not_placeholder(self):
        self.assertFalse(check(frame(640, 480, text_cells=0)))

    def test_other_colour_text_is_not(self):
        self.assertFalse(check(frame(640, 480, fg=b"\xff\xff\xff")))

    def test_off_centre_text_is_not(self):
        self.assertFalse(check(frame(640, 480, col=2)))

    def test_text_on_another_row_is_not(self):
        self.assertFalse(check(frame(640, 480, row=3)))

    def test_one_stray_pixel_outside_band_is_not(self):
        self.assertFalse(check(frame(640, 480, noise=(5, 5))))

    def test_tiny_frames(self):
        self.assertFalse(is_placeholder(b"\0" * 12, 0, 2, 2))


class Percentile(unittest.TestCase):
    def test_pct(self):
        v = sorted([10, 20, 30, 40, 50])
        self.assertEqual(pct(v, 50), 30)
        self.assertEqual(pct(v, 100), 50)
        self.assertAlmostEqual(pct(v, 95), 48)
        self.assertIsNone(pct([], 50))


class CaptureErrors(unittest.TestCase):
    """Recorder.capture() against a fake QMP server (no ffmpeg: the queue is drained here)."""

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="vmkit-rec-")
        self.run = os.path.join(self.td, "run")
        os.makedirs(self.run)
        self.srv = FakeQMP(os.path.join(self.td, "qmp-rec.sock"))
        self.calls = 0

    def tearDown(self):
        self.srv.stop()
        shutil.rmtree(self.td, ignore_errors=True)

    def recorder(self, **kw):
        a = argparse.Namespace(sock=self.srv.path, run=self.run, fps=100.0, shm=self.td, ffmpeg="ffmpeg",
                               degraded_ms=1000.0, queue=16, qmp_timeout=5.0, max_errors=30)
        for k, v in kw.items():
            setattr(a, k, v)
        return record.Recorder(a)

    def capture(self, R, seconds):
        def drain():
            while True:
                try:
                    if R.q.get(timeout=0.1) is None:
                        return
                except queue.Empty:
                    if done.is_set():
                        return
        done = threading.Event()
        dt = threading.Thread(target=drain, daemon=True); dt.start()
        ct = threading.Thread(target=R.capture, daemon=True); ct.start()
        ct.join(seconds)
        R.stop.set()
        ct.join(5)
        done.set()
        self.assertFalse(ct.is_alive(), "capture did not stop")

    def dumps(self, fail):
        """screendump fails (error reply) on the calls for which fail(i) is true (i from 1)."""
        def h(args):
            self.calls += 1
            if fail(self.calls):
                return {"error": {"class": "GenericError", "desc": "no surface"}}
            return self.srv.screendump(args)
        self.srv.handlers["screendump"] = h

    def test_transient_error_is_a_gap_not_the_end(self):
        self.dumps(lambda i: i == 3)
        R = self.recorder()
        self.capture(R, 0.5)
        self.assertIsNone(R.error)
        self.assertGreater(len(R.ts), 5)
        S = R.summary()
        self.assertEqual(S["screendump_errors"], 1)
        self.assertTrue(S["degraded"])

    def gaps(self):
        with open(os.path.join(self.run, "gaps.jsonl")) as f:
            return [json.loads(x) for x in f]

    def frames(self):
        with open(os.path.join(self.run, "frames.jsonl")) as f:
            return [json.loads(x) for x in f]

    def test_error_ticks_are_recorded_as_one_gap(self):
        self.dumps(lambda i: i in (3, 4, 5))
        R = self.recorder()
        self.capture(R, 0.5)
        G, F = self.gaps(), self.frames()
        self.assertEqual(len(G), 1, G)
        g = G[0]
        self.assertEqual(g["errors"], 3)
        self.assertIn("no surface", g["reason"])
        self.assertLess(F[1]["t"], g["t_from"])          # after frame 1's reply ...
        self.assertLessEqual(g["t_to"], F[2]["t_req"])   # ... up to frame 2's request
        self.assertEqual([f["n"] for f in F], list(range(len(F))))   # n is not consumed

    def test_a_gap_open_at_stop_is_recorded(self):
        self.dumps(lambda i: i > 2)
        R = self.recorder()
        self.capture(R, 0.3)
        G, F = self.gaps(), self.frames()
        self.assertEqual(len(G), 1, G)
        self.assertLess(F[-1]["t"], G[0]["t_from"])
        self.assertLess(G[0]["t_from"], G[0]["t_to"])

    def test_gives_up_after_consecutive_errors(self):
        self.dumps(lambda i: i > 2)
        R = self.recorder(max_errors=5)
        self.capture(R, 3)
        self.assertIn("5 errors in a row", R.error or "")
        self.assertEqual(len(R.ts), 2)

    def test_stop_interrupts_a_hung_screendump(self):
        n = {"i": 0}

        def h(args):         # QEMU stops answering after a few frames (qmp_timeout is long)
            n["i"] += 1
            return self.srv.screendump(args) if n["i"] <= 3 else None
        self.srv.handlers["screendump"] = h
        R = self.recorder(qmp_timeout=60.0)
        ct = threading.Thread(target=R.capture, daemon=True); ct.start()
        time.sleep(0.5)
        R.stop.set()
        ct.join(2)
        self.assertFalse(ct.is_alive(), "stop did not interrupt the screendump read")
        self.assertIsNone(R.error)
        self.assertEqual(len(R.ts), 3)

    def test_eof_is_fatal(self):
        self.srv.handlers["screendump"] = lambda args: (lambda f: f.close())   # QEMU went away
        R = self.recorder()
        self.capture(R, 3)
        self.assertIn("connection closed", R.error or "")

    def summary_after(self, screendump):
        self.srv.handlers["screendump"] = screendump
        R = self.recorder()
        t = threading.Thread(target=lambda: setattr(R, "rc", R.main()), daemon=True)
        t.start(); t.join(5)
        R.stop.set(); t.join(5)
        self.assertFalse(t.is_alive(), "recorder did not stop")
        self.assertEqual(R.rc, 1)
        with open(os.path.join(self.run, "summary.json")) as f:
            return json.load(f)

    def test_summary_written_on_a_bad_ppm(self):
        def garbage(args):
            with open(args["filename"], "wb") as f:
                f.write(b"GIF89a not a ppm")
            return {"return": {}}
        S = self.summary_after(garbage)
        self.assertIn("not a binary PPM", S["error"])
        self.assertTrue(S["degraded"])

    def test_summary_written_when_the_ppm_is_missing(self):
        S = self.summary_after(lambda args: {"return": {}})    # replies ok, writes nothing
        self.assertIn("No such file", S["error"])

    def test_default_qmp_timeout_outlasts_savevm_loadvm(self):
        with mock.patch.object(record, "Recorder") as Rec, mock.patch("signal.signal"):
            record.main(["--sock", self.srv.path, "--run", self.run])
        self.assertGreaterEqual(Rec.call_args[0][0].qmp_timeout, 600)


if __name__ == "__main__":
    unittest.main()
