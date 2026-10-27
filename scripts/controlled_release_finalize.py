"""Formal controlled release registration after actual activation, never deploy.

The live receiver capability owns the existing release lock. Original sender
operations are queried, never posted. A durable registry journal permits only
the same byte transitions after interruption; it cannot restart the workload.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time

if __package__ in (None, ""):
    sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from services import admission_fence as gate
from services import production_activation as runtime
from services import production_activation_control as activation
from services import production_container_lifecycle as lifecycle
from scripts import private_release_contract as private

# The existing host sender helpers use sibling script imports. Restrict this
# compatibility search to the reviewed source directory and restore the caller's
# search path; use the same bare driver class as the existing native harness.
_script_directory = str(Path(__file__).resolve().parent)
sys.path.insert(0, _script_directory)
try:
    import production_sender_driver as sender
finally:
    sys.path.remove(_script_directory)

ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 1024 * 1024
MAX_CHECK_SECONDS = 180
FIELDS = ("ref", "digest", "manifest", "health-services")
MARKER = "controlled-finalize.pending.json"
PLAN_KEYS = {"schema", "source_sha", "image_ref", "lifecycle_root", "lifecycle_operation_id",
             "sender_closed_operation_id", "sender_generation", "finalize_operation_id", "registry_root"}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _sources():
    names = ("scripts/controlled_release_finalize.py", "scripts/release.sh",
             "scripts/private_release_contract.py", "scripts/production_sender_driver.py",
             "scripts/production_admission_supervisor.py",
             "services/production_container_lifecycle.py", "services/production_activation_control.py")
    return {name: _sha((ROOT / name).read_bytes()) for name in names}


def _raw(path, *, missing=False, private_mode=False):
    if missing and not gate._present(path):
        return None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        modes = {0o600} if private_mode else {0o600, 0o644, 0o400}
        gate._require(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid()
                      and before.st_nlink == 1 and stat.S_IMODE(before.st_mode) in modes
                      and before.st_size <= MAX_FILE_BYTES, "unsafe or oversized finalize file")
        value = bytearray()
        while len(value) <= MAX_FILE_BYTES:
            chunk = os.read(fd, min(65536, MAX_FILE_BYTES + 1 - len(value)))
            if not chunk:
                break
            value.extend(chunk)
        after = os.fstat(fd)
        keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        gate._require(all(getattr(before, k) == getattr(after, k) for k in keys)
                      and path.stat(follow_symlinks=False).st_ino == before.st_ino
                      and len(value) == before.st_size, "finalize file changed while reading")
        return bytes(value)
    finally:
        os.close(fd)


def _new_raw(path, value):
    gate._require(len(value) <= MAX_FILE_BYTES, "finalize file budget exceeded")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    gate._sync_directory(path.parent)


def _confirmed(path, expected):
    gate._require(_raw(path, private_mode=True) == expected, "retained finalize bytes changed")
    gate._confirm_file(path)
    gate._sync_directory(path.parent)
    gate._require(_raw(path, private_mode=True) == expected, "finalize bytes changed during confirmation")


def _plan(plan):
    gate._require(isinstance(plan, dict) and set(plan) == PLAN_KEYS and type(plan["schema"]) is int
                  and plan["schema"] == 1, "exact finalize plan required")
    for name in ("lifecycle_operation_id", "sender_closed_operation_id", "finalize_operation_id"):
        gate._require(isinstance(plan[name], str) and gate.HEX64.fullmatch(plan[name]), "invalid finalize operation")
    gate._require(len({plan[k] for k in ("lifecycle_operation_id", "sender_closed_operation_id", "finalize_operation_id")}) == 3
                  and type(plan["sender_generation"]) is int and 0 <= plan["sender_generation"] < 2**63 - 2
                  and isinstance(plan["source_sha"], str) and re.fullmatch(r"[0-9a-f]{40,64}", plan["source_sha"]),
                  "invalid finalize source/CAS")
    ref = plan["image_ref"]
    gate._require(isinstance(ref, str) and 0 < len(ref) <= 256 and not re.search(r"[\s\x00-\x1f]", ref)
                  and ref != "latest" and not ref.endswith(":latest"), "immutable image reference required")
    return json.loads(json.dumps(plan))


def _contract_context(permit, plan, *, active):
    contract_path = Path(permit["config"]["private_contract_path"])
    contract, _manifest, digest = private.load(contract_path, ROOT, require_fresh=active)
    source = permit["source"]
    gate._require(contract["source_sha"] == source["revision"] == plan["source_sha"]
                  and contract["source_tree"] == source["tree"] and digest == source["private_contract_sha256"]
                  and contract["files"]["compose.json"] == source["manifest_sha256"],
                  "finalize source/private contract differs")
    target = [plan["image_ref"], source["image_id"], contract["files"]["compose.json"],
              " ".join(contract["review"]["health_services"])]
    path = Path(plan["registry_root"]) / "build-contracts" / (_sha(plan["image_ref"].encode()) + ".state")
    raw = _raw(path, private_mode=True)
    expected = [*target[:3], contract["example_config_sha256"], source["revision"] + ":clean",
                "event-mapping-store-v5-provider-attribution-v1", digest]
    gate._require(raw.decode().splitlines() == expected, "formal private build credential differs")
    return {"source": source, "private_contract_sha256": digest, "build_contract_sha256": _sha(raw),
            "target": target, "sources": _sources()}


def _shell_check(permit, plan, release_fd, target, old, *, health, recovery=False, evidence_root=None):
    registry = Path(plan["registry_root"])
    gate._require(Path(permit["locks"]["release"]["path"]) == registry / "release.lock",
                  "formal check release registry differs")
    expected, actual = permit["locks"]["release"], os.fstat(release_fd)
    gate._require((actual.st_dev, actual.st_ino) == (expected["device"], expected["inode"])
                  and registry.joinpath("release.lock").stat(follow_symlinks=False).st_ino == actual.st_ino,
                  "formal check release lock identity differs")
    activation._owned_lock(str(registry / "release.lock"), release_fd)
    env = os.environ.copy()
    keep = {key: env[key] for key in ("RELEASE_TEST_ATTESTATION_DIR", "RELEASE_HEALTH_TIMEOUT_SECONDS",
                                     "RELEASE_HEALTH_POLL_SECONDS") if key in env}
    seconds, poll = keep.get("RELEASE_HEALTH_TIMEOUT_SECONDS", "180"), keep.get("RELEASE_HEALTH_POLL_SECONDS", "2")
    gate._require(re.fullmatch(r"(?:0|[1-9][0-9]{0,2})", seconds) and int(seconds) <= MAX_CHECK_SECONDS
                  and re.fullmatch(r"(?:0|[1-9][0-9]?)(?:\.[0-9]{1,3})?", poll) and 0 < float(poll) <= 2,
                  "controlled health bounds may not exceed original limits")
    for key in tuple(env):
        if key.startswith("RELEASE_"):
            env.pop(key)
    import fcntl
    inherited = fcntl.fcntl(release_fd, fcntl.F_DUPFD, 10)
    try:
        gate._require(inherited <= 199, "no bounded inherited release descriptor")
        env.update(keep)
        env.update(RELEASE_LOCK_FD=str(inherited), RELEASE_PRIVATE_CONTRACT=permit["config"]["private_contract_path"],
                   RELEASE_REGISTRY_DIR=str(registry), RELEASE_AUTO_ROLLBACK="0",
                   RELEASE_REQUIRE_BUILD_CONTRACT="1", RELEASE_ALLOW_DIRTY_TREE="0",
                   RELEASE_CAPTURE_HEALTH_DIAGNOSTICS="0")
        args = ["bash", str(ROOT / "scripts/release.sh"), "controlled-check", target[0], target[2],
                "recovery" if recovery else "fresh", *old[:3], "health" if health else "preflight"]
        return _check_process(args, env, inherited, evidence_root=evidence_root)
    finally:
        os.close(inherited)


def check_contract(permit, finalize_plan, release_fd, *, health=False):
    """Formal pre-write check under the real outer lock, never a readiness flag."""
    plan = _plan(finalize_plan)
    registry = Path(plan["registry_root"])
    gate._directory(registry)
    for suffix in ("", ".pending"):
        gate._require(not gate._present(registry / (MARKER + suffix)),
                      "original controlled finalize must be queried before a new preflight")
    for name in ("current.override", "current.override-sha256"):
        gate._require(not gate._present(registry / name), "forced release override requires separate review")
    context = _contract_context(permit, plan, active=True)
    old, _files = _state(registry, "current", required=True)
    _state(registry, "previous")
    history = _raw(registry / "history.tsv", missing=True) or b""
    gate._require((not history or history.endswith(b"\n")) and len(history) + 512 <= MAX_FILE_BYTES,
                  "retained registry history capacity insufficient")
    result = _shell_check(permit, plan, release_fd, context["target"], old, health=health)
    gate._require(_contract_context(permit, plan, active=True) == context, "formal contract changed during check")
    return {"context": context, "check": result, "health_checked": health, "release_registered": False}


class _Finalizer:
    def __init__(self, receiver_session, sender_driver, plan, evidence_root):
        gate._require(type(receiver_session) is activation.ReceiverActivationSession
                      and type(sender_driver) is sender.SenderActivationDriver, "real activation and sender capabilities required")
        self.receiver, self.sender, self.plan = receiver_session, sender_driver, _plan(plan)
        self.root, self.registry = Path(evidence_root), Path(plan["registry_root"])
        self.paths = {"evidence": private.directory_identity(self.root),
                      "registry": private.directory_identity(self.registry)}
        gate._directory(self.root)
        gate._directory(self.registry)
        gate._require(self.root != self.registry and not self.registry.is_relative_to(self.root),
                      "finalize evidence must be a separate directory")
        self.contract_path = Path(receiver_session.permit["config"]["private_contract_path"])
        self.lock_fd = dict(receiver_session.shadow.fds)["release"]
        gate._require(Path(receiver_session.permit["locks"]["release"]["path"]) == self.registry / "release.lock",
                      "activation capability does not own this release registry")
        self.marker = self.registry / MARKER

    def paths_current(self):
        gate._require(self.paths == {"evidence": private.directory_identity(self.root),
                                    "registry": private.directory_identity(self.registry)}, "finalize directory identity changed")
        self.receiver._identity(active=False)

    def context(self, *, active):
        self.paths_current()
        self.receiver._identity(active=active)
        context = _contract_context(self.receiver.permit, self.plan, active=active)
        self.target = context["target"]
        return context

    def actual(self, *, active):
        expected = self.receiver.permit["sender"]
        gate._require(self.sender.plan["postgres_identity"] == expected["expected_identity"]
                      and self.sender.plan["binding"]["manifest"] == expected["manifest"]
                      and self.plan["sender_closed_operation_id"] == expected["closed_operation_id"],
                      "migration and sender PG/manifest/closed operation differ")
        proof = self.receiver.verification_proof(active=active)
        value = lifecycle.verify_active(self.receiver, self.plan["lifecycle_root"],
                                        operation_id=self.plan["lifecycle_operation_id"])
        create = runtime.read_confirmed(Path(self.plan["lifecycle_root"]) / "create.intent.json")
        previous_images = {key: create["previous_containers"][key]["image"] for key in value["retained_previous_ids"]}
        original = runtime.read_confirmed(self.sender.control.root / "production_resume.intent.json")
        gate._require(original["closed_operation_id"] == self.plan["sender_closed_operation_id"]
                      and original["closed_generation"] == self.plan["sender_generation"]
                      and original["receiver_activation_proof"] == self.receiver.check_sender_resume(active=active),
                      "finalize sender/receiver original CAS differs")
        result = self.sender.query_production_resume(original["operation_id"], self.receiver)
        gate._require(result["operation_id"] == original["operation_id"]
                      and result["pg_receipt"]["state"] == "open", "original sender resume not confirmed")
        # Counters and pulse times may advance. Only immutable operation/owner
        # identity enters this comparison; each fresh capability still checks liveness.
        stable = {"receiver": proof, "lifecycle_operation_id": value["operation_id"],
                  "containers": value["containers"], "retained_previous_ids": value["retained_previous_ids"],
                  "retained_previous_images": previous_images,
                  "sender_operation_id": original["operation_id"], "sender_intent_sha256": gate._hash(original),
                  "sender_binding_sha256": self.sender.control.binding_sha256,
                  "postgres_identity": self.sender.plan["postgres_identity"]}
        return stable

    def check(self, old, *, recovery=False):
        return _shell_check(self.receiver.permit, self.plan, self.lock_fd, self.target, old,
                            health=True, recovery=recovery, evidence_root=self.root)


def _check_process(args, env, inherited, *, evidence_root=None):
    output, deadline, observed = bytearray(), time.monotonic() + MAX_CHECK_SECONDS, 0
    log, stream, process = None, None, None
    if evidence_root is not None:
        count = len(list(evidence_root.glob("check-*.log")))
        gate._require(count < 32, "finalize check evidence budget exhausted")
        log = evidence_root / f"check-{count:02d}.log"
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        stream = os.fdopen(fd, "wb")
    try:
        process = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   pass_fds=(inherited,), start_new_session=True)
        with selectors.DefaultSelector() as poll:
            poll.register(process.stdout, selectors.EVENT_READ)
            while poll.get_map():
                gate._require(time.monotonic() < deadline, "controlled release check deadline exceeded")
                for key, _ in poll.select(min(1, max(0, deadline - time.monotonic()))):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        poll.unregister(key.fileobj)
                    observed += len(data)
                    output.extend(data[:MAX_FILE_BYTES - len(output)])
                    gate._require(observed <= MAX_FILE_BYTES, "controlled release check output budget exceeded")
        code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        gate._require(code == 0, "formal controlled-check failed; no registry update")
        return {"exit_code": code, "output_sha256": _sha(output), "bytes": len(output)}
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        if process is not None:
            process.wait()
        if stream is not None:
            with stream:
                info = os.fstat(stream.fileno())
                current = log.stat(follow_symlinks=False)
                gate._require(info.st_nlink == 1 and (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino),
                              "controlled check output replaced")
                stream.write(output)
                stream.flush()
                os.fsync(stream.fileno())
            gate._sync_directory(log.parent)
            gate._write(log.with_suffix(".json"), {"exit_code": None if process is None else process.returncode,
                "output_sha256": _sha(output), "bytes": len(output), "observed_bytes": observed,
                "truncated": observed > len(output)})


def _state(registry, prefix, *, required=False):
    files = {field: _raw(registry / (prefix + "." + field), missing=True) for field in (*FIELDS, "state")}
    if not any(value is not None for value in files.values()):
        gate._require(not required, "complete previous current release is required")
        return None, files
    gate._require(all(files[k] is not None for k in FIELDS), "incomplete release mirrors")
    values = [files[k].decode().removesuffix("\n") for k in FIELDS]
    gate._require(all(v and "\n" not in v and "\r" not in v and "\t" not in v for v in values)
                  and re.fullmatch(r"sha256:[0-9a-f]{64}", values[1]) and gate.HEX64.fullmatch(values[2]),
                  "invalid recorded release tuple")
    canonical = ("\n".join(values) + "\n").encode()
    gate._require(files["state"] is None or files["state"] == canonical, "release bundle/mirrors differ")
    return values, files


def _snapshot(f):
    for name in ("current.override", "current.override-sha256"):
        gate._require(not gate._present(f.registry / name), "forced release override requires separate review")
    old, current = _state(f.registry, "current", required=True)
    _previous, previous = _state(f.registry, "previous")
    gate._require(old != f.target, "target already current; query original finalize only")
    files = {prefix + "." + k: v for prefix, values in (("previous", previous), ("current", current))
             for k, v in values.items()}
    files["history.tsv"] = _raw(f.registry / "history.tsv", missing=True)
    gate._require(files["history.tsv"] is None or not files["history.tsv"] or files["history.tsv"].endswith(b"\n"),
                  "incomplete release history tail")
    return old, files


def _targets(f, old, before, stamp):
    result = {}
    for prefix, values in (("previous", old), ("current", f.target)):
        result.update({prefix + "." + field: (value + "\n").encode() for field, value in zip(FIELDS, values)})
        result[prefix + ".state"] = ("\n".join(values) + "\n").encode()
    row = "\t".join([stamp, "controlled-finalize", *f.target[:2], "healthy", *f.target[2:]]) + "\n"
    result["history.tsv"] = (before["history.tsv"] or b"") + row.encode()
    gate._require(all(len(v) <= MAX_FILE_BYTES for v in result.values()), "retained registry evidence budget exceeded")
    return result


def _retain(f, before, after):
    for name in ("before", "after"):
        (f.root / name).mkdir(mode=0o700)
        gate._sync_directory(f.root)
    entries = []
    for name, target in after.items():
        original = before[name]
        if original is not None:
            _new_raw(f.root / "before" / name, original)
        _new_raw(f.root / "after" / name, target)
        entries.append({"name": name, "before_sha256": None if original is None else _sha(original),
                        "after_sha256": _sha(target)})
    return entries


def _marker(f, intent):
    return {"schema": 1, "operation_id": f.plan["finalize_operation_id"],
            "evidence_root": str(f.root), "intent_sha256": gate._hash(intent)}


def _validate_intent(f, intent):
    keys = {"schema", "plan", "paths", "context", "actual", "old", "target", "check", "entries", "created_at"}
    gate._require(set(intent) == keys and intent["schema"] == 1 and intent["plan"] == f.plan
                  and intent["paths"] == f.paths and intent["target"] == f.target
                  and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", intent["created_at"]),
                  "original finalize intent differs")
    names = [prefix + "." + field for prefix in ("previous", "current") for field in (*FIELDS, "state")]
    names.append("history.tsv")
    gate._require([row["name"] for row in intent["entries"]] == names, "exact registry transition inventory required")
    before = {}
    for row in intent["entries"]:
        gate._require(set(row) == {"name", "before_sha256", "after_sha256"}
                      and gate.HEX64.fullmatch(row["after_sha256"])
                      and (row["before_sha256"] is None or gate.HEX64.fullmatch(row["before_sha256"])),
                      "invalid registry transition identity")
        value = _raw(f.root / "before" / row["name"], missing=True, private_mode=True)
        gate._require((None if value is None else _sha(value)) == row["before_sha256"], "retained registry original changed")
        before[row["name"]] = value
    expected = _targets(f, intent["old"], before, intent["created_at"])
    gate._require(all(_sha(expected[row["name"]]) == row["after_sha256"] for row in intent["entries"]),
                  "registry target is not the original exact release transition")


def _replace(f, row):
    f.paths_current()
    name = row["name"]
    target = _raw(f.root / "after" / name, private_mode=True)
    gate._require(_sha(target) == row["after_sha256"], "finalize retained target changed")
    path = f.registry / name
    current = _raw(path, missing=True)
    digest = None if current is None else _sha(current)
    gate._require(digest in (row["before_sha256"], row["after_sha256"]), "registry changed outside original finalize")
    if digest != row["after_sha256"]:
        pending = f.registry / (".controlled-" + f.plan["finalize_operation_id"] + "-" + name)
        if gate._present(pending):
            _confirmed(pending, target)
        else:
            _new_raw(pending, target)
        gate._require(_raw(path, missing=True) == current, "registry changed before finalize CAS")
        os.replace(pending, path)
    # A previous rename can have succeeded while directory fsync failed.
    gate._confirm_file(path)
    gate._sync_directory(f.registry)
    gate._require(_raw(path) == target, "registry changed after finalize CAS")


def _result(intent):
    return {"schema": 1, "operation_id": intent["plan"]["finalize_operation_id"], "status": "registered",
            "intent_sha256": gate._hash(intent), "target": intent["target"], "previous": intent["old"],
            "sender_operation_id": intent["actual"]["sender_operation_id"], "release_registered": True,
            "durable": True, "automatic_rollback_authorized": False,
            "historical_coverage": "unknown_before_T0_no_replay"}


def _complete(f, intent):
    f.paths_current()
    _validate_intent(f, intent)
    marker = _marker(f, intent)
    if gate._present(f.marker):
        gate._require(runtime.read_confirmed(f.marker) == marker, "another finalize is pending")
    else:
        gate._write(f.marker, marker)
    for row in intent["entries"]:
        _replace(f, row)
    result = _result(intent)
    gate._write(f.root / "result.json", result)
    gate._require(runtime.read_confirmed(f.root / "result.json") == result, "registration result durability uncertain")
    f.marker.unlink()
    gate._sync_directory(f.registry)
    return result


def finalize(receiver_session, sender_driver, plan, evidence_root):
    f = _Finalizer(receiver_session, sender_driver, plan, evidence_root)
    gate._require(not any(f.root.iterdir()) and not gate._present(f.marker)
                  and not gate._present(f.marker.with_suffix(".json.pending")), "finalize attempted; query original operation only")
    context = f.context(active=True)
    actual = f.actual(active=True)
    old, before = _snapshot(f)
    gate._require(actual["retained_previous_images"]
                  and set(actual["retained_previous_images"].values()) == {old[1]},
                  "current registry does not identify retained previous application images")
    check = f.check(old)
    gate._require(f.context(active=True) == context and f.actual(active=True) == actual,
                  "finalize authority or original owners changed during health check")
    old_again, before_again = _snapshot(f)
    gate._require(old_again == old and before_again == before, "registry changed during formal check")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries = _retain(f, before, _targets(f, old, before, stamp))
    intent = {"schema": 1, "plan": f.plan, "paths": f.paths, "context": context, "actual": actual,
              "old": old, "target": f.target, "check": check, "entries": entries, "created_at": stamp}
    gate._write(f.root / "intent.json", intent)
    return _complete(f, runtime.read_confirmed(f.root / "intent.json"))


def query(receiver_session, sender_driver, plan, evidence_root):
    f = _Finalizer(receiver_session, sender_driver, plan, evidence_root)
    intent = runtime.read_confirmed(f.root / "intent.json")
    gate._require(intent["plan"] == f.plan and intent["paths"] == f.paths
                  and intent["context"] == f.context(active=False), "query original finalize/source/registry only")
    _validate_intent(f, intent)
    result_path = f.root / "result.json"
    if gate._present(result_path) and not gate._present(result_path.with_suffix(".json.pending")):
        result = runtime.read_confirmed(result_path)
        gate._require(result == _result(intent), "finalize receipt differs")
        for row in intent["entries"]:
            gate._require(_sha(_raw(f.registry / row["name"])) == row["after_sha256"], "registered state changed")
        gate._require(not gate._present(f.marker.with_suffix(".json.pending")), "pending marker durability uncertain")
        if gate._present(f.marker):
            gate._require(runtime.read_confirmed(f.marker) == _marker(f, intent), "pending finalize differs")
            f.marker.unlink()
            gate._sync_directory(f.registry)
        return {**result, "query_only": True, "current_health_rechecked": False}
    gate._require(f.actual(active=False) == intent["actual"], "original activation changed before finalize recovery")
    f.check(intent["old"], recovery=True)
    gate._require(f.context(active=False) == intent["context"] and f.actual(active=False) == intent["actual"],
                  "original activation changed during recovery check")
    return {**_complete(f, intent), "query_only": True, "current_health_rechecked": True}


def _cli_call(request, action):
    from scripts import production_admission_supervisor as supervisor
    r, s = request["receiver"], request["sender"]
    gate._require(set(r) == {"migration_root", "runtime_root", "state", "operation_id"}
                  and set(s) == {"plan_file", "token_file", "dsn_file", "url", "evidence_root"}, "invalid capability references")
    sender_plan = json.loads(_raw(private.safe_path(s["plan_file"]), private_mode=True))
    container_id = sender_plan["binding"]["component"]["id"]
    gate._require(isinstance(container_id, str) and re.fullmatch(r"[0-9a-f]{64}", container_id),
                  "exact finalize sender namespace container ID required")
    permit = runtime.read_confirmed(Path(r["migration_root"]) / "permit.json")
    # Match the controller's bounded formal-check settings, without inheriting
    # arbitrary host values into the sealed application configuration.
    gate_settings = {key: os.environ[key] for key in ("RELEASE_TEST_ATTESTATION_DIR", "RELEASE_HEALTH_TIMEOUT_SECONDS",
                                                   "RELEASE_HEALTH_POLL_SECONDS") if key in os.environ}
    with supervisor.sealed_configuration(permit):
        os.environ.update(gate_settings)
        transport = sender.NativeSenderActivationTransport(s["url"], s["token_file"],
            container_id=container_id)
        driver = sender.SenderActivationDriver(sender_plan, transport,
            sender.PostgresReader(s["dsn_file"], sender_plan["postgres_identity"]), s["evidence_root"])
        with activation.locked_activation(r["migration_root"], r["runtime_root"], r["state"],
                                          operation_id=r["operation_id"]) as session:
            return (finalize if action == "finalize" else query)(session, driver, request["plan"], request["evidence_root"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("finalize", "query"), required=True)
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(_raw(private.safe_path(args.request), private_mode=True))
    gate._require(set(request) == {"plan", "receiver", "sender", "evidence_root"}
                  and request["plan"]["image_ref"] == args.image_ref, "exact controlled finalize CLI request required")
    print(json.dumps(_cli_call(request, args.action), sort_keys=True))


if __name__ == "__main__":
    main()
