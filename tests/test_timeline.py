import json
import os
import subprocess
import sys
import tempfile
import unittest

from vmkit import timeline as T

MS = 1_000_000
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def frames(ts_ms, req_lag_ms=5):
    return [{"n": i, "t_req": int((t - req_lag_ms) * MS), "t": int(t * MS), "inactive": False}
            for i, t in enumerate(ts_ms)]


class Offsets(unittest.TestCase):
    def test_units_and_signs(self):
        self.assertEqual(T.parse_offset("+100ms"), 100 * MS)
        self.assertEqual(T.parse_offset("-50ms"), -50 * MS)
        self.assertEqual(T.parse_offset("1.5s"), 1_500_000_000)
        self.assertEqual(T.parse_offset("+250us"), 250_000)
        self.assertEqual(T.parse_offset("7ns"), 7)
        self.assertEqual(T.parse_offset("300"), 300 * MS)     # bare number = ms

    def test_bad(self):
        for s in ["", "abc", "+ms", "1min", "++1ms"]:
            with self.assertRaises(T.TimelineError):
                T.parse_offset(s)

    def test_at(self):
        self.assertEqual(T.parse_at("+300ms"), ("point", 300 * MS))
        self.assertEqual(T.parse_at("+50ms..+300ms"), ("window", 50 * MS, 300 * MS))
        self.assertEqual(T.parse_at("-100ms..+0ms"), ("window", -100 * MS, 0))
        with self.assertRaises(T.TimelineError):
            T.parse_at("+300ms..+50ms")
        self.assertEqual(T.split_specs("+0ms, +100ms,,+0ms..+400ms"), ["+0ms", "+100ms", "+0ms..+400ms"])


class Selection(unittest.TestCase):
    F = frames([1000, 1033, 1066, 1100, 1133, 1166, 1200, 1500, 1533])

    def test_point_is_first_frame_at_or_after(self):
        self.assertEqual(T.select_point(self.F, 1066 * MS)["n"], 2)       # exactly at
        self.assertEqual(T.select_point(self.F, 1067 * MS)["n"], 3)       # just after
        self.assertEqual(T.select_point(self.F, 1201 * MS)["n"], 7)       # across a gap

    def test_point_outside_recording_is_error(self):
        with self.assertRaises(T.TimelineError):
            T.select_point(self.F, 900 * MS)          # before recording started
        with self.assertRaises(T.TimelineError):
            T.select_point(self.F, 1534 * MS)         # after the last frame

    def test_window_inclusive(self):
        got = [f["n"] for f in T.select_window(self.F, 1033 * MS, 1100 * MS)]
        self.assertEqual(got, [1, 2, 3])

    def test_empty_window_is_error(self):
        with self.assertRaises(T.TimelineError) as cm:
            T.select_window(self.F, 1250 * MS, 1400 * MS)
        self.assertIn("no frame in the window", str(cm.exception))

    def test_window_past_recording_is_error(self):
        with self.assertRaises(T.TimelineError):
            T.select_window(self.F, 1500 * MS, 1600 * MS)

    def test_select_with_anchor(self):
        anchor = 1000 * MS
        self.assertEqual([f["n"] for f in T.select(self.F, anchor, "+100ms")], [3])
        self.assertEqual([f["n"] for f in T.select(self.F, anchor, "+0ms..+100ms")], [0, 1, 2, 3])
        self.assertEqual([f["n"] for f in T.select(self.F, 1100 * MS, "-40ms..+0ms")], [2, 3])

    def test_baseline_is_last_frame_before_anchor(self):
        self.assertEqual(T.baseline(self.F, 1100 * MS)["n"], 2)
        self.assertEqual(T.baseline(self.F, 1101 * MS)["n"], 3)
        with self.assertRaises(T.TimelineError):
            T.baseline(self.F, 1000 * MS)

    def test_empty_recording(self):
        with self.assertRaises(T.TimelineError):
            T.select_point([], 0)


class Actions(unittest.TestCase):
    A = [{"cmd": "keys", "t_send": 10, "t_ack": 20}, {"cmd": "type", "step": "typing", "t_send": 30, "t_ack": 40}]

    def test_find(self):
        self.assertEqual(T.find_action(self.A, "#0")["cmd"], "keys")
        self.assertEqual(T.find_action(self.A, "#-1")["cmd"], "type")
        self.assertEqual(T.find_action(self.A, 0)["cmd"], "keys")       # a JSON int is an index
        self.assertEqual(T.find_action(self.A, -1)["cmd"], "type")
        self.assertEqual(T.find_action(self.A, "typing")["cmd"], "type")
        for bad in ("#5", 5, "nope", "#x", "0"):                         # "0": no step labelled "0"
            with self.assertRaises(T.TimelineError, msg=bad):
                T.find_action(self.A, bad)

    def test_numeric_step_label(self):
        A = [{"cmd": "keys", "step": "1"}, {"cmd": "type", "step": "2"}, {"cmd": "click"}]
        self.assertEqual(T.find_action(A, "2")["cmd"], "type")          # the label, not index 2
        self.assertEqual(T.find_action(A, "#2")["cmd"], "click")        # the index

    def test_repeated_label_is_ambiguous(self):
        A = [{"cmd": "keys", "step": "go"}, {"cmd": "type", "step": "go"}]
        with self.assertRaises(T.TimelineError) as cm:
            T.find_action(A, "go")
        self.assertIn("ambiguous", str(cm.exception))

    def test_anchor(self):
        self.assertEqual(T.anchor_of(self.A[1], "send"), 30)
        self.assertEqual(T.anchor_of(self.A[1], "ack"), 40)
        with self.assertRaises(T.TimelineError):
            T.anchor_of({"cmd": "shot"})


class CLI(unittest.TestCase):
    def test_frames_cli(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "frames.jsonl"), "w") as f:
                for fr in frames([1000, 1033, 1066, 1100, 1133]):
                    f.write(json.dumps(fr) + "\n")
            with open(os.path.join(d, "actions.jsonl"), "w") as f:
                f.write(json.dumps({"cmd": "keys", "t_send": 1010 * MS, "t_ack": 1020 * MS}) + "\n")
            cli = [sys.executable, os.path.join(ROOT, "lib", "vmkit", "timeline.py"), "--run", d]
            r = subprocess.run(cli + ["--action", "#0", "--anchor", "ack", "--offset", "+0ms..+50ms",
                                      "--baseline", "--json"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            rows = [json.loads(x) for x in r.stdout.splitlines()]
            self.assertEqual([(x["spec"], x["n"]) for x in rows],
                             [("baseline", 0), ("+0ms..+50ms", 1), ("+0ms..+50ms", 2)])
            self.assertTrue(rows[1]["path"].endswith("frames/000001.png"))
            r = subprocess.run(cli + ["--at", str(1100 * MS), "--offset", "+5ms..+20ms"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 2)
            self.assertIn("no frame in the window", r.stderr)


if __name__ == "__main__":
    unittest.main()
