"""Fail-closed, resource-bounded AI -> Quant service API."""

from __future__ import annotations

import hmac
import hashlib
import json
import logging
import os
import re
import stat
import threading
import time
from typing import Any

from flask import Blueprint, Response, request

from services.integration_signals import (
    CursorProblem,
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    MAX_RESPONSE_BYTES,
    read_signal_revision_page,
)


logger = logging.getLogger(__name__)
integration_bp = Blueprint("integration", __name__, url_prefix="/integration/v1")

_TOKEN_ENV = "AI_FILTER_INTEGRATION_TOKEN"
_TOKEN_FILE_ENV = "AI_FILTER_INTEGRATION_TOKEN_FILE"
_MIN_TOKEN_BYTES = 32
_MAX_TOKEN_BYTES = 4096
_MAX_SQLITE_ID = (1 << 63) - 1
_HEX_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_UNSIGNED_DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_WIRE_VERSIONS = {"v3": 3, "v4": 4}

_INTEGRATION_SLOTS = threading.BoundedSemaphore(2)
_RATE_LOCK = threading.Lock()
_RATE_WINDOW_STARTED = 0.0
_RATE_WINDOW_COUNT = 0


def _json_response(
    payload: dict[str, Any],
    status: int = 200,
    *,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    body = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(body) > MAX_RESPONSE_BYTES:
        status = 503
        body = b'{"error":"integration_response_budget_exceeded"}'
    response = Response(body, status=status, mimetype="application/json")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'none'"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Vary"] = "Authorization"
    # This service-to-service route intentionally emits no CORS permission.
    for header in (
        "Access-Control-Allow-Origin",
        "Access-Control-Allow-Credentials",
        "Access-Control-Allow-Headers",
        "Access-Control-Allow-Methods",
    ):
        response.headers.pop(header, None)
    for name, value in (extra_headers or {}).items():
        response.headers[name] = value
    return response


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _rate_limit_allowed() -> tuple[bool, int]:
    global _RATE_WINDOW_STARTED, _RATE_WINDOW_COUNT
    now = time.monotonic()
    window_seconds = 60
    maximum = _bounded_env_int(
        "AI_FILTER_INTEGRATION_RATE_LIMIT_PER_MINUTE", 120, 10, 600
    )
    with _RATE_LOCK:
        if _RATE_WINDOW_STARTED <= 0 or now - _RATE_WINDOW_STARTED >= window_seconds:
            _RATE_WINDOW_STARTED = now
            _RATE_WINDOW_COUNT = 0
        if _RATE_WINDOW_COUNT >= maximum:
            retry_after = max(1, int(window_seconds - (now - _RATE_WINDOW_STARTED)))
            return False, retry_after
        _RATE_WINDOW_COUNT += 1
        return True, 0


def _valid_token(raw: object) -> str:
    token = raw.strip() if isinstance(raw, str) else ""
    try:
        token_bytes = token.encode("utf-8")
    except UnicodeError:
        return ""
    if (
        not _MIN_TOKEN_BYTES <= len(token_bytes) <= _MAX_TOKEN_BYTES
        or any(character.isspace() for character in token)
        or "\x00" in token
    ):
        return ""
    return token


def _token_from_file(path: str) -> str:
    if not path or len(path) > 4096 or "\x00" in path:
        return ""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return ""
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o022
            or metadata.st_size > _MAX_TOKEN_BYTES + 2
        ):
            return ""
        payload = os.read(descriptor, _MAX_TOKEN_BYTES + 3)
    except OSError:
        return ""
    finally:
        os.close(descriptor)
    if len(payload) > _MAX_TOKEN_BYTES + 2:
        return ""
    try:
        return _valid_token(payload.decode("utf-8"))
    except UnicodeError:
        return ""


def _configured_service_token() -> str:
    token_file = (os.environ.get(_TOKEN_FILE_ENV) or "").strip()
    if token_file:
        # A configured but unreadable secret file is a hard failure; never
        # fall back to a stale environment credential.
        return _token_from_file(token_file)
    return _valid_token(os.environ.get(_TOKEN_ENV))


def _bearer_token() -> str:
    raw = request.headers.get("Authorization", "")
    if not isinstance(raw, str) or len(raw) > _MAX_TOKEN_BYTES + 16:
        return ""
    scheme, separator, candidate = raw.partition(" ")
    if not separator or scheme.casefold() != "bearer":
        return ""
    return _valid_token(candidate)


def _service_auth_state() -> str:
    expected = _configured_service_token()
    if not expected:
        return "unavailable"
    candidate = _bearer_token()
    expected_digest = hashlib.sha256(expected.encode("utf-8")).digest()
    candidate_digest = hashlib.sha256(candidate.encode("utf-8")).digest()
    return (
        "authorized"
        if candidate and hmac.compare_digest(candidate_digest, expected_digest)
        else "unauthorized"
    )


