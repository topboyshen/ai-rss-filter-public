"""Browser-only authentication for the administrative Dashboard.

The signed cookie is deliberately scoped to the Dashboard UI.  It never
authorizes API, refresh, Digest, SSE, or webhook endpoints; those continue to
require their existing explicit credentials.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qsl, urlencode, urlsplit

from flask import Blueprint, Response, redirect, render_template, request
from itsdangerous import BadData, URLSafeTimedSerializer

from routes.security import _rate_limit_client_ip, request_from_trusted_proxy
from routes.shared import (
    _security_cfg,
    check_api_key_header,
    constant_time_text_equal,
    credential_text,
)
from utils.report_paging import parse_report_paging, parse_weekly_paging


logger = logging.getLogger(__name__)
browser_auth_bp = Blueprint("browser_auth", __name__)

_SESSION_COOKIE = "__Host-ai-filter-dashboard"
_LOGIN_CSRF_COOKIE = "__Host-ai-filter-login-csrf"
_SESSION_SALT = "ai-filter-dashboard-session-v1"
_LOGIN_CSRF_SALT = "ai-filter-dashboard-login-csrf-v1"
_SESSION_VERSION = 1
_LOGIN_CSRF_TTL_SECONDS = 900
_LOGIN_RATE_WINDOW_SECONDS = 900
_LOGIN_RATE_MAX_ATTEMPTS = 5
_SESSION_STORE_PREFIX = "ai-filter:dashboard-session"
_ALLOWED_NEXT_PATHS = frozenset(
    {"/dashboard", "/dashboard/events", "/dashboard/events/daily",
     "/dashboard/events/weekly", "/dashboard/neutral-records"}
)
_STOCK_NEXT_PATH_RE = re.compile(
    r"/dashboard/stocks/(?:SSE|SZSE|BSE)/[0-9]{6}\Z"
)
_EVENT_NEXT_QUERY_KEYS = ("event_id", "has_stocks", "tab")
_EVENT_NEXT_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z"
)
_EVENT_NEXT_TABS = frozenset(
    {"overview", "detail", "market", "outcomes", "reaction", "similar"}
)
_DAILY_NEXT_PATH = "/dashboard/events/daily"
_WEEKLY_NEXT_PATH = "/dashboard/events/weekly"
_DAILY_NEXT_DATE_RE = re.compile(
    r"20[0-9]{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])\Z"
)


@dataclass(frozen=True)
class DashboardAuth:
    kind: str
    csrf_token: str = ""
    session_id: str = ""


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def dashboard_session_ttl_seconds() -> int:
    return _bounded_int(
        "AI_FILTER_DASHBOARD_SESSION_TTL_SECONDS", 8 * 3600, 300, 12 * 3600
    )


def _read_secret_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = handle.read(4097)
    except OSError:
        return ""
    if len(value) > 4096:
        return ""
    return value.strip()


def dashboard_session_secret() -> str:
    """Return a dedicated signing secret or fail closed with an empty value."""
    direct = (os.environ.get("AI_FILTER_SESSION_SECRET") or "").strip()
    path = (os.environ.get("AI_FILTER_SESSION_SECRET_FILE") or "").strip()
    from_file = _read_secret_file(path) if path else ""
    if direct and from_file and not constant_time_text_equal(direct, from_file):
        logger.error("Dashboard session secret sources disagree")
        return ""
    value = from_file or direct
    if not (32 <= len(value.encode("utf-8")) <= 4096):
        return ""
    api_key = credential_text(_security_cfg().get("api_key"))
    if api_key and constant_time_text_equal(value, api_key):
        logger.error("Dashboard session secret must not reuse the API key")
        return ""
    return value


def browser_login_ready() -> bool:
    return bool(
        dashboard_session_secret()
        and credential_text(_security_cfg().get("api_key"))
    )


def _serializer(secret: str, salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        secret_key=secret,
        salt=salt,
        signer_kwargs={"digest_method": hashlib.sha256},
    )


def _api_key_tag(secret: str) -> str:
    api_key = credential_text(_security_cfg().get("api_key"))
    if not api_key:
        return ""
    return hmac.new(
        secret.encode("utf-8"), api_key.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _cookie_options(max_age: int) -> dict:
    return {
        "max_age": max_age,
        "secure": True,
        "httponly": True,
        "samesite": "Strict",
        "path": "/",
    }


def apply_browser_security_headers(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data: https:; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "script-src 'none'; object-src 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=()"
    )
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    response.headers["Vary"] = "Accept, Cookie, X-API-Key"
    return response


def browser_error(message: str, status: int) -> Response:
    return apply_browser_security_headers(
        Response(
            json.dumps({"error": message}, ensure_ascii=False),
            status=status,
            mimetype="application/json",
        )
    )


def request_prefers_html() -> bool:
    return "text/html" in (request.headers.get("Accept") or "").lower()


def _request_is_https() -> bool:
    if request.is_secure:
        return True
    if not request_from_trusted_proxy():
        return False
    forwarded = (request.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0]
    return forwarded.strip().lower() == "https"


def _safe_event_next_query(query: str) -> str | None:
    """Canonicalize only the event selectors, never arbitrary return arguments."""
    try:
        pairs = parse_qsl(
            query, keep_blank_values=True, strict_parsing=True,
            max_num_fields=len(_EVENT_NEXT_QUERY_KEYS), errors="strict",
        )
    except (ValueError, UnicodeError):
        return None
    values = dict(pairs)
    if len(values) != len(pairs) or set(values) - set(_EVENT_NEXT_QUERY_KEYS):
        return None
    for key, value in values.items():
        if key == "event_id" and not _EVENT_NEXT_ID_RE.fullmatch(value):
            return None
        if key == "has_stocks" and value not in {"0", "1"}:
            return None
        if key == "tab" and value not in _EVENT_NEXT_TABS:
            return None
    return urlencode([(key, values[key]) for key in _EVENT_NEXT_QUERY_KEYS if key in values])


def _safe_daily_next(value: str) -> str | None:
    """Canonicalize a strict date and optional bounded block/page selector."""
    if "#" in value:
        return None
    try:
        parts = urlsplit(value)
        pairs = parse_qsl(
            parts.query, keep_blank_values=True, strict_parsing=True,
            max_num_fields=3, errors="strict",
        )
    except (ValueError, UnicodeError):
        return None
    if parts.scheme or parts.netloc or parts.fragment or parts.path != _DAILY_NEXT_PATH:
        return None
    values = dict(pairs)
    if (
        len(values) != len(pairs)
        or set(values) - {"date", "block", "page"}
        or "date" not in values
    ):
        return None
    selected = values["date"]
    if not _DAILY_NEXT_DATE_RE.fullmatch(selected):
        return None
    try:
        date.fromisoformat(selected)
        block, page = parse_report_paging(values.get("block"), values.get("page"))
    except ValueError:
        return None
    query = {"date": selected}
    if block is not None:
        query.update(block=block, page=str(page))
    return f"{_DAILY_NEXT_PATH}?{urlencode(query)}"


def _safe_next(value: str) -> str:
    if not isinstance(value, str) or len(value) > 512:
        return "/dashboard"
    if "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return "/dashboard"
    if value in _ALLOWED_NEXT_PATHS or _STOCK_NEXT_PATH_RE.fullmatch(value):
        return value
    if value.startswith(_DAILY_NEXT_PATH + "?"):
        return _safe_daily_next(value) or "/dashboard"
    if value.startswith(_WEEKLY_NEXT_PATH + "?"):
        return _safe_weekly_next(value) or "/dashboard"
    if not value.startswith("/dashboard/events?"):
        return "/dashboard"
    try:
        parts = urlsplit(value)
    except ValueError:
        return "/dashboard"
    if parts.scheme or parts.netloc or parts.fragment or parts.path != "/dashboard/events":
        return "/dashboard"
    query = _safe_event_next_query(parts.query)
    if query is None:
        return "/dashboard"
    return "/dashboard/events" + (f"?{query}" if query else "")


def _safe_weekly_next(value: str) -> str | None:
    """Canonicalize a strict Monday and optional bounded block/page selector."""
    if "#" in value:
        return None
    try:
        parts = urlsplit(value)
        pairs = parse_qsl(
            parts.query, keep_blank_values=True, strict_parsing=True,
            max_num_fields=3, errors="strict",
        )
    except (ValueError, UnicodeError):
        return None
    if parts.scheme or parts.netloc or parts.fragment or parts.path != _WEEKLY_NEXT_PATH:
        return None
    values = dict(pairs)
    if (
        len(values) != len(pairs)
        or set(values) - {"week", "block", "page"}
        or "week" not in values
    ):
        return None
    selected = values["week"]
    if not _DAILY_NEXT_DATE_RE.fullmatch(selected):
        return None
    try:
        if date.fromisoformat(selected).weekday() != 0:
            return None
        block, page = parse_weekly_paging(values.get("block"), values.get("page"))
    except ValueError:
        return None
    query = {"week": selected}
    if block is not None:
        query.update(block=block, page=str(page))
    return f"{_WEEKLY_NEXT_PATH}?{urlencode(query)}"


def _login_csrf_token(secret: str) -> str:
    return _serializer(secret, _LOGIN_CSRF_SALT).dumps(
        {"v": _SESSION_VERSION, "purpose": "login", "nonce": secrets.token_urlsafe(24)}
    )


def _valid_login_csrf(secret: str, form_token: str) -> bool:
    cookie_token = request.cookies.get(_LOGIN_CSRF_COOKIE, "")
    if (
        not isinstance(form_token, str)
        or not isinstance(cookie_token, str)
        or not form_token
        or not cookie_token
        or len(form_token) > 4096
        or len(cookie_token) > 4096
    ):
        return False
    if not constant_time_text_equal(form_token, cookie_token):
        return False
    try:
        payload = _serializer(secret, _LOGIN_CSRF_SALT).loads(
            form_token, max_age=_LOGIN_CSRF_TTL_SECONDS
        )
    except BadData:
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("v") == _SESSION_VERSION
        and payload.get("purpose") == "login"
        and payload.get("nonce")
    )


def _render_login(
    error: str = "", status: int = 200, *, next_path: str = ""
) -> Response:
    secret = dashboard_session_secret()
    ready = bool(secret and credential_text(_security_cfg().get("api_key")))
    token = _login_csrf_token(secret) if ready else ""
    response = Response(
        render_template(
            "dashboard_login.html",
            error=error,
            configured=ready,
            csrf_token=token,
            next_path=_safe_next(next_path),
            session_hours=dashboard_session_ttl_seconds() // 3600,
        ),
        status=status,
        mimetype="text/html",
    )
    if token:
        response.set_cookie(
            _LOGIN_CSRF_COOKIE,
            token,
            **_cookie_options(_LOGIN_CSRF_TTL_SECONDS),
        )
    return apply_browser_security_headers(response)


def _login_attempt_key(secret: str) -> str:
    client = _rate_limit_client_ip()
    digest = hmac.new(
        secret.encode("utf-8"), client.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]
    return f"ai-filter:dashboard-login:{digest}"


def _redis_client():
    from services.shared_cache import redis_client

    client = redis_client()
    if client is None:
        raise RuntimeError("redis unavailable")
    return client


def _consume_login_attempt(secret: str) -> tuple[bool, int]:
    client = _redis_client()
    key = _login_attempt_key(secret)
    script = """
