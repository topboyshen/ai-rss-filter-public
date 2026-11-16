"""Real SQLite/files/flocks; explicit Docker and PG doubles, no live services."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import time

import pytest

from test_production_legacy_takeover import case as legacy_case, save  # noqa: F401
from test_sender_drain_contract import synthetic_manifest
from scripts import production_receiver_bootstrap as bootstrap
from scripts import production_legacy_takeover as legacy
from services import admission_fence as gate


def write(path, value):
    raw = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True).encode()
    with path.open("xb") as stream:
        stream.write(raw)
    path.chmod(0o600)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def envelope(path, *, terminal=False, **changes):
    task = {"entries": [], "type": "refresh"}
    digest = gate._hash(task)
    name = digest + (".1.terminal.json" if terminal else ".json")
    return write(path / name, {"version": 1, "digest": digest, "task": task, "attempts": 0, **changes})


def journal_record(path, state="exhausted"):
    _, _, key = bootstrap.inventory.continuations.identity("synthetic", "https://example.invalid/retained")
    value = {"schema": 1, "article_key": key, "link": "https://example.invalid/retained", "strategy_version": "synthetic",
             "state": state, "attempts": 3, "created_at": 100, "updated_at": 101, "next_attempt_at": 102}
    if state in bootstrap.inventory.continuations.ACTIVE_STATES:
        value["payload"] = {"title": "synthetic", "summary": "", "prompt": "synthetic", "feed_url": "synthetic"}
        if state == "commit_pending":
            value["frozen_result"] = {"score": 1}
    return write(path / (key + ".json"), value)


def sender_fixture(root, monkeypatch):
    manifest, _ = synthetic_manifest(root)
    binding = manifest["sender_binding"]
    binding["component"]["id"] = "9" * 64
    from production_sender_driver import bridge_source, sources
    binding["source_sha256"] = bridge_source()
    binding["application_source_sha256"] = hashlib.sha256((bootstrap.ROOT / "ops/miniflux/pause-source-lock.json").read_bytes()).hexdigest()
    manifest["postgres_identity"]["schema_version"] = 135
    plan = {"schema": 1, "scope": "instrumented_sender_only", "binding": binding, "sources": sources(),
            "postgres_identity": manifest["postgres_identity"], "window": manifest["window"]}
    spec = {"plan": write(root / "sender.json", plan), "dsn": write(root / "dsn.json", {"dsn": "synthetic-no-connect"}),
            "token": write(root / "token.json", {"token": "a" * 64}), "url": "http://127.0.0.1:8081",
            "evidence_root": str(root / "sender-evidence"), "closed_operation_id": "7" * 64}
    state = SimpleNamespace(live=True, checks=0, closed=True, locks=[])
    def closed(_self, operation):
        gate._require(state.closed, "synthetic sender no longer closed")
        return {"operation_id": operation, "state": "closed"}
    monkeypatch.setattr(bootstrap.SenderActivationDriver, "query_initialize_closed", closed)
    def enter(frozen):
        frozen.snapshot = frozen.latest_snapshot = {"gate": {"phase": "closed", "current_operation": spec["closed_operation_id"]}}
        frozen.connection = object()
        state.locks.append(frozen)
        return frozen
    def check(frozen):
        state.checks += 1
        gate._require(state.live and state.closed and frozen.connection is not None, "synthetic PG closed lock lost")
    def leave(frozen, *_args):
        frozen.connection = None
    monkeypatch.setattr(bootstrap.FrozenPostgres, "__enter__", enter)
    monkeypatch.setattr(bootstrap.FrozenPostgres, "check", check)
    monkeypatch.setattr(bootstrap.FrozenPostgres, "__exit__", leave)
    return spec, state


@pytest.fixture
def prepared(request, tmp_path, monkeypatch):
    old = request.getfixturevalue("legacy_case")
    root = tmp_path.resolve()
    data = root / "actual-data"
    data.mkdir(mode=0o700)
    spool, journal, database = data / "webhook_spool", data / "natural_score_continuations", data / "cache.db"
    for path in (spool, journal, spool / "inflight", spool / "dead-letter"):
        path.mkdir(mode=0o700)
    for path in (spool / ".lock", journal / ".lock", root / "release.lock"):
        write(path, b"")
    anchor = sqlite3.connect(database)
    anchor.execute("PRAGMA journal_mode=WAL")
    anchor.execute("CREATE TABLE app_meta(k TEXT PRIMARY KEY,v TEXT)")
    anchor.executemany("INSERT INTO app_meta VALUES(?,?)", zip(bootstrap.inventory.META_KEYS, ("1", "1", "100")))
    anchor.commit()
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        path.chmod(0o600)
    for role in ("api", "worker"):
        old.state.rows[role]["Mounts"] = [{"Type": "bind", "Source": str(data), "Destination": "/app/data", "RW": True}]
        old.plan["containers"][role] = legacy.container_identity(old.state.rows[role])
    save(old.path, old.plan)
    with legacy.locked_takeover(old.path, old.evidence) as session:
        session.stop()
    evidence = root / "receiver-evidence"
    evidence.mkdir(mode=0o700)
    sender, state = sender_fixture(root, monkeypatch)
    spec = {"spool": str(spool), "journal": str(journal), "database": str(database),
            "identities": {"spool": bootstrap.inventory.directory_identity(spool),
                           "journal": bootstrap.inventory.directory_identity(journal),
                           "database": bootstrap.inventory._identity(database.lstat())}}
    now = int(time.time())
    plan = {"schema": bootstrap.SCHEMA, "operation_id": "f" * 64, "revision": "a" * 40, "sources": bootstrap.sources(),
            "approved_by": "synthetic-reviewer", "not_before": now - 1, "expires_at": now + 300,
            "host_boot_id": old.plan["host_boot_id"], "legacy_operation_id": old.plan["operation_id"],
            "legacy_plan_sha256": gate._hash(old.plan), "release_lock": bootstrap.migration.file_identity(root / "release.lock"),
            "inventory": spec, "database_identity": bootstrap.migration.database_identity(database),
            "database_fingerprint": bootstrap.migration.database_fingerprint(anchor), "sender": sender}
    monkeypatch.setattr(bootstrap.private, "source_identity", lambda _root: {"source_sha": "a" * 40})
    try:
        yield SimpleNamespace(root=root, data=data, spool=spool, journal=journal, database=database, anchor=anchor,
                              plan=plan, old=old, state=state, evidence=evidence, driver=bootstrap._driver(plan))
    finally:
        anchor.close()


@contextmanager
def held(p):
    with bootstrap.retained_release_lock(p.plan["release_lock"]["path"]) as release:
        with legacy.locked_takeover(p.old.path, p.old.evidence) as old:
            yield old, release


def call(p, action="execute"):
    with held(p) as (old, release):
        return getattr(bootstrap, action)(old, release, p.driver, p.plan, p.evidence)


def old_bytes(p):
    return {str(path): path.read_bytes() for root in (p.spool, p.journal) for path in root.rglob("*") if path.is_file()}


def test_real_empty_legacy_metadata_and_all_originals_preserved(prepared):
    p = prepared
    write(p.spool / ".completed-digests", {"retained": 123})
    envelope(p.spool / "dead-letter", terminal=True)
    journal_record(p.journal)
    before = old_bytes(p)
    result = call(p)
    assert result["status"] == "new_metadata_prepared"
    assert result["protocol_T0_established"] is result["migration_authorized"] is result["business_open"] is False
    assert result["historical_raw_coverage"] == "unknown_not_replayed" and old_bytes(p) == before
    for path in (p.spool / "receipts", p.spool / "receipts/tasks"):
        assert path.stat().st_mode & 0o777 == 0o700
    assert bootstrap._read(p.evidence / "before.json") == bootstrap._read(p.evidence / "after.json")
    assert call(p, "query")["query_only"] is True
    with pytest.raises(gate.AdmissionError, match="already attempted"):
        call(p)
    assert len(p.old.state.calls) == 5  # Original cron install and four TERM only.


@pytest.mark.parametrize("kind", ["queue", "inflight", "archive", "terminal_pending", "pending", "running", "commit_pending", "unknown"])
def test_known_work_and_unknown_artifacts_block_before_new_metadata(prepared, kind):
    p = prepared
    if kind in {"pending", "running", "commit_pending"}:
        journal_record(p.journal, kind)
    elif kind == "unknown":
        write(p.spool / "unknown.partial", b"retained")
    elif kind == "terminal_pending":
        record = envelope(p.spool / "dead-letter", terminal=True, archive_pending={"synthetic": True})
        path = Path(record["path"])
        path.rename(path.with_name(path.name.replace(".terminal.", ".terminal-pending.")))
    else:
        envelope(p.spool / "inflight" if kind == "inflight" else p.spool,
                 **({"archive_pending": {"synthetic": True}} if kind == "archive" else {}))
    before = old_bytes(p)
    with pytest.raises((gate.AdmissionError, bootstrap.inventory.InventoryError)):
        call(p)
    assert not (p.spool / "receipts").exists() and old_bytes(p) == before
    assert not list(p.evidence.iterdir())


@pytest.mark.parametrize("point", ["intent", "receipts_mkdir", "receipts_sync", "tasks_mkdir", "tasks_sync", "created", "after", "final"])
def test_each_creation_failure_is_query_only_without_recreating(prepared, monkeypatch, point):
    p = prepared
    original_write, original_mkdir, original_sync = bootstrap.records.durable_write, Path.mkdir, gate._sync_directory
    def write_record(path, value):
        if path == p.evidence / (point + ".json"):
            if point == "final":
                original_write(path, value)
            raise OSError("synthetic durable record failure")
        return original_write(path, value)
    def mkdir(path, *args, **kwargs):
        if (point == "receipts_mkdir" and path == p.spool / "receipts") or (point == "tasks_mkdir" and path == p.spool / "receipts/tasks"):
            raise OSError("synthetic mkdir failure")
        return original_mkdir(path, *args, **kwargs)
    def sync(path):
        if (point == "receipts_sync" and path == p.spool / "receipts") or (point == "tasks_sync" and path == p.spool / "receipts/tasks"):
            raise OSError("synthetic directory barrier failure")
        return original_sync(path)
    monkeypatch.setattr(bootstrap.records, "durable_write", write_record)
    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(gate, "_sync_directory", sync)
    with pytest.raises(OSError):
        call(p)
    before = {str(path): path.stat().st_ino for path in p.spool.rglob("*")}
    monkeypatch.setattr(bootstrap.records, "durable_write", original_write)
    monkeypatch.setattr(Path, "mkdir", lambda *_args, **_kwargs: pytest.fail("query attempted mkdir"))
    monkeypatch.setattr(gate, "_sync_directory", original_sync)
    if point == "intent":
        with pytest.raises(gate.AdmissionError, match="outcome unknown"):
            call(p, "query")
    else:
        result = call(p, "query")
        assert result["status"] == ("new_metadata_prepared" if point == "final" else "bootstrap_uncertain")
    assert before == {str(path): path.stat().st_ino for path in p.spool.rglob("*")}
    with pytest.raises(gate.AdmissionError, match="already attempted"):
        call(p)


@pytest.mark.parametrize("lock_name", ["spool", "journal", "release"])
def test_real_lock_contention_does_not_create_metadata(prepared, lock_name):
    p = prepared
    path = Path(p.plan["release_lock"]["path"]) if lock_name == "release" else Path(p.plan["inventory"][lock_name]) / ".lock"
    with path.open("rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises((gate.AdmissionError, gate.AdmissionBusy, bootstrap.inventory.InventoryError, BlockingIOError, TimeoutError)):
            call(p)
    assert not (p.spool / "receipts").exists()


def test_all_actual_locks_held_during_mkdir_and_released_after_failure(prepared, monkeypatch):
    p = prepared
    original = Path.mkdir
    paths = [p.old.lock, Path(p.plan["release_lock"]["path"]), p.spool / ".lock", p.journal / ".lock"]
    def mkdir(path, *args, **kwargs):
        if path == p.spool / "receipts":
            for lock in paths:
                with lock.open("rb") as stream, pytest.raises(BlockingIOError):
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert p.state.locks[-1].connection is not None
            raise OSError("synthetic crash")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "mkdir", mkdir)
    with pytest.raises(OSError):
        call(p)
    for lock in paths:
        with lock.open("rb") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert p.state.locks[-1].connection is None


@pytest.mark.parametrize("drift", ["source", "permission", "alias", "database", "missing_sidecar", "sender_closed", "old_restarted", "existing_receipts", "wrong_journal"])
def test_real_identity_and_permission_boundaries_fail_closed(prepared, drift):
    p = prepared
    if drift == "source":
        p.plan["revision"] = "b" * 40
    elif drift == "permission":
        p.spool.chmod(0o755)
    elif drift == "alias":
        p.spool.rename(p.data / "retained-spool")
        p.spool.symlink_to(p.data / "retained-spool")
    elif drift == "database":
        p.plan["database_fingerprint"]["schema_sha256"] = "0" * 64
    elif drift == "missing_sidecar":
        Path(str(p.database) + "-wal").rename(p.data / "retained-wal")
    elif drift == "sender_closed":
        p.state.closed = False
    elif drift == "old_restarted":
        p.old.state.rows["worker"]["State"].update(Running=True, Status="running")
    elif drift == "existing_receipts":
        (p.spool / "receipts").mkdir(mode=0o700)
    else:
        p.plan["inventory"]["journal"] = str(p.data / "invented-empty-journal")
    with pytest.raises((gate.AdmissionError, bootstrap.inventory.InventoryError, bootstrap.private.ContractError, FileNotFoundError)):
        call(p)
    assert not list(p.evidence.iterdir())


@pytest.mark.parametrize("change", ["original", "new_work", "sender", "new_receipt", "metadata_inode", "evidence_hash"])
def test_query_rejects_any_new_work_or_changed_original(prepared, change):
    p = prepared
    write(p.spool / ".completed-digests", {"preserved": 1})
    call(p)
    if change == "original":
        (p.spool / ".completed-digests").write_text('{"preserved":2}')
    elif change == "new_work":
        envelope(p.spool)
    elif change == "sender":
        p.state.closed = False
    elif change == "new_receipt":
        write(p.spool / "receipts/unknown.json", {"caller_ready": True})
    elif change == "metadata_inode":
        (p.spool / "receipts/tasks").rename(p.spool / "receipts/retained-tasks")
        (p.spool / "receipts/tasks").mkdir(mode=0o700)
    else:
        value = bootstrap._read(p.evidence / "before.json")
        value["counts"]["retained_dead_letters"] += 1
        bootstrap.records.durable_write(p.evidence / "before.json", value)
    with pytest.raises((gate.AdmissionError, bootstrap.inventory.InventoryError)):
        call(p, "query")


def test_sender_lock_lost_before_final_preserves_unknown(prepared, monkeypatch):
    p = prepared
    original = bootstrap.records.durable_write
    def write_record(path, value):
        original(path, value)
        if path == p.evidence / "after.json":
            p.state.live = False
    monkeypatch.setattr(bootstrap.records, "durable_write", write_record)
    with pytest.raises(gate.AdmissionError, match="PG closed lock lost"):
        call(p)
    assert not (p.evidence / "final.json").exists() and (p.evidence / "STOP.json").exists()
    p.state.live = True
    assert call(p, "query")["status"] == "bootstrap_uncertain"


def test_completed_query_after_window_expiry_never_recreates(prepared):
    p = prepared
    call(p)
    original_window = p.plan["not_before"], p.plan["expires_at"]
    # Advance the observed clock, preserving the exact original plan bytes.
    from unittest.mock import patch
    with patch.object(bootstrap.time, "time", return_value=original_window[1] + 1):
        assert call(p, "query")["query_only"] is True
        with pytest.raises(gate.AdmissionError, match="window expired"):
            call(p)


def test_checkpointed_wal_database_without_sidecars_never_creates_them(prepared, monkeypatch):
    p = prepared
    assert p.anchor.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    p.anchor.close()  # Real last-connection checkpoint removes both sidecars.
    identity = bootstrap.migration.database_identity(p.database)
    assert identity["wal"] is identity["shm"] is None
    p.plan["database_identity"] = identity
    before = {str(path): path.stat().st_ino for path in p.data.iterdir()}
    connect, uris = bootstrap.sqlite3.connect, []
    def readonly(database_uri, *args, **kwargs):
        assert database_uri == p.database.as_uri() + "?mode=ro&immutable=1"
        uris.append(database_uri)
        return connect(database_uri, *args, **kwargs)
    monkeypatch.setattr(bootstrap.sqlite3, "connect", readonly)
    assert call(p)["status"] == "new_metadata_prepared"
    assert call(p, "query")["query_only"] is True and uris
    assert bootstrap.migration.database_identity(p.database) == identity
    assert {str(path): path.stat().st_ino for path in p.data.iterdir()} == before


def test_existing_wal_is_read_and_never_ignored_as_immutable(prepared, monkeypatch):
    p = prepared
    p.anchor.execute("INSERT INTO app_meta VALUES('only_in_wal','retained')")
    p.anchor.commit()
    p.plan["database_fingerprint"] = bootstrap.migration.database_fingerprint(p.anchor)
    connect, uris = bootstrap.sqlite3.connect, []
    def readonly(database_uri, *args, **kwargs):
        assert database_uri == p.database.as_uri() + "?mode=ro"
        uris.append(database_uri)
        return connect(database_uri, *args, **kwargs)
    monkeypatch.setattr(bootstrap.sqlite3, "connect", readonly)
    assert call(p)["status"] == "new_metadata_prepared" and uris
    assert call(p, "query")["query_only"] is True
    assert bootstrap._read(p.evidence / "before.json")["database"]["fingerprint"] == p.plan["database_fingerprint"]
