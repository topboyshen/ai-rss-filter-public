import importlib.util
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "repair_market_close.py"
SPEC = importlib.util.spec_from_file_location("market_close_repair_test", MODULE_PATH)
repair = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(repair)


@pytest.fixture(autouse=True)
def _isolate_market_close_result_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKET_CLOSE_RESULT_DIR", str(tmp_path / "market_close"))


def test_import_resolves_root_from_temporary_checkout(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    copied_script = checkout / "scripts" / "repair_market_close.py"
    copied_script.parent.mkdir(parents=True)
    shutil.copy2(MODULE_PATH, copied_script)
    monkeypatch.chdir(tmp_path)

    original_sys_path = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location(
            "market_close_repair_temp_checkout",
            copied_script,
        )
        imported = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(imported)
    finally:
        sys.path[:] = original_sys_path

    assert sys.path == original_sys_path
    assert imported.ROOT == checkout
    assert imported.DB == checkout / "data" / "cache.db"


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit):
        assert limit == 200_000
        return self.payload


def test_expected_trade_date_matches_1525_timer_and_market_calendar():
    zone = ZoneInfo("Asia/Shanghai")

    assert (
        repair._expected_trade_date(datetime(2026, 8, 20, 15, 25, tzinfo=zone))
        == "2026-08-20"
    )
    assert (
        repair._expected_trade_date(datetime(2026, 8, 20, 14, 59, tzinfo=zone))
        == "2026-08-19"
    )
    assert (
        repair._expected_trade_date(datetime(2026, 8, 22, 15, 25, tzinfo=zone))
        == "2026-08-21"
    )
    assert (
        repair._expected_trade_date(datetime(2026, 10, 1, 15, 25, tzinfo=zone))
        == "2026-09-30"
    )


def test_sina_daily_delegates_to_timestamped_source_without_network(monkeypatch):
    calls = []
    row = _record('bj920021')
    monkeypatch.setattr(repair, 'fetch_sina_close_prices',
                        lambda symbols, **kw: calls.append((symbols, kw)) or [row])
    assert repair._sina_daily('bj920021', '2026-08-20') is row
    assert calls == [(['bj920021'], {'trade_date': '2026-08-20'})]


def _record(symbol, trade_date="2026-08-20", **updates):
    row = {
        "symbol": symbol,
        "name": symbol,
        "close_price": 10.0,
        "change_amt": 0.0,
        "change_pct": 0.0,
        "ts": int(datetime.fromisoformat(trade_date + "T15:00:00+08:00").timestamp()),
        "source_trade_date": trade_date,
    }
    row.update(updates)
    return row


@pytest.mark.parametrize("updates", [
    {"close_price": 0.0}, {"close_price": -1.0}, {"close_price": None},
    {"close_price": float("nan")}, {"close_price": float("inf")},
    {"close_price": "invalid"}, {"ts": "invalid"}, {"ts": None},
    {"ts": int(datetime.fromisoformat("2026-08-20T14:59:00+08:00").timestamp())},
    {"ts": int(datetime.fromisoformat("2026-08-19T15:00:00+08:00").timestamp())},
])
def test_existing_invalid_required_close_is_selected_for_repair(tmp_path, monkeypatch, updates):
    db = tmp_path / "required-closes.db"
    rows = [_record(symbol) for symbol in repair.REQUIRED]
    rows[0].update(updates)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE future_prices (symbol TEXT, trade_date TEXT, close_price, ts)")
        conn.executemany("INSERT INTO future_prices VALUES (?, ?, ?, ?)", [
            (row["symbol"], "2026-08-20", row["close_price"], row["ts"]) for row in rows
        ])
    monkeypatch.setattr(repair, "DB", db)
    monkeypatch.setattr(repair.event_outcomes, "get_pending_quote_requirements",
                        lambda *args, **kwargs: {"pairs": [], "total": 0, "truncated": False})
    before = db.read_bytes()
    assert repair._missing("2026-08-20") == [repair.REQUIRED[0]]
    date, pairs, _pending = repair._select_pairs("same-day", "2026-08-20")
    assert date == "2026-08-20"
    assert pairs == [(repair.REQUIRED[0], "2026-08-20")]
    assert db.read_bytes() == before