local value = redis.call('INCR', KEYS[1])
if value == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return value
"""
    try:
        count = int(client.eval(script, 1, key, _LOGIN_RATE_WINDOW_SECONDS))
        return count <= _LOGIN_RATE_MAX_ATTEMPTS, count
    finally:
        client.close()


def _clear_login_attempts(secret: str) -> None:
    client = _redis_client()
    try:
        client.delete(_login_attempt_key(secret))
    finally:
        client.close()


def _session_store_key(secret: str, session_id: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), session_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{_SESSION_STORE_PREFIX}:{digest}"


def _store_session(secret: str, session_id: str) -> None:
    client = _redis_client()
    try:
        stored = client.set(
            _session_store_key(secret, session_id),
            _api_key_tag(secret),
            ex=dashboard_session_ttl_seconds(),
        )
        if not stored:
            raise RuntimeError("session store rejected write")
    finally:
        client.close()


def _session_record_valid(secret: str, session_id: str) -> bool:
    client = _redis_client()
    try:
        value = client.get(_session_store_key(secret, session_id))
    finally:
        client.close()
    return constant_time_text_equal(credential_text(value), _api_key_tag(secret))


def _revoke_session(secret: str, session_id: str) -> None:
    client = _redis_client()
    try:
        client.delete(_session_store_key(secret, session_id))
    finally:
        client.close()


def _session_token(secret: str) -> tuple[str, str, str]:
    csrf = secrets.token_urlsafe(32)
    session_id = secrets.token_urlsafe(32)
    token = _serializer(secret, _SESSION_SALT).dumps(
        {
            "v": _SESSION_VERSION,
            "purpose": "dashboard",
            "csrf": csrf,
            "sid": session_id,
            "api_tag": _api_key_tag(secret),
        }
    )
    return token, csrf, session_id


def _session_auth() -> DashboardAuth | None:
    secret = dashboard_session_secret()
    token = request.cookies.get(_SESSION_COOKIE, "")
    if not secret or not token:
        return None
    try:
        payload = _serializer(secret, _SESSION_SALT).loads(
            token, max_age=dashboard_session_ttl_seconds()
        )
    except BadData:
        return None
    expected_tag = _api_key_tag(secret)
    supplied_tag = str(payload.get("api_tag") or "") if isinstance(payload, dict) else ""
    csrf = str(payload.get("csrf") or "") if isinstance(payload, dict) else ""
    session_id = str(payload.get("sid") or "") if isinstance(payload, dict) else ""
    if not (
        isinstance(payload, dict)
        and payload.get("v") == _SESSION_VERSION
        and payload.get("purpose") == "dashboard"
        and expected_tag
        and supplied_tag
        and constant_time_text_equal(supplied_tag, expected_tag)
        and csrf
        and session_id
    ):
        return None
    try:
        if not _session_record_valid(secret, session_id):
            return None
    except Exception as exc:
        logger.warning("Dashboard session store unavailable: %s", type(exc).__name__)
        return None
    return DashboardAuth(kind="session", csrf_token=csrf, session_id=session_id)


def get_dashboard_auth() -> DashboardAuth | None:
    # An explicitly supplied bad header must never silently fall back to an
    # ambient browser cookie.
    if request.headers.get("X-API-Key") is not None:
        return DashboardAuth(kind="header") if check_api_key_header() else None
    if not _request_is_https():
        return None
    return _session_auth()


def verify_session_csrf(auth: DashboardAuth) -> bool:
    if auth.kind == "header":
        return True
    provided = credential_text(request.form.get("csrf_token"))
    return bool(provided) and constant_time_text_equal(provided, auth.csrf_token)


def dashboard_login_redirect(next_path: str = "/dashboard") -> Response:
    # Encode the nested query as one value; an unescaped '&' loses the selected
    # event's filters before the login form even sees them.
    query = urlencode({"next": _safe_next(next_path)}, safe="/")
    response = redirect(f"/dashboard/login?{query}", code=302)
    return apply_browser_security_headers(response)


@browser_auth_bp.route("/dashboard/login", methods=["GET"])
def dashboard_login():
    if not _request_is_https():
        return browser_error("HTTPS required", 400)
    if get_dashboard_auth() is not None:
        return apply_browser_security_headers(
            redirect(_safe_next(request.args.get("next", "")), code=303)
        )
    if not browser_login_ready():
        return _render_login(
            "浏览器登录尚未完成安全配置。",
            status=503,
            next_path=request.args.get("next", ""),
        )
    return _render_login(next_path=request.args.get("next", ""))


@browser_auth_bp.route("/dashboard/login", methods=["POST"])
def dashboard_login_submit():
    if not _request_is_https():
        return browser_error("HTTPS required", 400)
    secret = dashboard_session_secret()
    expected = credential_text(_security_cfg().get("api_key"))
    if not secret or not expected:
        return _render_login(
            "浏览器登录尚未完成安全配置。",
            status=503,
            next_path=request.form.get("next", ""),
        )
    if not _valid_login_csrf(secret, request.form.get("csrf_token", "")):
        return _render_login(
            "登录页面已过期，请重新提交。",
            status=403,
            next_path=request.form.get("next", ""),
        )
    try:
        allowed, _count = _consume_login_attempt(secret)
    except Exception as exc:
        logger.warning("Dashboard login limiter unavailable: %s", type(exc).__name__)
        return _render_login(
            "登录保护服务暂不可用，请稍后重试。",
            status=503,
            next_path=request.form.get("next", ""),
        )
    if not allowed:
        response = _render_login(
            "尝试次数过多，请稍后重试。",
            status=429,
            next_path=request.form.get("next", ""),
        )
        response.headers["Retry-After"] = str(_LOGIN_RATE_WINDOW_SECONDS)
        return response
    supplied = credential_text(request.form.get("api_key"))
    if (
        not supplied
        or len(supplied) > 4096
        or not constant_time_text_equal(supplied, expected)
    ):
        return _render_login(
            "凭据无效。",
            status=401,
            next_path=request.form.get("next", ""),
        )
    try:
        _clear_login_attempts(secret)
    except Exception:
        logger.warning("Dashboard login limiter reset failed", exc_info=True)
    token, _csrf, session_id = _session_token(secret)
    try:
        _store_session(secret, session_id)
    except Exception as exc:
        logger.warning("Dashboard session store write failed: %s", type(exc).__name__)
        return _render_login(
            "会话保护服务暂不可用，请稍后重试。",
            status=503,
            next_path=request.form.get("next", ""),
        )
    response = apply_browser_security_headers(
        redirect(_safe_next(request.form.get("next", "")), code=303)
    )
    response.set_cookie(
        _SESSION_COOKIE,
        token,
        **_cookie_options(dashboard_session_ttl_seconds()),
    )
    response.delete_cookie(
        _LOGIN_CSRF_COOKIE, path="/", secure=True, httponly=True, samesite="Strict"
    )
    return response


@browser_auth_bp.route("/dashboard/logout", methods=["POST"])
def dashboard_logout():
    if not _request_is_https():
        return browser_error("HTTPS required", 400)
    auth = get_dashboard_auth()
    if auth is None or auth.kind != "session" or not verify_session_csrf(auth):
        return browser_error("Forbidden", 403)
    secret = dashboard_session_secret()
    try:
        _revoke_session(secret, auth.session_id)
    except Exception as exc:
        logger.warning("Dashboard session revocation failed: %s", type(exc).__name__)
        return browser_error("Session service unavailable", 503)
    response = apply_browser_security_headers(redirect("/dashboard/login", code=303))
    response.delete_cookie(
        _SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="Strict"
    )
    response.delete_cookie(
        _LOGIN_CSRF_COOKIE, path="/", secure=True, httponly=True, samesite="Strict"
    )
    return response
