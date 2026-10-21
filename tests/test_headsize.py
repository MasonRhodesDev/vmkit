"""lib/vmkit/headsize.py against a fake VNC server on a unix socket."""
import os
import shutil
import socket
import struct
import tempfile
import threading
import unittest

from vmkit import headsize


class FakeVNC:
    """Speaks just enough RFB 3.8: no auth, then answers SetDesktopSize."""

    def __init__(self, path, status=0, auth=(1,), reply=True):
        self.status, self.auth, self.reply = status, auth, reply
        self.got = {}
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(path)
        self.srv.listen(1)
        self.t = threading.Thread(target=self.serve, daemon=True)
        self.t.start()

    def serve(self):
        c, _ = self.srv.accept()
        try:
            c.sendall(b"RFB 003.008\n")
            self.got["version"] = headsize.recv_exact(c, 12)
            c.sendall(bytes([len(self.auth), *self.auth]))
            if 1 not in self.auth:
                return
            headsize.recv_exact(c, 1)
            c.sendall(struct.pack(">I", 0))
            headsize.recv_exact(c, 1)
            name = b"fake"
            c.sendall(struct.pack(">HH", 640, 480) + bytes(16) + struct.pack(">I", len(name)) + name)
            while True:
                t = headsize.recv_exact(c, 1)[0]
                if t == 2:
                    n = struct.unpack(">xH", headsize.recv_exact(c, 3))[0]
                    self.got["encodings"] = struct.unpack(">%di" % n, headsize.recv_exact(c, 4 * n))
                elif t == 251:
                    w, h, n = struct.unpack(">xHHBx", headsize.recv_exact(c, 7))
                    screens = [struct.unpack(">IHHHHI", headsize.recv_exact(c, 16)) for _ in range(n)]
                    self.got["size"] = (w, h, screens)
                    if self.reply:
                        # FramebufferUpdate with one ExtendedDesktopSize rectangle: x=1 (our request), y=status
                        c.sendall(struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 1, self.status, w, h, -308)
                                  + struct.pack(">B3x", 1) + struct.pack(">IHHHHI", 0, 0, 0, w, h, 0))
                elif t == 3:
                    headsize.recv_exact(c, 9)
        except (EOFError, OSError):
            pass
        finally:
            c.close()


class HeadSize(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="vmkit-headsize-")
        self.path = os.path.join(self.td, "head1.sock")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_accepted_size_is_requested_as_one_screen(self):
        srv = FakeVNC(self.path)
        self.assertEqual(headsize.set_size(self.path, 1024, 768, timeout=3), (True, 0))
        self.assertEqual(srv.got["size"], (1024, 768, [(0, 0, 0, 1024, 768, 0)]))
        self.assertEqual(srv.got["encodings"], (-308,))

    def test_qemu_answers_request_forwarded(self):
        FakeVNC(self.path, status=4)
        self.assertEqual(headsize.set_size(self.path, 1024, 768, timeout=3), (True, 4))

    def test_refused_size(self):
        FakeVNC(self.path, status=3)
        self.assertEqual(headsize.set_size(self.path, 1024, 768, timeout=3), (False, 3))

    def test_no_reply_is_a_timeout(self):
        FakeVNC(self.path, reply=False)
        with self.assertRaises(TimeoutError):
            headsize.set_size(self.path, 1024, 768, timeout=0.5)

    def test_authentication_is_an_error(self):
        FakeVNC(self.path, auth=(2,))
        with self.assertRaisesRegex(ValueError, "authentication"):
            headsize.set_size(self.path, 1024, 768, timeout=3)

    def test_main_exit_codes(self):
        FakeVNC(self.path)
        self.assertEqual(headsize.main([self.path, "800x600"]), 0)
        self.assertEqual(headsize.main([os.path.join(self.td, "none.sock"), "800x600"]), 2)


if __name__ == "__main__":
    unittest.main()
