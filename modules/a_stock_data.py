"""腾讯财经行情接口的最小、安全封装。"""

from __future__ import annotations

import logging
import math
import re
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
BAIDU_KLINE_URL = "https://finance.pae.baidu.com/selfselect/getstockquotation"
MAX_RESPONSE_BYTES = 512 * 1024
MAX_KLINE_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CODES_PER_REQUEST = 200
MAX_DAILY_CODES_PER_REQUEST = 20
_CODE_RE = re.compile(
    r"^(?:"
    r"(?:sh|sz|bj)\d{6}|"
    r"\d{6}(?:\.(?:sh|sz|bj))?|"
    r"us[A-Za-z0-9._-]{1,12}|"
    r"[A-Za-z][A-Za-z0-9._-]{0,11}"
    r")$",
    re.IGNORECASE,
)
_LINE_RE = re.compile(r'^v_([A-Za-z0-9._-]+)="([^"]*)"$')
_QUOTE_TIME_FORMATS = (
    (re.compile(r"^\d{14}$"), "%Y%m%d%H%M%S"),
    (re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"), "%Y-%m-%d %H:%M:%S"),
    (re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}$"), "%Y/%m/%d %H:%M:%S"),
)
_TRADE_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _open_tencent(request: urllib.request.Request, *, timeout: int):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RejectRedirects(),
    )
    return opener.open(request, timeout=timeout)


def _quote_timezone(symbol: str) -> ZoneInfo:
    key = symbol.casefold()
    if key.startswith("us"):
        return ZoneInfo("America/New_York")
    if key.startswith("hk"):
        return ZoneInfo("Asia/Hong_Kong")
    return ZoneInfo("Asia/Shanghai")


def _parse_quote_time(symbol: str, raw: str) -> tuple[int, str] | None:
    """Parse Tencent's exchange-local timestamp into epoch and market date."""
    value = str(raw or "").strip()
    for pattern, fmt in _QUOTE_TIME_FORMATS:
        if not pattern.fullmatch(value):
            continue
        try:
            parsed = datetime.strptime(value, fmt).replace(
                tzinfo=_quote_timezone(symbol)
            )
        except ValueError:
            return None
        return int(parsed.timestamp()), parsed.date().isoformat()
    return None


def _tencent_prefix(code: str) -> str:
    """校验用户代码并转换为腾讯接口前缀，拒绝查询字符串注入。"""
    if not isinstance(code, str):
        raise ValueError("行情代码必须是字符串")
    raw = code.strip()
    if not raw or not _CODE_RE.fullmatch(raw):
        raise ValueError(f"不支持的行情代码: {code!r}")

    upper = raw.upper()
    if "." in upper and upper[:6].isdigit():
        number, exchange = upper.split(".", 1)
        return exchange.lower() + number
    if upper.startswith("SH"):
        return "sh" + raw[2:]
    if upper.startswith("SZ"):
        return "sz" + raw[2:]
    if upper.startswith("BJ"):
        return "bj" + raw[2:]
    if upper.startswith("US"):
        return "us" + raw[2:]
    if upper.isdigit():
        if upper.startswith("920"):
            return "bj" + raw
        if upper.startswith(("6", "9")):
            return "sh" + raw
        if upper.startswith(("0", "3")):
            return "sz" + raw
        if upper.startswith(("4", "8")):
            return "bj" + raw
        raise ValueError(f"无法推断交易所: {code!r}")
    return "us" + raw


