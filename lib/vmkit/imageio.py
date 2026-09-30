"""Minimal stdlib image I/O: binary PPM (P6) read/write, PNG write, PNG read.

vmkit asks QEMU for PPM screendumps (PNG encoding inside QEMU costs ~30 ms per
call and stalls the guest) and converts on the host. Everything here is 8-bit
RGB, row-major, 3 bytes per pixel.

CLI:  python3 imageio.py ppm2png IN.ppm OUT.png
      python3 imageio.py size FILE          -> "W H" (PPM or PNG header)
"""
import struct
import sys
import zlib

PNG_SIG = b"\x89PNG\r\n\x1a\n"


class Image:
    """w x h RGB image; data is bytes of length w*h*3."""
    __slots__ = ("w", "h", "data")

    def __init__(self, w, h, data):
        if len(data) != w * h * 3:
            raise ValueError(f"image data is {len(data)} bytes, want {w*h*3} for {w}x{h}")
        self.w, self.h, self.data = w, h, data

    def px(self, x, y):
        i = (y * self.w + x) * 3
        return self.data[i], self.data[i + 1], self.data[i + 2]

    def crop(self, x, y, w, h):
        if x < 0 or y < 0 or w < 0 or h < 0 or x + w > self.w or y + h > self.h:
            raise ValueError(f"crop {x},{y},{w},{h} outside {self.w}x{self.h}")
        if (x, y, w, h) == (0, 0, self.w, self.h):
            return self
        stride = self.w * 3
        rows = [self.data[(y + r) * stride + x * 3:(y + r) * stride + (x + w) * 3] for r in range(h)]
        return Image(w, h, b"".join(rows))


# ---- PPM ---------------------------------------------------------------------

def _ppm_header(buf):
    """Parse a P6 header -> (w, h, maxval, offset of pixel data)."""
    if buf[:2] != b"P6":
        raise ValueError("not a binary PPM (P6)")
    fields, i, n = [], 2, len(buf)
    while len(fields) < 3:
        while i < n and buf[i] in b" \t\r\n":
            i += 1
        if i < n and buf[i:i + 1] == b"#":
            while i < n and buf[i] not in b"\r\n":
                i += 1
            continue
        j = i
        while j < n and buf[j] not in b" \t\r\n":
            j += 1
        if j == i:
            raise ValueError("truncated PPM header")
        fields.append(int(buf[i:j]))
        i = j
    return fields[0], fields[1], fields[2], i + 1   # one whitespace byte after maxval


def parse_ppm(buf):
    w, h, maxval, off = _ppm_header(buf)
    if maxval != 255:
        raise ValueError(f"PPM maxval {maxval} unsupported (want 255)")
    data = bytes(buf[off:off + w * h * 3])
    return Image(w, h, data)


def read_ppm(path):
    with open(path, "rb") as f:
        return parse_ppm(f.read())


def ppm_bytes(img):
    return b"P6\n%d %d\n255\n" % (img.w, img.h) + img.data


# ---- PNG ---------------------------------------------------------------------

def _chunk(typ, payload):
    c = typ + payload
    return struct.pack(">I", len(payload)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)


def png_bytes(img, level=3):
    """Encode RGB as PNG (filter 0 on every row: fast in Python, lossless)."""
    stride = img.w * 3
    raw = b"".join(b"\x00" + img.data[r * stride:(r + 1) * stride] for r in range(img.h))
    return (PNG_SIG + _chunk(b"IHDR", struct.pack(">IIBBBBB", img.w, img.h, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, level)) + _chunk(b"IEND", b""))


def write_png(img, path, level=3):
    with open(path, "wb") as f:
        f.write(png_bytes(img, level))


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def parse_png(buf):
    """Decode a non-interlaced 8-bit PNG (gray, gray+alpha, RGB, RGBA, palette) to RGB.

    Pure Python: fine for small images and tests; slow (seconds) for a full
    frame with Paeth rows. Callers with ImageMagick available should prefer it
    for large frames (see checks.load_region)."""
    if buf[:8] != PNG_SIG:
        raise ValueError("not a PNG")
    i, idat, plte = 8, [], None
    w = h = depth = ctype = interlace = None
    while i < len(buf):
        n, = struct.unpack(">I", buf[i:i + 4]); typ = buf[i + 4:i + 8]; body = buf[i + 8:i + 8 + n]
        i += 12 + n
        if typ == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif typ == b"PLTE":
            plte = body
        elif typ == b"IDAT":
            idat.append(body)
        elif typ == b"IEND":
            break
    if depth != 8 or interlace:
        raise ValueError(f"PNG depth {depth} interlace {interlace} unsupported")
    bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(b"".join(idat))
    stride = w * bpp
    out = bytearray(h * stride)
    prev = bytearray(stride)
    pos = 0
    for r in range(h):
        ft = raw[pos]; line = bytearray(raw[pos + 1:pos + 1 + stride]); pos += 1 + stride
        if ft == 1:
            for k in range(bpp, stride):
                line[k] = (line[k] + line[k - bpp]) & 255
        elif ft == 2:
            for k in range(stride):
                line[k] = (line[k] + prev[k]) & 255
        elif ft == 3:
            for k in range(stride):
                left = line[k - bpp] if k >= bpp else 0
                line[k] = (line[k] + ((left + prev[k]) >> 1)) & 255
        elif ft == 4:
            for k in range(stride):
                left = line[k - bpp] if k >= bpp else 0
                ul = prev[k - bpp] if k >= bpp else 0
                line[k] = (line[k] + _paeth(left, prev[k], ul)) & 255
        out[r * stride:(r + 1) * stride] = line
        prev = line
    if ctype == 2:
        data = bytes(out)
    elif ctype == 6:
        data = bytes(b for k in range(0, len(out), 4) for b in out[k:k + 3])
    elif ctype == 0:
        data = bytes(b for v in out for b in (v, v, v))
    elif ctype == 4:
        data = bytes(b for k in range(0, len(out), 2) for b in (out[k], out[k], out[k]))
    else:
        data = b"".join(plte[v * 3:v * 3 + 3] for v in out)
    return Image(w, h, data)


def read_png(path):
    with open(path, "rb") as f:
        return parse_png(f.read())


def size(path):
    """(w, h) from a PPM or PNG header, without decoding pixels."""
    with open(path, "rb") as f:
        head = f.read(64)
    if head[:8] == PNG_SIG:
        return struct.unpack(">II", head[16:24])
    if head[:2] == b"P6":
        w, h, _, _ = _ppm_header(head)
        return w, h
    raise ValueError(f"{path}: not a PNG or binary PPM")


def read_image(path):
    with open(path, "rb") as f:
        buf = f.read()
    return parse_png(buf) if buf[:8] == PNG_SIG else parse_ppm(buf)


def main(argv):
    if len(argv) == 3 and argv[0] == "ppm2png":
        write_png(read_ppm(argv[1]), argv[2])
        print(argv[2])
    elif len(argv) == 2 and argv[0] == "size":
        print(*size(argv[1]))
    else:
        sys.exit("usage: imageio.py ppm2png IN.ppm OUT.png | size FILE")


if __name__ == "__main__":
    main(sys.argv[1:])