def test_valid_completed_required_closes_are_not_selected(tmp_path, monkeypatch):
    db = tmp_path / "required-closes.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE future_prices (symbol TEXT, trade_date TEXT, close_price, ts)")
        conn.executemany("INSERT INTO future_prices VALUES (?, ?, ?, ?)", [
            (symbol.upper(), "2026-08-20", 10.0, _record(symbol)["ts"])
            for symbol in repair.REQUIRED
        ])
    monkeypatch.setattr(repair, "DB", db)
    assert repair._missing("2026-08-20") == []


def test_fetch_uses_sina_only_for_symbols_missing_from_tencent(monkeypatch):
    tencent = _record("sh000001", close_price=3905.23)
    sina = _record("sh000300", close_price=4600.0)
    sina_calls = []
    tencent_calls = []

    def fake_tencent(symbols, trade_date):
        tencent_calls.append((symbols, trade_date))
        return [tencent] if symbols == ["sh000001"] else []

    monkeypatch.setattr(repair, "fetch_tencent_daily_close_prices", fake_tencent)

    def fake_sina(symbol, trade_date):
        sina_calls.append((symbol, trade_date))
        return sina

    monkeypatch.setattr(repair, "_sina_daily", fake_sina)

    result = repair._fetch(["sh000001", "sh000300"], "2026-08-20")

    assert [row["symbol"] for row in result] == ["sh000001", "sh000300"]
    assert all(row["trade_date"] == "2026-08-20" for row in result)
    assert tencent_calls == [(["sh000001"], "2026-08-20"), (["sh000300"], "2026-08-20")]
    assert sina_calls == [("sh000300", "2026-08-20")]


@pytest.mark.parametrize(
    "invalid",
    [
        {"source_trade_date": "2026-08-19"},
        {"close_price": float("nan")},
        {"symbol": "sh600405"},
        {"ts": int(datetime.fromisoformat("2026-08-20T14:59:00+08:00").timestamp())},
    ],
)
def test_invalid_tencent_index_can_fall_through_to_valid_sina(monkeypatch, invalid):
    calls = []
    monkeypatch.setattr(
        repair,
        "fetch_tencent_daily_close_prices",
        lambda *a, **k: [_record("sh000300", **invalid)],
    )
    monkeypatch.setattr(
        repair,
        "_sina_daily",
        lambda symbol, date: calls.append((symbol, date)) or _record(symbol, date),
    )

    rows = repair._fetch(["sh000300"], "2026-08-20")

    assert len(rows) == 1
    assert rows[0]["symbol"] == "sh000300"
    assert rows[0]["close_price"] == 10.0
    assert calls == [("sh000300", "2026-08-20")]


def test_wrong_date_equity_and_invalid_sina_are_not_relabelled(monkeypatch):
    monkeypatch.setattr(
        repair,
        "fetch_tencent_daily_close_prices",
        lambda symbols, **k: [_record(symbols[0], "2026-08-19")],
    )
    calls = []
    monkeypatch.setattr(
        repair,
        "_sina_daily",
        lambda symbol, date: calls.append(symbol) or _record(symbol, "2026-08-19"),
    )

    assert repair._fetch(["sh600405", "sh000300"], "2026-08-20") == []
    assert calls == ["sh600405", "sh000300"]


def test_unsupported_symbol_is_visible_and_does_not_abort_next_pair(
    monkeypatch, caplog
):
    calls = []
    monkeypatch.setattr(
        repair,
        "fetch_tencent_daily_close_prices",
        lambda symbols, trade_date: calls.append(symbols)
        or [_record(symbols[0], trade_date)],
    )

    rows = repair._fetch(["usAAPL", "sh600405"], "2026-08-20")

    assert calls == [["sh600405"]]
    assert [row["symbol"] for row in rows] == ["sh600405"]
    assert "unsupported exact-date symbol usaapl/2026-08-20" in caplog.text


def test_deadline_checked_before_each_request_including_sina(monkeypatch):
    clock = [0.0]
    calls = []
    monkeypatch.setattr(repair.time, "monotonic", lambda: clock[0])

    def timeout_call(symbols, trade_date):
        calls.append(symbols)
        clock[0] = 121.0
        return []

    monkeypatch.setattr(repair, "fetch_tencent_daily_close_prices", timeout_call)
    monkeypatch.setattr(
        repair, "_sina_daily", lambda *a: pytest.fail("deadline must block fallback")
    )

    assert repair._fetch(["sh000300", "sh600405"], "2026-08-20", deadline=120.0) == []
    assert calls == [["sh000300"]]


