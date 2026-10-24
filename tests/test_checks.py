import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from vmkit import checks as C
from vmkit import checkcli as CLI
from vmkit import imageio as I

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HAVE_MAGICK = shutil.which("magick") is not None
HAVE_TESS = shutil.which("tesseract") is not None
OCR_FONT = "DejaVu-Sans-Mono"


def have_font(name):
    """ImageMagick can draw with this font (`magick -list font` names it)."""
    if not HAVE_MAGICK:
        return False
    out = subprocess.run(["magick", "-list", "font"], capture_output=True, text=True).stdout
    return any(l.strip() == "Font: " + name for l in out.splitlines())


def solid(w, h, rgb):
    return I.Image(w, h, bytes(rgb) * (w * h))


def paint(img, x, y, w, h, rgb):
    d = bytearray(img.data)
    for yy in range(y, y + h):
        for xx in range(x, x + w):
            i = (yy * img.w + xx) * 3
            d[i:i + 3] = bytes(rgb)
    return I.Image(img.w, img.h, bytes(d))


class Tmp(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="vmkit-test-")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def png(self, name, img):
        p = os.path.join(self.td, name)
        I.write_png(img, p)
        return p

    def ppm(self, name, img):
        p = os.path.join(self.td, name)
        with open(p, "wb") as f:
            f.write(I.ppm_bytes(img))
        return p


class Regions(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(C.resolve_region([10, 20, 30, 40], 100, 80), (10, 20, 30, 40))

    def test_none_is_full_frame(self):
        self.assertEqual(C.resolve_region(None, 100, 80), (0, 0, 100, 80))

    def test_negative_from_right_bottom(self):
        # schema example: the bottom 60 px of the screen
        self.assertEqual(C.resolve_region([0, -60, 0, 60], 1280, 800), (0, 740, 1280, 60))
        self.assertEqual(C.resolve_region([-100, -50, 100, 50], 1280, 800), (1180, 750, 100, 50))

    def test_zero_size_means_to_edge(self):
        self.assertEqual(C.resolve_region([100, 200, 0, 0], 1280, 800), (100, 200, 1180, 600))
        self.assertEqual(C.resolve_region([-10, 0, 0, 5], 100, 80), (90, 0, 10, 5))

    def test_outside_is_error(self):
        for r in ([90, 0, 20, 10], [0, 0, 101, 1], [-101, 0, 5, 5], [0, 0, -1, 5], [100, 0, 0, 5]):
            with self.assertRaises(C.CheckError, msg=r):
                C.resolve_region(r, 100, 80)

    def test_parse(self):
        self.assertEqual(C.parse_region("1,2,3,4"), [1, 2, 3, 4])
        self.assertEqual(C.parse_region("[0, -60, 0, 60]"), [0, -60, 0, 60])
        self.assertEqual(C.parse_region([5, 6, 7, 8]), [5, 6, 7, 8])
        self.assertIsNone(C.parse_region(None))
        with self.assertRaises(C.CheckError):
            C.parse_region("1,2,3")

    def test_near(self):
        self.assertEqual(C.near_region([50, 50], 10, 8, 8, 200, 200), (36, 36, 28, 28))
        self.assertEqual(C.near_region([2, 2], 10, 8, 8, 200, 200), (0, 0, 16, 16))   # clamped

    def test_color(self):
        self.assertEqual(C.parse_color("#00b4ff"), (0, 180, 255))
        with self.assertRaises(C.CheckError):
            C.parse_color("blue")


class ImageIO(Tmp):
    def test_png_roundtrip_and_size(self):
        img = paint(solid(7, 5, (1, 2, 3)), 2, 1, 3, 2, (200, 100, 50))
        p = self.png("a.png", img)
        self.assertEqual(I.size(p), (7, 5))
        self.assertEqual(I.read_png(p).data, img.data)

    def test_png_filters_decode(self):
        # PNG written with non-zero filters (sub/up/avg/paeth) by hand
        import struct
        import zlib
        w, h = 4, 5
        img = bytes((x * 40 + y * 7 + c * 3) & 255 for y in range(h) for x in range(w) for c in range(3))
        stride = w * 3
        raw = bytearray()
        prev = bytes(stride)
        for y, ft in enumerate([0, 1, 2, 3, 4]):
            line = img[y * stride:(y + 1) * stride]
            out = bytearray()
            for k in range(stride):
                a = line[k - 3] if k >= 3 else 0
                b = prev[k]
                c = prev[k - 3] if k >= 3 else 0
                pred = [0, a, b, (a + b) >> 1, I._paeth(a, b, c)][ft]
                out.append((line[k] - pred) & 255)
            raw += bytes([ft]) + out
            prev = line
        def chunk(t, d):
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
        png = (I.PNG_SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))
        self.assertEqual(I.parse_png(png).data, img)

    def test_ppm(self):
        img = paint(solid(6, 4, (9, 9, 9)), 1, 1, 2, 2, (255, 0, 0))
        p = self.ppm("a.ppm", img)
        self.assertEqual(I.size(p), (6, 4))
        self.assertEqual(I.read_ppm(p).data, img.data)
        self.assertEqual(I.parse_ppm(b"P6\n# comment\n6 4\n255\n" + img.data).data, img.data)

    def test_load_region_png_and_ppm_agree(self):
        img = paint(solid(40, 30, (10, 20, 30)), 30, 20, 10, 10, (250, 0, 0))
        for p in (self.png("r.png", img), self.ppm("r.ppm", img)):
            sub, box, wh = C.load_region(p, [-10, -10, 0, 0])
            self.assertEqual((box, wh), ((30, 20, 10, 10), (40, 30)))
            self.assertEqual(sub.data, bytes((250, 0, 0)) * 100)


