"""Malware scanning of uploads with ClamAV (clamd's INSTREAM command over TCP or a Unix socket)."""

from __future__ import annotations

import logging
import socket
import struct
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlparse

from ..config import get_settings

log = logging.getLogger("dah.scan")

CHUNK = 256 * 1024


class InfectedFile(ValueError):
    def __init__(self, filename: str, signature: str):
        super().__init__(f"{filename!r} was rejected by the virus scanner: {signature}")
        self.filename = filename
        self.signature = signature


class ScanError(RuntimeError):
    """The scanner couldn't give a verdict (unreachable, timeout, file over clamd's StreamMaxLength…)."""


def enabled() -> bool:
    return bool(get_settings().clamav_url)


def _connect() -> socket.socket:
    s = get_settings()
    u = urlparse(s.clamav_url)
    if u.scheme == "tcp":
        return socket.create_connection((u.hostname or "localhost", u.port or 3310), timeout=s.clamav_timeout_s)
    if u.scheme == "unix":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(s.clamav_timeout_s)
        sock.connect(u.path)
        return sock
    raise ScanError(f"unsupported DAH_CLAMAV_URL scheme {u.scheme!r} (use tcp://host:port or unix:///path)")


def _instream(stream: BinaryIO) -> str:
    """Send ``stream`` to clamd and return its reply, e.g. 'stream: OK' or 'stream: Eicar-Signature FOUND'."""
    with _connect() as sock:
        sock.sendall(b"zINSTREAM\0")
        try:
            while chunk := stream.read(CHUNK):
                sock.sendall(struct.pack("!L", len(chunk)) + chunk)
            sock.sendall(struct.pack("!L", 0))
        except (BrokenPipeError, ConnectionResetError):
            pass  # clamd hangs up once StreamMaxLength is exceeded; its reply says so
        reply = b""
        while not reply.endswith(b"\0") and (part := sock.recv(4096)):
            reply += part
    return reply.rstrip(b"\0").decode("utf-8", "replace").strip()


def scan_stream(stream: BinaryIO, filename: str) -> None:
    """Raise InfectedFile if clamd finds something; ScanError if it can't say (unless fail-open). No-op when disabled."""
    if not enabled():
        return
    try:
        reply = _instream(stream)
    except OSError as e:
        reply = f"ERROR {type(e).__name__}: {e}"
    if reply.endswith("FOUND"):
        signature = reply.removeprefix("stream:").removesuffix("FOUND").strip()
        log.warning("upload %r rejected: %s", filename, signature)
        raise InfectedFile(filename, signature)
    if reply.endswith("OK"):
        return
    if get_settings().clamav_fail_open:
        log.error("virus scan of %r failed, accepting it anyway (DAH_CLAMAV_FAIL_OPEN): %s", filename, reply)
        return
    raise ScanError(f"virus scan of {filename!r} failed: {reply or 'no reply from clamd'}")


def scan_file(path: Path, filename: str) -> None:
    if enabled():
        with open(path, "rb") as fh:
            scan_stream(fh, filename)
