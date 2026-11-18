"""Authenticated browser review of rejected records with a durable ledger."""

import logging
import re

from flask import Blueprint, Response, redirect, render_template, request

from db.rejection_review import (
    DECISION_CONFIRMED,
    DECISION_RESTORED,
    MAX_PAGE,
    find_rejected_record,
    read_rejection_page,
    record_decision,
    revoke_decision,
)
from routes.browser_auth import (
    apply_browser_security_headers,
    browser_error,
    dashboard_login_redirect,
    get_dashboard_auth,
    request_prefers_html,
    verify_session_csrf,
)
from routes.shared import _cfg
from services.navigation_origins import news_home_origin
from services.rejection_review import (
    REJECTIONS_PATH,
    decided_text,
    present_rejection_page,
)


logger = logging.getLogger(__name__)
rejection_review_bp = Blueprint("rejection_review", __name__)

_ALLOWED_ARGS = {"path", "reason", "decision", "page", "decided"}
_ALLOWED_FORM = {"csrf_token", "link", "action", "note"}
_ACTIONS = {"restore", "confirm", "revoke"}
_PATH_RE = re.compile(r"[A-Za-z0-9_]{0,64}\Z")
_REASON_RE = re.compile(r"(?:rule|none|[a-z0-9_]{1,64})?\Z")
_DECISION_RE = re.compile(r"(?:restored|confirmed)?\Z")


@rejection_review_bp.route(REJECTIONS_PATH, methods=["GET"])
def rejection_review_page():
    auth = get_dashboard_auth()
    if auth is None:
        if request.headers.get("X-API-Key") is None and request_prefers_html():
            return dashboard_login_redirect(next_path=REJECTIONS_PATH)
        return browser_error("Unauthorized", 401)
    if set(request.args) - _ALLOWED_ARGS:
        return browser_error("Unsupported query parameter", 400)

    path_filter = request.args.get("path", "").strip()
    reason_filter = request.args.get("reason", "").strip()
    decision_filter = request.args.get("decision", "").strip()
    if (
        not _PATH_RE.fullmatch(path_filter)
        or not _REASON_RE.fullmatch(reason_filter)
        or not _DECISION_RE.fullmatch(decision_filter)
    ):
        return browser_error("Invalid filter", 400)
    try:
        page = int(request.args.get("page", "1"))
    except (TypeError, ValueError):
        return browser_error("Invalid page", 400)
    if page < 1 or page > MAX_PAGE:
        return browser_error("Invalid page", 400)

    cfg = _cfg()
    try:
        snapshot = read_rejection_page(
            cfg,
            path_filter=path_filter,
            reason_filter=reason_filter,
            decision_filter=decision_filter,
            page=page,
        )
        view = present_rejection_page(snapshot, cfg)
    except Exception:
        logger.warning("Rejection review view unavailable", exc_info=True)
        snapshot = {"available": False, "window_hours": 72}
        view = {"items": [], "summary": None}

    decided = request.args.get("decided", "")
    view["decided_text"] = decided_text(decided) if decided in _ACTIONS else ""
    response = Response(
        render_template(
            "rejection_review.html",
            snapshot=snapshot,
            view=view,
            csrf_token=auth.csrf_token,
            news_home_origin=news_home_origin(),
        ),
        status=200 if snapshot["available"] else 503,
        mimetype="text/html",
    )
    return apply_browser_security_headers(response)


@rejection_review_bp.route(REJECTIONS_PATH + "/decision", methods=["POST"])
def rejection_decision():
    auth = get_dashboard_auth()
    if auth is None:
        return browser_error("Unauthorized", 401)
    if not verify_session_csrf(auth):
        return browser_error("Forbidden", 403)
    if set(request.form) - _ALLOWED_FORM:
        return browser_error("Unsupported form field", 400)

    link = request.form.get("link", "").strip()[:2048]
    action = request.form.get("action", "").strip()
    note = request.form.get("note", "").strip()[:500]
    if not link or action not in _ACTIONS:
        return browser_error("Invalid decision", 400)

    if action == "revoke":
        revoke_decision(link)
    else:
        decision = (
            DECISION_RESTORED if action == "restore" else DECISION_CONFIRMED
        )
        item = find_rejected_record(link, _cfg())
        if item is None:
            return browser_error(
                "Record is not a reviewable rejection in the current window",
                400,
            )
        record_decision(item, decision, note)

    response = redirect(f"{REJECTIONS_PATH}?decided={action}", code=303)
    return apply_browser_security_headers(response)
