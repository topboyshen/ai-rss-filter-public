"""Apply the locked Miniflux outbox then pause patches to a separate clean clone.

Default dry-run creates a temporary local clone and verifies the entire result.
There is no network, production access, image mutation or deployment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from apply_miniflux_outbox import apply as apply_outbox
from apply_miniflux_outbox import git, verify as verify_outbox, verify_patched_files

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "ops/miniflux/pause-source-lock.json"


def verified_pause_lock(lock_path: Path = LOCK) -> tuple[dict, Path]:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    patch = (lock_path.parent / lock["patch"]).resolve()
    if patch.parent != lock_path.parent.resolve():
        raise ValueError("pause patch must stay beside the lock")
    if hashlib.sha256(patch.read_bytes()).hexdigest() != lock["patch_sha256"]:
        raise ValueError("pause patch differs from source lock")
    return lock, patch


def apply_fixed(source: Path, lock: dict, patch: Path) -> dict:
    outbox, _ = verify_outbox(source)
    if outbox["revision"] != lock["revision"] or outbox["patch_sha256"] != lock["base_patch_sha256"]:
        raise ValueError("pause patch must follow the exact locked outbox patch")
    apply_outbox(source, execute=True)
    git(source, "apply", "--check", "--unidiff-zero", str(patch))
    git(source, "apply", "--unidiff-zero", str(patch))
    git(source, "diff", "--check")
    verify_patched_files(source, lock["patched_files_sha256"])
    return {"status": "applied", "revision": lock["revision"],
            "patch_sha256": lock["patch_sha256"], "deployment": False}


def apply(source: Path, execute: bool = False) -> dict:
    lock, patch = verified_pause_lock()
    verify_outbox(source)
    if execute:
        return apply_fixed(source, lock, patch)
    with tempfile.TemporaryDirectory(prefix="miniflux-pause-verify-") as directory:
        clone = Path(directory) / "source"
        subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(source), str(clone)],
                       capture_output=True, check=True, timeout=30)
        result = apply_fixed(clone, lock, patch)
    return {**result, "status": "validated_only"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(apply(args.source.resolve(), args.apply)))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "failed", "error": str(error), "deployment": False}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