class Pixel(Tmp):
    def setUp(self):
        super().setUp()
        img = solid(64, 48, (0, 0, 0))
        img = paint(img, 10, 10, 20, 10, (0, 180, 255))     # 200 px exact
        img = paint(img, 30, 10, 10, 10, (20, 190, 240))    # 100 px off by <= 20
        self.p = self.png("f.png", img)

    def test_exact_match(self):
        ok, v = C.pixel(self.p, [10, 10, 20, 10], "#00b4ff", 0, 1.0)
        self.assertTrue(ok)
        self.assertEqual(v["matched_px"], 200)

    def test_tolerance_is_per_channel(self):
        self.assertFalse(C.pixel(self.p, [10, 10, 30, 10], "#00b4ff", 16, 1.0)[0])   # 20 > 16
        ok, v = C.pixel(self.p, [10, 10, 30, 10], "#00b4ff", 20, 1.0)
        self.assertTrue(ok)
        self.assertEqual(v["fraction"], 1.0)

    def test_min_fraction(self):
        ok, v = C.pixel(self.p, [0, 0, 64, 48], "#00b4ff", 0, 0.05)   # 200 / 3072 = 0.065
        self.assertTrue(ok)
        self.assertAlmostEqual(v["fraction"], 200 / 3072, places=3)
        self.assertFalse(C.pixel(self.p, [0, 0, 64, 48], "#00b4ff", 0, 0.1)[0])

    def test_run_check_not_and_errors(self):
        spec = {"region": [10, 10, 20, 10], "color": "#00b4ff", "not": True}
        self.assertEqual(C.run_check("pixel", self.p, spec)["status"], "fail")
        spec = {"region": [60, 40, 10, 10], "color": "#00b4ff", "not": True}   # outside -> error, even with not
        self.assertEqual(C.run_check("pixel", self.p, spec)["status"], "error")
        self.assertEqual(C.run_check("pixel", self.p, {"region": [0, 0, 1, 1]})["status"], "error")