def _one_query_value(name: str) -> str | None:
    values = request.args.getlist(name)
    if not values:
        return None
    if len(values) != 1:
        raise ValueError(f"{name} must appear once")
    return values[0]


def _parse_uint(name: str, *, default: int | None, maximum: int) -> int | None:
    raw = _one_query_value(name)
    if raw is None:
        return default
    if (
        not _UNSIGNED_DECIMAL_RE.fullmatch(raw)
        or len(raw) > len(str(maximum))
    ):
        raise ValueError(f"{name} must be an unsigned in-range integer")
    value = int(raw)
    if value > maximum:
        raise ValueError(f"{name} is out of range")
    return value


def _parse_stream_id(name: str) -> str | None:
    raw = _one_query_value(name)
    if raw is None:
        return None
    if not _HEX_ID_RE.fullmatch(raw):
        raise ValueError(f"{name} is malformed")
    return raw


def _parse_wire_version() -> int:
    raw = _one_query_value("schema_version")
    if raw is None:
        return 3
    version = _WIRE_VERSIONS.get(raw)
    if version is None:
        raise ValueError("schema_version must be v3 or v4")
    return version


@integration_bp.get("/signals")
def integration_signals() -> Response:
    auth_state = _service_auth_state()
    if auth_state == "unavailable":
        return _json_response({"error": "integration_auth_unavailable"}, 503)
    if auth_state != "authorized":
        return _json_response(
            {"error": "Unauthorized"},
            401,
            extra_headers={"WWW-Authenticate": "Bearer"},
        )

    # Only an authenticated service request may consume the Quant business
    # quota.  Anonymous traffic is constrained at the reverse-proxy edge and
    # must not be able to starve the trusted consumer's process-local budget.
    allowed, retry_after = _rate_limit_allowed()
    if not allowed:
        return _json_response(
            {"error": "integration_rate_limited"},
            429,
            extra_headers={"Retry-After": str(retry_after)},
        )

    unknown = sorted(
        set(request.args)
        - {"ledger_id", "epoch", "after_seq", "limit", "schema_version"}
    )
    if unknown:
        return _json_response(
            {"error": "invalid_query", "detail": "unknown query parameter"},
            400,
        )
    try:
        ledger_id = _parse_stream_id("ledger_id")
        epoch = _parse_stream_id("epoch")
        after_seq = _parse_uint("after_seq", default=None, maximum=_MAX_SQLITE_ID)
        limit = _parse_uint(
            "limit", default=DEFAULT_PAGE_SIZE, maximum=MAX_PAGE_SIZE
        )
        if limit is None or limit < 1:
            raise ValueError("limit must be at least 1")
        wire_version = _parse_wire_version()
    except ValueError as exc:
        return _json_response(
            {"error": "invalid_query", "detail": str(exc)},
            400,
        )

    if not _INTEGRATION_SLOTS.acquire(blocking=False):
        return _json_response(
            {"error": "integration_capacity_reached"},
            503,
            extra_headers={"Retry-After": "1"},
        )
    try:
        page = read_signal_revision_page(
            ledger_id=ledger_id,
            epoch=epoch,
            after_seq=after_seq,
            limit=limit,
            wire_version=wire_version,
        )
    except ValueError as exc:
        return _json_response(
            {"error": "invalid_query", "detail": str(exc)}, 400
        )
    except CursorProblem as exc:
        return _json_response(
            {
                "error": exc.code,
                "cursor": {
                    "ledger_id": exc.state["ledger_id"],
                    "epoch": exc.state["epoch"],
                    "floor_seq": exc.state["floor_seq"],
                    "last_seq": exc.state["last_seq"],
                },
            },
            exc.status,
        )
    except Exception:
        logger.exception("Integration signal outbox read failed")
        return _json_response({"error": "integration_temporarily_unavailable"}, 503)
    finally:
        _INTEGRATION_SLOTS.release()

    state = page["state"]
    return _json_response({
        "schema_version": f"ai-filter.integration.signals.v{wire_version}",
        "delivery_semantics": "at_least_once",
        "apply_order_field": "seq",
        "event_time_field": "available_at",
        "idempotency_field": "signal_revision_id",
        "items": page["items"],
        "count": len(page["items"]),
        "cursor": {
            "ledger_id": state["ledger_id"],
            "epoch": state["epoch"],
            "floor_seq": state["floor_seq"],
            "last_seq": state["last_seq"],
            "after_seq": page["after_seq"],
            "next_after_seq": page["next_after_seq"],
            "limit": page["limit"],
            "has_more": page["has_more"],
        },
    })
