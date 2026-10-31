"""Real synthetic files/flocks; no Docker, cron, signals or production calls."""
from copy import deepcopy
import fcntl
import hashlib
from itertools import permutations
import json
import os
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from scripts import production_legacy_takeover as legacy
from services import admission_fence as gate


def test_native_transport_uses_fixed_bounded_capture_interface(monkeypatch, tmp_path):
    calls = []
    row = container(1, "sender")
    def capture(argv, **options):
        calls.append((argv, options))
        if "inspect" in argv:
            return json.dumps([row]).encode()
        if "kill" in argv:
            return (row["Id"] + "\n").encode()
        if "sha256sum" in argv:
            return ("a" * 64 + "  /etc/nginx/conf.d/default.conf\n").encode()
        return b"# synthetic root cron\n"
    monkeypatch.setattr(legacy.bounded, "_capture", capture)
    native = legacy.NativeTransport()
    assert native.inspect("/usr/bin/docker", row["Id"]) == row
    assert native.cron_read("/usr/bin/crontab") == b"# synthetic root cron\n"
    native.cron_install("/usr/bin/crontab", tmp_path / "after")
    native.term("/usr/bin/docker", row["Id"])
    assert native.gateway_config("/usr/bin/docker", row["Id"], "/etc/nginx/conf.d/default.conf") == "a" * 64
    assert calls[3][0] == ["/usr/bin/docker", "kill", "--signal", "TERM", row["Id"]]
    assert all(0 < options["deadline"] - time.monotonic() <= 3 and options["max_bytes"] == legacy.MAX_CRON
               for _, options in calls)


def save(path, value, mode=0o600):
    raw = value if isinstance(value, bytes) else (json.dumps(value, sort_keys=True) + "\n").encode()
    path.write_bytes(raw)
    path.chmod(mode)
    return legacy._record(path, executable=bool(mode & 0o111))[0]


