import errno
import io
import json
import socket
import xml.etree.ElementTree as ET
from urllib.error import HTTPError, URLError

import feedparser
import pytest

from services import publisher_feeds as feeds


def cfg(source="csrc", limit=2):
    route = "/gov/csrc/news" if source == "csrc" else "/caixin/latest"
    return {"feeds": [{"url": "http://127.0.0.1:1200" + route, "max_entries": limit}]}


def article(title="原始公告", date="2026-09-28 15:35:13"):
    return (f'<meta name="ArticleTitle" content="{title}"><meta name="PubDate" content="{date}">'
            '<div class="detail-news"><p>原始正文。</p><div>第二段</div></div><footer>不应入正文</footer>').encode()


def index():
    return b'''<ul><a href="/csrc/c100030/c99/content.shtml">Other section</a></ul>
    <ul><li><a href="/csrc/c100028/c1/content.shtml">First</a></li>
    <li><a href="/csrc/c106311/c2/content.shtml">Second</a></li>
    <li><a href="/csrc/c100028/c3/content.shtml">Third</a></li>
    <li><a href="/csrc/c100028/common_xq_list.shtml">''' + "加载更多".encode() + b"</a></li></ul>"


def caixin_row(**updates):
    row = {"title": "原始新闻", "summary": "原始公开摘要", "time": 1790604480000,
           "url": "https://finance.caixin.com/2026-09-28/102489393.html"}
    row.update(updates)
    return row


def caixin_payload(rows):
    return json.dumps({"code": 0, "data": {"articleList": rows}}, ensure_ascii=False).encode()


def test_csrc_uses_current_section_and_only_configured_articles(monkeypatch):
    calls = []

    def fetch(url, timeout, attempts):
        calls.append(url)
        return index() if url == feeds.CSRC_INDEX else article()

    monkeypatch.setattr(feeds, "_fetch", fetch)
    xml = feeds.load_publisher_feed("http://rsshub:1200/gov/csrc/news", cfg(), 20, 3)
    parsed = feedparser.parse(xml)
    assert len(parsed.entries) == 2
    assert set(calls) == {feeds.CSRC_INDEX, "https://www.csrc.gov.cn/csrc/c100028/c1/content.shtml",
                          "https://www.csrc.gov.cn/csrc/c106311/c2/content.shtml"}
    assert parsed.entries[0].title == "原始公告"
    assert "不应入正文" not in parsed.entries[0].summary
    assert parsed.entries[0].published == "Mon, 28 Sep 2026 15:35:13 +0800"


@pytest.mark.parametrize("body", [b"<html>homepage</html>", b'<ul id="list"><li>2021 archive</li></ul>'])
def test_csrc_missing_current_section_is_failure_not_empty_success(monkeypatch, body):
    monkeypatch.setattr(feeds, "_fetch", lambda *args: body)
    with pytest.raises(ValueError, match="current news section missing"):
        feeds.load_publisher_feed("http://rsshub:1200/gov/csrc/news", cfg(), 20, 3)


def test_csrc_article_failure_never_returns_partial_or_cached_success(monkeypatch):
    def fetch(url, *args):
        if url == feeds.CSRC_INDEX:
            return index()
        if "/c2/" in url:
            raise TimeoutError("source unavailable")
        return article()

    monkeypatch.setattr(feeds, "_fetch", fetch)
    with pytest.raises(TimeoutError):
        feeds.load_publisher_feed("http://rsshub:1200/gov/csrc/news", cfg(), 20, 3)


@pytest.mark.parametrize("payload", [b"<html>unexpected</html>", article(date="bad-date"), article(title="")])
def test_csrc_does_not_invent_missing_article_metadata(monkeypatch, payload):
    monkeypatch.setattr(feeds, "_fetch", lambda *args: payload)
    with pytest.raises(ValueError):
        feeds._csrc_article("https://www.csrc.gov.cn/csrc/c100028/c1/content.shtml", 20, 3)


def _addrinfo(*addresses):
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM,
             socket.IPPROTO_TCP, "", (ip, 443, 0, 0) if ":" in ip else (ip, 443)) for ip in addresses]


