import json
import xml.etree.ElementTree as ET

import feedparser
import pytest

from services import publisher_feeds as feeds

ROUTE = "http://rsshub:1200/wallstreetcn/news/global"


def config(limit=2):
    return {"feeds": [{"url": ROUTE, "max_entries": limit}]}


def resource(identity=123, kind="article", **changes):
    path = "articles" if kind == "article" else "livenews"
    value = {"id": identity, "uri": f"https://wallstreetcn.com/{path}/{identity}",
             "display_time": 1791536816, "title": "原始列表标题"}
    value.update(changes)
    return {"resource_type": kind, "resource": value}


def detail(identity=123, **changes):
    value = {"id": identity, "title": "发布方原始标题", "content": "<p>公开原文。</p>",
             "content_more": "<p>原文续段。</p>"}
    value.update(changes)
    return value


def payload(data, code=20000):
    return json.dumps({"code": code, "data": data}, ensure_ascii=False).encode()


def mock_fetch(monkeypatch, rows, details=None):
    calls = []
    details = details or {}

    def fetch(url, timeout, attempts):
        calls.append((url, timeout, attempts))
        if "information-flow?" in url:
            return payload({"items": rows})
        identity = int(url.split("/")[-1].split("?")[0])
        value = details.get(identity, detail(identity))
        if isinstance(value, Exception):
            raise value
        if isinstance(value, bytes):
            return value
        return payload(value)

    monkeypatch.setattr(feeds, "_fetch", fetch)
    return calls


def test_native_global_caps_before_details_and_preserves_original_identity(monkeypatch):
    rows = [{"resource_type": "ad"}, resource(), resource(124, "live"),
            resource(125), {"resource_type": "unknown", "resource": None}]
    calls = mock_fetch(monkeypatch, rows, {124: detail(124, title=None, content_text="快讯原始标题")})
    xml = feeds.load_publisher_feed(ROUTE, config(), 17, 2)
    entries = feedparser.parse(xml).entries
    assert len(entries) == 2
    assert [entry.link for entry in entries] == ["https://wallstreetcn.com/articles/123",
                                               "https://wallstreetcn.com/livenews/124"]
    assert [entry.title for entry in entries] == ["发布方原始标题", "快讯原始标题"]
    assert "公开原文" in entries[0].summary and "原文续段" in entries[0].summary
    assert entries[0].published_parsed[:6] == (2026, 10, 9, 9, 6, 56)
    assert set(calls) == {
        (feeds.WALLSTREET_API + "information-flow?channel=global-channel&accept=article&limit=2", 17, 2),
        (feeds.WALLSTREET_API + "articles/123?extract=0", 17, 2),
        (feeds.WALLSTREET_API + "lives/124", 17, 2),
    }
    channel = ET.fromstring(xml).find("channel")
    assert channel.findtext("title") == "华尔街见闻全球"
    assert "不绕过付费权限" in channel.findtext("description")
    assert entries[0].id == entries[0].link


def test_member_article_preserves_original_link_without_bypassing_access(monkeypatch):
    rows = [resource(uri="https://wallstreetcn.com/member/articles/123")]
    calls = mock_fetch(monkeypatch, rows, {123: detail(content="公开摘录", content_more=None)})
    entries = feedparser.parse(feeds.load_publisher_feed(ROUTE, config(1), 20, 3)).entries
    assert entries[0].link == "https://wallstreetcn.com/member/articles/123"
    assert entries[0].summary == "公开摘录"
    assert calls[-1][0] == feeds.WALLSTREET_API + "articles/123?extract=0"


@pytest.mark.parametrize("identity", [41959998, 41959997, 41959996])
def test_official_chart_flash_keeps_identity_and_uses_public_live_detail(monkeypatch, identity):
    # Observed in the publisher's global-channel list and lives/{id} detail.
    uri = f"https://wallstreetcn.com/charts/{identity}"
    rows = [resource(), resource(124, "live"),
            resource(identity, "live", type="flash", uri=uri, content="列表内容不应作为正文"),
            {"resource_type": "unknown", "resource": None}]
    calls = mock_fetch(monkeypatch, rows, {
        identity: detail(identity, type="flash", uri=uri, content_more=None),
    })
    xml = feeds.load_publisher_feed(ROUTE, config(3), 17, 2)
    entries = feedparser.parse(xml).entries
    assert [entry.link for entry in entries] == ["https://wallstreetcn.com/articles/123",
                                               "https://wallstreetcn.com/livenews/124", uri]
    assert entries[-1].id == uri
    assert entries[-1].title == "发布方原始标题"
    assert entries[-1].summary == "公开原文。"
    assert entries[-1].published_parsed[:6] == (2026, 10, 9, 9, 6, 56)
    assert set(calls) == {
        (feeds.WALLSTREET_API + "information-flow?channel=global-channel&accept=article&limit=3", 17, 2),
        (feeds.WALLSTREET_API + "articles/123?extract=0", 17, 2),
        (feeds.WALLSTREET_API + "lives/124", 17, 2),
        (feeds.WALLSTREET_API + f"lives/{identity}", 17, 2),
    }


