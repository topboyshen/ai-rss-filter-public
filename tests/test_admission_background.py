"""Real file fence/spool with fake work, no network, model or production data."""
import importlib.util
import json
import threading
import time
from concurrent.futures import Future
from pathlib import Path

import pytest

from services import admission_fence as admission


@pytest.fixture
def background(tmp_path, monkeypatch):
    import routes.webhook_routes as webhook

    webhook.stop_webhook_dispatcher(timeout=2.0)
    root = tmp_path / "admission"
    binding = {"revision": "a" * 40, "image_id": "sha256:" + "b" * 64,
               "strategy": "synthetic", "source_sha256": admission.source_sha256(),
               "deployment_nonce": "c" * 32}
    now = int(time.time())
    admission.provision(root, binding, {"not_before": now - 1, "expires_at": now + 600,
                                     "max_admissions": 100, "max_bytes": 1024 * 1024})
    fence = admission.Fence(root, binding)
    monkeypatch.setenv("AI_FILTER_ADMISSION_MODE", "enforced")
    monkeypatch.setenv("AI_FILTER_ADMISSION_ROOT", str(root))
    monkeypatch.setenv("AI_FILTER_ADMISSION_BINDING", json.dumps(binding))
    monkeypatch.setattr(webhook, "_webhook_spool_dir", tmp_path / "spool")
    monkeypatch.setattr(webhook, "_webhook_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(webhook, "_webhook_inflight", set())
    monkeypatch.setattr(webhook, "_webhook_claims", {})
    monkeypatch.setattr(webhook, "_webhook_retry_state", {})
    yield fence, webhook
    # Test-owned in-memory ownership only. Original disk evidence is retained.
    webhook._webhook_inflight.clear()
    webhook._webhook_claims.clear()
    webhook.stop_webhook_dispatcher(timeout=2.0)


def pending_task(webhook):
    task = {"feed_url": "https://example.invalid/feed", "entries": [{
        "title": "synthetic", "url": "https://example.invalid/1", "content": "synthetic"}]}
    assert webhook._persist_webhook_task(task) == "stored"
    return task


class DeferredExecutor:
    def __init__(self):
        self.future = Future()
        self.work = None

    def submit(self, function, *args):
        self.work = (function, args)
        return self.future

    def complete(self):
        function, args = self.work
        self.future.set_result(function(*args))


def test_frozen_dispatch_does_not_renew_claim_or_touch_spool(background, monkeypatch):
    fence, webhook = background
    monkeypatch.setattr(webhook, "_renew_owned_claims_once", lambda: pytest.fail("renew before fence"))
    monkeypatch.setattr(webhook, "_claim_next_spooled_task", lambda: pytest.fail("claim before fence"))
    assert webhook._dispatch_spooled_tasks_once() == 0
    assert fence.snapshot()["active"] == 0
    assert not webhook._webhook_spool_dir.exists()


def test_draining_executes_existing_durable_task_and_holds_lease_through_callback(background, monkeypatch):
    fence, webhook = background
    fence.open_trial("1" * 64, 0, "synthetic opening")
    pending_task(webhook)
    fence.stop_new("2" * 64, 1, "synthetic stop")
    with pytest.raises(admission.AdmissionClosed):
        webhook._persist_webhook_task({"synthetic_new": True})
    executor = DeferredExecutor()
    monkeypatch.setattr(webhook, "_get_executor", lambda: executor)
    seen = []

    def execute(_path, _envelope):
        seen.append((admission.current_kind(), fence.snapshot()["active"]))
        return True

    monkeypatch.setattr(webhook, "_execute_claimed_spooled_task", execute)
    assert webhook._dispatch_spooled_tasks_once() == 1
    assert fence.snapshot()["active"] == 1
    with pytest.raises(admission.AdmissionBusy):
        fence.freeze("3" * 64, 2, "synthetic freeze", wait_seconds=0)
    # Existing activity may complete even after freeze intent, without a new
    # admission that would deadlock the queue's own durable acknowledgement.
    executor.complete()
    assert seen == [("drain", 1)]
    assert webhook._webhook_inflight == set()
    assert webhook._webhook_slots.acquire(blocking=False)
    webhook._webhook_slots.release()
    result = fence.query_operation("3" * 64, wait_seconds=0)
    assert result["phase"] == "frozen" and result["activity_drained"] is True
    assert result["queue_checked"] is False and result["switch_authorized"] is False


def test_callback_failure_keeps_uncertain_original_and_freeze_cannot_confirm(background, monkeypatch):
    fence, webhook = background
    fence.open_trial("1" * 64, 0, "synthetic opening")
    pending_task(webhook)
    executor = DeferredExecutor()
    monkeypatch.setattr(webhook, "_get_executor", lambda: executor)
    monkeypatch.setattr(webhook, "_execute_claimed_spooled_task", lambda *_a: True)
    monkeypatch.setattr(webhook, "_finish_spooled_task", lambda *_a: (_ for _ in ()).throw(OSError("sync failed")))
    assert webhook._dispatch_spooled_tasks_once() == 1
    executor.complete()
    snapshot = fence.snapshot()
    assert snapshot["uncertain"] == 1 and snapshot["active"] == 0
    originals = {p.name: p.read_bytes() for p in (fence.root / "leases").iterdir()}
    fence.stop_new("2" * 64, 1, "synthetic stop")
    with pytest.raises(admission.AdmissionClosed):
        fence.freeze("3" * 64, 2, "synthetic freeze", wait_seconds=0)
    assert originals == {p.name: p.read_bytes() for p in (fence.root / "leases").iterdir()}


def test_executor_recovery_error_preserves_uncertain_and_releases_slot(background, monkeypatch):
    fence, webhook = background
    fence.open_trial("1" * 64, 0, "synthetic opening")
    pending_task(webhook)
    monkeypatch.setattr(webhook, "_get_executor", lambda: (_ for _ in ()).throw(RuntimeError("executor failed")))
    monkeypatch.setattr(webhook, "_retry_or_dead_letter", lambda *_a: (_ for _ in ()).throw(OSError("recovery sync failed")))
    with pytest.raises(OSError, match="recovery sync failed"):
        webhook._dispatch_spooled_tasks_once()
    assert fence.snapshot()["uncertain"] == 1
    assert webhook._webhook_slots.acquire(blocking=False)
    webhook._webhook_slots.release()


@pytest.mark.parametrize("entry", ["dispatcher", "direct_submit"])
def test_admission_storage_failure_does_not_leak_queue_capacity(background, monkeypatch, entry):
    fence, webhook = background
    fence.open_trial("1" * 64, 0, "synthetic opening")
    pending_task(webhook)
    monkeypatch.setattr(webhook, "reserve_lease", lambda *_a, **_k: (_ for _ in ()).throw(OSError("admission fsync failed")))
    with pytest.raises(OSError, match="admission fsync failed"):
        if entry == "dispatcher":
            webhook._dispatch_spooled_tasks_once()
        else:
            assert webhook._webhook_slots.acquire(blocking=False)
            webhook._submit_claimed_task(Path("synthetic"), {}, slot_acquired=True)
    assert webhook._webhook_slots.acquire(blocking=False)
    webhook._webhook_slots.release()


@pytest.mark.parametrize("target", ["quotes", "quant", "pending_dashboard", "direct_refresh", "maintenance"])
def test_producer_or_derived_writer_refuses_frozen_before_business(background, monkeypatch, target):
    fence, _webhook = background
    if target == "quotes":
        import routes.dashboard_routes as module
        monkeypatch.setattr(module, "_refresh_quotes_admitted", lambda: pytest.fail("quote write"))
        assert module._refresh_quotes_cache() is False
    elif target == "quant":
        import services.quant_market_context as module
        monkeypatch.setattr(module, "_poll_quant_context_admitted", lambda: pytest.fail("quant write"))
        assert module.poll_quant_market_context_once() is False
    elif target == "pending_dashboard":
        import services.dashboard_snapshot_updates as module
        monkeypatch.setattr(module, "snapshots_enabled", lambda: True)
        monkeypatch.setattr(module, "realtime_revision_status", lambda: {"input": 2, "published": 0, "published_at": 0})
        monkeypatch.setattr(module, "_enqueue_pending_realtime_admitted", lambda: pytest.fail("enqueue"))
        with pytest.raises(admission.AdmissionClosed):
            module.enqueue_pending_realtime_snapshot()
    elif target == "direct_refresh":
        import services.refresh as module
        with pytest.raises(admission.AdmissionClosed):
            module.run_refresh_command()
    else:
        source = Path(__file__).resolve().parents[1] / "scripts/run_db_maintenance.py"
        spec = importlib.util.spec_from_file_location("maintenance_fence_test", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "_database_maintenance_admitted", lambda *_a, **_k: pytest.fail("maintenance write"))
        with pytest.raises(admission.AdmissionClosed):
            module.database_maintenance("synthetic.sqlite", "test-run")
    assert fence.snapshot()["active"] == 0


def test_idle_dispatch_and_clean_publication_do_not_consume_lease_capacity(background, monkeypatch):
    fence, webhook = background
    import services.dashboard_snapshot_updates as publication

    fence.open_trial("1" * 64, 0, "synthetic opening")
    monkeypatch.setattr(publication, "snapshots_enabled", lambda: True)
    monkeypatch.setattr(publication, "realtime_revision_status", lambda: {"input": 1, "published": 1, "published_at": 0})
    for _ in range(12):
        assert webhook._dispatch_spooled_tasks_once() == 0
        assert publication.enqueue_pending_realtime_snapshot() == "clean"
    assert list((fence.root / "leases").iterdir()) == []
    assert not webhook._webhook_spool_dir.exists()


def test_read_only_hint_includes_unowned_inflight_and_archive_pending(background):
    _fence, webhook = background
    inflight = webhook._webhook_spool_dir / webhook._SPOOL_INFLIGHT_DIR
    inflight.mkdir(parents=True)
    owned = "d" * 64
    (inflight / (owned + ".json")).write_text("synthetic")
    webhook._webhook_claims[owned] = (inflight / (owned + ".json"), "synthetic-owner")
    assert webhook._spool_may_need_activity() is False
    (inflight / ("e" * 64 + ".json")).write_text("synthetic")
    assert webhook._spool_may_need_activity() is True
    (inflight / ("e" * 64 + ".json")).unlink()
    archive = webhook._webhook_spool_dir / webhook._SPOOL_DLQ_DIR
    archive.mkdir()
    (archive / (owned + ".1.terminal-pending.json")).write_text("synthetic")
    assert webhook._spool_may_need_activity() is True


def test_native_profile_keeps_strict_limits_and_stacked_branch_full_ci():
    root = Path(__file__).resolve().parents[1]
    source = root / "scripts/verify_admission_fence_native.py"
    spec = importlib.util.spec_from_file_location("admission_native_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    base = module.runner()
    assert base.MEMORY == 768 * 1024 * 1024 and base.PIDS == 256
    assert "test_admission_native.py" in module.CONTAINER_TEST
    assert "test_admission_fence_control.py" in module.CONTAINER_TEST
    assert "memory.events" in module.CONTAINER_TEST
    assert "services/admission_fence.py" in module.FILES
    assert "'codex/**'" in (root / ".github/workflows/ci.yml").read_text()
    workflow = (root / ".github/workflows/admission-fence.yml").read_text()
    assert "--repo ." in workflow and "if: always()" in workflow