def _safe_float(value: str) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def tencent_quote(
    codes: List[str],
    *,
    opener: Callable[..., Any] = _open_tencent,
) -> Dict[str, dict]:
    """批量拉取腾讯财经实时行情，返回 {code: {...}}。"""
    if not codes:
        return {}
    if len(codes) > MAX_CODES_PER_REQUEST:
        raise ValueError(f"单次行情代码不能超过 {MAX_CODES_PER_REQUEST} 个")

    prefixed = [_tencent_prefix(code) for code in codes]
    requested = {code.casefold() for code in prefixed}
    url = TENCENT_QUOTE_URL + ",".join(prefixed)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept-Encoding": "identity"},
    )
    with opener(request, timeout=15) as response:
        if response.geturl() != url:
            raise ValueError("腾讯行情端点发生重定向，拒绝响应")
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("腾讯行情响应超过大小限制")
        data = raw.decode("gbk", errors="replace")

    result: Dict[str, dict] = {}
    for line in data.strip().split(";"):
        line = line.strip()
        if not line:
            continue
        match = _LINE_RE.fullmatch(line)
        if not match:
            logger.warning("[a-stock-data] 丢弃格式异常的腾讯行情记录")
            continue
        key, payload = match.groups()
        if key.casefold() not in requested:
            logger.warning("[a-stock-data] 丢弃未请求的行情代码: %s", key)
            continue

        values = payload.split("~")
        if len(values) < 53:
            logger.warning("[a-stock-data] %s 字段不足: %d", key, len(values))
            continue
        name = values[1].strip()
        price = _safe_float(values[3])
        last_close = _safe_float(values[4])
        change_amt = _safe_float(values[31])
        change_pct = _safe_float(values[32])
        quote_time = _parse_quote_time(key, values[30])
        open_price = _safe_float(values[5])
        volume = _safe_float(values[6])
        high_price = _safe_float(values[33])
        low_price = _safe_float(values[34])
        limit_up = _safe_float(values[47])
        limit_down = _safe_float(values[48])
        suspended = bool(
            last_close is not None
            and last_close > 0
            and (price is None or price <= 0)
            and (open_price is None or open_price == 0)
            and (high_price is None or high_price == 0)
            and (low_price is None or low_price == 0)
            and (volume is None or volume == 0)
        )
        if not name or last_close is None or last_close <= 0:
            logger.warning("[a-stock-data] %s 缺少有效名称/价格", key)
            continue
        if suspended:
            price = last_close
            change_amt = 0.0
            change_pct = 0.0
        if price is None or price <= 0 or change_amt is None or change_pct is None:
            logger.warning("[a-stock-data] %s 缺少有效涨跌数据", key)
            continue

        tolerance = max(0.0001, abs(float(price)) * 0.00001)
        if suspended:
            limit_state = "not_applicable"
            tradability_state = "suspended"
        elif limit_up and abs(float(price) - limit_up) <= tolerance:
            limit_state = "closed_limit_up"
            tradability_state = "limit_price_close"
        elif limit_down and abs(float(price) - limit_down) <= tolerance:
            limit_state = "closed_limit_down"
            tradability_state = "limit_price_close"
        else:
            limit_state = "not_at_limit"
            tradability_state = "trading_ohlc"

        code = key[2:] if key.casefold().startswith(("sh", "sz", "bj")) else key
        result[code] = {
            "name": name,
            "price": price,
            "last_close": last_close,
            "open": open_price,
            "change_amt": change_amt,
            "change_pct": change_pct,
            "high": high_price,
            "low": low_price,
            "volume": volume,
            "amount_wan": _safe_float(values[37]),
            "turnover_pct": _safe_float(values[38]),
            "pe_ttm": _safe_float(values[39]),
            "amplitude_pct": _safe_float(values[43]),
            "mcap_yi": _safe_float(values[44]),
            "float_mcap_yi": _safe_float(values[45]),
            "pb": _safe_float(values[46]),
            "limit_up": limit_up,
            "limit_down": limit_down,
            "suspension_state": "suspended" if suspended else "trading",
            "limit_state": limit_state,
            "tradability_state": tradability_state,
            "symbol": key,
            "quote_ts": quote_time[0] if quote_time else None,
            "quote_date": quote_time[1] if quote_time else None,
        }
    return result


def _tencent_daily_rows(
    item: dict[str, Any], trade_date: str
) -> tuple[list[Any], list[Any]] | None:
    """Return the exact target row and its preceding close without look-ahead."""
    raw_rows = item.get("day")
    if not isinstance(raw_rows, list):
        raise ValueError("腾讯日 K 响应缺少 day 数据")
    rows: list[tuple[str, list[Any]]] = []
    for raw in raw_rows:
        if not isinstance(raw, list) or len(raw) < 6:
            continue
        row_date = str(raw[0])
        if not _TRADE_DATE_RE.fullmatch(row_date):
            continue
        if row_date > trade_date:
            raise ValueError("腾讯日 K 响应包含目标日期之后的数据")
        rows.append((row_date, raw))
    rows.sort(key=lambda item: item[0])
    if len({row_date for row_date, _row in rows}) != len(rows):
        raise ValueError("腾讯日 K 响应包含重复交易日")
    for position, (row_date, row) in enumerate(rows):
        if row_date == trade_date:
            if position == 0:
                return None
            from db.database import _is_trading_day_local, _trading_day_after

            previous_date, previous_row = rows[position - 1]
            # The preceding available bar may conceal a missing session. Never
            # relabel a multi-session change as the requested day's return.
            try:
                if (
                    not _is_trading_day_local(previous_date)
                    or _trading_day_after(previous_date, 1) != trade_date
                ):
                    return None
            except ValueError:
                return None
            return row, previous_row
    return None


