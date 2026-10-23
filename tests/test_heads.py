"""Multi-head: head addressing, pointer mapping, per-head shot/record (fake QMP, no VM)."""
import json
import os
import shutil
import time
import unittest

from vmkit import act, heads

from .fakeqmp import FakeQMP
from . import test_vmkit_cli as _cli

def read_lines(path):
    with open(path) as f:
        return f.read().splitlines()


LAYOUT = "0,0,1920,1080;1920,0,3440,1440;5360,0,2160,3840"


class Rfb(unittest.TestCase):
    def test_set_size_speaks_the_handshake_and_sends_set_desktop_size(self):
        import socket, struct, tempfile, threading
        from vmkit import rfb
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "v.sock")
        srv = socket.socket(socket.AF_UNIX); srv.bind(path); srv.listen(1)
        got = {}

        def serve():
            c, _ = srv.accept()
            c.sendall(b"RFB 003.008\n"); c.recv(12)
            c.sendall(b"\x01\x01"); c.recv(1); c.sendall(b"\0\0\0\0"); c.recv(1)
            c.sendall(struct.pack(">HH", 640, 480) + bytes(16) + struct.pack(">I", 2) + b"qe")
            buf = b""
            while len(buf) < 12 + 8 + 16:
                chunk = c.recv(64)
                if not chunk:
                    break
                buf += chunk
            got["msg"] = buf
            c.close()
        t = threading.Thread(target=serve); t.start()
        rfb.set_size(path, 1920, 1080)
        t.join(5); srv.close()
        m = got["msg"]
        self.assertEqual(m[0], 2)                                    # SetEncodings
        self.assertEqual(m[12], 251)                                 # SetDesktopSize
        self.assertEqual(struct.unpack(">HH", m[14:18]), (1920, 1080))


