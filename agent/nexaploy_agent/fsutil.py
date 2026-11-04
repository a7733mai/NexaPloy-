"""Small filesystem helpers: atomic writes, ownership, hashing."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, content: str | bytes, mode: int | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode() if isinstance(content, str) else content
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        elif path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_if_changed(path: Path, content: str, mode: int | None = None) -> bool:
    """Write ``content`` unless identical; return True when the file changed."""
    try:
        if Path(path).read_text() == content:
            if mode is not None:
                os.chmod(path, mode)
            return False
    except OSError:
        pass
    atomic_write(path, content, mode=mode)
    return True


def chown(path: Path, uid: int, gid: int, recursive: bool = False) -> None:
    """chown when running as root; silently a no-op otherwise (tests, dev)."""
    if os.geteuid() != 0:
        return
    os.chown(path, uid, gid)
    if recursive and Path(path).is_dir():
        for dirpath, dirnames, filenames in os.walk(path):
            for name in dirnames + filenames:
                try:
                    os.lchown(os.path.join(dirpath, name), uid, gid)
                except OSError:
                    pass


def ensure_dir(path: Path, uid: int | None = None, gid: int | None = None, mode: int = 0o755) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    if uid is not None:
        chown(path, uid, gid if gid is not None else uid)
    return path


def sha256_file(path: Path, bufsize: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(bufsize):
            h.update(chunk)
    return h.hexdigest()