class _PublisherSocket:
    def __init__(self, response):
        self.response = io.BytesIO(response)
        self.destination = None
        self.timeout = None
        self.sent = b""

    def connect(self, destination):
        self.destination = destination

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, request):
        self.sent += request

    def makefile(self, *_args, **_kwargs):
        return self.response

    def close(self):
        pass


def _http_response(status, reason, body=b"", headers=b""):
    return (f"HTTP/1.1 {status} {reason}\r\nContent-Length: {len(body)}\r\n".encode()
            + headers + b"Connection: close\r\n\r\n" + body)


def _install_publisher_transport(monkeypatch, raw_responses, *, tls_error=None):
    from routes import security

    sockets = []
    responses = iter(raw_responses)

    def socket_factory(family, kind):
        assert family == socket.AF_INET and kind == socket.SOCK_STREAM
        sock = _PublisherSocket(next(responses))
        sockets.append(sock)
        return sock

    class Context:
        check_hostname = True
        verify_mode = security.ssl.CERT_REQUIRED

        def wrap_socket(self, sock, *, server_hostname):
            assert server_hostname == "www.csrc.gov.cn"
            if tls_error:
                raise tls_error
            return sock

    monkeypatch.setattr(security.socket, "getaddrinfo", lambda *_args, **_kwargs: _addrinfo("93.184.216.34"))
    monkeypatch.setattr(security.socket, "socket", socket_factory)
    monkeypatch.setattr(security.ssl, "create_default_context", Context)
    return sockets


def test_csrc_diagnostic_reports_last_failed_pinned_address_family(monkeypatch):
    from routes import security

    attempted = []

    def refuse(family, _kind):
        attempted.append(family)
        errnum = errno.ECONNREFUSED if family == socket.AF_INET else errno.ENETUNREACH
        raise OSError(errnum, "private address and credentials")

    monkeypatch.setattr(security.socket, "getaddrinfo", lambda *_args, **_kwargs: _addrinfo(
        "2606:4700:4700::1111", "93.184.216.34"))
    monkeypatch.setattr(security.socket, "socket", refuse)

    with pytest.raises(URLError) as failure:
        feeds.load_publisher_feed(cfg()["feeds"][0]["url"], cfg(), 20, 1)
    assert attempted == [socket.AF_INET, socket.AF_INET6]
    assert str(failure.value.reason) == (
        f"CSRC fetch stage=index phase=connect family=IPv6 errno={errno.ENETUNREACH} http_status=unknown"
    )
    assert failure.value.__suppress_context__ is True


