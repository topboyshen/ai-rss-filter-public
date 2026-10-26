import importlib
import io
import socket
import sys
from urllib.request import Request

import pytest


def load_app(monkeypatch, tmp_path):
    db_path = str(tmp_path / "cache.db")
    monkeypatch.setenv("AI_FILTER_DB_PATH", db_path)
    import db.database as database
    database.DB_PATH = db_path
    sys.modules.pop("app", None)
    return importlib.import_module("app")


def test_webhook_signature_rejects_missing_secret(monkeypatch, tmp_path):
    app_module = load_app(monkeypatch, tmp_path)
    monkeypatch.delenv("MINIFLUX_WEBHOOK_SECRET", raising=False)

    with app_module.app.test_request_context("/webhook", method="POST", data=b"{}", headers={"X-Miniflux-Signature": "bad"}):
        from routes.security import verify_miniflux_signature

        assert verify_miniflux_signature() is False


def test_api_key_header_preferred_and_query_can_be_disabled(monkeypatch, tmp_path):
    app_module = load_app(monkeypatch, tmp_path)
    app_module.config._cfg = {
        "security": {
            "api_key": "secret",
            "allow_query_api_key": False,
        }
    }

    with app_module.app.test_request_context("/stats?key=secret"):
        assert app_module.check_api_key() is False

    with app_module.app.test_request_context("/stats", headers={"X-API-Key": "secret"}):
        assert app_module.check_api_key() is True


def test_cors_origin_can_be_restricted(monkeypatch, tmp_path):
    app_module = load_app(monkeypatch, tmp_path)
    app_module.config._cfg = {
        "security": {
            "cors_allowed_origins": ["https://allowed.example"],
        }
    }

    with app_module.app.test_request_context("/", headers={"Origin": "https://allowed.example"}):
        assert app_module._allowed_cors_origin() == "https://allowed.example"

    with app_module.app.test_request_context("/", headers={"Origin": "https://blocked.example"}):
        assert app_module._allowed_cors_origin() is None


def test_validate_target_url_allows_configured_feed_path_across_known_host_rewrites():
    from routes.security import validate_target_url

    cfg = {"feeds": [{"url": "http://ai-filter-web:5000/jin10/rss"}]}
    assert validate_target_url("http://127.0.0.1:5000/jin10/rss", cfg) == (True, "")


def test_validate_target_url_preserves_only_matching_docker_service_rewrites():
    from routes.security import validate_target_url

    cfg = {"feeds": [{"url": "http://rsshub:1200/cls/depth"}]}
    assert validate_target_url("http://localhost:1200/cls/depth", cfg) == (True, "")
    assert validate_target_url("http://ai-filter-web:1200/cls/depth", cfg)[0] is False
    assert validate_target_url("http://localhost:5000/cls/depth", cfg)[0] is False


def test_validate_target_url_requires_exact_configured_port_path_and_query(monkeypatch):
    import routes.security as security

    monkeypatch.setattr(
        security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _addrinfo("93.184.216.34"),
    )

    cfg = {"feeds": [{"url": "https://feeds.example:8443/rss?q=allowed"}]}
    assert security.validate_target_url("https://feeds.example:8443/rss?q=allowed", cfg) == (True, "")
    assert security.validate_target_url("https://feeds.example/rss?q=allowed", cfg)[0] is False
    assert security.validate_target_url("https://feeds.example:8443/other?q=allowed", cfg)[0] is False
    assert security.validate_target_url("https://feeds.example:8443/rss?q=changed", cfg)[0] is False


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.8/feed",
        "http://[fd00::8]/feed",
        "http://unapproved-internal:8080/feed",
    ],
)
def test_configured_feed_does_not_authorize_arbitrary_internal_targets(url):
    from routes.security import validate_target_url

    assert validate_target_url(url, {"feeds": [{"url": url}]})[0] is False


def test_validate_target_url_denies_unconfigured_url_by_default_without_dns(monkeypatch):
    import routes.security as security

    monkeypatch.setattr(
        security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: pytest.fail("default-denied URL must not trigger DNS"),
    )
    assert security.validate_target_url("https://public.example/rss", {"feeds": []})[0] is False


