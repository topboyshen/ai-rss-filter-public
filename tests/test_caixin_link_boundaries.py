"""Exercise Caixin URL identity through the real bounded feed adapter."""
import json
import logging
import re
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import feedparser
import pytest

from services import publisher_feeds as feeds


ROUTE = "http://rsshub:1200/caixin/latest"
ARTICLE = "https://finance.caixin.com/2026-09-28/102489393.html"
TOPIC = "https://key.caixin.com/topic/BQ02.000008077"
COLUMN = "https://finance.caixin.com/column/macro.news"
MEDIA = "https://video.caixin.com/2026-09-28/123.html"
LINK_KINDS = (ARTICLE, TOPIC, COLUMN, MEDIA)


def row(url=ARTICLE, **updates):
    value = {"url": url, "title": "有效文章", "summary": "原始摘要", "time": 1790604480000}
    value.update(updates)
    return value


def load(monkeypatch, rows, limit=2):
    calls = []

    def fetch(url, timeout, attempts):
        calls.append((url, timeout, attempts))
        return json.dumps({"code": 0, "data": {"articleList": rows}}).encode()

    monkeypatch.setattr(feeds, "_fetch", fetch)
    config = {"feeds": [{"url": ROUTE, "max_entries": limit}]}
    result = feeds.load_publisher_feed(ROUTE, config, 20, 3)
    assert calls == [(feeds.CAIXIN_INDEX, 20, 3)]
    return result


@pytest.mark.parametrize("url", LINK_KINDS)
@pytest.mark.parametrize("suffix", ["?", "#", "?query=1", "#fragment"])
def test_query_and_fragment_markers_never_become_eligible_or_skipped(monkeypatch, url, suffix):
    with pytest.raises(ValueError, match="Caixin article URL identity invalid"):
        load(monkeypatch, [row(), row(url + suffix)])


@pytest.mark.parametrize("url", LINK_KINDS)
@pytest.mark.parametrize("prefix", ["@", ":@", "user@", "user:secret@"])
def test_all_userinfo_including_empty_userinfo_is_rejected(monkeypatch, url, prefix):
    unsafe = url.replace("https://", "https://" + prefix, 1)
    with pytest.raises(ValueError, match="Caixin article URL identity invalid"):
        load(monkeypatch, [row(), row(unsafe)])


@pytest.mark.parametrize("url", LINK_KINDS)
@pytest.mark.parametrize("transform", [
    lambda url: " " + url,
    lambda url: "\t" + url,
    lambda url: url.replace("caixin", "cai\nxin"),
    lambda url: url.replace("caixin", "cai\txin"),
    lambda url: url.replace("https://", "https://evil\\"),
])
def test_raw_url_normalization_cannot_hide_identity_conflicts(monkeypatch, url, transform):
    with pytest.raises(ValueError, match="Caixin article URL identity invalid"):
        load(monkeypatch, [row(), row(transform(url))])


@pytest.mark.parametrize("authority", [
    "caixin.com", "caixin.com.evil.test", "notcaixin.com", ".caixin.com",
    "bad..caixin.com", "bad host.caixin.com", "bad_host.caixin.com",
    "-bad.caixin.com", "bad-.caixin.com", "finance.caixin.com.",
    "finance.caixin.com:80", "finance.caixin.com:444", "finance.caixin.com:",
    "finance.caixin.com:0443", "finance.caixin.com:secret", "finance.caixin.com:65536",
    "finance%2ecaixin.com", "財新.caixin.com", "K.caixin.com", "ſ.caixin.com", "[::1]", "[::1",
])
@pytest.mark.parametrize("path", ["/2026-09-28/102489393.html", "/column/macro.news"])
def test_similar_or_malformed_authorities_cannot_become_articles_or_skips(monkeypatch, authority, path):
    with pytest.raises(ValueError, match="Caixin article URL identity invalid"):
        load(monkeypatch, [row(), row("https://" + authority + path)])


