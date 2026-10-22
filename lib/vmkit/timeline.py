"""Frame selection against a recording's timeline (hypr-qa scenario-schema "Timing").

All times are host CLOCK_MONOTONIC ns: frames.jsonl `t` (screendump reply) and
actions.jsonl `t_send`/`t_ack`.

Rules:
  * a point offset ("+300ms"): the FIRST frame with t >= anchor + offset.
  * a window ("+50ms..+300ms"): EVERY frame with anchor+a <= t <= anchor+b.
  * a window with no frame in it is an error, not a pass; so is a point with
    no frame at or after it, and any offset that falls outside the recording
    (before the first frame's request, or after the last frame).
  * sampling gaps are errors too: a window that overlaps a gap in gaps.jsonl
    (a run of screendump errors: no frame exists for it, even when frames lie
    on both sides), and a point whose frame is more than
    max(2 x the recording's median frame interval, 100 ms) after it (an error
    gap or an ordinary slow interval).
  * the baseline for `changed` is the last frame with t < anchor.

CLI (`vmkit frames`):
  timeline.py [--dir VMKIT_DIR] [--run RUN] (--at T_NS | --action '#N'|STEP [--anchor send|ack])
              --offset SPEC[,SPEC...] [--baseline] [--json]
Prints one line per selected frame: spec, n, t, ms from anchor, path[, inactive].
Exit 0, or 2 when an offset cannot be resolved (message on stderr).
"""
import json
import os
import re
import sys

UNITS = {"ns": 1, "us": 1_000, "ms": 1_000_000, "s": 1_000_000_000}
_OFF = re.compile(r"^\s*([+-]?)\s*(\d+(?:\.\d+)?)\s*(ns|us|ms|s)?\s*$")


class TimelineError(Exception):
    pass


def parse_offset(s):
    """'+100ms' | '-1.5s' | '250' (ms) -> signed ns."""
    m = _OFF.match(s)
    if not m:
        raise TimelineError(f"bad offset {s!r} (want e.g. +100ms, -50ms, +1.5s)")
    sign, num, unit = m.groups()
    v = round(float(num) * UNITS[unit or "ms"])
    return -v if sign == "-" else v


def parse_at(spec):
    """'+300ms' -> ('point', ns); '+50ms..+300ms' -> ('window', a, b)."""
    if ".." in spec:
        a, b = spec.split("..", 1)
        a, b = parse_offset(a), parse_offset(b)
        if b < a:
            raise TimelineError(f"window {spec!r} ends before it starts")
        return ("window", a, b)
    return ("point", parse_offset(spec))


def split_specs(s):
    return [x.strip() for x in s.split(",") if x.strip()]


def load_jsonl(path):
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


class Frames(list):
    """A recording's frames (sorted by t), with its sampling gaps (gaps.jsonl rows)."""
    gaps = ()


def load_frames(run):
    fr = Frames(load_jsonl(os.path.join(run, "frames.jsonl")))
    fr.sort(key=lambda f: f["t"])
    p = os.path.join(run, "gaps.jsonl")
    fr.gaps = load_jsonl(p) if os.path.exists(p) else []
    return fr


LATE_FLOOR_NS = 100 * 1_000_000