def _addrinfo(*addresses):
    rows = []
    for address in addresses:
        if ":" in address:
            rows.append((socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443, 0, 0)))
        else:
            rows.append((socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443)))
    return rows

@pytest.mark.parametrize(
    "addresses",
    [
        ("10.23.45.67",),
        ("fd00::1234",),
        ("93.184.216.34", "10.23.45.67"),
        ("2606:4700:4700::1111", "fd00::1234"),
    ],
    ids=["private-ipv4", "ipv6-ula", "mixed-ipv4", "mixed-ipv6"],
)
def test_configured_external_feed_rejects_any_non_global_dns_answer(monkeypatch, addresses):
    import routes.security as security

    monkeypatch.setattr(
        security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _addrinfo(*addresses),
    )
    cfg = {"feeds": [{"url": "https://private.example/rss"}]}

    ok, error = security.validate_target_url("https://private.example/rss", cfg)

    assert ok is False
    assert "非公网" in error


def test_fetch_feed_revalidates_configured_host_on_each_retry(monkeypatch):
    import routes.security as security
    import utils.helpers as helpers

    dns_answers = iter([
        _addrinfo("93.184.216.34"),
        _addrinfo("10.23.45.67"),
    ])
    monkeypatch.setattr(
        security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: next(dns_answers),
    )

    class FailingOpener:
        open_calls = 0

        def open(self, *_args, **_kwargs):
            self.open_calls += 1
            raise OSError("transient connection failure")

    opener = FailingOpener()
    monkeypatch.setattr(helpers, "build_opener", lambda *_handlers: opener)
    cfg = {"feeds": [{"url": "https://public.example/rss"}]}

    with pytest.raises(PermissionError, match="非公网"):
        helpers.fetch_feed(
            "https://public.example/rss",
            timeout=1,
            max_attempts=2,
            backoff_base=0,
            cfg=cfg,
        )

    assert opener.open_calls == 1


class _FakeHTTPStreamSocket:
    def __init__(self, response: bytes):
        self._response = io.BytesIO(response)
        self.sent = bytearray()
        self.connected_to = None

    def sendall(self, data):
        self.sent.extend(data)

    def makefile(self, *_args, **_kwargs):
        return self._response

    def settimeout(self, _timeout):
        return None

    def connect(self, address):
        self.connected_to = address

    def bind(self, _address):
        return None

    def close(self):
        return None


def _ok_response(body=b"<rss></rss>"):
    return (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/rss+xml\r\n"
        + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        + body
    )


def test_fetch_feed_pins_validation_result_against_dns_rebinding(monkeypatch):
    import routes.security as security
    import utils.helpers as helpers

    dns_answers = iter([
        _addrinfo("93.184.216.34"),
        _addrinfo("169.254.169.254"),
    ])
    dns_calls = []

    def malicious_resolver(*_args, **_kwargs):
        dns_calls.append(True)
        return next(dns_answers)

    sockets = []
    def fake_socket(*_args, **_kwargs):
        sock = _FakeHTTPStreamSocket(_ok_response())
        sockets.append(sock)
        return sock

    monkeypatch.setattr(security.socket, "getaddrinfo", malicious_resolver)
    monkeypatch.setattr(security.socket, "socket", fake_socket)
    # Even a hostile inherited proxy must not regain control over resolution.
    monkeypatch.setenv("HTTP_PROXY", "http://169.254.169.254:8080")
    cfg = {"feeds": [{"url": "http://feeds.example/rss"}]}

    assert helpers.fetch_feed(
        "http://feeds.example/rss", timeout=1, max_attempts=1, cfg=cfg
    ) == b"<rss></rss>"

    assert len(dns_calls) == 1
    assert sockets[0].connected_to == ("93.184.216.34", 80)
    assert b"Host: feeds.example\r\n" in bytes(sockets[0].sent)


