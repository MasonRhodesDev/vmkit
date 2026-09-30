"""Write report.html for a recording run dir.

Inputs (all in the run dir): frames.jsonl, frames/, actions.jsonl,
summary.json (from the recorder), segments.jsonl, and optionally a results
JSON (default results.json, as written by `vmkit check batch`).

The page is plain HTML with inline CSS and no scripts or external assets; it
references the run's own frames/*.png and video*.mkv by relative path, so keep
it in the run dir. Per action it shows a contact sheet: the last frame before
t_send, then the first frames at or after +0/+50/+100/+200/+400/+800 ms (and
every frame a check used), with each check's region outlined in its status
color. At the top: the pass/fail summary, the recorder's fps/interval numbers
and the degraded flag, and the video links.

CLI: report.py RUN [--results FILE] [-o OUT]   (vmkit report [RUN] ...)
"""
import argparse
import html
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vmkit import timeline as T  # noqa: E402
from vmkit import imageio  # noqa: E402

SHEET_OFFSETS_MS = (0, 50, 100, 200, 400, 800)
COLORS = {"pass": "#1a9e4b", "fail": "#d23f31", "error": "#c77c00", None: "#3d7bd9"}

CSS = """
:root{--bg:#fafafa;--fg:#1b1b1b;--mut:#666;--card:#fff;--line:#ddd}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#e8e8e8;--mut:#9a9a9a;--card:#1e1e1e;--line:#333}}
body{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0;padding:16px 20px 40px}
h1{font-size:20px;margin:0 0 4px} h2{font-size:16px;margin:28px 0 8px} h3{font-size:14px;margin:0 0 6px}
.mut{color:var(--mut)} code{font:12px ui-monospace,monospace}
table{border-collapse:collapse;margin:6px 0} td,th{border-bottom:1px solid var(--line);padding:4px 10px;text-align:left;vertical-align:top}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;color:#fff;font-weight:600;font-size:12px}
.pass{background:#1a9e4b}.fail{background:#d23f31}.error{background:#c77c00}.info{background:#3d7bd9}
.deg{background:#d23f31}.ok{background:#1a9e4b}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin:12px 0}
.sheet{display:flex;flex-wrap:wrap;gap:8px}
.th{width:300px;position:relative} .th img{width:100%;display:block;border:1px solid var(--line)}
.fr{position:relative;line-height:0} .fr svg{position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none}
.th .cap{font:11px ui-monospace,monospace;color:var(--mut);margin-top:2px}
.th.inactive img{opacity:.45} .th.used img{outline:2px solid #3d7bd9}
"""


def esc(s):
    return html.escape(str(s), quote=True)


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def frame_size(run, n, cache={}):
    p = T.frame_path(run, n)
    if p not in cache:
        try:
            cache[p] = imageio.size(p)
        except (OSError, ValueError):
            cache[p] = None
    return cache[p]


def thumb(run, f, anchor, label, rects, used=False):
    """One contact-sheet cell. rects: [(x, y, w, h, color, title)]."""
    rel = f"frames/{f['n']:06d}.png"
    wh = frame_size(run, f["n"])
    svg = ""
    if wh and rects:
        W, H = wh
        body = "".join(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="none" stroke="{c}" '
            f'stroke-width="{max(2, W // 300)}"><title>{esc(t)}</title></rect>' for x, y, w, h, c, t in rects)
        svg = f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="none">{body}</svg>'
    cls = "th" + (" inactive" if f.get("inactive") else "") + (" used" if used else "")
    rel_ms = (f["t"] - anchor) / 1e6
    cap = f"{esc(label)} &middot; n={f['n']} &middot; {rel_ms:+.0f} ms" + (" &middot; INACTIVE" if f.get("inactive") else "")
    return (f'<div class="{cls}"><div class="fr"><a href="{rel}"><img loading="lazy" src="{rel}" '
            f'alt="frame {f["n"]}"></a>{svg}</div><div class="cap">{cap}</div></div>')


def result_rects(r):
    """Regions a result checked (and a template's match box)."""
    out = []
    color = COLORS.get(r.get("status"), COLORS[None])
    title = f"{r.get('id') or r.get('check')}: {r.get('status')}"
    seen = set()
    for fr in r.get("frames", []):
        v = fr.get("value") or {}
        reg = v.get("region")
        if reg and tuple(reg) not in seen:
            seen.add(tuple(reg))
            out.append((*reg, color, title))
        if r.get("check") == "template" and "x" in v:
            out.append((v["x"], v["y"], v["w"], v["h"], "#3d7bd9", title + " (match)"))
    return out


