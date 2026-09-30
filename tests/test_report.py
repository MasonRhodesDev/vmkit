import json
import os
import re
import shutil
import tempfile
import unittest
from html.parser import HTMLParser

from vmkit import checkcli as CLI
from vmkit import report as R
from vmkit import timeline as T
from tests.test_checks import paint, solid, write_run


class Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.refs = []; self.rects = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        for k in ("src", "href"):
            if a.get(k) and not a[k].startswith("#"):
                self.refs.append(a[k])
        if tag == "rect":
            self.rects += 1


class Report(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="vmkit-report-")
        base = solid(64, 48, (0, 0, 0))
        box = paint(base, 20, 20, 10, 10, (0, 180, 255))
        ms = 1_000_000
        write_run(self.td, [base] * 4 + [box] * 4,
                  [{"t": "x", "cmd": "keys", "args": "meta_l q", "t_send": 1090 * ms, "t_ack": 1095 * ms, "ok": True},
                   {"t": "x", "cmd": "type", "args": "(3 chars)", "t_send": 1150 * ms, "t_ack": 1160 * ms,
                    "ok": False, "error": "boom"}])
        with open(os.path.join(self.td, "summary.json"), "w") as f:
            json.dump({"frames": 8, "fps": 30.0, "target_fps": 30, "interval_ms": {"p50": 33, "p95": 34, "max": 61},
                       "screendump_call_ms": {"p50": 5, "p95": 6, "max": 9}, "degraded": True,
                       "degraded_threshold_ms": 50, "gaps_over_threshold": 1, "inactive_frames": 0}, f)
        with open(os.path.join(self.td, "segments.jsonl"), "w") as f:
            f.write(json.dumps({"seg": 0, "first_n": 0, "w": 64, "h": 48, "video": "video.mkv"}) + "\n")
        frames, actions = T.load_frames(self.td), T.load_actions(self.td)
        res = [CLI.evaluate(self.td, frames, actions, {"id": "box", "action": 0, "at": "+0ms..+100ms",
                                                        "check": "pixel", "region": [20, 20, 10, 10],
                                                        "color": "#00b4ff"}),
               CLI.evaluate(self.td, frames, actions, {"id": "late", "action": 0, "at": "+1ms..+2ms",
                                                        "check": "pixel", "region": [0, 0, 1, 1], "color": "#000000"})]
        with open(os.path.join(self.td, "results.json"), "w") as f:
            json.dump(res, f)

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_report(self):
        out = R.write_report(self.td)
        page = open(out).read()
        self.assertIn("degraded=true", page)
        self.assertIn("meta_l q", page)
        self.assertIn("action failed: boom", page)
        self.assertIn("no frame in the window", page)
        self.assertRegex(page, r"pass: 1")
        p = Links(); p.feed(page)
        self.assertGreater(p.rects, 0)                     # check regions outlined
        for ref in p.refs:                                 # no external assets; every link resolves
            self.assertFalse(re.match(r"^[a-z]+:", ref), ref)
            if ref != "video.mkv":
                self.assertTrue(os.path.exists(os.path.join(self.td, ref)), ref)
        self.assertNotIn("<script", page)


if __name__ == "__main__":
    unittest.main()
