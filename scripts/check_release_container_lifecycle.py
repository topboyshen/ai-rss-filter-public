"""Refuse ordinary Compose convergence for exact-ID controlled runtimes."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys

import yaml

KEYS = ("AI_FILTER_PRODUCTION_STARTUP_OPERATION", "AI_FILTER_PRODUCTION_STARTUP_ROOT")


def check(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 4 * 1024**2:
            raise ValueError("invalid bounded manifest")
        raw = handle.read(info.st_size + 1)
        if len(raw) != info.st_size or os.fstat(handle.fileno()).st_mtime_ns != info.st_mtime_ns:
            raise ValueError("manifest changed")
    value = yaml.safe_load(raw)
    if not isinstance(value, dict) or not isinstance(value.get("services"), dict):
        raise ValueError("invalid Compose services")
    pending, seen, count = [value], set(), 0
    while pending:
        item = pending.pop()
        count += 1
        if count > 20000:
            raise ValueError("manifest structural limit")
        if isinstance(item, str) and any(key in item for key in KEYS):
            raise ValueError("exact-ID startup requires controlled create/grant/start; ordinary recreate is forbidden")
        if isinstance(item, (dict, list)) and id(item) not in seen:
            seen.add(id(item))
            pending.extend(list(item) + list(item.values()) if isinstance(item, dict) else item)


def main():
    try:
        if len(sys.argv) != 2:
            raise ValueError("one archived manifest required")
        check(Path(sys.argv[1]))
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"Release container lifecycle refused: {error}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
