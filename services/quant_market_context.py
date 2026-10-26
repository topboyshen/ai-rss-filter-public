"""Bounded, read-only Quant-server market-context consumer.

Only the background worker performs network I/O.  Web processes read the
short-lived Redis snapshot and treat a missing, malformed, or stale snapshot
as neutral context.  This module must never be imported by scoring or trading
code: Quant context is presentation-only corroborating evidence.
"""

from __future__ import annotations

import json
import logging
import math
import os
import stat
import threading
from datetime import date, datetime, timedelta, timezone
from html import escape as html_escape
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from services.shared_cache import get_shared_json, set_shared_json


logger = logging.getLogger(__name__)

SCHEMA_VERSION = "quant.market-context.v1"
CACHE_NAME = "quant-market-context-v1"
FIXED_TUNNEL_URL = (
    "http://host.docker.internal:18092/integration/v1/market-context"
)

_POLL_MIN_SECONDS = 30
_POLL_MAX_SECONDS = 60
_DEFAULT_POLL_SECONDS = 45
_DEFAULT_TIMEOUT_SECONDS = 3.0
# Quant-Server 10-18 起在 market-context 里附带 research_pool（ETF 研究池，
# 本服务不使用）。10-22 实测响应 107137 字节，旧上限 64 KiB 导致每次刷新失败。
# 256 KiB 约为实测的 2.4 倍；真正的收敛应由 Quant-Server 侧不再下发该字段。
_DEFAULT_MAX_RESPONSE_BYTES = 256 * 1024
_MAX_RESPONSE_BYTES_CEILING = 1024 * 1024
_DEFAULT_CACHE_TTL_SECONDS = 150
_DEFAULT_CONTEXT_MAX_AGE_SECONDS = 120
_DEFAULT_MARKET_HARD_MAX_AGE_DAYS = 30
_DEFAULT_RISK_MAX_AGE_SECONDS = 60 * 60
_MAX_CLOCK_SKEW_SECONDS = 60
_MAX_TOKEN_BYTES = 4096
_MIN_TOKEN_BYTES = 32
_SHANGHAI_TZ = timezone(timedelta(hours=8))
_DECISION_ELIGIBILITY_REASONS = {
    "scheduled_window",
    "before_decision_window",
    "date_mismatch",
    "invalid_time",
    "legacy_assumed",
}
_DECISION_STATES = ("no_trigger", "allowed", "blocked", "data_failed")
_EFFECTIVENESS_STATES = {
    "sample_insufficient",
    "uncertain",
    "preliminary_effective",
    "not_effective",
}
_VALIDITY_HORIZONS = (1, 3, 5, 10, 20)
_BULLISH_SWING_SIGNAL_TYPES = {
    "volume_breakout_ma60",
    "weekly_macd_golden_cross",
}

_POLLER_LOCK = threading.Lock()
_POLLER_STOP = threading.Event()
_POLLER_THREAD: threading.Thread | None = None


class QuantContextError(ValueError):
    """Expected configuration, transport, or contract failure."""


