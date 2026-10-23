"""Minimal RFB (VNC) client that does one thing: ask QEMU's VNC server for a
desktop size (SetDesktopSize, ExtendedDesktopSize extension).

Why: QEMU's virtio-gpu enables output N > 0 only when a display backend sets that
console's UI info (dpy_set_ui_info), and a headless `-display none` never does.
A VNC server bound to one head (`-vnc unix:PATH,display=gpu0,head=N`) does it when
a client sends SetDesktopSize, and the size becomes that output's EDID mode. The
setting survives the client disconnecting, so this runs once after `up`.

    python3 rfb.py set-size SOCKET_OR_PORT WIDTH HEIGHT
"""
import socket
import struct
import sys
import time


def _read(s, n):
    b = b""
    while len(b) < n:
        c = s.recv(n - len(b))
        if not c:
            raise OSError("vnc server closed the connection")
        b += c
    return b


def set_size(target, w, h, timeout=10.0):
    """target: a unix socket path or a TCP port on 127.0.0.1. Raises OSError on failure."""
    if str(target).isdigit():
        s = socket.create_connection(("127.0.0.1", int(target)), timeout=timeout)
    else:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(target)
    try:
        _read(s, 12)
        s.sendall(b"RFB 003.008\n")
        types = _read(s, _read(s, 1)[0])
        if 1 not in types:
            raise OSError(f"vnc server wants authentication (security types {list(types)})")
        s.sendall(b"\x01")
        if _read(s, 4) != b"\0\0\0\0":
            raise OSError("vnc security handshake failed")
        s.sendall(b"\x01")                                    # ClientInit: shared
        _read(s, 20)                                           # width, height, pixel format
        _read(s, struct.unpack(">I", _read(s, 4))[0])          # name
        encs = (-308, 0)                                       # ExtendedDesktopSize, raw
        s.sendall(struct.pack(">BxH", 2, len(encs)) + b"".join(struct.pack(">i", e) for e in encs))
        s.sendall(struct.pack(">BxHHBx", 251, w, h, 1) + struct.pack(">IHHHHI", 0, 0, 0, w, h, 0))
        time.sleep(0.5)          # let QEMU apply it before the connection goes away
    finally:
        s.close()


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "set-size":
        try:
            set_size(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
        except OSError as e:
            sys.exit(f"vmkit: set-size {sys.argv[2]}: {e}")
    else:
        sys.exit(__doc__)
