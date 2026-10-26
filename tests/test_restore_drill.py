import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path

from db.event_core import init_event_core_schema
from db.integration_outbox import init_integration_signal_outbox
from ops.backup import restore_drill


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DRILL = PROJECT_ROOT / "ops" / "backup" / "restore_drill.py"


def test_restore_drill_help_loads_verifier_in_isolated_mode(tmp_path):
    result = subprocess.run(
        [sys.executable, "-I", str(DRILL), "--help"],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Isolated AI-Filter restore drill" in result.stdout


def _backup(tmp_path: Path, mutate=None) -> tuple[Path, Path]:
    source = tmp_path / "source"
    database = source / "2026-07-19" / "ai-rss-filter" / "sqlite" / "data" / "cache.db"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        init_event_core_schema(connection)
        connection.execute(
            "CREATE TABLE news_cache ("
            "link TEXT PRIMARY KEY, title TEXT NOT NULL, event_id TEXT, "
            "event_version INTEGER NOT NULL DEFAULT 0)"
        )
        connection.execute(
            "CREATE TABLE backtest_signals ("
            "id INTEGER PRIMARY KEY, event_id TEXT, "
            "event_version INTEGER NOT NULL DEFAULT 0, "
            "event_mapping_id TEXT, "
            "mapping_confidence REAL NOT NULL DEFAULT 0, "
            "mapping_relevance REAL NOT NULL DEFAULT 0, expected_gap REAL)"
        )
        for table in (
            "api_stats",
            "feedback_example_snapshots",
            "scoring_contract_snapshots",
        ):
            connection.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY)')
        connection.execute(
            "CREATE TABLE backtest_signal_securities ("
            "ledger_signal_id INTEGER NOT NULL, mapping_id TEXT NOT NULL, "
            "event_id TEXT NOT NULL, security_id TEXT NOT NULL, "
            "mapping_revision INTEGER NOT NULL, signal_available_at INTEGER NOT NULL, "
            "captured_at INTEGER NOT NULL, PRIMARY KEY (ledger_signal_id, mapping_id))"
        )
        connection.execute(
            "CREATE TABLE score_bridge_runs (run_id TEXT PRIMARY KEY)"
        )
        connection.execute(
            "CREATE TABLE score_bridge_members ("
            "run_id TEXT NOT NULL REFERENCES score_bridge_runs(run_id) ON DELETE RESTRICT, "
            "ledger_signal_id INTEGER NOT NULL REFERENCES backtest_signals(id) ON DELETE RESTRICT, "
            "PRIMARY KEY (run_id, ledger_signal_id))"
        )
        connection.execute("CREATE TABLE app_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        init_integration_signal_outbox(connection, backfill=False, trim=False)
        connection.executemany(
            "INSERT OR REPLACE INTO app_meta (k, v) VALUES (?, ?)",
            [
                ("integration_outbox_ledger_id", "a" * 32),
                ("integration_outbox_epoch", "b" * 32),
                ("integration_outbox_floor_seq", "1"),
                ("integration_outbox_last_seq", "0"),
                ("event_core_schema_version", "2"),
                ("event_core_backfill_v2", "100"),
            ],
        )
        connection.execute(
            "CREATE TABLE future_price_revisions ("
            "revision_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, "
            "trade_date TEXT NOT NULL, name TEXT, close_price REAL NOT NULL, "
            "change_amt REAL, change_pct REAL, open_price REAL, high_price REAL, "
            "low_price REAL, prev_close REAL, volume REAL, amount REAL, "
            "limit_up REAL, limit_down REAL, suspension_state TEXT, "
            "limit_state TEXT, tradability_state TEXT, source_ts INTEGER, "
            "recorded_at INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE future_prices ("
            "symbol TEXT NOT NULL, trade_date TEXT NOT NULL, name TEXT, "
            "close_price REAL NOT NULL, change_amt REAL, change_pct REAL, "
            "open_price REAL, high_price REAL, low_price REAL, prev_close REAL, "
            "volume REAL, amount REAL, limit_up REAL, limit_down REAL, "
            "suspension_state TEXT, limit_state TEXT, tradability_state TEXT, "
            "ts INTEGER, "
            "revision_id TEXT, PRIMARY KEY (symbol, trade_date))"
        )
        if mutate is not None:
            mutate(connection)
        connection.commit()
    finally:
        connection.close()

    backups = tmp_path / "backups"
    backups.mkdir()
    archive = backups / "system_backup_2026-07-19T033001.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(source / "2026-07-19", arcname="2026-07-19")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    Path(str(archive) + ".sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="ascii"
    )
    return backups, archive