def _main_environment(monkeypatch, pairs, *, required=(), total=None):
    """All reads/writes/fetches are mocks and paths use pytest's isolated DB."""
    written = []
    materialized = []
    requests = []
    monkeypatch.setattr(repair, "DB", Path(repair.database.DB_PATH))
    monkeypatch.setattr(repair, "_expected_trade_date", lambda now: "2026-08-20")
    monkeypatch.setattr(
        repair,
        "_missing",
        lambda date: [s for s in required if (s, date) not in written],
    )
    monkeypatch.setattr(
        repair.database,
        "store_future_prices",
        lambda rows: written.extend((r["symbol"], r["trade_date"]) for r in rows),
    )

    def requirements(date, *, limit, start_date=None):
        requests.append((date, limit, start_date))
        missing = [p for p in pairs if p not in written]
        if start_date is not None:
            missing = [p for p in missing if p[1] >= start_date]
        else:
            missing = [p for p in missing if p[1] <= date]
        count = len(missing) if total is None else max(0, int(total) - len(written))
        return {
            "pairs": [{"symbol": s, "trade_date": d} for s, d in missing[:limit]],
            "total": count,
            "truncated": count > limit,
        }

    monkeypatch.setattr(
        repair.event_outcomes,
        "get_pending_quote_requirements",
        requirements,
        raising=False,
    )
    monkeypatch.setattr(
        repair.event_outcomes,
        "materialize_event_outcomes",
        lambda date: materialized.append(date) or {"materialized": 2},
    )
    monkeypatch.setattr(
        repair,
        "fetch_tencent_daily_close_prices",
        lambda symbols, trade_date: [_record(symbols[0], trade_date)],
    )
    monkeypatch.setattr(repair, "_sina_daily", lambda *a: None)
    return written, materialized, requests


def test_post_close_repair_only_selects_same_day_pending(monkeypatch):
    pairs = [
        ("sh600405", "2026-08-05"),
        ("sh600405", "2026-08-20"),
        ("sh000300", "2026-08-07"),
    ]
    written, materialized, requests = _main_environment(monkeypatch, pairs)

    assert repair.main() == 0
    assert written == [("sh600405", "2026-08-20")]
    assert materialized == ["2026-08-20"]
    assert requests == [
        ("2026-08-20", 32, "2026-08-20"),
        ("2026-08-20", 32, "2026-08-20"),
    ]


def test_index_repair_is_preserved_and_deduplicated_with_pending_demand(monkeypatch):
    pair = ("sh000300", "2026-08-20")
    written, materialized, _ = _main_environment(
        monkeypatch, [pair], required=[pair[0]]
    )

    assert repair.main() == 0
    assert written == [pair]
    assert materialized == ["2026-08-20"]


def test_partial_success_persists_when_next_equity_request_fails(monkeypatch, caplog):
    pairs = [("sh688306", "2026-08-20"), ("sh600405", "2026-08-20")]
    written, materialized, _ = _main_environment(monkeypatch, pairs)

    def fetch(symbols, trade_date):
        if symbols == ["sh600405"]:
            raise TimeoutError("upstream timeout")
        return [_record(symbols[0], trade_date)]

    monkeypatch.setattr(repair, "fetch_tencent_daily_close_prices", fetch)

    assert repair.main() == 1
    assert written == [pairs[0]]
    assert materialized == ["2026-08-20"]
    assert "unresolved exact-date price sh600405/2026-08-20" in caplog.text


def test_main_budget_preserves_partial_success_and_reports_unattempted_pair(
    monkeypatch, caplog
):
    pairs = [("sh688306", "2026-08-20"), ("sh600405", "2026-08-20")]
    written, materialized, _ = _main_environment(monkeypatch, pairs)
    clock = [0.0]
    monkeypatch.setattr(repair.time, "monotonic", lambda: clock[0])

    def fetch(symbols, trade_date):
        clock[0] = 121.0
        return [_record(symbols[0], trade_date)]

    monkeypatch.setattr(repair, "fetch_tencent_daily_close_prices", fetch)

    assert repair.main() == 1
    assert written == [pairs[0]]
    assert materialized == ["2026-08-20"]
    assert "request budget exhausted before sh600405/2026-08-20" in caplog.text


