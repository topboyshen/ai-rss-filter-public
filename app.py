import os

from logging_bootstrap import logging
from flask import Flask, Response, request, abort

from routes.feed_routes import feed_bp
from routes.admin_routes import admin_bp
from routes.dashboard_routes import dash_bp
from routes.event_radar_routes import event_radar_bp
from routes.security_history_routes import security_history_bp
from routes.browser_auth import browser_auth_bp
from routes.integration_routes import integration_bp
from routes.webhook_routes import webhook_bp
from routes.jin10_routes import jin10_bp
from routes.neutral_records_routes import neutral_records_bp
from routes.shared import (
    init_db as _init_db,
    _allowed_cors_origin,
    _security_cfg,
    check_api_key,
)
from core.config import config as config_holder
from sse_engine import glance_sse_widget
from routes.security import verify_miniflux_signature
from services.admission_http import install_http_admission
from services.controlled_startup import initialize_runtime_database

app = Flask(__name__)
logger = logging.getLogger(__name__)
try:
    app.config["MAX_CONTENT_LENGTH"] = max(
        1024, int(os.environ.get("AI_FILTER_MAX_REQUEST_BYTES", 1024 * 1024))
    )
except (TypeError, ValueError):
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024

@app.before_request
def _gate_webhook():
    """门口验签：/webhook 路径必须在任何路由逻辑之前验证签名"""
    if request.path in {"/webhook", "/webhook/receipt"} and request.method == "POST":
        if not verify_miniflux_signature():
            abort(401, description="Invalid Signature: Intrusion Detected")

# Keep the original signature check first; admission does not replace auth.
install_http_admission(app)

# ============================================================

# 向测试兼容暴露原有接口
config = config_holder
check_api_key = check_api_key
_allowed_cors_origin = _allowed_cors_origin


def initialize_app(*, slot=None, attempt=0) -> None:
    """Initialize persistent application state at an explicit startup boundary."""
    initialize_runtime_database("web", _init_db, "http_database_initialize", slot=slot, attempt=attempt)


# Backward-compatible helper for callers that already initialize a test or
# maintenance database explicitly. Importing this module no longer calls it.
init_db = initialize_app

# CORS: glance 等前端 SPA 可跨域访问只读端点
def _append_vary(response, value):
    values = {
        item.strip()
        for item in (response.headers.get("Vary") or "").split(",")
        if item.strip()
    }
    values.add(value)
    response.headers["Vary"] = ", ".join(sorted(values))


def _cors_isolated_path() -> bool:
    return request.path == "/dashboard" or request.path.startswith("/dashboard/")


@app.after_request
def _add_cors_headers(response):
    if request.path.startswith("/integration/") or _cors_isolated_path():
        for header in (
            "Access-Control-Allow-Origin",
            "Access-Control-Allow-Credentials",
            "Access-Control-Allow-Headers",
            "Access-Control-Allow-Methods",
        ):
            response.headers.pop(header, None)
        _append_vary(response, "Authorization")
        return response
    origin = _allowed_cors_origin()
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        if origin != "*":
            _append_vary(response, "Origin")
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key"
    return response

@app.before_request
def _handle_options():
    if request.method == "OPTIONS":
        if request.path.startswith("/integration/"):
            resp = Response(status=405)
            resp.headers["Allow"] = "GET"
            return resp
        if _cors_isolated_path():
            resp = Response(status=405)
            resp.headers["Allow"] = "GET, POST"
            return resp
        resp = Response()
        origin = _allowed_cors_origin()
        if origin:
            resp.headers["Access-Control-Allow-Origin"] = origin
            if origin != "*":
                _append_vary(resp, "Origin")
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key"
        return resp

if not (_security_cfg().get("api_key") or "").strip():
    logger.warning(
        "API key is not configured: protected endpoints are closed until a key is provided."
    )

# 注\u518c\u84dd\u56fe
app.register_blueprint(feed_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(dash_bp)
app.register_blueprint(event_radar_bp)
app.register_blueprint(security_history_bp)
app.register_blueprint(browser_auth_bp)
app.register_blueprint(integration_bp)
app.register_blueprint(webhook_bp)
app.register_blueprint(jin10_bp)
app.register_blueprint(neutral_records_bp)

# Glance polls this short-lived, SQLite-backed HTML endpoint every 30 seconds.
app.add_url_rule("/glance-sse", "glance_sse", glance_sse_widget, methods=["GET"])

if __name__ == "__main__":
    initialize_app()
    app.run(host="0.0.0.0", port=5000)
