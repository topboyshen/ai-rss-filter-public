"""Retain the same actual release flock across migration and activation.

Only a live, process-local capability can lend a duplicate description. Neither
a serialized identity nor an unlocked descriptor can replace the held lock.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path

from services import admission_fence as gate


def _identity(path, fd):
    from services.production_migration import file_identity
    actual = file_identity(path)
    info = os.fstat(fd)
    gate._require((info.st_dev, info.st_ino, info.st_uid)
                  == (actual["device"], actual["inode"], actual["uid"]), "retained release inode changed")
    probe = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(probe, fcntl.LOCK_UN)
            raise gate.AdmissionError("retained release lock is no longer held")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise gate.AdmissionError("retained release descriptor has another owner") from error
    finally:
        os.close(probe)
    return actual


class RetainedReleaseLock:
    def __init__(self, path, fd):
        self.path, self.fd, self.pid = Path(path), fd, os.getpid()
        self.identity = _identity(self.path, self.fd)
        self.live = True

    def check(self):
        gate._require(self.live and self.pid == os.getpid(), "retained release capability is not live")
        gate._require(_identity(self.path, self.fd) == self.identity, "retained release binding changed")

    def duplicate(self, expected):
        self.check()
        gate._require(expected == self.identity, "migration uses a different release registry lock")
        fd = fcntl.fcntl(self.fd, fcntl.F_DUPFD_CLOEXEC, 10)
        try:
            gate._require(10 <= fd <= 199, "inherited release descriptor outside fixed range")
            _identity(self.path, fd)
        except BaseException:
            os.close(fd)
            raise
        return fd


def acquire_for_phase(expected, capability=None):
    if capability is None:
        return gate._lock(Path(expected["path"]), True, 0.1)
    gate._require(type(capability) is RetainedReleaseLock, "actual retained release capability required")
    return capability.duplicate(expected)


@contextmanager
def retained_release_lock(path):
    from services.production_migration import file_identity
    path = Path(path)
    before = file_identity(path)
    fd = gate._lock(path, True, 0.1)
    capability = None
    try:
        capability = RetainedReleaseLock(path, fd)
        gate._require(capability.identity == before, "release identity changed during acquisition")
        yield capability
        capability.check()
    finally:
        if capability is not None:
            capability.live = False
        os.close(fd)