def action_index(actions, r):
    if r.get("action") is None:
        return None
    try:
        a = T.find_action(actions, r["action"])
    except T.TimelineError:
        return None
    return next((i for i, x in enumerate(actions) if x is a), None)


def summary_block(S, segs):
    if not S:
        return '<p class="mut">No summary.json (recording not stopped cleanly?)</p>'
    iv = S.get("interval_ms", {}); c = S.get("screendump_call_ms", {})
    deg = S.get("degraded")
    badge = f'<span class="badge {"deg" if deg else "ok"}">degraded={str(bool(deg)).lower()}</span>'
    rows = [("frames", S.get("frames")), ("fps", f"{S.get('fps')} (target {S.get('target_fps')})"),
            ("interval ms p50 / p95 / max", f"{iv.get('p50')} / {iv.get('p95')} / {iv.get('max')}"),
            ("screendump call ms p50 / p95 / max", f"{c.get('p50')} / {c.get('p95')} / {c.get('max')}"),
            (f"gaps over {S.get('degraded_threshold_ms')} ms", S.get("gaps_over_threshold")),
            ("inactive frames", S.get("inactive_frames")),
            ("encoder backpressure ms", S.get("encoder_backpressure_ms")),
            ("recorder nice", S.get("nice")),
            ("integrity", json.dumps(S.get("integrity")))]
    if S.get("error"):
        rows.append(("ERROR", S["error"]))
    t = "".join(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in rows)
    vids = " ".join(f'<a href="{esc(s["video"])}">{esc(s["video"])}</a> <span class="mut">({s["w"]}x{s["h"]}, '
                    f'from n={s["first_n"]})</span>' for s in segs) or '<span class="mut">none</span>'
    return (f"<p>{badge}</p><table>{t}</table>"
            f"<p>Video: {vids}<br><span class=\"mut\">Lossless ffv1 in Matroska: browsers will not play it; "
            f"open it in mpv/VLC, or <code>ffmpeg -i video.mkv -c:v libx264 -crf 18 video.mp4</code>. "
            f"The frames below are the same pixels.</span></p>")


def results_table(results, actions):
    if results is None:
        return '<p class="mut">No results file: no checks were evaluated for this run.</p>'
    counts = {}
    for r in results:
        counts[r.get("status")] = counts.get(r.get("status"), 0) + 1
    head = " ".join(f'<span class="badge {esc(k)}">{esc(k)}: {v}</span>' for k, v in sorted(counts.items(), key=str))
    rows = []
    for r in results:
        ai = action_index(actions, r)
        link = f'<a href="#a{ai}">action {ai}</a>' if ai is not None else "&mdash;"
        rows.append(f'<tr><td><span class="badge {esc(r.get("status"))}">{esc(r.get("status"))}</span></td>'
                    f'<td>{esc(r.get("id") or "")}</td><td>{esc(r.get("check"))}</td>'
                    f'<td><code>{esc(r.get("at"))}</code> from {esc(r.get("anchor"))}'
                    f'{" (not)" if r.get("not") else ""}</td><td>{link}</td>'
                    f'<td>{esc(r.get("error") or "")}</td></tr>')
    return (f"<p>{head}</p><table><tr><th>status</th><th>id</th><th>check</th><th>at</th><th>anchor</th>"
            f"<th>error</th></tr>{''.join(rows)}</table>")


