"""Native protocol harness regressions; Docker proof belongs to hosted CI."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import production_activation_fixture as fixture
from scripts import verify_production_activation_native as native
from services import admission_fence as gate, webhook_receipts


def test_host_reader_is_the_exact_driver_capability_class():
    from scripts import production_sender_driver as sender
    assert native.PostgresReader is sender.PostgresReader


def test_synthetic_disk_is_never_opened_and_has_no_llm_backend(tmp_path, monkeypatch):
    from core import config
    monkeypatch.setattr(config, "CONFIG_PATH", config.CONFIG_PATH)
    monkeypatch.setattr(config.config, "_cfg", config.config._cfg)
    monkeypatch.setattr(config.config, "_blacklist_re", config.config._blacklist_re)
    monkeypatch.setenv("AI_FILTER_API_KEY", "synthetic-previous")
    monkeypatch.setattr(gate, "_boot_identity", lambda: "synthetic-unit-boot")
    p = fixture.prepare_disk(tmp_path / "synthetic", {"source_sha": "a" * 40}, "internal", "sha256:" + "b" * 64)
    try:
        assert p["fence"]._manifest()["binding"] == p["binding"]
        assert gate._read(p["paths"]["fence"] / "state.json")["generation"] == 0
        assert gate._read(p["paths"]["fence"] / "state.json")["phase"] == "frozen"
        assert list((p["paths"]["fence"] / "operations").iterdir()) == []
        assert p["frozen"]["historical_producers_verified"] is False
        assert config.config.cfg["llm"] == []
        assert config.config.blacklist_re.search("Synthetic activation-original")
        assert config.config.cfg["feeds"][0]["url"] == "http://fixture:8080/feed"
        from scripts import observe_handoff_capacity as capacity
        sample = capacity.sample_receiver(p["paths"]["spool"], capacity.spool_identity(p["paths"]["spool"]))
        assert sample["records"] == sample["reserved_bytes"] == sample["bytes_read"] == 0
        assert all(value == 0 for value in sample["states"].values())
        assert gate._read(p["directory"] / "SYNTHETIC_EMPTY_RECEIPT_STORE.json")["production_missing_metadata_authorized"] is False
    finally:
        p["anchor"].close()


def test_candidate_uses_internal_network_and_real_consumer_with_fixed_limits(tmp_path):
    paths = {name: tmp_path / name for name in ("data", "fence", "spool", "journal", "grants", "slots")}
    manifest = fixture.manifest_input(paths, "synthetic", "a" * 64, 2, {}, "owned-internal")
    assert manifest["networks"] == {"isolated": {"external": True, "name": "owned-internal"}}
    roles = {}
    for row in manifest["services"].values():
        roles[row["environment"]["AI_FILTER_ROLE"]] = row
        assert row["mem_limit"] == row["memswap_limit"] == "768m"
        assert row["pids_limit"] == 256 and row["cpus"] == 1
        assert row["networks"] == ["isolated"] and "network_mode" not in row
        assert not row.get("ports") and row["restart"] == "no"
        assert row["environment"]["REDIS_URL"] == "redis://redis:6379/0"
        assert row["environment"]["AI_FILTER_DASHBOARD_SNAPSHOTS"] == "0"
    assert roles["web"]["command"] == ["/app/start.sh"]
    assert roles["worker"]["command"] == ["python", "worker_main.py"]


def receipt_store(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / ".lock"
    path.touch(mode=0o600)
    return webhook_receipts.ReceiptStore(tmp_path)


def test_receipt_observer_waits_for_actual_completion_and_preserves_original_bytes(tmp_path):
    store = receipt_store(tmp_path)
    raw = b'{"entries":[{"title":"Synthetic retained","url":"https://example.invalid/a"}]}'
    original, _ = store.accept(raw, {"entries": []}, strategy="synthetic", revision="synthetic")
    assert native.completed_receipt(store, original["delivery_id"], raw) is None
    store.update_status(original["delivery_id"], status="completed", completion={"filtered_count": 1})
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = native.completed_receipt(store, original["delivery_id"], raw)
    assert result["status"] == "completed" and result["body_sha256"] == hashlib.sha256(raw).hexdigest()
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(gate.AdmissionError, match="raw delivery differs"):
        native.completed_receipt(store, original["delivery_id"], raw + b" ")


def test_receipt_observer_does_not_recover_a_pending_completion(tmp_path):
    store = receipt_store(tmp_path)
    raw = b"synthetic-original"
    original, _ = store.accept(raw, {"entries": []}, strategy="synthetic", revision="synthetic")
    store.update_status(original["delivery_id"], status="completed", completion={})
    path = store.directory / (original["delivery_id"] + ".json")
    pending = path.with_suffix(".json.pending")
    path.rename(pending)
    before = pending.read_bytes()
    assert native.completed_receipt(store, original["delivery_id"], raw) is None
    assert pending.read_bytes() == before and not path.exists()


def test_lost_ack_retains_actual_result_but_does_not_retry_request(tmp_path):
    requests = []
    def exchange(route, method, value):
        requests.append((route, method, value))
        return 200, {"confirmed": True, "result": {"operation_id": "a" * 64}}
    transport = native.ActivationTransport(SimpleNamespace(output=tmp_path, private_exchange=exchange))
    transport.drop.add("/initialize-closed")
    with pytest.raises(native.LostAck):
        transport.request("POST", "/initialize-closed", {"operation_id": "a" * 64})
    path = tmp_path / "LOST_ACK_initialize-closed.json"
    assert json.loads(path.read_bytes())["actual_response"]["confirmed"] is True
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(requests) == 1 and transport.posts == ["/initialize-closed"]


def test_foreign_actual_sender_identity_is_rejected(tmp_path):
    harness = SimpleNamespace(output=tmp_path, inspect_role=lambda role: {"Id": "b" * 64})
    with pytest.raises(gate.AdmissionError, match="sender container changed"):
        native.ActivationTransport(harness).inspect("a" * 64)


def test_actual_start_ack_loss_uses_query_and_never_second_start(tmp_path, monkeypatch):
    from services import production_container_lifecycle as lifecycle
    commands, queries = [], []
    roots = {name: tmp_path / name for name in ("grants", "slots", "lifecycle")}
    p = {"paths": roots, "operation": "a" * 64}
    created = {"containers": {"web": "b" * 64, "worker": "c" * 64}}
    monkeypatch.setattr(lifecycle, "create", lambda *a, **kw: created)
    monkeypatch.setattr(native.private, "run", lambda command: commands.append(command) or b"started")
    def start(*args, **kwargs):
        if commands:
            raise gate.AdmissionError("already attempted")
        native.private.run(["docker", "start", "b" * 64, "c" * 64])
    def query(*args, **kwargs):
        queries.append(kwargs["operation_id"])
        return {"status": "startup_pending" if len(queries) == 1 else "closed_initialized"}
    monkeypatch.setattr(lifecycle, "start", start)
    monkeypatch.setattr(lifecycle, "query", query)
    monkeypatch.setattr(native.time, "sleep", lambda _: None)
    result, closed = native.create_and_start(object(), p)
    assert result == created and closed["status"] == "closed_initialized"
    assert len(commands) == 1 and queries == [p["operation"], p["operation"]]


def test_proof_rejection_helper_does_not_hide_transport_or_programming_failure():
    with pytest.raises(AssertionError, match="unexpectedly accepted"):
        native.require_rejected(lambda: True)
    with pytest.raises(RuntimeError, match="transport lost"):
        native.require_rejected(lambda: (_ for _ in ()).throw(RuntimeError("transport lost")))


def test_non_native_fails_before_creating_output_or_contacting_docker(tmp_path, monkeypatch):
    import platform
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    output = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match="native Linux AMD64"):
        native.ActivationHarness(Path("/repo"), Path("/source"), "sha256:" + "a" * 64, Path("/binary"), output)
    assert not output.exists()


def test_failure_log_only_contains_type_hash_and_stage(tmp_path, capsys):
    secret = "synthetic-secret-token-and-body-never-log"
    try:
        raise RuntimeError(secret)
    except RuntimeError as error:
        failure = native.record_failure(error, Path(__file__).resolve().parents[1], tmp_path,
                                        "probe_exact_application_image")
    output = capsys.readouterr().out
    summary = json.loads(output.removeprefix("activation_native_failure="))
    assert summary == {"type": "RuntimeError", "message_sha256": hashlib.sha256(secret.encode()).hexdigest(),
                       "stage": "probe_exact_application_image"}
    assert secret not in output and "locations" not in output
    retained = json.loads((tmp_path / "FAILURE.json").read_bytes())
    assert retained == failure and retained["locations"]
    assert secret not in (tmp_path / "FAILURE.json").read_text()


def fresh_synthetic_spool(tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir(mode=0o700)
    for name in ("inflight", "dead-letter"):
        (spool / name).mkdir(mode=0o700)
    (spool / ".lock").touch(mode=0o600)
    return spool


def test_empty_receipt_preparation_uses_actual_exclusive_lock_and_sync(tmp_path, monkeypatch):
    spool = fresh_synthetic_spool(tmp_path)
    create, sync = webhook_receipts.ReceiptStore._directories, webhook_receipts._fsync_directory
    synchronized = []
    def check_locked(store, **kwargs):
        with pytest.raises(gate.AdmissionBusy):
            gate._lock(spool / ".lock", True, 0)
        return create(store, **kwargs)
    def record_sync(path):
        synchronized.append(path)
        sync(path)
    monkeypatch.setattr(webhook_receipts.ReceiptStore, "_directories", check_locked)
    monkeypatch.setattr(webhook_receipts, "_fsync_directory", record_sync)
    monkeypatch.setattr(webhook_receipts.ReceiptStore, "accept", lambda *_a, **_k: pytest.fail("must not fabricate original"))
    fixture.initialize_synthetic_empty_receipts(spool)
    assert set(synchronized) == {spool, spool.parent, spool / "receipts", spool / "receipts/tasks"}
    assert all(path.stat().st_mode & 0o777 == 0o700 for path in (spool / "receipts", spool / "receipts/tasks"))
    assert list((spool / "receipts/tasks").iterdir()) == []
    assert not list(spool.rglob("*.json"))


def test_missing_receipt_metadata_remains_readonly_failure(tmp_path):
    from scripts import observe_handoff_capacity as capacity
    spool = fresh_synthetic_spool(tmp_path)
    before = {str(path.relative_to(spool)): path.lstat().st_ino for path in spool.rglob("*")}
    webhook_receipts.ReceiptStore(spool)  # the constructor intentionally does not initialize storage
    with pytest.raises(FileNotFoundError):
        capacity.sample_receiver(spool, capacity.spool_identity(spool))
    assert before == {str(path.relative_to(spool)): path.lstat().st_ino for path in spool.rglob("*")}


def test_empty_receipt_preparation_propagates_sync_failure_and_retains_partial(tmp_path, monkeypatch):
    spool = fresh_synthetic_spool(tmp_path)
    def fail(_path):
        raise OSError("synthetic directory fsync failure")
    with monkeypatch.context() as patch:
        patch.setattr(webhook_receipts, "_fsync_directory", fail)
        with pytest.raises(OSError, match="directory fsync failure"):
            fixture.initialize_synthetic_empty_receipts(spool)
    assert (spool / "receipts").is_dir() and not (spool / "receipts/tasks").exists()
    with pytest.raises(gate.AdmissionError, match="must be pristine"):
        fixture.initialize_synthetic_empty_receipts(spool)


@pytest.mark.parametrize("unsafe", ["original", "link", "busy"])
def test_empty_receipt_preparation_refuses_retained_work_or_unsafe_storage(tmp_path, unsafe):
    spool = fresh_synthetic_spool(tmp_path)
    fd = None
    try:
        if unsafe == "original":
            (spool / "inflight/original.body").write_bytes(b"synthetic retained original")
        elif unsafe == "link":
            (spool / "receipts").symlink_to(tmp_path)
        else:
            fd = gate._lock(spool / ".lock", True, 0)
        before = {str(path.relative_to(spool)): path.lstat().st_ino for path in spool.rglob("*")}
        with pytest.raises(gate.AdmissionBusy if unsafe == "busy" else gate.AdmissionError):
            fixture.initialize_synthetic_empty_receipts(spool)
        assert before == {str(path.relative_to(spool)): path.lstat().st_ino for path in spool.rglob("*")}
    finally:
        if fd is not None:
            os.close(fd)
