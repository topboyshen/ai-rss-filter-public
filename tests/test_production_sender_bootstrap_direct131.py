"""Offline fixture regressions; only the explicit native run proves real Docker/PG."""

from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from scripts import production_sender_bootstrap_native as native
from services import admission_fence as gate
from test_production_sender_bootstrap_native import seed_case as seed_case_fixture


@pytest.fixture
def seed_case(tmp_path):
    return seed_case_fixture.__wrapped__(tmp_path)


def test_real_old131_seed_never_fabricates_or_applies132(seed_case):
    case = seed_case
    actual = native.seed131(case.harness)
    assert [event[1] for event in case.events if event[0] == "sql"] == [
        "SELECT version FROM schema_version;"
    ]
    assert not any("UPDATE" in event[1] for event in case.events if event[0] == "sql")
    assert actual["schema"] == 131 and actual["marker_rewritten"] is False
    assert actual["upstream_reference"] == case.reference
    assert actual["production_compatibility_proven"] is False
    assert json.loads((case.output / "SEED131.json").read_bytes()) == actual
    assert not (case.output / "SEED132.json").exists()


@pytest.mark.parametrize("version", ["130", "132", "0131", "131\n132", ""])
def test_seed131_refuses_unconfirmed_marker_without_fabrication(seed_case, version):
    original = seed_case.harness.sql
    seed_case.harness.sql = lambda sql: (
        version if sql == "SELECT version FROM schema_version;" else original(sql)
    )
    with pytest.raises(
        gate.AdmissionError, match="actual upstream synthetic schema131"
    ):
        native.seed131(seed_case.harness)
    assert not (seed_case.output / "SEED131.json").exists()


def test_existing132_is_preserved_as_separate_exact_transaction(seed_case):
    native.seed132(seed_case.harness)
    first = json.loads((seed_case.output / "SEED131.json").read_bytes())
    second = json.loads((seed_case.output / "SEED132.json").read_bytes())
    assert first["schema"] == 131 and first["marker_rewritten"] is False
    assert second["schema"] == 132 and second["marker_rewritten"] is True
    assert second["marker_and_sql_same_transaction"] is True
    assert (
        inspect.signature(native.execute).parameters["predecessor_schema"].default
        == 132
    )
    assert native.MAX_SECONDS == 180


def _connection(monkeypatch, rows):
    calls = []

    class Result:
        def __init__(self, value):
            self.value = value

        def fetchall(self):
            return self.value

        def fetchone(self):
            return self.value

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql):
            calls.append(sql)
            return Result(
                rows
                if sql.startswith("SELECT version")
                else tuple("identity" for _ in native.bootstrap.PG_KEYS)
            )

    def connect(*args, **kwargs):
        assert kwargs == {"autocommit": True, "connect_timeout": 3}
        return Connection()

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    return calls


def test_predecessor131_reads_real_marker_in_read_only_transaction(monkeypatch):
    calls = _connection(monkeypatch, [("131",)])
    value = native.postgres_before("synthetic-only", expected_schema=131)
    assert value["schema_version"] == 131
    assert "BEGIN READ ONLY" in calls and calls[-1] == "ROLLBACK"
    assert all("UPDATE" not in sql for sql in calls)


@pytest.mark.parametrize("rows", [[("132",)], [("0131",)], [], [("131",), ("131",)]])
def test_direct131_metadata_refuses_wrong_ambiguous_marker_and_rolls_back(
    monkeypatch, rows
):
    calls = _connection(monkeypatch, rows)
    with pytest.raises(gate.AdmissionError, match="actual fixed seed"):
        native.postgres_before("synthetic-only", expected_schema=131)
    assert calls[-1] == "ROLLBACK"


