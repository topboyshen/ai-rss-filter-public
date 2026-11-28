import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from db import database
from ops import ai_filter_ops_check as monitor
from scripts import check_trading_calendar as checker

CALENDAR = {
    "schema": 1,
    "years": {
        "2026": ["2026-01-01", "2026-01-03"],
        "2027": ["2027-01-01"],
    },
}


def _write(tmp_path, payload, name="trading_calendar.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_repository_calendar_matches_the_embedded_2026_table():
    payload = json.loads(checker.DEFAULT_FILE.read_text(encoding="utf-8"))
    assert payload["schema"] == 1
    assert set(payload["years"]["2026"]) == database.CHINA_HOLIDAYS_2026


def test_database_loader_reads_the_reviewed_file_and_crosses_the_year(
    tmp_path, monkeypatch
):
    path = _write(tmp_path, CALENDAR)
    monkeypatch.setenv("AI_FILTER_TRADING_CALENDAR", str(path))
    loaded = database._load_trading_calendar()
    assert set(loaded) == {2026, 2027}

    monkeypatch.setattr(database, "CHINA_HOLIDAYS_BY_YEAR", loaded)
    target = database._trading_day_after("2026-12-04", 20)
    assert target.startswith("2027-")
    assert database._is_trading_day_local(target)


def test_ops_loader_and_preflight_cover_the_next_year(tmp_path, monkeypatch):
    path = _write(tmp_path, CALENDAR)
    monkeypatch.setenv("AI_FILTER_TRADING_CALENDAR", str(path))
    loaded = monitor._load_calendar_file()
    assert set(loaded) == {2026, 2027}

    monkeypatch.setattr(monitor, "_CHINA_HOLIDAYS_BY_YEAR", loaded)
    now = int(datetime(2026, 12, 2, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
    assert monitor.check_trading_calendar(None, now).ok is True

    monkeypatch.setattr(monitor, "_CHINA_HOLIDAYS_BY_YEAR", {2026: set()})
    result = monitor.check_trading_calendar(None, now)
    assert result.ok is False and "2027" in result.detail


def test_validator_accepts_a_covered_year_and_rejects_a_gap(tmp_path, capsys):
    covered = _write(tmp_path, CALENDAR, "covered.json")
    assert checker.main(["--file", str(covered), "--today", "2026-12-02"]) == 0
    assert json.loads(capsys.readouterr().out)["horizon_covered"] is True

    gap = _write(
        tmp_path, {"schema": 1, "years": {"2026": ["2026-01-01"]}}, "gap.json"
    )
    assert checker.main(["--file", str(gap), "--today", "2026-12-02"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False

    assert checker.main(
        [
            "--file", str(gap), "--today", "2026-11-28",
            "--require-year", "2027",
        ]
    ) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": 1, "years": {"26": ["2026-01-01"]}},
        {"schema": 1, "years": {"2026": ["2027-01-01"]}},
        {"schema": 1, "years": {"2026": ["2026-01-01", "2026-01-01"]}},
        {"schema": 1, "years": {"2026": ["not-a-date"]}},
        {"schema": 1, "years": {"2026": []}},
        {"schema": 2, "years": {"2026": ["2026-01-01"]}},
    ],
)
def test_validator_rejects_malformed_calendars(tmp_path, payload):
    path = _write(tmp_path, payload)
    assert checker.main(["--file", str(path)]) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": 1, "years": {}},
        {"schema": 1, "years": {"2026": []}},
        {"schema": 1, "years": {"2026": ["2027-01-01"]}},
        {"schema": 1, "years": {"2026": ["bad"]}},
        {"schema": 1, "years": []},
    ],
)
def test_database_and_ops_loaders_reject_malformed_files(
    tmp_path, monkeypatch, payload
):
    path = _write(tmp_path, payload)
    monkeypatch.setenv("AI_FILTER_TRADING_CALENDAR", str(path))
    monkeypatch.setattr(database, "_trading_calendar_paths", lambda: iter([str(path)]))
    monkeypatch.setattr(monitor, "_calendar_paths", lambda: iter([path]))
    assert database._load_trading_calendar() == {}
    assert monitor._load_calendar_file() == {}


def test_loaders_reject_invalid_json(tmp_path, monkeypatch):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(database, "_trading_calendar_paths", lambda: iter([str(path)]))
    monkeypatch.setattr(monitor, "_calendar_paths", lambda: iter([path]))
    assert database._load_trading_calendar() == {}
    assert monitor._load_calendar_file() == {}


def test_calendar_paths_prefer_the_environment(tmp_path, monkeypatch):
    path = _write(tmp_path, CALENDAR)
    monkeypatch.setenv("AI_FILTER_TRADING_CALENDAR", str(path))
    assert str(next(database._trading_calendar_paths())) == str(path)
    assert str(next(monitor._calendar_paths())) == str(path)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"years": []},
        {"schema": 1, "years": {"2026": "not-a-list"}},
    ],
)
def test_loaders_reject_wrong_shapes(tmp_path, monkeypatch, payload):
    path = _write(tmp_path, payload)
    monkeypatch.setattr(database, "_trading_calendar_paths", lambda: iter([str(path)]))
    monkeypatch.setattr(monitor, "_calendar_paths", lambda: iter([path]))
    assert database._load_trading_calendar() == {}
    assert monitor._load_calendar_file() == {}


def test_calendar_paths_include_repo_and_host_fallbacks(monkeypatch):
    monkeypatch.delenv("AI_FILTER_TRADING_CALENDAR", raising=False)
    db_paths = list(database._trading_calendar_paths())
    ops_paths = [str(path) for path in monitor._calendar_paths()]
    assert any(path.endswith("config/trading_calendar.json") for path in db_paths)
    assert "/app/config/trading_calendar.json" in db_paths
    assert any(path.endswith("config/trading_calendar.json") for path in ops_paths)


def test_database_loader_skips_a_missing_file(tmp_path, monkeypatch):
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(database, "_trading_calendar_paths", lambda: iter([str(missing)]))
    assert database._load_trading_calendar() == {}


@pytest.mark.parametrize("bad_date", ["2026-02-30", "2026-13-01"])
def test_loaders_reject_impossible_dates(tmp_path, monkeypatch, bad_date):
    path = _write(tmp_path, {"schema": 1, "years": {"2026": [bad_date]}})
    monkeypatch.setattr(database, "_trading_calendar_paths", lambda: iter([str(path)]))
    monkeypatch.setattr(monitor, "_calendar_paths", lambda: iter([path]))
    assert database._load_trading_calendar() == {}
    assert monitor._load_calendar_file() == {}