def test_https_pin_keeps_original_hostname_for_host_sni_and_verification(monkeypatch):
    import routes.security as security
    import utils.helpers as helpers

    monkeypatch.setattr(
        security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _addrinfo("93.184.216.34"),
    )
    raw_socket = _FakeHTTPStreamSocket(_ok_response())
    monkeypatch.setattr(
        security.socket,
        "socket",
        lambda *_args, **_kwargs: raw_socket,
    )

    class VerifyingContext:
        check_hostname = True
        verify_mode = security.ssl.CERT_REQUIRED

        def __init__(self):
            self.server_hostname = ""

        def wrap_socket(self, sock, *, server_hostname):
            assert self.check_hostname is True
            assert server_hostname == "secure.example"
            self.server_hostname = server_hostname
            return sock

    context = VerifyingContext()
    monkeypatch.setattr(security.ssl, "create_default_context", lambda: context)
    cfg = {"feeds": [{"url": "https://secure.example/rss"}]}

    assert helpers.fetch_feed(
        "https://secure.example/rss", timeout=1, max_attempts=1, cfg=cfg
    ) == b"<rss></rss>"

    assert raw_socket.connected_to == ("93.184.216.34", 443)
    assert context.server_hostname == "secure.example"
    assert b"Host: secure.example\r\n" in bytes(raw_socket.sent)


def test_redirect_resolves_and_pins_each_hop(monkeypatch):
    import routes.security as security
    import utils.helpers as helpers

    answers = iter([
        _addrinfo("93.184.216.34"),
        _addrinfo("93.184.216.35"),
    ])
    dns_calls = []

    def resolver(*_args, **_kwargs):
        dns_calls.append(True)
        return next(answers)

    redirect_response = (
        b"HTTP/1.1 302 Found\r\n"
        b"Location: http://redirect.example/final\r\n"
        b"Content-Length: 0\r\nConnection: close\r\n\r\n"
    )
    sockets = [
        _FakeHTTPStreamSocket(redirect_response),
        _FakeHTTPStreamSocket(_ok_response(b"redirected")),
    ]
    created = []

    def fake_socket(*_args, **_kwargs):
        sock = sockets[len(created)]
        created.append(sock)
        return sock

    monkeypatch.setattr(security.socket, "getaddrinfo", resolver)
    monkeypatch.setattr(security.socket, "socket", fake_socket)
    cfg = {
        "feeds": [
            {"url": "http://feeds.example/rss"},
            {"url": "http://redirect.example/final"},
        ]
    }

    assert helpers.fetch_feed(
        "http://feeds.example/rss", timeout=1, max_attempts=1, cfg=cfg
    ) == b"redirected"

    assert len(dns_calls) == 2
    assert [sock.connected_to for sock in sockets] == [
        ("93.184.216.34", 80), ("93.184.216.35", 80)
    ]
    assert b"Host: feeds.example\r\n" in bytes(sockets[0].sent)
    assert b"Host: redirect.example\r\n" in bytes(sockets[1].sent)


@pytest.mark.parametrize(
    ("extra_bytes", "should_succeed"),
    [(0, True), (1, False)],
    ids=["exactly-at-limit", "one-byte-over-limit"],
)
def test_fetch_feed_enforces_bounded_response_size(
    monkeypatch, extra_bytes, should_succeed
):
    import routes.security as security
    import utils.helpers as helpers

    limit = 64 * 1024
    body = b"x" * (limit + extra_bytes)
    sock = _FakeHTTPStreamSocket(_ok_response(body))
    monkeypatch.setenv("AI_FILTER_MAX_FEED_BYTES", str(limit))
    monkeypatch.setattr(
        security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _addrinfo("93.184.216.34"),
    )
    monkeypatch.setattr(
        security.socket,
        "socket",
        lambda *_args, **_kwargs: sock,
    )
    cfg = {"feeds": [{"url": "http://feeds.example/rss"}]}

    if should_succeed:
        assert helpers.fetch_feed(
            "http://feeds.example/rss", timeout=1, max_attempts=1, cfg=cfg
        ) == body
    else:
        with pytest.raises(ValueError, match="feed response exceeds 65536 byte limit"):
            helpers.fetch_feed(
                "http://feeds.example/rss", timeout=1, max_attempts=1, cfg=cfg
            )


