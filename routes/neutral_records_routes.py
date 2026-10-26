"""Browser-accessible, authenticated read-only view of neutral cache records."""

import logging

from flask import Blueprint, Response, render_template, request

from core.user_feedback import resolve_scoring_contract
from db.neutral_records import read_neutral_records
from routes.browser_auth import (
    apply_browser_security_headers,
    browser_error,
    dashboard_login_redirect,
    get_dashboard_auth,
    request_prefers_html,
)
from routes.shared import _cfg
from services.neutral_records import NEUTRAL_RECORDS_PATH, present_neutral_records


logger = logging.getLogger(__name__)
neutral_records_bp = Blueprint("neutral_records", __name__)


@neutral_records_bp.route(NEUTRAL_RECORDS_PATH, methods=["GET"])
def neutral_records_page():
    auth = get_dashboard_auth()
    if auth is None:
        if request.headers.get("X-API-Key") is None and request_prefers_html():
            return dashboard_login_redirect(next_path=NEUTRAL_RECORDS_PATH)
        return browser_error("Unauthorized", 401)
    if request.args:
        return browser_error("This view has a fixed current-contract 24h window", 400)
    try:
        cfg = _cfg()
        version = str(resolve_scoring_contract(cfg, register_snapshot=False).get("scoring_strategy_version") or "")
        snapshot = read_neutral_records(version)
        records = present_neutral_records(snapshot["records"], cfg)
    except Exception:
        logger.warning("Neutral records view unavailable", exc_info=True)
        snapshot = {"available": False, "strategy_version": "", "has_more": False}
        records = []
    response = Response(
        render_template("neutral_records.html", snapshot=snapshot, records=records),
        status=200 if snapshot["available"] else 503,
        mimetype="text/html",
    )
    return apply_browser_security_headers(response)
