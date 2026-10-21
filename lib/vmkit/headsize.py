"""Ask a virtio-gpu head to exist, at a size, through its VNC server.

QEMU's virtio-gpu reports only head 0 as connected until a display frontend
sets a UI size for the others (`dpy_set_ui_info`; GTK does it from a window,
VNC from a client's SetDesktopSize). With no GUI attached this is the only way
to get a second output: `vmkit up --heads N` gives each head H > 0 its own VNC
server on a unix socket (<dir>/head<H>.sock), and this connects to it, sends
SetDesktopSize and waits for the ExtendedDesktopSize reply.

    python3 headsize.py SOCK WIDTHxHEIGHT [HOLD_SECONDS]

Exits 0 when the server accepted the size, 1 when it refused, 2 on a protocol
or socket error.
"""
import socket
import struct
import sys
import time


def recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise EOFError("VNC server closed the connection")
        buf += chunk
    return buf


def handshake(s):
    ver = recv_exact(s, 12)
    if not ver.startswith(b"RFB "):
        raise ValueError(f"not a VNC server: {ver!r}")
    s.sendall(b"RFB 003.008\n")
    types = recv_exact(s, recv_exact(s, 1)[0])
    if 1 not in types:
        raise ValueError(f"VNC server wants authentication (security types {list(types)})")
    s.sendall(b"\x01")
    if struct.unpack(">I", recv_exact(s, 4))[0] != 0:
        raise ValueError("VNC security handshake refused")
    s.sendall(b"\x01")                                     # ClientInit: shared
    recv_exact(s, 20)                                      # size and pixel format
    recv_exact(s, struct.unpack(">I", recv_exact(s, 4))[0])  # desktop name


def set_size(path, w, h, timeout=10.0, hold=0.0):
    """(accepted, status): the server's ExtendedDesktopSize result code, 0 = ok, 4 = request forwarded (QEMU hands it to the guest GPU)."""
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    s.connect(path)
    try:
        handshake(s)
        # SetEncodings: ExtendedDesktopSize (-308), which carries the result of a resize request.
        s.sendall(struct.pack(">BxHi", 2, 1, -308))
        # SetDesktopSize: one screen covering the whole desktop.
        s.sendall(struct.pack(">BxHHBx", 251, w, h, 1) + struct.pack(">IHHHHI", 0, 0, 0, w, h, 0))
        s.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))   # FramebufferUpdateRequest, so updates flow
        accepted = None; status = None
        end = time.monotonic() + timeout
        while accepted is None and time.monotonic() < end:
            if recv_exact(s, 1) != b"\x00":
                raise ValueError("unexpected VNC server message")
            recv_exact(s, 1)
            for _ in range(struct.unpack(">H", recv_exact(s, 2))[0]):
                x, y, rw, rh, enc = struct.unpack(">HHHHi", recv_exact(s, 12))
                if enc == -308:       # x = who asked (1: this client), y = status (0: ok)
                    n = recv_exact(s, 1)[0]
                    recv_exact(s, 3 + 16 * n)
                    if x == 1:
                        accepted = y in (0, 4)
                        status = y
                elif enc == 0:
                    recv_exact(s, rw * rh * 4)
                else:
                    raise ValueError(f"unexpected rectangle encoding {enc}")
        if accepted is None:
            raise TimeoutError(f"no ExtendedDesktopSize reply in {timeout:g} s")
        time.sleep(hold)
        return accepted, status
    finally:
        s.close()


def main(argv):
    if len(argv) not in (2, 3) or "x" not in argv[1]:
        sys.exit(__doc__)
    w, h = (int(v) for v in argv[1].split("x"))
    try:
        ok, status = set_size(argv[0], w, h, hold=float(argv[2]) if len(argv) == 3 else 0.0)
    except (OSError, EOFError, ValueError, TimeoutError, struct.error) as e:
        print(f"headsize: {e}", file=sys.stderr)
        return 2
    if not ok:
        print(f"headsize: the VNC server refused {w}x{h} (result {status}: 1 prohibited, 2 out of resources, 3 invalid layout)", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
