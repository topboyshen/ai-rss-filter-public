"""Verify and apply only the locked upstream Miniflux source patch.

No fetch, build, production access or deployment. Default is a dry-run. The
caller must supply a clean separate clone at the exact source-lock revision.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "ops/miniflux/source-lock.json"


def git(source: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(source), *args], text=True,
                            capture_output=True, check=False, timeout=30)
    if result.returncode:
        raise ValueError("locked source check failed: " + result.stderr.strip())
    return result.stdout.strip()


def archive_hash(source: Path) -> str:
    result = subprocess.run(["git", "-C", str(source), "archive", "--format=tar", "HEAD"],
                            capture_output=True, check=True, timeout=30)
    return hashlib.sha256(result.stdout).hexdigest()


def verify_patched_files(source: Path, expected: dict[str, str]) -> None:
    for relative, digest in expected.items():
        path = (source / relative).resolve()
        if not path.is_relative_to(source.resolve()):
            raise ValueError("patched file escapes source directory")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("patched file differs from reviewed source lock: " + relative)


def verify(source: Path, lock_path: Path = LOCK) -> tuple[dict, Path]:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    patch = lock_path.parent / lock["patch"]
    if patch.parent.resolve() != lock_path.parent.resolve():
        raise ValueError("patch must remain beside the source lock")
    if hashlib.sha256(patch.read_bytes()).hexdigest() != lock["patch_sha256"]:
        raise ValueError("patch hash differs from reviewed source lock")
    if git(source, "rev-parse", "HEAD") != lock["revision"]:
        raise ValueError("upstream revision differs from fixed source lock")
    if git(source, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("upstream worktree is not clean")
    if archive_hash(source) != lock["base_git_archive_tar_sha256"]:
        raise ValueError("upstream archive differs from fixed source lock")
    git(source, "apply", "--check", "--unidiff-zero", str(patch))
    return lock, patch


def apply(source: Path, execute: bool = False) -> dict:
    lock, patch = verify(source)
    if execute:
        git(source, "apply", "--unidiff-zero", str(patch))
        git(source, "diff", "--check")
        verify_patched_files(source, lock["patched_files_sha256"])
    return {"status": "applied" if execute else "validated_only", "revision": lock["revision"],
            "patch_sha256": lock["patch_sha256"], "deployment": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="Apply only to the verified clean local clone")
    args = parser.parse_args()
    try:
        print(json.dumps(apply(args.source.resolve(), args.apply)))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "failed", "error": str(error), "deployment": False}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