def container(index, role):
    return {"Id": str(index) * 64, "Image": "sha256:" + str(index) * 64, "RestartCount": 0,
        "Config": {"Labels": {"org.opencontainers.image.revision": "a" * 40, "com.docker.compose.service": role},
                   "User": "0:0", "Cmd": ["retained-original"], "Entrypoint": None, "Env": ["SECRET=synthetic"]},
        "State": {"Status": "running", "Running": True, "StartedAt": "2026-10-27T00:00:00Z", "FinishedAt": "",
                  "Paused": False, "Restarting": False, "Dead": False, "OOMKilled": False, "ExitCode": 0},
        "Mounts": [{"Source": "/synthetic/retained/data", "Destination": "/app/data", "RW": True}],
        "NetworkSettings": {"Networks": {"retained": {}}},
        "HostConfig": {"NetworkMode": "retained", "PortBindings": {}, "Memory": 768 * 1024**2,
                       "MemorySwap": 768 * 1024**2, "PidsLimit": 256, "NanoCpus": 1000000000}}


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(legacy, "ROOT_UID", os.geteuid())  # Synthetic owner; CLI remains hard root-only.
    tmp_path = tmp_path.resolve()
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    lock = tmp_path / "cron.lock"
    lock.touch(mode=0o600)
    binaries = {name: save(tmp_path / name, b"#!/bin/sh\nexit 91\n", 0o700) for name in ("docker", "crontab", "flock", "refresh")}
    wrapper = save(tmp_path / "writer", legacy.writer_script(str(lock), binaries["crontab"]["path"], binaries["flock"]["path"]), 0o700)
    before = ("MAILTO=retained@example.invalid\n# unrelated original\n* * * * * " + binaries["refresh"]["path"] + " --natural\n").encode()
    operation, now = "a" * 64, int(legacy.time.time())
    line_sha = hashlib.sha256(before.splitlines(keepends=True)[2]).hexdigest()
    after = b"".join(before.splitlines(keepends=True)[:2]) + ("# controlled-legacy-stop " + operation + " " + line_sha + "\n").encode()
    rows = {role: container(index, role) for index, role in enumerate(legacy.ROLES, 1)}
    plan = {"schema": 1, "operation_id": operation, "source_sha256": legacy.source_sha256(), "host_boot_id": "synthetic-boot",
        "approved_by": "synthetic-review", "not_before": now - 10, "expires_at": now + 300,
        "docker": binaries["docker"], "wait_seconds": 1, "max_polls": 1,
        "containers": {role: legacy.container_identity(row) for role, row in rows.items()},
        "gateway": {"original": save(tmp_path / "gateway.conf", b"upstream old-api;\n"),
                    "container_id": rows["gateway"]["Id"], "config_target": "/etc/nginx/conf.d/default.conf",
                    "candidate_service": "candidate-api", "candidate_image_id": "sha256:" + "b" * 64,
                    "private_contract_sha256": "c" * 64},
        "cron": {"binary": binaries["crontab"], "flock": binaries["flock"], "writer": wrapper,
                 "lock": legacy._lock_identity(lock), "before": save(tmp_path / "original-cron", before),
                 "after_sha256": hashlib.sha256(after).hexdigest(),
                 "entries": [{"line": 2, "sha256": line_sha, "executable": binaries["refresh"]}],
                 "daemon": {"pid": 77, "parent": 1, "start_ticks": 101, "uid": os.geteuid(), "command_sha256": "d" * 64}}}
    inventory = {"schema": 1, "scope": "registered-root-cron-writers", "host_boot_id": plan["host_boot_id"],
        "lock": plan["cron"]["lock"], "writer": wrapper, "adapter_source_sha256": plan["source_sha256"],
        "registered_writers": [wrapper["path"], str(Path(legacy.__file__).resolve())],
        "external_root_mutation": "forbidden-during-reviewed-window", "expires_at": plan["expires_at"], "reviewed_by": "independent-synthetic-review"}
    plan["cron"]["writer_inventory"] = save(tmp_path / "writers.json", inventory)
    path = tmp_path / "plan.json"
    save(path, plan)
    state = SimpleNamespace(cron=before, rows=rows, calls=[], children=[])
    monkeypatch.setattr(legacy.NativeTransport, "host_boot", lambda _self: "synthetic-boot")
    monkeypatch.setattr(legacy.NativeTransport, "cron_read", lambda _self, binary: state.cron)
    monkeypatch.setattr(legacy.NativeTransport, "cron_children", lambda _self, daemon: state.children)
    monkeypatch.setattr(legacy.NativeTransport, "gateway_config", lambda *_args: plan["gateway"]["original"]["sha256"])
    def install(_self, binary, source):
        state.calls.append(("cron_install", binary, str(source)))
        state.cron = Path(source).read_bytes()
    def inspect(_self, binary, identifier):
        return deepcopy(next(row for row in state.rows.values() if row["Id"] == identifier))
    def term(_self, binary, identifier):
        state.calls.append(("TERM", identifier))
        row = next(row for row in state.rows.values() if row["Id"] == identifier)
        row["State"].update(Running=False, Status="exited", FinishedAt="2026-10-27T00:01:00Z")
    monkeypatch.setattr(legacy.NativeTransport, "cron_install", install)
    monkeypatch.setattr(legacy.NativeTransport, "inspect", inspect)
    monkeypatch.setattr(legacy.NativeTransport, "term", term)
    monkeypatch.setattr(legacy.time, "sleep", lambda _seconds: None)
    return SimpleNamespace(path=path, plan=plan, evidence=evidence, before=before, after=after,
                           state=state, lock=lock, inventory=inventory, term=term, install=install)


