"""vmkit check: evaluate model-free checks on recorded frames.

One check on explicit frames:
  vmkit check pixel    --frame F --region X,Y,W,H --color '#rrggbb' [--tolerance 16] [--min-fraction 1.0]
  vmkit check changed  --frame F --baseline B [--region ..] [--min-px 50] [--fuzz 5]
  vmkit check ocr      --frame F [--region ..] (--text T | --regex R) [--psm N]
  vmkit check template --frame F --image T.png (--region .. | --near X,Y --radius R) [--threshold 0.9]

The same, with frames chosen from a recording by the Timing rules (instead of --frame):
  ... [--run RUN] (--at T_NS | --action N|STEP [--anchor send|ack]) --offset SPEC [--mode any|all]
  (`changed` then uses the last frame before the anchor as its baseline. A window
  defaults to --mode any, or to all with --not: "in no frame of the window".)

Any check: --not. Prints a JSON result; exit 0 pass, 1 fail, 2 error.

A batch of expectations (what a scenario runner calls):
  vmkit check batch [--run RUN] EXPECT.json [-o RESULTS.json]
EXPECT.json is a list of objects:
  {"id": "...", "action": N|"step", "anchor": "send"|"ack", "at": "+50ms..+300ms",
   "mode": "any"|"all", "check": "pixel", ...check fields..., "not": false}
(or "t": T_NS instead of "action"). Relative template paths resolve against
EXPECT.json's directory. Writes results.json (default <run>/results.json):
per expectation its status (pass/fail/error), the frames used with their
per-frame status and measured values. Exit 0 all pass, 1 a check failed,
2 an error (e.g. an unresolvable window), the schema's exit codes.
"""
import argparse
import json
import os
import sys

from . import checks as C
from . import timeline as T


def aggregate(per_frame, mode):
    st = [p["status"] for p in per_frame]
    if mode == "all":
        if "error" in st:
            return "error"
        return "pass" if all(s == "pass" for s in st) else "fail"
    if "pass" in st:
        return "pass"
    return "error" if "error" in st else "fail"


def evaluate(run, frames, actions, exp, base_dir="."):
    """Evaluate one expectation against a recording. Never raises."""
    kind = exp.get("check")
    at = exp.get("at", "+0ms")
    anchor_kind = exp.get("anchor", "send")
    out = {"id": exp.get("id"), "check": kind, "at": at, "anchor": anchor_kind, "not": bool(exp.get("not")),
           "action": exp.get("action"), "frames": []}
    try:
        if "t" in exp:
            anchor = int(exp["t"])
        else:
            act = T.find_action(actions, exp.get("action", -1))
            anchor = T.anchor_of(act, anchor_kind)
            out["action_cmd"] = act.get("cmd")
            if not act.get("ok", True):
                raise T.TimelineError(f"the action failed: {act.get('error')}")
        out["anchor_t"] = anchor
        is_window = T.parse_at(at)[0] == "window"
        # schema v0.3: `not` on a window means "in no frame", so it defaults to all
        mode = exp.get("mode", "any" if is_window and not exp.get("not") else "all")
        out["mode"] = mode
        sel = T.select(frames, anchor, at)
        base = None
        if kind == "changed":
            bf = T.baseline(frames, anchor)
            base = T.frame_path(run, bf["n"])
            out["baseline"] = {"n": bf["n"], "t": bf["t"], "path": base}
            if bf.get("inactive"):
                raise T.TimelineError("the baseline frame is inactive (display output off)")
    except (T.TimelineError, ValueError, KeyError) as e:
        out.update(status="error", error=str(e))
        return out
    for f in sel:
        path = T.frame_path(run, f["n"])
        row = {"n": f["n"], "t": f["t"], "rel_ms": round((f["t"] - anchor) / 1e6, 2), "path": path}
        if f.get("inactive"):
            row.update(status="error", passed=None, error="inactive frame (display output not active)")
        else:
            row.update(C.run_check(kind, path, exp, base, base_dir))
        out["frames"].append(row)
    out["status"] = aggregate(out["frames"], mode)
    if out["status"] == "error":
        errs = [r.get("error") for r in out["frames"] if r.get("error")]
        out["error"] = errs[0] if errs else "error"
    return out