def _old131_rows():
    created = {
        "Id": "1" * 64,
        "Image": "sha256:" + "f" * 64,
        "Name": "/owned-old131-after135",
        "Created": "2026-11-01T00:00:00.000000001Z",
        "Config": {"Labels": {native.LABEL: "owner"}, "Entrypoint": None,
                   "Cmd": ["/usr/bin/miniflux", "-migrate"]},
        "HostConfig": {"NetworkMode": "owned-private", "Memory": 768 * 1024**2,
            "MemorySwap": 768 * 1024**2, "PidsLimit": 256, "NanoCpus": 1_000_000_000,
            "Privileged": False, "SecurityOpt": ["no-new-privileges"],
            "ReadonlyRootfs": True, "CapDrop": ["ALL"]},
        "Mounts": [], "RestartCount": 0,
        "State": {"Running": True, "OOMKilled": False, "StartedAt": "2026-11-01T00:00:00.000000001Z"},
    }
    actual = deepcopy(created)
    actual["State"] = {"Status": "exited", "Running": False, "ExitCode": 0,
        "OOMKilled": False, "Paused": False, "Dead": False, "Restarting": False,
        "StartedAt": "2026-11-01T00:00:00.000000001Z",
        "FinishedAt": "2026-11-01T00:00:01.000000001Z"}
    return created, actual


@pytest.fixture
def restart_case(tmp_path, monkeypatch):
    digest = hashlib.sha256(native.DIRECT131_BODY).hexdigest()
    operation, manifest, image, candidate = (
        "b" * 64,
        "c" * 64,
        "sha256:" + "d" * 64,
        "e" * 64,
    )
    original = {
        "schema": "135",
        "gate": {
            "manifest": manifest,
            "generation": 0,
            "phase": "closed",
            "current_operation": operation,
        },
        "retained": {
            "delivery_id": digest,
            "body_sha256": digest,
            "body_hex": native.DIRECT131_BODY.hex(),
            "signature": "a" * 64,
            "state": "held",
            "contract": "synthetic-bootstrap-no-model",
        },
    }
    case = SimpleNamespace(
        snapshot=original,
        sqls=[],
        commands=[],
        launches=[],
        stopped=False,
        old_code="0",
        old_logs='time=2026-11-01T00:00:00.000Z level=INFO msg="Running database migrations" current_version=135 latest_version=131',
        mutate=None,
        query_rejected=True,
    )
    before = {
        "Id": candidate,
        "Image": image,
        "State": {"Running": True, "StartedAt": "before"},
    }
    exited = {
        "Id": candidate,
        "Image": image,
        "State": {"Running": False, "ExitCode": 0, "OOMKilled": False},
    }
    after = {
        "Id": candidate,
        "Image": image,
        "State": {"Running": True, "StartedAt": "after"},
    }
    case.before, case.exited, case.after = before, exited, after
    old_created, case.old_actual = _old131_rows()
    case.old_created = old_created
    rows = iter((before, exited, after))
    monkeypatch.setattr(native.bootstrap, "_inspect", lambda _id: next(rows))
    monkeypatch.setattr(native, "_ready", lambda *_args: {"schema_version": 135})
    monkeypatch.setattr(native, "closed_http", lambda *_args: {"status": 503})

    def stale(*args):
        if case.query_rejected:
            raise gate.AdmissionError("retained sender driver identity changed")
        return {"accepted": True}

    monkeypatch.setattr(native.bootstrap, "query", stale)

    def sql(statement):
        case.sqls.append(statement)
        if statement == native.DIRECT131_ORIGINAL_SQL:
            value = deepcopy(case.snapshot)
            if case.stopped and case.mutate:
                case.mutate(value)
            return json.dumps(value)
        assert statement.startswith("INSERT INTO webhook_legacy_originals")
        return ""

    def command(args, **options):
        case.commands.append((args, options))
        if args[0] == "stop":
            case.stopped = True
        if args[0] == "wait":
            return case.old_code
        if args[0] == "logs":
            return case.old_logs
        if args[0] == "inspect":
            assert args[1] == old_created["Id"]
            return json.dumps([case.old_actual])
        return ""

    names = {}

    def launch(*args):
        case.launches.append(args)
        names[args[0]] = "owned-" + args[0]
        return deepcopy(old_created)

    harness = SimpleNamespace(
        output=tmp_path,
        image_id=image,
        owner="owner",
        network="owned-private",
        password="synthetic-only",
        redacted=lambda text: text.replace("synthetic-only", "<synthetic-secret>"),
        names=names,
        sql=sql,
        command=command,
        launch=launch,
        wait=lambda function, **kwargs: function(),
    )
    (tmp_path / "SEED131.json").write_text(
        json.dumps({"upstream_image": "sha256:" + "f" * 64})
    )
    case.harness, case.plan, case.created = (
        harness,
        {"manifest": manifest, "operation_id": operation},
        {"container_id": candidate},
    )
    return case