def test_pending_pair_cap_cannot_report_all_green(monkeypatch, caplog):
    pairs = [(f"sh{600000 + i}", "2026-08-20") for i in range(40)]
    written, _, _ = _main_environment(monkeypatch, pairs)

    assert repair.main() == 1
    assert len(written) == 32
    assert "pending demand exceeded this run's 32-pair limit" in caplog.text
    assert "pending prices remain total=8" in caplog.text


def test_empty_or_unsupported_does_not_prove_nontrading(monkeypatch, caplog):
    pairs = [("bj920001", "2026-08-20"), ("sh688688", "2026-08-20"), ("usAAPL", "2026-08-20")]
    written, materialized, _ = _main_environment(monkeypatch, pairs)
    monkeypatch.setattr(
        repair,
        "fetch_tencent_daily_close_prices",
        lambda symbols, trade_date: [],
    )

    assert repair.main() == 1
    assert written == []
    assert materialized == []
    assert "unresolved exact-date price bj920001/2026-08-20" in caplog.text
    assert "unresolved exact-date price sh688688/2026-08-20" in caplog.text
    assert "unsupported exact-date symbol usaapl/2026-08-20" in caplog.text


def test_beijing_equity_uses_secondary_source_when_primary_empty(monkeypatch):
    pair = ('bj920021', '2026-08-20')
    written, _, _ = _main_environment(monkeypatch, [pair])
    monkeypatch.setattr(repair, 'fetch_tencent_daily_close_prices', lambda *a, **kw: [])
    monkeypatch.setattr(repair, '_sina_daily', lambda symbol, date: _record(symbol, date))
    assert repair.main() == 0
    assert written == [pair]


def test_no_missing_quotes_does_not_fetch_write_or_materialize(monkeypatch):
    written, materialized, _ = _main_environment(monkeypatch, [])
    monkeypatch.setattr(
        repair,
        "fetch_tencent_daily_close_prices",
        lambda *a, **k: pytest.fail("unexpected fetch"),
    )

    assert repair.main() == 0
    assert written == []
    assert materialized == []


def test_report_nonzero_if_a_write_does_not_resolve_demand(monkeypatch, caplog):
    _main_environment(monkeypatch, [("sh600405", "2026-08-20")])
    monkeypatch.setattr(repair.database, "store_future_prices", lambda rows: None)

    assert repair.main() == 1
    assert "pending prices remain total=1" in caplog.text


def test_refuse_split_read_and_write_database_paths(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(repair, "DB", tmp_path / "different.db")
    monkeypatch.setattr(
        repair, "_missing", lambda *a: pytest.fail("must reject before reading")
    )

    assert repair.main() == 1
    assert "read/write database paths differ" in caplog.text


def test_history_scope_ignores_same_day_pairs(monkeypatch, tmp_path):
    pairs = [
        ("sh600405", "2026-08-19"),
        ("sh600406", "2026-08-20"),
    ]
    written, materialized, requests = _main_environment(monkeypatch, pairs)
    monkeypatch.setattr(repair, "_previous_trading_day", lambda date: "2026-08-19")

    assert repair.main(["--scope", "history"]) == 0
    assert written == [("sh600405", "2026-08-19")]
    assert materialized == ["2026-08-20", "2026-08-19"]
    assert requests[0] == ("2026-08-19", 32, None)
    payload = json.loads(
        (tmp_path / "market_close" / "history_last.json").read_text(encoding="ascii")
    )
    assert payload["kind"] == "history"
    assert payload["status"] == "complete"
    assert payload["exit_code"] == 0


def test_history_unresolved_does_not_fail_the_job(monkeypatch, tmp_path):
    written, _, _ = _main_environment(monkeypatch, [("sh688688", "2026-08-19")])
    monkeypatch.setattr(repair, "_previous_trading_day", lambda date: "2026-08-19")
    monkeypatch.setattr(repair, "fetch_tencent_daily_close_prices", lambda *a, **k: [])
    monkeypatch.setattr(repair, "_sina_daily", lambda *a: None)

    assert repair.main(["--scope", "history"]) == 0
    assert written == []
    payload = json.loads(
        (tmp_path / "market_close" / "history_last.json").read_text(encoding="ascii")
    )
    assert payload["status"] == "incomplete"
    assert payload["unresolved"] == 1
    assert payload["exit_code"] == 0
