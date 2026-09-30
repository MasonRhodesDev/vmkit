# vmkit — development gaps & roadmap

The register of what an agentic OS/system-dev VM toolchain still needs. Each
entry: what's missing, why it matters, and the intended shape. Driven by real
use — bridging a graphical-desktop bring-up and a live debug session (default
editor, GTK icons, swaync styling, an idle-inhibitor tray).

## Present today (v0)

- Headless boot with `-serial file:` + `-qmp` (pre-SSH console + control).
- Vision: QMP `screendump` -> PPM, converted to PNG on the host (stdlib).
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

### Bridged in the v0.2 "blast" (were open, now shipped)
- `tap-text "LABEL"` — OCR consecutive-word-run targeting (tight bbox, so
  adjacent buttons like Restart / Shut Down don't merge). Validated: "Shut
  Down" -> (212,1310), not the row center.
- `drag`, `scroll` — pointer press-move-release and wheel.
- `xexec CMD` — run in the guest's graphical (systemd --user) session with
  WAYLAND_DISPLAY/HYPRLAND_INSTANCE/etc. resolved (validated: hyprctl version).
- `watch` — emit on screen change (fuzzy pixel-diff), motion-aware waiting.
- `find --line` — group multi-word labels.
- action transcript (`actions.jsonl`), `overlay new/reset`, `image pull`,
  `--arch`/`--accel`.
- **Non-x86 boot (validated)** — `up` is arch-aware: aarch64 gets `virt` +
  UEFI pflash firmware + virtio-gpu/xhci. Booted Alpine aarch64 under TCG to
  `Linux version 6.12 aarch64` via `--arch aarch64 --accel tcg` (needs
  qemu-system-aarch64 + edk2-aarch64 on the host).

### Still open
- **Video/stream** — only `watch` (change events) and still `shot`; no live
  encode. Fine for agent use; a human uses `view` (VNC).
- **Non-text / icon targets** — `find`/`tap-text` are text-only. Want template
  match (`magick -subimage-search`) to click an icon with no label.
- **Keymap** — `type` is US-ASCII; non-US layouts, unicode, compose unmapped.
  Want layout-aware typing or clipboard paste.
- **Multi-head / resolution changes** — pixel->abs mapping assumes one display
  at the current framebuffer size.
- **Provisioning** — `image pull` fetches a base, but cloud-init/autoinstall
  seed generation is still caller-supplied (`--cidata`).
- **Multi-VM networks** — single VM only; no client+server / cluster topology.
- **Session-state naming** (greeter vs lock vs session) stays guest-specific;
  `xexec` + `find`/`wait exec` cover the practical need.

## Model economy (how an agent should use these)
- **Don't read screenshots with an expensive model.** Prefer model-free
  signals — `find` (tesseract OCR → text+coords), `wait exec` (guest pgrep/
  journal), `wait screen` (pixel-diff) — to confirm state. Reserve visual
  judgment for a small/fast model (a Haiku subagent), not the main loop.
  `tap-text` (shipped) makes the common target-a-label case fully model-free.

## Host-environment footguns (codified)
- **Never `pkill -f <pattern>`** matching your own command line — it kills the
  calling shell (exit 144). vmkit tracks a pidfile and kills by PID only.
- **`-display gtk` needs a real display**; agent shells have none, so gtk init
  fails. Default headless; VNC for the human.
- **SSH into a rebooting guest hangs without keepalives** — `guest_ssh` sets
  `ServerAliveInterval`/`CountMax` so a black-holed connection drops.
- **Some agent shells block foreground `sleep`** — `poll` tolerates it.
- **`ls A B C` exits non-zero if ANY path is missing** — under `set -o
  pipefail` that aborts the script; use a loop to find the first existing file
  (bit the aarch64 firmware detection).
- **Don't pipe `up`'s stdout** — backgrounded qemu inherits and holds the fd,
  so a reader (sed/tee) blocks until the VM exits. Redirect to a file.
- **`pgrep -x` misses process names >15 chars** (e.g.
  `logind-idle-control-tray`) — use `pgrep -f` for long names.

## 2026-08-23 (vigil #52 validation)
- `snapshot`/`restore` are broken against modern QEMU: `savevm` is an HMP command, not QMP -- `{"error": {"class": "CommandNotFound", "desc": "The command savevm has not been found"}}`. Needs `human-monitor-command` wrapping (`{"execute":"human-monitor-command","arguments":{"command-line":"savevm NAME"}}`).
- `screendump` can stall 0.5-2 s while the guest compositor is under heavy software-GL load; timestamp each shot on the host when timing matters.
- `xexec loginctl lock-session` locks the SSH session, not the desktop -- pass the graphical session id explicitly.

## 2026-09-30 (timeline branch: gap fixes)
- **QMP error replies were discarded** — `keys super-q` (not a qcode) printed
  success. Fixed: every QMP client in vmkit checks each reply; an `{"error":...}`
  exits 1 with `vmkit: qmp error: <class>: <desc>` on stderr (`vmkit qmp` still
  prints the reply object on stdout). Timeout: `VMKIT_QMP_TIMEOUT` (default 30 s).
- **PNG screendumps stalled the guest** — QEMU's PNG encoder costs ~30 ms per
  call inside QEMU and halved the guest's frame rate in the recording spike;
  `shot`, `wait screen` and `watch` looped on it. Fixed: every screendump asks
  for PPM (a memcpy) into /dev/shm (`VMKIT_TMP` overrides) and `shot` converts
  to PNG on the host with a stdlib encoder (`lib/vmkit/imageio.py`).
- **One QMP socket** — a QMP chardev serves one client at a time, so a
  long-lived recorder holding `qmp.sock` blocked every other vmkit call for
  seconds. Fixed: `up` adds a second socket, `$DIR/qmp-rec.sock`, used only by
  the recorder.
- **`snapshot`/`restore` sent savevm/loadvm as QMP** (HMP-only on modern QEMU,
  see 2026-08-23). Fixed: both go through `human-monitor-command`; HMP returns
  errors as text in `"return"`, so any output from savevm/loadvm (or text
  starting "Error") is a nonzero exit. Their socket timeout is
  `VMKIT_SNAPSHOT_TIMEOUT` (default 600 s). New: `hmp LINE`, `snapshots`.
- **`ssh`/`xexec` joined args with spaces**, so `ssh printf '%s\n' 'a b'` lost
  its quoting. Fixed: two or more args are an argv, each quoted with
  `printf %q`; a single arg is still a shell command line (`ssh 'a | b'`).
  Footnote: `%q` writes `$'...'` for non-printable bytes, which needs a
  bash/zsh/ksh (or POSIX-2024 sh) login shell in the guest.
- **`up` set nice but not ionice** — qemu's disk I/O competed at normal
  priority. Fixed: qemu runs under `nice -n $VMKIT_NICE ionice -c $VMKIT_IONICE`
  (defaults 19 and 3 = idle; `--nice`/`--ionice`, `--ionice none` to skip).
