# vmkit

A toolchain for **agentic OS / system development in QEMU**: let an AI agent
boot a VM, *see* its screen, drive its keyboard and mouse, read the boot
console, and hand a live view to a human — all through the QEMU layer, so it is
**guest-OS-agnostic**. The same commands work whether the guest is Linux, BSD,
Windows, a bare installer ISO, or a kernel that never reaches userspace.

It exists because the usual VM test harnesses expose a window for a human and
nothing an agent can attach to. vmkit wires the four things an agent needs and a
human normally gets for free:

| Capability | Mechanism | Agent | Human |
|---|---|---|---|
| **Vision** | QMP `screendump` → PPM, converted to PNG on the host | ✅ still frames | ✅ live via VNC |
| **Keyboard** | QMP `send-key` (`keys`, `type`) | ✅ | ✅ via VNC |
| **Mouse** | QMP `input-send-event`, pixel→abs mapped (`move`/`click`/`tap`) | ✅ | ✅ via VNC |
| **Console** | `-serial file:` — the pre-SSH boot the guest never logs | ✅ | ✅ |
| **Control** | QMP (`query-status`, `savevm`/`loadvm`, powerdown) | ✅ | — |
| **Shell** | SSH into the guest once it is up | ✅ | ✅ |

| **Timeline** | `record` (30 fps PPM screendumps on a 2nd QMP socket → PNG sequence + ffv1), every input action stamped on the same host clock, `frames`/`check`/`report` | ✅ | ✅ `report.html` |

Agent and human drive the **same running VM at once**: the agent over QMP, the
human over VNC. That is the collaboration model (`--view vnc`).

## Host dependencies

All dependencies are on the **host**; the guest needs nothing installed for
vision/input/serial (that's the point — it's guest-OS-agnostic).

| Dependency | Required for | If missing |
|---|---|---|
| `qemu-system-x86_64` | everything (`up`) | vmkit can't run |
| `python3` (stdlib only) | QMP, mouse mapping, PNG sizing | vmkit can't run |
| `openssh` (`ssh`/`scp`) | `ssh`, `push`, `pull`, `wait ssh`/`exec` | those commands only |
| ImageMagick (`magick`/`convert`) | `shot --grid`, `wait screen`/`watch` (fuzzy diff) | grid/stable degrade; plain `shot` still works (stdlib PPM→PNG) |
| `tesseract` + a language pack (e.g. `tesseract-data-eng`) | `find` (OCR) | `find` prints a clear "not installed" message (exit 3) |
| `qemu-system-<arch>` + UEFI firmware (e.g. `edk2-aarch64`) | non-x86 guests via `--arch`/`--accel` (validated: aarch64/TCG) | `--arch` only |
| a VNC client (`remote-viewer`/`vinagre`/`gvncviewer`/`wlvncc`/`vncviewer`) | `view` (human live view) | `view` prints the address to connect manually |
| `ffmpeg` (+ `ffprobe`) | `record` (PNG sequence + ffv1 video; ffprobe checks the packet count) | `record start` exits 3 |
| `tesseract`, ImageMagick | `check ocr`, `check template` (and PNG decoding speed in `check`) | those checks report an error; `pixel`/`changed` fall back to pure-Python decoding |

Arch: `pacman -S qemu-full python openssh imagemagick tesseract tesseract-data-eng virt-viewer`.
Debian/Ubuntu: `apt install qemu-system-x86 python3 openssh-client imagemagick tesseract-ocr virt-viewer`.
Fedora: `dnf install qemu-system-x86 python3 openssh-clients ImageMagick tesseract virt-viewer`.

Core (`up`/`shot`/`keys`/`type`/`move`/`click`/`tap`/`serial`/`qmp`) needs only
qemu + python3; the rest degrade gracefully as noted above.

## Quickstart

```sh
# boot an existing disk image, headless, with a shared VNC view
bin/vmkit --overlay ~/vms/mybox/disk.qcow2 --view vnc up

# agent: look, then act
bin/vmkit --overlay ~/vms/mybox/disk.qcow2 shot login      # -> .../login.png
bin/vmkit --overlay ~/vms/mybox/disk.qcow2 tap 640 380     # move+click a field
bin/vmkit --overlay ~/vms/mybox/disk.qcow2 type 'hunter2'
bin/vmkit --overlay ~/vms/mybox/disk.qcow2 keys ret

# aim without guessing: grid overlay to read coords, or OCR a label
bin/vmkit --overlay ... shot ui --grid                     # -> ui-grid.png with a ruler
bin/vmkit --overlay ... find "Shut Down"                   # -> "1523,742  Shut Down" (needs tesseract)

# block on conditions instead of hand-rolled poll loops
bin/vmkit --overlay ... wait ssh                           # guest reachable
bin/vmkit --overlay ... wait exec 'pgrep -x waybar' 30     # a guest condition
bin/vmkit --overlay ... wait screen                        # a stable frame (tolerates a ticking clock)

# iterate on guest files without a rebuild
bin/vmkit --overlay ... push ./sysinit.vim /etc/xdg/nvim/sysinit.vim
bin/vmkit --overlay ... pull /var/log/get-hypr-de.log ./install.log

# human: join the same VM live (run from your own graphical session)
bin/vmkit --overlay ~/vms/mybox/disk.qcow2 view            # opens a VNC client

# boot console, guest shell, snapshots, shutdown
bin/vmkit --overlay ... serial 80
bin/vmkit --overlay ... ssh 'journalctl -b -p err --no-pager | tail'
bin/vmkit --overlay ... snapshot clean ; bin/vmkit --overlay ... restore clean
bin/vmkit --overlay ... down
```

