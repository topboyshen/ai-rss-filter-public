#!/usr/bin/env python3
"""Repair index closes and bounded missing frozen-signal prices after collection.

Primary: Tencent exact-date daily K.
Fallback: Sina timestamped session closes for SH/SZ/BJ (no extra packages).
Does not replace the 15:10 live job.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.a_stock_data import fetch_tencent_daily_close_prices  # noqa: E402
from modules.verified_close import fetch_sina_close_prices  # noqa: E402
from db import database  # noqa: E402
from services import event_outcomes  # noqa: E402
from workers.market_close_collector import _validated_record  # noqa: E402

LOG = logging.getLogger("market_close_repair")
TZ = ZoneInfo("Asia/Shanghai")
REQUIRED = ("sh000001", "sh000300", "sz399001", "sz399006")
NAMES = {
    "sh000001": "上证指数",
    "sh000300": "沪深300",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
}
DB = ROOT / "data" / "cache.db"
UA = "Mozilla/5.0 (compatible; ai-filter-repair/1.0)"
MAX_PENDING_PAIRS = 32
REQUEST_BUDGET_SECONDS = 120
EQUITY_RE = re.compile(r"(?:sh|sz|bj)\d{6}\Z")


def _expected_trade_date(now: datetime) -> str:
    day = now.date()
    if now.hour * 60 + now.minute < 15 * 60:
        day -= timedelta(days=1)
    for _ in range(15):
        if database._is_trading_day_local(day.isoformat()):
            return day.isoformat()
        day -= timedelta(days=1)
    raise SystemExit("cannot determine trade date")


def _previous_trading_day(trade_date: str) -> str:
    day = datetime.strptime(trade_date, "%Y-%m-%d").date() - timedelta(days=1)
    for _ in range(15):
        if database._is_trading_day_local(day.isoformat()):
            return day.isoformat()
        day -= timedelta(days=1)
    raise SystemExit("cannot determine previous trade date")


def _result_dir() -> Path:
    override = os.environ.get("MARKET_CLOSE_RESULT_DIR", "").strip()
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise ValueError("MARKET_CLOSE_RESULT_DIR must be absolute")
        return path
    return Path(DB).resolve().parent / "market_close"


def _persist_result(payload: dict) -> None:
    from services.market_close_result import write_result

    write_result(_result_dir(), **payload)


def _missing(trade_date: str) -> list[str]:
    if not DB.is_file():
        return list(REQUIRED)
    con = sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    evaluation_at = time.time()
    try:
        have = {
            str(r[0]).casefold()
            for r in con.execute(
                "SELECT lower(symbol), trade_date, close_price, ts FROM future_prices "
                "WHERE trade_date=? AND lower(symbol) IN (?,?,?,?)",
                (trade_date, *REQUIRED),
            )
            if event_outcomes.is_completed_close_price(
                r, trade_date, evaluation_at=evaluation_at,
            )
        }
    finally:
        con.close()
    return [s for s in REQUIRED if s not in have]


def _sina_daily(symbol: str, trade_date: str) -> dict | None:
    """Compatibility entry point; no date-only bars or fabricated timestamps."""
    rows = fetch_sina_close_prices([symbol], trade_date=trade_date)
    return rows[0] if rows else None


def _fetch(
    missing: list[str],
    trade_date: str,
    *,
    now_ts: int | None = None,
    deadline: float | None = None,
) -> list[dict]:
    """Validate each exact-date response; an invalid Tencent row is not success.

    The budget is checked before each network call. An already-running request
    may overrun the deadline by its existing 15-second transport timeout.
    """
    now_ts = int(time.time()) if now_ts is None else now_ts
    recs = []
    for symbol in dict.fromkeys(s.casefold() for s in missing):
        if EQUITY_RE.fullmatch(symbol) is None:
            LOG.warning(
                "[Repair] unsupported exact-date symbol %s/%s", symbol, trade_date
            )
            continue
        if deadline is not None and time.monotonic() >= deadline:
            break
        try:
            rows = fetch_tencent_daily_close_prices([symbol], trade_date=trade_date)
        except Exception as exc:
            LOG.warning(
                "[Repair] tencent daily %s/%s failed: %s",
                symbol,
                trade_date,
                str(exc)[:200],
            )
            rows = []
        valid = None
        for row in rows:
            candidate = _validated_record(row, trade_date, now_ts, {symbol})
            if candidate is None:
                LOG.warning(
                    "[Repair] rejected invalid/wrong-date Tencent row for %s/%s",
                    symbol,
                    trade_date,
                )
            elif valid is None or candidate["ts"] > valid["ts"]:
                valid = candidate
        if (
            valid is None
            and (deadline is None or time.monotonic() < deadline)
        ):
            try:
                row = _sina_daily(symbol, trade_date)
                valid = _validated_record(row, trade_date, now_ts, {symbol})
                if row is not None and valid is None:
                    LOG.warning(
                        "[Repair] rejected invalid/wrong-date Sina row for %s/%s",
                        symbol,
                        trade_date,
                    )
            except Exception as exc:
                LOG.warning(
                    "[Repair] sina daily %s/%s failed: %s",
                    symbol,
                    trade_date,
                    str(exc)[:200],
                )
        if valid is not None:
            recs.append(valid)
    return recs


def _select_pairs(scope: str, trade_date: str) -> tuple[str, list[tuple[str, str]], dict]:
    if scope == "history":
        history_date = _previous_trading_day(trade_date)
        pending = event_outcomes.get_pending_quote_requirements(
            history_date,
            limit=MAX_PENDING_PAIRS,
        )
        pairs = [(item["symbol"], item["trade_date"]) for item in pending["pairs"]]
        LOG.info(
            "[Repair] history pending through %s pairs=%d selected=%d truncated=%s",
            history_date,
            pending["total"],
            len(pairs),
            pending["truncated"],
        )
        return history_date, pairs, pending
    missing = _missing(trade_date)
    if not missing:
        LOG.info("[Repair] %s already has all required closes", trade_date)
    pending = event_outcomes.get_pending_quote_requirements(
        trade_date,
        start_date=trade_date,
        limit=MAX_PENDING_PAIRS,
    )
    LOG.info(
        "[Repair] same-day pending exact-date pairs=%d selected=%d truncated=%s",
        pending["total"],
        len(pending["pairs"]),
        pending["truncated"],
    )
    pairs = list(
        dict.fromkeys(
            [(symbol, trade_date) for symbol in missing]
            + [(item["symbol"], item["trade_date"]) for item in pending["pairs"]]
        )
    )
    return trade_date, pairs, pending


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description="Repair exact-date market closes")
    parser.add_argument(
        "--scope",
        choices=("same-day", "history"),
        default="same-day",
    )
    args = parser.parse_args([] if argv is None else argv)
    started_at = int(time.time())
    kind = "history" if args.scope == "history" else "repair"
    payload = {
        "kind": kind,
        "scope": args.scope,
        "status": "error",
        "started_at": started_at,
        "written": 0,
        "exit_code": 1,
    }
    if DB.resolve() != Path(database.DB_PATH).resolve():
        LOG.error(
            "[Repair] read/write database paths differ; refusing split-database repair"
        )
        payload["detail"] = "split database paths"
        _persist_result(payload)
        return 1
    deadline = time.monotonic() + REQUEST_BUDGET_SECONDS
    now = datetime.now(TZ)
    trade_date = _expected_trade_date(now)
    selected_date, pairs, pending = _select_pairs(args.scope, trade_date)
    payload["trade_date"] = selected_date
    payload["due_total"] = pending["total"]
    payload["truncated"] = bool(pending["truncated"])
    failed = bool(pending["truncated"]) if args.scope == "same-day" else False
    unresolved = 0
    written = 0
    for symbol, exact_date in pairs:
        if time.monotonic() >= deadline:
            LOG.error(
                "[Repair] request budget exhausted before %s/%s", symbol, exact_date
            )
            failed = True
            break
        recs = _fetch(
            [symbol], exact_date, now_ts=int(now.timestamp()), deadline=deadline
        )
        if not recs:
            # Empty/unsupported responses do not prove suspension or non-listing.
            # Keep the frozen endpoint visible; never turn missing data green.
            LOG.error("[Repair] unresolved exact-date price %s/%s", symbol, exact_date)
            unresolved += 1
            if args.scope == "same-day":
                failed = True
            continue
        try:
            database.store_future_prices(recs)
        except Exception as exc:
            LOG.error(
                "[Repair] write failed %s/%s: %s", symbol, exact_date, str(exc)[:200]
            )
            failed = True
            continue
        written += len(recs)
        LOG.info("[Repair] wrote %s/%s", symbol, exact_date)
    if written:
        dates = [trade_date]
        if selected_date != trade_date:
            dates.append(selected_date)
        try:
            for date in dates:
                result = event_outcomes.materialize_event_outcomes(date)
                LOG.info(
                    "[Repair] outcomes materialized=%d date=%s",
                    result.get("materialized", 0),
                    date,
                )
        except Exception as exc:
            LOG.error("[Repair] outcome materialization failed: %s", str(exc)[:200])
            failed = True
    remaining_kwargs = {"limit": MAX_PENDING_PAIRS}
    if args.scope == "same-day":
        still = _missing(trade_date)
        if still:
            LOG.error("[Repair] required closes still missing %s", ",".join(still))
            failed = True
        remaining_kwargs["start_date"] = trade_date
        remaining_target = trade_date
    else:
        remaining_target = selected_date
    remaining = event_outcomes.get_pending_quote_requirements(
        remaining_target,
        **remaining_kwargs,
    )
    if args.scope == "same-day" and (remaining["total"] or remaining["truncated"]):
        LOG.error(
            "[Repair] pending prices remain total=%d truncated=%s selected=%s",
            remaining["total"],
            remaining["truncated"],
            ",".join(f"{p['symbol']}/{p['trade_date']}" for p in remaining["pairs"]),
        )
        failed = True
    if pending["truncated"] and args.scope == "same-day":
        LOG.error(
            "[Repair] pending demand exceeded this run's %d-pair limit",
            MAX_PENDING_PAIRS,
        )
    if args.scope == "history" and (remaining["total"] or remaining["truncated"] or unresolved):
        LOG.info(
            "[Repair] historical pending remain total=%d truncated=%s unresolved=%d",
            remaining["total"],
            remaining["truncated"],
            unresolved,
        )
    status = "incomplete" if failed or remaining["total"] or remaining["truncated"] or unresolved else "complete"
    if args.scope == "history" and not failed:
        # Remaining historical demand is reported, not a same-day outage.
        exit_code = 0
    else:
        exit_code = 1 if failed else 0
        if failed:
            status = "incomplete"
    payload.update(
        {
            "status": status,
            "written": written,
            "unresolved": unresolved,
            "remaining_total": remaining["total"],
            "truncated": bool(pending["truncated"] or remaining["truncated"]),
            "completed_at": int(time.time()),
            "exit_code": exit_code,
        }
    )
    try:
        _persist_result(payload)
    except Exception as exc:
        LOG.error("[Repair] result file write failed: %s", str(exc)[:200])
        return 1
    LOG.info(
        "[Repair] wrote=%d status=%s scope=%s", written, status, args.scope
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
