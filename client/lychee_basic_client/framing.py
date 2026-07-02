import json
import socket
from typing import Any

MAX_BODY = 99999


def read_exact(sock: socket.socket, length: int) -> bytes:
    chunks = []
    remaining = length
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise EOFError("connection closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(sock: socket.socket) -> dict:
    prefix = read_exact(sock, 5)
    try:
        length = int(prefix.decode("ascii"))
    except ValueError as exc:
        raise ValueError(f"invalid frame prefix: {prefix!r}") from exc
    if length < 0 or length > MAX_BODY:
        raise ValueError(f"invalid frame length: {length}")
    body = read_exact(sock, length)
    return json.loads(body.decode("utf-8"))


def write_frame(sock: socket.socket, message: dict[str, Any]) -> None:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_BODY:
        raise ValueError(f"message too large: {len(body)}")
    sock.sendall(f"{len(body):05d}".encode("ascii") + body)