Config resolves from flags → env (`VMKIT_OVERLAY`, `VMKIT_DIR`, `VMKIT_SSH_PORT`,
`VMKIT_VIEW`, …) → defaults. Sockets/logs/screenshots live in
`<overlay-dir>/.vmkit` unless `--dir` is given. See the header of `bin/vmkit`
for the full command and option list, and [docs/CAPABILITIES.md](docs/CAPABILITIES.md)
for the agent contract.

## Timeline: record, act, check, report

The QA loop: record the screen while acting on the guest, then check the
recorded frames against expectations, model-free, and write a report. All
timestamps are the host's `CLOCK_MONOTONIC` in ns; nothing is stamped by the
guest.

```sh
V="bin/vmkit --overlay ~/vms/mybox/disk.qcow2"
$V record start                      # -> <dir>/runs/<UTC>/ ; blocks until frame 0
VMKIT_STEP=open $V keys meta_l q     # every input action -> actions.jsonl {t_send, t_ack, ok}
$V type 'echo hi'
$V record stop                       # frames 163  fps 30.0  interval p50 33.3 p95 33.9 max 35.0 ms  degraded=false

$V frames --action open --offset +0ms..+400ms --baseline   # which frames the Timing rules pick
$V check changed --action open --offset +0ms..+1500ms --region 22,22,1236,756 --min-px 1000
$V check ocr --action -1 --anchor ack --offset +100ms..+1s --region 0,0,0,200 --text 'echo hi'
$V check batch expect.json           # many expectations -> <run>/results.json (exit 0/1/2)
$V report                            # -> <run>/report.html
```

- **`record`** (`lib/vmkit/record.py`, its own process, pidfile `record.pid`)
  holds the recorder-only QMP socket `qmp-rec.sock` and paces `screendump`
  as PPM into /dev/shm at `--fps` (30). Each frame is stamped `t_req`/`t`;
  its raw RGB goes to one ffmpeg that writes `frames/%06d.png` (file number
  = frame `n`) and `video.mkv` (ffv1). QEMU's "Display output is not active"
  placeholder is marked `inactive`. A resolution change starts a new encoder
  segment. `stop` writes `summary.json`: fps, interval p50/p95/max,
  screendump call time, and `degraded=true` when any interval exceeded 50 ms
  (`--degraded-ms`): a busy host shows up in the numbers instead of being
  assumed away. The recorder runs under the same nice/ionice as qemu.
- **Actions**: `keys`/`type`/`move`/`click`/`tap`/`tap-text`/`drag`/`scroll`
  (QMP) and `ssh`/`xexec` (subprocess) append `{t, cmd, args, t_send, t_ack,
  ok, error?, step?}` to `<dir>/actions.jsonl` and, while recording, to the run.
- **`frames`**: `+300ms` = the first frame at or after anchor+300 ms;
  `+50ms..+300ms` = every frame in the window; an empty window or an offset
  outside the recording is an error (exit 2).
- **`check`** (`lib/vmkit/checks.py`): `pixel`, `ocr`, `template`, `changed`,
  regions `[x,y,w,h]` with negative x/y from the right/bottom and 0 w/h to the
  edge, `--not` on any check; inactive frames are errors. A window passes when
  any frame passes (`--mode any`), or with `--not` when no frame matches
  (`--mode all`), unless `--mode` is given.
- **`report`**: plain HTML, no external assets: pass/fail summary, recorder
  numbers and the degraded flag, per-action contact sheets with check regions
  outlined, links to the video.

Scenario files (TOML, steps, hyprhands) live in the consumer (hypr-qa); vmkit
provides the primitives. Tests: `python3 -m unittest` (stdlib; the OCR and
template tests run where tesseract/ImageMagick exist).

## Scope and non-goals

vmkit is the **substrate**, deliberately thin: it turns a QEMU VM into something
an agent and a human can jointly operate. It does not know about any particular
guest, distro, or product — provisioning (cloud-init/autoinstall), image
building, and product test suites layer on top and stay in their own repos. Its
first real driver was validated bringing up and inspecting a full graphical
Linux desktop (greeter → login via injected keystrokes → running session), but
nothing in the tool is specific to it.

Known gaps and the planned toolchain are tracked in [GAPS.md](GAPS.md).

## License

MIT © Mason Rhodes. See [LICENSE](LICENSE).
