"""Model-free frame checks (hypr-qa scenario-schema "Checks").

  pixel     region, color '#rrggbb', tolerance (0-255, default 16), min_fraction (default 1.0):
            passes when >= min_fraction of the region's pixels are within
            `tolerance` of color on EVERY channel (max-abs per channel).
  ocr       region (optional), text (case-insensitive substring, whitespace-
            normalized) or regex (re.search on the raw OCR text): tesseract.
  template  image, region or near=[x,y]+radius, threshold (0-1 similarity,
            default 0.9): ImageMagick subimage search (NCC; RMSE for a flat
            template, where NCC is undefined).
  changed   region, min_px (default 50), fuzz (percent, default 5): the region
            differs from the baseline frame (the last frame before the anchor)
            in >= min_px pixels. A pixel differs when the Euclidean distance of
            its RGB channels exceeds fuzz% of 255.

Any check takes `not` (invert pass/fail; an error stays an error). A check on
an inactive frame (QEMU's "Display output is not active" placeholder) is an
error.

Regions are [x, y, w, h] in framebuffer pixels; negative x/y count from the
right/bottom edge; w/h of 0 mean "to the edge". A region outside the frame is
an error.

Frames are decoded with ImageMagick (`magick ... rgb:-`, crop first) when it
is installed, else in pure Python (slow on large PNGs; fine for PPM and for the
small synthetic images the unit tests use). VMKIT_PURE_PY=1 forces Python.
"""
import os
import re
import shutil
import subprocess
import tempfile

from . import imageio

MAGICK = None if os.environ.get("VMKIT_PURE_PY") else shutil.which("magick")


class CheckError(Exception):
    pass


# ---- regions -----------------------------------------------------------------

def parse_region(v):
    """'x,y,w,h' | [x,y,w,h] | None -> list or None."""
    if v is None or v == "":
        return None
    if isinstance(v, str):
        parts = [p for p in re.split(r"[,\s]+", v.strip().strip("[]")) if p]
        v = parts
    if len(v) != 4:
        raise CheckError(f"region needs 4 numbers [x, y, w, h], got {v!r}")
    try:
        return [int(x) for x in v]
    except (TypeError, ValueError):
        raise CheckError(f"region must be integers, got {v!r}") from None


def resolve_region(region, W, H):
    """Schema region -> absolute (x, y, w, h) inside a WxH frame."""
    if region is None:
        return (0, 0, W, H)
    x, y, w, h = region
    if x < 0:
        x += W
    if y < 0:
        y += H
    if w == 0:
        w = W - x
    if h == 0:
        h = H - y
    if w < 0 or h < 0:
        raise CheckError(f"region {region} has a negative size")
    if x < 0 or y < 0 or x + w > W or y + h > H or w == 0 or h == 0:
        raise CheckError(f"region {region} -> {x},{y},{w}x{h} is outside the {W}x{H} frame")
    return (x, y, w, h)


