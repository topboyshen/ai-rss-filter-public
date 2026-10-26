"""Private, content-addressed research artifacts; no production DB or network I/O."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def private_directory(path: Path) -> Path:
    path = path.absolute()
    if path != path.resolve():
        raise ValueError("artifact directory must not traverse symlinks")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("artifact directory must be owned by caller and mode 0700")
    return path


def write_private_bytes(path: Path, data: bytes) -> None:
    """Publish atomically, refusing overwrite (including symlinks)."""
    directory = private_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # exclusive creation, unlike replace/rename
    finally:
        os.unlink(temporary)


def write_private_json(path: Path, payload: dict) -> None:
    write_private_bytes(path, canonical_json(payload) + b"\n")
