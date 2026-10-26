"""A fresh public Git checkout must contain every locked Miniflux patch."""

import hashlib
import json
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
PATCHES = (
    "ops/miniflux/0001-durable-webhook-outbox.patch",
    "ops/miniflux/0002-unified-pause-and-held-originals.patch",
)


@pytest.mark.parametrize("lock_name", ["source-lock.json", "pause-source-lock.json"])
def test_public_source_contains_exact_locked_patch(lock_name):
    directory = ROOT / "ops" / "miniflux"
    lock = json.loads((directory / lock_name).read_text())
    patch = directory / lock["patch"]
    assert patch.is_file() and not patch.is_symlink()
    assert hashlib.sha256(patch.read_bytes()).hexdigest() == lock["patch_sha256"]


def test_fresh_export_includes_only_two_named_patch_exceptions(tmp_path):
    repo = tmp_path / "fresh"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
    refused = (
        "draft.patch",
        "ops/miniflux/unreviewed.patch",
        "other/0001-durable-webhook-outbox.patch",
        "data/diagnostics/0002-unified-pause-and-held-originals.patch",
        ".env",
        "secrets/private-key",
        "data/cache.db",
    )
    for name in (*PATCHES, *refused):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic input\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z"],
        check=True, capture_output=True,
    )
    tracked = set(result.stdout.decode().strip("\0").split("\0"))
    assert tracked == {".gitignore", *PATCHES}
