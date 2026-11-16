"""Private bounded transfer evidence, with no cleanup or expiry deletion."""
from __future__ import annotations

import os
from pathlib import Path

from services import admission_fence as gate
from services import sender_drain_contract as originals

MAX_RECORD_BYTES = 8 * 1024**2


def directory(path, *, create=False):
    path = Path(path)
    gate._require(path.is_absolute() and str(path) == os.path.normpath(path), "absolute transfer path required")
    for parent in (path, *path.parents):
        gate._require(not parent.is_symlink(), "transfer symlink forbidden")
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    gate._directory(path)
    if create:
        gate._sync_directory(path)
        gate._sync_directory(path.parent)
    return path


def identity(path):
    directory(path)
    info = Path(path).lstat()
    return {"device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid}


def create_lock(path):
    """Create once; existing lock identity is never replaced."""
    directory(Path(path).parent)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    gate._sync_directory(Path(path).parent)


def read(path):
    directory(Path(path).parent)
    return gate._parsed(originals.read_blob(path, MAX_RECORD_BYTES))


def confirm(path):
    value = read(path)
    gate._confirm_file(path)
    gate._sync_directory(Path(path).parent)
    return value


def write(path, value):
    """Recover only identical full pending bytes; keep every partial failure."""
    path = Path(path)
    directory(path.parent)
    raw = gate._encoded(value)
    gate._require(0 < len(raw) <= MAX_RECORD_BYTES, "transfer record capacity")
    pending = path.with_name(path.name + ".pending")
    if gate._present(pending):
        gate._require(read(pending) == value, "unconfirmed transfer write differs")
        gate._confirm_file(pending)
    else:
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    os.replace(pending, path)
    gate._sync_directory(path.parent)


def once(path, value):
    if gate._present(path):
        gate._require(confirm(path) == value, "immutable transfer evidence differs")
    else:
        write(path, value)