class Changed(Tmp):
    def setUp(self):
        super().setUp()
        base = solid(64, 48, (40, 40, 40))
        self.b = self.png("b.png", base)
        self.f = self.png("f.png", paint(base, 5, 5, 10, 10, (200, 200, 200)))     # 100 px changed
        self.faint = self.png("g.png", paint(base, 5, 5, 10, 10, (45, 45, 45)))    # below fuzz

    def test_counts_changed_pixels(self):
        ok, v = C.changed(self.f, self.b, None, 50, 5)
        self.assertTrue(ok)
        self.assertEqual(v["changed_px"], 100)
        self.assertFalse(C.changed(self.f, self.b, None, 101, 5)[0])

    def test_region_limits_it(self):
        ok, v = C.changed(self.f, self.b, [30, 0, 0, 0], 1, 5)
        self.assertFalse(ok)
        self.assertEqual(v["changed_px"], 0)

    def test_fuzz(self):
        # distance sqrt(3*25)=8.7 > 0 but < 5% of 255 = 12.75
        self.assertEqual(C.changed(self.faint, self.b, None, 1, 5)[1]["changed_px"], 0)
        self.assertEqual(C.changed(self.faint, self.b, None, 1, 1)[1]["changed_px"], 100)

    def test_resolution_change_is_changed(self):
        other = self.png("o.png", solid(32, 24, (40, 40, 40)))
        ok, v = C.changed(other, self.b, None, 50, 5)
        self.assertTrue(ok)

    def test_not_and_missing_baseline(self):
        r = C.run_check("changed", self.f, {"not": True}, self.b)
        self.assertEqual(r["status"], "fail")
        self.assertEqual(C.run_check("changed", self.f, {}, None)["status"], "error")


@unittest.skipUnless(HAVE_MAGICK and HAVE_TESS, "needs magick + tesseract")
class OCR(Tmp):
    @unittest.skipUnless(have_font(OCR_FONT), "needs the DejaVu Sans Mono font known to ImageMagick "
                         "(e.g. ttf-dejavu / fonts-dejavu-core; see `magick -list font`)")
    def test_text_and_regex(self):
        p = os.path.join(self.td, "t.png")
        subprocess.run(["magick", "-size", "500x120", "xc:#202020", "-font", OCR_FONT, "-pointsize", "28",
                        "-fill", "#e0e0e0", "-annotate", "+20+50", "hello vmkit 42", "-annotate", "+20+100",
                        "second line", p], check=True)
        self.assertTrue(C.ocr(p, None, text="Hello  VMKIT")[0])
        self.assertTrue(C.ocr(p, [0, 0, 0, 60], regex=r"vmkit \d+")[0])
        self.assertFalse(C.ocr(p, [0, 0, 0, 60], text="second")[0])        # region excludes line 2
        self.assertEqual(C.run_check("ocr", p, {"text": "goodbye", "not": True})["status"], "pass")
        self.assertEqual(C.run_check("ocr", p, {"text": "a", "regex": "b"})["status"], "error")


@unittest.skipUnless(HAVE_MAGICK, "needs magick")
class Template(Tmp):
    def setUp(self):
        super().setUp()
        self.big = os.path.join(self.td, "big.png")
        subprocess.run(["magick", "-size", "200x150", "-seed", "3", "plasma:fractal", self.big], check=True)
        self.tmpl = os.path.join(self.td, "t.png")
        subprocess.run(["magick", self.big, "-crop", "30x20+120+80", "+repage", self.tmpl], check=True)
        self.other = os.path.join(self.td, "o.png")
        subprocess.run(["magick", "-size", "30x20", "-seed", "9", "plasma:fractal", self.other], check=True)

    def test_found_in_region(self):
        ok, v = C.template(self.big, self.tmpl, region=[100, 60, 80, 60])
        self.assertTrue(ok, v)
        self.assertEqual((v["x"], v["y"]), (120, 80))
        self.assertGreater(v["similarity"], 0.99)

    def test_near_radius(self):
        ok, v = C.template(self.big, self.tmpl, near=[135, 90], radius=12)
        self.assertTrue(ok, v)
        self.assertEqual((v["x"], v["y"]), (120, 80))

    def test_absent(self):
        ok, v = C.template(self.big, self.other, region=[0, 0, 0, 0], threshold=0.9)
        self.assertFalse(ok, v)
        r = C.run_check("template", self.big, {"image": self.other, "region": [0, 0, 0, 0], "not": True})
        self.assertEqual(r["status"], "pass")

    def test_needs_search_area(self):
        self.assertEqual(C.run_check("template", self.big, {"image": self.tmpl})["status"], "error")


def write_run(d, frames_imgs, actions, t0_ms=1000, step_ms=33, inactive=()):
    os.makedirs(os.path.join(d, "frames"), exist_ok=True)
    with open(os.path.join(d, "frames.jsonl"), "w") as f:
        for n, img in enumerate(frames_imgs):
            t = (t0_ms + n * step_ms) * 1_000_000
            I.write_png(img, os.path.join(d, "frames", f"{n:06d}.png"))
            f.write(json.dumps({"n": n, "t_req": t - 4_000_000, "t": t, "inactive": n in inactive}) + "\n")
    with open(os.path.join(d, "actions.jsonl"), "w") as f:
        for a in actions:
            f.write(json.dumps(a) + "\n")


