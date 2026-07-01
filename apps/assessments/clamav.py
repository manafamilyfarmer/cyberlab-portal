"""Minimal clamd client (INSTREAM + PING) over TCP — no shared volume, no
third-party dependency. Raises ConnectionError when clamd is not reachable/ready
so the caller can retry with backoff.
"""
import socket
import struct

CHUNK = 8192


def _connect(host, port, timeout):
    try:
        return socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise ConnectionError(f"clamd unreachable at {host}:{port}: {exc}") from exc


def ping(host, port, timeout=5):
    s = _connect(host, port, timeout)
    try:
        s.sendall(b"zPING\x00")
        resp = s.recv(64)
        return resp.strip(b"\x00").strip() == b"PONG"
    finally:
        s.close()


def instream_scan(host, port, data, timeout=30):
    """Stream bytes to clamd via INSTREAM.

    Returns ("clean"|"infected"|"error", detail). Raises ConnectionError if
    clamd can't be reached (so the task retries).
    """
    s = _connect(host, port, timeout)
    try:
        s.sendall(b"zINSTREAM\x00")
        view = memoryview(data)
        for i in range(0, len(view), CHUNK):
            chunk = view[i:i + CHUNK]
            s.sendall(struct.pack("!I", len(chunk)) + bytes(chunk))
        s.sendall(struct.pack("!I", 0))  # zero-length chunk terminates the stream
        resp = b""
        while b"\x00" not in resp:
            part = s.recv(4096)
            if not part:
                break
            resp += part
    finally:
        s.close()

    text = resp.strip(b"\x00").decode(errors="replace").strip()
    # Examples: "stream: OK", "stream: Eicar-Test-Signature FOUND", "... ERROR"
    if text.endswith("OK"):
        return "clean", text
    if text.endswith("FOUND"):
        sig = text.split(":", 1)[-1].strip().rsplit(" ", 1)[0].strip()
        return "infected", sig
    return "error", text