def test_stop_keeps_lock_exact_originals_and_term_only_without_drain_claim(case):
    p = case
    with legacy.locked_takeover(p.path, p.evidence) as session:
        fd = os.open(p.lock, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        result = session.stop()
        assert result["status"] == "registered_legacy_runtime_stopped"
        assert result["switch_authorized"] is result["inventory_drained"] is result["gateway_switched"] is False
        assert result["unregistered_or_detached_host_producers_proven"] is False
        assert session.check_stopped()["writer_lock_held"] is True
        assert (p.evidence / "cron.before").read_bytes() == p.before
        assert (p.evidence / "cron.after").read_bytes() == p.after
        assert (p.evidence / "gateway.original").read_bytes() == b"upstream old-api;\n"
        assert [item[1] for item in p.state.calls[1:]] == [p.plan["containers"][role]["id"] for role in legacy.ROLES]
        with pytest.raises(gate.AdmissionError, match="already attempted"):
            session.stop()
        count = len(p.state.calls)
        assert session.query()["status"] == result["status"]
        assert len(p.state.calls) == count
    fd = os.open(p.lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def test_only_mount_order_change_keeps_complete_identity_without_changing_originals(case):
    p = case
    row = p.state.rows["sender"]
    row["Mounts"].append({"Source": "/synthetic/secret-mount", "Destination": "/app/config", "RW": False})
    p.plan["containers"]["sender"] = legacy.container_identity(row)
    save(p.path, p.plan)
    row["Mounts"].reverse()
    plan_before, row_before = deepcopy(p.plan), deepcopy(row)
    with legacy.locked_takeover(p.path, p.evidence) as session:
        observed = session._containers()
    assert observed["sender"]["identity"] == p.plan["containers"]["sender"]
    assert p.plan == plan_before and row == row_before and p.state.calls == []
    assert not list(p.evidence.iterdir())


def complete_mount_records():
    first = {"Type": "bind", "Name": "", "Source": "/synthetic/data", "Destination": "/app/data",
             "Driver": "local", "Mode": "rw", "RW": True, "Propagation": "rprivate",
             "Unknown": {"nested": "\u5168\u90e8", "ordered": ["first", "second"]}}
    second = {"Source": "/synthetic/config", "Destination": "/app/config", "RW": False}
    return [first, second, deepcopy(first)]


def test_complete_mount_multiset_permutations_preserve_unknown_nested_fields_and_duplicates():
    rows = complete_mount_records()
    original = deepcopy(rows)
    expected = legacy._mounts_sha256(rows)
    for ordered in permutations(rows):
        candidate = deepcopy(list(ordered))
        before = deepcopy(candidate)
        assert legacy._mounts_sha256(candidate) == expected
        assert candidate == before
    assert rows == original
    assert len(rows) == 3 and rows[0] == rows[2]


@pytest.mark.parametrize("rows", [[], [{"Unknown": {"value": "kept"}}]])
def test_zero_and_single_complete_mount_identity_are_deterministic(rows):
    before = deepcopy(rows)
    assert legacy._mounts_sha256(rows) == gate._hash(rows)
    assert rows == before


def test_mount_dictionary_key_order_is_canonical_but_nested_array_order_is_not():
    rows = complete_mount_records()
    reversed_keys = [dict(reversed(list(row.items()))) for row in deepcopy(rows)]
    assert legacy._mounts_sha256(rows) == legacy._mounts_sha256(reversed_keys)
    reversed_keys[0]["Unknown"]["ordered"].reverse()
    assert legacy._mounts_sha256(rows) != legacy._mounts_sha256(reversed_keys)


@pytest.mark.parametrize("change", [
    "Source", "Destination", "Type", "Name", "Driver", "Mode", "RW", "Propagation", "bool-to-number",
    "add-unknown", "remove-known", "remove-unknown", "nested-value", "nested-list-order",
    "drop-duplicate", "add-duplicate", "same-destination-different-record",
])
def test_any_complete_mount_value_key_or_duplicate_count_change_still_blocks(case, change):
    p = case
    row = p.state.rows["sender"]
    row["Mounts"] = complete_mount_records()
    p.plan["containers"]["sender"] = legacy.container_identity(row)
    save(p.path, p.plan)
    mounts = row["Mounts"]
    if change in {"Source", "Destination", "Type", "Name", "Driver", "Mode", "Propagation"}:
        mounts[0][change] = "changed-full-value"
    elif change == "RW":
        mounts[0]["RW"] = False
    elif change == "bool-to-number":
        mounts[0]["RW"] = 1
    elif change == "add-unknown":
        mounts[0]["NewUnknown"] = {"preserved": True}
    elif change == "remove-known":
        del mounts[0]["Source"]
    elif change == "remove-unknown":
        del mounts[0]["Unknown"]
    elif change == "nested-value":
        mounts[0]["Unknown"]["nested"] = "changed"
    elif change == "nested-list-order":
        mounts[0]["Unknown"]["ordered"].reverse()
    elif change == "drop-duplicate":
        mounts.pop()
    elif change == "add-duplicate":
        mounts.append(deepcopy(mounts[0]))
    else:
        mounts[1]["Destination"] = mounts[0]["Destination"]
    plan_before, row_before = deepcopy(p.plan), deepcopy(row)
    with legacy.locked_takeover(p.path, p.evidence) as session:
        with pytest.raises(gate.AdmissionError, match="^old container identity changed: mounts_sha256$"):
            session.stop()
    assert p.plan == plan_before and row == row_before and p.state.calls == []
    assert not list(p.evidence.iterdir())


@pytest.mark.parametrize("rows", [None, (), {}, "unknown", [None], [[]], ["unknown"], [True]])
def test_unknown_mount_collection_or_record_shape_never_becomes_empty_or_success(rows):
    with pytest.raises(gate.AdmissionError, match="^old container mount records uncertain$"):
        legacy._mounts_sha256(rows)


@pytest.mark.parametrize("value", [float("nan"), {"not-json"}])
def test_non_json_mount_values_do_not_fall_back_to_ignored_or_stringified_values(value):
    with pytest.raises((ValueError, TypeError)):
        legacy._mounts_sha256([{"Unknown": value}])


def test_plan_with_old_adapter_source_sha_stays_rejected_before_any_stop(case, monkeypatch):
    p = case
    p.plan["source_sha256"] = "f" * 64
    save(p.path, p.plan)
    def forbidden(*_args):
        raise AssertionError("old source plan must not reach inspection")
    monkeypatch.setattr(legacy.NativeTransport, "inspect", forbidden)
    with pytest.raises(gate.AdmissionError, match="^exact legacy takeover plan required$"):
        with legacy.locked_takeover(p.path, p.evidence):
            raise AssertionError("old source plan must not obtain a live capability")
    assert not p.state.calls and not list(p.evidence.iterdir())


@pytest.mark.parametrize("field", tuple(legacy._CONTAINER_IDENTITY_ERRORS))
def test_each_protected_identity_value_change_keeps_original_comparison_blocking(case, monkeypatch, field):
    p = case
    row = deepcopy(p.state.rows["sender"])
    expected = legacy.container_identity(row)
    expected[field] = "do-not-export-secret-value"
    p.plan["containers"]["sender"] = expected
    before = deepcopy(p.plan)
    monkeypatch.setattr(legacy.NativeTransport, "inspect", lambda *_args: deepcopy(row))
    session = legacy.LegacyTakeover(p.plan, p.before, p.after, p.evidence, legacy.NativeTransport())
    with pytest.raises(gate.AdmissionError) as error:
        session._containers()
    assert str(error.value) == legacy._CONTAINER_IDENTITY_ERRORS[field]
    assert "do-not-export-secret-value" not in str(error.value)
    assert p.plan == before and p.state.calls == []


@pytest.mark.parametrize("field", tuple(legacy._CONTAINER_IDENTITY_ERRORS))
def test_each_missing_expected_identity_field_is_unknown_and_blocks_before_inspect(case, monkeypatch, field):
    p = case
    del p.plan["containers"]["sender"][field]
    def forbidden(*_args):
        raise AssertionError("missing identity must not reach inspection")
    monkeypatch.setattr(legacy.NativeTransport, "inspect", forbidden)
    session = legacy.LegacyTakeover(p.plan, p.before, p.after, p.evidence, legacy.NativeTransport())
    with pytest.raises(gate.AdmissionError, match="^old container identity changed: unknown$"):
        session._containers()
    assert p.state.calls == []


@pytest.mark.parametrize("shape", ["extra-expected", "missing-observed", "extra-observed", "raw-key", "raw-type", "raw-attribute"])
def test_unknown_identity_shapes_remain_blocked_without_runtime_values(case, monkeypatch, shape):
    p = case
    row = deepcopy(p.state.rows["sender"])
    identity = legacy.container_identity(row)
    if shape == "extra-expected":
        p.plan["containers"]["sender"]["secret-field-name"] = "secret-value"
    elif shape == "missing-observed":
        del identity["command_sha256"]
        monkeypatch.setattr(legacy, "container_identity", lambda _row: identity)
    elif shape == "extra-observed":
        identity["secret-field-name"] = "secret-value"
        monkeypatch.setattr(legacy, "container_identity", lambda _row: identity)
    elif shape == "raw-key":
        del row["Config"]
    elif shape == "raw-type":
        row["HostConfig"] = None
    else:
        row["Config"]["Labels"] = None
    monkeypatch.setattr(legacy.NativeTransport, "inspect", lambda *_args: deepcopy(row))
    session = legacy.LegacyTakeover(p.plan, p.before, p.after, p.evidence, legacy.NativeTransport())
    with pytest.raises(gate.AdmissionError) as error:
        session._containers()
    assert str(error.value) == "old container identity changed: unknown"
    assert "secret" not in str(error.value) and p.state.calls == []


def test_first_fixed_identity_difference_only_and_existing_literal_exporter_remains_strict():
    from scripts import verify_production_activation_native as native
    original = legacy.container_identity(container(1, "sender"))
    changed = deepcopy(original)
    changed["user"] = "private-account-value"
    changed["mounts_sha256"] = "/private/secret-path"
    assert legacy._container_identity_error(changed, original) == "old container identity changed: user"
    assert tuple(original) == tuple(legacy._CONTAINER_IDENTITY_ERRORS)
    repo = Path(__file__).resolve().parents[1]
    for literal in (*legacy._CONTAINER_IDENTITY_ERRORS.values(), "old container identity changed: unknown"):
        assert native.source_literal(literal, repo) == literal
    assert native.source_literal("old container identity changed: private-account-value", repo) is None
    assert native.source_literal("old container identity changed: /private/secret-path", repo) is None


def test_existing_failure_export_does_not_serialize_private_same_observation_values(tmp_path, capsys):
    from scripts import verify_production_activation_native as native
    error = gate.AdmissionError("old container identity changed: mounts_sha256")
    error.legacy_identity_observation = {"role": "sender", "identity": {"user": "private-account-sentinel"},
                                         "mounts": [{"Source": "/private/source-sentinel"}]}
    native.record_failure(error, Path(legacy.__file__).resolve().parents[1], tmp_path, "fixed_synthetic_stage")
    output = capsys.readouterr().out + (tmp_path / "FAILURE.json").read_text()
    assert "old container identity changed: mounts_sha256" in output
    assert "private-account-sentinel" not in output and "/private/source-sentinel" not in output
    assert "legacy_identity_observation" not in output


@pytest.mark.parametrize("change", ["boot", "writer_bytes", "inventory", "before", "docker", "lock_inode", "container", "restart", "cron_concurrent"])
def test_bad_bound_evidence_prevents_mutation(case, monkeypatch, change):
    p = case
    if change == "boot":
        monkeypatch.setattr(legacy.NativeTransport, "host_boot", lambda _self: "other-boot")
    elif change == "writer_bytes":
        Path(p.plan["cron"]["writer"]["path"]).write_bytes(b"#!/bin/sh\nexit 0\n")
    elif change == "inventory":
        value = deepcopy(p.inventory)
        value["scope"] = "caller_boolean_only"
        p.plan["cron"]["writer_inventory"] = save(Path(p.plan["cron"]["writer_inventory"]["path"]), value)
        save(p.path, p.plan)
    elif change in {"before", "docker"}:
        record = p.plan["cron"]["before"] if change == "before" else p.plan["docker"]
        Path(record["path"]).write_bytes(b"changed\n")
    elif change == "lock_inode":
        p.lock.rename(p.lock.with_suffix(".retained"))
        p.lock.touch(mode=0o600)
    elif change == "container":
        p.state.rows["worker"]["Image"] = "sha256:" + "f" * 64
    elif change == "restart":
        p.state.rows["api"]["RestartCount"] += 1
    else:
        p.state.cron += b"# concurrent operator\n"
    with pytest.raises((gate.AdmissionError, ValueError)):
        with legacy.locked_takeover(p.path, p.evidence) as session:
            session.stop()
    assert p.state.calls == []


@pytest.mark.parametrize("stage", ["cron_ack", "term_ack", "term_alive", "cron_children", "cron_after"])
def test_unknown_attempt_keeps_stop_and_only_queries_original(case, monkeypatch, stage):
    p = case
    if stage in {"cron_ack", "cron_after"}:
        def failed_install(*args):
            p.install(*args)
            if stage == "cron_ack":
                raise TimeoutError("synthetic lost cron setter ACK")
            p.state.cron += b"# external writer\n"
        monkeypatch.setattr(legacy.NativeTransport, "cron_install", failed_install)
    elif stage in {"term_ack", "term_alive"}:
        def failed_term(*args):
            if stage == "term_ack":
                p.term(*args)
                raise TimeoutError("synthetic lost TERM ACK")
            p.state.calls.append(("TERM", args[-1]))
        monkeypatch.setattr(legacy.NativeTransport, "term", failed_term)
    else:
        p.state.children = [{"pid": 88, "parent": 77, "start_ticks": 202}]
    with legacy.locked_takeover(p.path, p.evidence) as session:
        with pytest.raises((gate.AdmissionError, gate.AdmissionUncertain, TimeoutError)):
            session.stop()
        original = (p.evidence / "intent.json").read_bytes()
        assert (p.evidence / "STOP.json").is_file()
        count = len(p.state.calls)
        with pytest.raises(gate.AdmissionError, match="already attempted"):
            session.stop()
        if stage == "cron_after":
            with pytest.raises(gate.AdmissionError, match="outside original"):
                session.query()
        else:
            assert session.query()["status"] == "legacy_takeover_uncertain"
        assert len(p.state.calls) == count and (p.evidence / "intent.json").read_bytes() == original
        assert not (p.evidence / "stopped.json").exists()


def test_last_term_lost_ack_can_confirm_only_the_same_operation(case, monkeypatch):
    p = case
    def drop_last(*args):
        p.term(*args)
        if args[-1] == p.plan["containers"]["worker"]["id"]:
            raise TimeoutError("last actual TERM ACK discarded")
    monkeypatch.setattr(legacy.NativeTransport, "term", drop_last)
    with legacy.locked_takeover(p.path, p.evidence) as session:
        with pytest.raises(TimeoutError):
            session.stop()
        stop = (p.evidence / "STOP.json").read_bytes()
        count = len(p.state.calls)
        assert session.query()["status"] == "registered_legacy_runtime_stopped"
        assert len(p.state.calls) == count and (p.evidence / "STOP.json").read_bytes() == stop
        value = gate._read(p.evidence / "worker.term.intent.json")
        value["signal"] = "KILL"
        gate._write(p.evidence / "worker.term.intent.json", value)
        with pytest.raises(gate.AdmissionError, match="TERM intent changed"):
            session.query()


def test_expired_window_does_not_prevent_original_query_but_never_sends_again(case, monkeypatch):
    p = case
    with legacy.locked_takeover(p.path, p.evidence) as session:
        session.stop()
        count = len(p.state.calls)
        monkeypatch.setattr(legacy.time, "time", lambda: p.plan["expires_at"] + 1)
        assert session.query()["status"] == "registered_legacy_runtime_stopped"
        with pytest.raises(gate.AdmissionError, match="window expired"):
            session.stop()
        assert len(p.state.calls) == count


def test_native_signal_and_cron_commands_are_fixed_absolute_argv(case, monkeypatch):
    calls = []
    identifier = "e" * 64
    monkeypatch.setattr(legacy.bounded, "_capture", lambda args, **kw: calls.append((args, kw)) or (identifier + "\n").encode())
    # Call the original native implementation rather than the fixture doubles.
    import importlib.util
    spec = importlib.util.spec_from_file_location("isolated_legacy_native", legacy.__file__)
    isolated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolated)
    isolated.NativeTransport().term("/reviewed/docker", identifier)
    isolated.NativeTransport().cron_install("/reviewed/crontab", Path("/private/stopped-cron"))
    assert [args for args, _ in calls] == [["/reviewed/docker", "kill", "--signal", "TERM", identifier],
                                         ["/reviewed/crontab", "-u", "root", "/private/stopped-cron"]]
    assert all(0 < options["deadline"] - time.monotonic() <= 3 and options["max_bytes"] == legacy.MAX_CRON
               for _, options in calls)


def test_actual_gateway_drift_blocks_all_mutations(case, monkeypatch):
    monkeypatch.setattr(legacy.NativeTransport, "gateway_config", lambda *_args: "f" * 64)
    with legacy.locked_takeover(case.path, case.evidence) as session:
        with pytest.raises(gate.AdmissionError, match="gateway config differs"):
            session.stop()
    assert not case.state.calls and not list(case.evidence.iterdir())


def test_retained_capability_rechecks_old_runtime_before_next_phase(case):
    with legacy.locked_takeover(case.path, case.evidence) as session:
        session.stop()
        case.state.rows["api"]["State"].update(Running=True, Status="running")
        with pytest.raises(gate.AdmissionError, match="no longer stopped"):
            session.check_stopped()
    with pytest.raises(gate.AdmissionError, match="live legacy lock"):
        session.check_stopped()


def test_query_budget_cannot_be_reset_or_used_to_resignal(case, monkeypatch):
    monkeypatch.setattr(legacy, "MAX_QUERIES", 2)
    with legacy.locked_takeover(case.path, case.evidence) as session:
        session.stop()
        session.query()
        count = len(case.state.calls)
        with pytest.raises(gate.AdmissionError, match="budget exhausted"):
            session.query()
        assert len(case.state.calls) == count


def test_post_rename_sync_failure_retains_unknown_and_never_terms(case, monkeypatch):
    original = gate._write
    def fail(path, value):
        original(path, value)
        if path.name == "cron.confirmed.json":
            raise OSError("synthetic acknowledgment sync failure")
    monkeypatch.setattr(gate, "_write", fail)
    with legacy.locked_takeover(case.path, case.evidence) as session:
        with pytest.raises(OSError):
            session.stop()
        assert case.state.cron == case.after
        assert [row[0] for row in case.state.calls] == ["cron_install"]
        assert session.query()["status"] == "legacy_takeover_uncertain"
        assert (case.evidence / "STOP.json").is_file()
