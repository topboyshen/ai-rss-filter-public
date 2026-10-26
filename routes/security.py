"""
AI-Filter 安全工具模块：SSRF 防护、请求限速、API 密钥验证、Webhook HMAC
"""
import hashlib
import hmac
import http.client
import ipaddress
import os
import socket
import ssl
import time
import json
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from functools import wraps
from flask import Response, request

# SSRF 防护配置。默认策略是精确 feed allowlist；不要退回字符串黑名单。
_ALLOWED_SCHEMES = {"http", "https"}
_DEFAULT_PORTS = {"http": 80, "https": 443}
_DOCKER_HOST_REWRITE_GROUPS = (
    # RSSHub：宿主机访问地址与 Compose 服务名等价，但端口必须保持 1200。
    (frozenset({"127.0.0.1", "localhost", "rsshub", "172.17.0.1"}), frozenset({1200})),
    # AI-Filter Web：仅用于配置中的 Jin10 RSS 自调用端点。
    (frozenset({"127.0.0.1", "localhost", "ai-filter-web", "172.17.0.1"}), frozenset({5000})),
)


@dataclass(frozen=True)
class _Endpoint:
    raw: str
    scheme: str
    host: str
    port: int
    path: str
    query: str


def _parse_http_endpoint(value: object) -> tuple[_Endpoint | None, str]:
    raw = "" if value is None else str(value).strip()
    if not raw:
        return None, "URL 不能为空"
    if "\\" in raw or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return None, "URL 包含不允许的字符"
    try:
        parsed = urllib.parse.urlsplit(raw)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").casefold()
        port = parsed.port or _DEFAULT_PORTS.get(scheme)
    except (TypeError, ValueError):
        return None, "URL 格式无效"
    if scheme not in _ALLOWED_SCHEMES:
        return None, "只允许 HTTP/HTTPS"
    if not host or port is None:
        return None, "URL 缺少有效主机名"
    if parsed.username is not None or parsed.password is not None:
        return None, "URL 不允许携带用户凭据"
    if parsed.fragment:
        return None, "URL 不允许包含 fragment"
    if "%" in host:
        # 拒绝 zone-id 和 percent-encoded host，避免 URL/DNS 解析器语义不一致。
        return None, "URL 主机名格式无效"
    return _Endpoint(
        raw=raw,
        scheme=scheme,
        host=host,
        port=int(port),
        path=parsed.path or "/",
        query=parsed.query,
    ), ""


def _hosts_are_configured_rewrites(left: str, right: str, port: int) -> bool:
    if left == right:
        return True
    return any(
        port in ports and left in hosts and right in hosts
        for hosts, ports in _DOCKER_HOST_REWRITE_GROUPS
    )


def _endpoint_matches(target: _Endpoint, configured: _Endpoint) -> bool:
    return (
        target.scheme == configured.scheme
        and target.port == configured.port
        and target.path == configured.path
        and target.query == configured.query
        and _hosts_are_configured_rewrites(target.host, configured.host, target.port)
    )


def _matching_configured_endpoint(target: _Endpoint, cfg: dict) -> _Endpoint | None:
    for feed in (cfg or {}).get("feeds", []) or []:
        if not isinstance(feed, dict):
            continue
        configured, _error = _parse_http_endpoint(feed.get("url"))
        if configured is not None and _endpoint_matches(target, configured):
            return configured
    return None


def _security_flag(cfg: dict, name: str, default: bool = False) -> bool:
    value = ((cfg or {}).get("security", {}) or {}).get(name, default)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_addresses(endpoint: _Endpoint) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return {ipaddress.ip_address(endpoint.host)}
    except ValueError:
        pass

    infos = socket.getaddrinfo(
        endpoint.host,
        endpoint.port,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    addresses = {
        ipaddress.ip_address(info[4][0])
        for info in infos
        if info and len(info) >= 5 and info[4]
    }
    if not addresses:
        raise OSError("target hostname resolved to no addresses")
    return addresses


def _normalized_addresses(
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address],
) -> tuple[str, ...]:
    return tuple(
        str(address)
        for address in sorted(addresses, key=lambda item: (item.version, int(item)))
    )


def _is_declared_internal_endpoint(endpoint: _Endpoint) -> bool:
    if any(endpoint.host in hosts for hosts, _ports in _DOCKER_HOST_REWRITE_GROUPS):
        return True
    try:
        return not ipaddress.ip_address(endpoint.host).is_global
    except ValueError:
        # Compose/Kubernetes service names are normally single-label names.
        return "." not in endpoint.host