@pytest.mark.parametrize("kind, subtype", [("article", "flash"), ("live", None),
                                          ("live", "live"), ("live", "chart"),
                                          ("live", ["flash"]), ("flash", "flash")])
def test_chart_link_requires_official_live_flash_resource_type(monkeypatch, kind, subtype):
    calls = mock_fetch(monkeypatch, [resource(kind=kind, type=subtype,
                                            uri="https://wallstreetcn.com/charts/123")])
    with pytest.raises(ValueError, match="schema invalid|URL identity invalid"):
        feeds.load_publisher_feed(ROUTE, config(1), 20, 3)
    assert len(calls) == 1


@pytest.mark.parametrize("uri", [
    "http://wallstreetcn.com/charts/123", "https://wallstreetcn.com.evil.test/charts/123",
    "https://user:secret@wallstreetcn.com/charts/123", "https://wallstreetcn.com:8443/charts/123",
    "https://wallstreetcn.com/charts/124", "https://wallstreetcn.com/charts/123?redirect=x",
    "https://wallstreetcn.com/charts/123#x", "https://wallstreetcn.com/charts/123/",
    "https://wallstreetcn.com/charts/00123", "https://wallstreetcn.com/charts/%31%32%33",
    "https://wallstreetcn.com\\@evil.test/charts/123", "https://wallstreetcn.com/charts/123\n",
])
def test_chart_original_link_rejects_spoofed_host_id_or_path(monkeypatch, uri):
    calls = mock_fetch(monkeypatch, [resource(kind="live", type="flash", uri=uri)])
    with pytest.raises(ValueError, match="URL identity invalid"):
        feeds.load_publisher_feed(ROUTE, config(1), 20, 3)
    assert len(calls) == 1


@pytest.mark.parametrize("changes", [
    {"id": 124}, {"id": True}, {"id": 123.0}, {"type": None}, {"type": "live"},
    {"uri": None}, {"uri": "https://wallstreetcn.com/charts/124"},
    {"uri": "https://evil.test/charts/123"}, {"uri": "https://wallstreetcn.com/livenews/123"},
    {"content": None}, {"content": "<p></p>", "content_more": None},
    {"content_more": []}, {"title": " "},
])
def test_chart_detail_identity_and_public_body_failure_cannot_be_partial_success(monkeypatch, changes):
    uri = "https://wallstreetcn.com/charts/123"
    value = detail(123, type="flash", uri=uri, content_more=None)
    value.update(changes)
    mock_fetch(monkeypatch, [resource(122), resource(kind="live", type="flash", uri=uri)],
               {123: value})
    with pytest.raises(ValueError):
        feeds.load_publisher_feed(ROUTE, config(), 20, 3)


@pytest.mark.parametrize("bad", [None, [], "null", {"items": None}, {"items": []},
                                {"items": [{"resource_type": "ad"}]}, {"items": [None]},
                                {"items": [{"resource_type": "video", "resource": {}}]},
                                {"items": [{"resource_type": "article", "resource": None}]}])
def test_invalid_global_list_never_reports_empty_success(monkeypatch, bad):
    monkeypatch.setattr(feeds, "_fetch", lambda *args: payload(bad))
    with pytest.raises(ValueError):
        feeds.load_publisher_feed(ROUTE, config(1), 20, 3)


@pytest.mark.parametrize("raw", [b"null", b'"null"', b"<html>unavailable</html>",
                                 payload({"items": [resource()]}, code=401)])
