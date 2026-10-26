#!/usr/bin/env python3
"""Byte-accurate validation for file-backed production service secrets.

The secret is read only from the supplied file descriptor.  Its value is never
placed in a command argument, environment variable, diagnostic, or log line.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


MIN_SECRET_BYTES = 32
MAX_SECRET_BYTES = 4096
MAX_FILE_BYTES = MAX_SECRET_BYTES + 1


def valid_runtime_secret_file(path: Path) -> bool:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return False
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size < MIN_SECRET_BYTES
            or metadata.st_size > MAX_FILE_BYTES
        ):
            return False
        payload = bytearray()
        while len(payload) <= MAX_FILE_BYTES:
            chunk = os.read(descriptor, MAX_FILE_BYTES + 1 - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) != metadata.st_size or len(payload) > MAX_FILE_BYTES:
            return False
    except OSError:
        return False
    finally:
        os.close(descriptor)

    # Exactly one final LF is accepted for ordinary text-file tooling.  Every
    # secret byte itself must be an ASCII graphic (0x21..0x7e), which rejects
    # NUL, non-ASCII, spaces, CR, tabs, and embedded/multiple newlines.
    if payload.endswith(b"\n"):
        payload = payload[:-1]
    return (
        MIN_SECRET_BYTES <= len(payload) <= MAX_SECRET_BYTES
        and all(0x21 <= value <= 0x7E for value in payload)
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or not args[0] or "\x00" in args[0]:
        return 2
    return 0 if valid_runtime_secret_file(Path(args[0])) else 1


if __name__ == "__main__":
    raise SystemExit(main())