def near_region(near, radius, tw, th, W, H):
    """Search area for a template centred near (x, y) within radius, clamped to the frame."""
    cx, cy = int(near[0]), int(near[1]); r = int(radius)
    x0 = max(0, cx - r - tw // 2); y0 = max(0, cy - r - th // 2)
    x1 = min(W, cx + r + (tw - tw // 2)); y1 = min(H, cy + r + (th - th // 2))
    if x1 - x0 < tw or y1 - y0 < th:
        raise CheckError(f"near {near} radius {radius} leaves no room for a {tw}x{th} template")
    return (x0, y0, x1 - x0, y1 - y0)


def parse_color(c):
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(c).strip())
    if not m:
        raise CheckError(f"color must be #rrggbb, got {c!r}")
    v = int(m.group(1), 16)
    return (v >> 16) & 255, (v >> 8) & 255, v & 255


# ---- image loading -----------------------------------------------------------

def load_region(path, region=None):
    """Decode `path` and return (Image of the resolved region, (x,y,w,h), (W,H))."""
    try:
        W, H = imageio.size(path)
    except (OSError, ValueError) as e:
        raise CheckError(f"cannot read frame {path}: {e}") from None
    box = resolve_region(region, W, H)
    x, y, w, h = box
    with open(path, "rb") as f:
        head = f.read(2)
    if head == b"P6" or not MAGICK:
        img = imageio.read_image(path).crop(x, y, w, h)
    else:
        r = subprocess.run([MAGICK, path, "-crop", f"{w}x{h}+{x}+{y}", "+repage",
                            "-alpha", "off", "-depth", "8", "rgb:-"], capture_output=True)
        if r.returncode != 0 or len(r.stdout) != w * h * 3:
            raise CheckError(f"magick could not decode {path}: {r.stderr.decode(errors='replace').strip()}")
        img = imageio.Image(w, h, r.stdout)
    return img, box, (W, H)


# ---- checks ------------------------------------------------------------------
# Each returns (passed: bool, value: dict). Errors raise CheckError.

def _band_table(c, tol):
    return bytes(1 if abs(v - c) <= tol else 0 for v in range(256))


def pixel(path, region, color, tolerance=16, min_fraction=1.0):
    img, box, _ = load_region(path, region)
    r, g, b = parse_color(color)
    tol = int(tolerance)
    if not 0 <= tol <= 255:
        raise CheckError("tolerance must be 0-255")
    d = img.data
    # per-channel 0/1 masks via translate, AND-ed as big ints: C speed
    mr = int.from_bytes(d[0::3].translate(_band_table(r, tol)), "big")
    mg = int.from_bytes(d[1::3].translate(_band_table(g, tol)), "big")
    mb = int.from_bytes(d[2::3].translate(_band_table(b, tol)), "big")
    match = (mr & mg & mb).bit_count()
    total = img.w * img.h
    frac = match / total
    return frac >= float(min_fraction), {"fraction": round(frac, 4), "matched_px": match, "total_px": total,
                                         "region": list(box)}


def changed(path, baseline_path, region, min_px=50, fuzz=5):
    try:
        wh, bwh = imageio.size(path), imageio.size(baseline_path)
    except (OSError, ValueError) as e:
        raise CheckError(f"cannot read frame: {e}") from None
    if wh != bwh:   # a resolution change is a change everywhere
        box = resolve_region(region, *wh)
        return True, {"changed_px": box[2] * box[3], "region": list(box),
                      "note": f"resolution changed {bwh[0]}x{bwh[1]} -> {wh[0]}x{wh[1]}"}
    img, box, _ = load_region(path, region)
    base, _, _ = load_region(baseline_path, region)
    lim = (float(fuzz) / 100.0 * 255.0) ** 2
    a, b = img.data, base.data
    stride = img.w * 3
    n = 0
    for r in range(img.h):
        s = r * stride
        ra, rb = a[s:s + stride], b[s:s + stride]
        if ra == rb:
            continue
        for k in range(0, stride, 3):
            if ra[k:k + 3] != rb[k:k + 3]:
                dr = ra[k] - rb[k]; dg = ra[k + 1] - rb[k + 1]; db = ra[k + 2] - rb[k + 2]
                if dr * dr + dg * dg + db * db > lim:
                    n += 1
    return n >= int(min_px), {"changed_px": n, "region": list(box)}


def _norm(s):
    return re.sub(r"\s+", " ", s).strip().lower()


def ocr_text(path, region=None, scale=2, psm=None):
    tess = shutil.which("tesseract")
    if not tess:
        raise CheckError("ocr check needs tesseract on the host")
    img, box, (W, H) = load_region(path, region)
    psm = psm or (6 if region is not None else 11)
    with tempfile.TemporaryDirectory(prefix="vmkit-ocr-") as td:
        src = os.path.join(td, "in.png")
        if MAGICK and scale and scale != 1:
            ppm = os.path.join(td, "in.ppm")
            with open(ppm, "wb") as f:
                f.write(imageio.ppm_bytes(img))
            r = subprocess.run([MAGICK, ppm, "-filter", "Lanczos", "-resize", f"{int(scale * 100)}%", src],
                               capture_output=True)
            if r.returncode != 0:
                raise CheckError(f"magick resize failed: {r.stderr.decode(errors='replace').strip()}")
        else:
            imageio.write_png(img, src, level=1)
        # One OpenMP thread (see OMP_THREAD_LIMIT in bin/vmkit): many threads
        # thrash on a busy host and make each pass take tens of seconds.
        env = dict(os.environ)
        env.setdefault("OMP_THREAD_LIMIT", "1")
        r = subprocess.run([tess, src, "-", "--psm", str(psm)], capture_output=True, env=env)
        if r.returncode != 0:
            raise CheckError(f"tesseract failed: {r.stderr.decode(errors='replace').strip()[-300:]}")
    return r.stdout.decode(errors="replace"), box


def ocr(path, region=None, text=None, regex=None, scale=2, psm=None):
    if (text is None) == (regex is None):
        raise CheckError("ocr check needs exactly one of text or regex")
    out, box = ocr_text(path, region, scale, psm)
    if text is not None:
        ok = _norm(text) in _norm(out)
    else:
        try:
            ok = re.search(regex, out) is not None
        except re.error as e:
            raise CheckError(f"bad regex {regex!r}: {e}") from None
    return ok, {"text": out.strip()[:500], "region": list(box)}


_SEARCH = re.compile(r"([-+0-9.eE]+|nan|inf)\s+\(([-+0-9.eE]+|nan|inf)\)\s+@\s+(-?\d+),(-?\d+)")
_NCC_POLARITY = {}


def _magick_compare(args):
    r = subprocess.run([MAGICK, "compare"] + args + ["null:"], capture_output=True, text=True)
    return r.stderr + r.stdout


def _ncc_is_dissimilarity(tmpl_path):
    """IM >= 7.1.1 reports NCC as a dissimilarity (0 = identical); older IM as a
    correlation (1 = identical). Ask IM once: compare the template with itself."""
    if "v" not in _NCC_POLARITY:
        out = _magick_compare(["-metric", "NCC", tmpl_path, tmpl_path])
        m = re.search(r"\(([-+0-9.eE]+)\)", out)
        _NCC_POLARITY["v"] = (m is None) or float(m.group(1)) < 0.5
    return _NCC_POLARITY["v"]


def template(path, image, region=None, near=None, radius=None, threshold=0.9, metric=None):
    if not MAGICK:
        raise CheckError("template check needs ImageMagick (magick) on the host")
    if not os.path.exists(image):
        raise CheckError(f"template image not found: {image}")
    tw, th = imageio.size(image)
    W, H = imageio.size(path)
    if near is not None:
        if region is not None:
            raise CheckError("template: give region or near+radius, not both")
        if radius is None:
            raise CheckError("template: near needs radius")
        box = near_region(near, radius, tw, th, W, H)
        region = list(box)
    elif region is None:
        raise CheckError("template check needs region or near+radius (a full-frame search is too slow)")
    area, box, _ = load_region(path, region)
    if area.w < tw or area.h < th:
        raise CheckError(f"search area {area.w}x{area.h} is smaller than the {tw}x{th} template")
    tmpl, _, _ = load_region(image, None)      # any PNG (16-bit, RGBA, ...) via magick
    d = tmpl.data
    flat = len(set(d[0::3])) == 1 and len(set(d[1::3])) == 1 and len(set(d[2::3])) == 1
    metric = (metric or ("rmse" if flat else "ncc")).lower()
    with tempfile.TemporaryDirectory(prefix="vmkit-tmpl-") as td:
        ap = os.path.join(td, "area.ppm")
        with open(ap, "wb") as f:
            f.write(imageio.ppm_bytes(area))
        tp = os.path.join(td, "tmpl.ppm")
        with open(tp, "wb") as f:
            f.write(imageio.ppm_bytes(tmpl))
        out = _magick_compare(["-metric", metric.upper(), "-subimage-search", ap, tp])
        m = _SEARCH.search(out)
        if not m:
            raise CheckError(f"could not parse magick compare output: {out.strip()[:300]}")
        norm = float(m.group(2))
        if metric == "ncc":
            sim = 1.0 - norm if _ncc_is_dissimilarity(tp) else norm
        else:
            sim = 1.0 - norm
    x, y = box[0] + int(m.group(3)), box[1] + int(m.group(4))
    return sim >= float(threshold), {"similarity": round(sim, 4), "x": x, "y": y, "w": tw, "h": th,
                                     "metric": metric, "region": list(box)}


CHECKS = ("pixel", "ocr", "template", "changed")


def run_check(kind, frame_path, spec, baseline_path=None, base_dir="."):
    """Evaluate one check on one frame. spec: the expectation's fields.
    Returns {"status": pass|fail|error, "passed", "value"?, "error"?}."""
    try:
        region = parse_region(spec.get("region"))
        if kind == "pixel":
            if "color" not in spec:
                raise CheckError("pixel check needs color")
            ok, val = pixel(frame_path, region, spec["color"], spec.get("tolerance", 16),
                            spec.get("min_fraction", 1.0))
        elif kind == "changed":
            if baseline_path is None:
                raise CheckError("changed check needs a baseline frame (none before the anchor)")
            ok, val = changed(frame_path, baseline_path, region, spec.get("min_px", 50), spec.get("fuzz", 5))
        elif kind == "ocr":
            ok, val = ocr(frame_path, region, spec.get("text"), spec.get("regex"),
                          spec.get("scale", 2), spec.get("psm"))
        elif kind == "template":
            img = spec.get("image")
            if not img:
                raise CheckError("template check needs image")
            img = img if os.path.isabs(img) else os.path.join(base_dir, img)
            ok, val = template(frame_path, img, region, spec.get("near"), spec.get("radius"),
                               spec.get("threshold", 0.9), spec.get("metric"))
        else:
            raise CheckError(f"unknown check {kind!r} (want one of {', '.join(CHECKS)})")
    except (CheckError, ValueError, TypeError, OSError) as e:   # bad field types, unreadable files
        return {"status": "error", "passed": None, "error": str(e)}
    if spec.get("not"):
        ok = not ok
    return {"status": "pass" if ok else "fail", "passed": ok, "value": val}