@pytest.mark.parametrize("resolver_error", [True, False], ids=["dns-failure", "mixed-public-private"])
def test_csrc_diagnostic_distinguishes_dns_failure_from_rejected_address(monkeypatch, resolver_error):
    from routes import security

    def resolve(*_args, **_kwargs):
        if resolver_error:
            raise socket.gaierror(socket.EAI_AGAIN, "private DNS detail")
        return _addrinfo("93.184.216.34", "169.254.169.254")

    monkeypatch.setattr(security.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(security.socket, "socket", lambda *_args: pytest.fail("must stop before socket"))
    phase, errnum = ("dns", socket.EAI_AGAIN) if resolver_error else ("policy", "unknown")
    with pytest.raises(PermissionError) as failure:
        feeds.load_publisher_feed(cfg()["feeds"][0]["url"], cfg(), 20, 3)
    assert str(failure.value) == f"CSRC fetch stage=index phase={phase} family=unknown errno={errnum} http_status=unknown"


def test_csrc_diagnostic_redacts_detail_http_error_and_retains_pinned_request(monkeypatch):
    sockets = _install_publisher_transport(monkeypatch, [
        _http_response(200, "OK", index()),
        _http_response(403, "private reason https://secret.invalid", b"private response"),
    ])

    with pytest.raises(HTTPError) as failure:
        feeds.load_publisher_feed(cfg()["feeds"][0]["url"], cfg(limit=1), 17, 3)
    error = failure.value
    assert error.code == 403
    assert str(error) == "CSRC fetch stage=detail phase=http family=IPv4 errno=unknown http_status=403"
    assert error.filename == "" and error.read() == b"" and error.hdrs is None
    assert [sock.destination for sock in sockets] == [("93.184.216.34", 443)] * 2
    assert [sock.timeout for sock in sockets] == [17, 17]
    assert b"GET /csrc/xwfb/index.shtml " in sockets[0].sent
    assert b"GET /csrc/c100028/c1/content.shtml " in sockets[1].sent
    assert all(b"Host: www.csrc.gov.cn\r\n" in sock.sent for sock in sockets)


def test_csrc_diagnostic_keeps_successful_response_and_retry_contract(monkeypatch, caplog):
    sockets = _install_publisher_transport(monkeypatch, [
        _http_response(503, "temporary failure"),
        _http_response(200, "OK", index()),
        _http_response(200, "OK", article()),
    ])
    pauses = []
    monkeypatch.setattr("utils.helpers.time.sleep", pauses.append)
    monkeypatch.setattr("utils.helpers.random.uniform", lambda *_args: 0.75)

    rss = feeds.load_publisher_feed(cfg()["feeds"][0]["url"], cfg(limit=1), 17, 2)

    assert len(feedparser.parse(rss).entries) == 1
    assert len(sockets) == 3 and pauses == [1.5]
    assert "CSRC fetch" not in caplog.text


def test_csrc_diagnostic_does_not_follow_unconfigured_redirect(monkeypatch):
    sockets = _install_publisher_transport(monkeypatch, [
        _http_response(302, "Found", headers=b"Location: https://169.254.169.254/private?token=secret\r\n"),
    ])

    with pytest.raises(PermissionError) as failure:
        feeds.load_publisher_feed(cfg()["feeds"][0]["url"], cfg(), 17, 3)
    assert len(sockets) == 1
    assert str(failure.value) == "CSRC fetch stage=index phase=policy family=unknown errno=unknown http_status=302"


def test_csrc_diagnostic_identifies_tls_before_http(monkeypatch):
    from routes import security

    sockets = _install_publisher_transport(monkeypatch, [b""], tls_error=security.ssl.SSLError(1, "private cert"))
    with pytest.raises(URLError) as failure:
        feeds.load_publisher_feed(cfg()["feeds"][0]["url"], cfg(), 17, 1)
    assert sockets[0].sent == b""
    assert str(failure.value.reason) == "CSRC fetch stage=index phase=tls family=IPv4 errno=1 http_status=unknown"


def test_csrc_diagnostic_retains_http_status_when_the_response_body_fails(monkeypatch):
    _install_publisher_transport(monkeypatch, [
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
        b"FF\r\nprivate truncated body",
    ])
    with pytest.raises(OSError, match="^CSRC fetch stage=index phase=body family=IPv4 errno=unknown http_status=200$"):
        feeds.load_publisher_feed(cfg()["feeds"][0]["url"], cfg(), 17, 1)


@pytest.mark.parametrize("fail_phase", ["http", "request"])
def test_csrc_diagnostic_survives_processor_truncation_on_the_failure_path(monkeypatch, caplog, fail_phase):
    from core import processor

    if fail_phase == "http":
        _install_publisher_transport(monkeypatch, [
            _http_response(200, "OK", index()),
            _http_response(403, "private reason https://secret.invalid", b"private response"),
        ])
        family, status = "IPv4", 403
    else:
        def fail_request(url, *_args):
            if url == feeds.CSRC_INDEX:
                return index()
            raise URLError(OSError("private reason https://secret.invalid"))

        monkeypatch.setattr(feeds, "_fetch", fail_request)
        family, status = "unknown", "unknown"
    feed_cfg = cfg(limit=1) | {"rss_timeout": 17, "rss_max_fetch_attempts": 1}
    route = feed_cfg["feeds"][0]["url"]
    writes = []
    monkeypatch.setattr(processor, "resolve_scoring_contract", lambda cfg, **_kwargs: cfg)
    monkeypatch.setattr(processor.DB, "update_feed_health", lambda *args: writes.append(args))

    entries, _, error, _ = processor.process_feed(
        route, "synthetic prompt", "证监会新闻", feed_cfg, check_output_cache=False,
    )

    assert entries == []
    assert error == (f"RSS解析失败: CSRC fetch stage=detail phase={fail_phase} "
                     f"family={family} errno=unknown http_status={status}")
    assert writes == [(route, "证监会新闻", 0, False, 0, 0, None, error)]
    assert error in caplog.text and "private" not in caplog.text and "secret.invalid" not in caplog.text


@pytest.mark.parametrize("payload, expected", [
    (b'<div class><p>private response</p></div>', "CSRC detail response could not be parsed"),
    (article(date="private date"), "CSRC article publication date invalid"),
])
def test_csrc_diagnostic_does_not_echo_raw_body_from_parser(monkeypatch, payload, expected):
    monkeypatch.setattr(feeds, "_fetch", lambda *_args: payload)
    with pytest.raises(ValueError) as failure:
        feeds._csrc_article("https://www.csrc.gov.cn/csrc/c100028/c1/content.shtml", 20, 1)
    assert str(failure.value) == expected


def test_caixin_fetches_one_public_list_without_article_fanout(monkeypatch):
    calls = []
    rows = [caixin_row(url="https://video.caixin.com/2026-09-28/123.html"), caixin_row()]

    def fetch(url, *args):
        calls.append(url)
        return caixin_payload(rows)

    monkeypatch.setattr(feeds, "_fetch", fetch)
    xml = feeds.load_publisher_feed("http://rsshub:1200/caixin/latest", cfg("caixin", 1), 20, 3)
    parsed = feedparser.parse(xml)
    assert calls == [feeds.CAIXIN_INDEX]
    assert len(parsed.entries) == 1
    assert parsed.entries[0].summary == "原始公开摘要"
    assert "非全文" in ET.fromstring(xml).findtext("channel/description")
    assert parsed.entries[0].published_parsed[:3] == (2026, 9, 28)


@pytest.mark.parametrize("payload", [b"<html>error</html>", b'{"code":1}', caixin_payload([]),
    caixin_payload([caixin_row(time=None)]), caixin_payload([caixin_row(time=True)]),
    caixin_payload([caixin_row(summary=None)]), caixin_payload([caixin_row(title="")]),
    caixin_payload([caixin_row(url="https://caixin.com.evil.test/2026-09-28/123.html")])])
def test_caixin_schema_failure_cannot_clear_source_alert(monkeypatch, payload):
    monkeypatch.setattr(feeds, "_fetch", lambda *args: payload)
    with pytest.raises(ValueError):
        feeds.load_publisher_feed("http://rsshub:1200/caixin/latest", cfg("caixin"), 20, 3)


def test_caixin_keeps_articles_and_records_named_non_article_links(monkeypatch, caplog):
    topic = "https://key.caixin.com/topic/BQ02.000008077"
    column = "https://finance.caixin.com/column/macro.news"
    media = "https://video.caixin.com/2026-09-28/123.html"
    article_url = "https://finance.caixin.com/2026-09-28/102489393.html"
    rows = [
        caixin_row(url=topic, title="专题"),
        caixin_row(url=column, title="栏目"),
        caixin_row(url=media, title="视频"),
        caixin_row(url=article_url, title="有效文章"),
    ]
    monkeypatch.setattr(feeds, "_fetch", lambda *args: caixin_payload(rows))
    with caplog.at_level("WARNING"):
        xml = feeds.load_publisher_feed("http://rsshub:1200/caixin/latest", cfg("caixin", 5), 20, 3)
    parsed = feedparser.parse(xml)
    assert [entry.link for entry in parsed.entries] == [article_url]
    assert "已识别非文章链接 3 条未采集" in ET.fromstring(xml).findtext("channel/description")
    assert "topic=1" in caplog.text and "column=1" in caplog.text and "media=1" in caplog.text


def test_caixin_all_named_non_articles_are_not_an_empty_success(monkeypatch):
    rows = [
        caixin_row(url="https://key.caixin.com/topic/BQ02.000008077"),
        caixin_row(url="https://www.caixin.com/special/budget-2026"),
    ]
    monkeypatch.setattr(feeds, "_fetch", lambda *args: caixin_payload(rows))
    with pytest.raises(ValueError, match=r"no eligible articles \(2 recognized non-article links skipped\)"):
        feeds.load_publisher_feed("http://rsshub:1200/caixin/latest", cfg("caixin"), 20, 3)


@pytest.mark.parametrize("url", [
    "https://caixin.com.evil.test/2026-09-28/123.html",
    "https://user:secret@finance.caixin.com/2026-09-28/123.html",
    "https://finance.caixin.com/2026-09-28/123.html?fulltext=1",
    "https://finance.caixin.com/2026-09-28/123.html#part",
    "https://finance.caixin.com/news/index",
    "https://key.caixin.com/topic/BQ02.000008077?next=1",
    "https://video.caixin.com/playlist",
    "not a url",
])
def test_caixin_abnormal_link_fails_the_source_even_after_a_valid_article(monkeypatch, url):
    rows = [caixin_row(), caixin_row(url=url, title="异常")]
    monkeypatch.setattr(feeds, "_fetch", lambda *args: caixin_payload(rows))
    with pytest.raises(ValueError, match="URL identity invalid|schema invalid"):
        feeds.load_publisher_feed("http://rsshub:1200/caixin/latest", cfg("caixin"), 20, 3)


def test_adapter_requires_configured_enabled_route(monkeypatch):
    monkeypatch.setattr(feeds, "_fetch", lambda *args: pytest.fail("network must not be reached"))
    for config in ({}, {"feeds": []}, {"feeds": [{"url": "http://rsshub:1200/caixin/latest", "enabled": False}]}):
        with pytest.raises(PermissionError):
            feeds.load_publisher_feed("http://rsshub:1200/caixin/latest", config, 20, 3)
    for url in ("http://evil.test:1200/caixin/latest", "http://rsshub:1200/caixin/latest?fulltext=1",
                "http://secret@rsshub:1200/caixin/latest", "http://rsshub:1201/caixin/latest"):
        assert feeds.load_publisher_feed(url, cfg("caixin"), 20, 3) is None


@pytest.mark.parametrize("url", ["https://127.0.0.1/csrc/c100028/c1/content.shtml",
    "https://www.csrc.gov.cn.evil.test/csrc/c100028/c1/content.shtml",
    "https://user:secret@www.csrc.gov.cn/csrc/c100028/c1/content.shtml",
    "https://www.csrc.gov.cn:secret/csrc/c100028/c1/content.shtml",
    "https://www.csrc.gov.cn/csrc/c100028/c1/content.shtml?redirect=x"])
def test_csrc_article_identity_is_strict(url):
    with pytest.raises(ValueError, match="^CSRC article URL identity invalid$"):
        feeds._csrc_url(url)


def test_native_fetch_keeps_pinned_fetcher_with_exact_policy(monkeypatch):
    captured = {}

    def fetch(url, timeout, **kwargs):
        captured.update(url=url, timeout=timeout, **kwargs)
        return b"ok"

    monkeypatch.setattr(feeds, "fetch_feed", fetch)
    assert feeds._fetch(feeds.CAIXIN_INDEX, 20, 3) == b"ok"
    assert captured == {"url": feeds.CAIXIN_INDEX, "timeout": 20, "max_attempts": 3,
                        "cfg": {"feeds": [{"url": feeds.CAIXIN_INDEX}]}}


def test_processor_dispatches_native_feed_and_keeps_other_feeds(monkeypatch):
    from core import processor

    monkeypatch.setattr(feeds, "load_publisher_feed", lambda *args: b"native")
    monkeypatch.setattr(processor, "fetch_feed", lambda *args, **kwargs: b"rsshub")
    assert processor._load_feed_bytes("http://rsshub:1200/caixin/latest", cfg("caixin"), 20, 3) == b"native"
    monkeypatch.setattr(feeds, "load_publisher_feed", lambda *args: None)
    assert processor._load_feed_bytes("http://rsshub:1200/yicai/latest", {}, 20, 3) == b"rsshub"


def test_publisher_adapter_is_in_strategy_fingerprint(tmp_path, monkeypatch):
    import importlib
    config = importlib.import_module("core.config")
    (tmp_path / "core").mkdir()
    (tmp_path / "services").mkdir()
    monkeypatch.setattr(config, "__file__", str(tmp_path / "core" / "config.py"))
    adapter = tmp_path / "services" / "publisher_feeds.py"
    adapter.write_text("source = 1\n")
    before = config._strategy_code_fingerprint()
    adapter.write_text("source = 2\n")
    assert config._strategy_code_fingerprint() != before
