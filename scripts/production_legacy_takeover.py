"""One-attempt legacy stop under a registered root-cron writer contract.

This is a cooperative-writer fence, not an OS-wide CAS against arbitrary root.
It sends TERM only, never KILL/recreate/resume, and never claims queue drainage,
historical raw preservation, routing completion, or migration authorization.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import private_release_contract as private
from scripts import sender_namespace_http as bounded
from services import admission_fence as gate

ROOT_UID = 0
ROLES = ("sender", "gateway", "api", "worker")
MAX_CRON = 256 * 1024
MAX_QUERIES = 32
MAX_PROCESSES = 4096
ARCHIVE_KIND = "retained_container_config_archive"


def _archive_route(route):
    return isinstance(route.get("original"), dict) and route["original"].get("kind") == ARCHIVE_KIND


def source_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _record(path, *, executable=False):
    path = private.safe_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        limit = 128 * 1024**2 if executable else MAX_CRON
        mode = stat.S_IMODE(before.st_mode)
        gate._require(stat.S_ISREG(before.st_mode) and before.st_uid == ROOT_UID and before.st_nlink == 1
                      and 0 < before.st_size <= limit and not mode & 0o022
                      and (bool(mode & 0o111) if executable else mode in {0o400, 0o600}), "unsafe legacy input file")
        digest, chunks, count = hashlib.sha256(), [], 0
        while True:
            value = os.read(fd, 65536)
            if not value:
                break
            count += len(value)
            gate._require(count <= limit, "legacy input file too large")
            digest.update(value)
            if not executable:
                chunks.append(value)
        after = os.fstat(fd)
        gate._require(all(getattr(before, key) == getattr(after, key) for key in
                      ("st_dev", "st_ino", "st_uid", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns"))
                      and path.lstat().st_ino == after.st_ino and count == before.st_size, "legacy input changed")
        return {"path": str(path), "sha256": digest.hexdigest(), "device": before.st_dev,
                "inode": before.st_ino, "uid": before.st_uid, "mode": mode, "size": before.st_size,
                "mtime_ns": before.st_mtime_ns, "ctime_ns": before.st_ctime_ns}, b"".join(chunks)
    finally:
        os.close(fd)


def _bound(record, *, executable=False):
    gate._require(isinstance(record, dict) and set(record) == {"path", "sha256", "device", "inode", "uid", "mode",
                  "size", "mtime_ns", "ctime_ns"},
                  "exact legacy file identity required")
    gate._require(all(type(record[key]) is int and record[key] >= 0 for key in
                  ("device", "inode", "uid", "mode", "size", "mtime_ns", "ctime_ns")), "integer legacy file identity required")
    actual, raw = _record(record["path"], executable=executable)
    gate._require(actual == record, "legacy bound file changed")
    return raw


def _metadata(record):
    """Hash once on load; repeated polls pin the already-hashed file metadata."""
    path = private.safe_path(record["path"])
    info = path.lstat()
    actual = {"device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode),
              "size": info.st_size, "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}
    gate._require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                  and all(record[key] == value for key, value in actual.items()), "hashed legacy input metadata changed")


def _lock_identity(path):
    path = private.safe_path(path)
    info = path.lstat()
    gate._require(stat.S_ISREG(info.st_mode) and info.st_uid == ROOT_UID and info.st_nlink == 1
                  and stat.S_IMODE(info.st_mode) == 0o600, "unsafe root cron writer lock")
    return {"path": str(path), "device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid, "mode": 0o600}


def writer_script(lock, binary, flock):
    """Exact installed wrapper used by all registered external cron writers."""
    return ("#!/bin/sh\nset -eu\numask 077\nexec 9<>" + shlex.quote(lock) + "\n"
            + shlex.quote(flock) + " -n 9\nexec " + shlex.quote(binary) + " -u root \"$@\"\n").encode()


def _writer_contract(plan, *, metadata_only=False):
    cron = plan["cron"]
    gate._require(_lock_identity(cron["lock"]["path"]) == cron["lock"], "root cron lock changed")
    for key in ("binary", "flock", "writer"):
        _metadata(cron[key]) if metadata_only else _bound(cron[key], executable=True)
    fd = os.open(cron["writer"]["path"], os.O_RDONLY | os.O_NOFOLLOW)
    try:
        wrapper = os.read(fd, 4097)
        gate._require(len(wrapper) <= 4096 and os.fstat(fd).st_ino == cron["writer"]["inode"],
                      "installed cron writer changed or oversized")
    finally:
        os.close(fd)
    gate._require(wrapper == writer_script(cron["lock"]["path"], cron["binary"]["path"], cron["flock"]["path"]),
                  "installed cron writer is not the reviewed locking protocol")
    inventory = gate._parsed(_bound(cron["writer_inventory"]).decode())
    expected = {"schema": 1, "scope": "registered-root-cron-writers", "host_boot_id": plan["host_boot_id"],
        "lock": cron["lock"], "writer": cron["writer"], "adapter_source_sha256": plan["source_sha256"],
        "registered_writers": [cron["writer"]["path"], str(Path(__file__).resolve())],
        "external_root_mutation": "forbidden-during-reviewed-window", "expires_at": plan["expires_at"]}
    gate._require(isinstance(inventory, dict) and set(inventory) == {*expected, "reviewed_by"}
                  and all(inventory[key] == value for key, value in expected.items())
                  and isinstance(inventory["reviewed_by"], str) and 0 < len(inventory["reviewed_by"]) <= 128,
                  "installed registered-writer inventory is absent or differs")


def _disabled_cron(plan, before):
    gate._require(0 < len(before) <= MAX_CRON and before.endswith(b"\n") and b"\0" not in before,
                  "bounded complete root cron original required")
    lines, selected = before.splitlines(keepends=True), plan["cron"]["entries"]
    gate._require(isinstance(selected, list) and 1 <= len(selected) <= 64, "reviewed cron entries required")
    seen = set()
    for entry in selected:
        gate._require(isinstance(entry, dict) and set(entry) == {"line", "sha256", "executable"}
                      and type(entry["line"]) is int and 0 <= entry["line"] < len(lines)
                      and entry["line"] not in seen, "exact cron line identity required")
        seen.add(entry["line"])
        raw = lines[entry["line"]]
        _bound(entry["executable"], executable=True)
        words = shlex.split(raw.decode(), comments=False)
        offset = 1 if words and words[0].startswith("@") else 5
        gate._require(len(words) > offset and not raw.lstrip().startswith(b"#")
                      and words[offset] == entry["executable"]["path"]
                      and hashlib.sha256(raw).hexdigest() == entry["sha256"], "cron original command differs")
        lines[entry["line"]] = ("# controlled-legacy-stop " + plan["operation_id"] + " " + entry["sha256"] + "\n").encode()
    after = b"".join(lines)
    gate._require(hashlib.sha256(after).hexdigest() == plan["cron"]["after_sha256"], "reviewed stopped cron differs")
    return after


def container_identity(row):
    config, state, host = row["Config"], row["State"], row["HostConfig"]
    return {"id": row["Id"], "image_id": row["Image"], "revision": config.get("Labels", {}).get("org.opencontainers.image.revision"),
        "service": config.get("Labels", {}).get("com.docker.compose.service"), "started_at": state["StartedAt"],
        "restart_count": row["RestartCount"], "user": config.get("User", ""),
        "command_sha256": gate._hash({"entrypoint": config.get("Entrypoint"), "command": config.get("Cmd")}),
        "environment_sha256": gate._hash(config.get("Env", [])), "mounts_sha256": gate._hash(row["Mounts"]),
        "network_sha256": gate._hash({"mode": host.get("NetworkMode"), "ports": host.get("PortBindings"),
                                       "networks": sorted(row["NetworkSettings"]["Networks"])}),
        "resources_sha256": gate._hash({key: host.get(key) for key in ("Memory", "MemorySwap", "PidsLimit", "NanoCpus")})}


def _process(pid):
    root = Path("/proc") / str(pid)
    with (root / "stat").open("rb") as stream:
        raw = stream.read(8193)
    gate._require(len(raw) <= 8192, "process identity exceeds bound")
    fields = raw[raw.rfind(b")") + 2:].split()
    with (root / "cmdline").open("rb") as stream:
        command = stream.read(65537)
    gate._require(len(fields) > 20 and len(command) <= 65536, "bounded process identity required")
    return {"pid": pid, "parent": int(fields[1]), "start_ticks": int(fields[19]),
            "uid": root.stat().st_uid, "command_sha256": hashlib.sha256(command).hexdigest()}


class NativeTransport:
    def _run(self, argv):
        return bounded._capture(argv, deadline=time.monotonic() + 3, max_bytes=MAX_CRON)

    def host_boot(self):
        gate._require(sys.platform == "linux" and os.geteuid() == ROOT_UID, "native Linux root required")
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    def inspect(self, binary, identifier):
        rows = json.loads(self._run([binary, "inspect", identifier]))
        gate._require(isinstance(rows, list) and len(rows) == 1 and rows[0]["Id"] == identifier, "exact old container missing")
        return rows[0]

    def cron_read(self, binary):
        value = self._run([binary, "-u", "root", "-l"])
        gate._require(len(value) <= MAX_CRON, "root crontab exceeds bound")
        return value

    def cron_install(self, binary, path):
        self._run([binary, "-u", "root", str(path)])

    def term(self, binary, identifier):
        value = self._run([binary, "kill", "--signal", "TERM", identifier])
        gate._require(value.decode().strip() == identifier, "container TERM acknowledgment unknown")

    def gateway_config(self, binary, identifier, target):
        value = self._run([binary, "exec", identifier, "sha256sum", "--", target]).decode().split()
        gate._require(len(value) == 2 and value[1] == target and gate.HEX64.fullmatch(value[0]), "old gateway config hash unavailable")
        return value[0]

    def gateway_configuration(self, binary, route):
        from scripts import retained_gateway_configuration as configuration
        deadline = time.monotonic() + 3
        identifier = route["container_id"]
        def archive(actual_id, path, consumer):
            gate._require(actual_id == identifier, "fixed retained gateway archive identity required")
            return configuration.read_native_archive(actual_id, path, consumer, deadline=deadline)
        def inspect(actual_id):
            gate._require(actual_id == identifier and time.monotonic() < deadline,
                          "fixed retained gateway inspection deadline or identity differs")
            raw = bounded._capture([binary, "inspect", actual_id], deadline=deadline, max_bytes=MAX_CRON)
            rows = json.loads(raw)
            gate._require(isinstance(rows, list) and len(rows) == 1 and rows[0]["Id"] == identifier,
                          "exact retained gateway container missing")
            return rows[0]
        result = configuration.verify(route["original"], inspect(identifier), inspect=inspect, archive=archive)
        gate._require(time.monotonic() < deadline, "legacy gateway configuration deadline exceeded")
        return result

    def cron_children(self, daemon):
        gate._require(_process(daemon["pid"]) == daemon, "root cron daemon identity changed")
        processes = {}
        deadline = time.monotonic() + 3
        for path in Path("/proc").iterdir():
            gate._require(time.monotonic() < deadline, "process inventory deadline exceeded")
            if not path.name.isdigit():
                continue
            gate._require(len(processes) < MAX_PROCESSES, "process inventory exceeds bound")
            try:
                processes[int(path.name)] = _process(int(path.name))
            except FileNotFoundError:
                continue
        children = {daemon["pid"]}
        for _ in range(MAX_PROCESSES):
            gate._require(time.monotonic() < deadline, "process ancestry deadline exceeded")
            found = {pid for pid, row in processes.items() if row["parent"] in children} - children
            if not found:
                break
            children.update(found)
        return [processes[pid] for pid in sorted(children - {daemon["pid"]})]


def load_plan(path):
    _identity, raw = _record(path)
    plan = gate._parsed(raw.decode())
    keys = {"schema", "operation_id", "source_sha256", "host_boot_id", "approved_by", "not_before", "expires_at",
            "docker", "cron", "containers", "gateway", "wait_seconds", "max_polls"}
    gate._require(isinstance(plan, dict) and set(plan) == keys and type(plan["schema"]) is int and plan["schema"] == 1
                  and gate.HEX64.fullmatch(str(plan["operation_id"])) and plan["source_sha256"] == source_sha256(),
                  "exact legacy takeover plan required")
    gate._require(isinstance(plan["approved_by"], str) and 0 < len(plan["approved_by"]) <= 128
                  and isinstance(plan["host_boot_id"], str) and 0 < len(plan["host_boot_id"]) <= 128,
                  "reviewed host identity required")
    gate._require(all(type(plan[key]) is int for key in ("not_before", "expires_at", "wait_seconds", "max_polls"))
                  and 0 < plan["expires_at"] - plan["not_before"] <= 900
                  and 1 <= plan["wait_seconds"] <= 30 and 1 <= plan["max_polls"] <= 32, "finite legacy stop budget required")
    cron = plan["cron"]
    gate._require(isinstance(cron, dict) and set(cron) == {"lock", "binary", "flock", "writer", "writer_inventory",
                  "before", "after_sha256", "entries", "daemon"}, "exact root cron contract required")
    gate._require(isinstance(cron["daemon"], dict) and set(cron["daemon"]) ==
                  {"pid", "parent", "start_ticks", "uid", "command_sha256"}
                  and all(type(cron["daemon"][key]) is int and cron["daemon"][key] >= 0
                          for key in ("pid", "parent", "start_ticks", "uid")) and cron["daemon"]["pid"] > 1
                  and gate.HEX64.fullmatch(cron["daemon"]["command_sha256"])
                  and cron["daemon"]["uid"] == ROOT_UID, "root cron process identity required")
    gate._require(isinstance(plan["containers"], dict) and set(plan["containers"]) == set(ROLES), "all reviewed old roles required")
    identities = list(plan["containers"].values())
    gate._require(len({value["id"] for value in identities}) == 4
                  and all(gate.HEX64.fullmatch(value["id"]) and re.fullmatch(r"sha256:[0-9a-f]{64}", value["image_id"])
                          for value in identities), "unique immutable old containers required")
    gate._require(all(type(value["restart_count"]) is int and value["restart_count"] >= 0 for value in identities),
                  "integer old restart identities required")
    route = plan["gateway"]
    route_keys = {"original", "container_id", "config_target", "candidate_service", "candidate_image_id", "private_contract_sha256"}
    archive_route = _archive_route(route)
    gate._require(set(route) == (route_keys | {"gateway_plan_sha256"} if archive_route else route_keys)
                  and route["container_id"] == plan["containers"]["gateway"]["id"]
                  and (route["config_target"] in {"/etc/nginx/nginx.conf", "/etc/nginx/conf.d/default.conf"}
                       if archive_route else route["config_target"] == "/etc/nginx/conf.d/default.conf")
                  and re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", route["candidate_service"])
                  and re.fullmatch(r"sha256:[0-9a-f]{64}", route["candidate_image_id"])
                  and gate.HEX64.fullmatch(route["private_contract_sha256"]), "exact future route plan required")
    _bound(plan["docker"], executable=True)
    _writer_contract(plan)
    before = _bound(cron["before"])
    if archive_route:
        from scripts import retained_gateway_configuration as configuration
        verified = configuration.validate(route["original"])
        gate._require(gate.HEX64.fullmatch(str(route["gateway_plan_sha256"]))
                      and verified["container_identity"]["id"] == route["container_id"]
                      and route["config_target"] == configuration.legacy_config_target(route["original"]),
                      "archive route must bind exact old gateway and frozen gateway plan")
    else:
        _bound(route["original"])
    return plan, before, _disabled_cron(plan, before)


def _raw_once(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    gate._sync_directory(path.parent)


class LegacyTakeover:
    def __init__(self, plan, before, after, root, transport):
        gate._require(type(transport) is NativeTransport, "native legacy transport required")
        self.plan, self.before, self.after, self.root, self.transport = plan, before, after, Path(root), transport
        self.fd, self.pid = None, os.getpid()

    def check(self, *, active=False):
        gate._require(self.fd is not None and self.pid == os.getpid(), "live legacy lock capability required")
        gate._require(self.transport.host_boot() == self.plan["host_boot_id"], "legacy host boot changed")
        _writer_contract(self.plan, metadata_only=True)
        _metadata(self.plan["docker"])
        from services.production_release_lock import _identity
        _identity(self.plan["cron"]["lock"]["path"], self.fd)
        if active:
            gate._require(self.plan["not_before"] <= time.time() < self.plan["expires_at"], "legacy stop window expired")

    def _containers(self):
        rows = {}
        for role, expected in self.plan["containers"].items():
            row = self.transport.inspect(self.plan["docker"]["path"], expected["id"])
            gate._require(container_identity(row) == expected, "old container identity changed")
            state = row["State"]
            gate._require(not state.get("Paused") and not state.get("Restarting") and not state.get("Dead")
                          and not state.get("OOMKilled"), "old container state uncertain")
            rows[role] = {"running": state["Running"], "status": state["Status"], "exit_code": state.get("ExitCode"),
                          "finished_at": state.get("FinishedAt"), "identity": expected}
        return rows

    def _wait(self, inspect, ready):
        deadline = time.monotonic() + self.plan["wait_seconds"]
        for _ in range(self.plan["max_polls"]):
            self.check(active=True)
            value = inspect()
            self.check(active=True)
            gate._require(time.monotonic() <= deadline, "legacy observation exceeded stop deadline")
            if ready(value):
                return value
            if time.monotonic() >= deadline:
                break
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        raise gate.AdmissionUncertain("legacy natural stop or cron child completion unconfirmed")

    def _cron_stop(self):
        binary = self.plan["cron"]["binary"]["path"]
        gate._require(self.transport.cron_read(binary) == self.before, "root crontab changed before stop")
        _raw_once(self.root / "cron.before", self.before)
        _raw_once(self.root / "cron.after", self.after)
        gate._write(self.root / "cron.intent.json", {"operation_id": self.plan["operation_id"],
            "before_sha256": hashlib.sha256(self.before).hexdigest(), "after_sha256": hashlib.sha256(self.after).hexdigest()})
        self.check(active=True)
        gate._require(self.transport.cron_read(binary) == self.before, "root cron concurrent writer before install")
        self.transport.cron_install(binary, self.root / "cron.after")
        gate._require(self.transport.cron_read(binary) == self.after, "root cron result unknown or concurrent writer")
        gate._write(self.root / "cron.confirmed.json", gate._read(self.root / "cron.intent.json"))
        self._wait(lambda: self.transport.cron_children(self.plan["cron"]["daemon"]), lambda value: not value)

    def _gateway_original(self, *, retain=False):
        route = self.plan["gateway"]
        if _archive_route(route):
            self.transport.gateway_configuration(self.plan["docker"]["path"], route)
            if retain:
                _raw_once(self.root / "gateway.configuration.original.json", gate._encoded(route["original"]))
        elif retain:
            gate._require(self.transport.gateway_config(self.plan["docker"]["path"], route["container_id"], route["config_target"])
                          == route["original"]["sha256"], "actual old gateway config differs from retained original")
            _raw_once(self.root / "gateway.original", _bound(route["original"]))

    def stop(self):
        self.check(active=True)
        gate._require(not list(self.root.iterdir()), "legacy stop already attempted; query original operation only")
        rows = self._containers()
        gate._require(all(row["running"] and row["status"] == "running" for row in rows.values()), "reviewed old runtime is not running")
        self._gateway_original(retain=True)
        _raw_once(self.root / "writer-inventory.original", _bound(self.plan["cron"]["writer_inventory"]))
        gate._write(self.root / "plan.json", self.plan)
        gate._write(self.root / "intent.json", {"schema": 1, "operation_id": self.plan["operation_id"],
            "plan_sha256": gate._hash(self.plan), "observed_containers": rows, "gateway": self.plan["gateway"]})
        try:
            self._cron_stop()
            for role in ROLES:
                self.check(active=True)
                rows = self._containers()
                gate._require(rows[role]["running"], "old process ended before its original TERM")
                identifier = self.plan["containers"][role]["id"]
                gate._write(self.root / (role + ".term.intent.json"), {"operation_id": self.plan["operation_id"],
                            "identity": self.plan["containers"][role], "signal": "TERM"})
                self.transport.term(self.plan["docker"]["path"], identifier)
                self._wait(self._containers, lambda current: not current[role]["running"] and current[role]["status"] == "exited")
            return self.query()
        except BaseException as error:
            try:
                if not gate._present(self.root / "STOP.json"):
                    gate._write(self.root / "STOP.json", {"operation_id": self.plan["operation_id"],
                        "error_type": type(error).__name__, "message_sha256": hashlib.sha256(str(error).encode()).hexdigest(),
                        "next_action": "query_original_only", "automatic_kill_or_resume": False})
            except BaseException as secondary:
                error.add_note("Legacy STOP preservation failed: " + type(secondary).__name__)
            raise

    def query(self):
        self.check()
        intent = gate._read(self.root / "intent.json")
        gate._require(intent["operation_id"] == self.plan["operation_id"] and intent["plan_sha256"] == gate._hash(self.plan),
                      "query original legacy operation only")
        available = next((self.root / f"query-{i:02d}.json" for i in range(MAX_QUERIES)
                          if not gate._present(self.root / f"query-{i:02d}.json")
                          and not gate._present(self.root / f"query-{i:02d}.json.pending")), None)
        gate._require(available is not None, "legacy query evidence budget exhausted")
        gate._write(available, {"operation_id": self.plan["operation_id"], "reserved": True})
        cron = self.transport.cron_read(self.plan["cron"]["binary"]["path"])
        gate._require(cron in (self.before, self.after), "root cron changed outside original attempt")
        rows, children = self._containers(), self.transport.cron_children(self.plan["cron"]["daemon"])
        self._gateway_original()
        stopped = cron == self.after and not children and all(not row["running"] and row["status"] == "exited" for row in rows.values())
        attempted = self._term_intents()
        value = {"schema": 1, "operation_id": self.plan["operation_id"], "plan_sha256": gate._hash(self.plan),
            "status": "registered_legacy_runtime_stopped" if stopped and attempted else "legacy_takeover_uncertain",
            "containers": rows, "cron_after": cron == self.after, "cron_children": children,
            "switch_authorized": False, "inventory_drained": False, "gateway_switched": False,
            "historical_async_raw": "unknown_before_T0_no_replay", "writer_scope": "cooperative_registered_writers",
            "unregistered_or_detached_host_producers_proven": False, "mutation_replayed": False}
        gate._write(available.with_name(available.stem + ".result.json"), value)
        if stopped and attempted:
            gate._write(self.root / "stopped.json", value)
        return value

    def _term_intents(self):
        from services.controlled_startup import _read_complete
        attempted = True
        for role in ROLES:
            path = self.root / (role + ".term.intent.json")
            if not gate._present(path) and not gate._present(path.with_suffix(".json.pending")):
                attempted = False
                continue
            expected = {"operation_id": self.plan["operation_id"], "identity": self.plan["containers"][role], "signal": "TERM"}
            gate._require(_read_complete(path) == expected, "original TERM intent changed")
        return attempted

    def check_stopped(self):
        """Fresh local facts under the retained writer lock; never switch permission."""
        from services.controlled_startup import _read_complete
        self.check()
        original = _read_complete(self.root / "stopped.json")
        gate._require(original["operation_id"] == self.plan["operation_id"] and original["plan_sha256"] == gate._hash(self.plan)
                      and original["status"] == "registered_legacy_runtime_stopped" and self._term_intents(),
                      "original local stop is unconfirmed")
        rows = self._containers()
        self._gateway_original()
        gate._require(all(not row["running"] and row["status"] == "exited" for row in rows.values())
                      and not self.transport.cron_children(self.plan["cron"]["daemon"])
                      and self.transport.cron_read(self.plan["cron"]["binary"]["path"]) == self.after,
                      "registered old runtime or root cron is no longer stopped")
        return {"operation_id": self.plan["operation_id"], "plan_sha256": gate._hash(self.plan),
                "containers": rows, "cron_after_sha256": hashlib.sha256(self.after).hexdigest(),
                "writer_lock_held": True, "switch_authorized": False, "inventory_drained": False}


@contextmanager
def locked_takeover(plan_path, evidence_root):
    plan, before, after = load_plan(plan_path)
    root = Path(evidence_root)
    gate._directory(root)
    gate._require(root.is_absolute() and root.resolve() == root and root.stat().st_uid == ROOT_UID,
                  "private root-owned legacy evidence required")
    session = LegacyTakeover(plan, before, after, root, NativeTransport())
    session.fd = gate._lock(Path(plan["cron"]["lock"]["path"]), True, 0.1)
    try:
        session.check()
        yield session
        session.check()
    finally:
        fd, session.fd = session.fd, None
        os.close(fd)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stop", "query"))
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)
    args = parser.parse_args()
    with locked_takeover(args.plan, args.evidence_root) as session:
        result = session.stop() if args.action == "stop" else session.query()
    print(json.dumps({"operation_id": result["operation_id"], "status": result["status"], "switch_authorized": False}))
    return 0 if result["status"] == "registered_legacy_runtime_stopped" else 2


if __name__ == "__main__":
    raise SystemExit(main())