def late_limit(frames):
    """How far after a point its frame may be: max(2 x median interval, 100 ms)."""
    iv = sorted(frames[i]["t"] - frames[i - 1]["t"] for i in range(1, len(frames)))
    return max(2 * iv[len(iv) // 2] if iv else 0, LATE_FLOOR_NS)


def load_actions(run):
    p = os.path.join(run, "actions.jsonl")
    return load_jsonl(p) if os.path.exists(p) else []


def frame_path(run, n):
    return os.path.join(run, "frames", f"{n:06d}.png")


def _coverage(frames, lo, hi, what):
    if not frames:
        raise TimelineError("the recording has no frames")
    start = frames[0].get("t_req", frames[0]["t"])
    if lo < start:
        raise TimelineError(f"{what} starts {(start - lo) / 1e6:.1f} ms before the recording's first frame")
    if hi > frames[-1]["t"]:
        raise TimelineError(f"{what} ends {(hi - frames[-1]['t']) / 1e6:.1f} ms after the recording's last frame")


def select_point(frames, t):
    """First frame with f.t >= t."""
    _coverage(frames, t, t, "offset")
    for f in frames:
        if f["t"] >= t:
            limit = late_limit(frames)
            if f["t"] - t > limit:
                raise TimelineError(f"the first frame after the offset is {(f['t'] - t) / 1e6:.1f} ms late "
                                    f"(limit {limit / 1e6:.0f} ms): sampling gap")
            return f
    raise TimelineError("no frame at or after the offset")


def select_window(frames, t0, t1):
    """Every frame with t0 <= f.t <= t1; empty is an error."""
    _coverage(frames, t0, t1, "window")
    for g in getattr(frames, "gaps", ()):
        if g["t_from"] <= t1 and g["t_to"] >= t0:
            raise TimelineError(f"the window overlaps a sampling gap ({(g['t_to'] - g['t_from']) / 1e6:.1f} ms, "
                                f"{g.get('errors', '?')} errors: {g.get('reason', '')})")
    got = [f for f in frames if t0 <= f["t"] <= t1]
    if not got:
        raise TimelineError(f"no frame in the window ({(t1 - t0) / 1e6:.1f} ms wide)")
    return got


def select(frames, anchor, spec):
    kind = parse_at(spec)
    if kind[0] == "point":
        return [select_point(frames, anchor + kind[1])]
    return select_window(frames, anchor + kind[1], anchor + kind[2])


def baseline(frames, anchor):
    """Last frame with f.t < anchor (the 'before' frame for `changed`)."""
    prev = None
    for f in frames:
        if f["t"] >= anchor:
            break
        prev = f
    if prev is None:
        raise TimelineError("no frame before the anchor (recording started after it)")
    return prev


def find_action(actions, ref):
    """ref: an int or "#N" = index into actions.jsonl (negative from the end);
    any other string = the step label, exactly (so a label may be "2"). A label
    on more than one action is ambiguous: an error, as is no match."""
    if isinstance(ref, str) and not ref.startswith("#"):
        hits = [a for a in actions if a.get("step") == ref]
        if not hits:
            raise TimelineError(f"no action with step {ref!r} (an index is written #N, e.g. #0 or #-1)")
        if len(hits) > 1:
            raise TimelineError(f"step {ref!r} is ambiguous: {len(hits)} actions carry it (use #N)")
        return hits[0]
    try:
        i = int(ref[1:] if isinstance(ref, str) else ref)
    except (TypeError, ValueError):
        raise TimelineError(f"bad action index {ref!r} (want #N, e.g. #0 or #-1)") from None
    if not actions:
        raise TimelineError("no actions recorded")
    try:
        return actions[i]
    except IndexError:
        raise TimelineError(f"action index {i} out of range ({len(actions)} actions)") from None


def anchor_of(action, anchor="send"):
    key = {"send": "t_send", "ack": "t_ack"}[anchor]
    if key not in action:
        raise TimelineError(f"action has no {key} (not a timestamped action)")
    return action[key]


def head_run(run, head=0):
    """A multi-head recording's parent dir (heads.json) -> head N's run dir; any
    other dir is returned as it is."""
    try:
        with open(os.path.join(run, "heads.json")) as f:
            hs = json.load(f)["heads"]
    except (OSError, ValueError, KeyError):
        return run
    for h in hs:
        if h["head"] == head:
            return os.path.join(run, h["run"])
    raise TimelineError(f"{run} has no head {head} (recorded: {[h['head'] for h in hs]})")


def default_run(vmkit_dir):
    try:
        run = open(os.path.join(vmkit_dir, "record.run")).read().strip()
        if run:
            return head_run(run)
    except OSError:
        pass
    runs = os.path.join(vmkit_dir, "runs")
    cands = sorted(d for d in (os.listdir(runs) if os.path.isdir(runs) else [])
                   if os.path.exists(os.path.join(runs, d, "frames.jsonl"))
                   or os.path.exists(os.path.join(runs, d, "heads.json")))
    if not cands:
        raise TimelineError(f"no recording under {runs}; pass --run")
    return head_run(os.path.join(runs, cands[-1]))


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="vmkit frames")
    ap.add_argument("--dir", default=os.environ.get("VMKIT_DIR", ".vmkit"))
    ap.add_argument("--run")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--at", type=int, help="anchor, CLOCK_MONOTONIC ns")
    g.add_argument("--action", help="anchor on an action: #N = index in actions.jsonl (#-1 = last; quote "
                                    "it in a shell), anything else = its step label (exact, unique)")
    ap.add_argument("--anchor", choices=["send", "ack"], default="send")
    ap.add_argument("--offset", required=True, help="comma-separated: +100ms or +0ms..+400ms")
    ap.add_argument("--baseline", action="store_true", help="also print the last frame before the anchor")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        run = head_run(a.run) if a.run else default_run(a.dir)
        frames = load_frames(run)
        anchor = a.at if a.at is not None else anchor_of(find_action(load_actions(run), a.action), a.anchor)
        rows = []
        if a.baseline:
            rows.append(("baseline", baseline(frames, anchor)))
        for spec in split_specs(a.offset):
            for f in select(frames, anchor, spec):
                rows.append((spec, f))
    except (TimelineError, OSError, ValueError) as e:
        print(f"vmkit frames: {e}", file=sys.stderr)
        return 2
    for spec, f in rows:
        rel = (f["t"] - anchor) / 1e6
        path = frame_path(run, f["n"])
        if a.json:
            print(json.dumps({"spec": spec, "n": f["n"], "t": f["t"], "t_req": f.get("t_req"),
                              "rel_ms": round(rel, 2), "path": path, "inactive": f.get("inactive", False)}))
        else:
            print(f"{spec}\t{f['n']}\t{f['t']}\t{rel:+.1f}ms\t{path}" + ("\tinactive" if f.get("inactive") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