def _bounded_int(
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _bounded_float(
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(minimum, min(maximum, value))


def _poll_interval_seconds() -> int:
    return _bounded_int(
        "AI_FILTER_QUANT_CONTEXT_POLL_SECONDS",
        _DEFAULT_POLL_SECONDS,
        _POLL_MIN_SECONDS,
        _POLL_MAX_SECONDS,
    )


def _cache_ttl_seconds() -> int:
    return _bounded_int(
        "AI_FILTER_QUANT_CONTEXT_CACHE_TTL_SECONDS",
        _DEFAULT_CACHE_TTL_SECONDS,
        60,
        300,
    )


def _context_max_age_seconds() -> int:
    return _bounded_int(
        "AI_FILTER_QUANT_CONTEXT_MAX_AGE_SECONDS",
        _DEFAULT_CONTEXT_MAX_AGE_SECONDS,
        60,
        300,
    )


def quant_context_enabled() -> bool:
    """Return whether the operator requested the fixed tunnel consumer."""
    return bool((os.environ.get("AI_FILTER_QUANT_CONTEXT_URL") or "").strip())


def _validated_url() -> str:
    raw = (os.environ.get("AI_FILTER_QUANT_CONTEXT_URL") or "").strip()
    if not raw:
        raise QuantContextError("Quant market-context URL is not configured")
    # The first rollout intentionally has one compile-time allowlisted tunnel
    # endpoint.  Operator-controlled URLs are not a general outbound allowlist.
    if raw != FIXED_TUNNEL_URL:
        raise QuantContextError("Quant market-context URL is not allowlisted")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise QuantContextError("Quant market-context URL is malformed") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "host.docker.internal"
        or port != 18092
        or parsed.path != "/integration/v1/market-context"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise QuantContextError("Quant market-context URL boundary mismatch")
    return raw


def _read_service_token() -> str:
    configured = (os.environ.get("AI_FILTER_QUANT_TOKEN_FILE") or "").strip()
    if not configured:
        raise QuantContextError("Quant token file is not configured")
    path = Path(configured)
    if not path.is_absolute():
        raise QuantContextError("Quant token file path must be absolute")
    try:
        file_stat = path.lstat()
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            raise QuantContextError("Quant token file must be a regular file")
        # Docker secrets are commonly 0444.  Read access is acceptable inside
        # the dedicated worker; group/other write access is not.
        if file_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise QuantContextError("Quant token file is writable by group/other")
        if not 1 <= file_stat.st_size <= _MAX_TOKEN_BYTES:
            raise QuantContextError("Quant token file size is invalid")
        raw = path.read_bytes()
    except QuantContextError:
        raise
    except OSError as exc:
        raise QuantContextError("Quant token file is unreadable") from exc
    if len(raw) != file_stat.st_size:
        raise QuantContextError("Quant token file changed while reading")
    try:
        token = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise QuantContextError("Quant token is not UTF-8") from exc
    # A single trailing newline is convenient for secret provisioning, but no
    # whitespace is valid inside a Bearer credential.
    token = token.removesuffix("\n")
    token_bytes = token.encode("utf-8")
    if (
        not _MIN_TOKEN_BYTES <= len(token_bytes) <= _MAX_TOKEN_BYTES
        or not token
        or any(character.isspace() for character in token)
    ):
        raise QuantContextError("Quant token length or format is invalid")
    return token


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        raise QuantContextError("Quant market-context redirects are forbidden")


def _build_http_opener():
    # An empty ProxyHandler suppresses HTTP(S)_PROXY and prevents this internal
    # request (and its Bearer header) from crossing an ambient proxy.
    return build_opener(ProxyHandler({}), _RejectRedirects())


def _reject_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise QuantContextError("Quant response contains duplicate JSON keys")
        value[key] = item
    return value


def _fetch_payload(url: str, token: str, *, opener=None) -> dict[str, Any]:
    max_bytes = _bounded_int(
        "AI_FILTER_QUANT_CONTEXT_MAX_BYTES",
        _DEFAULT_MAX_RESPONSE_BYTES,
        4096,
        _MAX_RESPONSE_BYTES_CEILING,
    )
    timeout = _bounded_float(
        "AI_FILTER_QUANT_CONTEXT_TIMEOUT_SECONDS",
        _DEFAULT_TIMEOUT_SECONDS,
        0.5,
        10.0,
    )
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {token}",
            "Connection": "close",
            "User-Agent": "ai-filter-quant-context/1",
        },
        method="GET",
    )
    opener = opener or _build_http_opener()
    try:
        with opener.open(request, timeout=timeout) as response:
            if getattr(response, "status", None) != 200:
                raise QuantContextError("Quant market-context returned non-200")
            final_url = response.geturl()
            if final_url != url:
                raise QuantContextError("Quant market-context response URL changed")
            content_type = str(response.headers.get("Content-Type") or "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                raise QuantContextError("Quant market-context is not JSON")
            content_encoding = str(response.headers.get("Content-Encoding") or "")
            if content_encoding.strip().lower() not in {"", "identity"}:
                raise QuantContextError("Compressed Quant responses are forbidden")
            raw_length = response.headers.get("Content-Length")
            if raw_length:
                try:
                    declared_length = int(raw_length)
                except (TypeError, ValueError) as exc:
                    raise QuantContextError("Invalid Quant Content-Length") from exc
                if declared_length < 0 or declared_length > max_bytes:
                    raise QuantContextError(
                        "Quant response exceeds size limit "
                        f"(declared={declared_length} limit={max_bytes})"
                    )
            body = response.read(max_bytes + 1)
    except QuantContextError:
        raise
    except HTTPError as exc:
        raise QuantContextError(f"Quant HTTP error {exc.code}") from exc
    except (TimeoutError, URLError, OSError) as exc:
        raise QuantContextError("Quant market-context request failed") from exc
    if len(body) > max_bytes:
        raise QuantContextError(
            f"Quant response exceeds size limit (read>{max_bytes} limit={max_bytes})"
        )
    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except QuantContextError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QuantContextError("Quant response is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise QuantContextError("Quant response root must be an object")
    return payload


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _finite_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return value


def _short_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    return str(value).replace("\x00", "")[:limit]


def _safe_bool(value: Any) -> bool:
    return value if isinstance(value, bool) else False


def _strict_iso_date(value: Any) -> date | None:
    """Parse the producer's exact YYYY-MM-DD contract without coercion."""
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _sanitize_signal(
    value: Any,
    *,
    latest_allowed_decision_date: date,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    decision_date = _strict_iso_date(value.get("decision_date"))
    data_date = _strict_iso_date(value.get("data_date"))
    if (
        decision_date is None
        or data_date is None
        or data_date > decision_date
        or decision_date > latest_allowed_decision_date
    ):
        return None
    return {
        "decision_date": decision_date.isoformat(),
        "data_date": data_date.isoformat(),
        "code": _short_text(value.get("code"), 32),
        "name": _short_text(value.get("name"), 80),
        "signal_type": _short_text(value.get("signal_type"), 80),
        "period": _short_text(value.get("period"), 16),
        "title": _short_text(value.get("title"), 160),
        "confidence": _finite_number(value.get("confidence")),
    }


def _audit_count(value: Any, maximum: int = 10_000) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= maximum else None


def _sanitize_daily_audit(
    value: Any,
    *,
    latest_allowed_decision_date: date,
    current: datetime,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    # ``validate_market_context`` runs once before caching and again when a web
    # process reads the cache. Accept our bounded flattened form so validation
    # stays idempotent; all fields are still re-parsed below.
    normalized_state = _short_text(value.get("state"), 16)
    if normalized_state == "legacy":
        return {
            "state": "legacy",
            "decision_date": "",
            "data_date": "",
            "run_status": "unknown",
            "decision_outcome": "unknown",
            "signal_count": 0,
            "allowed_signal_count": 0,
            "blocked_signal_count": 0,
            "target_count": 0,
            "evaluated_count": 0,
            "failed_count": 0,
            "reason_codes": [],
            "is_stuck": False,
            "decision_eligible": False,
            "eligibility_reason": "legacy_assumed",
            "target_decisions": {
                "available": False,
                **{state: 0 for state in _DECISION_STATES},
            },
        }
    if normalized_state == "invalid":
        return None
    if normalized_state == "valid":
        value = {
            "decision_date": value.get("decision_date"),
            "data_date": value.get("data_date"),
            "run": {
                "run_id": value.get("run_id"),
                "status": value.get("run_status"),
                "started_at": value.get("started_at"),
                "completed_at": value.get("completed_at"),
                "reason_codes": value.get("reason_codes"),
                "decision_eligible": value.get("decision_eligible"),
                "eligibility_reason": value.get("eligibility_reason"),
            },
            "decision": {
                "outcome": value.get("decision_outcome"),
                "signal_count": value.get("signal_count"),
                "allowed_signal_count": value.get("allowed_signal_count"),
                "blocked_signal_count": value.get("blocked_signal_count"),
            },
            "coverage": {
                "target_count": value.get("target_count"),
                "evaluated_count": value.get("evaluated_count"),
                "failed_count": value.get("failed_count"),
            },
            "target_decisions": value.get("target_decisions"),
        }
    elif normalized_state:
        return None
    run = value.get("run") if isinstance(value.get("run"), dict) else {}
    decision = (
        value.get("decision") if isinstance(value.get("decision"), dict) else {}
    )
    coverage = (
        value.get("coverage") if isinstance(value.get("coverage"), dict) else {}
    )
    target_decisions_raw = (
        value.get("target_decisions")
        if isinstance(value.get("target_decisions"), dict)
        else {}
    )
    decision_date = _strict_iso_date(value.get("decision_date"))
    data_date = (
        _strict_iso_date(value.get("data_date")) if value.get("data_date") else None
    )
    run_status = _short_text(run.get("status"), 32)
    decision_outcome = _short_text(decision.get("outcome"), 32)
    started_at = _parse_timestamp(run.get("started_at"))
    completed_at = (
        _parse_timestamp(run.get("completed_at")) if run.get("completed_at") else None
    )
    target_count = _audit_count(coverage.get("target_count"))
    evaluated_count = _audit_count(coverage.get("evaluated_count"))
    failed_count = _audit_count(coverage.get("failed_count"))
    signal_count = _audit_count(decision.get("signal_count"), 1_000)
    allowed_signal_count = _audit_count(
        decision.get("allowed_signal_count"), 1_000
    )
    blocked_signal_count = _audit_count(
        decision.get("blocked_signal_count"), 1_000
    )
    eligibility_present = "decision_eligible" in run
    decision_eligible_raw = run.get("decision_eligible")
    if eligibility_present and not isinstance(decision_eligible_raw, bool):
        return None
    decision_eligible = (
        decision_eligible_raw if eligibility_present else True
    )
    eligibility_reason = _short_text(run.get("eligibility_reason"), 32)
    if not eligibility_present:
        eligibility_reason = "legacy_assumed"
    elif eligibility_reason not in _DECISION_ELIGIBILITY_REASONS:
        return None
    if decision_eligible and eligibility_reason not in {
        "scheduled_window",
        "legacy_assumed",
    }:
        return None
    target_decisions_available = target_decisions_raw.get("available") is True
    target_decisions = {
        state: _audit_count(target_decisions_raw.get(state), 10_000)
        for state in _DECISION_STATES
    }
    if target_decisions_available and any(
        count is None for count in target_decisions.values()
    ):
        return None
    if not target_decisions_available:
        target_decisions = {state: 0 for state in _DECISION_STATES}
    if (
        decision_date is None
        or decision_date > latest_allowed_decision_date
        or (data_date is not None and data_date > decision_date)
        or run_status
        not in {"not_run", "running", "success", "partial", "failed", "skipped"}
        or decision_outcome
        not in {
            "not_evaluated",
            "emitted",
            "no_signal",
            "blocked",
            "data_unavailable",
            "skipped",
        }
        or started_at is None
        or any(
            item is None
            for item in (
                target_count,
                evaluated_count,
                failed_count,
                signal_count,
                allowed_signal_count,
                blocked_signal_count,
            )
        )
        or evaluated_count + failed_count > target_count
        or allowed_signal_count + blocked_signal_count != signal_count
        or (
            run_status == "success"
            and (failed_count != 0 or evaluated_count != target_count)
        )
        or started_at > current + timedelta(seconds=_MAX_CLOCK_SKEW_SECONDS)
        or (
            target_decisions_available
            and sum(target_decisions.values()) > target_count
        )
    ):
        return None
    if run_status in {"not_run", "running"}:
        if completed_at is not None or decision_outcome != "not_evaluated":
            return None
    else:
        if (
            completed_at is None
            or completed_at < started_at
            or completed_at > current + timedelta(seconds=_MAX_CLOCK_SKEW_SECONDS)
        ):
            return None
    allowed_outcomes = {
        "success": {"emitted", "no_signal", "blocked"},
        "partial": {"emitted", "blocked", "data_unavailable"},
        "failed": {"data_unavailable"},
        "skipped": {"skipped"},
    }
    if run_status not in {"not_run", "running"} and decision_outcome not in (
        allowed_outcomes.get(run_status) or set()
    ):
        return None
    if (decision_outcome == "emitted" and signal_count == 0) or (
        decision_outcome in {"no_signal", "data_unavailable", "skipped"}
        and signal_count != 0
    ) or (
        decision_outcome == "blocked"
        and (signal_count == 0 or allowed_signal_count != 0)
    ) or (
        decision_outcome == "emitted" and allowed_signal_count == 0
    ):
        return None

    run_id = _short_text(run.get("run_id"), 64)
    if run_id and not all(char.isalnum() or char in "_-" for char in run_id):
        return None
    raw_reasons = run.get("reason_codes")
    raw_reasons = raw_reasons if isinstance(raw_reasons, list) else []
    reason_codes = []
    for value in raw_reasons[:8]:
        reason = _short_text(value, 64)
        if (
            reason
            and reason.replace("_", "").isalnum()
            and reason not in reason_codes
        ):
            reason_codes.append(reason)

    is_stuck = bool(
        run_status == "running" and (current - started_at).total_seconds() > 45 * 60
    )
    return {
        "state": "valid",
        "decision_date": decision_date.isoformat(),
        "data_date": data_date.isoformat() if data_date is not None else "",
        "run_id": run_id,
        "run_status": run_status,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "completed_at": (
            completed_at.isoformat().replace("+00:00", "Z")
            if completed_at is not None
            else ""
        ),
        "decision_outcome": decision_outcome,
        "signal_count": signal_count,
        "allowed_signal_count": allowed_signal_count,
        "blocked_signal_count": blocked_signal_count,
        "target_count": target_count,
        "evaluated_count": evaluated_count,
        "failed_count": failed_count,
        "reason_codes": reason_codes,
        "is_stuck": is_stuck,
        "decision_eligible": decision_eligible,
        "eligibility_reason": eligibility_reason,
        "target_decisions": {
            "available": target_decisions_available,
            **target_decisions,
        },
    }


def _sanitize_factor(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "factor": _short_text(value.get("factor"), 48),
        "label": _short_text(value.get("label"), 64),
        "unit": _short_text(value.get("unit"), 16),
        "value": _finite_number(value.get("value")),
        "value_date": _short_text(value.get("value_date"), 32),
        # Missing or malformed producer health flags fail conservatively.
        "is_stale": value.get("is_stale") is not False,
        "percentile": _finite_number(value.get("percentile")),
        "level": _short_text(value.get("level"), 32),
        "history_ready": value.get("history_ready") is True,
        "collect_available": value.get("collect_available") is True,
    }


def _sanitize_effectiveness(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("invalid") is True:
        return None
    state = _short_text(value.get("state"), 32)
    sample_size = _audit_count(value.get("sample_size"), 1_000_000)
    minimum_samples = _audit_count(value.get("minimum_samples"), 10_000)
    primary_horizon = _audit_count(value.get("primary_horizon_sessions"), 20)
    blocked_sample_size = _audit_count(
        value.get("blocked_counterfactual_sample_size"), 1_000_000
    )
    metrics_available = value.get("metrics_available")
    method = _short_text(value.get("method"), 64)
    raw_horizons = (
        value.get("horizon_samples")
        if isinstance(value.get("horizon_samples"), dict)
        else {}
    )
    horizon_samples = {
        str(horizon): _audit_count(raw_horizons.get(str(horizon)), 1_000_000)
        for horizon in _VALIDITY_HORIZONS
    }
    if (
        state not in _EFFECTIVENESS_STATES
        or sample_size is None
        or minimum_samples is None
        or minimum_samples <= 0
        or primary_horizon not in _VALIDITY_HORIZONS
        or blocked_sample_size is None
        or not isinstance(metrics_available, bool)
        or method != "live_oos_or_walk_forward_oos"
        or any(count is None for count in horizon_samples.values())
        or (sample_size < minimum_samples and metrics_available)
        or (sample_size >= minimum_samples and not metrics_available)
        or (state == "sample_insufficient" and sample_size >= minimum_samples)
        or (state != "sample_insufficient" and sample_size < minimum_samples)
    ):
        return None
    return {
        "state": state,
        "sample_size": sample_size,
        "minimum_samples": minimum_samples,
        "primary_horizon_sessions": primary_horizon,
        "method": method,
        "metrics_available": metrics_available,
        "horizon_samples": horizon_samples,
        "blocked_counterfactual_sample_size": blocked_sample_size,
    }


def validate_market_context(
    payload: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate and project the v1 wire payload into a bounded cache value."""
    if not isinstance(payload, dict):
        raise QuantContextError("Quant response root must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise QuantContextError("Quant market-context schema version mismatch")
    generated_at = _parse_timestamp(payload.get("generated_at"))
    if generated_at is None:
        raise QuantContextError("Quant generated_at must be timezone-aware ISO-8601")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    generated_age = (current - generated_at).total_seconds()
    if generated_age < -_MAX_CLOCK_SKEW_SECONDS:
        raise QuantContextError("Quant generated_at is in the future")
    if generated_age > _context_max_age_seconds():
        raise QuantContextError("Quant market-context response is stale")
    status_value = payload.get("status")
    if status_value not in {"ok", "degraded"}:
        raise QuantContextError("Quant market-context status is invalid")

    market_raw = payload.get("market")
    market_raw = market_raw if isinstance(market_raw, dict) else {}
    market_as_of_date = _strict_iso_date(market_raw.get("as_of"))
    market_as_of = market_as_of_date.isoformat() if market_as_of_date else ""
    expected_as_of_date = _strict_iso_date(market_raw.get("expected_as_of"))
    expected_as_of = (
        expected_as_of_date.isoformat() if expected_as_of_date is not None else ""
    )
    today_shanghai = current.astimezone(_SHANGHAI_TZ).date()
    market_as_of_valid = (
        market_as_of_date is not None and market_as_of_date <= today_shanghai
    )
    expected_as_of_valid = (
        expected_as_of_date is not None and expected_as_of_date <= today_shanghai
    )
    market_age_days = (
        (today_shanghai - market_as_of_date).days
        if market_as_of_valid and market_as_of_date is not None
        else None
    )
    hard_max_age_days = _bounded_int(
        "AI_FILTER_QUANT_MARKET_HARD_MAX_AGE_DAYS",
        _DEFAULT_MARKET_HARD_MAX_AGE_DAYS,
        14,
        60,
    )
    market_hard_stale = (
        market_age_days is None or market_age_days > hard_max_age_days
    )
    expected_slice_missing = (
        market_as_of_valid
        and expected_as_of_valid
        and market_as_of_date is not None
        and expected_as_of_date is not None
        and market_as_of_date < expected_as_of_date
    )
    market_stale_flag = market_raw.get("is_stale")
    market_stale_flag_valid = isinstance(market_stale_flag, bool)
    market_fresh = (
        market_as_of_valid
        and expected_as_of_valid
        and not expected_slice_missing
        and not market_hard_stale
        and market_stale_flag_valid
        and market_stale_flag is False
    )
    factors_raw = market_raw.get("factors")
    factors_raw = factors_raw if isinstance(factors_raw, list) else []
    factors = [
        item
        for item in (_sanitize_factor(value) for value in factors_raw[:16])
        if item is not None
    ]
    breadth_raw = market_raw.get("breadth")
    breadth_raw = breadth_raw if isinstance(breadth_raw, dict) else {}
    market_available = _safe_bool(market_raw.get("available")) and market_fresh

    signals_raw = payload.get("signals")
    signals_raw = signals_raw if isinstance(signals_raw, dict) else {}
    current_raw = signals_raw.get("current")
    current_raw = current_raw if isinstance(current_raw, list) else []
    recent_raw = signals_raw.get("recent")
    recent_raw = recent_raw if isinstance(recent_raw, list) else []
    latest_allowed_decision_date = current.astimezone(_SHANGHAI_TZ).date()
    current_candidates = [
        _sanitize_signal(
            value,
            latest_allowed_decision_date=latest_allowed_decision_date,
        )
        for value in current_raw[:20]
    ]
    recent_candidates = [
        _sanitize_signal(
            value,
            latest_allowed_decision_date=latest_allowed_decision_date,
        )
        for value in recent_raw[:20]
    ]
    invalid_signal_contract = any(
        item is None for item in current_candidates + recent_candidates
    )
    current_signals = [item for item in current_candidates if item is not None]
    recent_signals = [item for item in recent_candidates if item is not None]
    mismatched_current_signal = any(
        item["decision_date"] != market_as_of for item in current_signals
    )
    current_signals = [
        item for item in current_signals if item["decision_date"] == market_as_of
    ]
    raw_latest_signal_date = signals_raw.get(
        "latest_signal_date", signals_raw.get("latest_decision_date")
    )
    parsed_latest_signal_date = _strict_iso_date(raw_latest_signal_date)
    latest_signal_date = (
        parsed_latest_signal_date.isoformat()
        if parsed_latest_signal_date is not None
        and parsed_latest_signal_date <= latest_allowed_decision_date
        else ""
    )
    invalid_latest_signal_date = bool(raw_latest_signal_date) and not latest_signal_date
    audit_present = "daily_audit" in signals_raw
    if audit_present:
        daily_audit = _sanitize_daily_audit(
            signals_raw.get("daily_audit"),
            latest_allowed_decision_date=latest_allowed_decision_date,
            current=current,
        )
        if daily_audit is None:
            daily_audit = {
                "state": "invalid",
                "decision_date": "",
                "data_date": "",
                "run_status": "unknown",
                "decision_outcome": "unknown",
                "signal_count": 0,
                "allowed_signal_count": 0,
                "blocked_signal_count": 0,
                "target_count": 0,
                "evaluated_count": 0,
                "failed_count": 0,
                "reason_codes": [],
                "is_stuck": False,
                "decision_eligible": False,
                "eligibility_reason": "invalid_time",
                "target_decisions": {
                    "available": False,
                    **{state: 0 for state in _DECISION_STATES},
                },
            }
    else:
        daily_audit = {
            "state": "legacy",
            "decision_date": "",
            "data_date": "",
            "run_status": "unknown",
            "decision_outcome": "unknown",
            "signal_count": 0,
            "allowed_signal_count": 0,
            "blocked_signal_count": 0,
            "target_count": 0,
            "evaluated_count": 0,
            "failed_count": 0,
            "reason_codes": [],
            "is_stuck": False,
            "decision_eligible": False,
            "eligibility_reason": "legacy_assumed",
            "target_decisions": {
                "available": False,
                **{state: 0 for state in _DECISION_STATES},
            },
        }
    audit_inconsistent = False
    if (
        daily_audit.get("state") == "valid"
        and daily_audit.get("decision_eligible") is True
    ):
        run_status = daily_audit.get("run_status")
        decision_outcome = daily_audit.get("decision_outcome")
        if current_signals and decision_outcome not in {"emitted", "blocked"}:
            audit_inconsistent = True
        if decision_outcome == "no_signal" and current_signals:
            audit_inconsistent = True
        if (
            run_status == "success"
            and decision_outcome in {"emitted", "blocked"}
            and daily_audit.get("decision_date") == market_as_of
            and not current_signals
        ):
            audit_inconsistent = True
    daily_audit["consistent"] = not audit_inconsistent
    effectiveness_present = "effectiveness" in signals_raw
    effectiveness = (
        _sanitize_effectiveness(signals_raw.get("effectiveness"))
        if effectiveness_present
        else None
    )
    if effectiveness is None:
        effectiveness = {
            "state": "invalid" if effectiveness_present else "legacy",
            "sample_size": 0,
            "minimum_samples": 0,
            "primary_horizon_sessions": 20,
            "method": "",
            "metrics_available": False,
            "horizon_samples": {
                str(horizon): 0 for horizon in _VALIDITY_HORIZONS
            },
            "blocked_counterfactual_sample_size": 0,
        }
    if not market_available:
        # Old signals must never masquerade as current corroboration.
        current_signals = []

    risk_raw = payload.get("risk")
    risk_raw = risk_raw if isinstance(risk_raw, dict) else {}
    risk_observed_at = _short_text(risk_raw.get("observed_at"), 40)
    risk_timestamp = _parse_timestamp(risk_observed_at)
    risk_age = (
        (current - risk_timestamp).total_seconds()
        if risk_timestamp is not None
        else math.inf
    )
    risk_reported_age = _finite_number(risk_raw.get("age_seconds"))
    risk_max_age = _bounded_int(
        "AI_FILTER_QUANT_RISK_MAX_AGE_SECONDS",
        _DEFAULT_RISK_MAX_AGE_SECONDS,
        300,
        24 * 60 * 60,
    )
    risk_stale_flag = risk_raw.get("is_stale")
    risk_future_flag = risk_raw.get("is_future")
    risk_flags_valid = isinstance(risk_stale_flag, bool) and isinstance(
        risk_future_flag, bool
    )
    risk_fresh = (
        -_MAX_CLOCK_SKEW_SECONDS <= risk_age <= risk_max_age
        and (risk_reported_age is None or 0 <= risk_reported_age <= risk_max_age)
        and risk_flags_valid
        and risk_stale_flag is False
        and risk_future_flag is False
    )
    risk_available = _safe_bool(risk_raw.get("available")) and risk_fresh
    allowed_risk_fields = {"vix", "us_10y", "crude_oil"}
    missing_raw = risk_raw.get("missing_fields")
    cached_raw = risk_raw.get("cached_fields")
    missing_raw = missing_raw if isinstance(missing_raw, list) else []
    cached_raw = cached_raw if isinstance(cached_raw, list) else []

    quality_raw = payload.get("quality")
    quality_raw = quality_raw if isinstance(quality_raw, dict) else {}
    reasons_raw = quality_raw.get("degraded_reasons")
    reasons_raw = reasons_raw if isinstance(reasons_raw, list) else []
    reasons = [_short_text(reason, 80) for reason in reasons_raw[:16]]
    if _safe_bool(market_raw.get("available")) and not market_fresh:
        if not market_stale_flag_valid:
            reasons.append("consumer_market_staleness_invalid")
        elif market_stale_flag:
            reasons.append("consumer_market_reported_stale")
        if not market_as_of_valid:
            reasons.append("consumer_market_as_of_invalid")
        if not expected_as_of_valid:
            reasons.append("consumer_market_expected_as_of_invalid")
        if expected_slice_missing:
            reasons.append("consumer_market_expected_slice_missing")
        if market_hard_stale:
            reasons.append("consumer_market_hard_age_exceeded")
        if (
            not market_as_of_valid
            or not expected_as_of_valid
            or expected_slice_missing
            or market_hard_stale
        ):
            reasons.append("consumer_market_snapshot_stale")
    if any(
        item["is_stale"] or not item["history_ready"] or not item["collect_available"]
        for item in factors
    ):
        reasons.append("consumer_market_factor_degraded")
    if invalid_signal_contract or invalid_latest_signal_date:
        reasons.append("consumer_signal_contract_invalid")
    if mismatched_current_signal:
        reasons.append("consumer_signal_decision_date_mismatch")
    if daily_audit.get("state") == "invalid":
        reasons.append("consumer_signal_audit_invalid")
    elif daily_audit.get("state") == "valid":
        run_status = daily_audit.get("run_status")
        audit_date = daily_audit.get("decision_date")
        if daily_audit.get("decision_eligible") is not True:
            reasons.append("consumer_signal_run_outside_window")
        else:
            if run_status == "partial":
                reasons.append("consumer_signal_run_partial")
            elif run_status == "failed":
                reasons.append("consumer_signal_run_failed")
            elif daily_audit.get("is_stuck"):
                reasons.append("consumer_signal_run_stuck")
            if expected_as_of and audit_date and audit_date < expected_as_of:
                reasons.append("consumer_signal_audit_stale")
    if audit_inconsistent:
        reasons.append("consumer_signal_audit_inconsistent")
    if effectiveness.get("state") == "invalid":
        reasons.append("consumer_signal_effectiveness_invalid")
    if _safe_bool(risk_raw.get("available")) and not risk_fresh:
        if not risk_flags_valid:
            reasons.append("consumer_risk_staleness_invalid")
        elif risk_future_flag:
            reasons.append("consumer_risk_snapshot_from_future")
        if risk_stale_flag or not (-_MAX_CLOCK_SKEW_SECONDS <= risk_age <= risk_max_age):
            reasons.append("consumer_risk_snapshot_stale")

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "status": "degraded" if status_value == "degraded" or reasons else "ok",
        "market": {
            "available": market_available,
            "as_of": market_as_of,
            "expected_as_of": expected_as_of,
            "age_days": _finite_number(market_raw.get("age_days")),
            "is_stale": not market_fresh,
            "source": _short_text(market_raw.get("source"), 64),
            "breadth": {
                "limit_up": _finite_number(breadth_raw.get("limit_up")),
                "limit_down": _finite_number(breadth_raw.get("limit_down")),
            },
            "margin_balance": _finite_number(market_raw.get("margin_balance")),
            "factors": factors,
            "strategy": {
                "role": _short_text(
                    (market_raw.get("strategy") or {}).get("role"), 64
                ) if isinstance(market_raw.get("strategy"), dict) else "",
                "decision_time": _short_text(
                    (market_raw.get("strategy") or {}).get("decision_time"), 64
                ) if isinstance(market_raw.get("strategy"), dict) else "",
            },
        },
        "signals": {
            "available": market_available,
            "current": current_signals,
            "latest_decision_date": latest_signal_date,
            "latest_signal_date": latest_signal_date,
            "recent": recent_signals,
            "daily_audit": daily_audit,
            "effectiveness": effectiveness,
        },
        "risk": {
            "available": risk_available,
            "level": _short_text(risk_raw.get("level"), 32) if risk_available else "",
            "observed_at": risk_observed_at,
            "age_seconds": risk_reported_age,
            "is_stale": not risk_fresh,
            "is_future": risk_future_flag is not False,
            "data_complete": _safe_bool(risk_raw.get("data_complete")),
            "missing_fields": sorted(
                allowed_risk_fields.intersection(str(item) for item in missing_raw)
            ),
            "cached_fields": sorted(
                allowed_risk_fields.intersection(str(item) for item in cached_raw)
            ),
            "vix": _finite_number(risk_raw.get("vix")) if risk_available else None,
            "us_10y": _finite_number(risk_raw.get("us_10y")) if risk_available else None,
            "crude_oil": _finite_number(risk_raw.get("crude_oil")) if risk_available else None,
        },
        "quality": {
            "degraded_reasons": sorted(set(reason for reason in reasons if reason))[:24]
        },
    }


def poll_quant_market_context_once(*, opener=None, now: datetime | None = None) -> bool:
    """Fetch, validate, and atomically replace the Redis TTL snapshot."""
    from services.admission_fence import AdmissionClosed, admission

    try:
        with admission("quant_context_poll", kind="intake"):
            return _poll_quant_context_admitted(opener=opener, now=now)
    except AdmissionClosed:
        return False


def _poll_quant_context_admitted(*, opener=None, now: datetime | None = None) -> bool:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        url = _validated_url()
        token = _read_service_token()
        payload = _fetch_payload(url, token, opener=opener)
        normalized = validate_market_context(payload, now=current)
        envelope = {
            "cache_version": 1,
            "fetched_at": current.timestamp(),
            "payload": normalized,
        }
        if not set_shared_json(CACHE_NAME, envelope, _cache_ttl_seconds()):
            raise QuantContextError("Unable to write Quant context to Redis")
        return True
    except QuantContextError as exc:
        # Do not delete a still-fresh previous value on a transient failure.
        # Redis TTL and reader freshness checks provide bounded fail-neutral
        # behavior without extending stale data indefinitely.
        logger.warning("[QuantContext] refresh failed: %s", exc)
        return False
    except Exception:
        logger.exception("[QuantContext] unexpected refresh failure")
        return False


def _poller_loop() -> None:
    interval = _poll_interval_seconds()
    while not _POLLER_STOP.is_set():
        poll_quant_market_context_once()
        if _POLLER_STOP.wait(interval):
            break


def start_quant_market_context_poller() -> threading.Thread | None:
    """Start the single worker poller; disabled configuration is a no-op."""
    global _POLLER_THREAD
    if not quant_context_enabled():
        logger.info("[QuantContext] consumer disabled (URL not configured)")
        return None
    with _POLLER_LOCK:
        if _POLLER_THREAD is not None and _POLLER_THREAD.is_alive():
            return _POLLER_THREAD
        _POLLER_STOP.clear()
        _POLLER_THREAD = threading.Thread(
            target=_poller_loop,
            name="quant-market-context-poller",
            daemon=True,
        )
        _POLLER_THREAD.start()
        return _POLLER_THREAD


def stop_quant_market_context_poller(timeout: float = 12.0) -> None:
    global _POLLER_THREAD
    _POLLER_STOP.set()
    thread = _POLLER_THREAD
    if thread is not None:
        thread.join(timeout=max(0.0, float(timeout)))
    with _POLLER_LOCK:
        if _POLLER_THREAD is thread and (
            thread is None or not thread.is_alive()
        ):
            _POLLER_THREAD = None


def _neutral_public_context(reason: str = "unavailable_or_stale") -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "unavailable",
        "available": False,
        "decision_effect": "none",
        "reason": reason,
        "generated_at": "",
        "fetched_at": "",
        "age_seconds": None,
        "market": {"available": False, "as_of": "", "expected_as_of": ""},
        "risk": {
            "available": False,
            "level": "",
            "normalized_state": "unavailable",
            "scope": "unknown",
            "observed_at": "",
            "age_seconds": None,
            "vix": None,
            "us_10y": None,
            "crude_oil": None,
        },
        "swing": {
            "available": False,
            "as_of": "",
            "decision_time": "15:30 Asia/Shanghai",
            "signal_count": 0,
            "signals": [],
            "latest_signal_date": "",
            "audit": {
                "state": "unavailable",
                "decision_date": "",
                "run_status": "unknown",
                "decision_outcome": "unknown",
                "consistent": True,
                "decision_eligible": False,
            },
            "effectiveness": {
                "state": "unavailable",
                "sample_size": 0,
                "minimum_samples": 0,
                "primary_horizon_sessions": 20,
                "metrics_available": False,
            },
        },
        "quality": {"degraded_reasons": [reason]},
    }


def get_quant_market_context(*, now: datetime | None = None) -> dict[str, Any]:
    """Return a bounded presentation DTO, neutral on every failure/stale read."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        envelope = get_shared_json(CACHE_NAME)
        if not isinstance(envelope, dict) or envelope.get("cache_version") != 1:
            return _neutral_public_context()
        fetched_at = envelope.get("fetched_at")
        if isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float)):
            return _neutral_public_context("invalid_cache_envelope")
        cache_age = current.timestamp() - float(fetched_at)
        if (
            not math.isfinite(cache_age)
            or cache_age < -_MAX_CLOCK_SKEW_SECONDS
            or cache_age > _context_max_age_seconds()
        ):
            return _neutral_public_context("cache_stale")
        payload = validate_market_context(envelope.get("payload"), now=current)
    except Exception:
        logger.warning("[QuantContext] shared snapshot read failed", exc_info=True)
        return _neutral_public_context()

    signals = payload["signals"]["current"][:3]
    market = payload["market"]
    risk = payload["risk"]
    risk_observed_at = _parse_timestamp(risk.get("observed_at"))
    risk_age_seconds = (
        max(0, int((current - risk_observed_at).total_seconds()))
        if risk_observed_at is not None
        else None
    )
    normalized_risk = _normalize_risk_level(
        risk.get("level"), available=bool(risk.get("available"))
    )
    daily_audit = payload["signals"].get("daily_audit")
    daily_audit = daily_audit if isinstance(daily_audit, dict) else {
        "state": "invalid"
    }
    audit_available = daily_audit.get("state") == "valid"
    available = bool(market["available"] or risk["available"] or audit_available)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": payload["status"] if available else "unavailable",
        "available": available,
        "decision_effect": "none",
        "reason": "" if available else "sections_unavailable_or_stale",
        "generated_at": payload["generated_at"],
        "fetched_at": datetime.fromtimestamp(
            float(fetched_at), tz=timezone.utc
        ).isoformat().replace("+00:00", "Z"),
        "age_seconds": max(0, int(cache_age)),
        "market": {
            "available": market["available"],
            "as_of": market["as_of"],
            "expected_as_of": market["expected_as_of"],
            "breadth": market["breadth"],
        },
        "risk": {
            "available": risk["available"],
            "level": risk["level"],
            "normalized_state": normalized_risk["state"],
            "scope": normalized_risk["scope"],
            "observed_at": risk["observed_at"],
            "age_seconds": risk_age_seconds,
            "data_complete": risk["data_complete"],
            "vix": risk["vix"],
            "us_10y": risk["us_10y"],
            "crude_oil": risk["crude_oil"],
        },
        "swing": {
            "available": payload["signals"]["available"],
            "as_of": (
                daily_audit.get("decision_date")
                if audit_available
                else payload["signals"]["latest_signal_date"] or market["as_of"]
            ),
            "decision_time": market.get("strategy", {}).get("decision_time")
            if isinstance(market.get("strategy"), dict)
            else "",
            "signal_count": len(payload["signals"]["current"]),
            "signals": signals,
            "latest_signal_date": payload["signals"]["latest_signal_date"],
            "audit": daily_audit,
            "effectiveness": payload["signals"].get("effectiveness", {}),
        },
        "quality": payload["quality"],
    }


def _direction_from_text(value: Any) -> str:
    text = _short_text(value, 200).casefold()
    if any(token in text for token in ("看多", "利多", "偏多", "买入", "转强", "上行", "bull", "long")):
        return "bullish"
    if any(token in text for token in ("看空", "利空", "偏空", "卖出", "转弱", "下行", "bear", "short")):
        return "bearish"
    return "neutral"


def _signal_direction(signal: dict[str, Any]) -> str:
    """Map the producer's bounded strategy vocabulary before text fallback."""
    signal_type = _short_text(signal.get("signal_type"), 80).casefold()
    if signal_type in _BULLISH_SWING_SIGNAL_TYPES:
        return "bullish"
    return _direction_from_text(
        " ".join(
            _short_text(signal.get(key), 160)
            for key in ("title", "name", "signal_type")
        )
    )


def _briefing_bias(briefing: dict[str, Any]) -> dict[str, Any]:
    themes = briefing.get("themes") if isinstance(briefing.get("themes"), list) else []
    counts = {"bullish": 0, "bearish": 0, "neutral": 0}
    for theme in themes[:12]:
        if not isinstance(theme, dict):
            continue
        counts[_direction_from_text(theme.get("direction"))] += 1
    if counts["bullish"] > counts["bearish"]:
        value = "bullish"
    elif counts["bearish"] > counts["bullish"]:
        value = "bearish"
    elif counts["bullish"] or counts["bearish"]:
        value = "mixed"
    else:
        fallback = _direction_from_text(briefing.get("market_impact"))
        value = fallback if fallback != "neutral" else "neutral"
    return {
        "value": value,
        "label": {
            "bullish": "偏多",
            "bearish": "偏空",
            "mixed": "多空混合",
            "neutral": "中性",
        }[value],
        "bullish_themes": counts["bullish"],
        "bearish_themes": counts["bearish"],
    }


def _swing_decision_state(context: dict[str, Any]) -> str:
    swing = context.get("swing") if isinstance(context.get("swing"), dict) else {}
    audit = swing.get("audit") if isinstance(swing.get("audit"), dict) else {}
    audit_state = _short_text(audit.get("state"), 32)
    if audit_state == "legacy":
        return "legacy"
    if audit_state != "valid":
        return "unavailable"
    if audit.get("decision_eligible") is not True:
        return "operational_only"
    if audit.get("consistent") is not True:
        return "invalid"
    status = _short_text(audit.get("run_status"), 32)
    outcome = _short_text(audit.get("decision_outcome"), 32)
    if status != "success":
        return "data_limited"
    return {
        "emitted": "allowed",
        "blocked": "blocked",
        "no_signal": "no_trigger",
    }.get(outcome, "data_limited")


def _swing_bias(context: dict[str, Any]) -> str:
    swing = context.get("swing") if isinstance(context.get("swing"), dict) else {}
    audit = swing.get("audit") if isinstance(swing.get("audit"), dict) else {}
    audit_state = _short_text(audit.get("state"), 32)
    if audit_state == "valid":
        market = (
            context.get("market") if isinstance(context.get("market"), dict) else {}
        )
        if (
            audit.get("consistent") is not True
            or audit.get("decision_eligible") is not True
            or audit.get("run_status") != "success"
            or audit.get("decision_outcome") not in {"emitted", "blocked"}
            or audit.get("decision_date") != market.get("as_of")
        ):
            return "neutral"
    elif audit_state != "legacy":
        return "neutral"
    signals = swing.get("signals") if isinstance(swing.get("signals"), list) else []
    directions = {
        _signal_direction(signal)
        for signal in signals[:3]
        if isinstance(signal, dict)
    }
    directions.discard("neutral")
    if directions == {"bullish"}:
        return "bullish"
    if directions == {"bearish"}:
        return "bearish"
    return "mixed" if directions else "neutral"


def _normalize_risk_level(level: Any, *, available: bool) -> dict[str, str]:
    """Separate producer severity, data quality, and constraint scope."""
    if not available:
        return {
            "state": "unavailable",
            "scope": "unknown",
            "label": "暂不可用",
            "color": "#71717a",
        }
    value = _short_text(level, 32).casefold()
    mapping = {
        "ok": ("stable", "global", "宏观平稳", "#16a34a"),
        "normal": ("stable", "global", "宏观平稳", "#16a34a"),
        "low": ("stable", "global", "低风险", "#16a34a"),
        "medium": ("caution", "global", "风险关注", "#d97706"),
        "elevated": ("caution", "global", "风险抬升", "#d97706"),
        "high": ("critical", "global", "高风险", "#dc2626"),
        "critical": ("critical", "global", "强风险约束", "#dc2626"),
        # Producer warning means missing/partial observations, not market risk.
        "warning": ("data_limited", "data", "数据告警", "#d97706"),
        # This producer state constrains growth buys, not the whole market.
        "growth_blocked": (
            "growth_constrained",
            "growth",
            "成长风格受限",
            "#d97706",
        ),
    }
    state, scope, label, color = mapping.get(
        value, ("unknown", "unknown", "状态未知", "#71717a")
    )
    return {"state": state, "scope": scope, "label": label, "color": color}


def _build_comparison(
    briefing: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    ai = _briefing_bias(briefing)
    if not context.get("available"):
        return {
            "ai_bias": ai["value"],
            "ai_label": ai["label"],
            "swing_bias": "neutral",
            "direction_axis": "unavailable",
            "risk_axis": "unavailable",
            "risk_scope": "unknown",
            "execution_state": "unavailable",
            "relation": "unavailable",
            "relation_label": "旁证不可用",
        }
    swing_bias = _swing_bias(context)
    risk = context.get("risk") if isinstance(context.get("risk"), dict) else {}
    risk_level = _short_text(risk.get("level"), 32).casefold()
    normalized_risk = _normalize_risk_level(
        risk_level, available=bool(risk.get("available"))
    )
    ai_value = ai["value"]
    execution_state = _swing_decision_state(context)

    if swing_bias in {"bullish", "bearish"} and ai_value in {"bullish", "bearish"}:
        direction_axis = "support" if swing_bias == ai_value else "conflict"
    elif swing_bias == "mixed":
        direction_axis = "mixed"
    else:
        direction_axis = "unconfirmed"

    if execution_state in {"unavailable", "operational_only", "invalid", "data_limited"}:
        relation = "data_limited"
    elif swing_bias in {"bullish", "bearish"} and ai_value in {"bullish", "bearish"}:
        if swing_bias != ai_value:
            relation = "direction_conflict"
        elif execution_state == "blocked":
            relation = "aligned_but_blocked"
        else:
            relation = "dual_support"
    elif execution_state == "no_trigger" and ai_value in {"bullish", "bearish"}:
        relation = "ai_leads"
    elif execution_state in {"allowed", "blocked", "legacy"} and swing_bias in {
        "bullish",
        "bearish",
    }:
        relation = "quant_leads" if ai_value == "neutral" else "mixed"
    else:
        relation = "independent"
    return {
        "ai_bias": ai_value,
        "ai_label": ai["label"],
        "swing_bias": swing_bias,
        "direction_axis": direction_axis,
        "risk_axis": normalized_risk["state"],
        "risk_scope": normalized_risk["scope"],
        "execution_state": execution_state,
        "relation": relation,
        "relation_label": {
            "dual_support": "双轴同向强化",
            "aligned_but_blocked": "同向但风控阻断",
            "direction_conflict": "方向冲突",
            "ai_leads": "AI领先 · Quant未确认",
            "quant_leads": "Quant领先 · AI未确认",
            "mixed": "双轴方向混合",
            "data_limited": "运行或数据受限",
            "independent": "双轴独立观察",
        }[relation],
    }


def attach_quant_market_context(briefing: dict[str, Any]) -> dict[str, Any]:
    """Copy a Briefing result and add presentation-only Quant evidence."""
    result = dict(briefing or {})
    context = get_quant_market_context()
    context["comparison"] = _build_comparison(result, context)
    result["quant_context"] = context
    return result


def _risk_label(context: dict[str, Any]) -> str:
    risk = context.get("risk") if isinstance(context.get("risk"), dict) else {}
    return _normalize_risk_level(
        risk.get("level"), available=bool(risk.get("available"))
    )["label"]


def _risk_color(context: dict[str, Any]) -> str:
    risk = context.get("risk") if isinstance(context.get("risk"), dict) else {}
    return _normalize_risk_level(
        risk.get("level"), available=bool(risk.get("available"))
    )["color"]


def _direction_axis_color(comparison: dict[str, Any]) -> str:
    relation = _short_text(comparison.get("relation"), 32)
    if relation == "aligned_but_blocked":
        return "#d97706"
    if relation in {"data_limited", "unavailable"}:
        return "#71717a"
    direction_axis = _short_text(comparison.get("direction_axis"), 32)
    if direction_axis == "support":
        return "#16a34a"
    if direction_axis == "conflict":
        return "#dc2626"
    if direction_axis == "mixed":
        return "#d97706"
    return {
        "bullish": "#2563eb",
        "bearish": "#7c3aed",
    }.get(_short_text(comparison.get("ai_bias"), 32), "#71717a")


def _card_color(context: dict[str, Any], comparison: dict[str, Any]) -> str:
    risk_axis = _short_text(comparison.get("risk_axis"), 32)
    direction_axis = _short_text(comparison.get("direction_axis"), 32)
    if risk_axis == "critical" or direction_axis == "conflict":
        return "#dc2626"
    if risk_axis in {"caution", "growth_constrained", "data_limited"}:
        return "#d97706"
    if direction_axis == "support" or risk_axis == "stable":
        return "#16a34a"
    return _risk_color(context)


def _risk_axis_summary(comparison: dict[str, Any]) -> str:
    return {
        "stable": "宏观环境平稳",
        "caution": "宏观风险需关注",
        "critical": "宏观风险为强约束",
        "growth_constrained": "成长风格买入受限",
        "data_limited": "风险数据存在缺口",
        "unknown": "宏观风险状态未知",
        "unavailable": "宏观风险旁证不可用",
    }.get(
        _short_text(comparison.get("risk_axis"), 32),
        "宏观风险状态未知",
    )


def _market_slice_label(context: dict[str, Any]) -> str:
    market = context.get("market") if isinstance(context.get("market"), dict) else {}
    if not market.get("available"):
        return "A股收盘切片暂不可用"
    as_of = _short_text(market.get("as_of"), 32)
    expected_as_of = _short_text(market.get("expected_as_of"), 32)
    if as_of and expected_as_of and as_of == expected_as_of:
        return f"最近交易日收盘切片（{as_of}）"
    if as_of and expected_as_of:
        return f"历史收盘切片（截至 {as_of}，预期 {expected_as_of}）"
    if as_of:
        return f"历史收盘切片（截至 {as_of}）"
    return "A股收盘切片暂不可用"


def _age_label(value: Any, *, unavailable: str) -> str:
    age = _finite_number(value)
    if age is None or age < 0:
        return unavailable
    seconds = int(age)
    if seconds < 60:
        return f"{seconds} 秒前"
    if seconds < 60 * 60:
        return f"{seconds // 60} 分钟前"
    return f"{seconds // (60 * 60)} 小时前"


def _swing_label(context: dict[str, Any]) -> str:
    swing = context.get("swing") if isinstance(context.get("swing"), dict) else {}
    audit = swing.get("audit") if isinstance(swing.get("audit"), dict) else {}
    audit_state = _short_text(audit.get("state"), 32)
    market = context.get("market") if isinstance(context.get("market"), dict) else {}
    expected_as_of = _short_text(market.get("expected_as_of"), 32)
    if audit_state == "valid":
        decision_date = _short_text(audit.get("decision_date"), 32)
        decision_time = _short_text(swing.get("decision_time"), 64) or "15:30 Asia/Shanghai"
        run_status = _short_text(audit.get("run_status"), 32)
        if audit.get("decision_eligible") is not True:
            status_label = {
                "partial": "partial",
                "failed": "failed",
                "success": "success",
                "running": "running",
            }.get(run_status, "非正式")
            return (
                f"量化运行审计（{decision_date or '日期未知'}）：非正式时点补跑"
                f" · {status_label} 记录保留 · 不构成 {decision_time} 方向确认"
            )
        if expected_as_of and decision_date and decision_date < expected_as_of:
            return f"量化日决策：当前交易日审计未更新（最近 {decision_date}）"
        outcome = _short_text(audit.get("decision_outcome"), 32)
        target_count = _audit_count(audit.get("target_count")) or 0
        evaluated_count = _audit_count(audit.get("evaluated_count")) or 0
        signal_count = _audit_count(audit.get("signal_count"), 1_000) or 0
        allowed_count = _audit_count(
            audit.get("allowed_signal_count"), 1_000
        ) or 0
        blocked_count = _audit_count(
            audit.get("blocked_signal_count"), 1_000
        ) or 0
        decisions = (
            audit.get("target_decisions")
            if isinstance(audit.get("target_decisions"), dict)
            else {}
        )
        decisions_available = decisions.get("available") is True
        no_trigger_count = _audit_count(decisions.get("no_trigger")) or 0
        allowed_decision_count = _audit_count(decisions.get("allowed")) or 0
        blocked_decision_count = _audit_count(decisions.get("blocked")) or 0
        data_failed_count = _audit_count(decisions.get("data_failed")) or 0
        decision_suffix = ""
        if decisions_available:
            decision_suffix = (
                f" · 标的状态 未触发 {no_trigger_count} / 放行 {allowed_decision_count}"
                f" / 风控拦截 {blocked_decision_count} / 数据失败 {data_failed_count}"
            )
        prefix = f"量化日决策（{decision_date or '日期未知'}）"
        if audit.get("consistent") is not True:
            return f"{prefix}：运行与信号记录不一致 · 决策不可用"
        if audit.get("is_stuck"):
            return f"{prefix}：运行超时 · 决策不可用"
        if run_status == "success" and outcome == "no_signal":
            return (
                f"{prefix}：已完成 · 无新增可发布技术信号 · 已检查 {evaluated_count}/{target_count}"
                f"{decision_suffix}"
            )
        if run_status == "success" and outcome == "emitted":
            signals = (
                swing.get("signals") if isinstance(swing.get("signals"), list) else []
            )
            first = signals[0] if signals and isinstance(signals[0], dict) else {}
            first_label = (
                _short_text(first.get("title"), 80)
                or _short_text(first.get("name"), 40)
            )
            gate_suffix = f" · 放行 {allowed_count}"
            if blocked_count:
                gate_suffix += f" · 风控拦截 {blocked_count}"
            first_suffix = f" · 首个：{first_label}" if first_label else ""
            return (
                f"{prefix}：已完成 · 形成 {signal_count} 个新技术信号"
                f"{gate_suffix}{first_suffix}{decision_suffix}"
            )
        if run_status == "success" and outcome == "blocked":
            return (
                f"{prefix}：已完成 · 形成 {signal_count} 个新技术信号"
                f" · 全部被风控拦截{decision_suffix}"
            )
        if run_status == "partial":
            return f"{prefix}：运行不完整 · 已检查 {evaluated_count}/{target_count} · 决策未确认"
        if run_status == "failed":
            return f"{prefix}：运行失败 · 已检查 {evaluated_count}/{target_count} · 决策不可用"
        if run_status == "running":
            return f"{prefix}：运行中 · 尚未形成决策"
        if run_status == "not_run":
            return f"{prefix}：尚未运行"
        if run_status == "skipped":
            return f"{prefix}：按计划跳过"
        return f"{prefix}：运行状态未知"
    if audit_state == "invalid":
        return "量化日决策：运行审计不可用"
    if audit_state == "unavailable":
        return "量化日决策：暂不可用"

    signals = swing.get("signals") if isinstance(swing.get("signals"), list) else []
    if signals:
        first = signals[0] if isinstance(signals[0], dict) else {}
        label = (
            _short_text(first.get("title"), 80)
            or " ".join(
                value
                for value in (
                    _short_text(first.get("name"), 40),
                    _short_text(first.get("signal_type"), 40),
                )
                if value
            )
            or "有新信号"
        )
        return f"量化波段信号：{label} · 运行审计未提供（兼容旧版）"
    latest_signal_date = _short_text(swing.get("latest_signal_date"), 32)
    suffix = f" · 最近信号 {latest_signal_date}" if latest_signal_date else ""
    return f"量化日决策：运行状态未知（旧版接口未提供审计）{suffix}"


def _effectiveness_label(context: dict[str, Any]) -> str:
    swing = context.get("swing") if isinstance(context.get("swing"), dict) else {}
    summary = (
        swing.get("effectiveness")
        if isinstance(swing.get("effectiveness"), dict)
        else {}
    )
    state = _short_text(summary.get("state"), 32)
    sample_size = _audit_count(summary.get("sample_size"), 1_000_000) or 0
    minimum_samples = _audit_count(summary.get("minimum_samples"), 10_000) or 0
    primary = _audit_count(summary.get("primary_horizon_sessions"), 20) or 20
    if state == "sample_insufficient":
        return (
            f"样本不足 · {primary}个交易日成熟样本 {sample_size}/{minimum_samples}"
            " · 精确胜率隐藏"
        )
    if state == "uncertain":
        return f"有效性不确定 · {primary}个交易日成熟样本 {sample_size}"
    if state == "preliminary_effective":
        return f"初步有效 · {primary}个交易日成熟样本 {sample_size}"
    if state == "not_effective":
        return f"未见有效 · {primary}个交易日成熟样本 {sample_size}"
    if state == "invalid":
        return "有效性摘要不可用"
    if state == "legacy":
        return "旧版接口未提供有效性摘要"
    return "有效性暂不可用"


def _quality_label(context: dict[str, Any]) -> str:
    quality = context.get("quality") if isinstance(context.get("quality"), dict) else {}
    reasons = {
        _short_text(reason, 80)
        for reason in quality.get("degraded_reasons", [])
        if isinstance(reason, str)
    }
    if {
        "consumer_signal_run_outside_window",
        "swing_audit_outside_decision_window",
    } & reasons:
        return "非正式时点补跑已隔离"
    if {
        "consumer_signal_effectiveness_invalid",
        "swing_effectiveness_invalid",
    } & reasons:
        return "有效性摘要不可用"
    if {
        "consumer_signal_run_failed",
        "consumer_signal_run_stuck",
        "swing_audit_failed",
    } & reasons:
        return "量化日决策运行失败"
    if {
        "consumer_signal_audit_invalid",
        "consumer_signal_audit_inconsistent",
        "swing_audit_invalid",
    } & reasons:
        return "量化运行审计不可用"
    if {
        "consumer_signal_run_partial",
        "swing_audit_partial",
    } & reasons:
        return "量化日决策运行受限"
    if "consumer_signal_audit_stale" in reasons or "swing_audit_stale" in reasons:
        return "量化日决策审计未更新"
    if "factor_history_insufficient" in reasons:
        return "因子历史积累中"
    if "consumer_market_factor_degraded" in reasons:
        return "部分市场因子尚未就绪"
    if any("risk" in reason for reason in reasons):
        return "风控数据质量受限"
    if any("market" in reason for reason in reasons):
        return "行情数据质量受限"
    return "部分数据质量受限" if reasons else "数据正常"


def _number_label(value: Any, digits: int = 2) -> str:
    number = _finite_number(value)
    if number is None:
        return ""
    formatted = f"{float(number):.{digits}f}"
    return formatted if digits <= 0 else formatted.rstrip("0").rstrip(".")


def render_quant_market_context_html(context: Any) -> str:
    """Render two independent AI-theme and Quant-risk evidence axes."""
    context = context if isinstance(context, dict) else _neutral_public_context()
    available = bool(context.get("available"))
    degraded = available and context.get("status") == "degraded"
    comparison = (
        context.get("comparison")
        if isinstance(context.get("comparison"), dict)
        else _build_comparison({}, context)
    )
    risk_color = _risk_color(context)
    direction_color = _direction_axis_color(comparison)
    card_color = _card_color(context, comparison)
    risk_label = _risk_label(context)
    market = context.get("market") if isinstance(context.get("market"), dict) else {}
    risk = context.get("risk") if isinstance(context.get("risk"), dict) else {}
    cache_age_label = _age_label(
        context.get("age_seconds"), unavailable="无新鲜快照"
    )
    risk_age_label = _age_label(
        risk.get("age_seconds"), unavailable="观测时间未知"
    )
    state_label = (
        "只读 · 部分降级"
        if degraded
        else "只读"
        if available
        else "不可达或已过期，已中性降级"
    )
    breadth = market.get("breadth") if isinstance(market.get("breadth"), dict) else {}
    limit_up = _number_label(breadth.get("limit_up"), 0)
    limit_down = _number_label(breadth.get("limit_down"), 0)
    market_evidence = []
    if limit_up or limit_down:
        market_evidence.append(
            f"收盘涨停 {limit_up or '—'} / 跌停 {limit_down or '—'}"
        )
    macro_evidence = []
    vix = _number_label(risk.get("vix"))
    us_10y = _number_label(risk.get("us_10y"))
    crude_oil = _number_label(risk.get("crude_oil"))
    if vix:
        macro_evidence.append(f"VIX {vix}")
    if us_10y:
        macro_evidence.append(f"美债10Y {us_10y}%")
    if crude_oil:
        macro_evidence.append(f"原油 {crude_oil}")
    ai_label = _short_text(comparison.get("ai_label"), 32) or "中性"
    relation_label = (
        _short_text(comparison.get("relation_label"), 48) or "双轴独立观察"
    )
    quant_bias_label = {
        "bullish": "偏多",
        "bearish": "偏空",
        "mixed": "多空混合",
        "neutral": "未确认",
    }.get(_short_text(comparison.get("swing_bias"), 32), "未确认")
    risk_summary = _risk_axis_summary(comparison)
    market_slice_label = _market_slice_label(context)
    quality_label = _quality_label(context)
    run_evidence = _swing_label(context)
    effectiveness_label = _effectiveness_label(context)
    market_line = market_slice_label
    if market_evidence:
        market_line += "：" + " · ".join(market_evidence)
    macro_line = "宏观指标：" + (
        " · ".join(macro_evidence) if macro_evidence else "暂不可用"
    )
    macro_line += f" · 风控观测 {risk_age_label}"
    return (
        f'<div class="quant-market-context" style="border-left:3px solid {card_color};'
        'padding:9px 11px;margin:0 0 12px;border-radius:10px;'
        'background:rgba(113,113,122,0.09);font-size:12px;line-height:1.55;">'
        '<div style="display:flex;align-items:center;gap:7px;flex-wrap:wrap;font-weight:750;margin-bottom:3px;">'
        'AI × Quant 双轴旁证 '
        f'<span style="font-weight:500;color:var(--color-text-subdue,inherit);">{html_escape(state_label)}</span></div>'
        '<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:2px 0 3px;">'
        f'<span style="font-size:11px;padding:1px 6px;border-radius:999px;border:1px solid {direction_color};color:{direction_color};">方向关系 {html_escape(relation_label)}</span>'
        f'<span style="font-size:11px;padding:1px 6px;border-radius:999px;border:1px solid {risk_color};color:{risk_color};">风险约束 {html_escape(risk_label)}</span></div>'
        f'<div>方向关系：<b>{html_escape(relation_label)}</b> <span style="color:var(--color-text-subdue,inherit);">· AI题材{html_escape(ai_label)} · Quant技术{html_escape(quant_bias_label)}（非当日涨跌判断）</span></div>'
        f'<div>风险约束：<b>{html_escape(risk_summary)}</b></div>'
        f'<div>运行证据：<b>{html_escape(run_evidence)}</b></div>'
        f'<div>历史有效性：<b>{html_escape(effectiveness_label)}</b></div>'
        f'<div style="color:var(--color-text-subdue,inherit);">{html_escape(market_line)}</div>'
        f'<div style="color:var(--color-text-subdue,inherit);">{html_escape(macro_line)}</div>'
        f'<div style="color:var(--color-text-subdue,inherit);">快照同步 {html_escape(cache_age_label)} · 数据质量：{html_escape(quality_label)}</div>'
        '<div style="color:var(--color-text-subdue,inherit);font-size:11px;">'
        'A股为收盘切片，海外风险为独立风控轴；仅作旁证，不参与 AI 筛选、评分或交易决策</div></div>'
    )


def quant_market_context_text(context: Any) -> str:
    """Return a bounded plain-text summary for Markdown/Telegram surfaces."""
    context = context if isinstance(context, dict) else _neutral_public_context()
    if not context.get("available"):
        return "AI × Quant 双轴旁证：暂不可用或已过期（未参与筛选/交易决策）"
    risk = context.get("risk") if isinstance(context.get("risk"), dict) else {}
    comparison = (
        context.get("comparison")
        if isinstance(context.get("comparison"), dict)
        else _build_comparison({}, context)
    )
    risk_age_label = _age_label(
        risk.get("age_seconds"), unavailable="观测时间未知"
    )
    return (
        f"AI × Quant 双轴旁证：方向关系 {_short_text(comparison.get('relation_label'), 48) or '双轴独立观察'}"
        "（非当日涨跌判断）；"
        f"风险约束 {_risk_axis_summary(comparison)}；运行证据 {_swing_label(context)}；"
        f"历史有效性 {_effectiveness_label(context)}；{_market_slice_label(context)}；"
        f"风控观测 {risk_age_label}；{_quality_label(context)}"
        "（A股为收盘切片，海外风险为独立风控轴；只读，未参与筛选/交易决策）"
    )


def _reset_quant_context_state_for_tests() -> None:
    """Test-only lifecycle reset."""
    stop_quant_market_context_poller(timeout=1.0)
    _POLLER_STOP.clear()
