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

## Requirements

Host: `qemu-system-x86_64`, `python3` (stdlib only). Optional: `magick`/`convert`
(only as a screenshot fallback on qemu too old for PNG `screendump`); a VNC
client for the human view (`remote-viewer`, `vinagre`, `gvncviewer`, `wlvncc`,
or `vncviewer`). The guest needs nothing installed for vision/input/serial;
SSH is optional and only used by the `ssh` command.

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
