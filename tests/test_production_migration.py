"""Real synthetic SQLite/flock; PG lock adapter is a unit-test double here."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import os
import sqlite3
import time
from types import SimpleNamespace

import pytest

from core.config import config as holder
from db import database, startup_migration_steps as steps
from scripts import independent_drain_inventory as inventory
from services import admission_fence as gate, controlled_startup as isolation
from services import production_migration as migration


class FrozenSenderDouble:
    """No claim of actual PostgreSQL proof; native integration supplies that."""
    def __init__(self):
        self.snapshot = inventory.capacity._postgres_counts([("outbox", "acked", 1, 1024, 1, 0, 1, 0)])
        self.live = False
        self.checks = 0

    @contextmanager
    def locked(self, operation, manifest):
        assert operation == "7" * 64 and manifest == "8" * 64
        assert not self.live
        self.live = True
        try:
            yield self
            self.check()
        finally:
            self.live = False

    def check(self):
        self.checks += 1
        gate._require(self.live, "synthetic sender lock lost")
        return {"synthetic_only": True}


def write(path, raw):
    with path.open("xb") as handle:
        handle.write(raw)
    path.chmod(0o600)
    return path


def target_schema(directory, monkeypatch):
    target = directory / "review-target.db"
    with monkeypatch.context() as patch:
        patch.setattr(database, "DB_PATH", str(target))
        database.init_db()
    target.chmod(0o600)
    conn = migration._connection(target)
    try:
        for step in steps.STEP_IDS:
            conn.execute("BEGIN IMMEDIATE")
            steps.apply_step(conn, step, now=1)
            conn.execute("COMMIT")
        return migration.schema_fingerprint(conn)
    finally:
        conn.close()


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "_boot_identity", lambda: "b" * 64)
    # The sealed release loader has separate filesystem regression coverage;
    # native integration uses the actual clean-source seal and actual PG lock.
    monkeypatch.setattr(migration, "_release_contract", lambda permit, **kwargs: None)
    # Keep every original in the retained fixture root without repeating a long
    # directory label in thousands of bounded inventory records.
    directory = tmp_path.resolve() / "m"
    directory.mkdir(mode=0o700)
    db = directory / "fixture.db"
    monkeypatch.setattr(database, "DB_PATH", str(db))
    old_umask = os.umask(0o077)
    try:
        database.init_db()
        expected_schema = target_schema(directory, monkeypatch)
    finally:
        os.umask(old_umask)
    conn = migration._connection(db)
    conn.executemany("INSERT OR REPLACE INTO app_meta(k,v) VALUES(?,?)", zip(inventory.META_KEYS, ("5", "5", "100")))
    cfg = write(directory / "config.yaml", b"synthetic_runtime: true\n")
    binding = {"revision": "a" * 40, "image_id": "sha256:" + "b" * 64, "strategy": "synthetic",
               "source_sha256": gate.source_sha256(), "deployment_nonce": "c" * 32}
    now = int(time.time())
    fence_root = directory / "fence"
    gate.provision(fence_root, binding, {"not_before": now - 1, "expires_at": now + 600,
                                        "max_admissions": 10, "max_bytes": 10000})
    fence = gate.Fence(fence_root, binding)
    fence.open_trial("1" * 64, 0, "synthetic initial open")
    fence.stop_new("2" * 64, 1, "synthetic stop")
    frozen = fence.freeze("3" * 64, 2, "synthetic freeze")
    spool, journal = directory / "spool", directory / "journal"
    for path in (spool, journal, spool / "inflight", spool / "dead-letter"):
        path.mkdir(mode=0o700)
    for path in (spool / ".lock", journal / ".lock", directory / "release.lock", directory / "refresh.lock"):
        write(path, b"")
    store = inventory.capacity.receipt.ReceiptStore(spool)
    accepted, _ = store.accept(b"synthetic exact bytes", {"entries": []}, strategy="synthetic", revision="synthetic", accepted_at=100.0)
    store.update_status(accepted["delivery_id"], status="completed", completion={"synthetic": True})
    spec = {"spool": str(spool), "journal": str(journal), "database": str(db), "identities": {
        "spool": inventory.directory_identity(spool), "journal": inventory.directory_identity(journal),
        "database": inventory._identity(db.lstat())}}
    sender = FrozenSenderDouble()
    monkeypatch.setattr(migration, "_postgres_reader", lambda permit: sender)
    with inventory.locked_inventory(spec, postgres=lambda: sender.snapshot) as sample:
        inventory_hash = migration.inventory_fingerprint(sample)
    sender_contract = {"dsn_file": str(directory / "unread-test-only-dsn"), "dsn_sha256": "0" * 64,
                       "expected_identity": {"synthetic_only": True}, "closed_operation_id": "7" * 64, "manifest": "8" * 64}
    lock_paths = {"release": directory / "release.lock", "refresh": directory / "refresh.lock",
                  "controller": fence_root / "locks/controller.lock", "activity": fence_root / "locks/activity.lock",
                  "spool": spool / ".lock", "journal": journal / ".lock"}
    permit = {"schema": migration.SCHEMA, "operation_id": "4" * 64, "parent_operation_id": "5" * 64,
              "approved_by": "synthetic-reviewer", "not_before": now - 1, "expires_at": now + 600,
              "runtime_sources_sha256": isolation.runtime_sources_sha256(), "migration_code_sha256": migration.migration_code_sha256(),
              "source": {"revision": binding["revision"], "tree": "d" * 40, "image_id": binding["image_id"],
                         "private_contract_sha256": "e" * 64, "manifest_sha256": "f" * 64},
              "config": {"path": str(cfg), "sha256": hashlib.sha256(cfg.read_bytes()).hexdigest(), "effective_sha256": gate._hash(holder.cfg),
                         "private_contract_path": str(directory / "not-loaded-unit-contract.json")},
              "database": migration.database_identity(db),
              "freeze": {"root": str(fence_root), "binding": binding, "manifest_sha256": gate._hash(fence._manifest()),
                         "operation_id": "3" * 64, "generation": 3, "receipt_sha256": gate._hash(frozen)},
              "locks": {key: migration.file_identity(path) for key, path in lock_paths.items()},
              "before": migration.database_fingerprint(conn), "target_schema_sha256": expected_schema,
              "step_ids": list(steps.STEP_IDS), "sender": sender_contract,
              "sender_contract_sha256": gate._hash(sender_contract), "inventory": spec,
              "inventory_sha256": inventory_hash, "minimum_free_bytes": 64 * 1024**2, "max_database_bytes": 4 * 1024**3}
    set_compatibility(permit)
    try:
        yield {"directory": directory, "root": directory / "migration", "permit": permit,
               "anchor": conn, "sender": sender, "db": db, "fence": fence, "store": store}
    finally:
        conn.close()


def set_compatibility(permit):
    permit["compatibility"] = {"old_binding": deepcopy(permit["freeze"]["binding"]), "candidate_source": deepcopy(permit["source"]),
        "from_schema_sha256": permit["before"]["schema_sha256"], "to_schema_sha256": permit["target_schema_sha256"],
        "sqlite_policy": "fixed-current-event-core-forward-only", "old_image_execution": "not-proven-no-fallback"}
    permit["compatibility_sha256"] = gate._hash(permit["compatibility"])


def test_real_steps_are_atomic_journaled_and_never_reinitialize(prepared, monkeypatch):
    p = prepared
    monkeypatch.setattr(database, "init_db", lambda: pytest.fail("normal initializer must never run"))
    with migration.locked_session(p["root"], p["permit"]) as session:
        session.authorize()
        receipt = session.migrate()
        assert receipt["status"] == "migration_completed" and receipt["durable"]
        assert receipt["completed_steps"] == list(steps.STEP_IDS)
        assert receipt["postcondition"]["schema_sha256"] == p["permit"]["target_schema_sha256"]
        assert not receipt["business_open"] and not receipt["switch_authorized"]
        assert session.query_migration(p["permit"]["operation_id"]) == receipt
        with pytest.raises(gate.AdmissionError, match="already attempted"):
            session.migrate()
    assert p["sender"].checks >= len(steps.STEP_IDS)
    rows = p["anchor"].execute(f"SELECT count(*) FROM {migration.JOURNAL}").fetchone()[0]
    assert rows == len(steps.STEP_IDS)


@pytest.mark.parametrize("number", range(len(steps.STEP_IDS)))
@pytest.mark.parametrize("point", ["before", "after_changes", "after_commit"])
def test_each_commit_boundary_preserves_original_and_query_never_executes(prepared, monkeypatch, number, point):
    p = prepared
    original, confirm = steps.apply_step, isolation._confirm_database
    with migration.locked_session(p["root"], p["permit"]) as session:
        session.authorize()
        def apply(conn, name, **kwargs):
            if name == steps.STEP_IDS[number] and point == "before":
                raise OSError("synthetic pre-step fault")
            result = original(conn, name, **kwargs)
            if name == steps.STEP_IDS[number] and point == "after_changes":
                raise OSError("synthetic pre-marker fault")
            return result
        confirmations = []
        def failing_confirm(path):
            confirmations.append(path)
            if point == "after_commit" and len(confirmations) == number + 1:
                raise OSError("synthetic post-commit fsync/ACK fault")
            return confirm(path)
        with monkeypatch.context() as patch:
            patch.setattr(steps, "apply_step", apply)
            patch.setattr(isolation, "_confirm_database", failing_confirm)
            with pytest.raises(OSError):
                session.migrate()
        intent = (p["root"] / "migration.intent.json").read_bytes()
        monkeypatch.setattr(steps, "apply_step", lambda *_a, **_k: pytest.fail("query re-executed a migration"))
        receipt = session.query_migration(p["permit"]["operation_id"])
        committed = number + int(point == "after_commit")
        assert receipt["completed_steps"] == list(steps.STEP_IDS[:committed])
        assert receipt["durable"] == (committed == len(steps.STEP_IDS))
        with pytest.raises(gate.AdmissionError, match="already attempted"):
            session.migrate()
        assert (p["root"] / "migration.intent.json").read_bytes() == intent


@pytest.mark.parametrize("change", ["inode", "source", "schema", "config", "lock", "expired", "generation", "steps", "inventory"])
def test_wrong_identity_refused_before_authorization_or_database_write(prepared, change):
    p, permit = prepared, deepcopy(prepared["permit"])
    if change == "inode":
        permit["database"]["file"]["inode"] += 1
    elif change == "source":
        permit["runtime_sources_sha256"] = "0" * 64
    elif change == "schema":
        permit["before"]["schema_sha256"] = "0" * 64
    elif change == "config":
        permit["config"]["sha256"] = "0" * 64
    elif change == "lock":
        permit["locks"]["refresh"]["inode"] += 1
    elif change == "expired":
        permit["not_before"], permit["expires_at"] = int(time.time()) - 100, int(time.time()) - 1
    elif change == "generation":
        permit["freeze"]["generation"] = 2
    elif change == "steps":
        permit["step_ids"] = list(reversed(steps.STEP_IDS))
    else:
        permit["inventory_sha256"] = "0" * 64
    before = migration.database_fingerprint(p["anchor"])
    with pytest.raises(gate.AdmissionError):
        with migration.locked_session(p["root"], permit) as session:
            session.authorize()
    assert migration.database_fingerprint(p["anchor"]) == before
    assert not p["root"].exists()


def test_normal_initializer_commit_and_storage_pragma_cannot_escape_step(prepared):
    p = prepared
    conn = migration._connection(p["db"])
    migration._step_authorizer(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.controlled_step = True
        with pytest.raises(gate.AdmissionError, match="independent commit"):
            conn.commit()
        for sql in ("COMMIT", "PRAGMA synchronous=NORMAL", "PRAGMA journal_mode=DELETE", "PRAGMA max_page_count=999999999"):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(sql)
        assert conn.in_transaction and conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    finally:
        conn.controlled_step = False
        conn.close()


def test_committed_ack_loss_can_be_queried_after_free_space_changes(prepared, monkeypatch):
    p = prepared
    original = gate._write
    with migration.locked_session(p["root"], p["permit"]) as session:
        session.authorize()
        def fail_receipt(path, value):
            if path.name == "migration.json":
                raise OSError("synthetic lost host receipt acknowledgement")
            return original(path, value)
        with monkeypatch.context() as patch:
            patch.setattr(gate, "_write", fail_receipt)
            with pytest.raises(OSError):
                session.migrate()
    original_reader = inventory.capacity._receiver_locked
    def changed_free(*args, **kwargs):
        value = original_reader(*args, **kwargs)
        return {**value, "physical_free_bytes": value["physical_free_bytes"] - 4096}
    monkeypatch.setattr(inventory.capacity, "_receiver_locked", changed_free)
    monkeypatch.setattr(steps, "apply_step", lambda *_a, **_k: pytest.fail("recovery repeated migration"))
    with migration.locked_session(p["root"], p["permit"]) as restarted:
        assert restarted.query_migration(p["permit"]["operation_id"])["status"] == "migration_completed"


def test_wrong_operation_query_cannot_refresh_or_replace_authority(prepared):
    p = prepared
    with migration.locked_session(p["root"], p["permit"]) as session:
        session.authorize()
        session.migrate()
        before = (p["root"] / "authorization.json").read_bytes()
        with pytest.raises(gate.AdmissionError, match="operation differs"):
            session.query_migration("0" * 64)
        with pytest.raises(gate.AdmissionError, match="already attempted"):
            session.authorize()
        assert (p["root"] / "authorization.json").read_bytes() == before


def test_legacy_event_rows_without_backfill_marker_are_refused_before_any_step(prepared):
    p = prepared
    p["anchor"].execute("DELETE FROM app_meta WHERE k=?", (database.EVENT_CORE_BACKFILL_META_KEY,))
    # Existing outbox plus the schema marker is sufficient to make this an
    # unsupported legacy start, even when the visible queue has no work.
    p["permit"]["before"] = migration.database_fingerprint(p["anchor"])
    set_compatibility(p["permit"])
    with migration.locked_session(p["root"], p["permit"]) as session:
        with pytest.raises(database.EventCoreSchemaError, match="missing"):
            session.authorize()
    assert not p["root"].exists()


def test_a_different_process_cannot_use_the_inherited_lock_capability(prepared, monkeypatch):
    p = prepared
    with migration.locked_session(p["root"], p["permit"]) as session:
        actual = os.getpid()
        with monkeypatch.context() as patch:
            patch.setattr(migration.os, "getpid", lambda: actual + 1)
            with pytest.raises(gate.AdmissionError, match="session is not live"):
                session.authorize()
    assert not p["root"].exists()


def test_old_frozen_image_and_new_candidate_are_distinct_reviewed_endpoints(prepared):
    p = prepared
    p["permit"]["source"]["revision"] = "6" * 40
    p["permit"]["source"]["image_id"] = "sha256:" + "7" * 64
    set_compatibility(p["permit"])
    with migration.locked_session(p["root"], p["permit"]) as session:
        session.authorize()
        assert session.migrate()["status"] == "migration_completed"


def test_arbitrary_compatibility_hash_or_swapped_endpoints_refused(prepared):
    permit = deepcopy(prepared["permit"])
    permit["compatibility"]["old_binding"]["image_id"] = "sha256:" + "0" * 64
    permit["compatibility_sha256"] = gate._hash(permit["compatibility"])
    with pytest.raises(gate.AdmissionError, match="compatibility endpoints"):
        with migration.locked_session(prepared["root"], permit):
            pytest.fail("swapped endpoints admitted")
    assert not prepared["root"].exists()


@pytest.mark.parametrize("field,value", [("score", 9.0), ("mapping_confidence", 0.2), ("domain_event_id", "changed")])
def test_retained_outbox_full_rows_detect_same_identity_payload_change(field, value):
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE integration_signal_outbox(seq INTEGER,ledger_signal_id INTEGER,event_id TEXT,"
                     "score REAL,mapping_confidence REAL,domain_event_id TEXT)")
        conn.execute("INSERT INTO integration_signal_outbox VALUES(1,2,'same-signal',5.0,0.8,'same-domain')")
        before = steps.retained_outbox(conn)
        conn.execute(f"UPDATE integration_signal_outbox SET {field}=?", (value,))
        after = steps.retained_outbox(conn)
        assert before["rows"] == after["rows"] == 1
        assert before["columns"] == after["columns"]
        assert before["content_sha256"] != after["content_sha256"]


def test_database_identity_supports_reviewed_multigigabyte_storage_without_reading_or_copying(tmp_path):
    tmp_path = tmp_path.resolve()
    tmp_path.chmod(0o700)
    path = tmp_path / "synthetic-sparse.db"
    with path.open("xb") as handle:
        handle.truncate(2 * 1024**3)
    path.chmod(0o600)
    assert migration.database_identity(path)["file"]["inode"] == path.stat().st_ino
    # Metadata capacity only; not a 2 GiB SQL migration or production throughput claim.


@pytest.mark.parametrize("value", ["8", "-1", "07", " 7", "7.0", "unknown"])
def test_unknown_news_version_refused_readonly_before_authorization(prepared, value):
    p = prepared
    p["anchor"].execute("UPDATE app_meta SET v=? WHERE k='schema_version'", (value,))
    p["permit"]["before"] = migration.database_fingerprint(p["anchor"])
    set_compatibility(p["permit"])
    before = migration.database_fingerprint(p["anchor"])
    with migration.locked_session(p["root"], p["permit"]) as session:
        with pytest.raises(RuntimeError, match="unsupported news schema version"):
            session.authorize()
    assert migration.database_fingerprint(p["anchor"]) == before
    assert not p["root"].exists()


def test_binary_news_marker_is_not_treated_as_a_text_version():
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE app_meta(k TEXT PRIMARY KEY,v TEXT NOT NULL)")
        conn.execute("INSERT INTO app_meta VALUES('schema_version',?)", (b"7",))
        changes = conn.total_changes
        with pytest.raises(RuntimeError, match="unsupported news schema version"):
            steps.preflight(conn)
        assert conn.total_changes == changes


@pytest.mark.parametrize("value", [None, *map(str, range(8))])
def test_supported_news_marker_preflight_is_readonly(prepared, value):
    p = prepared
    if value is None:
        p["anchor"].execute("DELETE FROM app_meta WHERE k='schema_version'")
    else:
        p["anchor"].execute("UPDATE app_meta SET v=? WHERE k='schema_version'", (value,))
    before = migration.database_fingerprint(p["anchor"])
    conn = migration._connection(p["db"], readonly=True)
    try:
        steps.preflight(conn)
        assert conn.total_changes == 0
    finally:
        conn.close()
    assert migration.database_fingerprint(p["anchor"]) == before


def test_pg_lock_lost_during_real_step_rolls_back_business_and_marker(prepared, monkeypatch):
    p = prepared
    p["anchor"].execute("UPDATE app_meta SET v='0' WHERE k='schema_version'")
    p["permit"]["before"] = migration.database_fingerprint(p["anchor"])
    set_compatibility(p["permit"])
    original = steps.apply_step
    def lose_lock(conn, name, **kwargs):
        result = original(conn, name, **kwargs)
        if name == "news-schema-1":
            p["sender"].live = False
        return result
    monkeypatch.setattr(steps, "apply_step", lose_lock)
    with pytest.raises(gate.AdmissionError, match="sender lock lost"):
        with migration.locked_session(p["root"], p["permit"]) as session:
            session.authorize()
            session.migrate()
    assert p["anchor"].execute("SELECT v FROM app_meta WHERE k='schema_version'").fetchone()[0] == "0"
    assert p["anchor"].execute(f"SELECT count(*) FROM {migration.JOURNAL}").fetchone()[0] == 1
    monkeypatch.setattr(steps, "apply_step", lambda *_a, **_k: pytest.fail("lost-lock query executed business SQL"))
    with migration.locked_session(p["root"], p["permit"]) as recovered:
        receipt = recovered.query_migration(p["permit"]["operation_id"])
    assert receipt["completed_steps"] == ["base-tables"] and receipt["durable"] is False


@pytest.mark.parametrize("commit_number", [1, len(steps.STEP_IDS)])
def test_lock_loss_immediately_after_commit_is_unknown_and_queries_only(prepared, monkeypatch, commit_number):
    p, commits = prepared, []
    original = migration._Connection
    class LostLock(original):
        def execute(self, sql, *args, **kwargs):
            result = super().execute(sql, *args, **kwargs)
            if sql == "COMMIT":
                commits.append(sql)
                if len(commits) == commit_number:
                    p["sender"].live = False
            return result
    monkeypatch.setattr(migration, "_Connection", LostLock)
    with pytest.raises(gate.AdmissionError, match="sender lock lost"):
        with migration.locked_session(p["root"], p["permit"]) as session:
            session.authorize()
            session.migrate()
    monkeypatch.setattr(steps, "apply_step", lambda *_a, **_k: pytest.fail("committed step repeated"))
    with migration.locked_session(p["root"], p["permit"]) as recovered:
        result = recovered.query_migration(p["permit"]["operation_id"])
        with pytest.raises(gate.AdmissionError, match="already attempted"):
            recovered.migrate()
    assert len(commits) == commit_number
    assert len(result["completed_steps"]) == commit_number
    assert result["durable"] is (commit_number == len(steps.STEP_IDS))


def test_sqlite_page_limit_and_wal_budget_abort_before_oversize_commit(prepared, monkeypatch):
    p = prepared
    p["permit"]["max_database_bytes"] = 64 * 1024**2
    original = steps.apply_step
    def grow(conn, name, **kwargs):
        result = original(conn, name, **kwargs)
        if name == "base-tables":
            assert conn.execute("PRAGMA max_page_count").fetchone()[0] * conn.execute("PRAGMA page_size").fetchone()[0] == 64 * 1024**2
            conn.execute("CREATE TABLE synthetic_capacity_probe(value BLOB)")
            for _ in range(10):
                conn.execute("INSERT INTO synthetic_capacity_probe VALUES(zeroblob(?))", (7 * 1024**2,))
        return result
    monkeypatch.setattr(steps, "apply_step", grow)
    with pytest.raises((sqlite3.DatabaseError, gate.AdmissionError), match="full|budget|size"):
        with migration.locked_session(p["root"], p["permit"]) as session:
            session.authorize()
            session.migrate()
    assert not steps.table_exists(p["anchor"], "synthetic_capacity_probe")
    assert not steps.table_exists(p["anchor"], migration.JOURNAL)
    assert migration.database_fingerprint(p["anchor"]) == p["permit"]["before"]
    assert (p["root"] / "migration.intent.json").exists()


def test_normal_migration_fits_minimum_reviewed_database_budget(prepared):
    p = prepared
    p["permit"]["max_database_bytes"] = 64 * 1024**2
    with migration.locked_session(p["root"], p["permit"]) as session:
        session.authorize()
        assert session.migrate()["status"] == "migration_completed"


def test_free_space_drop_during_step_rolls_back_but_does_not_block_exact_query(prepared, monkeypatch):
    p, low = prepared, []
    original, statvfs = steps.apply_step, migration.os.statvfs
    def apply(conn, name, **kwargs):
        result = original(conn, name, **kwargs)
        low.append(True)
        return result
    def free(path):
        return SimpleNamespace(f_bavail=1, f_frsize=4096) if low else statvfs(path)
    monkeypatch.setattr(steps, "apply_step", apply)
    monkeypatch.setattr(migration.os, "statvfs", free)
    with pytest.raises(gate.AdmissionError, match="free-space floor"):
        with migration.locked_session(p["root"], p["permit"]) as session:
            session.authorize()
            session.migrate()
    assert migration.database_fingerprint(p["anchor"]) == p["permit"]["before"]
    with migration.locked_session(p["root"], p["permit"]) as recovered:
        result = recovered.query_migration(p["permit"]["operation_id"])
    assert result["status"] == "migration_uncertain" and result["completed_steps"] == []


def test_exact_migration_query_survives_sender_ack_age_boundary(prepared, monkeypatch):
    p = prepared
    p["sender"].snapshot["acked_at_least_7_days"] = 0
    with migration.locked_session(p["root"], p["permit"]) as session:
        session.authorize()
        receipt = session.migrate()
    p["sender"].snapshot["acked_at_least_7_days"] = 1
    monkeypatch.setattr(steps, "apply_step", lambda *_a, **_k: pytest.fail("age boundary replayed a migration"))
    with migration.locked_session(p["root"], p["permit"]) as resumed:
        assert resumed.inventory["sender"]["acked_at_least_7_days"] == 1
        assert resumed.inventory["sender"]["cleanup_credit"] == 0
        assert resumed.query_migration(p["permit"]["operation_id"]) == receipt


@pytest.mark.parametrize("change", [lambda s:s.update(rows=2,outbox_rows=2,states={"pending":0,"inflight":0,"held":0,"acked":2}),
    lambda s:s.update(charged_bytes=s["charged_bytes"]+1,outbox_charged_bytes=s["outbox_charged_bytes"]+1),
    lambda s:s.update(max_attempts=s["max_attempts"]+1)])
def test_true_sender_inventory_change_still_blocks_original_migration_query(prepared, change):
    p = prepared
    with migration.locked_session(p["root"], p["permit"]) as session:
        session.authorize()
        session.migrate()
    change(p["sender"].snapshot)
    with pytest.raises(gate.AdmissionError, match="inventory differs"):
        with migration.locked_session(p["root"], p["permit"]):
            pytest.fail("actual retained data drift accepted")
