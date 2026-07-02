import json
import socket
import sys
from typing import Any, Optional

from .config import Config
from .framing import read_frame, write_frame
from .messages import heartbeat_action, ready_message, registration_message


class ClientSession:
    def __init__(self, sock: socket.socket, config: Config) -> None:
        self._sock = sock
        self._config = config
        self._match_id = ""

    def run(self) -> int:
        self._send_registration()

        while True:
            try:
                message = read_frame(self._sock)
            except EOFError:
                print("connection closed")
                return 0

            result = self._handle_message(message)
            if result is not None:
                return result

    def _send_registration(self) -> None:
        write_frame(self._sock, registration_message(self._config))

    def _handle_message(self, message: dict[str, Any]) -> Optional[int]:
        msg_name = message.get("msg_name")
        data = message.get("msg_data") or {}

        if msg_name == "start":
            self._handle_start(data)
        elif msg_name == "inquire":
            self._handle_inquire(data)
        elif msg_name == "over":
            print("over received")
            return 0
        elif msg_name == "error":
            print(f"error received: {json.dumps(message, ensure_ascii=False)}", file=sys.stderr)
            return 1
        else:
            print(f"ignored msg_name={msg_name}")
        return None

    def _handle_start(self, data: dict[str, Any]) -> None:
        self._match_id = data["matchId"]
        round_no = data["round"]
        print(f"start match={self._match_id} round={round_no}")
        write_frame(self._sock, ready_message(self._match_id, round_no, self._config.player_id))

    def _handle_inquire(self, data: dict[str, Any]) -> None:
        round_no = data["round"]
        print(f"inquire round={round_no} -> heartbeat")
        write_frame(self._sock, heartbeat_action(self._match_id, round_no, self._config.player_id))
