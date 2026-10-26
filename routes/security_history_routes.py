"""Authenticated, server-rendered stock event history and daily K-line page."""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, render_template, request, url_for

from routes.browser_auth import (
    apply_browser_security_headers,
    browser_error,
    dashboard_login_redirect,
    get_dashboard_auth,
    request_prefers_html,
)
from routes.security import rate_limit
from services.release_info import release_version
from services.security_history_query import (
    SecurityHistoryNotFoundError,
    SecurityHistoryQueryError,
    SecurityHistoryUnavailableError,
    load_security_history,
)


logger = logging.getLogger(__name__)
security_history_bp = Blueprint("security_history", __name__)
_MARKET_TZ = ZoneInfo("Asia/Shanghai")
_CHART_WIDTH = 1040
_CHART_HEIGHT = 430
_PLOT_LEFT = 70
_PLOT_RIGHT = 22
_PLOT_TOP = 55
_PLOT_BOTTOM = 52


def _time_label(value: object) -> str:
    try:
        timestamp = int(value or 0)
        if timestamp <= 0:
            return "时间未知"
        return datetime.fromtimestamp(timestamp, _MARKET_TZ).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "时间未知"


def _event_type(value: object) -> str:
    text = str(value or "").strip()
    if (
        not text
        or "�" in text
        or text.casefold() in {"unknown", "none", "null", "未知"}
        or re.fullmatch(r"-?\d+(?:\.\d+)?", text)
    ):
        return "其他事件"
    return text[:64]


def _signed(
    value: object,
    *,
    percent: bool = False,
    percentage_points: bool = False,
) -> str | None:
    try:
        parsed = float(value) * (100 if percent else 1)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    suffix = "%" if percent or percentage_points else ""
    return f"{parsed:+.2f}".replace("-", "−") + suffix


def _confidence(value: object) -> str:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return "未知"
    return f"{parsed * 100:.0f}%" if math.isfinite(parsed) else "未知"


def _direction_class(value: object) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized in {"看多", "利多", "bullish", "positive", "up", "long"}:
        return "bullish"
    if normalized in {"看空", "利空", "bearish", "negative", "down", "short"}:
        return "bearish"
    return "neutral"


def _price_label(value: object) -> str:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(parsed):
        return "—"
    return f"{parsed:.2f}"


def _chart_model(
    prices: list[dict[str, Any]], events: list[dict[str, Any]]
) -> dict[str, Any] | None:
    valid: list[dict[str, Any]] = []
    for row in prices:
        try:
            open_price = float(row["open_price"])
            high_price = float(row["high_price"])
            low_price = float(row["low_price"])
            close_price = float(row["close_price"])
        except (TypeError, ValueError):
            continue
        if (
            not all(math.isfinite(item) and item > 0 for item in (open_price, high_price, low_price, close_price))
            or high_price < max(open_price, close_price, low_price)
            or low_price > min(open_price, close_price, high_price)
        ):
            continue
        item = dict(row)
        item.update(
            {
                "open_price": open_price,
                "high_price": high_price,
                "low_price": low_price,
                "close_price": close_price,
            }
        )
        valid.append(item)
    if not valid:
        return None

    plot_width = _CHART_WIDTH - _PLOT_LEFT - _PLOT_RIGHT
    plot_height = _CHART_HEIGHT - _PLOT_TOP - _PLOT_BOTTOM
    low = min(float(item["low_price"]) for item in valid)
    high = max(float(item["high_price"]) for item in valid)
    padding = max((high - low) * 0.08, high * 0.005, 0.01)
    chart_low = max(0.0, low - padding)
    chart_high = high + padding
    span = max(chart_high - chart_low, 0.01)

    def y(value: float) -> float:
        return _PLOT_TOP + (chart_high - value) / span * plot_height

    step = plot_width / max(1, len(valid))
    candle_width = max(2.0, min(9.0, step * 0.62))
    candles: list[dict[str, Any]] = []
    x_by_date: dict[str, float] = {}
    for index, item in enumerate(valid):
        x = _PLOT_LEFT + step * index + step / 2
        open_y = y(float(item["open_price"]))
        close_y = y(float(item["close_price"]))
        date = str(item["trade_date"])
        x_by_date[date] = x
        candles.append(
            {
                "x": round(x, 2),
                "body_x": round(x - candle_width / 2, 2),
                "body_width": round(candle_width, 2),
                "high_y": round(y(float(item["high_price"])), 2),
                "low_y": round(y(float(item["low_price"])), 2),
                "body_y": round(min(open_y, close_y), 2),
                "body_height": round(max(1.2, abs(open_y - close_y)), 2),
                "direction": (
                    "up"
                    if float(item["close_price"]) > float(item["open_price"])
                    else "down"
                    if float(item["close_price"]) < float(item["open_price"])
                    else "flat"
                ),
                "trade_date": date,
                "open": _price_label(item["open_price"]),
                "high": _price_label(item["high_price"]),
                "low": _price_label(item["low_price"]),
                "close": _price_label(item["close_price"]),
                "tradability_state": str(item.get("tradability_state") or "unknown"),
                "limit_state": str(item.get("limit_state") or "unknown"),
                "suspension_state": str(item.get("suspension_state") or "unknown"),
            }
        )

    ticks: list[dict[str, Any]] = []
    for index in range(5):
        value = chart_high - span * index / 4
        ticks.append({"y": round(y(value), 2), "label": _price_label(value)})

    date_ticks: list[dict[str, Any]] = []
    desired = min(6, len(valid))
    indexes = sorted(
        {round(index * (len(valid) - 1) / max(1, desired - 1)) for index in range(desired)}
    )
    for index in indexes:
        date_ticks.append(
            {"x": candles[index]["x"], "label": candles[index]["trade_date"][5:]}
        )

    markers: list[dict[str, Any]] = []
    lanes: dict[str, int] = {}
    for event in events:
        date = str(event.get("tradable_trade_date") or "")
        if event.get("marker_state") != "on_chart" or date not in x_by_date:
            continue
        lane = lanes.get(date, 0)
        if lane >= 5:
            continue
        lanes[date] = lane + 1
        markers.append(
            {
                "x": round(x_by_date[date], 2),
                "y": _PLOT_TOP - 13 - lane * 9,
                "line_y": _PLOT_TOP,
                "number": int(event["history_number"]),
                "event_id": str(event["event_id"]),
                "title": str(event["title"])[:1000],
                "trade_date": date,
                "direction_class": _direction_class(event.get("direction")),
            }
        )

    return {
        "width": _CHART_WIDTH,
        "height": _CHART_HEIGHT,
        "plot_left": _PLOT_LEFT,
        "plot_right": _CHART_WIDTH - _PLOT_RIGHT,
        "plot_top": _PLOT_TOP,
        "plot_bottom": _CHART_HEIGHT - _PLOT_BOTTOM,
        "candles": candles,
        "ticks": ticks,
        "date_ticks": date_ticks,
        "markers": markers,
        "ohlc_rows": len(valid),
    }


