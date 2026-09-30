"""A fake QMP server for tests: threaded AF_UNIX listener, one handler per command.

    srv = FakeQMP(path)                 # starts listening
    srv.handlers["screendump"] = fn     # fn(args) -> reply dict, or None to send nothing
    ...
    srv.requests                        # [(execute, arguments), ...] in arrival order
    srv.stop()

Default handlers: qmp_capabilities -> {}, screendump -> writes a solid PPM
(srv.size, srv.color) and returns {}, anything else -> {}.
"""
import json
import os
import socket
import threading


def ppm(w, h, rgb=(16, 16, 16)):
    return b"P6\n%d %d\n255\n" % (w, h) + bytes(rgb) * (w * h)


class FakeQMP:
    def __init__(self, path, size=(64, 48), color=(16, 16, 16)):
        self.path, self.size, self.color = path, size, color
        self.requests = []
        self.handlers = {}
        self.conns = []
        self.closing = threading.Event()
        try:
            os.unlink(path)
        except OSError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(8)
        threading.Thread(target=self._accept, daemon=True).start()

    def screendump(self, args):
        with open(args["filename"], "wb") as f:
            f.write(ppm(*self.size, self.color))
        return {"return": {}}

    def _reply(self, ex, args):
        if ex in self.handlers:
            return self.handlers[ex](args)
        if ex == "screendump":
            return self.screendump(args)
        return {"return": {}}

    def _accept(self):
        while not self.closing.is_set():
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(c)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        try:
            f = c.makefile("rwb", buffering=0)
            f.write(b'{"QMP": {"version": {}, "capabilities": []}}\n')
            for line in f:
                m = json.loads(line)
                ex, args = m.get("execute"), m.get("arguments", {})
                self.requests.append((ex, args))
                r = self._reply(ex, args)
                if r is not None:
                    f.write((json.dumps(r) + "\n").encode())
        except (OSError, ValueError):
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass

    def commands(self):
        return [ex for ex, _ in self.requests]

    def stop(self):
        self.closing.set()
        try:
            self.sock.close()
        except OSError:
            pass
        for c in self.conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
                c.close()
            except OSError:
                pass
        try:
            os.unlink(self.path)
        except OSError:
            pass
