# vmkit — development gaps & roadmap

The register of what an agentic OS/system-dev VM toolchain still needs. Each
entry: what's missing, why it matters, and the intended shape. Discovered while
building vmkit and driving a real graphical-desktop bring-up.

## Present today (v0)

- Headless boot with `-serial file:` + `-qmp` (pre-SSH console + control).
- Vision: QMP `screendump` → PNG (PPM+convert fallback for old qemu).
- Keyboard: `send-key` (`keys`, `type`, US-ASCII map).
- Mouse: `input-send-event`, pixel→abs mapped from live framebuffer size
  (`move`, `click`, `tap`).
- Shared human view: `--view vnc` + `view` (agent QMP + human VNC, same VM).
- SSH passthrough, qcow2 `savevm`/`loadvm`, graceful `down` with pidfile kill.

## Gaps

### Vision
- **Still frames only.** No video/stream for the agent; motion (animations,
  progress, flicker) is invisible between shots. Want: a `watch` that diffs
  successive screendumps and surfaces meaningful change.
- **No OCR / element location.** The agent eyeballs pixel coordinates from a
  screenshot to aim the mouse. Want: optional OCR (bbox + text) and template
  match so `tap "Shut Down"` works instead of `tap 211 754`.
- **No readiness primitive.** "Wait until the framebuffer is rendered/stable"
  is done by proxy (poll SSH). Want: `wait --stable` (screendump hash settles)
  and `wait --serial <regex>` for headless/pre-graphical guests.

### Input
- **Partial keymap.** `type` covers US-ASCII; non-US layouts, unicode, and
  compose are unmapped. Want: layout-aware typing, or paste via clipboard
  where the guest supports it.
- **No drag / scroll / hold.** Only move + discrete click. Want: `drag`,
  `scroll`, and modifier-held gestures.
- **Coordinate space.** Pixel→abs assumes a single display at the screendump
  resolution; multi-head guests and mid-session resolution changes aren't
  handled.

### Provisioning & images (out of core, needed by the toolchain)
- **cloud-init/cidata is distro-shaped.** vmkit takes a prebuilt `--cidata`
  ISO but doesn't generate one. Want a pluggable `provision` step (cloud-init
  for Linux, autounattend for Windows, etc.) so a guest can be created from
  scratch, not just booted.
- **No image lifecycle.** No fetch of base cloud images, no `create-overlay`,
  no reset. Currently borrowed from per-product harnesses. Want first-class
  `image pull` / `overlay new` / `overlay reset`.

### Orchestration & observability
- **Single VM, single arch.** x86_64/KVM only; no aarch64/TCG, no multi-VM
  networks (a common systems-dev need: client+server, cluster).
- **No structured event log.** Actions (shots, keys, clicks) aren't recorded
  to a replayable transcript. Want a JSONL trace so a session can be reviewed
  or re-run.
- **No device/hotplug helpers.** USB/disk/net hotplug, and passing host
  devices, are raw QMP. Want thin verbs.

## Host-environment footguns (learned the hard way, worth codifying)
- **Never `pkill -f <pattern>`** where the pattern also appears in the caller's
  own command line — it kills the calling shell (seen as exit 144). vmkit
  tracks a pidfile and kills by PID only; keep it that way.
- **`-display gtk` needs a real display.** An agent shell usually has no
  `DISPLAY`/`WAYLAND_DISPLAY`, so gtk init fails. Default to headless; use VNC
  for the human. (`--view gtk` is only for a human-launched vmkit.)
- **SSH into a rebooting guest hangs without keepalives.** Always set
  `ServerAliveInterval`/`CountMax` so a connection that black-holes during a
  reboot drops instead of wedging the run. (vmkit's `guest_ssh` does.)
- **Some agent shells block foreground `sleep`.** Pace waits with a bounded
  poll loop, not `sleep`.
