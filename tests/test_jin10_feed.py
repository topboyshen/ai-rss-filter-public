import gzip
import html
import xml.etree.ElementTree as ET

import httpx
import pytest


JIN10_ID = "20260722060338499800"
INTERNAL_LINK = f"jin10:index:{JIN10_ID}"
PUBLIC_LINK = f"https://flash.jin10.com/detail/{JIN10_ID}"
MARKUP_TITLE = (
    '<span class="section-news">1. 国务院国资委推进央企“AI+”专项行动。</span>'
    '<br /><span class="section-news">2. 加快算力基础设施建设。</span>'
)
PLAIN_TITLE = "1. 国务院国资委推进央企“AI+”专项行动。 2. 加快算力基础设施建设。"
TARGET_DANGLING_TITLE = (
    "国内新闻： 1. 央行公告，8月5日将开展5000亿元买断式逆回购操作，"
    '期限为3个月。 <span class="section-news...'
)
TARGET_PLAIN_TITLE = (
    "国内新闻： 1. 央行公告，8月5日将开展5000亿元买断式逆回购操作，"
    "期限为3个月。"
)
ASIA_DANGLING_TITLE = (
    "亚股 1. 中国上证综指8月4日（周二）收盘上涨12.62点，涨幅：0.33%，"
    '报3822.28点； <span class="section-ne...'
)
ASIA_PLAIN_TITLE = (
    "亚股 1. 中国上证综指8月4日（周二）收盘上涨12.62点，涨幅：0.33%，"
    "报3822.28点；"
)


class Jin10ResponseStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        yield from self.chunks

    def close(self):
        self.closed = True


@pytest.fixture
def jin10_upstream(monkeypatch):
    import services.jin10_feed as jin10_feed

    cache = {"source_url": "", "ts": 0.0, "items": []}
    requests = []
    original_client = httpx.Client
    monkeypatch.setattr(jin10_feed, "_JIN10_CACHE", cache)
    monkeypatch.setattr(jin10_feed, "_JIN10_MAX_RESPONSE_BYTES", 128, raising=False)

    def respond(*, headers, chunks):
        stream = Jin10ResponseStream(chunks)

        def handle(request):
            requests.append(request)
            return httpx.Response(200, headers=headers, stream=stream)

        transport = httpx.MockTransport(handle)
        monkeypatch.setattr(jin10_feed.httpx, "Client", lambda **kwargs: original_client(transport=transport, **kwargs))
        return stream

    return jin10_feed, cache, requests, respond


@pytest.mark.parametrize("encoding", ["identity", "gzip"])
def test_jin10_reads_feed_at_exact_decoded_limit_without_content_length(jin10_upstream, encoding):
    jin10_feed, _, requests, respond = jin10_upstream
    body = b'[{"title":"ok"}]'.ljust(128)
    wire = gzip.compress(body) if encoding == "gzip" else body
    stream = respond(
        headers={"Content-Type": "application/json", "Content-Encoding": encoding},
        chunks=(wire[:9], wire[9:]),
    )
    assert jin10_feed.fetch_jin10_latest("https://example.test/jin10") == [{"title": "ok"}]
    assert requests[0].headers["accept-encoding"] == "gzip, identity"
    assert stream.closed


def test_jin10_stops_reading_at_the_limit_without_content_length(jin10_upstream):
    jin10_feed, _, _, respond = jin10_upstream

    def chunks():
        yield b" " * 128
        yield b" "
        pytest.fail("upstream must be closed before reading another chunk")

    stream = respond(headers={}, chunks=chunks())
    with pytest.raises(ValueError, match="Jin10 response exceeds 128 bytes"):
        jin10_feed.fetch_jin10_latest("https://example.test/jin10")
    assert stream.closed


