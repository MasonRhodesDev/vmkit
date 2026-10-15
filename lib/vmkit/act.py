"""Timestamped input actions.

Every input action vmkit performs (keys/type/move/click/tap/drag/scroll over
QMP; ssh/xexec as a subprocess) runs here so it can be stamped on the host's
CLOCK_MONOTONIC (time.monotonic_ns), the same clock the recorder stamps frames
with:

  t_send  just before the action's first QMP write / the subprocess start
  t_ack   after the action's last QMP reply / the subprocess exit

One JSON line per action is appended to <dir>/actions.jsonl (the session
transcript) and, while a recording is active (<dir>/record.run names its run
dir), to <run>/actions.jsonl as well. Fields:

  t        host wall clock, UTC ISO-8601 (as in the original transcript)
  cmd      action name; args: its arguments as one string (original fields)
  t_send, t_ack   CLOCK_MONOTONIC ns
  ok       bool; error: message when ok is false
  step     $VMKIT_STEP when set (lets a runner label its steps)
  x, y     pointer target, when there is one

Usage: act.py --dir DIR --sock QMP_SOCK [--tmp TMPD] OP ARGS...
       act.py --dir DIR exec NAME DISPLAY_ARGS -- ARGV...   (subprocess, stdio inherited)
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vmkit.qmp import QMP, QMPError  # noqa: E402
from vmkit import imageio  # noqa: E402

now = time.monotonic_ns

BASE = {c: c for c in "abcdefghijklmnopqrstuvwxyz0123456789"}
BASE.update({" ": "spc", ".": "dot", ",": "comma", "-": "minus", "/": "slash", "=": "equal",
             ";": "semicolon", "'": "apostrophe", "\n": "ret", "\t": "tab", "[": "bracket_left",
             "]": "bracket_right", "\\": "backslash", "`": "grave_accent"})
SHIFTED = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9",
           ")": "0", "_": "minus", "+": "equal", ":": "semicolon", "\"": "apostrophe", "?": "slash",
           "<": "comma", ">": "dot", "{": "bracket_left", "}": "bracket_right", "|": "backslash",
           "~": "grave_accent"}


def wall():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log_paths(d):
    paths = [os.path.join(d, "actions.jsonl")]
    try:
        run = open(os.path.join(d, "record.run")).read().strip()
        if run and os.path.isdir(run):
            paths.append(os.path.join(run, "actions.jsonl"))
    except OSError:
        pass
    return paths


def log(d, rec):
    step = os.environ.get("VMKIT_STEP")
    if step:
        rec["step"] = step
    line = json.dumps(rec) + "\n"
    for p in log_paths(d):
        try:
            with open(p, "a") as f:
                f.write(line)
        except OSError:
            pass


def fb_size(q, tmp):
    path = os.path.join(tmp, f"vmkit-act.{os.getpid()}.ppm")
    try:
        try:
            q.cmd("screendump", {"filename": path, "format": "ppm"})
        except QMPError:
            q.cmd("screendump", {"filename": path})     # QEMU < 7.1: PPM, no format arg
        return imageio.size(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


class Actor:
    def __init__(self, q, size=None):
        self.q, self.size, self.t_send = q, size, None

    def _cmd(self, name, args):
        if self.t_send is None:
            self.t_send = now()
        return self.q.cmd(name, args)

    def ev(self, events):
        self._cmd("input-send-event", {"events": events})

    def ax(self, v, n):
        return max(0, min(32767, round(int(v) / max(1, n - 1) * 32767)))

    def moveto(self, x, y):
        W, H = self.size
        self.ev([{"type": "abs", "data": {"axis": "x", "value": self.ax(x, W)}},
                 {"type": "abs", "data": {"axis": "y", "value": self.ax(y, H)}}])

    def button(self, btn):
        self.ev([{"type": "btn", "data": {"down": True, "button": btn}}])
        self.ev([{"type": "btn", "data": {"down": False, "button": btn}}])

    def keys(self, keys):
        self._cmd("send-key", {"keys": [{"type": "qcode", "data": k} for k in keys]})


# Pause between the characters of `type`. Back-to-back send-key chords lose
# events in the guest (a dropped shift release turns the rest of the text
# upper-case, a dropped ctrl breaks the next chord); spacing them out keeps
# every press and release. VMKIT_TYPE_GAP_MS overrides it.
TYPE_GAP = float(os.environ.get("VMKIT_TYPE_GAP_MS", "40")) / 1000


NEEDS_SIZE = {"move", "tap", "tap-text", "drag"}


def run_qmp(op, a, sock, tmp):
    """Perform op; returns (record fields, stdout message)."""
    try:
        q = QMP(sock)
    except (QMPError, OSError) as e:    # VM down: a failed action, logged like any other
        t = now()
        return {"t_send": t, "t_ack": t, "ok": False, "error": str(e)}, None
    try:
        A = Actor(q)
        rec, msg = {}, ""
        try:
            if op in NEEDS_SIZE:
                A.size = fb_size(q, tmp)
            if op == "move":
                A.moveto(a[0], a[1]); rec.update(x=int(a[0]), y=int(a[1])); msg = f"move {a[0]},{a[1]}"
            elif op == "click":
                btn = a[0] if a else "left"; A.button(btn); msg = f"click {btn}"
            elif op in ("tap", "tap-text"):
                btn = a[2] if len(a) > 2 else "left"
                A.moveto(a[0], a[1]); A.button(btn); rec.update(x=int(a[0]), y=int(a[1]))
                msg = f"tap {a[0]},{a[1]} ({btn})"
            elif op == "drag":
                btn = a[4] if len(a) > 4 else "left"
                x0, y0, x1, y1 = map(int, a[:4])
                A.moveto(x0, y0)
                A.ev([{"type": "btn", "data": {"down": True, "button": btn}}])
                for i in range(1, 6):   # interpolated steps so drag-aware UIs see motion
                    A.moveto(x0 + (x1 - x0) * i // 5, y0 + (y1 - y0) * i // 5)
                A.ev([{"type": "btn", "data": {"down": False, "button": btn}}])
                rec.update(x=x0, y=y0)
                msg = f"drag {x0},{y0} -> {x1},{y1} ({btn})"
            elif op == "scroll":
                direction = a[0] if a else "down"; n = int(a[1]) if len(a) > 1 else 3
                btn = "wheel-up" if direction in ("up", "u") else "wheel-down"
                for _ in range(n):
                    A.button(btn)
                msg = f"scroll {direction} x{n}"
            elif op == "keys":
                if not a:
                    raise ValueError("usage: vmkit keys K [K...]")
                A.keys(a); msg = "keys: " + " ".join(a)
            elif op == "type":
                text = " ".join(a)
                bad = sorted({ch for ch in text if not (ch in SHIFTED or ch in BASE or (ch.isupper() and ch.lower() in BASE))})
                if bad:
                    raise ValueError(f"no key mapping for {''.join(bad)!r} (US-ASCII only)")
                for i, ch in enumerate(text):
                    if i:
                        time.sleep(TYPE_GAP)
                    if ch in SHIFTED:
                        A.keys(["shift", SHIFTED[ch]])
                    elif ch in BASE:
                        A.keys([BASE[ch]])
                    else:
                        A.keys(["shift", BASE[ch.lower()]])
                msg = f"typed {len(text)} chars"
            else:
                raise ValueError(f"unknown action {op}")
            t_ack = now()
            return dict(rec, t_send=A.t_send or t_ack, t_ack=t_ack, ok=True), msg
        except (QMPError, ValueError, OSError) as e:
            t = now()
            return dict(rec, t_send=A.t_send or t, t_ack=t, ok=False, error=str(e)), None
    finally:
        q.close()


def main(argv):
    d = sock = shown_override = None; tmp = "/dev/shm" if os.access("/dev/shm", os.W_OK) else None
    while argv and argv[0].startswith("--"):
        k, v = argv[0], argv[1]; argv = argv[2:]
        if k == "--dir": d = v
        elif k == "--sock": sock = v
        elif k == "--tmp": tmp = v
        elif k == "--shown": shown_override = v
        else: sys.exit(f"act.py: unknown option {k}")
    tmp = tmp or d
    op, a = argv[0], argv[1:]
    if op == "exec":
        name, shown = a[0], a[1]
        cmd = a[a.index("--") + 1:]
        t_send = now()
        try:
            rc = subprocess.call(cmd)
        except OSError as e:
            rc, err = 127, str(e)
        else:
            err = None if rc == 0 else f"exit {rc}"
        t_ack = now()
        rec = {"t": wall(), "cmd": name, "args": shown, "t_send": t_send, "t_ack": t_ack, "ok": rc == 0}
        if err:
            rec["error"] = err
        log(d, rec)
        return rc
    shown = shown_override or (" ".join(a) if op != "type" else f"({len(' '.join(a))} chars)")
    fields, msg = run_qmp(op, a, sock, tmp)
    rec = {"t": wall(), "cmd": op, "args": shown}
    rec.update(fields)
    log(d, rec)
    if not rec["ok"]:
        print(f"vmkit {op}: {rec['error']}", file=sys.stderr)
        return 1
    if msg:
        print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