def _is_approved_configured_internal_endpoint(endpoint: _Endpoint) -> bool:
    return endpoint.scheme == "http" and any(
        endpoint.host in hosts and endpoint.port in ports
        for hosts, ports in _DOCKER_HOST_REWRITE_GROUPS
    )


def _is_internal_redirect_target(endpoint: _Endpoint) -> bool:
    if _is_declared_internal_endpoint(endpoint):
        return True
    try:
        return any(not address.is_global for address in _resolve_addresses(endpoint))
    except (OSError, ValueError):
        return True


# 简易请求限速器（内存存储，单实例有效）
_rate_limit_store: dict[str, tuple[float, int]] = {}
_rate_limit_lock = threading.Lock()

# 全局 /refresh 限流器（更严格：1次/分钟）
_refresh_rate_limit_store: dict[str, float] = {}
_refresh_rate_limit_lock = threading.Lock()


def _rate_limit_client_ip() -> str:
    """Return a gateway-observed peer only when the TCP peer is trusted.

    Arbitrary ``X-Forwarded-For`` remains untrusted.  The project gateway
    receives an edge-observed client address from NPM and overwrites
    ``X-AI-Filter-Client-IP``.  The API accepts that value only when
    ``request.remote_addr`` currently resolves to the exact Compose gateway
    service configured by the operator. Direct API callers therefore cannot
    spoof a new rate-limit bucket.
    """
    raw_peer = (request.remote_addr or "unknown").strip()
    try:
        peer = ipaddress.ip_address(raw_peer)
    except ValueError:
        return raw_peer or "unknown"

    trusted_host = (os.environ.get("AI_FILTER_TRUSTED_PROXY_HOST") or "").strip()
    if not trusted_host:
        return str(peer)
    try:
        trusted_addresses = {
            ipaddress.ip_address(row[4][0])
            for row in socket.getaddrinfo(
                trusted_host, 5000, type=socket.SOCK_STREAM
            )
        }
    except (OSError, ValueError, IndexError):
        return str(peer)
    if peer not in trusted_addresses:
        return str(peer)

    observed = (request.headers.get("X-AI-Filter-Client-IP") or "").strip()
    try:
        return str(ipaddress.ip_address(observed))
    except ValueError:
        return str(peer)


def request_from_trusted_proxy() -> bool:
    """Return True only when the TCP peer is the configured Compose gateway."""
    raw_peer = (request.remote_addr or "").strip()
    trusted_host = (os.environ.get("AI_FILTER_TRUSTED_PROXY_HOST") or "").strip()
    if not raw_peer or not trusted_host:
        return False
    try:
        peer = ipaddress.ip_address(raw_peer)
        trusted_addresses = {
            ipaddress.ip_address(row[4][0])
            for row in socket.getaddrinfo(
                trusted_host, 5000, type=socket.SOCK_STREAM
            )
        }
    except (OSError, ValueError, IndexError):
        return False
    return peer in trusted_addresses


def resolve_target_url(
    url: str,
    cfg: dict,
    *,
    redirect_from: str | None = None,
) -> tuple[bool, str, tuple[str, ...]]:
    """Validate and resolve one hop, returning the only IPs it may connect to.

    Production defaults to exact configured feed endpoints. Host rewrites are
    deliberately limited to the known Docker service, the same scheme/port,
    and the exact configured path/query. Operators may explicitly enable
    ``security.allow_unconfigured_feed_urls`` for public Internet feeds; that
    exception accepts only the scheme's standard port and requires every A/AAAA
    result to be globally routable.
    """
    target, error = _parse_http_endpoint(url)
    if target is None:
        return False, error, ()

    configured = _matching_configured_endpoint(target, cfg)
    if configured is not None:
        configured_is_internal = _is_declared_internal_endpoint(configured)
        target_is_internal = _is_declared_internal_endpoint(target)
        approved_internal = (
            _is_approved_configured_internal_endpoint(configured)
            and _is_approved_configured_internal_endpoint(target)
        )
        if configured_is_internal or target_is_internal:
            if not approved_internal:
                return False, "配置 feed 指向未批准的内部地址", ()
        if redirect_from:
            source, source_error = _parse_http_endpoint(redirect_from)
            if source is None:
                return False, source_error, ()
            source_configured = _matching_configured_endpoint(source, cfg)
            source_is_internal = bool(
                source_configured is not None
                and _is_declared_internal_endpoint(source_configured)
            )
            if approved_internal and not source_is_internal:
                return False, "外部目标不允许重定向到内部服务", ()
        try:
            addresses = _resolve_addresses(target)
        except (OSError, ValueError):
            return False, "目标主机解析失败", ()
        # An exact allowlist match is not enough to make a hostname public:
        # the same validated A/AAAA set is passed to the socket layer below so
        # DNS cannot change between policy validation and connect().
        if not approved_internal and any(not address.is_global for address in addresses):
            return False, "目标主机解析到非公网地址", ()
        return True, "", _normalized_addresses(addresses)

    if not _security_flag(cfg, "allow_unconfigured_feed_urls", False):
        return False, "目标 URL 不在已配置 feed allowlist", ()

    if target.port != _DEFAULT_PORTS[target.scheme]:
        return False, "未配置 URL 只允许协议默认端口", ()
    try:
        addresses = _resolve_addresses(target)
    except (OSError, ValueError):
        return False, "目标主机解析失败", ()
    if any(not address.is_global for address in addresses):
        return False, "目标主机解析到非公网地址", ()
    return True, "", _normalized_addresses(addresses)


