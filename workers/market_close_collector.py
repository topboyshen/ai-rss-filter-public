#!/usr/bin/env python3
"""收盘行情采集器，由显式 cron profile 在 A 股收盘后调用。"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import database
from services.admission_fence import guarded
from modules.verified_close import fetch_sina_close_prices
from modules.a_stock_data import (
    fetch_index_close_prices,
    fetch_tencent_daily_close_prices,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("market_close_collector")

SYMBOLS = [
    "sh000001",
    "sh000300",
    "sz399001",
    "sz399006",
]
_REQUIRED_SYMBOLS = {symbol.casefold() for symbol in SYMBOLS}
FETCH_ATTEMPTS = 3
FETCH_RETRY_DELAYS_SECONDS = (5, 15)
# Pending frozen observations get their own budget, independent of the newest
# event universe. Overflow is reported as incomplete, never silently discarded.
PENDING_SYMBOL_LIMIT = 500
DAILY_PENDING_SYMBOL_LIMIT = 32
DAILY_FALLBACK_BUDGET_SECONDS = 120
_MARKET_TZ = ZoneInfo("Asia/Shanghai")
_EARLIEST_VALID_CLOSE_MINUTE = 15 * 60


def _today_local() -> str:
    """Return the Beijing calendar date independent of the process TZ."""
    return datetime.now(_MARKET_TZ).strftime("%Y-%m-%d")


def _validated_trade_date(trade_date: str | None) -> str:
    today = _today_local()
    target = trade_date or today
    try:
        parsed = datetime.strptime(target, "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise ValueError("trade_date 必须是 YYYY-MM-DD 格式") from exc
    if parsed.strftime("%Y-%m-%d") != target:
        raise ValueError("trade_date 必须是规范的 YYYY-MM-DD 格式")
    if target != today:
        raise ValueError("腾讯实时接口不支持历史日期回填")
    return target


def _validated_record(
    record: Any,
    target_date: str,
    now: int,
    allowed_symbols: set[str] | None = None,
    rejects: dict[str, str] | None = None,
) -> dict | None:
    def reject(reason: str) -> None:
        symbol = record.get("symbol") if isinstance(record, dict) else None
        logger.warning(
            "[MarketClose] 行情拒收 symbol=%r date=%s reason=%s",
            str(symbol)[:24], target_date, reason,
        )
        if rejects is not None and isinstance(symbol, str) and symbol.strip():
            rejects[symbol.casefold()] = reason
        return None

    if not isinstance(record, dict):
        return reject("not_a_record")
    required = (
        "symbol",
        "name",
        "close_price",
        "change_amt",
        "change_pct",
        "ts",
        "source_trade_date",
    )
    if any(key not in record for key in required):
        return reject("missing_fields")

    symbol = record["symbol"]
    name = record["name"]
    permitted = allowed_symbols or _REQUIRED_SYMBOLS
    if not isinstance(symbol, str) or symbol.casefold() not in permitted:
        return reject("unexpected_symbol")
    if not isinstance(name, str) or not name.strip():
        return reject("missing_name")

    numeric = {}
    for key in ("close_price", "change_amt", "change_pct"):
        value = record[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return reject("invalid_" + key)
        value = float(value)
        if not math.isfinite(value):
            return reject("nonfinite_" + key)
        numeric[key] = value
    if numeric["close_price"] <= 0:
        return reject("nonpositive_close")

    timestamp = record["ts"]
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, int)
        or timestamp <= 0
        or timestamp > now + 300
    ):
        return reject("invalid_timestamp")
    if record["source_trade_date"] != target_date:
        return reject("wrong_source_date")
    source_time = datetime.fromtimestamp(timestamp, _MARKET_TZ)
    if source_time.date().isoformat() != target_date:
        return reject("wrong_timestamp_date")
    if source_time.hour * 60 + source_time.minute < _EARLIEST_VALID_CLOSE_MINUTE:
        return reject("before_close")

    return {
        "symbol": symbol.casefold(),
        "trade_date": target_date,
        "name": name.strip(),
        "close_price": numeric["close_price"],
        "change_amt": numeric["change_amt"],
        "change_pct": numeric["change_pct"],
        "open_price": record.get("open_price"),
        "high_price": record.get("high_price"),
        "low_price": record.get("low_price"),
        "prev_close": record.get("prev_close"),
        "volume": record.get("volume"),
        "amount": record.get("amount"),
        "limit_up": record.get("limit_up"),
        "limit_down": record.get("limit_down"),
        "suspension_state": str(record.get("suspension_state") or "unknown"),
        "limit_state": str(record.get("limit_state") or "unknown"),
        "tradability_state": str(record.get("tradability_state") or "unknown"),
        "ts": timestamp,
    }


def _fallback_close(symbol: str, target_date: str, deadline: float) -> dict | None:
    """Try both bounded sources; invalid/empty primary data is not suspension."""
    for provider in (fetch_tencent_daily_close_prices, fetch_sina_close_prices):
        if time.monotonic() >= deadline:
            logger.warning("[MarketClose] 日 K 时间额度耗尽，剩余端点留给补采")
            return None
        try:
            prices = provider([symbol], trade_date=target_date)
        except Exception as exc:
            logger.warning("[MarketClose] 日 K %s 请求失败: %s", symbol, str(exc)[:200])
            continue
        for price in prices:
            record = _validated_record(price, target_date, int(time.time()), {symbol})
            if record is not None:
                logger.info("[MarketClose] 日 K 恢复 %s %s", symbol, target_date)
                return record
    return None


def _note_result(result: dict[str, Any] | None, **fields: Any) -> None:
    if result is not None:
        result.update(fields)


@guarded("market_close", kind="intake")
def run(trade_date: str | None = None, *, result: dict[str, Any] | None = None) -> int:
    target_date = _validated_trade_date(trade_date)
    if not database._is_trading_day_local(target_date):
        logger.info("[MarketClose] %s 非交易日，跳过采集", target_date)
        _note_result(result, status="skipped_nontrading", trade_date=target_date)
        return 0

    from services.event_outcomes import (
        get_pending_quote_requirements,
        materialize_event_outcomes,
    )

    now = int(time.time())
    dynamic = database.get_event_quote_universe(since=now - 120 * 86400, limit=500)
    pending = get_pending_quote_requirements(
        target_date, start_date=target_date, limit=PENDING_SYMBOL_LIMIT,
    )
    pending_symbols = {item["symbol"].casefold() for item in pending["pairs"]}
    symbols = list(dict.fromkeys(
        SYMBOLS + sorted(pending_symbols)
        + [item["symbol"].casefold() for item in dynamic]
    ))
    allowed_symbols = {symbol.casefold() for symbol in symbols}
    tracked_symbols = _REQUIRED_SYMBOLS | pending_symbols
    logger.info(
        "[MarketClose] %s 待结算端点=%d 本轮=%d truncated=%s 总请求证券=%d",
        target_date, pending["total"], len(pending_symbols), pending["truncated"],
        len(symbols),
    )
    records_by_symbol: dict[str, dict] = {}
    rejects: dict[str, str] = {}
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        prices = []
        for offset in range(0, len(symbols), 200):
            try:
                prices.extend(fetch_index_close_prices(symbols[offset : offset + 200]))
            except Exception as exc:
                # A later failed chunk must not erase earlier successful chunks.
                logger.warning(
                    "[MarketClose] 第 %d/%d 次行情请求批次 %d 失败: %s",
                    attempt, FETCH_ATTEMPTS, offset // 200 + 1, str(exc)[:200],
                )
        for price in prices:
            record = _validated_record(
                price, target_date, now, allowed_symbols, rejects=rejects,
            )
            if record is None:
                continue
            key = record["symbol"].casefold()
            previous = records_by_symbol.get(key)
            if previous is None or record["ts"] > previous["ts"]:
                records_by_symbol[key] = record
        missing_tracked = tracked_symbols - set(records_by_symbol)
        if not missing_tracked:
            break
        if attempt < FETCH_ATTEMPTS:
            delay = FETCH_RETRY_DELAYS_SECONDS[attempt - 1]
            logger.warning(
                "[MarketClose] 行情批次不完整，缺少 %s；%d 秒后重试",
                ",".join(sorted(missing_tracked)[:20]),
                delay,
            )
            time.sleep(delay)

    missing_required = _REQUIRED_SYMBOLS - set(records_by_symbol)
    missing_pending = pending_symbols - _REQUIRED_SYMBOLS - set(records_by_symbol)
    fallback_symbols = sorted(missing_required) + sorted(missing_pending)[
        :DAILY_PENDING_SYMBOL_LIMIT
    ]
    if fallback_symbols:
        logger.warning(
            "[MarketClose] 实时缺指数=%d 待结算个股=%d；日 K 本轮最多 %d 只",
            len(missing_required), len(missing_pending), len(fallback_symbols),
        )
        deadline = time.monotonic() + DAILY_FALLBACK_BUDGET_SECONDS
        for symbol in fallback_symbols:
            # Budget is checked between calls; one in-flight request retains
            # the existing 15-second transport timeout.
            if time.monotonic() >= deadline:
                logger.warning("[MarketClose] 日 K 时间额度耗尽，剩余端点留给补采")
                break
            record = _fallback_close(symbol, target_date, deadline)
            if record is not None:
                records_by_symbol[symbol] = record

    missing_required = _REQUIRED_SYMBOLS - set(records_by_symbol)
    if missing_required:
        _note_result(
            result,
            status="error",
            trade_date=target_date,
            due_total=pending["total"],
            unresolved=len(missing_required),
            truncated=bool(pending["truncated"]),
            written=0,
            detail="missing required closes",
        )
        raise RuntimeError(
            "[MarketClose] 完整性校验失败，未写入；缺少 "
            + ",".join(sorted(missing_required))
        )

    missing_pending = pending_symbols - set(records_by_symbol)
    # A before-close/nonpositive quote is not authoritative non-trading evidence.
    optional_missing = allowed_symbols - tracked_symbols - set(records_by_symbol)
    if optional_missing:
        logger.warning(
            "[MarketClose] %d 只其他事件证券缺少当日行情，示例=%s",
            len(optional_missing), ",".join(sorted(optional_missing)[:20]),
        )

    records = [
        records_by_symbol[symbol.casefold()]
        for symbol in symbols
        if symbol.casefold() in records_by_symbol
    ]

    database.store_future_prices(records)
    logger.info(
        "[MarketClose] 已写入 %d 条 future_prices: %s", len(records), target_date
    )
    outcome_result = materialize_event_outcomes(target_date)
    logger.info(
        "[MarketClose] 事件后验候选=%d 持久系列=%d",
        int(outcome_result.get("eligible") or 0),
        int(outcome_result.get("materialized") or 0),
    )
    if missing_pending or pending["truncated"]:
        _note_result(
            result,
            status="incomplete",
            trade_date=target_date,
            due_total=pending["total"],
            unresolved=len(missing_pending),
            truncated=bool(pending["truncated"]),
            written=len(records),
        )
        raise RuntimeError(
            "[MarketClose] 指数完整但待结算覆盖未完成；有效行情已保存；"
            f"missing={len(missing_pending)} truncated={pending['truncated']} "
            + ",".join(sorted(missing_pending)[:20])
        )
    logger.info("[MarketClose] 当日待结算端点完整；历史缺口由精确日期补采处理")
    _note_result(
        result,
        status="success",
        trade_date=target_date,
        due_total=pending["total"],
        unresolved=0,
        truncated=False,
        written=len(records),
    )
    return len(records)


def _persist_collector_result(payload: dict[str, Any]) -> None:
    from services.market_close_result import result_dir_from_env, write_result

    directory = result_dir_from_env(db_path=Path(database.DB_PATH))
    write_result(directory, **payload)


@guarded("market_close_command", kind="intake")
def main() -> None:
    parser = argparse.ArgumentParser(description="收盘行情采集器")
    parser.add_argument("--trade-date", help="指定交易日（仅允许最新交易日）")
    args = parser.parse_args()
    payload: dict[str, Any] = {
        "kind": "collector",
        "status": "error",
        "started_at": int(time.time()),
    }
    try:
        written = run(args.trade_date, result=payload)
        payload.setdefault("written", written)
        if payload.get("status") not in {
            "skipped_nontrading",
            "success",
            "incomplete",
            "error",
        }:
            payload["status"] = "success"
    except ValueError as exc:
        payload["detail"] = str(exc)[:200]
        parser.error(str(exc))
    finally:
        payload["completed_at"] = int(time.time())
        if payload.get("status") != "skipped_nontrading":
            try:
                _persist_collector_result(payload)
            except Exception:
                logger.exception("[MarketClose] 未能写入采集结果文件")


if __name__ == "__main__":
    main()