def _render_model(model: dict[str, Any], auth: Any) -> Response:
    security = dict(model["security"])
    prices = [dict(item) for item in model["prices"]]
    events: list[dict[str, Any]] = []
    for raw in model["events"]:
        event = dict(raw)
        event["event_type_label"] = _event_type(event.get("event_type"))
        event["occurred_at_label"] = _time_label(event.get("occurred_at"))
        event["available_at_label"] = _time_label(event.get("available_at"))
        event["expected_gap_display"] = _signed(event.get("expected_gap"))
        event["mapping_confidence_display"] = _confidence(
            event.get("mapping_confidence")
        )
        event["direction_class"] = _direction_class(event.get("direction"))
        event["event_url"] = url_for(
            "event_radar.event_radar_page",
            event_id=event["event_id"],
            tab="overview",
            _anchor="event-detail",
        )
        event["reaction_url"] = url_for(
            "event_radar.event_radar_page",
            event_id=event["event_id"],
            tab="reaction",
            _anchor="event-detail",
        )
        for evidence in event.get("evidence") or []:
            evidence["reliability_display"] = _confidence(evidence.get("reliability"))
            evidence["announcement_at_label"] = _time_label(
                evidence.get("announcement_at") or evidence.get("observed_at")
            )
        for outcome in event.get("outcomes") or []:
            outcome["gross_return_display"] = _signed(
                outcome.get("gross_return"), percent=True
            )
            outcome["excess_return_display"] = _signed(
                outcome.get("excess_return"), percent=True
            )
        events.append(event)

    latest = prices[-1] if prices else {}
    security["latest_close_display"] = _price_label(latest.get("close_price"))
    security["latest_change_display"] = _signed(
        latest.get("change_pct"), percentage_points=True
    )
    security["latest_change_class"] = (
        "bullish"
        if (latest.get("change_pct") or 0) > 0
        else "bearish"
        if (latest.get("change_pct") or 0) < 0
        else "neutral"
    )
    chart = _chart_model(prices, events)
    response = Response(
        render_template(
            "security_history.html",
            security=security,
            prices=prices,
            events=events,
            events_truncated=bool(model.get("events_truncated")),
            event_cursor=model.get("event_cursor") or "",
            next_event_cursor=model.get("next_event_cursor") or "",
            chart=chart,
            price_window=int(model["price_window"]),
            price_start_date=model.get("price_start_date") or "",
            price_end_date=model.get("price_end_date") or "",
            auth=auth,
            now=datetime.now(_MARKET_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            release_version=release_version(),
        ),
        mimetype="text/html",
    )
    return apply_browser_security_headers(response)


@security_history_bp.route(
    "/dashboard/stocks/<exchange>/<ticker>", methods=["GET"]
)
@rate_limit(max_requests=180, window_seconds=60)
def security_history_page(exchange: str, ticker: str) -> Response:
    auth = get_dashboard_auth()
    if auth is None:
        if request.headers.get("X-API-Key") is None and request_prefers_html():
            return dashboard_login_redirect(next_path=request.path)
        return browser_error("Unauthorized", 401)
    if (
        set(request.args) - {"window", "cursor"}
        or len(request.args.getlist("window")) > 1
        or len(request.args.getlist("cursor")) > 1
    ):
        return browser_error("Invalid stock history query", 400)
    try:
        model = load_security_history(
            exchange,
            ticker,
            window=request.args.get("window", ""),
            cursor=request.args.get("cursor", ""),
        )
    except SecurityHistoryQueryError:
        return browser_error("Invalid stock history query", 400)
    except SecurityHistoryNotFoundError:
        return browser_error("Security not found", 404)
    except SecurityHistoryUnavailableError:
        logger.warning("Security history is unavailable", exc_info=True)
        return browser_error("Security history unavailable", 503)
    return _render_model(model, auth)


__all__ = ["security_history_bp"]
