import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


HELPER = Path(__file__).resolve().parents[1] / "scripts" / "validate_lock_path.py"
SPEC = importlib.util.spec_from_file_location("lock_path_validator", HELPER)
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


def _check(path, fd="", *, cwd=None, pass_fds=()):
    return subprocess.run(
        [sys.executable, str(HELPER), str(path), str(fd)],
        cwd=cwd,
        pass_fds=pass_fds,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )


def test_missing_lock_is_allowed_before_open_without_creating_it(tmp_path):
    lock = tmp_path / "task.lock"
    result = _check(lock, cwd=tmp_path.parent)
    assert result.returncode == 0, result.stderr
    assert not lock.exists()


def test_opened_lock_must_still_exist(tmp_path):
    lock = tmp_path / "task.lock"
    with lock.open("w") as opened:
        lock.unlink()
        result = _check(lock, opened.fileno(), pass_fds=(opened.fileno(),))
    assert result.returncode != 0
    assert "opened lock disappeared" in result.stderr


def test_relative_lock_is_rejected(tmp_path):
    result = _check("task.lock", cwd=tmp_path)
    assert result.returncode != 0
    assert "lock path must be absolute" in result.stderr
    assert not (tmp_path / "task.lock").exists()


@pytest.mark.parametrize("broken", [False, True])
def test_symlink_is_rejected_without_touching_target(tmp_path, broken):
    target = tmp_path / "target"
    if not broken:
        target.write_text("unchanged\n")
    lock = tmp_path / "task.lock"
    lock.symlink_to(target)
    result = _check(lock)
    assert result.returncode != 0
    assert "regular non-symlink" in result.stderr
    if broken:
        assert not target.exists()
    else:
        assert target.read_text() == "unchanged\n"


def test_nonregular_lock_is_rejected(tmp_path):
    lock = tmp_path / "task.lock"
    lock.mkdir()
    result = _check(lock)
    assert result.returncode != 0
    assert "regular non-symlink" in result.stderr


def test_missing_parent_is_rejected(tmp_path):
    result = _check(tmp_path / "missing" / "task.lock")
    assert result.returncode != 0
    assert "cannot inspect lock directory" in result.stderr


def test_nondirectory_parent_is_rejected(tmp_path):
    parent = tmp_path / "not-a-directory"
    parent.write_text("")
    result = _check(parent / "task.lock")
    assert result.returncode != 0
    assert "lock parent must be a directory" in result.stderr


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666])
def test_group_or_world_writable_lock_is_rejected(tmp_path, mode):
    lock = tmp_path / "task.lock"
    lock.write_text("unchanged\n")
    lock.chmod(mode)
    result = _check(lock)
    assert result.returncode != 0
    assert "lock must not be group/world-writable" in result.stderr
    assert lock.read_text() == "unchanged\n"


def test_group_or_world_writable_parent_is_rejected(tmp_path):
    parent = tmp_path / "locks"
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    result = _check(parent / "task.lock")
    assert result.returncode != 0
    assert "lock directory must be owned" in result.stderr


def test_foreign_parent_owner_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(VALIDATOR.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(SystemExit, match="lock directory must be owned"):
        VALIDATOR.validate_lock_path(str(tmp_path / "task.lock"))


def test_foreign_lock_owner_is_rejected(tmp_path, monkeypatch):
    lock = tmp_path / "task.lock"
    lock.write_text("")
    actual = lock.stat()
    monkeypatch.setattr(
        VALIDATOR.os, "lstat",
        lambda _path: SimpleNamespace(st_mode=actual.st_mode, st_uid=os.geteuid() + 1),
    )
    with pytest.raises(SystemExit, match="lock must be owned"):
        VALIDATOR.validate_lock_path(str(lock))


def test_inode_replacement_after_open_is_rejected(tmp_path):
    lock = tmp_path / "task.lock"
    with lock.open("w") as opened:
        replacement = tmp_path / "replacement"
        replacement.write_text("replacement\n")
        replacement.replace(lock)
        result = _check(lock, opened.fileno(), pass_fds=(opened.fileno(),))
    assert result.returncode != 0
    assert "lock changed while it was being opened" in result.stderr
    assert lock.read_text() == "replacement\n"


@pytest.mark.parametrize("raw_fd", ["not-a-descriptor", "99999"])
def test_invalid_opened_descriptor_is_rejected(tmp_path, raw_fd):
    lock = tmp_path / "task.lock"
    lock.write_text("")
    result = _check(lock, raw_fd)
    assert result.returncode != 0
    assert "cannot inspect opened lock" in result.stderr


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("st_mode", 0o040700, "opened lock is not a regular file"),
        ("st_uid", -1, "opened lock is not owned"),
        ("st_mode", 0o100622, "opened lock is group/world-writable"),
    ],
)
def test_opened_descriptor_owns_its_safety_checks(
    tmp_path, monkeypatch, field, value, message
):
    lock = tmp_path / "task.lock"
    lock.write_text("")
    lock.chmod(0o600)
    actual = lock.stat()
    descriptor = SimpleNamespace(
        st_mode=actual.st_mode, st_uid=actual.st_uid,
        st_dev=actual.st_dev, st_ino=actual.st_ino,
    )
    setattr(descriptor, field, value)
    monkeypatch.setattr(VALIDATOR.os, "fstat", lambda _fd: descriptor)
    with pytest.raises(SystemExit, match=message):
        VALIDATOR.validate_lock_path(str(lock), "8")


def test_repeated_validation_preserves_path_descriptor_and_contents(tmp_path):
    lock = tmp_path / "task.lock"
    lock.write_text("unchanged\n")
    lock.chmod(0o600)
    with lock.open("a") as opened:
        identity = os.fstat(opened.fileno())
        for _ in range(2):
            result = _check(
                lock, opened.fileno(), cwd=tmp_path.parent,
                pass_fds=(opened.fileno(),),
            )
            assert result.returncode == 0, result.stderr
        current = lock.stat()
        assert (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino)
    assert lock.read_text() == "unchanged\n"
    assert lock.stat().st_mode & 0o777 == 0o600