def test_response_failure_and_cached_null_are_not_valid_feeds(monkeypatch, raw):
    monkeypatch.setattr(feeds, "_fetch", lambda *args: raw)
    with pytest.raises(ValueError):
        feeds.load_publisher_feed(ROUTE, config(1), 20, 3)


@pytest.mark.parametrize("identity", [True, 0, -1, "123", 123.0, None])
def test_resource_id_requires_positive_integer(monkeypatch, identity):
    calls = mock_fetch(monkeypatch, [resource(identity)])
    with pytest.raises(ValueError, match="id invalid"):
        feeds.load_publisher_feed(ROUTE, config(1), 20, 3)
    assert len(calls) == 1


@pytest.mark.parametrize("stamp", [True, 0, -1, "1791536816", None, float("inf"), float("nan")])
def test_resource_time_is_original_finite_positive_epoch(monkeypatch, stamp):
    calls = mock_fetch(monkeypatch, [resource(display_time=stamp)])
    with pytest.raises(ValueError, match="timestamp invalid"):
        feeds.load_publisher_feed(ROUTE, config(1), 20, 3)
    assert len(calls) == 1


@pytest.mark.parametrize("uri", [
    None, "http://wallstreetcn.com/articles/123", "https://wallstreetcn.com.evil.test/articles/123",
    "https://user:secret@wallstreetcn.com/articles/123", "https://wallstreetcn.com:8443/articles/123",
    "https://wallstreetcn.com/articles/124", "https://wallstreetcn.com/livenews/123",
    "https://wallstreetcn.com/articles/123?redirect=x", "https://wallstreetcn.com/articles/123#x",
    "https://wallstreetcn.com/articles/123/", "https://wallstreetcn.com/articles/00123",
    "https://wallstreetcn.com/articles/%31%32%33", "https://wallstreetcn.com\\@evil.test/articles/123",
    "https://wallstreetcn.com/articles/123\n", " https://wallstreetcn.com/articles/123",
])
def test_original_link_must_match_official_host_type_and_id(monkeypatch, uri):
    calls = mock_fetch(monkeypatch, [resource(uri=uri)])
    with pytest.raises(ValueError, match="URL identity invalid"):
        feeds.load_publisher_feed(ROUTE, config(1), 20, 3)
    assert len(calls) == 1


@pytest.mark.parametrize("value", [None, "null", {}, detail(124), detail(True),
                                  detail(123.0), detail(title=None), detail(title=" "),
                                  detail(content=None), detail(content_more=[]),
                                  detail(content="", content_more=""), detail(content="<p></p>", content_more="")])
def test_selected_detail_failure_cannot_be_disguised_as_partial_success(monkeypatch, value):
    mock_fetch(monkeypatch, [resource(122), resource(123)], {123: value})
    with pytest.raises(ValueError):
        feeds.load_publisher_feed(ROUTE, config(), 20, 3)


@pytest.mark.parametrize("kind, changes", [("article", {}),
                                          ("live", {"type": "flash", "uri": "https://wallstreetcn.com/charts/124"})])
def test_selected_network_failure_propagates_without_list_summary_fallback(monkeypatch, kind, changes):
    mock_fetch(monkeypatch, [resource(), resource(124, kind, **changes)],
               {124: TimeoutError("upstream unavailable")})
    with pytest.raises(TimeoutError, match="upstream unavailable"):
        feeds.load_publisher_feed(ROUTE, config(), 20, 3)


def deleted_payload(message="内容不存在或已被删除", data=None, code=60301):
    return json.dumps({"code": code, "message": message, "data": {} if data is None else data}).encode()


@pytest.mark.parametrize("kind, changes", [("article", {}),
                                          ("live", {"type": "flash", "uri": "https://wallstreetcn.com/charts/123"})])
def test_confirmed_deleted_item_is_excluded_within_fixed_prefix_without_backfill(monkeypatch, caplog, kind, changes):
    calls = mock_fetch(monkeypatch, [resource(123, kind, **changes), resource(124), resource(125)],
                       {123: deleted_payload()})
    xml = feeds.load_publisher_feed(ROUTE, config(), 20, 3)
    entries = feedparser.parse(xml).entries
    assert [entry.link for entry in entries] == ["https://wallstreetcn.com/articles/124"]
    assert len(calls) == 3
    assert not any("articles/125" in url for url, _, _ in calls)
    assert "1条公开内容，1条已删除未展示" in ET.fromstring(xml).findtext("channel/description")
    assert "publisher confirms deleted content" in caplog.text


