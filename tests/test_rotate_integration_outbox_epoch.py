import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from db import database
from db.integration_outbox import get_integration_outbox_state


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rotate_integration_outbox_epoch.py"


def _signal(link: str) -> dict:
    now = int(time.time())
    return {
        "event_key": link,
        "independence_key": link,
        "link": link,
        "available_at": now,
        "scored_at": now,
        "score": 8.5,
        "direction": "看多",
        "score_confidence": 0.7,
        "strategy_version": "test-v1",
        "mapping_version": "map-v1",
        "mapped_theme": "AI算力",
        "score_path": "stage2",
        "aggregation_threshold": 8.0,
        "selected": True,
    }


def _state() -> dict:
    with database.get_connection() as connection:
        return get_integration_outbox_state(connection)


def _run(path: Path, *extra: str):
    return subprocess.run(
        [
            sys.executable,
            os.fspath(SCRIPT),
            "--db",
            os.fspath(path),
            *extra,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def test_rotation_requires_stopped_writer_confirmation_and_preserves_bounds():
    database.init_db()
    assert database.store_backtest_signal(_signal("https://example.com/restore"))
    before = _state()
    path = Path(database.DB_PATH)

    refused = _run(path)

    assert refused.returncode == 2
    assert "--confirm-writes-stopped is required" in refused.stderr
    assert _state() == before

    rotated = _run(path, "--confirm-writes-stopped")
    after = _state()

    assert rotated.returncode == 0, rotated.stderr
    assert "OK integration Outbox epoch rotated" in rotated.stdout
    assert after["epoch"] != before["epoch"]
    assert after["ledger_id"] == before["ledger_id"]
    assert after["floor_seq"] == before["floor_seq"]
    assert after["last_seq"] == before["last_seq"]


def test_retained_clear_requires_separate_irreversible_replay_confirmation():
    database.init_db()
    assert database.store_backtest_signal(
        _signal("https://example.com/clear-confirmation")
    )
    before = _state()
    path = Path(database.DB_PATH)

    missing_drop_confirmation = _run(
        path,
        "--confirm-writes-stopped",
        "--clear-retained",
    )
    stray_drop_confirmation = _run(
        path,
        "--confirm-writes-stopped",
        "--confirm-drop-retained-replay",
    )

    assert missing_drop_confirmation.returncode == 2
    assert (
        "--confirm-drop-retained-replay is required with --clear-retained"
        in missing_drop_confirmation.stderr
    )
    assert stray_drop_confirmation.returncode == 2
    assert (
        "--confirm-drop-retained-replay requires --clear-retained"
        in stray_drop_confirmation.stderr
    )
    assert _state() == before


def test_confirmed_retained_clear_is_atomic_and_invalidates_old_cursor():
    database.init_db()
    for number in range(2):
        assert database.store_backtest_signal(
            _signal(f"https://example.com/clear-{number}")
        )
    before = _state()
    path = Path(database.DB_PATH)

    cleared = _run(
        path,
        "--confirm-writes-stopped",
        "--clear-retained",
        "--confirm-drop-retained-replay",
    )
    after = _state()

    assert cleared.returncode == 0, cleared.stderr
    assert "OK integration Outbox cleared and epoch rotated" in cleared.stdout
    assert "cleared_retained_rows=2" in cleared.stdout
    assert after["ledger_id"] == before["ledger_id"]
    assert after["epoch"] != before["epoch"]
    assert after["last_seq"] == before["last_seq"]
    assert after["floor_seq"] == before["last_seq"] + 1
    with database.get_connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM integration_signal_outbox"
            ).fetchone()[0]
            == 0
        )
    assert database.store_backtest_signal(
        _signal("https://example.com/clear-resumed")
    )
    resumed = _state()
    assert resumed["epoch"] == after["epoch"]
    assert resumed["floor_seq"] == before["last_seq"] + 1
    assert resumed["last_seq"] == before["last_seq"] + 1


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_rotation_rejects_non_regular_or_symlink_database(tmp_path, kind):
    database.init_db()
    path = tmp_path / "candidate"
    if kind == "symlink":
        path.symlink_to(Path(database.DB_PATH))
    else:
        path.mkdir()

    result = _run(path, "--confirm-writes-stopped")

    assert result.returncode == 1
    assert "regular non-symlink" in result.stderr


def test_rotation_fails_if_another_writer_holds_sqlite_lock():
    database.init_db()
    before = _state()
    holder = sqlite3.connect(database.DB_PATH)
    try:
        holder.execute("BEGIN IMMEDIATE")
        result = _run(
            Path(database.DB_PATH),
            "--confirm-writes-stopped",
            "--busy-timeout-ms",
            "25",
        )
    finally:
        holder.rollback()
        holder.close()

    assert result.returncode == 1
    assert "locked" in result.stderr
    assert _state() == before


def test_rotation_rolls_back_when_outbox_state_is_not_contiguous():
    database.init_db()
    for number in range(2):
        assert database.store_backtest_signal(
            _signal(f"https://example.com/corrupt-{number}")
        )
    before = _state()
    with database.get_connection() as connection:
        connection.execute("DELETE FROM integration_signal_outbox WHERE seq = 1")
        connection.commit()

    result = _run(Path(database.DB_PATH), "--confirm-writes-stopped")

    assert result.returncode == 1
    assert "retained gap" in result.stderr
    with database.get_connection() as connection:
        epoch = connection.execute(
            "SELECT v FROM app_meta WHERE k = 'integration_outbox_epoch'"
        ).fetchone()[0]
    assert epoch == before["epoch"]