def _run(case):
    return native.direct131_restart_and_old_limit(
        case.harness, case.plan, case.created, object(), case.harness.output
    )


def test_direct131_restart_keeps_closed_original_and_old_image_is_not_direct_rollback(
    restart_case,
):
    case = restart_case
    result = _run(case)
    assert result["candidate_restart_closed"] and result["retained_original_unchanged"]
    assert (
        result["old_image_direct_rollback_safe"] is False
        and result["production_compatibility_proven"] is False
    )
    proof = json.loads((case.harness.output / "DIRECT131_RESTART.json").read_bytes())
    assert proof["retained_fixture_created_after_migration"] is True
    assert proof["historical_raw_coverage"] == "unknown_before_T0_no_replay"
    assert (
        proof["stale_release_binding_rejected"] is True
        and proof["power_loss_durability_proven"] is False
    )
    old = json.loads((case.harness.output / "DIRECT131_OLD_LIMIT.json").read_bytes())
    assert old["exit_code"] == 0 and old["old_image_direct_rollback_safe"] is False
    assert old["status"] == "no_op_not_schema_refusal"
    assert result["old_image_schema_refusal"] is False and result["old_image_migration_no_op"] is True
    probe = json.loads((case.harness.output / "DIRECT131_OLD_PROBE.json").read_bytes())
    assert probe["wait_code"] == "0" and probe["inspect_exit_code"] == 0
    assert all(probe["checks"].values())
    assert probe["logs"]["source_literals"] == [{"literal": "Running database migrations",
        "current_version": 135, "latest_version": 131}]
    assert case.launches[0][1] == "sha256:" + "f" * 64
    assert case.launches[0][-1] == ["/usr/bin/miniflux", "-migrate"]
    assert all(
        sql == native.DIRECT131_ORIGINAL_SQL
        or sql.startswith("INSERT INTO webhook_legacy_originals")
        for sql in case.sqls
    )
    assert (
        b'"synthetic"'
        not in (case.harness.output / "DIRECT131_RESTART.json").read_bytes()
    )


@pytest.mark.parametrize("kind", ["body", "state", "gate", "schema"])
def test_restart_original_or_closed_state_drift_is_not_success(restart_case, kind):
    def mutate(value):
        if kind == "body":
            value["retained"]["body_hex"] = "00"
        if kind == "state":
            value["retained"]["state"] = "acked"
        if kind == "gate":
            value["gate"]["phase"] = "open"
        if kind == "schema":
            value["schema"] = "134"

    restart_case.mutate = mutate
    with pytest.raises(gate.AdmissionError, match="original or closed"):
        _run(restart_case)
    assert not restart_case.launches
    assert not (restart_case.harness.output / "DIRECT131_RESTART.json").exists()


@pytest.mark.parametrize(
    "kind", ["oom", "exit137", "still_running", "same_start", "different_image"]
)
def test_unconfirmed_exit_restart_identity_blocks_old_probe(restart_case, kind):
    case = restart_case
    if kind == "oom":
        case.exited["State"]["OOMKilled"] = True
    if kind == "exit137":
        case.exited["State"]["ExitCode"] = 137
    if kind == "still_running":
        case.exited["State"]["Running"] = True
    if kind == "same_start":
        case.after["State"]["StartedAt"] = "before"
    if kind == "different_image":
        case.after["Image"] = "sha256:" + "1" * 64
    with pytest.raises(gate.AdmissionError):
        _run(case)
    assert not case.launches


def test_stale_release_identity_may_not_silently_rebind(restart_case):
    restart_case.query_rejected = False
    with pytest.raises(AssertionError, match="accepted"):
        _run(restart_case)
    assert not restart_case.launches