@pytest.mark.parametrize("kind, changes", [("article", {}),
                                          ("live", {"type": "flash", "uri": "https://wallstreetcn.com/charts/123"})])
def test_all_selected_items_deleted_remains_source_failure(monkeypatch, kind, changes):
    mock_fetch(monkeypatch, [resource(123, kind, **changes)], {123: deleted_payload()})
    with pytest.raises(ValueError, match="no eligible public articles"):
        feeds.load_publisher_feed(ROUTE, config(1), 20, 3)


@pytest.mark.parametrize("raw", [deleted_payload(message="无权限"),
                                 deleted_payload(data={"id": 123}), deleted_payload(code=403),
                                 '{"code":60301,"message":"内容不存在或已被删除","data":null}'.encode()])
@pytest.mark.parametrize("kind, changes", [("article", {}),
                                          ("live", {"type": "flash", "uri": "https://wallstreetcn.com/charts/123"})])
def test_only_exact_confirmed_deletion_is_excluded(monkeypatch, raw, kind, changes):
    mock_fetch(monkeypatch, [resource(123, kind, **changes), resource(124)], {123: raw})
    with pytest.raises(ValueError, match="not successful"):
        feeds.load_publisher_feed(ROUTE, config(), 20, 3)


def test_detail_error_code_is_failure(monkeypatch):
    def fetch(url, *args):
        return payload({"items": [resource()]}) if "information-flow?" in url else payload({}, code=403)

    monkeypatch.setattr(feeds, "_fetch", fetch)
    with pytest.raises(ValueError, match="not successful"):
        feeds.load_publisher_feed(ROUTE, config(1), 20, 3)


def test_selected_prefix_is_validated_before_any_detail_requests(monkeypatch):
    # Contract since the skip-and-count change: a spoofed row is skipped and
    # never fetched or served; validation still precedes every detail request.
    calls = mock_fetch(monkeypatch, [resource(), resource(124, uri="https://evil.test/124")])
    entries = feedparser.parse(feeds.load_publisher_feed(ROUTE, config(), 20, 3)).entries
    assert [entry.link for entry in entries] == ["https://wallstreetcn.com/articles/123"]
    assert [url for url, *_ in calls] == [
        feeds.WALLSTREET_API + "information-flow?channel=global-channel&accept=article&limit=2",
        feeds.WALLSTREET_API + "articles/123?extract=0",
    ]


def test_mixed_list_skips_and_counts_unrecognized_rows_by_shape_only(monkeypatch, caplog):
    rows = [
        resource(uri="https://wallstreetcn.com/premium/articles/9001?token=secret"),
        None,
        {"resource_type": "video", "resource": {"uri": "https://wallstreetcn.com/videos/7"}},
        resource(123),
        resource(124, "live"),
    ]
    calls = mock_fetch(monkeypatch, rows)
    with caplog.at_level("WARNING", logger=feeds._LOGGER.name):
        parsed = feedparser.parse(feeds.load_publisher_feed(ROUTE, config(), 20, 3))
        entries = parsed.entries
    assert [entry.link for entry in entries] == [
        "https://wallstreetcn.com/articles/123", "https://wallstreetcn.com/livenews/124",
    ]
    assert len(calls) == 3  # list + two valid details; skipped rows never fetched
    message = "\n".join(record.getMessage() for record in caplog.records)
    assert "skipped 3 unrecognized list rows" in message
    assert "article:/premium/articles/{n}x1" in message
    assert "row:NoneTypex1" in message and "video:/videos/{n}x1" in message
    assert "secret" not in message and "wallstreetcn.com" not in message
    assert feeds.publisher_source_coverage(parsed) == {
        "source_unrecognized": 3, "source_excluded_ads": 0, "source_excluded_deleted": 0,
    }


def test_known_exclusions_are_counted_separately_from_unknown_rows(monkeypatch):
    mock_fetch(monkeypatch, [{"resource_type": "ad"}, resource(123), resource(124)],
               {123: deleted_payload()})

    parsed = feedparser.parse(feeds.load_publisher_feed(ROUTE, config(), 20, 3))

    assert [entry.link for entry in parsed.entries] == ["https://wallstreetcn.com/articles/124"]
    assert feeds.publisher_source_coverage(parsed) == {
        "source_unrecognized": 0, "source_excluded_ads": 1, "source_excluded_deleted": 1,
    }


