"""Real private queue/journal/receipt files and SQLite, never production."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("drain", ROOT / "scripts/independent_drain_inventory.py")
drain = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drain)


class IndependentInventoryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="actual-drain-", dir=os.environ.get("AI_FILTER_CUTOVER_EVIDENCE_DIR")))
        self.spool, self.journal, self.db = (self.root / n for n in ("spool", "journal", "cache.db"))
        for folder in (self.spool, self.journal, self.spool / "inflight", self.spool / "dead-letter"):
            folder.mkdir(mode=0o700)
        for path in (self.spool / ".lock", self.journal / ".lock"):
            self.write(path, b"")
        store = drain.capacity.receipt.ReceiptStore(self.spool)
        record, _ = store.accept(b"synthetic exact bytes", {"entries": []},
                                 strategy="synthetic", revision="synthetic", accepted_at=100.0)
        self.record, self.store = record, store
        store.update_status(record["delivery_id"], status="completed", completion={"synthetic": True})
        with sqlite3.connect(self.db) as connection:
            connection.execute("CREATE TABLE app_meta (k TEXT PRIMARY KEY,v TEXT NOT NULL)")
            connection.executemany("INSERT INTO app_meta VALUES (?,?)", zip(drain.META_KEYS, ("5", "5", "100")))
        os.chmod(self.db, 0o600)
        self.spec = {"spool": str(self.spool), "journal": str(self.journal), "database": str(self.db),
                     "identities": {"spool": drain.directory_identity(self.spool),
                                    "journal": drain.directory_identity(self.journal),
                                    "database": drain._identity(self.db.lstat())}}

    @staticmethod
    def write(path, raw):
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)

    @staticmethod
    def sender():
        # Sampler protocol fixture only; real PostgreSQL is a separate native test.
        return drain.capacity._postgres_counts([("outbox", "acked", 1, 1024, 1, 0, 1, 0)])

    @contextmanager
    def sample(self, **kwargs):
        with drain.locked_inventory(self.spec, postgres=kwargs.pop("postgres", self.sender), **kwargs) as value:
            yield value

    def test_actual_empty_storage_locks_retained_and_no_switch_permit(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with self.sample() as value:
            self.assertTrue(value["storage_drained"])
            self.assertFalse(value["switch_authorized"])
            for lock in (self.spool / ".lock", self.journal / ".lock"):
                with lock.open("rb") as stream:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(value["sender"]["states"]["held"], 0)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_absent_postgres_is_not_synthetic_zero(self):
        with self.sample(postgres=None) as value:
            self.assertFalse(value["storage_drained"])
            self.assertIn("postgres_not_observed", value["blockers"])

    def envelope(self, folder=None, **changes):
        task = {"entries": [], "type": "refresh"}
        digest = hashlib.sha256(json.dumps(task, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        value = {"task": task, "digest": digest, "version": 1, "attempts": 0, **changes}
        self.write((folder or self.spool) / (digest + ".json"), json.dumps(value).encode())

    def test_pending_queue_blocks(self):
        self.envelope()
        with self.sample() as value:
            self.assertIn("queue_pending", value["blockers"])

    def test_inflight_blocks_and_frozen_result_is_not_empty(self):
        self.envelope(self.spool / "inflight", frozen_result={"score": 4})
        with self.sample() as value:
            self.assertIn("queue_inflight", value["blockers"])

    def test_archive_intent_blocks(self):
        self.envelope(archive_pending={"synthetic": True})
        with self.sample() as value:
            self.assertIn("archive_pending", value["blockers"])

    def test_uncompleted_receipt_blocks_even_queue_empty(self):
        self.store.accept(b"another", {"entries": [{"id": 2}]}, strategy="synthetic", revision="synthetic")
        with self.sample() as value:
            self.assertIn("uncompleted_receipts", value["blockers"])

    def test_publication_lag_blocks(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute("UPDATE app_meta SET v='6' WHERE k=?", (drain.META_KEYS[0],))
        with self.sample() as value:
            self.assertIn("publication_not_caught_up", value["blockers"])

    def test_sender_pending_or_inflight_blocks(self):
        for state in ("pending", "inflight"):
            with self.subTest(state=state):
                value = self.sender()
                value["states"][state] = 1
                value["rows"] += 1
                value["outbox_rows"] += 1
                with self.sample(postgres=lambda: value) as result:
                    self.assertIn("sender_unfinished", result["blockers"])

    def test_held_outbox_or_legacy_never_claims_independent_zero(self):
        for source, blocker in (("outbox", "sender_held_retained"), ("legacy", "sender_legacy_held")):
            value = drain.capacity._postgres_counts([(source, "held", 1, 1024, 0, 0, 0, 0)])
            with self.subTest(source=source), self.sample(postgres=lambda: value) as result:
                self.assertFalse(result["storage_drained"])
                self.assertFalse(result["switch_authorized"])
                self.assertIn(blocker, result["blockers"])

    def test_old_outbox_only_sample_cannot_hide_legacy(self):
        for value in ({"states": {"pending": 0, "inflight": 0, "held": 0, "acked": 0}, "rows": 0},
                      {**self.sender(), "legacy": {}}):
            with self.subTest(value=value), self.assertRaises((drain.capacity.ObservationError, KeyError)), \
                    self.sample(postgres=lambda: value):
                pass

    def test_terminal_journal_retained_not_automatically_erased(self):
        strategy, link = "synthetic", "https://example.invalid/test"
        _, _, key = drain.continuations.identity(strategy, link)
        value = {"schema": 1, "article_key": key, "link": link, "strategy_version": strategy,
                 "state": "exhausted", "attempts": 3, "created_at": 100, "updated_at": 101,
                 "next_attempt_at": 102}
        self.write(self.journal / (key + ".json"), json.dumps(value).encode())
        with self.sample() as result:
            self.assertEqual(result["journal"]["exhausted"], 1)
            self.assertTrue(result["storage_drained"])
        self.assertTrue((self.journal / (key + ".json")).exists())

    def test_all_active_journal_states_block_with_frozen_result_retained(self):
        for state in ("pending", "running", "commit_pending"):
            link = "https://example.invalid/" + state
            _, _, key = drain.continuations.identity("synthetic", link)
            value = {"schema": 1, "article_key": key, "link": link, "strategy_version": "synthetic",
                     "state": state, "attempts": 1, "created_at": 100, "updated_at": 101, "next_attempt_at": 102,
                     "payload": {"title": "synthetic", "summary": "", "prompt": "synthetic", "feed_url": "synthetic"}}
            if state == "commit_pending":
                value["frozen_result"] = {"synthetic": True}
            self.write(self.journal / (key + ".json"), json.dumps(value).encode())
        with self.sample() as result:
            for state in ("pending", "running", "commit_pending"):
                self.assertIn("journal_" + state, result["blockers"])
            self.assertFalse(result["storage_drained"])

    def test_partial_unknown_and_symlink_artifacts_fail(self):
        for name in (".tmp-crash", "odd", "x.json"):
            with self.subTest(name=name):
                path = self.spool / name
                self.write(path, b"{}")
                with self.assertRaises(drain.InventoryError), self.sample():
                    pass
                path.unlink()  # Only this test's known synthetic artifact.
        (self.spool / "receipts" / "tasks" / ("a" * 64 + ".json")).symlink_to(self.db)
        with self.assertRaises(Exception), self.sample():
            pass

    def test_missing_or_busy_lock_is_not_zero(self):
        path = self.journal / ".lock"
        with path.open("rb") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError), self.sample():
                pass
        path.unlink()
        with self.assertRaises(FileNotFoundError), self.sample():
            pass
        self.assertFalse(path.exists())

    def test_directory_identity_and_database_replacement_fail(self):
        changed = copy.deepcopy(self.spec)
        changed["identities"]["spool"]["inode"] += 1
        with self.assertRaises(drain.InventoryError), drain.locked_inventory(changed):
            pass
        self.db.rename(self.root / "original.db")
        self.write(self.db, b"not a database")
        with self.assertRaises(drain.InventoryError), self.sample():
            pass

    def test_duplicate_json_is_not_valid_queue(self):
        self.write(self.spool / ("a" * 64 + ".json"), b'{"task":{},"task":{}}')
        with self.assertRaises(drain.InventoryError), self.sample():
            pass


if __name__ == "__main__":
    unittest.main()