class Helpers(unittest.TestCase):
    def test_screendump_args_head0_is_the_default_console(self):
        self.assertEqual(heads.screendump_args("/x", None, 0), {"filename": "/x", "format": "ppm"})
        self.assertEqual(heads.screendump_args("/x", "gpu0", 0)["device"], "gpu0")

    def test_screendump_args_other_head_needs_a_device(self):
        a = heads.screendump_args("/x", "gpu0", 2)
        self.assertEqual((a["device"], a["head"]), ("gpu0", 2))
        with self.assertRaises(heads.HeadError):
            heads.screendump_args("/x", None, 1)

    def test_parse_heads(self):
        self.assertEqual(heads.parse_heads("all", 3), [0, 1, 2])
        self.assertEqual(heads.parse_heads("2,0", 3), [0, 2])
        for bad in ("3", "x", "-1"):
            with self.assertRaises(heads.HeadError):
                heads.parse_heads(bad, 3)

    def test_parse_layout_rejects_junk(self):
        self.assertEqual(heads.parse_layout("0,0,10,20;10,0,5,5"), [(0, 0, 10, 20), (10, 0, 5, 5)])
        for bad in ("1,2,3", "a,b,c,d", "0,0,0,5"):
            with self.assertRaises(heads.HeadError):
                heads.parse_layout(bad)

    def test_pointer_abs_spans_the_bounding_box(self):
        lay = heads.parse_layout(LAYOUT)            # box 7520 x 3840
        self.assertEqual(heads.pointer_abs(lay, 0, 0, 0), (0, 0))
        self.assertEqual(heads.pointer_abs(lay, 2, 2159, 3839), (32767, 32767))
        # centre of head 1 (3440x1440 at x=1920): global x = 1920 + 1720
        x, y = heads.pointer_abs(lay, 1, 1720, 720)
        self.assertEqual(x, round(3640 / 7519 * 32767))
        self.assertEqual(y, round(720 / 3839 * 32767))

    def test_pointer_abs_box_starts_at_the_smallest_origin(self):
        lay = heads.parse_layout("-1920,0,1920,1080;0,0,1920,1080")      # a head left of the origin
        self.assertEqual(heads.pointer_abs(lay, 0, 0, 0), (0, 0))
        self.assertEqual(heads.pointer_abs(lay, 1, 1919, 1079), (32767, 32767))

    def test_pointer_abs_unknown_head(self):
        with self.assertRaises(heads.HeadError):
            heads.pointer_abs(heads.parse_layout("0,0,10,10"), 1, 0, 0)

    def test_gpu_info_from_file_and_env(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.assertEqual(heads.gpu_info(d), (None, 1))
        with open(heads.gpu_json_path(d), "w") as f:
            json.dump({"id": "gpu0", "heads": 3}, f)
        self.assertEqual(heads.gpu_info(d), ("gpu0", 3))
        os.environ["VMKIT_GPU_ID"] = "other"
        self.addCleanup(os.environ.pop, "VMKIT_GPU_ID", None)
        self.assertEqual(heads.gpu_info(d), ("other", 3))


class ActHead(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.td = tempfile.mkdtemp(prefix="vmkit-act-")
        self.sock = os.path.join(self.td, "qmp.sock")
        self.srv = FakeQMP(self.sock)

    def tearDown(self):
        self.srv.stop()
        shutil.rmtree(self.td, ignore_errors=True)

    def test_move_on_a_head_uses_the_layout_and_no_screendump(self):
        rec, _ = act.run_qmp("move", ["1720", "720"], self.sock, self.td, 1, heads.parse_layout(LAYOUT))
        self.assertTrue(rec["ok"], rec.get("error"))
        ev = [a["events"] for ex, a in self.srv.requests if ex == "input-send-event"][0]
        self.assertEqual([e["data"]["value"] for e in ev],
                         [round(3640 / 7519 * 32767), round(720 / 3839 * 32767)])
        self.assertNotIn("screendump", self.srv.commands())

    def test_a_head_outside_the_layout_is_a_failed_action(self):
        rec, _ = act.run_qmp("move", ["1", "1"], self.sock, self.td, 5, heads.parse_layout(LAYOUT))
        self.assertFalse(rec["ok"])
        self.assertIn("not in the layout", rec["error"])

    def test_actions_go_to_every_head_run_dir(self):
        run = os.path.join(self.td, "run")
        for h in (0, 1):
            os.makedirs(os.path.join(run, f"head{h}"))
        with open(os.path.join(run, "heads.json"), "w") as f:
            json.dump({"heads": [{"head": 0, "run": "head0"}, {"head": 1, "run": "head1"}]}, f)
        with open(os.path.join(self.td, "record.run"), "w") as f:
            f.write(run)
        act.log(self.td, {"cmd": "x"})
        self.assertTrue(os.path.exists(os.path.join(self.td, "actions.jsonl")))
        for h in (0, 1):
            self.assertTrue(os.path.exists(os.path.join(run, f"head{h}", "actions.jsonl")))


class MultiHeadCLI(unittest.TestCase):
    """bin/vmkit against fake QMP sockets; the harness is test_vmkit_cli's."""
    setUp, tearDown, qmp, fake, vmkit, up = (_cli.VmkitCLI.setUp, _cli.VmkitCLI.tearDown, _cli.VmkitCLI.qmp, _cli.VmkitCLI.fake,
                                              _cli.VmkitCLI.vmkit, _cli.VmkitCLI.up)

    def gpu(self, n=3):
        with open(os.path.join(self.dir, "gpu.json"), "w") as f:
            json.dump({"id": "gpu0", "heads": n}, f)

    def dumps(self):
        return [a for ex, a in self.srv.requests if ex == "screendump"]

    def test_shot_head_names_the_device_and_head(self):
        self.qmp(); self.gpu()
        r = self.vmkit("shot", "--head", "2", "x")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(r.stdout.strip().endswith("x-head2.png"))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "x-head2.png")))
        self.assertEqual((self.dumps()[0]["device"], self.dumps()[0]["head"]), ("gpu0", 2))

    def test_shot_heads_writes_one_file_per_head(self):
        self.qmp(); self.gpu()
        r = self.vmkit("shot", "--heads", "s")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([os.path.basename(x) for x in r.stdout.split()], ["s-head0.png", "s-head1.png", "s-head2.png"])
        self.assertEqual([d.get("head", 0) for d in self.dumps()], [0, 1, 2])

    def test_plain_shot_is_unchanged(self):
        self.qmp(); self.gpu()
        r = self.vmkit("shot", "p")
        self.assertTrue(r.stdout.strip().endswith("/p.png"))
        self.assertNotIn("device", self.dumps()[0])

    def test_head_out_of_range_never_reaches_qemu(self):
        # QEMU 11.1.2 does not validate the index; a bad one killed the process
        self.qmp(); self.gpu(2)
        r = self.vmkit("shot", "--head", "2")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("out of range", r.stderr)
        self.assertEqual(self.dumps(), [])

    def test_head_without_a_device_id_is_refused(self):
        self.qmp()
        r = self.vmkit("shot", "--head", "1")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.dumps(), [])

    def test_heads_lists_sizes(self):
        self.qmp(size=(32, 16)); self.gpu(2)
        r = self.vmkit("heads")
        self.assertEqual(r.stdout.split("\n")[:2], ["0 32x16", "1 32x16"])

    def test_pointer_head_needs_a_layout(self):
        self.qmp(); self.gpu()
        r = self.vmkit("move", "--head", "1", "5", "5")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("layout", r.stderr)
        r = self.vmkit("move", "--head", "1", "--layout", LAYOUT, "5", "5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_up_gpu_heads_builds_one_multi_output_device(self):
        r, argv = self.up({"VMKIT_GPU_HEADS": "3", "VMKIT_GPU_RES": "1920x1080"})
        dev = argv[argv.index("-vga") + 1:]
        self.assertEqual(dev[0], "none")
        self.assertIn("virtio-vga,id=gpu0,max_outputs=3,xres=1920,yres=1080", argv)
        self.assertEqual(sum(1 for a in argv if a.startswith("unix:") and "qmp-rec-h" in a), 2)
        with open(os.path.join(self.dir, "gpu.json")) as f:
            self.assertEqual(json.load(f)["heads"], 3)

    def test_up_gpu_modes_sets_the_heads_and_gives_each_extra_head_a_vnc_socket(self):
        r, argv = self.up({"VMKIT_GPU_MODES": "3440x1440,1920x1080,3840x2160"})
        self.assertIn("virtio-vga,id=gpu0,max_outputs=3,xres=3440,yres=1440", argv)
        vnc = [a for a in argv if a.startswith("unix:") and "vnc-h" in a]
        self.assertEqual([v.split(",", 1)[1] for v in vnc], ["display=gpu0,head=1", "display=gpu0,head=2"])

    def test_up_gpu_modes_count_must_match_heads(self):
        self.fake("qemu-system-x86_64", "exit 0")
        disk = os.path.join(self.td, "disk.qcow2"); open(disk, "w").close()
        r = self.vmkit("--overlay", disk, "up", env={"VMKIT_GPU_MODES": "1x1,2x2", "VMKIT_GPU_HEADS": "3"})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)

    def test_heads_set_needs_a_control_socket(self):
        self.qmp(); self.gpu(3)
        r = self.vmkit("heads", "set", "1", "800x600")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("control socket", r.stderr)

    def test_up_without_gpu_flags_keeps_vga_virtio(self):
        r, argv = self.up({})
        self.assertEqual(argv[argv.index("-vga") + 1], "virtio")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "gpu.json")))

    def test_up_uefi_uses_ovmf_pflash_and_a_vars_copy(self):
        if not os.path.exists("/usr/share/edk2/x64/OVMF_CODE.4m.fd"):
            self.skipTest("no OVMF on this host")
        r, argv = self.up({"VMKIT_UEFI": "1"})
        self.assertEqual(sum(1 for a in argv if a.startswith("if=pflash")), 2)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "efivars.fd")))

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_record_heads_runs_one_recorder_per_head_on_its_own_socket(self):
        self.gpu(2)
        s0 = FakeQMP(os.path.join(self.dir, "qmp-rec.sock"), size=(64, 48))
        s1 = FakeQMP(os.path.join(self.dir, "qmp-rec-h1.sock"), size=(32, 24))
        self.addCleanup(s0.stop); self.addCleanup(s1.stop)
        run = os.path.join(self.td, "run")
        r = self.vmkit("record", "start", "--fps", "10", "--out", run, "--heads", "all", timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        time.sleep(0.5)
        self.vmkit("keys", "a"); time.sleep(0.6)
        r = self.vmkit("record", "stop", timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("head 0:", r.stdout); self.assertIn("head 1:", r.stdout)
        with open(os.path.join(run, "heads.json")) as f:
            self.assertEqual([h["head"] for h in json.load(f)["heads"]], [0, 1])
        for h, size in ((0, "64x48"), (1, "32x24")):
            rows = [json.loads(x) for x in read_lines(os.path.join(run, f"head{h}", "frames.jsonl"))]
            self.assertGreater(len(rows), 2)
            seg = json.loads(read_lines(os.path.join(run, f"head{h}", "segments.jsonl"))[0])
            self.assertEqual(f"{seg['w']}x{seg['h']}", size)
        d0 = [a for ex, a in s0.requests if ex == "screendump"]
        self.assertTrue(d0 and all(a["device"] == "gpu0" and "head" not in a for a in d0))
        d1 = [a for ex, a in s1.requests if ex == "screendump"]
        self.assertTrue(d1 and all(a["device"] == "gpu0" and a["head"] == 1 for a in d1))
        # the key press is in every head's action log, on the shared clock
        for h in (0, 1):
            acts = [json.loads(x) for x in read_lines(os.path.join(run, f"head{h}", "actions.jsonl"))]
            self.assertEqual([x["cmd"] for x in acts], ["keys"])
        # --run PARENT means head 0 (so does the default); --run PARENT/head1 selects another
        r = self.vmkit("frames", "--run", run, "--action", "#0", "--offset", "+100ms", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("/head0/", r.stdout)
        r = self.vmkit("frames", "--run", os.path.join(run, "head1"), "--action", "#0", "--offset", "+100ms", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("/head1/", r.stdout)

    def test_record_heads_bad_head_is_refused(self):
        self.gpu(2)
        s0 = FakeQMP(os.path.join(self.dir, "qmp-rec.sock")); self.addCleanup(s0.stop)
        self.fake("ffmpeg", "exit 0")
        r = self.vmkit("record", "start", "--out", os.path.join(self.td, "r"), "--heads", "5")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("out of range", r.stderr)


if __name__ == "__main__":
    unittest.main()
