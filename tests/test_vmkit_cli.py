"""bin/vmkit end to end against a fake QMP server and fake host tools (no VM)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
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

    def vmkit(self, *args, env=None, timeout=60, dir=None, cwd=None):
        e = dict(os.environ, PATH=self.fakebin + os.pathsep + os.environ["PATH"],
                 VMKIT_TMP=self.shm, VMKIT_IONICE="none", PYTHONDONTWRITEBYTECODE="1")
        e.update(env or {})
        return subprocess.run([VMKIT, "--dir", dir or self.dir] + list(args), capture_output=True, text=True,
                              env=e, timeout=timeout, cwd=cwd)

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

    def colors(self, seq):
        """screendump serves solid frames in these colours, the last one repeating."""
        seq = list(seq)

        def h(args):
            self.srv.color = seq.pop(0) if len(seq) > 1 else seq[0]
            return self.srv.screendump(args)
        self.srv.handlers["screendump"] = h

    # `magick compare` exits 1 when the images differ (checked on IM 7.1.2); under
    # set -euo pipefail that used to end wait screen/watch silently.
    def test_wait_screen_survives_differing_frames_fake_magick(self):
        self.qmp()
        n = os.path.join(self.td, "n")
        self.fake("magick", f'if [ -e {n} ]; then echo "0 (0)" >&2; exit 0; fi; touch {n}; echo "3072 (1)" >&2; exit 1')
        r = self.vmkit("wait", "screen", "6", "2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("screen stable (last diff 0px)", r.stdout)

    def test_watch_survives_differing_frames_fake_magick(self):
        self.qmp()
        self.fake("magick", 'echo "3072 (1)" >&2; exit 1')
        r = self.vmkit("watch", "3", env={"VMKIT_STABLE_PX": "100"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("changed 3072px", r.stdout)

    def sizes(self, seq):
        """screendump serves frames of these sizes in turn, cycling."""
        seq = list(seq); i = {"n": 0}

        def h(args):
            self.srv.size = seq[i["n"] % len(seq)]; i["n"] += 1
            return self.srv.screendump(args)
        self.srv.handlers["screendump"] = h

    # On IM 7.1.2 `compare -metric AE` of two sizes prints "0 (0)" and exits 0,
    # so a resolution change must be caught by comparing the dimensions.
    def test_watch_resolution_change_is_a_change(self):
        self.qmp()
        self.sizes([(64, 48), (32, 24)])
        self.fake("magick", 'echo "0 (0)" >&2; exit 0')
        r = self.vmkit("watch", "3")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("changed", r.stdout)

    def test_watch_compare_error_is_a_change(self):
        self.qmp()
        self.fake("magick", 'echo "magick: image widths or heights differ" >&2; exit 2')
        r = self.vmkit("watch", "3")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("changed", r.stdout)

    def test_wait_screen_resolution_change_is_not_stable(self):
        self.qmp()
        self.sizes([(64, 48), (32, 24)])
        self.fake("magick", 'echo "0 (0)" >&2; exit 0')
        r = self.vmkit("wait", "screen", "4", "2")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("stable", r.stdout)

    @unittest.skipUnless(HAVE_MAGICK, "needs ImageMagick")
    def test_watch_resolution_change_real_magick(self):
        self.qmp()
        self.sizes([(64, 48), (32, 24)])
        r = self.vmkit("watch", "3")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("changed", r.stdout)

    @unittest.skipUnless(HAVE_MAGICK, "needs ImageMagick")
    def test_wait_screen_and_watch_real_magick(self):
        self.qmp()
        black, white = (0, 0, 0), (255, 255, 255)
        self.colors([black, white])            # differ once, then stable
        r = self.vmkit("wait", "screen", "6", "2", env={"VMKIT_STABLE_PX": "100"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("screen stable (last diff 0px)", r.stdout)
        self.colors([black, white, black, white])
        r = self.vmkit("watch", "3", env={"VMKIT_STABLE_PX": "100"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("changed 3072px", r.stdout)


    # ---- record start ------------------------------------------------------

    def test_record_start_timeout_kills_the_recorder_and_cleans_up(self):
        srv = FakeQMP(os.path.join(self.dir, "qmp-rec.sock"))
        self.srv = srv

        def trickle(f):     # never completes a reply line (the recorder's reply deadline is 600 s)
            while not srv.closing.wait(0.5):
                f.write(b" ")
        srv.handlers["screendump"] = lambda args: trickle
        self.fake("ffmpeg", "exit 0")
        run = os.path.join(self.td, "run")
        r = self.vmkit("record", "start", "--out", run, timeout=40)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("no frame after", r.stderr)
        with open(os.path.join(run, "recorder.log")) as f:
            pid = int(f.read().split("start pid=")[1].split()[0])
        try:
            with open(f"/proc/{pid}/stat") as f:
                state = f.read().split()[2]
        except OSError:
            state = "gone"
        self.assertIn(state, ("gone", "Z"), "recorder still running")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "record.pid")))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "record.run")))
        # a retry with the same --out is not refused as "already has a recording"; nor is
        # one after a start whose recorder exited (here: QEMU closes the socket)
        srv.handlers["screendump"] = lambda args: (lambda f: f.close())
        for _ in range(2):
            r = self.vmkit("record", "start", "--out", run, timeout=40)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertNotIn("already has a recording", r.stderr)
            self.assertIn("recorder exited", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(run, "frames.jsonl")))
        self.assertTrue(os.path.exists(os.path.join(run, "recorder.log")), "the log the error points at")


    def test_record_stop_kills_a_recorder_that_ignores_term(self):
        self.srv = FakeQMP(os.path.join(self.dir, "qmp-rec.sock"))
        # an encoder that never reads: the pipe and the queue fill, and the
        # recorder is still waiting out its drain (10 s) when the 2 s escalation hits
        self.fake("ffmpeg", "exec sleep 300")
        run = os.path.join(self.td, "run")
        r = self.vmkit("record", "start", "--out", run)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(self.dir, "record.pid")) as f:
            rp = int(f.read())
        self.addCleanup(lambda: subprocess.run(["kill", "-KILL", "--", f"-{rp}"], capture_output=True))
        time.sleep(2)
        kids = subprocess.run(["pgrep", "-P", str(rp)], capture_output=True, text=True).stdout.split()
        self.assertTrue(kids, "no ffmpeg child")
        t0 = time.monotonic()
        r = self.vmkit("record", "stop", env={"VMKIT_REC_STOP_TIMEOUT": "2"}, timeout=60)
        self.assertLess(time.monotonic() - t0, 15, "record stop did not escalate")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("killed", r.stdout + r.stderr)
        time.sleep(0.5)
        for pid in [rp] + [int(k) for k in kids]:
            try:
                with open(f"/proc/{pid}/stat") as f:
                    state = f.read().split()[2]
            except OSError:
                state = "gone"
            self.assertIn(state, ("gone", "Z"), f"pid {pid} still running")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "record.pid")))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "record.run")))
        with open(os.path.join(run, "summary.json")) as f:
            S = json.load(f)
        self.assertTrue(S["killed"])
        self.assertTrue(S["degraded"])
        self.assertGreater(S["frames"], 0)
        with open(os.path.join(run, "frames.jsonl")) as f:
            self.assertEqual(S["frames"], sum(1 for _ in f))
        r = self.vmkit("record", "status")
        self.assertEqual(r.stdout.strip(), "not recording")

    def start_recorder(self, ffmpeg):
        self.srv = FakeQMP(os.path.join(self.dir, "qmp-rec.sock"))
        self.fake("ffmpeg", ffmpeg)
        run = os.path.join(self.td, "run")
        r = self.vmkit("record", "start", "--out", run)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(self.dir, "record.pid")) as f:
            rp = int(f.read())
        self.addCleanup(lambda: subprocess.run(["kill", "-KILL", "--", f"-{rp}"], capture_output=True))
        time.sleep(1)
        return rp, run

    def test_record_stop_timeout_is_checked_before_term_and_may_be_fractional(self):
        rp, run = self.start_recorder("exec sleep 300")
        for bad in ("abc", "-1", "1e3", "2."):
            r = self.vmkit("record", "stop", env={"VMKIT_REC_STOP_TIMEOUT": bad})
            self.assertEqual(r.returncode, 2, (bad, r.stdout + r.stderr))
            self.assertIn("VMKIT_REC_STOP_TIMEOUT", r.stderr)
            with open(f"/proc/{rp}/stat") as f:
                self.assertNotEqual(f.read().split()[2], "Z", f"{bad}: recorder was signalled")
            self.assertTrue(os.path.exists(os.path.join(self.dir, "record.pid")))
        t0 = time.monotonic()
        r = self.vmkit("record", "stop", env={"VMKIT_REC_STOP_TIMEOUT": "1.5"}, timeout=60)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("ignored SIGTERM for 1.5 s", r.stderr)
        self.assertNotIn("syntax error", r.stderr)

    def test_record_stop_default_waits_out_a_slow_clean_drain(self):
        # the encoder takes 8 s to finish after its input ends (nice 19 under load)
        rp, run = self.start_recorder("cat >/dev/null; sleep 8")
        r = self.vmkit("record", "stop", env={"VMKIT_REC_STOP_TIMEOUT": ""}, timeout=90)
        self.assertNotIn("killed", r.stdout + r.stderr)
        with open(os.path.join(run, "summary.json")) as f:
            self.assertNotIn("killed", json.load(f))

    # ---- scratch PPMs are removed on failure --------------------------------

    def dump_then(self, reply):
        """screendump writes a (truncated) PPM, then replies `reply`."""
        def h(args):
            with open(args["filename"], "wb") as f:
                f.write(b"P6\n64 48\n255\n" + bytes(10))
            return reply
        self.srv.handlers["screendump"] = h

    def test_shot_cleans_up_when_ppm2png_fails(self):
        self.qmp()
        self.dump_then({"return": {}})
        r = self.vmkit("shot", "t")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(os.listdir(self.shm), [])

    def test_shot_cleans_up_when_screendump_fails(self):
        self.qmp()
        self.dump_then({"error": {"class": "GenericError", "desc": "disk full"}})
        r = self.vmkit("shot", "t")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(os.listdir(self.shm), [])

    def test_find_and_tap_text_clean_up_when_tesseract_fails(self):
        self.qmp()
        self.fake("tesseract", "exit 1")
        for args in (["find", "x"], ["tap-text", "x"]):
            r = self.vmkit(*args)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertEqual(os.listdir(self.shm), [], args)


    def test_shot_logs_valid_json_for_any_name(self):
        self.qmp()
        name = 'a\\q"b'                                 # backslash + quote
        r = self.vmkit("shot", name)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(self.dir, "actions.jsonl")) as f:
            recs = [json.loads(x) for x in f]
        self.assertEqual([(x["cmd"], x["args"]) for x in recs], [("shot", name)])
        self.assertRegex(recs[0]["t"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    # ---- up ------------------------------------------------------------------

    def up(self, env):
        """`vmkit up` with a fake qemu-system-x86_64 that saves its argv; returns that argv."""
        argv = os.path.join(self.td, "qemu.argv")
        self.fake("qemu-system-x86_64", f'for a in "$@"; do printf "%s\\0" "$a"; done >{argv}.tmp; mv {argv}.tmp {argv}')
        disk = os.path.join(self.td, "disk.qcow2")
        open(disk, "w").close()
        r = self.vmkit("--overlay", disk, "up", env=env)
        for _ in range(50):
            if os.path.exists(argv):
                break
            time.sleep(0.1)
        else:
            self.fail("qemu never started: " + r.stdout + r.stderr)
        with open(argv) as f:
            return r, f.read().split("\0")[:-1]

    def test_up_drops_ionice_when_denied(self):
        self.fake("ionice", 'echo "ionice: ioprio_set failed: Operation not permitted" >&2; exit 1')
        r, argv = self.up({"VMKIT_IONICE": "1"})
        self.assertIn("ionice", r.stderr)
        self.assertIn("-pidfile", argv)

    def test_up_heads_names_the_gpu_with_max_outputs(self):
        r, argv = self.up({"VMKIT_HEADS": "2"})
        self.assertIn("virtio-vga,id=gpu0,max_outputs=2", argv)
        self.assertNotIn("-vga", argv)

    def test_up_gpu_opts_set_the_preferred_mode(self):
        r, argv = self.up({"VMKIT_HEADS": "2", "VMKIT_GPU_OPTS": "xres=1536 yres=960"})
        self.assertIn("virtio-vga,id=gpu0,max_outputs=2,xres=1536,yres=960", argv)

    def test_up_one_head_keeps_vga_virtio(self):
        r, argv = self.up({})
        self.assertIn("-vga", argv)
        self.assertFalse([a for a in argv if "max_outputs" in a])

    def test_head_out_of_range_is_an_error(self):
        r = self.vmkit("--heads", "2", "--head", "2", "status")
        self.assertEqual(r.returncode, 2)
        self.assertIn("2 head", r.stderr)

    def test_shot_addresses_the_chosen_head(self):
        self.qmp()
        r = self.vmkit("--heads", "2", "--head", "1", "shot", "h1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        args = [a for ex, a in self.srv.requests if ex == "screendump"]
        self.assertEqual((args[0]["device"], args[0]["head"]), ("gpu0", 1))

    def test_shot_one_head_sends_no_device(self):
        self.qmp()
        r = self.vmkit("shot", "h0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        args = [a for ex, a in self.srv.requests if ex == "screendump"]
        self.assertNotIn("device", args[0])

    def test_up_qemu_args_env_is_shell_split(self):
        extra = "-device 'virtio-rng-pci,id=r 1'\n-smp 2  -name \"a b\""
        r, argv = self.up({"VMKIT_QEMU_ARGS": extra})
        self.assertEqual(argv[-6:], ["-device", "virtio-rng-pci,id=r 1", "-smp", "2", "-name", "a b"])

    def test_up_qemu_args_env_bad_quoting_is_an_error(self):
        self.fake("qemu-system-x86_64", "exit 0")      # never a real qemu
        disk = os.path.join(self.td, "disk.qcow2")
        open(disk, "w").close()
        r = self.vmkit("--overlay", disk, "up", env={"VMKIT_QEMU_ARGS": "-name 'unclosed"})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("VMKIT_QEMU_ARGS", r.stderr)

    # ---- down ----------------------------------------------------------------

    def fake_qemu(self, pidfile, ignore_term=False):
        code = "import signal, time\n"
        if ignore_term:
            code += "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        code += "time.sleep(120)\n"
        p = subprocess.Popen([sys.executable, "-c", code, "qemu-system-x86_64", "-pidfile", pidfile])
        self.addCleanup(lambda: (p.kill(), p.wait()))
        time.sleep(0.3)     # let it exec (its cmdline is python's by then)
        return p

    def write_pidfile(self, pid):
        with open(os.path.join(self.dir, "qemu.pid"), "w") as f:
            f.write(f"{pid}\n")

    def test_down_does_not_signal_another_vms_qemu(self):
        other = self.fake_qemu(os.path.join(self.td, "other", "qemu.pid"))
        self.write_pidfile(other.pid)                   # stale pidfile, pid reused by another qemu
        r = self.vmkit("down", env={"VMKIT_DOWN_TIMEOUT": "1"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNone(other.poll(), "signalled a qemu that is not this VM's")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "qemu.pid")), "stale pidfile kept")

    def test_down_removes_the_pidfile_after_sigkill(self):
        pidfile = os.path.join(self.dir, "qemu.pid")
        q = self.fake_qemu(pidfile, ignore_term=True)
        self.write_pidfile(q.pid)
        r = self.vmkit("down", env={"VMKIT_DOWN_TIMEOUT": "1"}, timeout=40)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(q.wait(5), -9)
        self.assertFalse(os.path.exists(pidfile), "pidfile left after SIGKILL")

    def test_down_matches_the_pidfile_however_dir_is_spelled(self):
        pidfile = os.path.join(self.dir, "qemu.pid")         # `up` ran with an absolute --dir
        q = self.fake_qemu(pidfile, ignore_term=True)
        self.write_pidfile(q.pid)
        r = self.vmkit("down", env={"VMKIT_DOWN_TIMEOUT": "1"}, timeout=40, dir="./d", cwd=self.td)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(q.wait(5), -9, "qemu not force-killed: pidfile path spelled differently")

    def test_down_without_pidfile_waits_on_qmp(self):
        srv = self.qmp()

        def powerdown(args):          # the guest shuts down: QEMU goes away shortly after replying
            def w(f):
                f.write(b'{"return": {}}\n')
                threading.Timer(0.5, srv.stop).start()
            return w
        srv.handlers["system_powerdown"] = powerdown
        r = self.vmkit("down", env={"VMKIT_DOWN_TIMEOUT": "5"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(), "down")

    def test_down_without_pidfile_and_qemu_still_up_is_unknown(self):
        self.qmp()                    # answers, ignores the powerdown
        r = self.vmkit("down", env={"VMKIT_DOWN_TIMEOUT": "2"})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("down", r.stdout.split())
        self.assertIn("unknown", r.stderr)


if __name__ == "__main__":
    unittest.main()