def _tencent_daily_record(
    symbol: str, item: dict[str, Any], trade_date: str
) -> dict | None:
    pair = _tencent_daily_rows(item, trade_date)
    if pair is None:
        return None
    row, previous = pair
    open_price = _safe_float(row[1])
    close_price = _safe_float(row[2])
    high_price = _safe_float(row[3])
    low_price = _safe_float(row[4])
    volume = _safe_float(row[5])
    prev_close = _safe_float(previous[2]) if len(previous) > 2 else None
    prices = (open_price, close_price, high_price, low_price, prev_close)
    if any(value is None or value <= 0 for value in prices):
        raise ValueError("腾讯日 K 响应包含无效价格")
    if volume is None or volume < 0:
        raise ValueError("腾讯日 K 响应包含无效成交量")

    name = symbol
    quotes = item.get("qt")
    if isinstance(quotes, dict):
        quote = quotes.get(symbol)
        if isinstance(quote, list) and len(quote) > 1 and str(quote[1]).strip():
            name = str(quote[1]).strip()
    close_at = int(
        datetime.strptime(trade_date + " 15:00:00", "%Y-%m-%d %H:%M:%S")
        .replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        .timestamp()
    )
    change_amt = float(close_price) - float(prev_close)
    return {
        "symbol": symbol,
        "trade_date": trade_date,
        "source_trade_date": trade_date,
        "name": name,
        "close_price": float(close_price),
        "change_amt": change_amt,
        "change_pct": change_amt / float(prev_close) * 100.0,
        "open_price": float(open_price),
        "high_price": float(high_price),
        "low_price": float(low_price),
        "prev_close": float(prev_close),
        "volume": float(volume),
        "amount": None,
        "limit_up": None,
        "limit_down": None,
        "suspension_state": "trading",
        "limit_state": "not_applicable",
        "tradability_state": "trading_ohlc",
        "ts": close_at,
    }


def fetch_tencent_daily_close_prices(
    codes: List[str],
    *,
    trade_date: str,
    opener: Callable[..., Any] = _open_tencent,
) -> List[dict]:
    """Fetch exact-date A-share daily closes from Tencent's bounded K-line API."""
    if not _TRADE_DATE_RE.fullmatch(str(trade_date or "")):
        raise ValueError("trade_date must be YYYY-MM-DD")
    target = datetime.strptime(trade_date, "%Y-%m-%d")
    if len(codes) > MAX_DAILY_CODES_PER_REQUEST:
        raise ValueError(f"单次日 K 行情代码不能超过 {MAX_DAILY_CODES_PER_REQUEST} 个")
    start_date = (target.date() - timedelta(days=40)).isoformat()
    records: List[dict] = []
    for code in codes:
        symbol = _tencent_prefix(code).casefold()
        if not re.fullmatch(r"(?:sh|sz)\d{6}", symbol):
            raise ValueError(f"日 K 行情仅支持沪深证券: {code!r}")
        params = {
            "param": f"{symbol},day,{start_date},{trade_date},40",
        }
        url = TENCENT_KLINE_URL + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
        )
        with opener(request, timeout=15) as response:
            if response.geturl() != url:
                raise ValueError("腾讯日 K 端点发生重定向，拒绝响应")
            raw = response.read(MAX_KLINE_RESPONSE_BYTES + 1)
        if len(raw) > MAX_KLINE_RESPONSE_BYTES:
            raise ValueError("腾讯日 K 响应超过大小限制")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("腾讯日 K 响应不是有效 JSON") from exc
        if not isinstance(document, dict) or str(document.get("code")) != "0":
            raise ValueError("腾讯日 K 返回失败状态")
        data = document.get("data")
        if not isinstance(data, dict):
            raise ValueError("腾讯日 K 响应缺少 data")
        item = data.get(symbol)
        if not isinstance(item, dict):
            raise ValueError(f"腾讯日 K 响应缺少 {symbol}")
        record = _tencent_daily_record(symbol, item, trade_date)
        if record is not None:
            records.append(record)
    return records