@pytest.mark.parametrize("url", [
    "https://key.caixin.com/topic/.", "https://key.caixin.com/topic/..",
    "https://finance.caixin.com/column/.", "https://finance.caixin.com/special/..",
    "https://key.caixin.com/topic/", "https://key.caixin.com/topic/A/subpage",
    "https://finance.caixin.com/topic/BQ02.000008077",
    "https://key.caixin.com/topic/BQ02.000008077/",
    "https://key.caixin.com/topic/%42Q02.000008077",
    "https://key.caixin.com/topic/%2e%2e",
    "https://finance.caixin.com/column/a/../macro.news",
    "https://finance.caixin.com/special/a%2fb",
    "https://finance.caixin.com/columns/macro.news",
    "https://finance.caixin.com/specials/budget",
    "https://fm.caixin.com/playlist", "https://video.caixin.com/program",
    "https://datanews.caixin.com/index.html",
    "http://finance.caixin.com/2026-09-28/102489393.html",
    "//finance.caixin.com/2026-09-28/102489393.html",
])
def test_unknown_and_encoded_paths_remain_source_errors(monkeypatch, url):
    with pytest.raises(ValueError, match="Caixin article URL identity invalid"):
        load(monkeypatch, [row(), row(url)])


@pytest.mark.parametrize("url", [None, 17, True, {}, [], ""])
def test_non_string_link_is_a_named_identity_error(monkeypatch, url):
    with pytest.raises(ValueError, match="Caixin article URL identity invalid"):
        load(monkeypatch, [row(), row(url)])


@pytest.mark.parametrize("url", [
    ARTICLE, "https://FINANCE.CAIXIN.COM:443/2026-09-28/102489393.html",
    "HTTPS://finance.caixin.com/2026-09-28/102489393.html",
    "https://www.caixin.com/2026-09-28/102489393.html",
    "https://channel.finance.caixin.com/2026-09-28/102489393.html",
])
def test_existing_https_article_identity_is_preserved_without_canonicalization(monkeypatch, url, caplog):
    xml = load(monkeypatch, [row(url)], limit=1)
    parsed = feedparser.parse(xml)
    assert [(entry.link, entry.title, entry.summary) for entry in parsed.entries] == [
        (url, "有效文章", "原始摘要")]
    assert ET.fromstring(xml).findtext("channel/item/guid") == url
    assert "已识别非文章" not in ET.fromstring(xml).findtext("channel/description")
    assert "Caixin skipped" not in caplog.text


@pytest.mark.parametrize("url, kind", [
    (TOPIC, "topic"), ("https://KEY.CAIXIN.COM:443/topic/BQ02.000008077", "topic"),
    (COLUMN, "column"), ("https://www.caixin.com/special/budget-2026", "column"),
    (MEDIA, "media"), ("https://fm.caixin.com/2026-09-28/123.html", "media"),
    ("https://datanews.caixin.com/2026-09-28/123.html", "media"),
])
def test_named_non_article_can_lack_article_metadata_but_does_not_use_article_quota(
        monkeypatch, caplog, url, kind):
    with caplog.at_level(logging.WARNING):
        xml = load(monkeypatch, [{"url": url}, row()], limit=1)
    assert [entry.link for entry in feedparser.parse(xml).entries] == [ARTICLE]
    assert "已识别非文章链接 1 条未采集" in ET.fromstring(xml).findtext("channel/description")
    assert f"{kind}=1" in caplog.text
    assert feeds.publisher_source_coverage(feedparser.parse(xml)) == {
        "source_unrecognized": 0, "source_excluded_ads": 0, "source_excluded_deleted": 0,
    }


@pytest.mark.parametrize("url", [TOPIC, COLUMN, MEDIA])
def test_only_non_articles_fail_even_without_article_metadata(monkeypatch, url, caplog):
    with caplog.at_level(logging.WARNING), pytest.raises(ValueError, match="no eligible articles"):
        load(monkeypatch, [{"url": url}], limit=1)
    assert "Caixin skipped 1" in caplog.text


