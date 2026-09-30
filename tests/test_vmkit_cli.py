"""bin/vmkit end to end against a fake QMP server and fake host tools (no VM)."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from .fakeqmp import FakeQMP

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VMKIT = os.path.join(ROOT, "bin", "vmkit")
HAVE_MAGICK = shutil.which("magick") is not None


class VmkitCLI(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="vmkit-cli-")
        self.dir = os.path.join(self.td, "d")
        self.shm = os.path.join(self.td, "shm")
        self.fakebin = os.path.join(self.td, "bin")
        for d in (self.dir, self.shm, self.fakebin):
            os.makedirs(d)
        self.srv = None

    def tearDown(self):
        if self.srv:
            self.srv.stop()
        shutil.rmtree(self.td, ignore_errors=True)

    def qmp(self, **kw):
        self.srv = FakeQMP(os.path.join(self.dir, "qmp.sock"), **kw)
        return self.srv

    def fake(self, name, script):
        p = os.path.join(self.fakebin, name)
        with open(p, "w") as f:
            f.write("#!/bin/sh\n" + script + "\n")
        os.chmod(p, 0o755)
        return p

    def vmkit(self, *args, env=None, timeout=60):
        e = dict(os.environ, PATH=self.fakebin + os.pathsep + os.environ["PATH"],
                 VMKIT_TMP=self.shm, VMKIT_IONICE="none", PYTHONDONTWRITEBYTECODE="1")
        e.update(env or {})
        return subprocess.run([VMKIT, "--dir", self.dir] + list(args), capture_output=True, text=True,
                              env=e, timeout=timeout)

    def fail_first_dumps(self, n):
        """screendump returns a QMP error for the first n calls, then works."""
        calls = {"n": 0}

        def h(args):
            calls["n"] += 1
            if calls["n"] <= n:
                return {"error": {"class": "GenericError", "desc": "no surface"}}
            return self.srv.screendump(args)
        self.srv.handlers["screendump"] = h

    # ---- wait screen / watch ----------------------------------------------

    def test_wait_screen_retries_a_failed_screendump(self):
        self.qmp()
        self.fail_first_dumps(2)            # dump_ppm tries with and without format
        self.fake("magick", 'echo 0 >&2')   # frames identical
        r = self.vmkit("wait", "screen", "6", "2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("screen stable", r.stdout)

    def test_watch_retries_a_failed_screendump(self):
        self.qmp()
        self.fail_first_dumps(2)
        self.fake("magick", 'echo 0 >&2')
        r = self.vmkit("watch", "3")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(os.listdir(self.shm), [])


if __name__ == "__main__":
    unittest.main()
