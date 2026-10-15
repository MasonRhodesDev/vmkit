import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from vmkit import act

from .fakeqmp import FakeQMP


class ActQMP(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="vmkit-act-")
        self.sock = os.path.join(self.td, "qmp.sock")
        self.srv = FakeQMP(self.sock, size=(101, 51))

    def tearDown(self):
        self.srv.stop()
        shutil.rmtree(self.td, ignore_errors=True)

    def events(self):
        return [a["events"] for ex, a in self.srv.requests if ex == "input-send-event"]

    def test_tap_text_moves_and_clicks(self):
        rec, msg = act.run_qmp("tap-text", ["50", "25", "left"], self.sock, self.td)
        self.assertTrue(rec["ok"], rec.get("error"))
        self.assertEqual((rec["x"], rec["y"]), (50, 25))
        ev = self.events()
        # abs move to the centre of a 101x51 frame, then press + release
        self.assertEqual([e["data"]["value"] for e in ev[0]], [16384, 16384])
        self.assertEqual([e["data"]["down"] for e in ev[1] + ev[2]], [True, False])

    def test_tap_moves_and_clicks(self):
        rec, _ = act.run_qmp("tap", ["100", "0"], self.sock, self.td)
        self.assertTrue(rec["ok"], rec.get("error"))
        self.assertEqual([e["data"]["value"] for e in self.events()[0]], [32767, 0])

    def test_type_paces_its_chords(self):
        # One send-key per character, in order, with TYPE_GAP between them
        # (back-to-back chords lost a shift release in the guest).
        with mock.patch.object(act.time, "sleep") as sleep:
            rec, msg = act.run_qmp("type", ["Hi", "1"], self.sock, self.td)
        self.assertTrue(rec["ok"], rec.get("error"))
        sent = [[k["data"] for k in a["keys"]] for ex, a in self.srv.requests if ex == "send-key"]
        self.assertEqual(sent, [["shift", "h"], ["i"], ["spc"], ["1"]])
        self.assertEqual([c.args for c in sleep.call_args_list], [(act.TYPE_GAP,)] * 3)

    def main_logged(self, *argv):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = act.main(["--dir", self.td, "--tmp", self.td] + list(argv))
        with open(os.path.join(self.td, "actions.jsonl")) as f:
            recs = [json.loads(x) for x in f]
        return rc, err.getvalue(), recs

    def test_vm_down_is_a_logged_failed_action(self):
        rc, err, recs = self.main_logged("--sock", os.path.join(self.td, "nope.sock"), "keys", "a")
        self.assertEqual(rc, 1)
        self.assertIn("cannot connect", err)
        self.assertEqual([(r["cmd"], r["ok"]) for r in recs], [("keys", False)])
        self.assertIn("t_send", recs[0])

    def test_failed_size_screendump_is_a_logged_failed_action(self):
        self.srv.handlers["screendump"] = lambda a: {"error": {"class": "GenericError", "desc": "no surface"}}
        rc, err, recs = self.main_logged("--sock", self.sock, "tap", "1", "2")
        self.assertEqual(rc, 1)
        self.assertEqual([(r["cmd"], r["ok"]) for r in recs], [("tap", False)])
        self.assertIn("no surface", recs[0]["error"])


if __name__ == "__main__":
    unittest.main()
