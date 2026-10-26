import json
import xml.etree.ElementTree as ET

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
    "https://www.csrc.gov.cn/csrc/c100028/c1/content.shtml?redirect=x"])
def test_csrc_article_identity_is_strict(url):
    with pytest.raises(ValueError):
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
