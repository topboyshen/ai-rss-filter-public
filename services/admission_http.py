"""Conservative HTTP admission, after existing signature authentication.

Only audited, exact endpoint/rule pairs are read-only exemptions. A method name
or URL prefix is never an exemption. This layer owns synchronous request work;
background executors must own their own transferable activity lease.
"""
from __future__ import annotations

import logging
import os

from flask import Response, g, request

from services import admission_fence

logger = logging.getLogger(__name__)
_REQUEST_LEASE = "_admission_http_lease"
_READ_ENDPOINTS = {
    ("admin.health", "/health"),
    ("event_radar.event_radar_page", "/dashboard/events"),
    ("event_radar.event_daily_report_page", "/dashboard/events/daily"),
    ("event_radar.event_weekly_report_page", "/dashboard/events/weekly"),
}


def request_kind() -> str | None:
    """Return intake/drain, or None for an exact audited read-only endpoint."""
    rule = request.url_rule.rule if request.url_rule is not None else None
    pair = (request.endpoint, rule)
    if request.method == "POST" and pair == ("webhook.webhook_receipt", "/webhook/receipt"):
        # Receipt lookup may recover/strictly sync files: it is not a pure read.
        return "drain"
    if request.method in {"GET", "HEAD"} and pair in _READ_ENDPOINTS:
        # Do not duplicate business flag parsing. Even false/duplicate flags
        # take the conservative lane, rather than relying on first-value rules.
        if "llm" not in request.args and "push" not in request.args:
            return None
    return "intake"


def _unavailable() -> Response:
    response = Response('{"error":"admission_unavailable","durable":false}',
                        status=503, mimetype="application/json")
    response.headers["Retry-After"] = "30"
    response.headers["Cache-Control"] = "no-store"
    return response


def _declared_request_bytes() -> int:
    """Reserve declared bytes; unknown/ambiguous body framing is not zero."""
    raw = request.environ.get("CONTENT_LENGTH", "")
    transfer_encoding = request.headers.get("Transfer-Encoding", "")
    terminated = request.environ.get("wsgi.input_terminated", False)
    if transfer_encoding or (terminated and not raw):
        raise admission_fence.AdmissionClosed("unknown declared request bytes")
    if raw and (not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal()):
        raise admission_fence.AdmissionClosed("invalid declared request bytes")
    return int(raw) if raw else 0


def _begin_request():
    if os.environ.get("AI_FILTER_ADMISSION_MODE", "disabled") == "disabled":
        return None
    kind = request_kind()
    if kind is None:
        return None
    try:
        declared_request_bytes = _declared_request_bytes()
        # This is an admission reservation, not actual/persisted body usage.
        lease = admission_fence.reserve_lease(
            "http_request", kind=kind, charge_bytes=declared_request_bytes
        )
        activation = lease.activate()
        try:
            activation.__enter__()
        except BaseException:
            lease.finish(False)
            raise
        setattr(g, _REQUEST_LEASE, (lease, activation))
    except Exception:
        logger.warning("HTTP admission refused", exc_info=False)
        return _unavailable()
    return None


def _finish_request(success: bool) -> bool:
    owned = g.pop(_REQUEST_LEASE, None)
    if owned is None:
        return True
    lease, activation = owned
    try:
        activation.__exit__(None, None, None)
        lease.finish(success)
    except Exception:
        # The lease's original evidence remains. Never turn a failed strict
        # finish into a successful/empty activity proof or an HTTP 202.
        logger.error("HTTP admission finish unconfirmed", exc_info=False)
        return False
    return True


def _after_request(response):
    if not hasattr(g, _REQUEST_LEASE):
        return response
    if response.is_streamed:
        # A generator could mutate after Flask teardown. No producer streaming
        # contract is implemented; do not execute it outside the held lease.
        try:
            response.close()
        finally:
            _finish_request(False)
        return _unavailable()
    if not _finish_request(response.status_code < 500):
        return _unavailable()
    return response


def _teardown_request(error):
    # Flask can invoke teardown twice for a preserved test/request context.
    # Pop ownership once. If after_request never ran, completion is uncertain.
    if hasattr(g, _REQUEST_LEASE):
        _finish_request(False)


def install_http_admission(app) -> None:
    """Install after the existing webhook signature gate, before other hooks."""
    if app.extensions.get("ai_filter_http_admission"):
        return
    app.extensions["ai_filter_http_admission"] = True
    app.before_request(_begin_request)
    app.after_request(_after_request)
    app.teardown_request(_teardown_request)