@pytest.mark.parametrize(
    "code,logs",
    [
        ("0", "schema version"),
        ("137", "schema version"),
        ("1", "unrelated network failure"),
        ("1", "database schema version check: network error"),
        ("1", "database schema version is newer"),
        ("0", 'msg="Running database migrations" current_version=134 latest_version=131'),
        ("0", 'msg="Running database migrations" current_version=135 latest_version=132'),
    ],
)
def test_old_image_unknown_failure_is_not_schema_refusal_proof(
    restart_case, code, logs
):
    restart_case.old_code, restart_case.old_logs = code, logs
    with pytest.raises(gate.AdmissionError, match="old fixed image"):
        _run(restart_case)
    assert not (restart_case.harness.output / "DIRECT131_OLD_LIMIT.json").exists()
    observed = json.loads((restart_case.harness.output / "DIRECT131_OLD_PROBE.json").read_bytes())
    assert observed["status"] == "observed_not_accepted"
    assert observed["logs"]["sha256"] == hashlib.sha256(logs.encode()).hexdigest()


@pytest.mark.parametrize("kind", ["oom", "running", "paused", "dead", "restarting", "exit_mismatch",
    "created_status", "missing_finished", "zero_finished", "invalid_finished", "before_started", "started",
    "image", "id", "owner", "name", "created", "missing_created", "missing_started", "command", "mount",
    "memory", "pids", "read_only", "cap_drop", "restart"])
def test_old_probe_real_lifecycle_identity_or_resource_drift_is_not_noop(restart_case, kind):
    case = restart_case
    row, state = case.old_actual, case.old_actual["State"]
    if kind in {"oom", "running", "paused", "dead", "restarting"}:
        state[{"oom": "OOMKilled", "running": "Running", "paused": "Paused",
            "dead": "Dead", "restarting": "Restarting"}[kind]] = True
    elif kind == "exit_mismatch":
        state["ExitCode"] = 1
    elif kind == "created_status":
        state["Status"] = "created"
    elif kind == "missing_finished":
        state["FinishedAt"] = ""
    elif kind == "zero_finished":
        state["FinishedAt"] = "0001-01-01T00:00:00Z"
    elif kind == "invalid_finished":
        state["FinishedAt"] = "2026-99-99T00:00:00Z"
    elif kind == "before_started":
        state["FinishedAt"] = "2026-10-31T23:59:59Z"
    elif kind == "started":
        state["StartedAt"] = "2026-10-31T23:59:59Z"
    elif kind == "missing_started":
        state.pop("StartedAt")
        case.old_created["State"].pop("StartedAt")
    elif kind == "missing_created":
        row.pop("Created")
        case.old_created.pop("Created")
    elif kind in {"image", "id", "name", "created"}:
        row[{"image": "Image", "id": "Id", "name": "Name", "created": "Created"}[kind]] = "different"
    elif kind == "owner":
        row["Config"]["Labels"][native.LABEL] = "other"
    elif kind == "command":
        row["Config"]["Cmd"] = ["/usr/bin/miniflux"]
    elif kind == "mount":
        row["Mounts"] = [{"Destination": "/unexpected"}]
    elif kind in {"memory", "pids", "read_only", "cap_drop"}:
        row["HostConfig"][{"memory": "Memory", "pids": "PidsLimit", "read_only": "ReadonlyRootfs",
            "cap_drop": "CapDrop"}[kind]] = {"memory": 1024**3, "pids": 512,
            "read_only": False, "cap_drop": []}[kind]
    elif kind == "restart":
        row["RestartCount"] = 1
    with pytest.raises((gate.AdmissionError, ValueError), match="old fixed image|actual resource"):
        _run(case)
    assert (case.harness.output / "DIRECT131_OLD_PROBE.json").exists()
    assert not (case.harness.output / "DIRECT131_OLD_LIMIT.json").exists()


@pytest.mark.parametrize("logs", ["", "database schema version is newer",
    'msg="Running database migrations" current_version=135 latest_version=131\nunknown error',
    'msg="Running database migrations" current_version=135 latest_version=131 network_error',
    'msg="Running database migrations" current_version=135 latest_version=131\n' * 64,
    "x" * (native.OLD131_LOG_BYTES + 1)])
def test_old_noop_unknown_or_truncated_log_blocks_but_preserves_diagnostic(restart_case, logs):
    restart_case.old_logs = logs
    with pytest.raises(gate.AdmissionError, match="old fixed image"):
        _run(restart_case)
    assert (restart_case.harness.output / "DIRECT131_OLD_PROBE.json").exists()
    assert not (restart_case.harness.output / "DIRECT131_OLD_LIMIT.json").exists()