def _run(tmp_path: Path, backups: Path, state: Path):
    return subprocess.run(
        [
            os.fspath(DRILL),
            "--backup-dir",
            os.fspath(backups),
            "--state-file",
            os.fspath(state),
            "--tmp-root",
            os.fspath(tmp_path),
            "--now",
            "2000000000",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_restore_drill_extracts_only_to_temp_and_writes_private_success(tmp_path):
    backups, archive = _backup(tmp_path)
    state = tmp_path / "state" / "restore-drill.json"

    result = _run(tmp_path, backups, state)

    assert result.returncode == 0, result.stderr
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["status"] == "ok"
    assert payload["archive"] == archive.name
    assert payload["required_tables"] == [
        "news_cache",
        "api_stats",
        "future_prices",
        "future_price_revisions",
        "backtest_signals",
        "backtest_signal_securities",
        "score_bridge_runs",
        "score_bridge_members",
        "app_meta",
        "integration_signal_outbox",
        "events",
        "event_identities",
        "securities",
        "event_securities",
        "event_evidence",
        "event_mapping_replay_ledger",
        "event_mapping_replay_dirty_roots",
        "event_similarity_replay_dirty_roots",
        "event_outcomes",
        "event_similarities",
        "event_merge_history",
        "feedback_example_snapshots",
        "scoring_contract_snapshots",
    ]
    assert state.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("ai-filter-restore-drill.*"))
    assert archive.is_file()


def test_restore_drill_records_failure_without_touching_archive(tmp_path):
    backups, archive = _backup(tmp_path)
    state = tmp_path / "state" / "restore-drill.json"
    archive.write_bytes(b"corrupted")
    original = archive.read_bytes()

    result = _run(tmp_path, backups, state)

    assert result.returncode == 1
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["error"] in {"ReadError", "ValueError"}
    assert archive.read_bytes() == original


def test_restore_drill_rejects_foreign_key_violation(tmp_path):
    def corrupt(connection):
        # Build a deliberately corrupt backup fixture.  The production restore
        # verifier re-enables FK checks before opening this candidate.
        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))"
        )
        connection.execute("INSERT INTO child VALUES (99)")

    backups, _archive = _backup(tmp_path, corrupt)
    state = tmp_path / "state" / "restore-drill.json"

    result = _run(tmp_path, backups, state)

    assert result.returncode == 1
    assert json.loads(state.read_text(encoding="utf-8"))["error"] == "ValueError"


def test_restore_drill_rejects_missing_event_core_backfill_marker(tmp_path):
    def corrupt(connection):
        connection.execute(
            "DELETE FROM app_meta WHERE k = 'event_core_backfill_v2'"
        )

    backups, _archive = _backup(tmp_path, corrupt)
    state = tmp_path / "state" / "restore-drill.json"

    result = _run(tmp_path, backups, state)

    assert result.returncode == 1
    assert json.loads(state.read_text(encoding="utf-8"))["error"] == "ValueError"


def test_restore_drill_rejects_orphaned_news_event_reference(tmp_path):
    def corrupt(connection):
        connection.execute(
            "INSERT INTO news_cache (link, title, event_id, event_version) "
            "VALUES ('https://example.com/orphan', 'Orphan', ?, 1)",
            (str(uuid.uuid4()),),
        )

    backups, _archive = _backup(tmp_path, corrupt)
    state = tmp_path / "state" / "restore-drill.json"

    result = _run(tmp_path, backups, state)

    assert result.returncode == 1
    assert json.loads(state.read_text(encoding="utf-8"))["error"] == "ValueError"


