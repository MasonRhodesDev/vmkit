import unittest

from vmkit.record import is_placeholder, pct

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


if __name__ == "__main__":
    unittest.main()
