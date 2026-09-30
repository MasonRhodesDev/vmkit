import os
import shutil
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
