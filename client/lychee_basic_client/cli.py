import socket

from .config import parse_args
from .session import ClientSession


def main() -> int:
    config = parse_args()
    with socket.create_connection((config.host, config.port)) as sock:
        print(f"connected to {config.host}:{config.port} as player {config.player_id}")
        return ClientSession(sock, config).run()
