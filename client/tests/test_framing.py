import socket
import unittest

from lychee_basic_client.framing import read_frame, write_frame


class FramingTests(unittest.TestCase):
    def test_write_and_read_frame(self) -> None:
        left, right = socket.socketpair()
        try:
            write_frame(left, {"msg_name": "ping"})
            self.assertEqual({"msg_name": "ping"}, read_frame(right))
        finally:
            left.close()
            right.close()


if __name__ == "__main__":
    unittest.main()
