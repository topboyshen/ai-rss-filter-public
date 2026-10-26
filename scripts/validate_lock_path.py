"""Shared host-runner lock validation; never opens or acquires a lock."""

import os
import stat
import sys


def validate_lock_path(path: str, raw_fd: str = "") -> None:
    euid = os.geteuid()
    if not os.path.isabs(path):
        raise SystemExit("lock path must be absolute")
    parent = os.path.realpath(os.path.dirname(path))
    try:
        parent_stat = os.stat(parent)
    except OSError as exc:
        raise SystemExit(f"cannot inspect lock directory: {exc}") from exc
    if not stat.S_ISDIR(parent_stat.st_mode):
        raise SystemExit("lock parent must be a directory")
    if parent_stat.st_uid != euid or stat.S_IMODE(parent_stat.st_mode) & 0o022:
        raise SystemExit(
            "lock directory must be owned by the effective user and not group/world-writable"
        )
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        if raw_fd:
            raise SystemExit("opened lock disappeared")
        return
    except OSError as exc:
        raise SystemExit(f"cannot inspect lock: {exc}") from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise SystemExit("lock must be a regular non-symlink file")
    if path_stat.st_uid != euid:
        raise SystemExit("lock must be owned by the effective user")
    if stat.S_IMODE(path_stat.st_mode) & 0o022:
        raise SystemExit("lock must not be group/world-writable")
    if raw_fd:
        try:
            descriptor_stat = os.fstat(int(raw_fd))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"cannot inspect opened lock: {exc}") from exc
        if not stat.S_ISREG(descriptor_stat.st_mode):
            raise SystemExit("opened lock is not a regular file")
        if (path_stat.st_dev, path_stat.st_ino) != (
            descriptor_stat.st_dev,
            descriptor_stat.st_ino,
        ):
            raise SystemExit("lock changed while it was being opened")
        if descriptor_stat.st_uid != euid:
            raise SystemExit("opened lock is not owned by the effective user")
        if stat.S_IMODE(descriptor_stat.st_mode) & 0o022:
            raise SystemExit("opened lock is group/world-writable")


if __name__ == "__main__":
    validate_lock_path(*sys.argv[1:])