class Evaluate(Tmp):
    """Expectations over a synthetic recording: a box appears ~66 ms after the action."""

    def setUp(self):
        super().setUp()
        base = solid(64, 48, (0, 0, 0))
        box = paint(base, 20, 20, 10, 10, (0, 180, 255))
        imgs = [base, base, base, base, box, box, base, base]           # frames at 1000..1231 ms (33 ms apart)
        self.run_dir = os.path.join(self.td, "run")
        ms = 1_000_000
        write_run(self.run_dir, imgs,
                  [{"cmd": "keys", "t_send": 1090 * ms, "t_ack": 1095 * ms, "ok": True, "step": "open"},
                   {"cmd": "keys", "t_send": 1150 * ms, "t_ack": 1151 * ms, "ok": False, "error": "boom"}],
                  inactive=(7,))
        from vmkit import timeline as T
        self.frames, self.actions = T.load_frames(self.run_dir), T.load_actions(self.run_dir)

    def ev(self, **exp):
        return CLI.evaluate(self.run_dir, self.frames, self.actions, dict({"action": 0}, **exp))

    def test_window_any(self):
        r = self.ev(check="pixel", at="+0ms..+100ms", region=[20, 20, 10, 10], color="#00b4ff")
        self.assertEqual(r["status"], "pass")
        self.assertEqual([f["n"] for f in r["frames"]], [3, 4, 5])

    def test_window_all(self):
        r = self.ev(check="pixel", at="+0ms..+100ms", mode="all", region=[20, 20, 10, 10], color="#00b4ff")
        self.assertEqual(r["status"], "fail")

    def test_not_on_window_defaults_to_all(self):
        # schema v0.3: not on a window = "in no frame of the window"
        box = dict(check="pixel", at="+0ms..+100ms", region=[20, 20, 10, 10], color="#00b4ff")
        r = self.ev(**box, **{"not": True})
        self.assertEqual((r["mode"], r["status"]), ("all", "fail"))    # the box is in frames 4, 5
        r = self.ev(**box, mode="any", **{"not": True})                  # explicit any: some frame lacks it
        self.assertEqual((r["mode"], r["status"]), ("any", "pass"))
        r = self.ev(**dict(box, at="+100ms..+140ms"), **{"not": True})  # frame 6 only: no box
        self.assertEqual(r["status"], "pass")

    def test_not_on_a_window_over_a_sampling_gap_is_error(self):
        # frame 6 (1198 ms) lacks the box; screendump errors from 1205 to 1228 ms left no frame
        from vmkit import timeline as T
        with open(os.path.join(self.run_dir, "gaps.jsonl"), "w") as f:
            f.write(json.dumps({"t_from": 1205_000_000, "t_to": 1228_000_000, "errors": 1, "reason": "x"}) + "\n")
        self.frames = T.load_frames(self.run_dir)
        r = self.ev(check="pixel", at="+100ms..+140ms", region=[20, 20, 10, 10], color="#00b4ff",
                    **{"not": True})
        self.assertEqual(r["status"], "error")
        self.assertIn("gap", r["error"])

    def test_point_before_action_as_baseline_check(self):
        r = self.ev(check="pixel", at="-50ms", region=[20, 20, 10, 10], color="#00b4ff", **{"not": True})
        self.assertEqual(r["status"], "pass")

    def test_changed_uses_last_frame_before_anchor(self):
        r = self.ev(check="changed", at="+30ms..+100ms", region=[0, 0, 0, 0], min_px=50)
        self.assertEqual(r["status"], "pass")
        self.assertEqual(r["baseline"]["n"], 2)

    def test_anchor_ack_and_step(self):
        r = self.ev(action="open", anchor="ack", check="pixel", at="+40ms", region=[20, 20, 10, 10],
                    color="#00b4ff")
        self.assertEqual(r["frames"][0]["n"], 5)   # 1095+40=1135 -> first frame at/after is 1165
        self.assertEqual(r["status"], "pass")

    def test_empty_window_is_error(self):
        r = self.ev(check="pixel", at="+1ms..+5ms", region=[20, 20, 10, 10], color="#00b4ff")
        self.assertEqual(r["status"], "error")
        self.assertIn("no frame in the window", r["error"])

    def test_inactive_frame_is_error(self):
        r = self.ev(check="pixel", at="+141ms", region=[20, 20, 10, 10], color="#000000")
        self.assertEqual(r["frames"][0]["n"], 7)
        self.assertEqual(r["status"], "error")

    def test_failed_action_is_error(self):
        r = self.ev(action=1, check="pixel", at="+0ms", region=[0, 0, 1, 1], color="#000000")
        self.assertEqual(r["status"], "error")

    def test_aggregate(self):
        P, F, E = ({"status": s} for s in ("pass", "fail", "error"))
        self.assertEqual(CLI.aggregate([F, E, P], "any"), "pass")
        self.assertEqual(CLI.aggregate([F, E], "any"), "error")
        self.assertEqual(CLI.aggregate([F, F], "any"), "fail")
        self.assertEqual(CLI.aggregate([P, E], "all"), "error")
        self.assertEqual(CLI.aggregate([P, F], "all"), "fail")
        self.assertEqual(CLI.aggregate([P, P], "all"), "pass")

    def test_batch_bad_fields_are_per_expectation_errors(self):
        notimg = os.path.join(self.td, "notimg.png")
        with open(notimg, "w") as f:
            f.write("not an image")
        px = dict(action=0, at="+0ms..+100ms", check="pixel", region=[20, 20, 10, 10], color="#00b4ff")
        exps = [dict(px, id="tol-null", tolerance=None),
                dict(px, id="tol-str", tolerance="x"),
                dict(px, id="frac-str", min_fraction="most"),
                dict(action=0, at="+30ms..+100ms", check="changed", id="minpx-str", min_px="x"),
                dict(action=0, at="+30ms..+100ms", check="changed", id="fuzz-null", fuzz=None),
                dict(px, id="at-null", at=None),
                dict(px, id="good")]
        exp = os.path.join(self.td, "expect.json")
        with open(exp, "w") as f:
            json.dump(exps, f)
        out = os.path.join(self.td, "results.json")
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            rc = CLI.main(["batch", "--run", self.run_dir, exp, "-o", out])
        self.assertEqual(rc, 2)
        with open(out) as f:
            res = {r["id"]: r for r in json.load(f)}
        self.assertEqual(res.pop("good")["status"], "pass")
        for k, r in res.items():
            self.assertEqual(r["status"], "error", k)
            self.assertTrue(r.get("error"), k)

    def test_template_unreadable_files_are_errors(self):
        notimg = os.path.join(self.td, "notimg.png")
        with open(notimg, "w") as f:
            f.write("not an image")
        frame = os.path.join(self.run_dir, "frames", "000000.png")
        with mock.patch.object(C, "MAGICK", "magick"):     # past the "needs ImageMagick" gate
            r1 = C.run_check("template", frame, {"image": notimg, "region": [0, 0, 0, 0]})
            r2 = C.run_check("template", frame + ".missing", {"image": frame, "region": [0, 0, 0, 0]})
        self.assertEqual((r1["status"], r2["status"]), ("error", "error"))

    def test_batch_cli(self):
        exp = os.path.join(self.td, "expect.json")
        with open(exp, "w") as f:
            json.dump([{"id": "box", "action": 0, "at": "+0ms..+100ms", "check": "pixel",
                        "region": [20, 20, 10, 10], "color": "#00b4ff"},
                       {"id": "gone", "action": 0, "at": "+100ms", "check": "pixel",
                        "region": [20, 20, 10, 10], "color": "#00b4ff", "not": True}], f)
        cli = [sys.executable, os.path.join(ROOT, "bin", "vmkit-check")]
        r = subprocess.run(cli + ["batch", "--run", self.run_dir, exp], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(self.run_dir, "results.json")) as f:
            res = json.load(f)
        self.assertEqual([x["status"] for x in res], ["pass", "pass"])
        # single-check CLI on an explicit frame: exit 1 on fail
        r = subprocess.run(cli + ["pixel", "--frame", os.path.join(self.run_dir, "frames", "000000.png"),
                                  "--region", "20,20,10,10", "--color", "#00b4ff"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout)


if __name__ == "__main__":
    unittest.main()