def test_jin10_gzip_over_limit_uses_same_source_stale_cache(jin10_upstream, caplog):
    jin10_feed, cache, requests, respond = jin10_upstream
    url = "https://example.test/jin10"
    stale = [{"title": "previous feed"}]
    cache.update(source_url=url, ts=0.0, items=stale)
    wire = gzip.compress(b'[{"title":"new feed"}]') + gzip.compress(b" " * 128)
    assert len(wire) < 128
    stream = respond(
        headers={"Content-Encoding": "gzip", "Content-Length": str(len(wire))},
        chunks=(wire,),
    )
    assert jin10_feed.fetch_jin10_latest(url) == stale
    assert jin10_feed.fetch_jin10_latest(url) == stale
    assert "Jin10ResponseTooLarge" in caplog.text
    assert len(requests) == 1
    assert stream.closed


def test_jin10_parses_compressed_rss_after_reading_the_bounded_body(jin10_upstream):
    jin10_feed, _, _, respond = jin10_upstream
    wire = gzip.compress(b"<rss><channel><item><title>OK</title></item></channel></rss>")
    respond(headers={"Content-Type": "application/rss+xml", "Content-Encoding": "gzip"}, chunks=(wire,))
    items = jin10_feed.fetch_jin10_latest("https://example.test/jin10")
    assert [item["title"] for item in items] == ["OK"]


@pytest.mark.parametrize(
    ("encoding", "body", "error"),
    [
        ("gzip", b"not gzip", "Invalid Jin10 gzip response"),
        ("gzip", gzip.compress(b'[{"title":"ok"}]')[:-1], "Incomplete Jin10 gzip response"),
        ("br", b"not advertised", "Unsupported Jin10 content encoding"),
    ],
)
def test_jin10_closes_invalid_encoded_responses(jin10_upstream, encoding, body, error):
    jin10_feed, _, _, respond = jin10_upstream
    stream = respond(headers={"Content-Encoding": encoding}, chunks=(body,))
    with pytest.raises(httpx.DecodingError, match=error):
        jin10_feed.fetch_jin10_latest("https://example.test/jin10")
    assert stream.closed


def test_normalize_item_maps_exact_rsshub_index_id_to_public_detail_url():
    from services.jin10_feed import normalize_item

    normalized = normalize_item({
        "id": INTERNAL_LINK,
        "title": "金十快讯",
        "url": INTERNAL_LINK,
        "content": "正文",
    })

    assert normalized is not None
    assert normalized["id"] == INTERNAL_LINK
    assert normalized["url"] == PUBLIC_LINK

    # The stable Jin10 ID wins over an incidental/shared upstream URL so the
    # RSS GUID remains stable between refreshes.
    normalized_with_shared_url = normalize_item({
        "id": INTERNAL_LINK,
        "title": "金十快讯",
        "url": "https://example.test/shared",
    })
    assert normalized_with_shared_url is not None
    assert normalized_with_shared_url["url"] == PUBLIC_LINK


def test_normalize_item_preserves_safe_http_url_and_rejects_unsafe_links():
    from services.jin10_feed import normalize_item

    safe = normalize_item({
        "id": "safe-id",
        "title": "已有公开链接",
        "url": "https://flash.jin10.com/detail/123",
    })
    assert safe is not None
    assert safe["url"] == "https://flash.jin10.com/detail/123"

    for unsafe_url in (
        "javascript:alert(1)",
        "https://user:password@flash.jin10.com/detail/123",
        "jin10:index:not-a-numeric-id",
    ):
        assert normalize_item({
            "id": "untrusted-id",
            "title": "不安全链接",
            "url": unsafe_url,
        }) is None

    for invalid_internal_link in (
        "jin10:index:2026072206033849980",
        "jin10:index:202607220603384998000",
        f"{INTERNAL_LINK}?redirect=https://evil.example",
        "jin10:index:２０２６０７２２０６０３３８４９９８００",
    ):
        assert normalize_item({
            "id": invalid_internal_link,
            "title": "格式不符",
            "url": invalid_internal_link,
        }) is None


def test_build_jin10_rss_emits_clickable_links_for_rsshub_items(monkeypatch):
    import services.jin10_feed as jin10_feed

    monkeypatch.setattr(
        jin10_feed,
        "fetch_jin10_latest",
        lambda _source_url=None: [{
            "id": INTERNAL_LINK,
            "title": "金十快讯",
            "url": INTERNAL_LINK,
            "content": "正文",
            "pubDate": "Wed, 22 Jul 2026 06:03:38 GMT",
        }],
    )

    xml = jin10_feed.build_jin10_rss(
        "https://ai.example.test/jin10/rss",
        {},
        source_url="https://rsshub.example.test/jin10",
    )
    root = ET.fromstring(xml)
    item = root.find("./channel/item")

    assert item is not None
    assert item.findtext("link") == PUBLIC_LINK
    assert item.findtext("guid") == PUBLIC_LINK


