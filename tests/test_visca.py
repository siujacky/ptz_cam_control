import socket
import threading
import unittest

from ptzcam.ptz import ViscaClient, ViscaError


class FakeCamera:
    """Answers each received VISCA packet with the next scripted reply (bytes, possibly several messages)."""

    def __init__(self, script):
        self.script = list(script)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        conn, _ = self.srv.accept()
        buf = b""
        while self.script:
            chunk = conn.recv(256)
            if not chunk:
                break
            buf += chunk
            while b"\xff" in buf and self.script:
                _, _, buf = buf.partition(b"\xff")
                reply = self.script.pop(0)
                if reply:
                    conn.sendall(reply)
        conn.close()

    def close(self):
        self.srv.close()


class ViscaMatchingTests(unittest.TestCase):
    def client(self, *script):
        self.cam = FakeCamera(script)
        self.addCleanup(self.cam.close)
        c = ViscaClient("127.0.0.1", self.cam.port, timeout=0.3)
        self.addCleanup(c.close)
        return c

    def test_inquiry_skips_a_late_completion(self):
        c = self.client(b"\x90\x51\xff" + b"\x90\x50\x00\x05\x09\x04\xff")
        self.assertEqual(c.send(b"\x09\x04\x47"), b"\x90\x50\x00\x05\x09\x04\xff")

    def test_command_completion_must_match_its_ack(self):
        c = self.client(b"\x90\x52\xff" + b"\x90\x41\xff" + b"\x90\x51\xff")
        self.assertEqual(c.send(b"\x01\x04\x3f\x01\xc9"), b"\x90\x51\xff")

    def test_error_on_own_socket_raises_but_stale_error_is_skipped(self):
        c = self.client(b"\x90\x41\xff\x90\x61\x41\xff", b"\x90\x62\x41\xff\x90\x41\xff\x90\x51\xff")
        with self.assertRaises(ViscaError):
            c.send(b"\x01\x04\x3f\x02\xc8")
        self.assertEqual(c.send(b"\x01\x04\x38\x02"), b"\x90\x51\xff")

    def test_syntax_error_raises_and_silence_counts_as_sent(self):
        c = self.client(b"\x90\x60\x02\xff", b"")
        with self.assertRaises(ViscaError):
            c.send(b"\x01\x04\x18\x01")
        self.assertIsNone(c.send(b"\x01\x06\x01\x05\x05\x03\x03"))


if __name__ == "__main__":
    unittest.main()
