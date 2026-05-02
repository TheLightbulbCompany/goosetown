"""IPC helpers — Unix socket communication with the daemon."""

import json
import socket
from pathlib import Path


def ipc_send(sock_path: Path, payload: dict, timeout: float = 130.0) -> dict:
    """Send a JSON payload to the daemon socket and return the JSON response.

    Uses shutdown(SHUT_WR) as EOF signal so the daemon knows the frame is
    complete.  Reads until the socket closes on the daemon side.
    """
    raw = json.dumps(payload).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(sock_path))
        s.sendall(raw)
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    return json.loads(b"".join(chunks).decode())