def test_skip_count_only_covers_rows_traversed_before_article_limit(monkeypatch, caplog):
    second = "https://finance.caixin.com/2026-09-28/102489394.html"
    rows = [{"url": TOPIC}, row(), {"url": MEDIA}, row(second), {"url": COLUMN}, None]
    with caplog.at_level(logging.WARNING):
        xml = load(monkeypatch, rows, limit=2)
    assert [entry.link for entry in feedparser.parse(xml).entries] == [ARTICLE, second]
    assert "本次已遍历范围" in ET.fromstring(xml).findtext("channel/description")
    assert "已识别非文章链接 2 条未采集" in ET.fromstring(xml).findtext("channel/description")
    assert "topic=1" in caplog.text and "media=1" in caplog.text
    assert "column=" not in caplog.text


@pytest.mark.parametrize("updates", [{"title": ""}, {"summary": None}, {"time": None}])
def test_article_schema_failure_after_named_skip_is_not_hidden(monkeypatch, updates):
    with pytest.raises(ValueError):
        load(monkeypatch, [{"url": TOPIC}, row(**updates)])


def configure_refresh(monkeypatch, rows):
    from core import processor
    from db import database
    from services import refresh

    database.init_db()
    config = {
        "feeds": [{"url": ROUTE, "name": "财新网", "max_entries": 2}],
        "cache_ttl": 600, "staleness_warn_seconds": 14400, "similarity_window_hours": 72,
        "_blacklist_re": re.compile("never-match"),
    }
    scored, cached = [], []

    def fetch(url, *args):
        assert url == feeds.CAIXIN_INDEX
        return json.dumps({"code": 0, "data": {"articleList": rows}}).encode()

    def score(entry, *args, **kwargs):
        scored.append(entry.link)
        entry.title = "[⭐8.0] " + entry.title
        return entry, "scored"

    monkeypatch.setattr(feeds, "_fetch", fetch)
    monkeypatch.setattr(processor, "resolve_scoring_contract", lambda cfg, **kwargs: cfg)
    monkeypatch.setattr(processor, "_repair_cached_feed_origins", lambda *args: {})
    monkeypatch.setattr(processor, "Scorer", lambda: SimpleNamespace(backends=[1]))
    monkeypatch.setattr(processor, "process_entry", score)
    monkeypatch.setattr(processor, "bump_stat", lambda *args, **kwargs: None)
    monkeypatch.setattr(refresh, "_cfg", lambda: config)
    monkeypatch.setattr(refresh, "resolve_scoring_contract", lambda cfg, **kwargs: cfg)
    monkeypatch.setattr(refresh, "get_metrics", lambda: None)
    monkeypatch.setattr(refresh, "flush_stats", lambda *args: None)
    monkeypatch.setattr(refresh, "cleanup_old_entries", lambda *args: None)
    monkeypatch.setattr(refresh, "set_cached", lambda *args: cached.append(args))
    return refresh, database, scored, cached


@pytest.mark.parametrize("rows, expected_status, expected_scored", [
    ([{"url": TOPIC}, {"url": MEDIA}, row()], "success", [ARTICLE]),
    ([{"url": TOPIC}, {"url": MEDIA}], "failed", []),
    ([{"url": TOPIC}, row(), row("https://evil.test/news")], "failed", []),
])
def test_actual_refresh_only_advances_for_eligible_articles_without_unknown_identity(
        monkeypatch, rows, expected_status, expected_scored):
    refresh, database, scored, cached = configure_refresh(monkeypatch, rows)
    database.set_last_full_refresh_ts(123)

    result = refresh._execute_feed_refresh()

    assert result["status"] == expected_status
    assert result["partial_sources"] == result["source_unrecognized_entries"] == 0
    assert scored == expected_scored
    health = database.get_feed_health()[0]
    if expected_status == "success":
        assert result["succeeded_sources"] == 1 and result["failed_sources"] == 0
        assert database.get_last_full_refresh_ts() == result["finished_at"]
        assert len(cached) == 1 and health[9] is None
    else:
        assert result["failed_sources"] == 1 and result["succeeded_sources"] == 0
        assert database.get_last_full_refresh_ts() == 123
        assert cached == [] and health[9]
        assert "evil.test" not in health[9]