def test_restore_drill_rejects_unresolved_outbox_event_reference(tmp_path):
    def corrupt(connection):
        connection.execute(
            "INSERT INTO integration_signal_outbox "
            "(ledger_signal_id, event_id, available_at, recorded_at, enqueued_at, "
            "score, direction, score_confidence, strategy_version, mapping_version, "
            "mapped_theme, score_path, revision_key, signal_stage, revision_seq, "
            "revision_state_key, independence_id, aggregation_threshold, selected, "
            "is_similarity_copy, domain_event_id, event_version, event_mapping_id) "
            "VALUES (1, ?, 1, 1, 1, 1, 'bullish', 1, 'strategy', 'mapping', "
            "'theme', 'llm', 'base', 'base', 0, ?, ?, 8.5, 1, 0, NULL, NULL, NULL)",
            ("a" * 64, "b" * 64, "c" * 64),
        )
        connection.execute(
            "UPDATE app_meta SET v = '1' "
            "WHERE k = 'integration_outbox_last_seq'"
        )

    backups, _archive = _backup(tmp_path, corrupt)
    state = tmp_path / "state" / "restore-drill.json"

    result = _run(tmp_path, backups, state)

    assert result.returncode == 1
    assert json.loads(state.read_text(encoding="utf-8"))["error"] == "ValueError"


def test_restore_drill_rejects_weakened_same_name_replay_table(tmp_path):
    def weaken(connection):
        connection.execute("DROP TABLE event_mapping_replay_ledger")
        connection.execute(
            "CREATE TABLE event_mapping_replay_ledger (id INTEGER PRIMARY KEY)"
        )

    backups, _archive = _backup(tmp_path, weaken)
    state = tmp_path / "state" / "restore-drill.json"

    result = _run(tmp_path, backups, state)

    assert result.returncode == 1
    assert json.loads(state.read_text(encoding="utf-8"))["error"] == "ValueError"


def test_restore_drill_rejects_empty_weakened_outbox_table(tmp_path):
    def weaken(connection):
        connection.execute("DROP TABLE integration_signal_outbox")
        connection.execute(
            "CREATE TABLE integration_signal_outbox (seq INTEGER PRIMARY KEY)"
        )

    backups, _archive = _backup(tmp_path, weaken)
    state = tmp_path / "state" / "restore-drill.json"

    result = _run(tmp_path, backups, state)

    assert result.returncode == 1
    assert json.loads(state.read_text(encoding="utf-8"))["error"] == "ValueError"


def test_restore_drill_rejects_outbox_sequence_gap(tmp_path):
    def corrupt(connection):
        connection.execute(
            "UPDATE app_meta SET v = '2' "
            "WHERE k = 'integration_outbox_last_seq'"
        )

    backups, _archive = _backup(tmp_path, corrupt)
    state = tmp_path / "state" / "restore-drill.json"

    result = _run(tmp_path, backups, state)

    assert result.returncode == 1
    assert json.loads(state.read_text(encoding="utf-8"))["error"] == "ValueError"


def test_restore_drill_rejects_mismatched_future_price_revision(tmp_path):
    def corrupt(connection):
        connection.execute(
            "INSERT INTO future_price_revisions "
            "(revision_id, symbol, trade_date, name, close_price, change_amt, "
            "change_pct, suspension_state, limit_state, tradability_state, "
            "source_ts, recorded_at) VALUES "
            "('revision-1', 'sz399006', '2026-07-20', '创业板指', 100, 1, 1, "
            "'unknown', 'unknown', 'unknown', 100, 1000)"
        )
        connection.execute(
            "INSERT INTO future_prices "
            "(symbol, trade_date, name, close_price, change_amt, change_pct, "
            "suspension_state, limit_state, tradability_state, ts, revision_id) "
            "VALUES ('sz399006', '2026-07-20', '创业板指', 101, 1, 1, "
            "'unknown', 'unknown', 'unknown', 100, 'revision-1')"
        )

    backups, _archive = _backup(tmp_path, corrupt)
    state = tmp_path / "state" / "restore-drill.json"

    result = _run(tmp_path, backups, state)

    assert result.returncode == 1
    assert json.loads(state.read_text(encoding="utf-8"))["error"] == "ValueError"


