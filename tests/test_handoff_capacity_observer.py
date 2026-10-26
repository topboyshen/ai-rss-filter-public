"""Real private files and isolation admission gates; no models, Docker or production.

PostgreSQL cursor tests verify protocol only, NOT a real PostgreSQL execution.
The separate isolated PG job invokes sample_postgres against actual PostgreSQL.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import observe_handoff_capacity as observer  # noqa: E402
import production_handoff_adapter as gate  # noqa: E402


def sample(identity, now=100.0, admitted=0):
    return {"identity": identity, "generation": 0, "observed_epoch": now, "admission_total": admitted,
            "receiver": {"records": 0, "reserved_bytes": 0, "physical_free_bytes": 10**9,
                         "states": {"accepted": 0, "completed": 0, "terminal": 0}},
            "sender": observer._postgres_counts([])}


def policy(identity, manifest_hash="a" * 64):
    return {"schema": 1, "run_id": "b" * 32, "identity": identity,
            "adapter_manifest_sha256": manifest_hash, "generation": 0,
            "baseline_admission_total": 0,
            "duration_seconds": 60, "interval_seconds": 1, "max_checks": 3,
            "max_new_deliveries": 2, "receiver_stop_records": 100, "sender_stop_rows": 100,
            "receiver_stop_bytes": 200 * 1024**2, "sender_stop_bytes": 200 * 1024**2,
            "sampling_seconds": 1, "stop_seconds": 1,
            "minimum_free_bytes": observer.receipt.MAX_ALIAS_RESERVATION}


class Backend:
    """Protocol fault injection only; actual gate tests below do not mock ACKs."""

    def __init__(self, config):
        self.config = config
        self.value = sample(config["identity"])
        self.stops = self.queries = self.snapshots = 0
        self.receipt = None
        self.fault = None

    def snapshot(self, identity, generation):
        self.snapshots += 1
        if isinstance(self.value, Exception):
            raise self.value
        return copy.deepcopy(self.value)

    def stop(self, operation_id, reason, generation):
        self.stops += 1
        self.receipt = {"operation_id": operation_id, "reason": reason,
                        "identity": self.config["identity"], "previous_generation": generation,
                        "generation": generation + 1, "state": "closed", "durable": True,
                        "manifest_sha256": self.config["adapter_manifest_sha256"]}
        if self.fault:
            return self.fault(self.receipt)
        return self.receipt

    def query_operation(self, operation_id):
        self.queries += 1
        return self.receipt


class CapacityObserverTests(unittest.TestCase):
    def setUp(self):
        evidence = os.environ.get("AI_FILTER_HANDOFF_EVIDENCE_DIR")
        if evidence:
            Path(evidence).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix=self._testMethodName + "-", dir=evidence))
        self.identity = {"scope": "isolation", "revision": "synthetic"}
        self.config = policy(self.identity)
        self.backend = Backend(self.config)
        self.now = 100.0
        self.mono = 1000.0
        self.controllers = []

    def tearDown(self):
        for controller in self.controllers:
            controller.close()
        # Preserve all original journals, receipts and failed-state evidence.

    def controller(self, backend=None, config=None, name="journal", clock=None, monotonic=None):
        value = observer.CapacityObserver(config or self.config, backend or self.backend,
                                          self.root / name, clock=clock or (lambda: self.now),
                                          monotonic=monotonic or (lambda: self.mono))
        self.controllers.append(value)
        return value

    def spool(self):
        path = self.root / "spool"
        path.mkdir(mode=0o700)
        fd = os.open(path / ".lock", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        store = observer.receipt.ReceiptStore(path)
        record, _ = store.accept(b"synthetic-exact-body", {"entries": []},
                                 strategy="synthetic", revision="synthetic", accepted_at=100.0)
        return path, store, record

    def actual_gate(self):
        identity = {"scope": "isolation", "run_id": "b" * 32,
                    "adapter_sha256": hashlib.sha256((SCRIPTS / "production_handoff_adapter.py").read_bytes()).hexdigest()}
        manifest = {"mode": "isolation", "run_id": "b" * 32,
                    "identity": identity, "entrances": list(gate.ENTRANCES)}
        path = self.root / "gate"
        gate.init_isolation(path, manifest)
        backend = gate.ProductionHandoffAdapter(path, manifest, mode="isolation", lock_seconds=0.05)
        return backend, policy(identity, observer.digest(manifest))

    def test_actual_receipt_primary_alias_and_terminal_charge_without_writes(self):
        path, store, primary = self.spool()
        alias, _ = store.accept(b"other-body-same-task", {"entries": []},
                               strategy="synthetic", revision="synthetic", accepted_at=101.0)
        store.update_status(primary["delivery_id"], status="terminal", completion={"reason": "synthetic"})
        originals = {p: p.read_bytes() for p in path.rglob("*") if p.is_file()}
        with patch.object(os, "fsync", side_effect=AssertionError("read-only sampler wrote")):
            result = observer.sample_receiver(path, observer.spool_identity(path))
        self.assertEqual(result["records"], 2)
        self.assertEqual(result["reserved_bytes"], primary["_reserved_bytes"] + alias["_reserved_bytes"])
        self.assertEqual(result["states"], {"accepted": 1, "completed": 0, "terminal": 1})
        self.assertTrue(all(p.read_bytes() == value for p, value in originals.items()))

    def test_pending_original_is_preserved_and_not_recovered(self):
        path, _, record = self.spool()
        pending = path / "receipts" / (record["delivery_id"] + ".json.pending")
        observer.journal_module.durable_write(pending, {"partial": "synthetic"})
        original = pending.read_bytes()
        with self.assertRaises(observer.ObservationError):
            observer.sample_receiver(path, observer.spool_identity(path))
        self.assertEqual(pending.read_bytes(), original)

    def test_receiver_identity_symlink_permissions_and_missing_index_fail_closed(self):
        path, _, record = self.spool()
        expected = observer.spool_identity(path)
        with self.assertRaises(observer.ObservationError):
            observer.sample_receiver(path, {**expected, "inode": expected["inode"] + 1})
        index = path / "receipts" / "tasks" / (record["task_digest"] + ".json")
        retained = index.with_suffix(".retained")
        index.rename(retained)
        with self.assertRaises(observer.ObservationError):
            observer.sample_receiver(path, expected)
        retained.rename(index)
        index.chmod(0o644)
        with self.assertRaises(observer.receipt.ReceiptCorrupt):
            observer.sample_receiver(path, expected)

    def test_receiver_lock_contention_is_not_empty_success(self):
        path, _, _ = self.spool()
        fd = os.open(path / ".lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(observer.ObservationError):
                observer.sample_receiver(path, observer.spool_identity(path))
        finally:
            os.close(fd)

    def test_receiver_scan_file_and_byte_budgets(self):
        path, _, _ = self.spool()
        for field in ("MAX_SCAN_FILES", "MAX_SCAN_BYTES"):
            with self.subTest(field=field), patch.object(observer, field, 1), self.assertRaises(observer.ObservationError):
                observer.sample_receiver(path, observer.spool_identity(path))

    def test_all_hard_limits_cannot_be_increased_or_bool_float(self):
        for key, bad in (("receiver_stop_records", 4097), ("sender_stop_rows", 5001),
                         ("receiver_stop_bytes", observer.CAPACITY_BYTES + 1),
                         ("sender_stop_bytes", observer.CAPACITY_BYTES + 1),
                         ("duration_seconds", 7201), ("max_checks", 49),
                         ("generation", True), ("sampling_seconds", 1.5),
                         ("minimum_free_bytes", observer.receipt.MAX_ALIAS_RESERVATION - 1)):
            with self.subTest(key=key), self.assertRaises(observer.ObservationError):
                observer.validate_policy({**self.config, key: bad})

    def test_manifest_binding_is_independent_not_self_referential(self):
        self.assertNotIn("manifest_sha256", self.config["identity"])
        for bad in (None, "wrong", True):
            with self.subTest(bad=bad), self.assertRaises(observer.ObservationError):
                observer.validate_policy({**self.config, "adapter_manifest_sha256": bad})

    def test_normal_sample_then_admission_count_stop_once(self):
        item = self.controller()
        self.assertEqual(item.tick()["status"], "observing")
        self.backend.value["admission_total"] = 2
        self.assertEqual(item.tick()["status"], "stop_confirmed")
        self.assertEqual(item.tick()["status"], "stop_confirmed")
        self.assertEqual((self.backend.stops, self.backend.queries), (1, 0))

    def test_first_sample_cannot_hide_arrivals_since_frozen_pre_admission_baseline(self):
        item = self.controller()
        self.backend.value["admission_total"] = 2
        result = item.tick()
        self.assertEqual(result["reason"], "trial_delivery_limit")
        self.assertEqual(item.state["baseline_admission_total"], 0)

    def test_time_limit_and_restart_do_not_reset_trial(self):
        item = self.controller()
        item.tick()
        item.close()
        self.controllers.remove(item)
        self.now = 160.0
        restarted = self.controller()
        self.assertEqual(restarted.tick()["status"], "stop_confirmed")
        self.assertEqual(self.backend.snapshots, 1)

    def test_initializer_latest_error_releases_lock_for_same_process_reopen(self):
        with patch.object(observer.journal_module.Journal, "latest", side_effect=OSError("synthetic read failure")), \
                self.assertRaises(OSError):
            self.controller()
        self.assertEqual(self.controller().tick()["status"], "observing")

    def test_initializer_policy_mismatch_releases_lock_without_changing_original(self):
        original = self.controller()
        before = original.state.copy()
        original.close()
        self.controllers.remove(original)
        with self.assertRaises(observer.ObservationError):
            self.controller(config={**self.config, "duration_seconds": 30})
        restored = self.controller()
        self.assertEqual(restored.state, before)
        self.assertEqual(restored.tick()["status"], "observing")

    def test_recovered_stopped_revalidates_saved_receipt_without_new_io_requests(self):
        item = self.controller()
        self.now = 160.0
        item.tick()
        original = {path: path.read_bytes() for path in item.journal.records.iterdir()}
        item.close()
        self.controllers.remove(item)
        restored = self.controller()
        result = restored.tick()
        self.assertEqual(result["status"], "stop_confirmed")
        self.assertTrue(result["reused"])
        self.assertEqual((self.backend.stops, self.backend.queries), (1, 0))
        self.assertEqual({path: path.read_bytes() for path in restored.journal.records.iterdir()}, original)

    def test_corrupt_stopped_receipt_preserved_and_never_reconfirmed_or_resent(self):
        changes = [{"durable": False}, {"durable": 1}, {"operation_id": "f" * 64},
                   {"reason": "other-reason"}, {"identity": {}}, {"previous_generation": False},
                   {"generation": True}, {"generation": 2}, {"manifest_sha256": "f" * 64}, {"state": "open"}]
        for index, change in enumerate(changes):
            backend = Backend(self.config)
            item = self.controller(backend, name=f"bad-receipt-{index}")
            self.now = 160.0
            item.tick()
            item._save(stop_receipt={**item.state["stop_receipt"], **change})
            root = item.journal.root
            original = {path: path.read_bytes() for path in item.journal.records.iterdir()}
            item.close()
            self.controllers.remove(item)
            with self.subTest(change=change), self.assertRaises(observer.ObservationError):
                self.controller(backend, name=root.name)
            self.assertEqual((backend.stops, backend.queries), (1, 0))
            self.assertEqual({path: path.read_bytes() for path in (root / "records").iterdir()}, original)
            # Initialization failure released the lock even though the bad evidence remains.
            check = observer.journal_module.Journal(root)
            check.close()
            self.now = 100.0

    def test_saved_phase_intent_counter_baseline_and_time_budget_are_not_trusted(self):
        changes = [{"phase": "stopped", "intent": {"operation_id": "f" * 64, "reason": "synthetic"},
                    "stop_receipt": {"durable": False}},
                   {"phase": "stopped", "intent": None}, {"checks": True}, {"checks": 4},
                   {"baseline_admission_total": 1}, {"expires_epoch": 161.0}, {"phase": "invented"}]
        for index, change in enumerate(changes):
            backend = Backend(self.config)
            item = self.controller(backend, name=f"bad-state-{index}")
            item._save(**change)
            root = item.journal.root
            original = {path: path.read_bytes() for path in item.journal.records.iterdir()}
            item.close()
            self.controllers.remove(item)
            with self.subTest(change=change), self.assertRaises(observer.ObservationError):
                self.controller(backend, name=root.name)
            self.assertEqual((backend.stops, backend.queries, backend.snapshots), (0, 0, 0))
            self.assertEqual({path: path.read_bytes() for path in (root / "records").iterdir()}, original)

    def test_saved_intent_opid_recomputed_even_when_ack_matches_tampered_intent(self):
        item = self.controller()
        self.now = 160.0
        item.tick()
        item._save(intent={**item.state["intent"], "operation_id": "f" * 64},
                   stop_receipt={**item.state["stop_receipt"], "operation_id": "f" * 64})
        with self.assertRaises(observer.ObservationError):
            item.tick()
        self.assertEqual((self.backend.stops, self.backend.queries), (1, 0))

    def test_monotonic_elapsed_limit_survives_wall_clock_remaining_above_start(self):
        item = self.controller()
        self.now, self.mono = 130.0, 1030.0
        self.backend.value["observed_epoch"] = 130.0
        self.assertEqual(item.tick()["status"], "observing")
        # Wall remains >= start and < persisted expiry, but real monotonic budget is gone.
        self.now, self.mono = 130.0, 1060.0
        self.assertEqual(item.tick()["status"], "stop_confirmed")
        self.assertEqual(self.backend.snapshots, 1)

    def test_wall_clock_reversal_above_original_start_stops_before_monotonic_limit(self):
        item = self.controller()
        self.now, self.mono = 140.0, 1040.0
        self.backend.value["observed_epoch"] = 140.0
        self.assertEqual(item.tick()["status"], "observing")
        self.now, self.mono = 130.0, 1041.0
        self.assertEqual(item.tick()["status"], "stop_confirmed")
        self.assertEqual(self.backend.snapshots, 1)

    def test_clock_reversal_since_deadline_initialization_stops_before_first_sample(self):
        times = iter((100.0, 110.0, 109.0))
        item = self.controller(clock=lambda: next(times))
        self.assertEqual(item.tick()["status"], "stop_confirmed")
        self.assertEqual(self.backend.snapshots, 0)

    def test_wall_forward_limit_and_restart_remaining_use_shorter_deadline(self):
        item = self.controller()
        self.now, self.mono = 159.0, 1001.0
        self.backend.value["observed_epoch"] = 159.0
        self.assertEqual(item.tick()["status"], "observing")
        item.close()
        self.controllers.remove(item)
        # A new monotonic origin cannot grant another 60 seconds: UTC has 1 second left.
        self.mono = 5000.0
        restarted = self.controller()
        self.mono = 5001.0
        self.assertEqual(restarted.tick()["status"], "stop_confirmed")
        self.assertEqual(self.backend.snapshots, 1)

    def test_unknown_or_backwards_monotonic_requests_stop(self):
        for index, bad in enumerate((float("nan"), 999.0, None)):
            item = self.controller(name=f"mono{index}")
            self.mono = bad
            self.assertEqual(item.tick()["status"], "stop_confirmed")
            self.mono = 1000.0

    def test_clock_reversal_across_restart_does_not_reset_known_last_observation(self):
        item = self.controller()
        self.now = 130.0
        self.backend.value["observed_epoch"] = 130.0
        item.tick()
        item.close()
        self.controllers.remove(item)
        self.now = 120.0
        restarted = self.controller()
        self.assertEqual(restarted.tick()["status"], "stop_confirmed")
        self.assertEqual(self.backend.snapshots, 1)

    def test_observation_count_limit_does_not_do_fourth_query(self):
        item = self.controller()
        for _ in range(3):
            self.assertEqual(item.tick()["status"], "observing")
        self.assertEqual(item.tick()["status"], "stop_confirmed")
        self.assertEqual(self.backend.snapshots, 3)

    def test_stale_future_nan_missing_and_identity_generation_stop(self):
        mutations = [{"observed_epoch": 98.0}, {"observed_epoch": 101.0}, {"observed_epoch": float("nan")},
                     {"observed_epoch": None}, {"identity": {}}, {"generation": True}, {"generation": 1}]
        for index, values in enumerate(mutations):
            backend = Backend(self.config)
            backend.value.update(values)
            with self.subTest(values=values):
                self.assertEqual(self.controller(backend, name=f"j{index}").tick()["status"], "stop_confirmed")
                self.assertEqual(backend.stops, 1)

    def test_decreasing_actual_counters_and_observation_times_stop(self):
        for field, start, end in (("admission_total", 2, 1), ("observed_epoch", 100.0, 99.5)):
            backend = Backend(self.config)
            backend.value[field] = start
            item = self.controller(backend, {**self.config, "max_new_deliveries": 10}, name=field)
            self.assertEqual(item.tick()["status"], "observing")
            backend.value[field] = end
            self.assertEqual(item.tick()["status"], "stop_confirmed")

    def test_each_capacity_limit_and_reservation_headroom_stops(self):
        cases = [("receiver", "records", 100), ("receiver", "reserved_bytes", 200 * 1024**2),
                 ("sender", "rows", 100), ("sender", "charged_bytes", 200 * 1024**2),
                 ("receiver", "physical_free_bytes", 0)]
        for index, (side, field, value) in enumerate(cases):
            backend = Backend(self.config)
            backend.value[side][field] = value
            if side == "sender":
                backend.value[side]["outbox_rows" if field == "rows" else "outbox_charged_bytes"] = value
            if field in {"records", "rows"}:
                backend.value[side]["states"]["accepted" if side == "receiver" else "acked"] = value
            with self.subTest(field=field):
                self.assertEqual(self.controller(backend, name=f"j{index}").tick()["status"], "stop_confirmed")
        config = {**self.config, "receiver_stop_bytes": observer.CAPACITY_BYTES}
        backend = Backend(config)
        backend.value["receiver"]["reserved_bytes"] = observer.CAPACITY_BYTES - observer.receipt.MAX_ALIAS_RESERVATION + 1
        result = self.controller(backend, config, name="headroom").tick()
        self.assertEqual(result["reason"], "receiver_alias_reservation_headroom")

    def test_held_or_expired_unacknowledged_is_not_pruned(self):
        for field in ("held", "expired"):
            backend = Backend(self.config)
            backend.value["sender"]["rows"] = 1
            backend.value["sender"]["outbox_rows"] = 1
            backend.value["sender"]["states"]["held" if field == "held" else "pending"] = 1
            backend.value["sender"]["expired_unacknowledged"] = int(field == "expired")
            with self.subTest(field=field):
                result = self.controller(backend, name=field).tick()
                self.assertEqual(result["reason"], "sender_held_or_expired_requires_disposition")

    def test_missing_state_and_excess_capacity_fail_closed(self):
        for index, mutate in enumerate((lambda value: value["sender"].pop("states"),
                                        lambda value: value["receiver"].update(records=4097),
                                        lambda value: value["sender"]["states"].update(pending=1))):
            backend = Backend(self.config)
            mutate(backend.value)
            self.assertEqual(self.controller(backend, name=f"j{index}").tick()["status"], "stop_confirmed")

    def test_sampling_error_requests_stop_and_retains_intent(self):
        self.backend.value = OSError("synthetic storage unavailable")
        item = self.controller()
        self.assertEqual(item.tick()["status"], "stop_confirmed")
        self.assertIsNotNone(item.state["intent"])

    def test_lost_ack_restart_only_queries_original_operation(self):
        def lost(_):
            raise TimeoutError("synthetic ACK transport loss")
        self.backend.value["sender"]["expired_unacknowledged"] = 1
        self.backend.fault = lost
        item = self.controller()
        self.assertEqual(item.tick()["status"], "stop_unconfirmed")
        operation_id = item.state["intent"]["operation_id"]
        item.close()
        self.controllers.remove(item)
        restarted = self.controller()
        self.assertEqual(restarted.tick()["operation_id"], operation_id)
        self.assertEqual((self.backend.stops, self.backend.queries), (1, 1))

    def test_unknown_receipt_and_ordinary_202_never_confirm_stop(self):
        for index, response in enumerate((None, {"status": 202}, {"durable": True}, {"all_producers_paused": True})):
            backend = Backend(self.config)
            backend.value = RuntimeError("synthetic failure")
            backend.fault = lambda original, response=response: response
            item = self.controller(backend, name=f"j{index}")
            with self.subTest(response=response):
                self.assertEqual(item.tick()["status"], "stop_unconfirmed")
                self.assertEqual(item.state["phase"], "stop_pending")

    def test_stop_receipt_wrong_hash_identity_generation_or_operation_rejected(self):
        changes = [{"manifest_sha256": "f" * 64}, {"identity": {}}, {"generation": True},
                   {"previous_generation": 1}, {"operation_id": "f" * 64}, {"state": "open"}, {"durable": 1}]
        for index, change in enumerate(changes):
            backend = Backend(self.config)
            backend.value = RuntimeError()
            backend.fault = lambda result, change=change: {**result, **change}
            with self.subTest(change=change):
                self.assertEqual(self.controller(backend, name=f"j{index}").tick()["status"], "stop_unconfirmed")

    def test_equal_boolean_sample_generations_are_not_integer_generations(self):
        for generation, impostor in ((0, False), (1, True)):
            config = {**self.config, "generation": generation}
            backend = Backend(config)
            backend.value["generation"] = impostor
            item = self.controller(backend, config, name=f"sample-bool-{generation}")
            with self.subTest(generation=generation):
                self.assertEqual(impostor, generation)  # Equality alone cannot protect this boundary.
                result = item.tick()
                self.assertEqual(result["status"], "stop_confirmed")
                self.assertTrue(result["reason"].startswith("observation_failed_"))
                self.assertEqual(item.state["checks"], 0)

    def test_equal_boolean_ack_generations_never_confirm_stop(self):
        cases = [(0, {"previous_generation": False}), (0, {"generation": True}),
                 (0, {"previous_generation": False, "generation": True}),
                 (1, {"previous_generation": True})]
        for index, (generation, change) in enumerate(cases):
            config = {**self.config, "generation": generation}
            backend = Backend(config)
            backend.value = RuntimeError("synthetic sampling failure")
            backend.fault = lambda result, change=change: {**result, **change}
            item = self.controller(backend, config, name=f"ack-bool-{index}")
            with self.subTest(generation=generation, change=change):
                self.assertEqual(item.tick()["status"], "stop_unconfirmed")
                self.assertEqual(item.state["phase"], "stop_pending")

    def test_storage_sampler_both_identity_brackets_reject_equal_boolean_generations(self):
        path, _, _ = self.spool()
        receiver = observer.sample_receiver(path, observer.spool_identity(path))
        sender = sample(self.identity)["sender"]
        for generation, impostor in ((0, False), (1, True)):
            for bad_index in (0, 1):
                before = {"identity": self.identity, "generation": generation, "admission_total": 0}
                brackets = [dict(before), dict(before)]
                brackets[bad_index]["generation"] = impostor
                storage = observer.StorageSampler(self.backend, path, observer.spool_identity(path), None, "", {})
                with self.subTest(generation=generation, bracket=bad_index), \
                        patch.object(self.backend, "snapshot", side_effect=brackets), \
                        patch.object(observer, "sample_receiver", return_value=receiver), \
                        patch.object(observer, "sample_postgres", return_value=sender), \
                        self.assertRaises(observer.ObservationError):
                    storage.snapshot(self.identity, generation)

    def test_intent_sync_failure_makes_no_stop_request(self):
        item = self.controller()
        self.now = 160.0
        with patch.object(item.journal, "append", side_effect=OSError("synthetic ENOSPC")), self.assertRaises(OSError):
            item.tick()
        self.assertEqual(self.backend.stops, 0)

    def test_actual_gate_ack_closes_all_entries_and_exact_manifest(self):
        backend, config = self.actual_gate()
        backend.accept_isolated("webhook", b"synthetic body")
        item = self.controller(backend, config)
        self.now = 160.0  # Time stop needs no invented storage snapshot.
        result = item.tick()
        self.assertEqual(result["status"], "stop_confirmed")
        self.assertEqual(item.state["stop_receipt"]["manifest_sha256"], observer.digest(backend.manifest))
        self.assertFalse(item.state["stop_receipt"]["production_adapter_bound"])
        for entrance in gate.ENTRANCES:
            with self.subTest(entrance=entrance), self.assertRaises(gate.AdapterBlocked):
                backend.accept_isolated(entrance, b"synthetic refused")
        self.assertEqual(backend.snapshot(config["identity"], 1)["admission_total"], 1)

    def test_actual_gate_lost_ack_recovers_via_exact_durable_query(self):
        backend, config = self.actual_gate()
        real_stop = backend.stop
        def lose(op, reason, generation):
            real_stop(op, reason, generation)
            raise TimeoutError("synthetic loss after strict stop")
        item = self.controller(backend, config)
        self.now = 160.0
        with patch.object(backend, "stop", side_effect=lose) as call:
            self.assertEqual(item.tick()["status"], "stop_unconfirmed")
            self.assertEqual(item.tick()["status"], "stop_confirmed")
            self.assertEqual(call.call_count, 1)
        self.assertEqual(len(list((backend.root / "operations").iterdir())), 1)

    def test_real_production_mode_missing_fence_is_unconfirmed_not_stopped(self):
        backend, config = self.actual_gate()
        backend.mode = "production"
        item = self.controller(backend, config)
        self.now = 160.0
        result = item.tick()
        self.assertEqual(result["status"], "stop_unconfirmed")
        self.assertTrue(result["must_not_resume"])
        self.assertEqual(list((backend.root / "operations").iterdir()), [])

    def test_storage_sampler_reads_files_between_actual_gate_snapshots(self):
        backend, config = self.actual_gate()
        path, _, _ = self.spool()
        sender = sample(config["identity"])["sender"]
        storage = observer.StorageSampler(backend, path, observer.spool_identity(path), None, "", {})
        # No PostgreSQL claim: sender is controlled fixture; receiver/gate are real.
        with patch.object(observer, "sample_postgres", return_value=sender):
            live = storage.snapshot(config["identity"], 0)
            self.assertEqual(live["receiver"]["records"], 1)
            self.assertEqual(live["admission_total"], 0)
            self.assertLessEqual(time.time() - live["observed_epoch"], 1)

    def test_postgres_aggregate_counts_retention_without_credit(self):
        result = observer._postgres_counts([("outbox", "acked", 3, 4096, 2, 0, 3, 0),
                                           ("outbox", "held", 1, 2048, 20, 0, 0, 1),
                                           ("legacy", "held", 2, 4000, 0, 0, 0, 2)])
        self.assertEqual(result["rows"], 6)
        self.assertEqual(result["charged_bytes"], 10144)
        self.assertEqual(result["legacy"]["older_than_7_days"], 2)
        self.assertEqual(result["legacy"]["cleanup_credit"], 0)
        self.assertFalse(result["legacy"]["forwarding_allowed"])
        self.assertEqual(result["acked_at_least_7_days"], 3)
        self.assertEqual(result["expired_unacknowledged"], 1)
        self.assertEqual(result["cleanup_credit"], 0)

    def test_postgres_invalid_aggregate_fails_not_zero(self):
        cases = [[("unknown", 1, 1, 0, 0, 0, 0)], [("acked", 1, 1, 0, 1, 0, 0)],
                 [("held", 1, 1, 21, 0, 0, 0)], [("pending", 5001, 1, 0, 0, 0, 0)],
                 [("acked", 1, observer.CAPACITY_BYTES + 1, 0, 0, 0, 0)],
                 [("pending", True, 1, 0, 0, 0, 0)]]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(observer.ObservationError):
                observer._postgres_counts([("outbox", *row) for row in values])

    def test_legacy_coverage_and_shared_capacity_cannot_be_omitted(self):
        bad = [[("held", 1, 100, 0, 0, 0, 0)],
               [("legacy", "pending", 1, 100, 0, 0, 0, 0)],
               [("legacy", "held", 1, 100, 1, 0, 0, 0)],
               [("legacy", "held", 1, 100, 0, 0, 1, 0)],
               [("legacy", "held", 1, 100, 0, 0, 0, 2)],
               [("outbox", "acked", 4999, 100, 0, 0, 0, 0), ("legacy", "held", 2, 100, 0, 0, 0, 0)],
               [("outbox", "acked", 1, observer.CAPACITY_BYTES, 0, 0, 0, 0), ("legacy", "held", 1, 1, 0, 0, 0, 0)]]
        for rows in bad:
            with self.subTest(rows=rows), self.assertRaises(observer.ObservationError):
                observer._postgres_counts(rows)
        for mutation in ({"coverage": "outbox_only"}, {"legacy": {}}, {"outbox_rows": 1}):
            with self.subTest(mutation=mutation), self.assertRaises((observer.ObservationError, KeyError)):
                observer.validate_sender_sample({**observer._postgres_counts([]), **mutation})

    def test_legacy_held_requires_stop_not_automatic_cleanup(self):
        self.backend.value["sender"] = observer._postgres_counts([("legacy", "held", 1, 1024, 0, 0, 0, 1)])
        self.assertEqual(self.controller().tick()["reason"], "sender_legacy_held_requires_disposition")

    def test_postgres_cursor_protocol_and_identity_error_closes(self):
        identity = ("synthetic_outbox", "public", "12345", "synthetic-start", 123, 124)
        expected = dict(zip(("database", "schema", "system_identifier", "postmaster_started_at", "table_oid", "legacy_table_oid"), identity))
        class Cursor:
            def __init__(self):
                self.commands, self.ones = [], iter([("on",), identity])
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return None
            def execute(self, command):
                self.commands.append(command)
            def fetchone(self):
                return next(self.ones)
            def fetchall(self):
                return [("outbox", "pending", 1, 1234, 0, 0, 0, 0)]
        class Connection:
            def __init__(self):
                self.value, self.closed = Cursor(), False
            def cursor(self):
                return self.value
            def close(self):
                self.closed = True
        for mismatch in (False, True):
            connection = Connection()
            bound = {**expected, "table_oid": 999} if mismatch else expected
            if mismatch:
                with self.assertRaises(observer.ObservationError):
                    observer.sample_postgres(lambda *args, **kwargs: connection, "explicit-synthetic", bound)
                self.assertNotIn(observer.PG_SAMPLE_SQL, connection.value.commands)
            else:
                result = observer.sample_postgres(lambda *args, **kwargs: connection, "explicit-synthetic", bound)
                self.assertEqual(result["rows"], 1)
                self.assertEqual(connection.value.commands[0], "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
                self.assertEqual(connection.value.commands[-1], "ROLLBACK")
                self.assertIn("LIMIT 5001", observer.PG_SAMPLE_SQL)
            self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