def test_old_probe_safe_catalogue_and_private_redaction_never_export_arbitrary_text(restart_case):
    case = restart_case
    case.old_logs += "\npassword=synthetic-only\npostgres://account:OTHER_SECRET@pg/db\nPRIVATE_KEY=OTHER_SECRET\nopaque-unknown"
    with pytest.raises(gate.AdmissionError, match="no-op"):
        _run(case)
    safe = (case.harness.output / "DIRECT131_OLD_PROBE.json").read_bytes()
    private = (case.harness.output / "DIRECT131_OLD_LOG_REDACTED.log").read_bytes()
    assert all(secret not in safe for secret in (b"synthetic-only", b"OTHER_SECRET", b"opaque-unknown", b"postgres://"))
    assert all(secret not in private for secret in (b"synthetic-only", b"OTHER_SECRET", b"postgres://"))
    assert b"opaque-unknown" in private
    assert (case.harness.output / "DIRECT131_OLD_LOG_REDACTED.log").stat().st_mode & 0o777 == 0o600
    assert "DIRECT131_OLD_LOG_REDACTED.log" not in native.SAFE_FILES


@pytest.mark.parametrize("prefix", ["", "level=INFO ", "time=2026-10-31T16:59:02.000Z level=INFO "])
def test_old_actual_info_with_optional_reviewed_time_retains_exact_migration_fields(restart_case, prefix):
    restart_case.old_logs = prefix + 'msg="Running database migrations" current_version=135 latest_version=131\n'
    _run(restart_case)
    proof = json.loads((restart_case.harness.output / "DIRECT131_OLD_LIMIT.json").read_bytes())
    assert proof["status"] == "no_op_not_schema_refusal" and proof["old_image_direct_rollback_safe"] is False


@pytest.mark.parametrize("prefix", ["level=ERROR ", "level=DEBUG ", "level=WARN ",
    "time=2026-10-31T16:59:02.000Z ", "level=info "])
def test_old_migration_unknown_level_or_unreviewed_time_prefix_still_blocks(restart_case, prefix):
    restart_case.old_logs = prefix + 'msg="Running database migrations" current_version=135 latest_version=131\n'
    with pytest.raises(gate.AdmissionError, match="no-op"):
        _run(restart_case)
    assert not (restart_case.harness.output / "DIRECT131_OLD_LIMIT.json").exists()


def test_noop_still_requires_post_probe_original_and_closed_snapshot_unchanged(restart_case):
    original_command = restart_case.harness.command

    def command(args, **options):
        value = original_command(args, **options)
        if args[0] == "inspect":
            restart_case.snapshot["retained"]["state"] = "acked"
        return value

    restart_case.harness.command = command
    with pytest.raises(gate.AdmissionError, match="original or closed"):
        _run(restart_case)
    assert (restart_case.harness.output / "DIRECT131_OLD_PROBE.json").exists()
    assert not (restart_case.harness.output / "DIRECT131_OLD_LIMIT.json").exists()


def test_new_safe_export_records_are_allowlisted_without_removing_old132():
    assert {
        "SEED131.json",
        "SEED132.json",
        "DIRECT131_RESTART.json",
        "DIRECT131_OLD_LIMIT.json",
        "DIRECT131_OLD_PROBE.json",
    } <= set(native.SAFE_FILES)


