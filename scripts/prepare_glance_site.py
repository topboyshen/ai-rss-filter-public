#!/usr/bin/env python3
"""Prepare a private local Glance site-style candidate; never install or reload it."""

import argparse
import os
from pathlib import Path

import yaml

if __package__:
    from .glance_institutional_site import site_layout
else:
    from glance_institutional_site import site_layout


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    if args.source.resolve() == args.output.resolve():
        parser.error("Output must differ from source; production install is not supported")
    with args.source.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        parser.error("Expected a Glance configuration mapping")
    candidate = site_layout(config)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        yaml.safe_dump(candidate, handle, allow_unicode=True, sort_keys=False)
    print("Local site candidate written; not installed, no services reloaded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