def exit_code(statuses):
    if "error" in statuses:
        return 2
    if "fail" in statuses:
        return 1
    return 0


def _spec_from_args(a):
    spec = {"check": a.kind}
    for k in ("region", "color", "tolerance", "min_fraction", "min_px", "fuzz", "text", "regex",
              "image", "radius", "threshold", "metric", "psm"):
        v = getattr(a, k, None)
        if v is not None:
            spec[k] = v
    if a.near:
        spec["near"] = [int(x) for x in a.near.split(",")]
    if a.region is not None:
        spec["region"] = C.parse_region(a.region)
    if a.do_not:
        spec["not"] = True
    return spec


def main_single(a):
    spec = _spec_from_args(a)
    if a.frame:
        if a.kind == "changed" and not a.baseline:
            res = {"status": "error", "error": "changed needs --baseline with --frame"}
        else:
            res = C.run_check(a.kind, a.frame, spec, a.baseline, os.getcwd())
            res.update(check=a.kind, frame=a.frame)
    else:
        run = a.run or T.default_run(a.dir)
        spec["at"] = a.offset or "+0ms"
        if a.mode:
            spec["mode"] = a.mode
        spec["anchor"] = a.anchor
        if a.at is not None:
            spec["t"] = a.at
        else:
            spec["action"] = a.action if a.action is not None else -1
        res = evaluate(run, T.load_frames(run), T.load_actions(run), spec, os.getcwd())
    print(json.dumps(res, indent=1))
    return exit_code([res["status"]])


def main_batch(argv):
    ap = argparse.ArgumentParser(prog="vmkit check batch")
    ap.add_argument("--dir", default=os.environ.get("VMKIT_DIR", ".vmkit"))
    ap.add_argument("--run")
    ap.add_argument("expect")
    ap.add_argument("-o", "--out")
    a = ap.parse_args(argv)
    run = a.run or T.default_run(a.dir)
    with open(a.expect) as f:
        exps = json.load(f)
    if isinstance(exps, dict):
        exps = exps.get("expect", [])
    frames, actions = T.load_frames(run), T.load_actions(run)
    base_dir = os.path.dirname(os.path.abspath(a.expect))
    results = [evaluate(run, frames, actions, e, base_dir) for e in exps]
    out = a.out or os.path.join(run, "results.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=1)
    for r in results:
        line = f"{r['status']:5}  {r.get('id') or ''}  {r['check']} {r['at']}"
        if r.get("error"):
            line += f"  ({r['error']})"
        print(line)
    print(out)
    return exit_code([r["status"] for r in results])


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "batch":
        try:
            return main_batch(argv[1:])
        except (OSError, ValueError, T.TimelineError) as e:
            print(f"vmkit check batch: {e}", file=sys.stderr)
            return 2
    ap = argparse.ArgumentParser(prog="vmkit check", description="model-free frame checks")
    ap.add_argument("kind", choices=C.CHECKS)
    ap.add_argument("--dir", default=os.environ.get("VMKIT_DIR", ".vmkit"))
    ap.add_argument("--frame"); ap.add_argument("--baseline")
    ap.add_argument("--run")
    ap.add_argument("--at", type=int); ap.add_argument("--action")
    ap.add_argument("--anchor", choices=["send", "ack"], default="send")
    ap.add_argument("--offset"); ap.add_argument("--mode", choices=["any", "all"])
    ap.add_argument("--region"); ap.add_argument("--color")
    ap.add_argument("--tolerance", type=int); ap.add_argument("--min-fraction", dest="min_fraction", type=float)
    ap.add_argument("--min-px", dest="min_px", type=int); ap.add_argument("--fuzz", type=float)
    ap.add_argument("--text"); ap.add_argument("--regex"); ap.add_argument("--psm", type=int)
    ap.add_argument("--image"); ap.add_argument("--near"); ap.add_argument("--radius", type=int)
    ap.add_argument("--threshold", type=float); ap.add_argument("--metric", choices=["ncc", "rmse"])
    ap.add_argument("--not", dest="do_not", action="store_true")
    a = ap.parse_args(argv)
    try:
        return main_single(a)
    except (OSError, ValueError, T.TimelineError, C.CheckError) as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
