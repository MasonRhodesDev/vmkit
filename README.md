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
| **Vision** | QMP `screendump` → PNG the agent reads | ✅ still frames | ✅ live via VNC |
| **Keyboard** | QMP `send-key` (`keys`, `type`) | ✅ | ✅ via VNC |
| **Mouse** | QMP `input-send-event`, pixel→abs mapped (`move`/`click`/`tap`) | ✅ | ✅ via VNC |
| **Console** | `-serial file:` — the pre-SSH boot the guest never logs | ✅ | ✅ |
| **Control** | QMP (`query-status`, `savevm`/`loadvm`, powerdown) | ✅ | — |
| **Shell** | SSH into the guest once it is up | ✅ | ✅ |

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
| ImageMagick (`magick`/`convert`) | `shot --grid`, `wait screen` (fuzzy diff), old-qemu PNG fallback | grid/stable degrade; plain `shot` still works via QMP PNG |
| `tesseract` + a language pack (e.g. `tesseract-data-eng`) | `find` (OCR) | `find` prints a clear "not installed" message (exit 3) |
| `qemu-system-<arch>` + UEFI firmware (e.g. `edk2-aarch64`) | non-x86 guests via `--arch`/`--accel` (validated: aarch64/TCG) | `--arch` only |
| a VNC client (`remote-viewer`/`vinagre`/`gvncviewer`/`wlvncc`/`vncviewer`) | `view` (human live view) | `view` prints the address to connect manually |

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