def test_restore_drill_rejects_non_current_future_price_pointer(tmp_path):
    def corrupt(connection):
        rows = [
            ("revision-old", 100.0, 100, 1_000),
            ("revision-new", 101.0, 200, 1_001),
        ]
        connection.executemany(
            "INSERT INTO future_price_revisions "
            "(revision_id, symbol, trade_date, name, close_price, change_amt, "
            "change_pct, suspension_state, limit_state, tradability_state, "
            "source_ts, recorded_at) VALUES (?, 'sz399006', '2026-07-20', "
            "'创业板指', ?, 1, 1, 'unknown', 'unknown', 'unknown', ?, ?)",
            rows,
        )
        connection.execute(
            "INSERT INTO future_prices "
            "(symbol, trade_date, name, close_price, change_amt, change_pct, "
            "suspension_state, limit_state, tradability_state, ts, revision_id) "
            "VALUES ('sz399006', '2026-07-20', '创业板指', 100, 1, 1, "
            "'unknown', 'unknown', 'unknown', 100, 'revision-old')"
        )

    backups, _archive = _backup(tmp_path, corrupt)
    state = tmp_path / "state" / "restore-drill.json"

    result = _run(tmp_path, backups, state)

    assert result.returncode == 1
    assert json.loads(state.read_text(encoding="utf-8"))["error"] == "ValueError"


def test_restore_drill_rejects_revision_key_without_current_pointer(tmp_path):
    def corrupt(connection):
        connection.execute(
            "INSERT INTO future_price_revisions "
            "(revision_id, symbol, trade_date, name, close_price, change_amt, "
            "change_pct, suspension_state, limit_state, tradability_state, "
            "source_ts, recorded_at) VALUES "
            "('ledger-only', 'sz399006', '2026-07-20', '创业板指', 101, 1, 1, "
            "'unknown', 'unknown', 'unknown', 200, 1001)"
        )

    backups, _archive = _backup(tmp_path, corrupt)
    state = tmp_path / "state" / "restore-drill.json"

    result = _run(tmp_path, backups, state)

    assert result.returncode == 1
    assert json.loads(state.read_text(encoding="utf-8"))["error"] == "ValueError"


def test_restore_drill_replaces_old_success_then_persists_sigterm_failure(
    monkeypatch, tmp_path
):
    backups, _archive = _backup(tmp_path)
    state = tmp_path / "state" / "restore-drill.json"
    state.parent.mkdir()
    state.write_text(
        json.dumps(
            {
                "version": 1,
                "status": "ok",
                "checked_at": 1,
                "archive": "system_backup_old.tar.gz",
                "sha256": "a" * 64,
                "required_tables": ["news_cache"],
            }
        ),
        encoding="utf-8",
    )
    state.chmod(0o600)
    observed = {}

    def terminate_after_running(_path):
        observed.update(json.loads(state.read_text(encoding="utf-8")))
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("SIGTERM handler did not abort the drill")

    monkeypatch.setattr(restore_drill, "_hash_file", terminate_after_running)

    rc = restore_drill.main(
        [
            "--backup-dir",
            os.fspath(backups),
            "--state-file",
            os.fspath(state),
            "--tmp-root",
            os.fspath(tmp_path),
            "--now",
            "2000000000",
        ]
    )

    assert rc == 1
    assert observed["status"] == "running"
    assert observed["started_at"] == 2_000_000_000
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["error"] == "SIGTERM"
    assert payload["checked_at"] == 2_000_000_000


def test_restore_drill_configuration_exception_clears_old_success(monkeypatch, tmp_path):
    state = tmp_path / "state" / "restore-drill.json"
    state.parent.mkdir()
    state.write_text(
        json.dumps({"version": 1, "status": "ok", "checked_at": 1}),
        encoding="utf-8",
    )
    state.chmod(0o600)
    monkeypatch.setenv("RESTORE_DRILL_REQUIRED_TABLES", "unsafe-table-name!")

    rc = restore_drill.main(
        [
            "--backup-dir",
            os.fspath(tmp_path / "backups"),
            "--state-file",
            os.fspath(state),
            "--tmp-root",
            os.fspath(tmp_path),
            "--now",
            "2000000000",
        ]
    )

    assert rc == 1
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["error"] == "ValueError"