def test_explicit_unconfigured_url_requires_every_a_and_aaaa_result_global(monkeypatch):
    import routes.security as security

    cfg = {"security": {"allow_unconfigured_feed_urls": True}, "feeds": []}
    monkeypatch.setattr(
        security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _addrinfo("93.184.216.34", "2606:4700:4700::1111"),
    )
    assert security.validate_target_url("https://public.example/rss", cfg) == (True, "")

    monkeypatch.setattr(
        security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _addrinfo("93.184.216.34", "10.0.0.8"),
    )
    assert security.validate_target_url("https://public.example/rss", cfg)[0] is False


@pytest.mark.parametrize("address", ["127.0.0.2", "169.254.10.20", "::1", "fd00::1", "fe80::1"])
def test_explicit_unconfigured_url_rejects_non_global_ipv4_and_ipv6(monkeypatch, address):
    import routes.security as security

    cfg = {"security": {"allow_unconfigured_feed_urls": True}, "feeds": []}
    monkeypatch.setattr(
        security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _addrinfo(address),
    )
    assert security.validate_target_url("https://public.example/rss", cfg)[0] is False


def test_explicit_unconfigured_url_restricts_ports_before_dns(monkeypatch):
    import routes.security as security

    cfg = {"security": {"allow_unconfigured_feed_urls": True}, "feeds": []}
    monkeypatch.setattr(
        security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: pytest.fail("non-standard port must fail before DNS"),
    )
    assert security.validate_target_url("https://public.example:8443/rss", cfg)[0] is False


def test_redirect_handler_rejects_external_to_configured_internal_service():
    from routes.security import ValidatingRedirectHandler

    cfg = {
        "feeds": [
            {"url": "https://feeds.example/rss"},
            {"url": "http://rsshub:1200/internal"},
        ]
    }
    handler = ValidatingRedirectHandler(cfg)
    request = Request("https://feeds.example/rss")
    with pytest.raises(PermissionError, match="内部服务"):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "http://rsshub:1200/internal",
        )


def test_validate_target_url_does_not_allow_arbitrary_host_by_path_only():
    from routes.security import validate_target_url

    cfg = {"feeds": [{"url": "http://ai-filter-web:5000/jin10/rss"}]}
    assert validate_target_url("http://169.254.169.254/jin10/rss", cfg)[0] is False


@pytest.mark.parametrize("prefix", ["", "sha256="])
def test_webhook_signature_accepts_sha256_prefix(monkeypatch, tmp_path, prefix):
    import hashlib
    import hmac

    app_module = load_app(monkeypatch, tmp_path)
    monkeypatch.setenv("MINIFLUX_WEBHOOK_SECRET", "secret")
    body = b'{"entries": []}'
    digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()

    with app_module.app.test_request_context(
        "/webhook",
        method="POST",
        data=body,
        headers={"X-Miniflux-Signature": f"{prefix}{digest}"},
    ):
        from routes.security import verify_miniflux_signature

        assert verify_miniflux_signature() is True


@pytest.mark.parametrize(
    "signature",
    ["é", "sha256=é", "é" * 64, "g" * 64, "0" * 63, "0" * 65, "sha256=", "0" * 64],
)
def test_malformed_webhook_signature_returns_unauthorized(monkeypatch, tmp_path, signature):
    app_module = load_app(monkeypatch, tmp_path)
    monkeypatch.setenv("MINIFLUX_WEBHOOK_SECRET", "secret")

    with app_module.app.test_client() as client:
        response = client.post(
            "/webhook", data=b"{}", headers={"X-Miniflux-Signature": signature}
        )

    assert response.status_code == 401


def test_api_key_fails_closed_when_missing(monkeypatch, tmp_path):
    app_module = load_app(monkeypatch, tmp_path)
    app_module.config._cfg = {"security": {}}

    with app_module.app.test_request_context("/stats"):
        assert app_module.check_api_key() is False


def test_webhook_rejects_oversized_body(monkeypatch, tmp_path):
    import hashlib
    import hmac

    app_module = load_app(monkeypatch, tmp_path)
    monkeypatch.setenv("MINIFLUX_WEBHOOK_SECRET", "secret")
    app_module.app.config["MAX_CONTENT_LENGTH"] = 64
    body = b"x" * 65
    digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()

    with app_module.app.test_client() as client:
        response = client.post(
            "/webhook",
            data=body,
            headers={"X-Miniflux-Signature": f"sha256={digest}"},
        )

    assert response.status_code == 413
