"""Pre-open watchdog evidence while the receiver session holds sender PG locks."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import production_admission_watchdog as watchdog
from scripts import miniflux_handoff_adapter as adapter  # noqa: E402
from scripts import production_sender_driver as driver
from scripts.sender_drain_inventory import FrozenPostgres
from services import admission_fence as gate
from durable_handoff_cutover import CutoverError, durable_write  # noqa: E402
REFUSED = (CutoverError, gate.AdmissionClosed)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST, OP = "a" * 64, "b" * 64
IDENTITY = {"system_identifier": "1", "state_table_oid": 7}


def receipt(**change):
    return {"schema": 1, "manifest": MANIFEST, "operation_id": OP, "action": "initialize_closed",
            "previous_generation": -1, "generation": 0, "state": "closed", "durable": True,
            "confirmed": True, "active_leases": 0, "uncertain_leases": 0, **change}


def snapshot(**gate_change):
    return {"database_identity": IDENTITY, "retained_leases": {"active": 0, "uncertain": 0},
            "gate": {"manifest": MANIFEST, "generation": 0, "phase": "closed", "current_operation": OP, **gate_change}}


@pytest.fixture
def control(tmp_path):
    value = object.__new__(adapter.MinifluxActivationControl)
    value.root = tmp_path / "control"
    value.root.mkdir(mode=0o700)
    (value.root / "control.lock").touch(mode=0o600)
    value.binding = {"manifest": MANIFEST, "generation": 0, "initial_operation_id": OP}
    value.binding_sha256, value.window = "c" * 64, {"not_before": 1, "expires_at": 2}
    durable_write(value.root / "initialize_closed.intent.json", value._closed_intent(OP))
    durable_write(value.root / "initialize_closed.confirmed.json", receipt())
    value._exchange = lambda *a, **k: pytest.fail("frozen evidence must never contact Go")
    return value


def sender_for(control, calls):
    value = object.__new__(driver.SenderActivationDriver)
    value.control, value.plan = control, {"binding": control.binding, "postgres_identity": IDENTITY}
    value.bind = lambda: calls.append("bind") or snapshot()
    value.query_initialize_closed = lambda op: calls.append("go_query") or receipt()
    return value


def frozen_session(sample, *, connection=True):
    frozen = object.__new__(FrozenPostgres)
    frozen.connection = object() if connection else None
    frozen.latest_snapshot = sample
    return SimpleNamespace(opened=False, storage=object(), shadow=SimpleNamespace(sender=frozen))


def test_frozen_path_uses_locked_snapshot_and_recorded_receipt_without_go(control):
    calls = []
    sample, value, source = watchdog._closed_evidence(frozen_session(snapshot()), sender_for(control, calls))
    assert source == watchdog.FROZEN_EVIDENCE and value == receipt() and sample == snapshot()
    assert "go_query" not in calls


@pytest.mark.parametrize("sample", [snapshot(generation=1), snapshot(phase="open"), snapshot(current_operation="d" * 64),
                                    {**snapshot(), "retained_leases": {"active": 1, "uncertain": 0}},
                                    {**snapshot(), "database_identity": {"other": 1}}])
def test_frozen_snapshot_receipt_mismatch_fails_closed(control, sample):
    calls = []
    with pytest.raises(REFUSED):
        watchdog._closed_evidence(frozen_session(sample), sender_for(control, calls))
    assert "go_query" not in calls


@pytest.mark.parametrize("damage", ["missing", "pending", "intent", "receipt"])
def test_missing_or_conflicting_recorded_receipt_fails_closed(control, damage):
    path = control.root / "initialize_closed.confirmed.json"
    if damage == "missing":
        path.unlink()
    elif damage == "pending":
        (control.root / "initialize_closed.intent.json.pending").write_text("{}")
    elif damage == "intent":
        (control.root / "initialize_closed.intent.json").unlink()
        durable_write(control.root / "initialize_closed.intent.json", {**control._closed_intent(OP), "generation": 1})
    else:
        path.unlink()
        durable_write(path, receipt(generation=1, previous_generation=0))
    with pytest.raises(REFUSED):
        watchdog._closed_evidence(frozen_session(snapshot()), sender_for(control, []))


@pytest.mark.parametrize("session", [frozen_session(snapshot(), connection=False), frozen_session(None),
                                     SimpleNamespace(opened=False, storage=object(), shadow=SimpleNamespace(sender=None))])
def test_lost_frozen_lock_evidence_fails_closed_without_unlocked_fallback(control, session):
    calls = []
    with pytest.raises(gate.AdmissionClosed):
        watchdog._closed_evidence(session, sender_for(control, calls))
    assert calls == []


def test_non_frozen_path_still_queries_go(control):
    calls = []
    session = SimpleNamespace(opened=False, storage=None, shadow=SimpleNamespace(sender=None))
    _sample, value, source = watchdog._closed_evidence(session, sender_for(control, calls))
    assert source == watchdog.LIVE_EVIDENCE and calls == ["bind", "go_query"] and value == receipt()


def test_prepare_runs_lock_check_before_evidence_and_stops_on_lock_failure(monkeypatch):
    from services.production_activation_control import ReceiverActivationSession
    calls = []
    session = object.__new__(ReceiverActivationSession)
    session._identity = lambda active=True: calls.append("identity")

    def lost():
        calls.append("check_receiver_resume")
        raise gate.AdmissionClosed("sender PG lock ownership lost")
    session.check_receiver_resume = lost
    sender = object.__new__(watchdog.SenderActivationDriver)
    monkeypatch.setattr(watchdog, "_closed_evidence", lambda *a: calls.append("evidence"))
    with pytest.raises(gate.AdmissionClosed):
        watchdog.prepare(session, sender)
    assert calls == ["identity", "check_receiver_resume"]


def test_both_activation_paths_use_the_shared_watchdog_prepare():
    for name in ("scripts/verify_production_activation_native.py", "scripts/controlled_production_release.py"):
        source = (ROOT / name).read_text()
        assert "watchdog.prepare(receiver, " in source
    text = (ROOT / "scripts/production_admission_watchdog.py").read_text()
    body = text.split("def prepare(session, sender):", 1)[1].split("\ndef ", 1)[0]
    assert body.index("check_receiver_resume()") < body.index("_closed_evidence(session, sender)")
    assert "query_initialize_closed" not in body


def test_actual_pg_frozen_locks_held_watchdog_evidence_needs_no_go(tmp_path):
    """Real PG: while FrozenPostgres holds both locks, the frozen evidence succeeds
    and a metadata-lock transaction (what Go's Query begins with) times out."""
    import json
    import os
    raw = os.environ.get("MINIFLUX_OUTBOX_TEST_DSN")
    if not raw:
        pytest.skip("actual isolated PostgreSQL DSN not configured; no PG proof")
    from scripts import sender_drain_inventory as inventory
    from scripts.production_startup_pg_fixture import create_fixture, source_sql
    connection, fixture = create_fixture(raw, Path(os.environ["MINIFLUX_PATCHED_SOURCE"]))
    try:
        oid = fixture["expected_identity"]["state_table_oid"]
        manifest = connection.execute("SELECT manifest FROM handoff_state WHERE id=1").fetchone()[0]
        sql = source_sql(Path(os.environ["MINIFLUX_PATCHED_SOURCE"]), "internal/handoff/activation.go",
                         r"const ActivationSchemaSQL = `(.*?)`")
        with connection.transaction():
            connection.execute(sql)
            connection.execute("UPDATE schema_version SET version='135'")
            connection.execute("UPDATE handoff_state SET phase='closed',generation=0,current_operation=%s", (OP,))
            connection.execute("""INSERT INTO handoff_operations(operation_id,manifest,action,previous_generation,generation,prior_operation,status,confirmed_at)
 VALUES(%s,%s,'initialize_closed',-1,0,'','confirmed',clock_timestamp())""", (OP, manifest))
        path = tmp_path / "dsn.json"
        path.write_text(json.dumps({"dsn": fixture["dsn"]}))
        path.chmod(0o600)
        identity = dict(fixture["expected_identity"], schema_version=135)
        reader = inventory.PostgresReader(path, identity)
        control = object.__new__(adapter.MinifluxActivationControl)
        control.root = tmp_path / "control"
        control.root.mkdir(mode=0o700)
        (control.root / "control.lock").touch(mode=0o600)
        control.binding = {"manifest": manifest, "generation": 0, "initial_operation_id": OP}
        control.binding_sha256, control.window = "c" * 64, {"not_before": 1, "expires_at": 2}
        durable_write(control.root / "initialize_closed.intent.json", control._closed_intent(OP))
        durable_write(control.root / "initialize_closed.confirmed.json", receipt(manifest=manifest))
        control._exchange = lambda *a, **k: pytest.fail("frozen evidence must never contact Go")
        calls = []
        sender = sender_for(control, calls)
        sender.plan["postgres_identity"] = identity
        with reader.locked(OP, manifest) as held:
            held.check()
            session = SimpleNamespace(opened=False, storage=object(), shadow=SimpleNamespace(sender=held))
            sample, value, source = watchdog._closed_evidence(session, sender)
            assert source == watchdog.FROZEN_EVIDENCE and value["generation"] == 0 and "go_query" not in calls
            assert sample["gate"]["phase"] == "closed" and sample["gate"]["current_operation"] == OP
            import psycopg
            with psycopg.connect(fixture["dsn"]) as other:
                other.execute("SET lock_timeout='500ms'")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    other.execute("SELECT pg_advisory_xact_lock(20261023,%s)", (oid,))
    finally:
        connection.close()
