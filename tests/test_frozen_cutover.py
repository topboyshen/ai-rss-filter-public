"""Combined protocol faults with real file locks and real raw SQLite inventory."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
from pathlib import Path
import time
import unittest

import test_independent_drain_inventory as fixtures

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("combined", ROOT / "scripts/frozen_cutover.py")
cutover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cutover)


class RuntimeProtocolFixture:
    """Protocol fault injection only, NOT actual Docker identity proof."""
    def __init__(self):
        self.calls = []
        self.stopped = {"status": "listed_producers_stopped", "remaining_controls": [], "scope": "isolation",
                        "receipts": {"fixture": {"durable": True}}}

    def stop_producers(self, operation_id, reason):
        self.calls.append("stop")
        return self.stopped

    def inspect_and_bind(self):
        self.calls.append("bind")
        return {"synthetic_protocol_fixture": True}


class StartupProtocolFixture:
    def __init__(self, binding, operation_id):
        self.binding, self.operation_id = binding, operation_id
        self.starts = self.queries = 0
        self.fail_ack = False
        self.check_locks = lambda: None

    def start_frozen(self, intent_sha256):
        self.starts += 1
        self.check_locks()
        self.result = {"status": "completed_frozen", "binding": self.binding, "operation_id": self.operation_id,
                       "intent_sha256": intent_sha256, "durable": True, "business_open": False}
        if self.fail_ack:
            raise OSError("isolated lost ACK")
        return self.result

    def query_frozen(self, intent_sha256):
        self.queries += 1
        assert intent_sha256 == self.result["intent_sha256"]
        return self.result


class FrozenCutoverTests(unittest.TestCase):
    def setUp(self):
        self.storage = fixtures.IndependentInventoryTests()
        self.storage.setUp()
        self.root = self.storage.root
        self.locks = [self.root / name for name in ("release.lock", "refresh.lock")]
        for path in self.locks:
            self.storage.write(path, b"")
        binding = {"revision": "1" * 40, "image_id": "sha256:" + "2" * 64,
                   "strategy": "synthetic", "source_sha256": cutover.fence_module.source_sha256(),
                   "deployment_nonce": "3" * 32}
        now = int(time.time())
        cutover.fence_module.provision(self.root / "fence", binding,
                                      {"not_before": now - 1, "expires_at": now + 120,
                                       "max_admissions": 100, "max_bytes": 10000})
        self.fence = cutover.fence_module.Fence(self.root / "fence", binding)
        new = {**binding, "deployment_nonce": "4" * 32}
        self.plan = {"schema": 1, "scope": "isolation", "run_id": "5" * 32,
                     "operation_id": "6" * 64, "startup_operation_id": "7" * 64,
                     "not_before": now - 1, "expires_at": now + 120, "old_binding": binding, "new_binding": new,
                     "release_lock": str(self.locks[0]), "refresh_lock": str(self.locks[1]),
                     "inventory": self.storage.spec, "source_sha256": {
                         "controller": hashlib.sha256((ROOT / "scripts/frozen_cutover.py").read_bytes()).hexdigest(),
                         "inventory": hashlib.sha256((ROOT / "scripts/independent_drain_inventory.py").read_bytes()).hexdigest()}}
        self.runtime = RuntimeProtocolFixture()
        self.startup = StartupProtocolFixture(new, self.plan["startup_operation_id"])

    def controller(self, **changes):
        return cutover.FrozenCutover(changes.pop("plan", self.plan), self.runtime, self.fence, self.startup,
                                     self.root / "evidence", postgres=changes.pop("postgres", self.storage.sender))

    def test_stop_before_lock_and_locks_retained_through_start(self):
        def check():
            import fcntl
            for path in [*self.locks, self.storage.spool / ".lock", self.storage.journal / ".lock",
                         self.fence.root / "locks/activity.lock"]:
                with path.open("rb") as stream:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.startup.check_locks = check
        value = self.controller().run()
        self.assertEqual(self.runtime.calls, ["stop", "bind"])
        self.assertEqual(value["status"], "completed_frozen")
        self.assertFalse(value["business_open"])
        self.assertFalse(value["switch_authorized"])
        self.assertEqual(self.fence.snapshot()["phase"], "frozen")

    def test_lost_ack_never_reissues_start_queries_original(self):
        controller = self.controller()
        self.startup.fail_ack = True
        with self.assertRaises(OSError):
            controller.run()
        self.assertTrue(controller.intent.exists())
        with self.assertRaises(cutover.journal.CutoverError):
            controller.run()
        self.assertEqual(controller.query()["status"], "completed_frozen")
        self.assertEqual((self.startup.starts, self.startup.queries), (1, 1))

    def test_queue_change_after_old_zero_is_reread_not_trusted(self):
        with self.storage.sample() as value:
            self.assertTrue(value["storage_drained"])
        self.storage.envelope()
        with self.assertRaisesRegex(cutover.journal.CutoverError, "drain not complete"):
            self.controller().run()
        self.assertEqual(self.startup.starts, 0)

    def test_startup_lock_replacement_cannot_leave_or_query_success(self):
        controller = self.controller()
        def replace():
            self.locks[0].rename(self.root / "release.original")
            self.storage.write(self.locks[0], b"")
        self.startup.check_locks = replace
        with self.assertRaisesRegex(cutover.storage.InventoryError, "lock replaced"):
            controller.run()
        self.assertFalse(controller.final.exists())
        with self.assertRaisesRegex(cutover.journal.CutoverError, "lock identity differs"):
            controller.query()
        self.assertFalse(controller.final.exists())
        self.assertEqual((self.startup.starts, self.startup.queries), (1, 0))

    def test_startup_storage_replacement_cannot_leave_or_query_success(self):
        controller = self.controller()
        def replace():
            self.storage.spool.rename(self.root / "spool.original")
            self.storage.spool.mkdir(mode=0o700)
        self.startup.check_locks = replace
        with self.assertRaises(cutover.storage.InventoryError):
            controller.run()
        self.assertFalse(controller.final.exists())
        with self.assertRaises(cutover.storage.InventoryError):
            controller.query()
        self.assertFalse(controller.final.exists())
        self.assertEqual((self.startup.starts, self.startup.queries), (1, 0))

    def test_startup_spool_lock_only_replacement_cannot_query_success(self):
        controller = self.controller()
        path = self.storage.spool / ".lock"
        def replace():
            path.rename(self.root / "spool-lock.original")
            self.storage.write(path, b"")
        self.startup.check_locks = replace
        with self.assertRaisesRegex(cutover.storage.InventoryError, "lock replaced"):
            controller.run()
        self.assertFalse(controller.final.exists())
        with self.assertRaisesRegex(cutover.journal.CutoverError, "lock identity differs"):
            controller.query()
        self.assertFalse(controller.final.exists())
        self.assertEqual((self.startup.starts, self.startup.queries), (1, 0))

    def test_partial_stop_cannot_switch(self):
        self.runtime.stopped["remaining_controls"] = ["old_async"]
        with self.assertRaises(cutover.journal.CutoverError):
            self.controller().run()
        self.assertEqual(self.startup.starts, 0)

    def test_unknown_postgres_blocks(self):
        with self.assertRaisesRegex(cutover.journal.CutoverError, "postgres_not_observed"):
            self.controller(postgres=None).run()
        self.assertEqual(self.startup.starts, 0)

    def test_production_scope_and_stale_sources_refused_before_stop(self):
        for key, value in (("scope", "production"), ("source_sha256", {})):
            plan = copy.deepcopy(self.plan)
            plan[key] = value
            with self.assertRaises(cutover.journal.CutoverError):
                self.controller(plan=plan)
        self.assertEqual(self.runtime.calls, [])

    def test_startup_wrong_binding_or_business_open_is_not_success(self):
        def bad(intent):
            return {"status": "completed_frozen", "binding": self.plan["new_binding"],
                    "operation_id": self.plan["startup_operation_id"], "intent_sha256": intent,
                    "durable": True, "business_open": True}
        self.startup.start_frozen = bad
        with self.assertRaises(cutover.journal.CutoverError):
            self.controller().run()

    def test_running_fence_is_not_drain_proof(self):
        self.fence.open_trial("8" * 64, 0, "isolated test")
        with self.assertRaises(cutover.journal.CutoverError):
            self.controller().run()
        self.assertEqual(self.startup.starts, 0)


if __name__ == "__main__":
    unittest.main()