def fetch_index_close_prices(codes: List[str]) -> List[dict]:
    """为收盘采集器拉取指数/ETF 价格并返回标准化记录。"""
    quotes = tencent_quote(codes)
    records = []
    for code in codes:
        quote = (
            quotes.get(code)
            or quotes.get(code.casefold())
            or quotes.get(_tencent_prefix(code))
        )
        if not quote:
            for value in quotes.values():
                symbol = value["symbol"].casefold()
                if symbol.endswith(code.casefold()) or symbol == code.casefold():
                    quote = value
                    break
        if not quote:
            logger.warning("[a-stock-data] 未获取到 %s 行情", code)
            continue
        records.append(
            {
                "symbol": quote["symbol"],
                "name": quote["name"],
                "close_price": quote["price"],
                "prev_close": quote["last_close"],
                "open_price": quote.get("open"),
                "high_price": quote.get("high"),
                "low_price": quote.get("low"),
                "volume": quote.get("volume"),
                "amount": quote.get("amount_wan"),
                "limit_up": quote.get("limit_up"),
                "limit_down": quote.get("limit_down"),
                "suspension_state": quote.get("suspension_state", "unknown"),
                "limit_state": quote.get("limit_state", "unknown"),
                "tradability_state": quote.get("tradability_state", "unknown"),
                "change_amt": quote["change_amt"],
                "change_pct": quote["change_pct"],
                "ts": quote.get("quote_ts") or 0,
                "source_trade_date": quote.get("quote_date") or "",
            }
        )
    return records


def fetch_historical_daily_prices(
    code: str,
    *,
    start_date: str,
    is_index: bool = False,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> List[dict]:
    """Fetch a bounded historical daily OHLC series from one fixed endpoint."""
    symbol = _tencent_prefix(code).casefold()
    ticker = symbol[2:]
    if not _TRADE_DATE_RE.fullmatch(str(start_date or "")):
        raise ValueError("start_date must be YYYY-MM-DD")
    datetime.strptime(start_date, "%Y-%m-%d")
    params = {
        "all": "1",
        "isIndex": "true" if is_index else "false",
        "isBk": "false",
        "isBlock": "false",
        "isFutures": "false",
        "isStock": "false" if is_index else "true",
        "newFormat": "1",
        "group": "quotation_kline_ab",
        "finClientType": "pc",
        "code": ticker,
        "start_time": start_date,
        "ktype": "1",
    }
    url = BAIDU_KLINE_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/vnd.finance-web.v1+json",
            "Accept-Encoding": "identity",
            "Origin": "https://gushitong.baidu.com",
            "Referer": "https://gushitong.baidu.com/",
        },
    )
    with opener(request, timeout=15) as response:
        if response.geturl() != url:
            raise ValueError("百度历史行情端点发生重定向，拒绝响应")
        raw = response.read(MAX_KLINE_RESPONSE_BYTES + 1)
    if len(raw) > MAX_KLINE_RESPONSE_BYTES:
        raise ValueError("百度历史行情响应超过大小限制")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("百度历史行情响应不是有效 JSON") from exc
    if str(document.get("ResultCode")) != "0":
        raise ValueError("百度历史行情返回失败状态")
    market = (document.get("Result") or {}).get("newMarketData") or {}
    keys = market.get("keys") or []
    if not isinstance(keys, list) or not isinstance(market.get("marketData"), str):
        raise ValueError("百度历史行情缺少日 K 数据")
    required = {"time", "open", "close", "volume", "high", "low", "amount", "preClose"}
    if not required.issubset(set(keys)):
        raise ValueError("百度历史行情字段不完整")
    records: List[dict] = []
    for line in market["marketData"].split(";"):
        if not line:
            continue
        values = line.split(",")
        if len(values) != len(keys):
            continue
        row = dict(zip(keys, values))
        trade_date = str(row["time"])
        if trade_date < start_date or not _TRADE_DATE_RE.fullmatch(trade_date):
            continue
        try:
            close_price = float(row["close"])
            open_price = float(row["open"])
            high_price = float(row["high"])
            low_price = float(row["low"])
            prev_close = float(row["preClose"])
            volume_value = float(row["volume"])
            amount_value = float(row["amount"])
        except (TypeError, ValueError):
            continue
        numeric = (
            close_price,
            open_price,
            high_price,
            low_price,
            prev_close,
            volume_value,
            amount_value,
        )
        if (
            not all(math.isfinite(value) for value in numeric)
            or min(close_price, open_price, high_price, low_price, prev_close) <= 0
        ):
            continue
        close_at = int(
            datetime.strptime(trade_date + " 15:00:00", "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            .timestamp()
        )
        change_amt = close_price - prev_close
        records.append(
            {
                "symbol": symbol,
                "trade_date": trade_date,
                "name": ticker,
                "close_price": close_price,
                "change_amt": change_amt,
                "change_pct": change_amt / prev_close * 100.0,
                "open_price": open_price,
                "high_price": high_price,
                "low_price": low_price,
                "prev_close": prev_close,
                "volume": volume_value,
                "amount": amount_value,
                "limit_up": None,
                "limit_down": None,
                "suspension_state": "trading",
                "limit_state": "unknown_historical",
                "tradability_state": "trading_ohlc",
                "ts": close_at,
            }
        )
    if len(records) > 2500:
        raise ValueError("百度历史行情记录数超过限制")
    return records
