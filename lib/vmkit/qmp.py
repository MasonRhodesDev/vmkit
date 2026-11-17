import json
import socket
import time


class QMPError(Exception):
    pass


class QMPClosed(QMPError):
    pass


class QMPInterrupted(QMPError):
    pass


class QMP:
    POLL = 0.25

    def __init__(self, path, timeout=30.0, stop=None):
        self.timeout, self.stop, self.buf = timeout, stop, b""
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(path)
        except OSError as e:
            raise QMPError(f"cannot connect to {path}: {e}") from None
        if stop is not None:
            self.sock.settimeout(min(timeout, self.POLL))
        self.readline()
        self.cmd("qmp_capabilities")

    def readline(self):
        deadline = time.monotonic() + self.timeout
        while b"\n" not in self.buf:
            if self.stop is not None and self.stop.is_set():
                raise QMPInterrupted("stopped while waiting for a reply")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"no complete reply in {self.timeout:g} s")
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                if self.stop is None or time.monotonic() >= deadline:
                    raise
                continue
            if not chunk:
                line, self.buf = self.buf, b""
                return line
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line + b"\n"

    def send(self, name, args=None):
        m = {"execute": name}
        if args is not None:
            m["arguments"] = args
        self.sock.sendall((json.dumps(m) + "\n").encode())

    def recv(self):
        while True:
            line = self.readline()
            if not line:
                raise QMPClosed("connection closed before a reply")
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