def action_card(run, frames, i, a, results, t0):
    anchor = a.get("t_send")
    title = f"{i}. {a.get('cmd')} {a.get('args', '')}"
    step = f' <span class="mut">step {esc(a["step"])}</span>' if a.get("step") else ""
    status = "" if a.get("ok", True) else f' <span class="badge error">action failed: {esc(a.get("error"))}</span>'
    if anchor is None:
        return f'<div class="card" id="a{i}"><h3>{esc(title)}</h3><p class="mut">not timestamped</p></div>'
    ack = (a["t_ack"] - anchor) / 1e6 if a.get("t_ack") else None
    meta = (f'<p class="mut">t_send {((anchor - t0) / 1e6):.1f} ms into the recording'
            + (f", ack after {ack:.1f} ms" if ack is not None else "") + "</p>")
    rects = [rr for r in results for rr in result_rects(r)]
    used = {fr["n"] for r in results for fr in r.get("frames", [])}
    cells, shown = [], set()

    def add(f, label):
        if f and f["n"] not in shown:
            shown.add(f["n"])
            cells.append((f["t"], thumb(run, f, anchor, label, rects, f["n"] in used)))

    try:
        add(T.baseline(frames, anchor), "before")
    except T.TimelineError:
        pass
    for ms in SHEET_OFFSETS_MS:
        try:
            add(T.select_point(frames, anchor + ms * 1_000_000), f"+{ms}ms")
        except T.TimelineError:
            pass
    for r in results:
        for fr in r.get("frames", []):
            f = next((x for x in frames if x["n"] == fr["n"]), None)
            add(f, f"{r.get('id') or r.get('check')}")
        if r.get("baseline"):
            f = next((x for x in frames if x["n"] == r["baseline"]["n"]), None)
            add(f, "baseline")
    cells.sort(key=lambda c: c[0])
    res = ""
    if results:
        res = "<ul>" + "".join(
            f'<li><span class="badge {esc(r.get("status"))}">{esc(r.get("status"))}</span> '
            f'{esc(r.get("id") or "")} <code>{esc(r.get("check"))} {esc(r.get("at"))}</code> '
            f'{esc(r.get("error") or "")}</li>' for r in results) + "</ul>"
    return (f'<div class="card" id="a{i}"><h3>{esc(title)}{step}{status}</h3>{meta}{res}'
            f'<div class="sheet">{"".join(c for _, c in cells)}</div></div>')


def write_report(run, results_path=None, out=None):
    frames = T.load_frames(run)
    actions = T.load_actions(run)
    S = load_json(os.path.join(run, "summary.json"))
    segs = []
    p = os.path.join(run, "segments.jsonl")
    if os.path.exists(p):
        segs = T.load_jsonl(p)
    results_path = results_path or os.path.join(run, "results.json")
    results = load_json(results_path) if os.path.exists(results_path) else None
    t0 = frames[0]["t"] if frames else 0
    by_action, loose = {}, []
    for r in results or []:
        ai = action_index(actions, r)
        (by_action.setdefault(ai, []) if ai is not None else loose).append(r)
    parts = [f"<!doctype html><html lang=en><head><meta charset=utf-8>"
             f"<meta name=viewport content='width=device-width,initial-scale=1'>"
             f"<title>vmkit run report</title><style>{CSS}</style></head><body>",
             f"<h1>vmkit run report</h1><p class=mut><code>{esc(os.path.abspath(run))}</code></p>",
             "<h2>Checks</h2>", results_table(results, actions),
             "<h2>Recording</h2>", summary_block(S, segs),
             f"<h2>Actions ({len(actions)})</h2>"]
    if not actions:
        parts.append('<p class="mut">No actions were recorded during this run.</p>')
    for i, a in enumerate(actions):
        parts.append(action_card(run, frames, i, a, by_action.get(i, []), t0))
    if loose:
        parts.append("<h2>Checks not tied to an action</h2>")
        for r in loose:
            anchor = r.get("anchor_t") or t0
            cells = []
            for fr in r.get("frames", []):
                f = next((x for x in frames if x["n"] == fr["n"]), None)
                if f:
                    cells.append(thumb(run, f, anchor, r.get("id") or r.get("check"), result_rects(r), True))
            parts.append(f'<div class="card"><h3><span class="badge {esc(r.get("status"))}">{esc(r.get("status"))}'
                         f'</span> {esc(r.get("id") or r.get("check"))}</h3><p class="mut">{esc(r.get("error") or "")}</p>'
                         f'<div class="sheet">{"".join(cells)}</div></div>')
    parts.append("</body></html>")
    out = out or os.path.join(run, "report.html")
    with open(out, "w") as f:
        f.write("\n".join(parts))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vmkit report")
    ap.add_argument("--dir", default=os.environ.get("VMKIT_DIR", ".vmkit"))
    ap.add_argument("run", nargs="?")
    ap.add_argument("--results")
    ap.add_argument("-o", "--out")
    a = ap.parse_args(argv)
    try:
        run = a.run or T.default_run(a.dir)
        print(write_report(run, a.results, a.out))
    except (OSError, ValueError, T.TimelineError) as e:
        print(f"vmkit report: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
