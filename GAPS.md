# vmkit — development gaps & roadmap

The register of what an agentic OS/system-dev VM toolchain still needs. Each
entry: what's missing, why it matters, and the intended shape. Driven by real
use — bridging a graphical-desktop bring-up and a live debug session (default
editor, GTK icons, swaync styling, an idle-inhibitor tray).

## Present today (v0)

- Headless boot with `-serial file:` + `-qmp` (pre-SSH console + control).
- Vision: QMP `screendump` -> PNG (PPM+convert fallback for old qemu).
- Keyboard: `send-key` (`keys`, `type`, US-ASCII map).
- Mouse: `input-send-event`, pixel->abs mapped from live framebuffer size
  (`move`, `click`, `tap`).
- Shared human view: `--view vnc` + `view` (agent QMP + human VNC, same VM).
- SSH passthrough, qcow2 `savevm`/`loadvm`, graceful `down` with pidfile kill.

## Bridged this session (were gaps, now shipped)

- **`wait ssh|exec|serial|screen`** — replaces ad-hoc `for i in seq … ssh`
  loops. `wait exec` polls a guest command; `wait serial <regex>` watches the
  console log; `wait screen` waits for a *stable* frame.
- **`push` / `pull`** — scp + sudo-install a file into the guest (and back),
  replacing the tar+scp+sudo dance used to smoke-test config changes.
- **`shot --grid`** — overlays a 100px coordinate ruler with axis labels so
  tap targets are read off the image instead of guessed (the single biggest
  friction while driving GUIs).
- **`find TEXT`** — OCR the screen to `x,y<TAB>text` so you can `tap` a label;
  degrades with a clear message when the host lacks tesseract.

## Findings that shaped the above

- **Exact-hash "stable" is defeated by a ticking clock.** waybar shows seconds,
  so full-frame hashes never repeat. `wait screen` uses a fuzzy pixel-diff
  (`magick compare -fuzz`, threshold `VMKIT_STABLE_PX`, default 5000px) so a
  clock tick doesn't count but a window open/close does.
- **Screenshots catch transition/stale frames** (a stale ⊘ crop; a surprise
  idle-lock). Pair `shot` with `wait screen` before reading.

## Gaps still open

### Vision
- **OCR is a host dependency** (`tesseract` + a language pack) — documented in
  the README dependency table; installed and validated here. `find` degrades
  gracefully (exit 3 + message) when it's absent. `find` does case-insensitive
  substring matching, so `find How` also matches "sHOW" — use specific queries.
- **Still frames only.** No video/stream; motion between shots is invisible.
  Want a `watch` that surfaces meaningful change (fuzzy-diff regions).
- **`find` returns raw OCR tokens** — no layout grouping (button vs label),
  no template/icon match for non-text targets.

### Input
- **Partial keymap.** `type` covers US-ASCII; non-US layouts, unicode, compose
  unmapped. Want layout-aware typing or clipboard paste.
- **No drag / scroll / hold**; single display only (multi-head + mid-session
  resolution changes not handled by the pixel->abs mapping).

### Session awareness
- **No "what state is the guest in" primitive** (greeter vs lock vs session).
  It's guest/compositor-specific, so likely a recipe layer, not core — but the
  idle-lock surprise argues for at least a documented pattern.
- **Launching into a graphical session is manual** (`XDG_RUNTIME_DIR=… hyprctl
  dispatch exec …`). A generic `ssh --session` that resolves the seat's
  runtime dir + wayland socket would cover most Linux guests.

### Provisioning & images (out of core, needed by the toolchain)
- cloud-init/cidata is taken prebuilt (`--cidata`), not generated; no image
  pull / overlay-new / reset. Borrowed from per-product harnesses today.

### Orchestration & observability
- Single VM, x86_64/KVM only; no aarch64/TCG, no multi-VM networks.
- No replayable action transcript (JSONL of shots/keys/clicks).
- Device/disk/net hotplug are raw QMP; want thin verbs.

## Host-environment footguns (codified)
- **Never `pkill -f <pattern>`** matching your own command line — it kills the
  calling shell (exit 144). vmkit tracks a pidfile and kills by PID only.
- **`-display gtk` needs a real display**; agent shells have none, so gtk init
  fails. Default headless; VNC for the human.
- **SSH into a rebooting guest hangs without keepalives** — `guest_ssh` sets
  `ServerAliveInterval`/`CountMax` so a black-holed connection drops.
- **Some agent shells block foreground `sleep`** — `poll` tolerates it.
- **`pgrep -x` misses process names >15 chars** (e.g.
  `logind-idle-control-tray`) — use `pgrep -f` for long names.
