"""Installable isolation-only cron wrapper; a stop is not queue-empty proof.

Registered argv is immutable and never passed to a shell. The wrapper takes its
activity lock BEFORE spawning an old wrapper that may itself take refresh locks.
There is deliberately no production enable switch, resume, signal or cleanup.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import time

from durable_handoff_cutover import durable_write, encoded, private_directory, read_private, require

HEX64 = re.compile(r"[0-9a-f]{64}\Z")
MAX_RUNS = 256
MAX_WAIT = 5.0


def sha(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def source_sha256():
    # The strict writer/reader is part of the control's identity, not an
    # unbound helper that an installed wrapper can silently replace.
    sources = {"handoff_host_cron.py": Path(__file__),
               "durable_handoff_cutover.py": Path(__file__).with_name("durable_handoff_cutover.py")}
    return sha({name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sources.items()})


def present(path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


@contextmanager
def locked(path, exclusive, seconds=0.2):
    require(type(seconds) in (int, float) and math.isfinite(seconds) and 0 <= seconds <= MAX_WAIT, "bad lock budget")
    fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600, "unsafe gate lock")
        deadline = time.monotonic() + seconds
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise TimeoutError("existing child has not exited; not killed") from error
                time.sleep(min(0.005, max(0, deadline - time.monotonic())))
        after = path.lstat()
        require((info.st_dev, info.st_ino) == (after.st_dev, after.st_ino), "gate lock replaced")
        yield fd
    finally:
        os.close(fd)


def _manifest(value):
    keys = {"schema", "scope", "run_id", "control_id", "source_sha256", "entrances", "commands"}
    require(isinstance(value, dict) and set(value) == keys and type(value["schema"]) is int
            and value["schema"] == 1 and value["scope"] == "isolation", "isolation manifest required")
    require(re.fullmatch(r"[0-9a-f]{32}", value["run_id"]) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value["control_id"])
            and value["source_sha256"] == source_sha256(), "gate source/identity differs")
    require(isinstance(value["entrances"], list) and 0 < len(value["entrances"]) <= 32
            and len(set(value["entrances"])) == len(value["entrances"])
            and all(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) for name in value["entrances"]), "bad entrance inventory")
    require(isinstance(value["commands"], dict) and 0 < len(value["commands"]) <= 32, "registered commands required")
    for name, argv in value["commands"].items():
        require(re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) and isinstance(argv, list) and 0 < len(argv) <= 32
                and all(isinstance(arg, str) and 0 < len(arg) <= 4096 and "\x00" not in arg for arg in argv)
                and Path(argv[0]).is_absolute(), "fixed executable/argv required")
    return value


def provision(root, manifest):
    root, manifest = Path(root), _manifest(manifest)
    require(root.is_absolute() and not present(root), "new absolute isolation gate required")
    private_directory(root, create=True)
    for name in ("runs", "finishes"):
        private_directory(root / name, create=True)
    for name in ("metadata.lock", "activity.lock", "controller.lock"):
        fd = os.open(root / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    durable_write(root / "manifest.json", manifest)
    durable_write(root / "state.json", {"schema": 1, "generation": 0, "state": "open"})


class HostCronGate:
    def __init__(self, root):
        self.root = Path(root)
        private_directory(self.root)
        self.manifest = _manifest(read_private(self.root / "manifest.json"))
        info = self.root.lstat()
        self.directory_identity = {"device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid}
        self.binding_sha256 = sha({"manifest": self.manifest, "directory": self.directory_identity})

    def registration(self):
        self._identity()
        return {"binding_sha256": self.binding_sha256, "source_sha256": source_sha256(),
                "entrances": self.manifest["entrances"]}

    def _identity(self):
        private_directory(self.root)
        info = self.root.lstat()
        require({"device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid} == self.directory_identity, "gate root replaced")
        require(_manifest(read_private(self.root / "manifest.json")) == self.manifest, "gate manifest changed")

    def _state(self):
        value = read_private(self.root / "state.json")
        require(set(value) == {"schema", "generation", "state"} and type(value["schema"]) is int
                and value["schema"] == 1 and type(value["generation"]) is int
                and (value["generation"], value["state"]) in ((0, "open"), (1, "closed")), "invalid gate state")
        return value

    def _inventory(self):
        originals, finishes = {}, {}
        for folder, rows in (("runs", originals), ("finishes", finishes)):
            private_directory(self.root / folder)
            with os.scandir(self.root / folder) as entries:
                for entry in entries:
                    require(len(rows) < MAX_RUNS and re.fullmatch(r"[0-9a-f]{32}\.json", entry.name), "run capacity/partial evidence")
                    row = read_private(Path(entry.path))
                    require(row["run_id"] == Path(entry.name).stem and row["binding_sha256"] == self.binding_sha256, "run identity changed")
                    rows[row["run_id"]] = row
        require(set(finishes) <= set(originals), "finish without original")
        for row in originals.values():
            require(row.get("command") in self.manifest["commands"]
                    and row.get("argv_sha256") == sha(self.manifest["commands"][row["command"]])
                    and type(row.get("parent_pid")) is int and row["parent_pid"] > 0, "invalid original command")
        for key, value in finishes.items():
            require(value.get("original_sha256") == sha(originals[key]) and value.get("outcome") == "exited"
                    and type(value.get("returncode")) is int, "invalid child finish")
        return originals, finishes

    def _has_orphan(self, originals, finishes):
        for key in set(originals) - set(finishes):
            fd = os.open(self.root / "runs" / (key + ".json"), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                return True
            finally:
                os.close(fd)
        return False

    @contextmanager
    def activity(self, command):
        require(command in self.manifest["commands"], "unregistered command")
        with locked(self.root / "activity.lock", False) as activity_fd:
            with locked(self.root / "metadata.lock", True):
                self._identity()
                require(self._state()["state"] == "open" and not present(self.root / "intent.json")
                        and not present(self.root / "intent.json.pending"), "new child admission stopped")
                originals, finishes = self._inventory()
                require(len(originals) < MAX_RUNS and not self._has_orphan(originals, finishes), "prior child outcome unresolved")
                run = {"run_id": secrets.token_hex(16), "binding_sha256": self.binding_sha256,
                       "command": command, "argv_sha256": sha(self.manifest["commands"][command]), "parent_pid": os.getpid()}
                durable_write(self.root / "runs" / (run["run_id"] + ".json"), run)
                owner_fd = os.open(self.root / "runs" / (run["run_id"] + ".json"), os.O_RDONLY)
                try:
                    fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BaseException:
                    os.close(owner_fd)
                    raise
            try:
                result = {"returncode": None, "pass_fds": (activity_fd, owner_fd)}
                yield result
                require(type(result["returncode"]) is int, "child exit was not observed")
                with locked(self.root / "metadata.lock", True):
                    finish = {"run_id": run["run_id"], "binding_sha256": self.binding_sha256,
                              "original_sha256": sha(run), "outcome": "exited", "returncode": result["returncode"]}
                    durable_write(self.root / "finishes" / (run["run_id"] + ".json"), finish)
            finally:
                os.close(owner_fd)

    def run_registered(self, command, *, input_data=None, timeout=5.0):
        require(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= MAX_WAIT, "bad child timeout")
        with self.activity(command) as run:
            child = subprocess.Popen(self.manifest["commands"][command], shell=False, pass_fds=run["pass_fds"],
                                     stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # Timeout deliberately does not kill or retry the child. Its inherited
            # activity FD remains held; missing finish also prevents confirmation.
            child.communicate(input=input_data, timeout=timeout)
            run["returncode"] = child.returncode
        return {"returncode": child.returncode}

    def _request(self, operation_id, generation, reason):
        require(isinstance(operation_id, str) and HEX64.fullmatch(operation_id) and type(generation) is int
                and generation == 0 and isinstance(reason, str) and 0 < len(reason) <= 256, "bad exact stop request")
        return {"schema": 1, "control_id": self.manifest["control_id"], "operation_id": operation_id,
                **self.registration(), "previous_generation": generation, "generation": generation + 1, "reason": reason}

    def stop(self, operation_id, generation, reason, *, wait_seconds=0):
        with locked(self.root / "controller.lock", True):
            with locked(self.root / "metadata.lock", True):
                request = self._request(operation_id, generation, reason)
                require(not present(self.root / "intent.json") and not present(self.root / "intent.json.pending"), "stop attempted; query only")
                require(self._state() == {"schema": 1, "generation": 0, "state": "open"}, "stop generation changed")
                durable_write(self.root / "intent.json", request)
                durable_write(self.root / "state.json", {"schema": 1, "generation": 1, "state": "closed"})
            return self._confirm(request, wait_seconds)

    def _confirm(self, request, wait_seconds):
        with locked(self.root / "activity.lock", True, wait_seconds):
            with locked(self.root / "metadata.lock", True):
                self._identity()
                require(self._state() == {"schema": 1, "generation": 1, "state": "closed"}, "stop state not closed")
                originals, finishes = self._inventory()
                require(set(originals) == set(finishes), "child crash/unobserved exit remains unresolved")
                for folder, values in (("runs", originals), ("finishes", finishes)):
                    for key, value in values.items():
                        durable_write(self.root / folder / (key + ".json"), value)
                receipt = {**request, "state": "closed", "durable": True, "inflight": 0, "unknown": 0}
                existing = self.root / "receipt.json"
                if present(existing):
                    require(read_private(existing) == receipt, "conflicting existing stop receipt")
                durable_write(existing, receipt)
                return receipt

    def query(self, operation_id, *, wait_seconds=0):
        with locked(self.root / "controller.lock", True):
            with locked(self.root / "metadata.lock", True):
                path = self.root / "intent.json"
                pending = path.with_name(path.name + ".pending")
                if not present(path) and not present(pending):
                    return None
                request = read_private(pending if present(pending) else path)
                require(request == self._request(operation_id, request["previous_generation"], request["reason"]), "stop query identity differs")
                state = self._state()
                require(state in ({"schema": 1, "generation": 0, "state": "open"},
                                  {"schema": 1, "generation": 1, "state": "closed"}), "unexpected query state")
                durable_write(path, request)
                durable_write(self.root / "state.json", {"schema": 1, "generation": 1, "state": "closed"})
            return self._confirm(request, wait_seconds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--isolation", action="store_true", required=True)
    args = parser.parse_args()
    # No provision/stop/live mode exposed: install only against a separately
    # authorized private isolation manifest containing the exact child argv.
    result = HostCronGate(args.root).run_registered(args.command)
    print(json.dumps({"returncode": result["returncode"], "scope": "isolation", "switch_authorized": False}))
    return result["returncode"]


if __name__ == "__main__":
    raise SystemExit(main())
