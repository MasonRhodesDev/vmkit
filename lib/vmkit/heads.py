"""Multi-head helpers: which GPU device and head a screendump targets, and the
head-aware pointer mapping.

QMP `screendump` takes `device` (the QOM id of the graphics device) and `head`
(the output index on it). Without `device`, QEMU dumps the primary console, which
is head 0 of the first graphics device. A head index past the device's
max_outputs is NOT validated by QEMU 11.1.2: it killed the QEMU process in a test,
so every caller here checks against the head count first.

`vmkit up --gpu-heads N` writes $DIR/gpu.json ({"id", "heads"}); a VM started
some other way can set VMKIT_GPU_ID and VMKIT_GPU_HEADS.

Layout (pointer mapping): the heads' rectangles in the guest compositor's global
coordinate space, "X,Y,W,H" per head, ";" separated, in head order, e.g.
"0,0,1920,1080;1920,0,3440,1440". W,H are the monitor's size as the compositor
sees it (after scale and rotation), and pointer pixels for `--head N` are in that
space. The tablet's absolute axes span the bounding box of all rectangles (what
Wayland compositors do with an unmapped absolute device), so the abs value is
global_pixel / (box_size - 1) * 32767.
"""
import json
import os
import sys


class HeadError(Exception):
    pass


def gpu_json_path(d):
    return os.path.join(d, "gpu.json")


def gpu_info(d):
    """(device id or None, head count) for the VM whose work dir is d."""
    dev, n = os.environ.get("VMKIT_GPU_ID") or None, os.environ.get("VMKIT_GPU_HEADS")
    try:
        with open(gpu_json_path(d)) as f:
            j = json.load(f)
        dev = dev or j.get("id")
        n = n or j.get("heads")
    except (OSError, ValueError):
        pass
    try:
        n = int(n) if n else 1
    except ValueError:
        raise HeadError(f"VMKIT_GPU_HEADS is not a number: {n!r}") from None
    return dev, max(1, n)


def screendump_args(path, device, head):
    """QMP screendump arguments for one head. Head 0 of an unnamed device is the
    default console, so nothing extra is sent for it."""
    a = {"filename": path, "format": "ppm"}
    if device:
        a["device"] = device
    if head:
        if not device:
            raise HeadError("head %d needs the GPU device id (vmkit up --gpu-heads, or VMKIT_GPU_ID)" % head)
        a["head"] = head
    return a


def check_head(d, head):
    dev, n = gpu_info(d)
    if not 0 <= head < n:
        raise HeadError(f"head {head} out of range: this VM has {n} head(s) (0..{n - 1})")
    return dev, n


def parse_heads(spec, n):
    """'all' | 'N' | 'N,M,...' -> sorted list of head indices below n."""
    if spec in ("all", "", None):
        return list(range(n))
    try:
        hs = sorted({int(x) for x in spec.split(",")})
    except ValueError:
        raise HeadError(f"bad head list {spec!r} (all, or 0,1,2)") from None
    for h in hs:
        if not 0 <= h < n:
            raise HeadError(f"head {h} out of range: this VM has {n} head(s)")
    return hs


def parse_layout(spec):
    rects = []
    for part in spec.split(";"):
        try:
            x, y, w, h = (int(v) for v in part.split(","))
        except ValueError:
            raise HeadError(f"bad layout entry {part!r} (X,Y,W,H)") from None
        if w < 1 or h < 1:
            raise HeadError(f"layout entry {part!r}: empty rectangle")
        rects.append((x, y, w, h))
    return rects


def pointer_abs(layout, head, x, y):
    """(abs_x, abs_y) on 0..32767 for pixel (x, y) of head `head`."""
    if not 0 <= head < len(layout):
        raise HeadError(f"head {head} is not in the layout ({len(layout)} entries)")
    hx, hy, _, _ = layout[head]
    x0 = min(r[0] for r in layout); y0 = min(r[1] for r in layout)
    bw = max(r[0] + r[2] for r in layout) - x0
    bh = max(r[1] + r[3] for r in layout) - y0

    def ax(v, n):
        return max(0, min(32767, round(v / max(1, n - 1) * 32767)))
    return ax(hx + x - x0, bw), ax(hy + y - y0, bh)


def main(argv):
    """heads.py info DIR  ->  'DEVICE HEADS' (DEVICE '-' when unnamed); heads.py dump-args DIR PATH HEAD; used by bin/vmkit."""
    if len(argv) == 2 and argv[0] == "info":
        dev, n = gpu_info(argv[1])
        print(f"{dev or '-'} {n}")
        return 0
    if len(argv) == 3 and argv[0] == "head-list":     # DIR SPEC -> space separated head indices
        try:
            print(*parse_heads(argv[2], gpu_info(argv[1])[1]))
        except HeadError as e:
            print(f"vmkit: {e}", file=sys.stderr)
            return 2
        return 0
    if len(argv) >= 5 and argv[0] == "write-heads":   # RUN DEVICE FPS HEAD...
        run, dev, fps = argv[1:4]
        with open(os.path.join(run, "heads.json"), "w") as f:
            json.dump({"device": dev, "fps": float(fps),
                       "heads": [{"head": int(h), "run": f"head{h}"} for h in argv[4:]]}, f, indent=1)
        return 0
    if len(argv) == 4 and argv[0] == "dump-args":    # DIR PATH HEAD -> the screendump arguments, JSON
        try:
            dev, _ = check_head(argv[1], int(argv[3]))
            print(json.dumps(screendump_args(argv[2], dev, int(argv[3]))))
        except HeadError as e:
            print(f"vmkit: {e}", file=sys.stderr)
            return 2
        return 0
    print(main.__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
