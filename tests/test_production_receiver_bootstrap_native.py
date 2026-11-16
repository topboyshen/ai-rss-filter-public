"""Real synthetic storage and locks; Docker/PG doubles stay in tests only."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from test_production_legacy_takeover import case as legacy_case, save  # noqa: F401
from test_production_receiver_bootstrap import sender_fixture, envelope
from scripts import production_receiver_bootstrap_native as native
from scripts import production_sender_bootstrap_native as sender_native
from scripts import production_legacy_takeover as legacy
from services import admission_fence as gate


@pytest.fixture
def prepared(request, tmp_path, monkeypatch):
    old = request.getfixturevalue("legacy_case")
    root = tmp_path.resolve() / "native"
    root.mkdir(mode=0o700)
    p = native.prepare(root)
    for role in ("api", "worker"):
        old.state.rows[role]["Mounts"] = [{"Type": "bind", "Source": str(p["data"]), "Destination": "/app/data", "RW": True}]
        old.plan["containers"][role] = legacy.container_identity(old.state.rows[role])
    save(old.path, old.plan)
    sender, pg = sender_fixture(root, monkeypatch)
    value = json.loads(Path(sender["plan"]["path"]).read_bytes())
    value["binding"].update(initial_operation_id=sender["closed_operation_id"], generation=0)
    sender["plan"] = native.write(root / "driver-plan.json", value)
    driver = native.receiver._driver({"sender": sender})
    monkeypatch.setattr(native.receiver.private, "source_identity", lambda _root: {"source_sha": "a" * 40})
    try:
        yield SimpleNamespace(root=root, p=p, old=old, pg=pg, driver=driver)
    finally:
        native.preserve(root, p)


@contextmanager
def held(p):
    with native.receiver.retained_release_lock(p.p["release_lock"]) as release:
        with legacy.locked_takeover(p.old.path, p.old.evidence) as cap:
            cap.stop()
            yield cap, release


def test_real_storage_execute_query_and_retained_originals(prepared):
    p = prepared
    assert not (p.p["spool"] / "receipts").exists()
    with held(p) as (cap, release):
        proof = native.exercise(p.root, p.p, cap, release, p.driver, "a" * 40)
    assert proof["status"] == "passed" and proof["duplicate_execute_rejected"]
    assert proof["query"]["query_only"] and proof["result"]["status"] == "new_metadata_prepared"
    assert proof["historical_raw_coverage"] == "unknown_not_replayed"
    assert proof["protocol_T0_established"] is proof["migration_authorized"] is proof["power_loss_proven"] is False
    assert len(proof["lock_precheck"]["exclusive_storage_paths"]) == 3
    assert proof["lock_precheck"]["postgres_reader_same_instance"] is True
    assert proof["before_sha256"] == proof["after_sha256"]
    assert all(native.original(Path(path))[0] == value for path, value in p.p["originals"].items())
    assert list((p.p["spool"] / "receipts/tasks").iterdir()) == []
    assert all(item.connection is None for item in p.pg.locks)


@pytest.mark.parametrize("kind", ["queue", "inflight", "unknown"])
def test_real_pending_or_unknown_old_files_block_without_discard(prepared, kind):
    p = prepared
    if kind == "unknown":
        native.write(p.p["spool"] / "unknown.partial", b"synthetic retained original")
    else:
        envelope(p.p["spool"] / "inflight" if kind == "inflight" else p.p["spool"])
    before = {str(path): path.read_bytes() for path in p.p["spool"].rglob("*") if path.is_file()}
    with held(p) as (cap, release), pytest.raises((gate.AdmissionError, native.receiver.inventory.InventoryError)):
        native.exercise(p.root, p.p, cap, release, p.driver, "a" * 40)
    assert not (p.p["spool"] / "receipts").exists()
    assert before == {str(path): path.read_bytes() for path in p.p["spool"].rglob("*") if path.is_file()}


def test_real_directory_fsync_failure_remains_original_query_only(prepared, monkeypatch):
    p, sync = prepared, gate._sync_directory
    def fail(path):
        if path == p.p["spool"] / "receipts":
            raise OSError("synthetic receipt fsync barrier failure")
        return sync(path)
    with held(p) as (cap, release):
        monkeypatch.setattr(gate, "_sync_directory", fail)
        with pytest.raises(OSError, match="fsync barrier"):
            native.exercise(p.root, p.p, cap, release, p.driver, "a" * 40)
        inode = (p.p["spool"] / "receipts").stat().st_ino
        monkeypatch.setattr(gate, "_sync_directory", sync)
        value = json.loads((p.root / "receiver-bootstrap-plan.json").read_bytes())
        result = native.receiver.query(cap, release, p.driver, value, p.root / "receiver-bootstrap")
        assert result["status"] == "bootstrap_uncertain" and result["metadata_recreated"] is False
    assert (p.p["spool"] / "receipts").stat().st_ino == inode
    assert not (p.p["spool"] / "receipts/tasks").exists()
    assert (p.root / "receiver-bootstrap/STOP.json").is_file()
    assert not (p.root / "RECEIVER_BOOTSTRAP_PROOF.json").exists()


def test_preservation_copies_small_sqlite_originals_before_anchor_close(tmp_path):
    root = tmp_path.resolve()
    root.chmod(0o700)
    with native.prepared(root) as p:
        originals = {suffix: native.original(Path(str(p["database"]) + suffix)) for suffix in ("", "-wal", "-shm")}
    record = json.loads((root / "RECEIVER_SQLITE_PRESERVED.json").read_bytes())
    assert record["copied_before_synthetic_anchor_close"] and not record["production_database_access"]
    for suffix, (identity, raw) in originals.items():
        target = root / "receiver-sqlite-original-copies" / ("cache.db" + suffix)
        assert target.read_bytes() == raw and target.stat().st_mode & 0o777 == 0o600
        assert record["copies"][str(p["database"]) + suffix]["original"] == identity
    with pytest.raises(sqlite3.ProgrammingError):
        p["anchor"].execute("SELECT 1")


def test_native_has_real_capabilities_and_does_not_synthesize_receipts():
    source = Path(native.__file__).read_text()
    combined = Path(sender_native.__file__).read_text()
    assert "receiver.execute(cap, release, driver, value, evidence)" in source
    assert "receiver.query(cap, release, driver, value, evidence)" in source
    assert "LegacyTakeover.__new__" not in source and "ReceiptStore" not in source and "monkeypatch" not in source
    assert "receiver_native.receiver.retained_release_lock(receiver[\"release_lock\"])" in combined
    assert "bootstrap._driver(cap, plan, evidence, recovery=True)" in combined
    assert "receiver_native.exercise(root, receiver, cap, release, driver" in combined
    assert all(name in sender_native.SAFE_FILES for name in ("RECEIVER_OLD_STORAGE.json", "RECEIVER_BOOTSTRAP_PROOF.json",
                                                           "RECEIVER_SQLITE_PRESERVED.json"))
    assert sender_native.MAX_SECONDS == 180
