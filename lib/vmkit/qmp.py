"""Tiny QMP client (stdlib). Errors in a reply raise QMPError."""
import json
import socket


class QMPError(Exception):
    pass


class QMP:
    def __init__(self, path, timeout=30.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(path)
        except OSError as e:
            raise QMPError(f"cannot connect to {path}: {e}") from None
        self.f = self.sock.makefile("rwb", buffering=0)
        self.f.readline()                       # greeting
        self.cmd("qmp_capabilities")

    def send(self, name, args=None):
        m = {"execute": name}
        if args is not None:
            m["arguments"] = args
        self.f.write((json.dumps(m) + "\n").encode())

    def recv(self):
        """Next reply, skipping async events. Returns the reply object."""
        while True:
            line = self.f.readline()
            if not line:
                raise QMPError("connection closed before a reply")
            o = json.loads(line)
            if "return" in o or "error" in o:
                return o

    def cmd(self, name, args=None):
        self.send(name, args)
        o = self.recv()
        if "error" in o:
            e = o["error"]
            raise QMPError(f"{e.get('class', '')}: {e.get('desc', '')}")
        return o["return"]

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