def validate_target_url(
    url: str,
    cfg: dict,
    *,
    redirect_from: str | None = None,
) -> tuple[bool, str]:
    """Compatibility policy API for routes that only need allow/deny."""
    ok, error, _addresses = resolve_target_url(
        url, cfg, redirect_from=redirect_from
    )
    return ok, error


def require_target_url(
    url: str,
    cfg: dict,
    *,
    redirect_from: str | None = None,
) -> tuple[str, ...]:
    """Return the validated A/AAAA set or raise before any connection."""
    ok, error, addresses = resolve_target_url(
        url, cfg, redirect_from=redirect_from
    )
    if not ok:
        raise PermissionError(error)
    if not addresses:
        raise PermissionError("目标主机解析失败")
    return addresses


_PINNED_ADDRESSES_ATTR = "_ai_filter_pinned_addresses"
_PINNED_URL_ATTR = "_ai_filter_pinned_url"


def pin_request_target(
    req: urllib.request.Request,
    cfg: dict,
    *,
    redirect_from: str | None = None,
) -> urllib.request.Request:
    """Attach the validated resolution to exactly this urllib Request."""
    addresses = require_target_url(
        req.full_url, cfg, redirect_from=redirect_from
    )
    setattr(req, _PINNED_ADDRESSES_ATTR, addresses)
    setattr(req, _PINNED_URL_ATTR, req.full_url)
    return req


def _request_pinned_addresses(
    req: urllib.request.Request, cfg: dict
) -> tuple[str, ...]:
    addresses = getattr(req, _PINNED_ADDRESSES_ATTR, ())
    pinned_url = getattr(req, _PINNED_URL_ATTR, "")
    if pinned_url != req.full_url or not addresses:
        return require_target_url(req.full_url, cfg)
    return tuple(str(address) for address in addresses)


def _connect_pinned_addresses(
    addresses: tuple[str, ...],
    port: int,
    timeout,
    source_address=None,
):
    last_error: OSError | None = None
    for address in addresses:
        parsed = ipaddress.ip_address(address)
        family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
        sock = None
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            destination = (
                (address, port, 0, 0) if parsed.version == 6 else (address, port)
            )
            # Connect the numeric sockaddr directly. No resolver is involved
            # after policy validation, including for an IP-looking hostname.
            sock.connect(destination)
            return sock
        except OSError as exc:
            if sock is not None:
                sock.close()
            last_error = exc
    if last_error is not None:
        raise last_error
    raise OSError("no validated target addresses")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, *, pinned_addresses, **kwargs):
        self._pinned_addresses = tuple(pinned_addresses)
        super().__init__(host, **kwargs)

    def connect(self):
        if self._tunnel_host:
            raise OSError("HTTP proxy tunneling is disabled for feed fetches")
        self.sock = _connect_pinned_addresses(
            self._pinned_addresses,
            self.port,
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, pinned_addresses, context=None, **kwargs):
        self._pinned_addresses = tuple(pinned_addresses)
        # A default client context enforces CA and hostname validation. The
        # original hostname remains self.host and is passed as TLS SNI below.
        context = context or ssl.create_default_context()
        super().__init__(host, context=context, **kwargs)

    def connect(self):
        if self._tunnel_host:
            raise OSError("HTTPS proxy tunneling is disabled for feed fetches")
        raw_socket = _connect_pinned_addresses(
            self._pinned_addresses,
            self.port,
            self.timeout,
            self.source_address,
        )
        try:
            self.sock = self._context.wrap_socket(
                raw_socket,
                server_hostname=self.host,
            )
        except Exception:
            raw_socket.close()
            raise


class PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, cfg: dict):
        super().__init__()
        self._cfg = cfg or {}

    def http_open(self, req):  # type: ignore[override]
        addresses = _request_pinned_addresses(req, self._cfg)

        def connection(host, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, **kwargs):
            return _PinnedHTTPConnection(
                host,
                timeout=timeout,
                pinned_addresses=addresses,
                **kwargs,
            )

        return self.do_open(connection, req)


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, cfg: dict):
        super().__init__()
        self._cfg = cfg or {}

    def https_open(self, req):  # type: ignore[override]
        addresses = _request_pinned_addresses(req, self._cfg)

        def connection(host, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, **kwargs):
            return _PinnedHTTPSConnection(
                host,
                timeout=timeout,
                pinned_addresses=addresses,
                **kwargs,
            )

        return self.do_open(connection, req)


class ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """urllib redirect handler that applies ``validate_target_url`` per hop."""

    def __init__(self, cfg: dict):
        super().__init__()
        self._cfg = cfg or {}

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        absolute_url = urllib.parse.urljoin(req.full_url, newurl)
        addresses = require_target_url(
            absolute_url, self._cfg, redirect_from=req.full_url
        )
        redirected = super().redirect_request(
            req, fp, code, msg, headers, absolute_url
        )
        if redirected is not None:
            setattr(redirected, _PINNED_ADDRESSES_ATTR, addresses)
            setattr(redirected, _PINNED_URL_ATTR, redirected.full_url)
        return redirected


def rate_limit(max_requests: int = 10, window_seconds: float = 60.0):
    """Flask 路由装饰器：限制单个 IP 在时间窗内的请求次数"""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            client_ip = _rate_limit_client_ip()
            store_key = (request.endpoint or request.path, client_ip)
            now = time.time()
            with _rate_limit_lock:
                if len(_rate_limit_store) > 4096:
                    cutoff = now - max(window_seconds, 3600.0)
                    for key, (last_seen, _count) in list(_rate_limit_store.items()):
                        if last_seen < cutoff:
                            _rate_limit_store.pop(key, None)
                last_time, count = _rate_limit_store.get(store_key, (0.0, 0))
                if now - last_time > window_seconds:
                    count = 1
                    last_time = now
                else:
                    count += 1
                _rate_limit_store[store_key] = (last_time, count)
                if count > max_requests:
                    response = Response(
                        json.dumps({"error": "Rate limit exceeded"}, ensure_ascii=False),
                        status=429, mimetype="application/json"
                    )
                    response.headers["Retry-After"] = str(max(1, int(window_seconds)))
                    return response
            return f(*args, **kwargs)
        return wrapper
    return decorator


def check_refresh_rate_limit() -> tuple[bool, str]:
    """检查 /refresh 是\u5426\u89e6\u53d1\u5168\u5c40\u9650\u6d41。返回 (ok, error_msg)"""
    client_ip = _rate_limit_client_ip()
    now = time.time()
    with _refresh_rate_limit_lock:
        if len(_refresh_rate_limit_store) > 4096:
            for key, last_seen in list(_refresh_rate_limit_store.items()):
                if now - last_seen >= 60.0:
                    _refresh_rate_limit_store.pop(key, None)
        last = _refresh_rate_limit_store.get(client_ip, 0.0)
        if now - last < 60.0:
            return False, f"\u8bf7\u6c42\u8fc7\u4e8e\u9891\u7e41\uff0c\u8bf7 {int(60 - (now - last))} \u79d2\u540e\u91cd\u8bd5"
        _refresh_rate_limit_store[client_ip] = now
    return True, ""


def refresh_rate_limit(f):
    """Flask 装饰器：限制 /refresh 端点 1 次/分钟（装饰器版本）。"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        ok, msg = check_refresh_rate_limit()
        if not ok:
            return Response(
                json.dumps({"error": msg}, ensure_ascii=False),
                status=429, mimetype="application/json"
            )
        return f(*args, **kwargs)
    return wrapper


def verify_miniflux_signature() -> bool:
    """验证 Miniflux Webhook HMAC-SHA256 签名。"""
    secret = os.environ.get("MINIFLUX_WEBHOOK_SECRET", "").strip().encode("utf-8")
    if not secret:
        return False
    signature_header = request.headers.get("X-Miniflux-Signature")
    if not signature_header:
        return False
    provided = signature_header.strip()
    if provided.lower().startswith("sha256="):
        provided = provided.split("=", 1)[1].strip()
    # compare_digest on str rejects non-ASCII input with TypeError. Invalid
    # signature headers must fail authentication before reading the body.
    if len(provided) != 64 or any(ch not in "0123456789abcdef" for ch in provided):
        return False
    payload = request.get_data()
    computed_hmac = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed_hmac, provided)
