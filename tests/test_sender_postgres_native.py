"""Opt-in actual fixed-source PostgreSQL reader/lock evidence, synthetic only."""

import json
import os
from pathlib import Path

import pytest

from services import sender_drain_contract as contract
from scripts import sender_drain_inventory as inventory
from scripts.production_startup_pg_fixture import create_fixture, close_fixture as close_seeded, phase
from test_sender_drain_contract import synthetic_manifest


@pytest.fixture
def actual(tmp_path):
    raw = os.environ.get("MINIFLUX_OUTBOX_TEST_DSN")
    if not raw:
        pytest.skip("actual isolated PostgreSQL DSN not configured; no PG proof")
    connection, fixture = create_fixture(raw, Path(os.environ["MINIFLUX_PATCHED_SOURCE"]))
    value, body = synthetic_manifest(tmp_path)
    binding = value["sender_binding"]
    receipt = value["control_receipt"]
    row = value["entries"][0]
    connection.execute(
        """INSERT INTO webhook_outbox(delivery_id,body_sha256,user_id,feed_id,entry_id,target_url,signature,contract,body)
 VALUES(%s,%s,1,1,1,%s,%s,%s,%s)""",
        (
            row["delivery_id"],
            row["body_sha256"],
            row["target_url"],
            row["signature"],
            row["contract"],
            body,
        ),
    )
    path = tmp_path / "dsn.json"
    path.write_text(json.dumps({"dsn": fixture["dsn"]}))
    path.chmod(0o600)
    reader = inventory.PostgresReader(path, fixture["expected_identity"])
    yield connection, reader, binding, receipt, value, body
    phase("retaining_schema_" + fixture["schema"])
    connection.close()


def test_actual_readonly_bundle_preserves_exact_body_and_pg_identity(actual, tmp_path):
    connection, reader, binding, receipt, _, body = actual
    phase("before_actual_readonly_original_snapshot")
    sample = reader.snapshot()
    assert (
        sample["states"]["pending"] == 1
        and sample["retained_leases"]["active_producers"] == 0
    )
    bundle = tmp_path / "originals"
    import time

    now = int(time.time())
    value = reader.freeze(
        binding, receipt, {"not_before": now - 1, "expires_at": now + 300}, bundle
    )
    loaded, sha = contract.load_bundle(bundle)
    assert value == loaded and sha == contract.digest(value)
    assert contract.read_original(bundle, loaded["entries"][0]) == body
    assert (
        bytes(connection.execute("SELECT body FROM webhook_outbox").fetchone()[0])
        == body
    )
    phase("original_bundle_sealed_exact_bytes_no_database_mutation")
    before = {p.name: p.read_bytes() for p in bundle.iterdir()}
    with pytest.raises(Exception):
        reader.freeze(binding, receipt, value["window"], bundle)
    assert before == {p.name: p.read_bytes() for p in bundle.iterdir()}


def close_fixture(connection, binding):
    schema = connection.execute("SELECT current_schema()").fetchone()[0]
    return close_seeded(connection, {"manifest": binding["manifest"], "generation": 3,
                                     "stop_operation_id": "9" * 64, "schema": schema})


def test_actual_frozen_session_locks_block_control_and_release_only_on_exit(actual):
    connection, reader, binding, _, _, _ = actual
    operation = close_fixture(connection, binding)
    phase("before_actual_frozen_pg_lock_acquire")
    with reader.locked(operation, binding["manifest"]) as held:
        assert held.check() == held.lock_identity
        for namespace in (20261024, 20261023):
            assert (
                connection.execute(
                    "SELECT pg_try_advisory_lock(%s,%s)",
                    (namespace, reader.expected["state_table_oid"]),
                ).fetchone()[0]
                is False
            )
        assert held.snapshot["states"]["acked"] == 1
        phase("same_real_pg_activity_and_metadata_locks_retained")
    for namespace in (20261024, 20261023):
        assert (
            connection.execute(
                "SELECT pg_try_advisory_lock(%s,%s)",
                (namespace, reader.expected["state_table_oid"]),
            ).fetchone()[0]
            is True
        )
        connection.execute(
            "SELECT pg_advisory_unlock(%s,%s)",
            (namespace, reader.expected["state_table_oid"]),
        )
    phase("actual_pg_locks_released_after_context")


def test_actual_lost_lock_session_cannot_reconnect_or_confirm(actual):
    connection, reader, binding, _, _, _ = actual
    operation = close_fixture(connection, binding)
    phase("before_actual_lost_pg_backend")
    with pytest.raises(Exception):
        with reader.locked(operation, binding["manifest"]) as held:
            pid = held.lock_identity["backend_pid"]
            assert (
                connection.execute(
                    "SELECT pg_terminate_backend(%s)", (pid,)
                ).fetchone()[0]
                is True
            )
            held.check()
    assert (
        connection.execute("SELECT phase FROM handoff_state").fetchone()[0] == "closed"
    )
    phase("backend_loss_retained_closed_no_reconnect_no_reopen")


def test_actual_held_and_wrong_database_never_yield_empty_or_original_bundle(
    actual, tmp_path
):
    connection, reader, binding, receipt, _, body = actual
    connection.execute("UPDATE webhook_outbox SET state='held'")
    import time

    now = int(time.time())
    with pytest.raises(Exception):
        reader.freeze(
            binding,
            receipt,
            {"not_before": now - 1, "expires_at": now + 300},
            tmp_path / "blocked",
        )
    assert not (tmp_path / "blocked/manifest.json").exists()
    assert (
        bytes(connection.execute("SELECT body FROM webhook_outbox").fetchone()[0])
        == body
    )
    reader.expected = {**reader.expected, "table_oid": reader.expected["table_oid"] + 1}
    with pytest.raises(Exception):
        reader.snapshot()
    phase("actual_held_original_and_wrong_oid_blocked_without_repair")


def test_actual_statement_timeout_shrinks_to_remaining_total_budget(actual):
    import time
    import psycopg
    connection, _, _, _, _, _ = actual
    phase("before_actual_remaining_statement_timeout")
    started = time.monotonic()
    with connection.cursor() as raw:
        cursor = inventory._TimedCursor(raw, started + 0.15)
        with pytest.raises(psycopg.errors.QueryCanceled):
            cursor.execute("SELECT pg_sleep(0.6)")
    assert time.monotonic() - started < 0.5
    phase("actual_postgres_cancelled_using_remaining_deadline")


def test_actual_closed_state_with_pending_marker_cannot_grant_frozen_context(actual):
    connection, reader, binding, _, _, _ = actual
    operation = "e" * 64
    connection.execute("UPDATE webhook_outbox SET state='acked',acked_at=clock_timestamp()")
    connection.execute("UPDATE handoff_state SET phase='closed',generation=4,current_operation=%s", (operation,))
    connection.execute("""INSERT INTO handoff_operations(operation_id,manifest,action,previous_generation,generation,prior_operation,status)
 VALUES(%s,%s,'pause',3,4,%s,'pending')""", (operation, binding["manifest"], "9" * 64))
    phase("before_pending_closed_marker_rejection")
    with pytest.raises(Exception, match="pause operation"):
        with reader.locked(operation, binding["manifest"]):
            pytest.fail("unconfirmed marker granted a frozen context")
    assert connection.execute("SELECT status FROM handoff_operations WHERE operation_id=%s", (operation,)).fetchone() == ("pending",)
    phase("pending_marker_retained_no_confirmation_no_frozen_context")