def _host_pg_row():
    ports = {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5432"}]}
    return {
        "Id": "e" * 64,
        "Image": "sha256:" + "a" * 64,
        "Config": {
            "Labels": {native.LABEL: "owner"},
            "Healthcheck": {
                "Test": ["CMD-SHELL", "pg_isready -U synthetic -d synthetic_pause"],
                "Interval": 1_000_000_000,
                "Timeout": 1_000_000_000,
                "Retries": 5,
            },
        },
        "HostConfig": {
            "NetworkMode": "owned-private",
            "PortBindings": deepcopy(ports),
            "Memory": 384 * 1024**2,
            "MemorySwap": 384 * 1024**2,
            "PidsLimit": 64,
            "NanoCpus": 1_000_000_000,
            "Privileged": False,
            "SecurityOpt": ["no-new-privileges:true"],
        },
        "NetworkSettings": {"Networks": {"owned-private": {}}, "Ports": ports},
        "State": {"OOMKilled": False, "Health": {"Status": "healthy"}},
    }


def test_exact_pg_loopback_topology_does_not_normalize_actual_inspect():
    row = _host_pg_row()
    original = deepcopy(row)
    assert all(
        native.loopback_pg_resources(
            row, row["Image"], "owner", "owned-private", 384 * 1024**2, 64
        ).values()
    )
    assert row == original


@pytest.mark.parametrize(
    "change",
    [
        "public",
        "other_port",
        "other_actual_port",
        "extra_port",
        "hostnet",
        "extra_network",
        "memory",
        "swap",
        "pids",
        "cpu",
        "privileged",
        "security",
        "oom",
        "health",
        "owner",
        "image",
    ],
)
def test_loopback_pg_keeps_endpoint_resources_and_health_strict(change):
    row = _host_pg_row()
    host = row["HostConfig"]
    if change == "public":
        host["PortBindings"]["5432/tcp"][0]["HostIp"] = "0.0.0.0"
    if change == "other_port":
        host["PortBindings"]["5432/tcp"][0]["HostPort"] = "15432"
    if change == "other_actual_port":
        row["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"] = "15432"
    if change == "extra_port":
        host["PortBindings"]["9999/tcp"] = []
    if change == "hostnet":
        host["NetworkMode"] = "host"
    if change == "extra_network":
        row["NetworkSettings"]["Networks"]["extra"] = {}
    if change == "memory":
        host["Memory"] *= 2
    if change == "swap":
        host["MemorySwap"] *= 2
    if change == "pids":
        host["PidsLimit"] *= 2
    if change == "cpu":
        host["NanoCpus"] *= 2
    if change == "privileged":
        host["Privileged"] = True
    if change == "security":
        host["SecurityOpt"] = []
    if change == "oom":
        row["State"]["OOMKilled"] = True
    if change == "health":
        row["Config"]["Healthcheck"]["Test"] = ["CMD", "true"]
    if change == "owner":
        row["Config"]["Labels"][native.LABEL] = "other"
    if change == "image":
        row["Image"] = "sha256:" + "b" * 64
    with pytest.raises(gate.AdmissionError, match="resource/topology"):
        native.loopback_pg_resources(
            row, "sha256:" + "a" * 64, "owner", "owned-private", 384 * 1024**2, 64
        )


def _host_harness(tmp_path, row):
    harness = object.__new__(native.HostLoopbackHarness)
    harness.prefix, harness.owner, harness.network, harness.output = (
        "owned",
        "owner",
        "owned-private",
        tmp_path,
    )
    harness.names, harness.images, harness.created, harness.commands = {}, {}, [], []

    def command(args, **_kwargs):
        harness.commands.append(args)
        return json.dumps([row]) if args[0] == "inspect" else ""

    harness.command = command
    return harness


def test_host_fixture_pg_publishes_only_loopback_with_original_caps(tmp_path):
    row = _host_pg_row()
    harness = _host_harness(tmp_path, row)
    assert harness.launch("pg", row["Image"], memory=384 * 1024**2, pids=64) == row
    args = harness.commands[0]
    assert args[args.index("--network") + 1] == "owned-private"
    assert args[args.index("--publish") + 1] == "127.0.0.1:5432:5432"
    assert (
        args[args.index("--memory") + 1]
        == args[args.index("--memory-swap") + 1]
        == str(384 * 1024**2)
    )
    assert args[args.index("--pids-limit") + 1] == "64"
    assert (
        args[args.index("--health-cmd") + 1]
        == "pg_isready -U synthetic -d synthetic_pause"
    )


def test_only_old_sender_moves_to_host_without_network_alias(tmp_path):
    row = _host_pg_row()
    row["HostConfig"].update(
        NetworkMode="host",
        PortBindings={},
        Memory=768 * 1024**2,
        MemorySwap=768 * 1024**2,
        PidsLimit=256,
    )
    row["NetworkSettings"] = {"Networks": {"host": {}}, "Ports": {}}
    harness = _host_harness(tmp_path, row)
    harness.launch("old-sender", row["Image"])
    args = harness.commands[0]
    assert args[args.index("--network") + 1] == "host"
    assert "--network-alias" not in args and "--publish" not in args
    assert args[args.index("--memory") + 1] == str(768 * 1024**2)
    assert args[args.index("--pids-limit") + 1] == "256"


def test_non_sender_roles_keep_original_bridge_launcher(tmp_path, monkeypatch):
    harness = _host_harness(tmp_path, _host_pg_row())
    calls = []
    monkeypatch.setattr(
        native.Harness, "launch", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    harness.launch("old-api", "sha256:" + "a" * 64, ["--read-only"], ["python"])
    assert calls[0][0][1:3] == ("old-api", "sha256:" + "a" * 64)
    assert calls[0][1] == {"memory": 768 * 1024**2, "pids": 256}
    assert not harness.commands


def test_pg_bind_conflict_is_retained_failure_without_retry_or_port_change(tmp_path):
    harness = _host_harness(tmp_path, _host_pg_row())
    calls = []

    def fail(args, **kwargs):
        calls.append(args)
        raise RuntimeError("owned synthetic exact port already allocated")

    harness.command = fail
    with pytest.raises(RuntimeError, match="already allocated"):
        harness.launch("pg", "sha256:" + "a" * 64, memory=384 * 1024**2, pids=64)
    assert len(calls) == 1 and "127.0.0.1:5432:5432" in calls[0]
    assert harness.created == ["pg"]
    assert not any("kill" in args or "rm" in args for args in calls)


@pytest.mark.parametrize("status", ["starting", "unhealthy", None])
def test_real_pg_health_must_be_ready_not_only_socket(tmp_path, monkeypatch, status):
    row = _host_pg_row()
    row["State"]["Health"]["Status"] = status
    harness = _host_harness(tmp_path, row)
    harness.names["pg"] = "owned-pg"
    monkeypatch.setattr(
        native.Harness, "postgres_ready", lambda _self: "accepting connections"
    )
    with pytest.raises(gate.AdmissionError, match="PG health not ready"):
        harness.postgres_ready()


def test_host_mode_requires_explicit131_and_actual_daemon_host_namespace(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(native.sys, "platform", "linux")
    monkeypatch.setattr(native.os, "geteuid", lambda: 0)
    calls = []

    def not_host():
        calls.append("host_check")
        raise ValueError("collector is not in host network namespace")

    monkeypatch.setattr(native.local_pg, "_host", not_host)
    arguments = (
        tmp_path,
        tmp_path,
        "sha256:" + "a" * 64,
        tmp_path,
        tmp_path / "new",
        tmp_path,
    )
    with pytest.raises(gate.AdmissionError, match="explicit actual131"):
        native.execute(*arguments, host_loopback=True)
    assert calls == []
    with pytest.raises(ValueError, match="host network namespace"):
        native.execute(*arguments, predecessor_schema=131, host_loopback=True)
    assert calls == ["host_check"] and not list(tmp_path.iterdir())


def test_host_business_probe_has_fixed_loopback_no_proxy_redirect_or_body_read(
    monkeypatch,
):
    import http.client

    calls = []

    class Connection:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

        def request(self, *args, **kwargs):
            calls.append((args, kwargs))

        def getresponse(self):
            return SimpleNamespace(status=503)

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(http.client, "HTTPConnection", Connection)
    harness = SimpleNamespace(host_loopback=True, sql=lambda sql: "0")
    result = native.closed_http(harness, {})
    assert result["status"] == 503 and calls[0] == (("127.0.0.1", 8080), {"timeout": 3})
    assert calls[1][0] == ("POST", "/login") and calls[-1] == "closed"


def test_host_plan_seals_observed_pg_capability_and_uses_true_host_mode(
    tmp_path, monkeypatch
):
    from test_production_sender_bootstrap_native import records as build_fixture

    fixture = build_fixture.__wrapped__(tmp_path)
    directory = tmp_path / "inputs"
    directory.mkdir(mode=0o700)
    network = {
        "Name": "host",
        "Id": "a" * 64,
        "Driver": "host",
        "Scope": "local",
        "Internal": False,
        "Options": {},
        "IPAM": {},
    }
    pg = {
        "Id": "b" * 64,
        "NetworkSettings": {"Networks": {"private": {"IPAddress": "172.31.0.2"}}},
    }
    calls = []
    harness = SimpleNamespace(
        owner="d" * 32,
        password="synthetic",
        token="e" * 64,
        image_id=fixture.image,
        network="private",
        names={"pg": "owned-pg"},
        repo=native.ROOT,
        output=tmp_path,
        host_loopback=True,
        predecessor_schema=131,
        command=lambda args: json.dumps([network if args[0] == "network" else pg]),
    )
    capability = {"synthetic": "metadata-only-test-double-not-live-proof"}
    monkeypatch.setattr(
        native.local_pg,
        "observe",
        lambda dsn, identifier: calls.append((dsn, identifier)) or capability,
    )

    def before(dsn, **kwargs):
        calls.append((dsn, kwargs))
        return {"schema_version": 131}

    monkeypatch.setattr(native, "postgres_before", before)
    plan = native.prepare_bootstrap(
        harness,
        directory,
        {"operation_id": "b" * 64, "plan_sha256": "c" * 64},
        {
            "revision": fixture.revision,
            "upstream_revision": "f" * 40,
            "binary_sha256": "a" * 64,
        },
        native.build_records(fixture.root, fixture.image, fixture.revision),
    )
    compose = json.loads(Path(plan["compose"]["path"]).read_bytes())
    spec = compose["services"][plan["service"]]
    assert (
        compose["networks"] == {}
        and spec["network_mode"] == "host"
        and "networks" not in spec
    )
    assert spec["ports"] == spec["volumes"] == []
    environment = Path(plan["environment"]["path"]).read_text()
    assert (
        "@127.0.0.1:5432/" in environment
        and "LISTEN_ADDR=127.0.0.1:8080" in environment
    )
    assert (
        plan["postgres_transport"] == capability
        and plan["postgres_before"]["schema_version"] == 131
    )
    assert calls[0][1] == pg["Id"] and calls[1][1] == {
        "expected_schema": 131,
        "postgres_transport": capability,
    }
    assert {"HOST_LOOPBACK.json", "SEED132.json"} <= set(native.SAFE_FILES)


def test_host_pg_verifies_same_real_connection_and_keeps_read_only_rollback(
    monkeypatch,
):
    events = []
    cursor = object()

    class CursorContext:
        def __enter__(self):
            return cursor

        def __exit__(self, *args):
            events.append("cursor_closed")

    class Result:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return self.value

        def fetchall(self):
            return self.value

    class Connection:
        def __enter__(self):
            events.append("connection_open")
            return self

        def __exit__(self, *args):
            events.append("connection_closed")

        def execute(self, sql):
            events.append(sql)
            return Result(
                [("131",)]
                if sql.startswith("SELECT version")
                else tuple("identity" for _ in native.bootstrap.PG_KEYS)
            )

        def cursor(self):
            return CursorContext()

    capability = {"test": "exact-local-capability"}

    class Transport:
        def __init__(self, actual):
            assert actual is capability

        def check(self, dsn, *, deadline):
            assert dsn == "same-private-synthetic-dsn" and deadline > 0
            events.append("transport_check")

        def check_connection(self, actual, expected):
            assert actual is cursor and set(expected) == set(native.bootstrap.PG_KEYS)
            events.append("actual_connection_identity_check")

    def connect(dsn, **kwargs):
        assert dsn == "same-private-synthetic-dsn" and kwargs == {
            "autocommit": True,
            "connect_timeout": 3,
        }
        events.append("connect_exact_dsn")
        return Connection()

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    monkeypatch.setattr(native.local_pg, "HostLoopbackPostgres", Transport)
    result = native.postgres_before(
        "same-private-synthetic-dsn", expected_schema=131, postgres_transport=capability
    )
    assert result["schema_version"] == 131
    assert (
        events.count("connect_exact_dsn")
        == events.count("actual_connection_identity_check")
        == 1
    )
    assert events.count("transport_check") == 2
    assert events.index("transport_check") < events.index("connect_exact_dsn")
    assert "BEGIN READ ONLY" in events and events[-2:] == [
        "ROLLBACK",
        "connection_closed",
    ]


def test_host_business_open_response_is_failure_and_connection_closes(monkeypatch):
    import http.client

    calls = []

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return SimpleNamespace(status=200)

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(http.client, "HTTPConnection", Connection)
    with pytest.raises(gate.AdmissionError, match="remain closed"):
        native.closed_http(SimpleNamespace(host_loopback=True), {})
    assert calls == ["closed"]
