"""Raw zone: immutable, content-addressed blob storage. Identical bytes are stored once."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO, Callable

from .config import get_settings

CHUNK = 1024 * 1024


class BlobStore:
    def __init__(self, root: Path | None = None):
        self.root = root or get_settings().blob_dir
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, sha256: str) -> Path:
        if len(sha256) != 64 or not all(c in "0123456789abcdef" for c in sha256):
            raise ValueError("invalid blob id")
        return self.root / sha256[:2] / sha256[2:4] / sha256

    def exists(self, sha256: str) -> bool:
        return self.path(sha256).exists()

    def put_stream(self, src: BinaryIO, max_bytes: int | None = None, check: Callable[[Path], None] | None = None) -> tuple[str, int]:
        """Stream into a temp file while hashing, then move into place. Returns (sha256, size).

        ``check(tmp_path)`` runs before the move; if it raises, nothing is stored.
        """
        h = hashlib.sha256()
        size = 0
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".upload-")
        try:
            with os.fdopen(fd, "wb") as out:
                while chunk := src.read(CHUNK):
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise ValueError(f"upload exceeds {max_bytes} bytes")
                    h.update(chunk)
                    out.write(chunk)
            if check is not None:
                check(Path(tmp))
            sha = h.hexdigest()
            dest = self.path(sha)
            if dest.exists():
                os.unlink(tmp)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(tmp, dest)
                os.chmod(dest, 0o444)
            return sha, size
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def put_bytes(self, data: bytes) -> tuple[str, int]:
        import io

        return self.put_stream(io.BytesIO(data))

    def open(self, sha256: str) -> BinaryIO:
        return open(self.path(sha256), "rb")