@pytest.mark.parametrize(
    "source_title",
    (
        MARKUP_TITLE,
        html.escape(MARKUP_TITLE),
        html.escape(html.escape(MARKUP_TITLE)),
    ),
)
def test_normalize_item_converts_literal_and_encoded_markup_to_plain_text(source_title):
    from services.jin10_feed import normalize_item, stable_item_id

    raw_item = {
        "url": PUBLIC_LINK,
        "title": source_title,
        "content": source_title,
    }
    identity_before_normalization = stable_item_id(raw_item)

    normalized = normalize_item(raw_item)

    assert normalized is not None
    assert normalized["id"] == identity_before_normalization
    assert normalized["title"] == PLAIN_TITLE
    assert normalized["content"] == PLAIN_TITLE
    assert "span" not in normalized["title"]
    assert "section-news" not in normalized["title"]
    assert "<br" not in normalized["title"]


@pytest.mark.parametrize(
    "source_title",
    (
        TARGET_DANGLING_TITLE,
        html.escape(TARGET_DANGLING_TITLE),
        html.escape(html.escape(TARGET_DANGLING_TITLE)),
    ),
)
def test_normalize_item_drops_truncated_target_markup_across_encoding_layers(source_title):
    from services.jin10_feed import normalize_item

    normalized = normalize_item({
        "id": "jin10:index:20260804235338178800",
        "url": "jin10:index:20260804235338178800",
        "title": source_title,
        "content": source_title,
    })

    assert normalized is not None
    assert normalized["title"] == TARGET_PLAIN_TITLE
    assert normalized["content"] == TARGET_PLAIN_TITLE


def test_plain_text_markup_cleanup_drops_current_asia_stock_truncated_tag():
    from utils.helpers import plain_text_from_markup

    assert plain_text_from_markup(ASIA_DANGLING_TITLE) == ASIA_PLAIN_TITLE


def test_display_title_does_not_restore_a_markup_only_fallback():
    from utils.helpers import format_article_display_title

    dangling_only = '<span class="section-news...'
    scored_dangling = f"[⭐8.8 📈 宏观] {dangling_only} 💡政策工具"

    assert format_article_display_title(dangling_only, dangling_only) == ""
    assert format_article_display_title(scored_dangling, dangling_only) == ""


def test_plain_text_markup_cleanup_drops_active_markup_but_preserves_market_comparisons():
    from utils.helpers import plain_text_from_markup

    source = (
        '研发投入<5%，AI+保持不变。'
        '<script>alert(1)</script><style>.bad{display:block}</style>'
        '<img src=x onerror="alert(2)">后续 1 < 2'
    )

    cleaned = plain_text_from_markup(source)

    assert cleaned == "研发投入<5%，AI+保持不变。 后续 1 < 2"
    assert "alert" not in cleaned
    assert "onerror" not in cleaned


def test_build_jin10_rss_does_not_reemit_upstream_title_markup(monkeypatch):
    import services.jin10_feed as jin10_feed

    monkeypatch.setattr(
        jin10_feed,
        "fetch_jin10_latest",
        lambda _source_url=None: [{
            "id": INTERNAL_LINK,
            "title": html.escape(MARKUP_TITLE),
            "url": INTERNAL_LINK,
            "content": html.escape(MARKUP_TITLE),
        }],
    )

    xml = jin10_feed.build_jin10_rss(
        "https://ai.example.test/jin10/rss",
        {},
        source_url="https://rsshub.example.test/jin10",
    )
    item = ET.fromstring(xml).find("./channel/item")

    assert item is not None
    assert item.findtext("title") == PLAIN_TITLE
    assert item.findtext("description") == PLAIN_TITLE
    assert "section-news" not in xml
