"""Offline pause packaging boundaries; no real clone, network, image or server."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_OUTBOX_PATCH_SHA256 = "0bf94e8fcb31dc902088122221c459f92a00854976f5a34200b21a37c40102f6"


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("pause_source_boundary", ROOT / "scripts/apply_miniflux_pause.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def make_lock(directory: Path, **changes):
    patch = directory / "0002.patch"
    patch.write_bytes(b"synthetic reviewed patch\n")
    lock = {"patch": patch.name, "patch_sha256": digest(patch.read_bytes()),
            "revision": "a" * 40, "base_patch_sha256": "b" * 64,
            "patched_files_sha256": {"synthetic.go": digest(b"expected\n")}}
    lock.update(changes)
    path = directory / "pause-source-lock.json"
    path.write_text(json.dumps(lock))
    return path, lock, patch


def test_reviewed_patch_lock_covers_every_changed_file_without_freezing_new_hash(module):
    lock, patch = module.verified_pause_lock()
    base = json.loads((ROOT / "ops/miniflux/source-lock.json").read_text())
    assert lock["revision"] == base["revision"]
    assert lock["base_patch_sha256"] == base["patch_sha256"]
    paths = {line.split(" b/", 1)[1] for line in patch.read_text().splitlines()
             if line.startswith("diff --git a/")}
    assert paths
    # Final identity includes unchanged files introduced by 0001, not only the
    # incremental 0002 diff; neither set may silently disappear from the lock.
    assert paths | set(base["patched_files_sha256"]) == set(lock["patched_files_sha256"])
    for relative, expected in lock["patched_files_sha256"].items():
        assert not Path(relative).is_absolute()
        assert ".." not in Path(relative).parts
        assert len(expected) == 64 and set(expected) <= set("0123456789abcdef")


def test_original_0001_and_its_lock_remain_unchanged():
    base = json.loads((ROOT / "ops/miniflux/source-lock.json").read_text())
    assert base["patch_sha256"] == ORIGINAL_OUTBOX_PATCH_SHA256
    assert digest((ROOT / "ops/miniflux" / base["patch"]).read_bytes()) == ORIGINAL_OUTBOX_PATCH_SHA256
    assert base["patched_files_sha256"]["internal/database/webhook_outbox.go"] == (
        "42ee96b4a961887d5387f8a0261ec6f8e9132f78eb362760451c172341640dad"
    )


def test_valid_lock_requires_actual_patch_bytes(module, tmp_path):
    path, expected, patch = make_lock(tmp_path)
    assert module.verified_pause_lock(path) == (expected, patch)
    patch.write_bytes(b"tampered\n")
    with pytest.raises(ValueError, match="differs"):
        module.verified_pause_lock(path)


@pytest.mark.parametrize("name", ["../outside.patch", "nested/inside.patch"])
def test_patch_path_cannot_escape_or_descend_beside_lock(module, tmp_path, name):
    path, _, _ = make_lock(tmp_path, patch=name)
    with pytest.raises(ValueError, match="beside"):
        module.verified_pause_lock(path)


def test_symlink_patch_to_outside_is_not_trusted(module, tmp_path):
    nested = tmp_path / "lock"
    nested.mkdir()
    path, lock, patch = make_lock(nested)
    outside = tmp_path / "outside.patch"
    outside.write_bytes(patch.read_bytes())
    patch.unlink()
    patch.symlink_to(outside)
    assert digest(outside.read_bytes()) == lock["patch_sha256"]
    with pytest.raises(ValueError, match="beside"):
        module.verified_pause_lock(path)


@pytest.mark.parametrize("field,value", [("revision", "e" * 40), ("base_patch_sha256", "f" * 64)])
def test_wrong_base_binding_rejected_before_outbox_mutates_source(module, monkeypatch, tmp_path, field, value):
    _, lock, patch = make_lock(tmp_path)
    original = tmp_path / "original.go"
    original.write_bytes(b"unmodified\n")
    base = {"revision": lock["revision"], "patch_sha256": lock["base_patch_sha256"]}
    lock[field] = value
    monkeypatch.setattr(module, "verify_outbox", lambda _: (base, patch))

    def mutate(*args, **kwargs):
        original.write_bytes(b"0001 applied\n")
        return base

    monkeypatch.setattr(module, "apply_outbox", mutate)
    with pytest.raises(ValueError, match="exact|locked|binding"):
        module.apply_fixed(tmp_path, lock, patch)
    assert original.read_bytes() == b"unmodified\n"


def test_all_file_hashes_checked_after_patch_and_mismatch_cannot_return_applied(module, monkeypatch, tmp_path):
    _, lock, patch = make_lock(tmp_path)
    base = {"revision": lock["revision"], "patch_sha256": lock["base_patch_sha256"]}
    monkeypatch.setattr(module, "verify_outbox", lambda _: (base, patch))
    monkeypatch.setattr(module, "apply_outbox", lambda *a, **k: base)
    calls = []

    def git(source, *args):
        calls.append(args)
        if args[:1] == ("apply",) and args[1] != "--check":
            (source / "synthetic.go").write_bytes(b"unexpected\n")
        return ""

    monkeypatch.setattr(module, "git", git)
    with pytest.raises(ValueError, match="differs"):
        module.apply_fixed(tmp_path, lock, patch)
    assert calls[0][:2] == ("apply", "--check")
    assert calls[-1] == ("diff", "--check")


@pytest.mark.parametrize("escape", ["../outside.go", "/outside.go"])
def test_per_file_hash_cannot_read_outside_source(module, tmp_path, escape):
    with pytest.raises(ValueError, match="escapes"):
        module.verify_patched_files(tmp_path, {escape: "0" * 64})


def test_per_file_verification_checks_every_file_and_symlink_target(module, tmp_path):
    a, b = tmp_path / "a.go", tmp_path / "b.go"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    expected = {"a.go": digest(b"a"), "b.go": digest(b"b")}
    module.verify_patched_files(tmp_path, expected)
    b.write_bytes(b"changed")
    with pytest.raises(ValueError, match="b.go"):
        module.verify_patched_files(tmp_path, expected)
    b.unlink()
    b.symlink_to(tmp_path.parent / "outside.go")
    with pytest.raises(ValueError, match="escapes"):
        module.verify_patched_files(tmp_path, expected)


@pytest.mark.parametrize("failure", [False, True])
def test_dry_run_only_modifies_disposable_copy_not_original(module, monkeypatch, tmp_path, failure):
    source = tmp_path / "source"
    source.mkdir()
    (source / "original.go").write_bytes(b"original\n")
    _, lock, patch = make_lock(tmp_path)
    monkeypatch.setattr(module, "verified_pause_lock", lambda: (lock, patch))
    monkeypatch.setattr(module, "verify_outbox", lambda value: None if value == source else pytest.fail("unverified source"))
    copied = []

    def fake_local_clone(argv, **kwargs):
        assert argv[:4] == ["git", "clone", "--quiet", "--no-hardlinks"]
        assert argv[4] == str(source)
        assert kwargs["timeout"] == 30 and kwargs["check"] is True
        target = Path(argv[5])
        assert target != source
        shutil.copytree(source, target)
        copied.append(target)
        return subprocess.CompletedProcess(argv, 0)

    def apply_clone(target, actual_lock, actual_patch):
        assert target != source and actual_lock == lock and actual_patch == patch
        (target / "original.go").write_bytes(b"changed only in isolated copy\n")
        if failure:
            raise ValueError("synthetic post-apply verification failure")
        return {"status": "applied", "deployment": False}

    monkeypatch.setattr(module.subprocess, "run", fake_local_clone)
    monkeypatch.setattr(module, "apply_fixed", apply_clone)
    if failure:
        with pytest.raises(ValueError, match="synthetic"):
            module.apply(source)
    else:
        assert module.apply(source) == {"status": "validated_only", "deployment": False}
    assert (source / "original.go").read_bytes() == b"original\n"
    assert copied and all(not path.exists() for path in copied)


def test_source_preflight_failure_prevents_clone_or_application(module, monkeypatch, tmp_path):
    _, lock, patch = make_lock(tmp_path)
    monkeypatch.setattr(module, "verified_pause_lock", lambda: (lock, patch))

    def failed(_):
        raise ValueError("source not clean")

    monkeypatch.setattr(module, "verify_outbox", failed)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: pytest.fail("clone reached"))
    monkeypatch.setattr(module, "apply_fixed", lambda *a: pytest.fail("apply reached"))
    for execute in (False, True):
        with pytest.raises(ValueError, match="clean"):
            module.apply(tmp_path, execute)


def test_explicit_apply_still_preflights_and_reports_no_deployment(module, monkeypatch, tmp_path):
    _, lock, patch = make_lock(tmp_path)
    monkeypatch.setattr(module, "verified_pause_lock", lambda: (lock, patch))
    events = []
    monkeypatch.setattr(module, "verify_outbox", lambda source: events.append(("verify", source)))

    def apply(source, actual_lock, actual_patch):
        assert events == [("verify", source)]
        assert actual_lock == lock and actual_patch == patch
        return {"status": "applied", "deployment": False}

    monkeypatch.setattr(module, "apply_fixed", apply)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: pytest.fail("explicit path must not clone"))
    assert module.apply(tmp_path, True) == {"status": "applied", "deployment": False}
