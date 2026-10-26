#!/usr/bin/env python3
"""Verify the newest backup by restoring SQLite into an isolated temp dir.

The drill never extracts onto the application tree and never opens the
production database. Its only persistent write is a private atomic status file
consumed by ``ai-filter-ops-check``.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import importlib.util
import json
import os
import re
import signal
import sqlite3
import sys
import tarfile
import tempfile
import time
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve()
_BUNDLE_NAME_RE = re.compile(r"^restore-drill-([0-9a-f]{64})$")


def _event_core_path() -> Path:
    bundle_match = _BUNDLE_NAME_RE.fullmatch(_SCRIPT_PATH.parent.name)
    if bundle_match:
        # Installed layout: bind the verifier to the content-addressed bundle
        # before importing any of its code. Corruption must fail closed rather
        # than falling back to an unrelated checkout.
        candidate = _SCRIPT_PATH.with_name("event_core.py")
        if not candidate.is_file() or candidate.is_symlink():
            raise RuntimeError("restore drill bundle verifier is missing or unsafe")
        digest = hashlib.sha256()
        for path in (_SCRIPT_PATH, candidate):
            data = path.read_bytes()
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        if not hmac.compare_digest(digest.hexdigest(), bundle_match.group(1)):
            raise RuntimeError("restore drill bundle digest does not match")
        return candidate

    # Source-tree layout used by developers and CI.
    return _SCRIPT_PATH.parents[2] / "db" / "event_core.py"


_EVENT_CORE_PATH = _event_core_path()
_EVENT_CORE_SPEC = importlib.util.spec_from_file_location(
    "_restore_drill_event_core",
    _EVENT_CORE_PATH,
)
if _EVENT_CORE_SPEC is None or _EVENT_CORE_SPEC.loader is None:
    raise RuntimeError("cannot load the event-core schema verifier")
_EVENT_CORE_MODULE = importlib.util.module_from_spec(_EVENT_CORE_SPEC)
_EVENT_CORE_SPEC.loader.exec_module(_EVENT_CORE_MODULE)
EventCoreError = _EVENT_CORE_MODULE.EventCoreError
verify_event_core_schema = _EVENT_CORE_MODULE.verify_event_core_schema
verify_event_core_backfill_state = (
    _EVENT_CORE_MODULE.verify_event_core_backfill_state
)
verify_integration_outbox_schema = (
    _EVENT_CORE_MODULE.verify_integration_outbox_schema
)


HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ARCHIVE_RE = re.compile(r"^system_backup_[A-Za-z0-9T_.-]+\.tar\.gz$")
DB_MEMBER_RE = re.compile(
    r"^[^/]+/ai-rss-filter/sqlite/data/cache\.db$"
)
SAFE_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
OUTBOX_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class _DrillSignal(Exception):
    """Turn a termination signal into a persisted failed drill result."""

    def __init__(self, signum: int):
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


def _positive_int(value: str, name: str) -> int:
    if not re.fullmatch(r"[0-9]+", str(value or "")) or int(value) <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return int(value)


def _required_tables(value: str) -> tuple[str, ...]:
    tables = tuple(part.strip() for part in value.split(",") if part.strip())
    if not tables or any(not SAFE_TABLE_RE.fullmatch(table) for table in tables):
        raise ValueError("RESTORE_DRILL_REQUIRED_TABLES is invalid")
    return tables


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_hash(archive: Path) -> str:
    manifest = Path(str(archive) + ".sha256")
    parts = manifest.read_text(encoding="ascii").strip().split()
    if len(parts) != 2 or not HASH_RE.fullmatch(parts[0]):
        raise ValueError("backup manifest is malformed")
    if parts[1].lstrip("*") != archive.name:
        raise ValueError("backup manifest filename does not match")
    return parts[0]


def _atomic_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".restore-drill-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _extract_database(archive: Path, destination: Path, max_bytes: int) -> int:
    with tarfile.open(archive, mode="r:gz") as bundle:
        matches = [
            member
            for member in bundle.getmembers()
            if DB_MEMBER_RE.fullmatch(member.name)
        ]
        if len(matches) != 1:
            raise ValueError("backup must contain exactly one AI-Filter cache.db")
        member = matches[0]
        if not member.isfile() or member.size <= 0 or member.size > max_bytes:
            raise ValueError("backup cache.db member is invalid or too large")
        source = bundle.extractfile(member)
        if source is None:
            raise ValueError("backup cache.db cannot be read")
        copied = 0
        with source, destination.open("xb") as output:
            while True:
                chunk = source.read(min(1024 * 1024, max_bytes - copied + 1))
                if not chunk:
                    break
                copied += len(chunk)
                if copied > max_bytes or copied > member.size:
                    raise ValueError("backup cache.db exceeded declared size")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if copied != member.size:
            raise ValueError("backup cache.db was truncated")
        os.chmod(destination, 0o600)
        return copied


def _verify_database(path: Path, required_tables: tuple[str, ...]) -> None:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        result = connection.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise ValueError("restored SQLite quick_check failed")
        present = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing = sorted(set(required_tables).difference(present))
        if missing:
            raise ValueError("restored SQLite is missing required tables")
        # Force representative reads through SQLite, not just sqlite_master.
        for table in required_tables:
            connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ValueError("restored SQLite foreign_key_check failed")
        try:
            verify_event_core_schema(connection)
            verify_event_core_backfill_state(connection)
            verify_integration_outbox_schema(connection)
        except (sqlite3.DatabaseError, EventCoreError) as exc:
            raise ValueError("restored SQLite event-core schema is incompatible") from exc
        _verify_outbox_state(connection, present)
        _verify_future_price_revisions(connection, present)
        _verify_signal_mapping_snapshots(connection, present)
    finally:
        connection.close()


def _verify_outbox_state(connection: sqlite3.Connection, tables: set[str]) -> None:
    required = {"app_meta", "integration_signal_outbox"}
    if not required.issubset(tables):
        raise ValueError("restored SQLite is missing durable Outbox state")
    metadata = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT k, v FROM app_meta WHERE k IN ("
            "'integration_outbox_ledger_id', 'integration_outbox_epoch', "
            "'integration_outbox_floor_seq', 'integration_outbox_last_seq')"
        )
    }
    ledger_id = metadata.get("integration_outbox_ledger_id", "")
    epoch = metadata.get("integration_outbox_epoch", "")
    if not OUTBOX_ID_RE.fullmatch(ledger_id) or not OUTBOX_ID_RE.fullmatch(epoch):
        raise ValueError("restored SQLite Outbox identity metadata is invalid")
    try:
        floor = int(metadata["integration_outbox_floor_seq"])
        last = int(metadata["integration_outbox_last_seq"])
    except (KeyError, ValueError) as exc:
        raise ValueError("restored SQLite Outbox bounds metadata is invalid") from exc
    if floor < 1 or last < 0 or floor > last + 1:
        raise ValueError("restored SQLite Outbox bounds metadata is invalid")
    minimum, maximum, count = connection.execute(
        "SELECT MIN(seq), MAX(seq), COUNT(*) FROM integration_signal_outbox"
    ).fetchone()
    expected = max(0, last - floor + 1)
    if int(count or 0) != expected:
        raise ValueError("restored SQLite Outbox sequence has a retained gap")
    if expected:
        if int(minimum) != floor or int(maximum) != last:
            raise ValueError("restored SQLite Outbox bounds do not match rows")
    elif minimum is not None or maximum is not None or floor != last + 1:
        raise ValueError("restored SQLite empty Outbox bounds are invalid")


def _verify_future_price_revisions(
    connection: sqlite3.Connection, tables: set[str]
) -> None:
    required = {"future_prices", "future_price_revisions"}
    if not required.issubset(tables):
        raise ValueError("restored SQLite is missing future price revision state")
    invalid = connection.execute(
        "SELECT 1 FROM future_prices AS p "
        "LEFT JOIN future_price_revisions AS r ON r.revision_id = p.revision_id "
        "WHERE COALESCE(TRIM(p.revision_id), '') = '' OR r.revision_id IS NULL "
        "OR p.symbol IS NOT r.symbol OR p.trade_date IS NOT r.trade_date "
        "OR p.name IS NOT r.name OR p.close_price IS NOT r.close_price "
        "OR p.change_amt IS NOT r.change_amt OR p.change_pct IS NOT r.change_pct "
        "OR p.open_price IS NOT r.open_price OR p.high_price IS NOT r.high_price "
        "OR p.low_price IS NOT r.low_price OR p.prev_close IS NOT r.prev_close "
        "OR p.volume IS NOT r.volume OR p.amount IS NOT r.amount "
        "OR p.limit_up IS NOT r.limit_up OR p.limit_down IS NOT r.limit_down "
        "OR p.suspension_state IS NOT r.suspension_state "
        "OR p.limit_state IS NOT r.limit_state "
        "OR p.tradability_state IS NOT r.tradability_state "
        "OR p.ts IS NOT r.source_ts "
        "LIMIT 1"
    ).fetchone()
    if invalid is not None:
        raise ValueError("restored SQLite future price revision is inconsistent")
    missing = connection.execute(
        "SELECT 1 FROM future_price_revisions AS revision "
        "WHERE NOT EXISTS (SELECT 1 FROM future_prices AS current_pointer "
        "WHERE current_pointer.symbol IS revision.symbol "
        "AND current_pointer.trade_date IS revision.trade_date) LIMIT 1"
    ).fetchone()
    if missing is not None:
        raise ValueError("restored SQLite future price pointer is missing")
    stale = connection.execute(
        "SELECT 1 FROM future_prices AS current_pointer "
        "JOIN future_price_revisions AS current_revision "
        "ON current_revision.revision_id = current_pointer.revision_id "
        "WHERE EXISTS (SELECT 1 FROM future_price_revisions AS candidate "
        "WHERE candidate.symbol IS current_pointer.symbol "
        "AND candidate.trade_date IS current_pointer.trade_date AND ("
        "COALESCE(candidate.source_ts, 0) > COALESCE(current_revision.source_ts, 0) "
        "OR (COALESCE(candidate.source_ts, 0) = "
        "COALESCE(current_revision.source_ts, 0) AND ("
        "candidate.recorded_at > current_revision.recorded_at OR ("
        "candidate.recorded_at = current_revision.recorded_at "
        "AND candidate.revision_id > current_revision.revision_id))))) LIMIT 1"
    ).fetchone()
    if stale is not None:
        raise ValueError("restored SQLite future price pointer is not globally current")


def _verify_signal_mapping_snapshots(
    connection: sqlite3.Connection, tables: set[str]
) -> None:
    table = "backtest_signal_securities"
    if table not in tables:
        raise ValueError("restored SQLite is missing signal mapping snapshots")
    required_columns = {
        "ledger_signal_id",
        "mapping_id",
        "event_id",
        "security_id",
        "mapping_revision",
        "signal_available_at",
        "captured_at",
    }
    columns = {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
    }
    if not required_columns.issubset(columns):
        raise ValueError("restored SQLite signal mapping snapshot schema is invalid")
    # Empty is a valid prospective cohort.  Non-empty rows are already covered
    # by foreign_key_check; force a bounded read of every contract field here.
    connection.execute(
        "SELECT ledger_signal_id, mapping_id, event_id, security_id, "
        "mapping_revision, signal_available_at, captured_at "
        "FROM backtest_signal_securities LIMIT 1"
    ).fetchone()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Isolated AI-Filter restore drill")
    parser.add_argument(
        "--backup-dir",
        default=os.environ.get("OPS_BACKUP_DIR", "/root/backups/system"),
    )
    parser.add_argument(
        "--state-file",
        default=os.environ.get(
            "OPS_RESTORE_DRILL_STATE",
            "/var/lib/ai-filter-ops-check/restore-drill.json",
        ),
    )
    parser.add_argument(
        "--tmp-root", default=os.environ.get("RESTORE_DRILL_TMP_ROOT", "/var/tmp")
    )
    parser.add_argument("--now", type=int, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    state_path = Path(args.state_file)
    state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_path.parent, 0o700)
    lock_path = Path(str(state_path) + ".lock")
    now = int(args.now if args.now is not None else time.time())

    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0

        archive_name = ""
        archive_hash = ""
        previous_handlers: dict[int, object] = {}

        def abort_on_signal(signum, _frame):  # noqa: ANN001
            raise _DrillSignal(int(signum))

        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[signum] = signal.signal(signum, abort_on_signal)

            # Replace any prior success before touching the archive. If the
            # process is later SIGKILLed or the host loses power, the monitor
            # sees a bounded "running" attestation rather than an old green one.
            _atomic_state(
                state_path,
                {
                    "version": 1,
                    "status": "running",
                    "started_at": now,
                    "checked_at": now,
                },
            )
            required = _required_tables(
                os.environ.get(
                    "RESTORE_DRILL_REQUIRED_TABLES",
                    "news_cache,api_stats,future_prices,future_price_revisions,"
                    "backtest_signals,backtest_signal_securities,"
                    "score_bridge_runs,score_bridge_members,app_meta,"
                    "integration_signal_outbox,events,"
                    "event_identities,securities,event_securities,event_evidence,"
                    "event_mapping_replay_ledger,event_mapping_replay_dirty_roots,"
                    "event_similarity_replay_dirty_roots,"
                    "event_outcomes,event_similarities,"
                    "event_merge_history,"
                    "feedback_example_snapshots,scoring_contract_snapshots",
                )
            )
            max_bytes = _positive_int(
                os.environ.get("RESTORE_DRILL_MAX_DB_BYTES", str(8 * 1024**3)),
                "RESTORE_DRILL_MAX_DB_BYTES",
            )
            backup_dir = Path(args.backup_dir)
            archives = [
                path
                for path in backup_dir.glob("system_backup_*.tar.gz")
                if path.is_file() and ARCHIVE_RE.fullmatch(path.name)
            ]
            if not archives:
                raise ValueError("no backup archive is available")
            archive = max(archives, key=lambda path: path.stat().st_mtime)
            archive_name = archive.name
            archive_hash = _manifest_hash(archive)
            if not hmac.compare_digest(archive_hash, _hash_file(archive)):
                raise ValueError("backup archive SHA256 mismatch")
            tmp_root = Path(args.tmp_root)
            with tempfile.TemporaryDirectory(
                prefix="ai-filter-restore-drill.", dir=tmp_root
            ) as temporary:
                database = Path(temporary) / "cache.db"
                database_size = _extract_database(archive, database, max_bytes)
                _verify_database(database, required)
            payload: dict[str, object] = {
                "version": 1,
                "status": "ok",
                "checked_at": now,
                "archive": archive_name,
                "sha256": archive_hash,
                "database_size": database_size,
                "required_tables": list(required),
            }
            _atomic_state(state_path, payload)
            print(f"OK restore drill archive={archive_name}")
            return 0
        except Exception as exc:
            if isinstance(exc, _DrillSignal):
                error = signal.Signals(exc.signum).name
            else:
                error = type(exc).__name__
            _atomic_state(
                state_path,
                {
                    "version": 1,
                    "status": "failed",
                    "checked_at": now,
                    "archive": archive_name,
                    "sha256": archive_hash if HASH_RE.fullmatch(archive_hash) else "",
                    "error": error,
                },
            )
            print(f"FAIL restore drill error={error}", file=sys.stderr)
            return 1
        finally:
            for signum, previous in previous_handlers.items():
                signal.signal(signum, previous)


if __name__ == "__main__":
    raise SystemExit(main())
