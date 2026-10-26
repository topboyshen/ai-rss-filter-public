"""Bounded secondary close source, using provider timestamps, never invented 15:00s."""
from __future__ import annotations

import json
import math
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from modules.a_stock_data import UA, _open_tencent, _tencent_prefix

SINA_BARS_URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData"
)
MAX_BARS = 240
MAX_BYTES = 200_000
MARKET_TZ = ZoneInfo("Asia/Shanghai")
SYMBOL_RE = re.compile(r"(?:sh|sz|bj)\d{6}\Z")
SOURCE_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2} 15:00:00\Z")


def _closing_bars(rows: object, trade_date: str, now: int) -> list[tuple[str, int, float]]:
    """Ignore non-close/future bars; duplicate close dates are ambiguous, not evidence."""
    if not isinstance(rows, list) or len(rows) > MAX_BARS:
        raise ValueError("invalid or oversized secondary bar list")
    closes = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = row.get("day")
        if not isinstance(day, str) or not SOURCE_TIME_RE.fullmatch(day):
            continue
        try:
            observed = datetime.strptime(day, "%Y-%m-%d %H:%M:%S").replace(tzinfo=MARKET_TZ)
            price = float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        date = observed.date().isoformat()
        timestamp = int(observed.timestamp())
        if date > trade_date or timestamp > now:
            continue
        if date in closes:
            raise ValueError("duplicate secondary close date")
        if isinstance(row["close"], bool) or not math.isfinite(price) or price <= 0:
            raise ValueError("invalid secondary close price")
        closes[date] = (date, timestamp, price)
    return sorted(closes.values())


def _close_record(symbol: str, rows: object, trade_date: str, now: int) -> dict | None:
    closes = _closing_bars(rows, trade_date, now)
    if len(closes) < 2 or closes[-1][0] != trade_date:
        return None
    from db.database import _is_trading_day_local, _latest_trading_day
    # A missing intervening session cannot be relabelled as yesterday's close.
    previous_date = _latest_trading_day(datetime.fromisoformat(trade_date) - timedelta(days=1))
    if not _is_trading_day_local(trade_date) or closes[-2][0] != previous_date:
        return None
    _, timestamp, price = closes[-1]
    previous = closes[-2][2]
    # A final hourly close is the closing price, not a full daily OHLC bar.
    # Do not pass its hourly volume/high/low off as daily observations.
    return {
        "symbol": symbol, "name": symbol, "trade_date": trade_date,
        "source_trade_date": trade_date, "ts": timestamp,
        "close_price": price, "prev_close": previous,
        "change_amt": price - previous,
        "change_pct": (price - previous) / previous * 100.0,
        "suspension_state": "unknown", "limit_state": "unknown",
        "tradability_state": "unknown", "source": "sina_timestamped_close",
    }


def fetch_sina_close_prices(
    codes: list[str], *, trade_date: str, now_ts: int | None = None,
    opener=_open_tencent,
) -> list[dict]:
    """Read exact-date SH/SZ/BJ closes from time-stamped hourly bars.

    Sina's scale=240 daily rows only contain dates. They are deliberately NOT
    used here: scale=60 supplies the actual final-session timestamp. The bounded
    window cannot repair arbitrary history; missing dates remain missing.
    """
    target = datetime.strptime(trade_date, "%Y-%m-%d")
    if target.strftime("%Y-%m-%d") != trade_date or len(codes) > 20:
        raise ValueError("invalid date or too many secondary symbols")
    now = int(time.time()) if now_ts is None else now_ts
    symbols = list(dict.fromkeys(_tencent_prefix(code).casefold() for code in codes))
    if any(not SYMBOL_RE.fullmatch(symbol) for symbol in symbols):
        raise ValueError("secondary close supports SH/SZ/BJ only")
    records = []
    for symbol in symbols:
        params = urllib.parse.urlencode({
            "symbol": symbol, "scale": 60, "ma": "no", "datalen": MAX_BARS,
        })
        url = SINA_BARS_URL + "?" + params
        request = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept": "application/json", "Accept-Encoding": "identity",
        })
        with opener(request, timeout=15) as response:
            if response.geturl() != url:
                raise ValueError("secondary close redirect rejected")
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("secondary close response too large")
        rows = json.loads(raw.decode("utf-8"))
        record = _close_record(symbol, rows, trade_date, now)
        if record is not None:
            records.append(record)
    return records
