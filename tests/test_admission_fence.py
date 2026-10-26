"""Actual private files/flock/processes; no application/model/production access."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from services import admission_fence as fence


def identity():
    return {"revision": "1" * 40, "image_id": "sha256:" + "2" * 64,
            "strategy": "synthetic", "source_sha256": fence.source_sha256(), "deployment_nonce": "3" * 32}


def policy(**changes):
    now = int(time.time())
    return {"not_before": now - 1, "expires_at": now + 60, "max_admissions": 100,
            "max_bytes": 10000, **changes}


@pytest.fixture
def installed(tmp_path):
    root, binding = tmp_path / "fence", identity()
    fence.provision(root, binding, policy())
    value = fence.Fence(root, binding, lock_seconds=0.05)
    value.open_trial("1" * 64, 0, "approved isolated trial")
    return value


def environment(monkeypatch, value):
    monkeypatch.setenv("AI_FILTER_ADMISSION_MODE", "enforced")
    monkeypatch.setenv("AI_FILTER_ADMISSION_ROOT", str(value.root))
    monkeypatch.setenv("AI_FILTER_ADMISSION_BINDING", json.dumps(value.binding))


def test_initial_frozen_requires_explicit_current_finite_open(tmp_path):
    root, binding = tmp_path / "fence", identity()
    receipt = fence.provision(root, binding, policy())
    assert receipt["status"] == "provisioned_frozen" and not receipt["switch_authorized"]
    value = fence.Fence(root, binding)
    with pytest.raises(fence.AdmissionClosed):
        value.reserve_lease("http")
    assert value.snapshot()["phase"] == "frozen"
    assert value.open_trial("1" * 64, 0, "approved")["phase"] == "open"
    with pytest.raises(fence.AdmissionClosed):
        value.open_trial("2" * 64, 1, "cannot reset")


def test_normal_two_stage_keeps_drain_open_and_never_claims_queue_zero(installed):
    lease = installed.reserve_lease("http", charge_bytes=20)
    assert installed.stop_new("2" * 64, 1, "pause")["phase"] == "draining"
    with pytest.raises(fence.AdmissionClosed):
        installed.reserve_lease("late_http")
    consumer = installed.reserve_lease("persisted_queue", "drain")
    consumer.finish()
    lease.finish()
    receipt = installed.freeze("3" * 64, 2, "freeze")
    assert receipt["phase"] == "frozen" and receipt["activity_drained"]
    assert receipt["queue_checked"] is False and receipt["switch_authorized"] is False
    with pytest.raises(fence.AdmissionClosed):
        installed.reserve_lease("consumer", "drain")
    assert installed.query_operation("3" * 64) == receipt


def test_lost_ack_query_exact_op_does_not_reissue_or_mutate_receipt(installed):
    first = installed.query_operation("1" * 64)
    lease = installed.reserve_lease("http")
    # Original opening ACK is immutable even though activity is now running.
    assert installed.query_operation("1" * 64) == first
    lease.finish()
    original = (installed.root / "operations" / ("1" * 64 + ".json")).read_bytes()
    with pytest.raises(fence.AdmissionUncertain):
        installed.open_trial("1" * 64, 0, "approved isolated trial")
    assert (installed.root / "operations" / ("1" * 64 + ".json")).read_bytes() == original


def test_freeze_timeout_leaves_closing_intent_then_query_after_completion(installed):
    active = installed.reserve_lease("long_request")
    installed.stop_new("2" * 64, 1, "pause")
    with pytest.raises(fence.AdmissionBusy):
        installed.freeze("3" * 64, 2, "freeze", wait_seconds=0.01)
    assert installed.snapshot()["phase"] == "freezing"
    with pytest.raises(fence.AdmissionClosed):
        installed.reserve_lease("cannot_slip", "drain")
    assert not (installed.root / "operations" / ("3" * 64 + ".json")).exists()
    active.finish()
    assert installed.query_operation("3" * 64)["phase"] == "frozen"


def test_concurrent_capacity_reservation_is_atomic(tmp_path):
    root, binding = tmp_path / "fence", identity()
    fence.provision(root, binding, policy(max_admissions=4, max_bytes=40))
    gate = fence.Fence(root, binding)
    gate.open_trial("1" * 64, 0, "approved")
    def attempt(_):
        try:
            lease = fence.Fence(root, binding).reserve_lease("concurrent", charge_bytes=10)
            lease.finish()
            return True
        except fence.AdmissionClosed:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(attempt, range(12))).count(True) == 4
    assert gate.snapshot()["intake_count"] == 4
    assert gate.snapshot()["charged_bytes"] == 40
    # Durable tasks may drain after intake quota is exhausted, not counted twice.
    gate.reserve_lease("durable", "drain").finish()
    assert gate.snapshot()["intake_count"] == 4


def test_expiry_does_not_stop_existing_durable_consumers(installed, monkeypatch):
    deadline = fence._read(installed.root / "manifest.json")["policy"]["expires_at"]
    monkeypatch.setattr(fence.time, "time", lambda: deadline)
    with pytest.raises(fence.AdmissionClosed):
        installed.reserve_lease("http")
    installed.reserve_lease("durable", "drain").finish()


def test_clock_rollback_rejects_new_intake(installed, monkeypatch):
    first = installed.reserve_lease("http")
    first.finish()
    monkeypatch.setattr(fence.time, "time", lambda: first.row["created_at"] - 0.01)
    with pytest.raises(fence.AdmissionClosed):
        installed.reserve_lease("rollback")


def test_elapsed_deadline_cannot_extend_by_rollback_after_last_intake(installed, monkeypatch):
    first = installed.reserve_lease("http")
    first.finish()
    opening = installed.snapshot()["opening_clock"]
    deadline = fence._read(installed.root / "manifest.json")["policy"]["expires_at"]
    # Wall remains after the last accepted request and before expiry, while
    # the shared elapsed clock proves the actual finite window has elapsed.
    monkeypatch.setattr(fence.time, "time", lambda: first.row["created_at"] + 0.01)
    monkeypatch.setattr(fence, "_elapsed", lambda: opening["elapsed"] + deadline - opening["wall"] + 1)
    restarted = fence.Fence(installed.root, installed.binding)
    with pytest.raises(fence.AdmissionClosed):
        restarted.reserve_lease("rollback_loophole")
    assert restarted.snapshot()["opening_clock"] == opening


def test_different_host_boot_and_expired_open_fail_closed(installed, tmp_path, monkeypatch):
    with monkeypatch.context() as scoped:
        scoped.setattr(fence, "_boot_identity", lambda: "synthetic-different-boot")
        with pytest.raises(fence.AdmissionClosed):
            fence.Fence(installed.root, installed.binding)
    root = tmp_path / "expired"
    now = int(time.time())
    fence.provision(root, identity(), policy(not_before=now - 20, expires_at=now - 1))
    value = fence.Fence(root, identity())
    with pytest.raises(fence.AdmissionClosed):
        value.open_trial("1" * 64, 0, "expired")
    assert value.snapshot()["phase"] == "frozen"


def test_total_lease_evidence_limit_also_counts_idle_and_drain(installed, monkeypatch):
    monkeypatch.setattr(fence, "MAX_LEASES", 2)
    installed.reserve_lease("idle_renew", "drain").finish()
    installed.reserve_lease("idle_claim", "drain").finish()
    assert installed.snapshot()["intake_count"] == 0
    for kind in ("drain", "intake"):
        with pytest.raises(fence.AdmissionClosed):
            installed.reserve_lease("after_evidence_full", kind)
    assert len(list((installed.root / "leases").glob("*.json"))) == 2
    assert len(list((installed.root / "finishes").glob("*.json"))) == 2


def test_transfer_lease_across_serial_threads_and_no_concurrent_activate(installed):
    lease = installed.reserve_lease("claimed", "drain")
    entered, release = threading.Event(), threading.Event()
    def work():
        with lease.activate():
            assert fence.current_kind() == "drain"
            entered.set()
            assert release.wait(3)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(work)
        assert entered.wait(3)
        with pytest.raises(fence.AdmissionClosed):
            with lease.activate():
                pass
        with pytest.raises(fence.AdmissionClosed):
            lease.finish()
        release.set()
        future.result(timeout=3)
    with lease.activate():
        assert fence.current_kind() == "drain"
    lease.finish()
    lease.finish()  # Exact already-confirmed outcome only.
    with pytest.raises(fence.AdmissionClosed):
        lease.finish(False)


def test_activate_rechecks_after_finish_and_freeze_wins_before_lock(installed, monkeypatch):
    lease = installed.reserve_lease("claimed", "drain")
    checked, resume = threading.Event(), threading.Event()
    original = fence._require
    def pause_after_fast_check(condition, message):
        original(condition, message)
        if message == "lease ended or inherited after fork" and not checked.is_set():
            checked.set()
            assert resume.wait(3)
    monkeypatch.setattr(fence, "_require", pause_after_fast_check)
    entered = []
    def attempt():
        with pytest.raises(fence.AdmissionClosed):
            with lease.activate():
                entered.append(True)
        return fence.current_kind()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(attempt)
        try:
            assert checked.wait(3)
            lease.finish()
            installed.stop_new("2" * 64, 1, "stop")
            assert installed.freeze("3" * 64, 2, "freeze")["activity_drained"]
        finally:
            resume.set()
        assert future.result(timeout=3) == "intake"  # No stale thread-local lease.
    assert entered == []
    assert lease.activation.acquire(blocking=False)  # Failed check releases lock.
    lease.activation.release()


@pytest.mark.parametrize("first_outcome,second_outcome", [(True, True), (True, False), (False, False), (False, True), (None, True)])
def test_finish_rechecks_after_another_finisher_wins(installed, monkeypatch, first_outcome, second_outcome):
    lease = installed.reserve_lease("claimed", "drain")
    checked, resume = threading.Event(), threading.Event()
    original_lock, main_thread = lease.activation, threading.get_ident()
    class DelayedLock:
        def acquire(self, blocking=False):
            if threading.get_ident() != main_thread:
                checked.set()
                assert resume.wait(3)
            return original_lock.acquire(blocking=blocking)
        def release(self):
            original_lock.release()
    lease.activation = DelayedLock()
    def attempt():
        try:
            lease.finish(second_outcome)
            return "confirmed"
        except fence.AdmissionClosed:
            return "rejected"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(attempt)
        try:
            assert checked.wait(3)
            if first_outcome is None:
                with monkeypatch.context() as scoped:
                    scoped.setattr(fence, "_write", lambda *args: (_ for _ in ()).throw(OSError("EIO")))
                    with pytest.raises(OSError):
                        lease.finish(True)
            else:
                lease.finish(first_outcome)
            before = installed.snapshot()
        finally:
            resume.set()
        expected = "confirmed" if first_outcome is not None and first_outcome == second_outcome else "rejected"
        assert future.result(timeout=3) == expected
    assert installed.snapshot() == before
    assert lease.outcome is first_outcome
    assert original_lock.acquire(blocking=False)
    original_lock.release()


def test_nested_inheritance_does_not_recharge_or_bypass_draining(installed, monkeypatch):
    environment(monkeypatch, installed)
    with fence.admission("request", charge_bytes=11):
        with fence.admission("inner", "inherit"):
            with fence.admission("dispatch", "drain"):
                assert fence.current_kind() == "intake"
        assert installed.snapshot()["intake_count"] == 1
    installed.stop_new("2" * 64, 1, "pause")
    with fence.admission("persisted", "drain"):
        with fence.admission("publication", "inherit"):
            assert fence.current_kind() == "drain"
        with pytest.raises(fence.AdmissionClosed):
            with fence.admission("new_http", "intake"):
                pass
    assert installed.snapshot()["charged_bytes"] == 11


def test_guarded_preserves_metadata_and_context(installed, monkeypatch):
    environment(monkeypatch, installed)
    @fence.guarded("derived", kind="inherit")
    def actual(value):
        return value, fence.current_kind()
    assert actual.__name__ == "actual"
    assert actual(2) == (2, "intake")
    with fence.admission("consumer", "drain"):
        assert actual(3) == (3, "drain")


def test_exception_retains_uncertain_and_freeze_cannot_hide_it(installed, monkeypatch):
    environment(monkeypatch, installed)
    with pytest.raises(ValueError, match="business failed"):
        with fence.admission("request"):
            raise ValueError("business failed")
    assert installed.snapshot()["uncertain"] == 1
    with pytest.raises(fence.AdmissionUncertain):
        installed.reserve_lease("after_error")
    installed.stop_new("2" * 64, 1, "pause")
    with pytest.raises(fence.AdmissionUncertain):
        installed.freeze("3" * 64, 2, "freeze")


def test_real_child_crash_preserves_orphan_and_restart_failclosed(installed):
    source = "from services.admission_fence import Fence;import sys,json,os;f=Fence(sys.argv[1],json.loads(sys.argv[2]));f.reserve_lease('crashed','drain');os._exit(0)"
    result = subprocess.run([sys.executable, "-B", "-c", source, str(installed.root), json.dumps(installed.binding)],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    restarted = fence.Fence(installed.root, installed.binding)
    assert restarted.snapshot()["orphaned"] == 1
    with pytest.raises(fence.AdmissionUncertain):
        restarted.reserve_lease("after_restart")
    restarted.stop_new("2" * 64, 1, "pause")
    with pytest.raises(fence.AdmissionUncertain):
        restarted.freeze("3" * 64, 2, "freeze")
    assert len(list((installed.root / "leases").glob("*.json"))) == 1


def test_real_process_active_lock_waits_without_killing(installed):
    source = "from services.admission_fence import Fence;import sys,json,time;f=Fence(sys.argv[1],json.loads(sys.argv[2]));l=f.reserve_lease('child','drain');print('active',flush=True);time.sleep(.3);l.finish()"
    child = subprocess.Popen([sys.executable, "-B", "-c", source, str(installed.root), json.dumps(installed.binding)],
                             cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "active"
        installed.stop_new("2" * 64, 1, "pause")
        with pytest.raises(fence.AdmissionBusy):
            installed.freeze("3" * 64, 2, "freeze", wait_seconds=0.01)
        assert child.poll() is None
        assert child.wait(timeout=5) == 0
        assert installed.query_operation("3" * 64)["activity_drained"]
    finally:
        child.wait(timeout=5)
        child.stdout.close()
        child.stderr.close()


@pytest.mark.parametrize("cutpoint", ["intent", "state", "receipt"])
def test_partial_stop_cutpoints_only_exact_query_recovers(installed, monkeypatch, cutpoint):
    original = fence._write
    def fail(path, value):
        match = ((cutpoint == "intent" and path.name.endswith(".intent.json"))
                 or (cutpoint == "state" and path.name == "state.json")
                 or (cutpoint == "receipt" and path.parent.name == "operations" and not path.name.endswith(".intent.json")))
        if match:
            original(path, value)
            raise OSError("synthetic lost durability confirmation")
        return original(path, value)
    with monkeypatch.context() as scoped:
        scoped.setattr(fence, "_write", fail)
        with pytest.raises(OSError):
            installed.stop_new("2" * 64, 1, "pause")
    if cutpoint != "receipt":
        with pytest.raises(fence.AdmissionClosed):
            installed.reserve_lease("must_not_slip")
    assert installed.query_operation("2" * 64)["phase"] == "draining"
    with pytest.raises(fence.AdmissionClosed):
        installed.stop_new("4" * 64, 1, "different")


def test_finish_sync_failure_keeps_orphan_evidence_not_success(installed, monkeypatch):
    lease = installed.reserve_lease("work")
    original = fence._sync_directory
    def fail(path):
        if path.name == "finishes":
            raise OSError("EIO")
        return original(path)
    with monkeypatch.context() as scoped:
        scoped.setattr(fence, "_sync_directory", fail)
        with pytest.raises(OSError):
            lease.finish()
    assert lease.outcome is None
    with pytest.raises(fence.AdmissionClosed):
        lease.finish()
    # A visible but unconfirmed completion cannot be mistaken for durable success.
    installed.stop_new("2" * 64, 1, "pause")
    # Final freeze must re-sync completion before it can acknowledge it.
    receipt = installed.freeze("3" * 64, 2, "freeze")
    assert receipt["activity_drained"]


def test_disk_full_initial_reservation_never_allows_work(installed, monkeypatch):
    original = fence._write
    def full(path, value):
        if path.parent.name == "leases":
            raise OSError(28, "synthetic ENOSPC")
        return original(path, value)
    monkeypatch.setattr(fence, "_write", full)
    with pytest.raises(OSError):
        installed.reserve_lease("full")
    assert installed.snapshot()["intake_count"] == 0


@pytest.mark.parametrize("damage", ["symlink", "mode", "pending", "duplicate_json"])
def test_unsafe_and_partial_storage_fail_closed(installed, damage):
    path = installed.root / "state.json"
    if damage == "symlink":
        original = path.read_bytes()
        path.unlink()
        target = installed.root / "state_original"
        target.write_bytes(original)
        target.chmod(0o600)
        path.symlink_to(target)
    elif damage == "mode":
        path.chmod(0o644)
    elif damage == "pending":
        pending = installed.root / "operations" / ("2" * 64 + ".intent.json.pending")
        pending.write_bytes(b'{"partial":')
        pending.chmod(0o600)
    else:
        path.write_bytes(b'{"schema":1,"schema":1}')
    with pytest.raises((fence.AdmissionClosed, OSError, ValueError, KeyError)):
        installed.reserve_lease("unsafe")


def test_disabled_never_opens_files_and_unknown_config_never_disables(monkeypatch):
    monkeypatch.delenv("AI_FILTER_ADMISSION_MODE", raising=False)
    monkeypatch.setattr(fence, "Fence", lambda *a, **k: pytest.fail("disabled touched fence"))
    with fence.admission("legacy"):
        assert fence.current_kind() == "intake"
    with fence.admission("legacy_consumer", "drain"):
        with fence.admission("legacy_nested_intake", "intake"):
            assert fence.current_kind() == "drain"  # Disabled preserves legacy behavior.
        monkeypatch.setenv("AI_FILTER_ADMISSION_MODE", "typo")
        with pytest.raises(fence.AdmissionClosed):
            with fence.admission("cannot_hide_bad_mode"):
                pass
        monkeypatch.delenv("AI_FILTER_ADMISSION_MODE", raising=False)
    monkeypatch.setenv("AI_FILTER_ADMISSION_MODE", "typo")
    with pytest.raises(fence.AdmissionClosed):
        with fence.admission("bad"):
            pass
    monkeypatch.setenv("AI_FILTER_ADMISSION_MODE", "enforced")
    monkeypatch.setenv("AI_FILTER_ADMISSION_BINDING", "invalid")
    with pytest.raises(fence.AdmissionClosed):
        with fence.admission("missing"):
            pass


@pytest.mark.parametrize("change", [{"generation": True}, {"generation": -1}, {"phase": "other"}, {"pending_operation": True}])
def test_corrupt_generation_state_never_recovers_by_guess(installed, change):
    path = installed.root / "state.json"
    value = {**fence._read(path), **change}
    fence._write(path, value)
    before = path.read_bytes()
    with pytest.raises(fence.AdmissionClosed):
        installed.reserve_lease("badstate")
    assert path.read_bytes() == before


def test_identity_binding_and_wait_bounds_are_strict(installed):
    for field, value in (("deployment_nonce", "4" * 32), ("image_id", "sha256:" + "5" * 64), ("source_sha256", "6" * 64)):
        with pytest.raises(fence.AdmissionClosed):
            fence.Fence(installed.root, {**installed.binding, field: value})
    for value in (True, -1, float("nan"), float("inf"), 31):
        with pytest.raises(fence.AdmissionClosed):
            installed.freeze("3" * 64, 2, "bound", wait_seconds=value)
    assert fence.source_sha256() == hashlib.sha256(Path(fence.__file__).read_bytes()).hexdigest()
