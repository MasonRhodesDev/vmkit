# vmkit capability contract

What an agent can rely on, and the exact commands. Everything operates at the
QEMU layer, so it holds for any guest OS.

## Lifecycle
- `up` — boot `--overlay` headless with serial + QMP; human view per `--view`
  (`none` default, `vnc[:N]`, or `gtk`). Idempotent: a second `up` is a no-op
  if the VM already answers QMP.
- `status` — QMP socket, SSH reachability, run-state, guest uname.
- `down` — QMP `system_powerdown`; if the guest doesn't stop, kill by pidfile.
- `snapshot NAME` / `restore NAME` — qcow2 internal `savevm`/`loadvm` (HMP via
  `human-monitor-command`; HMP error text is a nonzero exit). `snapshots` lists them.

## Vision (agent) + shared view (human)
- `shot [name]` — screendump to `<dir>/<name>.png`; prints the path. Read that
  PNG to "see" the screen. Crop+upscale a region for small text.
- `view` — human opens a VNC client to the same VM (needs `up --view vnc`).
- Contract: a screenshot reflects the framebuffer at call time. There is no
  event/stream; poll `shot` and compare for motion (see GAPS: `watch`).

## Input (drives whatever surface has focus)
- `move X Y` — pointer to pixel (X,Y). Mapped to the abs axis using the live
  framebuffer size, so coordinates are the same pixels you see in `shot`.
- `click [left|right|middle]` — click at the current position.
- `tap X Y [button]` — move then click; the usual "press this thing" verb.
- `keys K [K...]` — QMP qcodes: `ret esc tab spc up down left right`,
  letters/digits, and chords like `ctrl-alt-f2`, `shift-1`, `ctrl-c`.
- `type TEXT` — types a US-ASCII string (shifted symbols handled; see GAPS for
  layout limits).

## Console & shell
- `serial [N]` — last N lines of the serial/console log; the only window into
  bootloader/initramfs/early-kernel and any pre-networking hang.
- `ssh [CMD...]` — run in the guest (once sshd is up); the deep-inspection path
  (`journalctl -b`, unit state, files, package queries).
- `qmp JSON` — escape hatch: any QMP command; prints the reply object.

## A typical agent loop
1. `up --view vnc` → 2. poll `shot` (or `wait`, see GAPS) until the screen is
   what you expect → 3. `tap`/`type`/`keys` to act → 4. `shot` to confirm →
   5. `ssh`/`serial` to verify underneath → 6. `snapshot` before risky steps →
   7. `down` when done. The human can `view` and take over at any point.
