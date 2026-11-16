import fcntl
import os

import pytest

from services import admission_fence as gate
from services.production_migration import file_identity
from services.production_release_lock import acquire_for_phase, retained_release_lock


@pytest.fixture
def lock_path(tmp_path):
    path = tmp_path / "release.lock"
    path.touch(mode=0o600)
    return path


def busy(path):
    fd = os.open(path, os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def test_retained_owner_keeps_release_locked_between_phases(lock_path):
    expected = file_identity(lock_path)
    with retained_release_lock(lock_path) as capability:
        for _phase in ("migration", "activation", "finalize"):
            fd = acquire_for_phase(expected, capability)
            assert 10 <= fd <= 199
            busy(lock_path)
            os.close(fd)
            busy(lock_path)
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
    with pytest.raises(gate.AdmissionError, match="not live"):
        capability.check()


def test_borrowed_other_registry_or_serialized_capability_rejected(lock_path, tmp_path):
    other = tmp_path / "other.lock"
    other.touch(mode=0o600)
    with retained_release_lock(lock_path) as capability:
        with pytest.raises(gate.AdmissionError, match="different release"):
            acquire_for_phase(file_identity(other), capability)
        with pytest.raises(gate.AdmissionError, match="actual retained"):
            acquire_for_phase(file_identity(lock_path), {"fd": capability.fd})


def test_replaced_path_does_not_borrow_old_locked_inode(lock_path):
    with pytest.raises(gate.AdmissionError, match="inode changed"):
        with retained_release_lock(lock_path) as capability:
            lock_path.rename(lock_path.with_suffix(".retained"))
            lock_path.touch(mode=0o600)
            capability.check()


def test_accidentally_unlocked_descriptor_does_not_prove_ownership(lock_path):
    with pytest.raises(gate.AdmissionError, match="no longer held"):
        with retained_release_lock(lock_path) as capability:
            fcntl.flock(capability.fd, fcntl.LOCK_UN)
            capability.check()


def test_after_fork_capability_is_not_a_child_authority(lock_path, monkeypatch):
    with retained_release_lock(lock_path) as capability:
        original = os.getpid
        monkeypatch.setattr(os, "getpid", lambda: original() + 1)
        with pytest.raises(gate.AdmissionError, match="not live"):
            capability.check()
        monkeypatch.setattr(os, "getpid", original)