@pytest.mark.parametrize("changes", [
    {"id": True}, {"display_time": 10**100},
    {"uri": "https://wallstreetcn.com/articles/124?private=secret"},
])
def test_malformed_identity_rows_leave_explicit_coverage_gap(monkeypatch, changes):
    calls = mock_fetch(monkeypatch, [resource(124, **changes), resource(123)])

    parsed = feedparser.parse(feeds.load_publisher_feed(ROUTE, config(), 20, 3))

    assert len(parsed.entries) == 1
    assert feeds.publisher_source_coverage(parsed)["source_unrecognized"] == 1
    assert len(calls) == 2


@pytest.mark.parametrize("field", [
    "source_unrecognized", "source_excluded_ads", "source_excluded_deleted",
])
@pytest.mark.parametrize("value", ["-1", "1.5", "true", "nan", "1000000000", "1\n0"])
def test_invalid_coverage_metadata_cannot_claim_full_source(field, value):
    xml = f"<rss><channel><ai_filter_{field}>{value}</ai_filter_{field}></channel></rss>".encode()
    with pytest.raises(ValueError, match="coverage metadata invalid"):
        feeds.publisher_source_coverage(feedparser.parse(xml))


def test_ordinary_rss_without_adapter_metadata_keeps_complete_coverage():
    parsed = feedparser.parse(b"<rss><channel><title>ordinary</title></channel></rss>")
    assert feeds.publisher_source_coverage(parsed) == {
        "source_unrecognized": 0, "source_excluded_ads": 0, "source_excluded_deleted": 0,
    }


def test_coverage_metadata_accepts_feed_attribute_used_by_parsed_feed_wrappers():
    from types import SimpleNamespace

    parsed = SimpleNamespace(feed={"ai_filter_source_unrecognized": "2"})
    assert feeds.publisher_source_coverage(parsed)["source_unrecognized"] == 2


def test_all_unrecognized_rows_still_fail_the_source(monkeypatch):
    rows = [resource(uri="https://evil.test/articles/123"), None,
            {"resource_type": "video", "resource": {}}]
    calls = mock_fetch(monkeypatch, rows)
    with pytest.raises(ValueError, match="no recognizable rows .*3 skipped"):
        feeds.load_publisher_feed(ROUTE, config(), 20, 3)
    assert len(calls) == 1


def test_detail_parallelism_is_bounded_at_three(monkeypatch):
    workers = []
    real_pool = feeds.ThreadPoolExecutor

    def pool(*args, **kwargs):
        workers.append(kwargs["max_workers"])
        return real_pool(*args, **kwargs)

    monkeypatch.setattr(feeds, "ThreadPoolExecutor", pool)
    mock_fetch(monkeypatch, [resource(index) for index in range(1, 51)])
    assert len(feedparser.parse(feeds.load_publisher_feed(ROUTE, config(50), 20, 3)).entries) == 50
    assert workers == [3]


def test_adapter_requires_single_enabled_exact_global_route(monkeypatch):
    monkeypatch.setattr(feeds, "_fetch", lambda *args: pytest.fail("network must not be reached"))
    for cfg in ({}, {"feeds": [{"url": ROUTE, "enabled": False}]},
                {"feeds": [{"url": ROUTE}, {"url": ROUTE}]}):
        with pytest.raises(PermissionError):
            feeds.load_publisher_feed(ROUTE, cfg, 20, 3)
    for url in (ROUTE + "?limit=1", ROUTE + "/", ROUTE.replace("global", "live"),
                ROUTE.replace("rsshub", "evil.test"), ROUTE.replace("1200", "1201")):
        assert feeds.load_publisher_feed(url, config(), 20, 3) is None


def test_processor_dispatches_global_native_feed_without_changing_other_routes(monkeypatch):
    from core import processor

    mock_fetch(monkeypatch, [resource()])
    monkeypatch.setattr(processor, "fetch_feed", lambda *args, **kwargs: b"normal-rsshub")
    entries = feedparser.parse(processor._load_feed_bytes(ROUTE, config(1), 20, 3)).entries
    assert entries[0].link == "https://wallstreetcn.com/articles/123"
    assert processor._load_feed_bytes(ROUTE.replace("global", "live"), {}, 20, 3) == b"normal-rsshub"
