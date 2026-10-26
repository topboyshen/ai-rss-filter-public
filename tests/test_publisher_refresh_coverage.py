"""Partial publisher lists must remain visible through real refresh/readiness paths."""
import json
import re
import time
from types import SimpleNamespace

import pytest

from services import publisher_feeds

ROUTE = "http://rsshub:1200/wallstreetcn/news/global"


def _resource(identity, uri=None, kind="article"):
    return {"resource_type": kind, "resource": {
        "id": identity, "uri": uri or f"https://wallstreetcn.com/articles/{identity}",
        "display_time": 1791536816,
    }}


def _configure_real_chain(monkeypatch, rows, *, deleted=()):
    from core import processor
    from db import database
    from services import refresh

    database.init_db()
    cfg = {
        "feeds": [{"url": ROUTE, "name": "华尔街见闻全球", "max_entries": 2}],
        "cache_ttl": 600, "staleness_warn_seconds": 14400, "similarity_window_hours": 72,
        "_blacklist_re": re.compile("never-match"),
    }
    captured, cached = [], []

    def fetch(url, *args):
        if "information-flow?" in url:
            response = {"code": 20000, "data": {"items": rows}}
        else:
            identity = int(url.rsplit("/", 1)[-1].split("?")[0])
            if identity in deleted:
                response = {"code": 60301, "message": "内容不存在或已被删除", "data": {}}
            else:
                response = {"code": 20000, "data": {
                    "id": identity, "title": f"公司{identity}获得项目订单", "content": "公开原文",
                }}
        return json.dumps(response).encode()

    def real_process(*args, **kwargs):
        outcome = processor.process_feed(*args, **kwargs)
        captured.append(outcome)
        return outcome

    def passed_entry(entry, *args, **kwargs):
        entry.title = "[⭐8.0] " + entry.title
        return entry, "scored"

    monkeypatch.setattr(publisher_feeds, "_fetch", fetch)
    monkeypatch.setattr(processor, "resolve_scoring_contract", lambda cfg, **kwargs: cfg)
    monkeypatch.setattr(processor, "_repair_cached_feed_origins", lambda *args: {})
    monkeypatch.setattr(processor, "Scorer", lambda: SimpleNamespace(backends=[1]))
    monkeypatch.setattr(processor, "process_entry", passed_entry)
    monkeypatch.setattr(processor, "bump_stat", lambda *args, **kwargs: None)
    monkeypatch.setattr(refresh, "_cfg", lambda: cfg)
    monkeypatch.setattr(refresh, "resolve_scoring_contract", lambda cfg, **kwargs: cfg)
    monkeypatch.setattr(refresh, "process_feed", real_process)
    monkeypatch.setattr(refresh, "get_metrics", lambda: None)
    monkeypatch.setattr(refresh, "flush_stats", lambda *args: None)
    monkeypatch.setattr(refresh, "cleanup_old_entries", lambda *args: None)
    monkeypatch.setattr(refresh, "set_cached", lambda *args: cached.append(args))
    return refresh, database, cfg, captured, cached


@pytest.mark.parametrize("unrecognized", [
    _resource(124, "https://wallstreetcn.com/premium/articles/124"),
    _resource(124, "https://evil.test/articles/124?private=secret"),
    {"resource_type": "video", "resource": {}},
])
def test_missing_row_keeps_valid_news_and_degradation_until_complete_refresh(monkeypatch, unrecognized):
    from routes import shared
    from services.ingestion_status import describe_refresh_coverage

    rows = [unrecognized, _resource(123)]
    refresh, database, cfg, captured, cached = _configure_real_chain(monkeypatch, rows)
    prior_full = int(time.time()) - 300
    database.set_last_full_refresh_ts(prior_full)

    partial = refresh._execute_feed_refresh()

    assert [entry.link for entry in captured[0][0]] == ["https://wallstreetcn.com/articles/123"]
    assert captured[0][1]["source_unrecognized"] == 1
    assert partial["status"] == "partial"
    assert partial["succeeded_sources"] == 0 and partial["partial_sources"] == 1
    assert partial["source_unrecognized_entries"] == 1
    assert partial["score_deferred_entries"] == 0
    assert database.get_last_full_refresh_ts() == prior_full
    assert cached == []
    health = database.get_feed_health()[0]
    assert health[4:6] == (0, 1)
    assert "1" in health[9] and "secret" not in health[9]
    readiness = shared._staleness_info(cfg)
    assert readiness["degraded"] is True and readiness["stale"] is False
    assert readiness["last_attempt"] == partial
    state, text = describe_refresh_coverage(partial)
    assert state == "incomplete" and "华尔街见闻全球" in text

    rows[:] = [_resource(123), _resource(124)]
    complete = refresh._execute_feed_refresh()

    assert complete["status"] == "success"
    assert complete["partial_sources"] == complete["source_unrecognized_entries"] == 0
    assert len(captured[1][0]) == 2 and len(cached) == 1
    assert database.get_last_full_refresh_ts() == complete["finished_at"]
    assert database.get_feed_health()[0][9] is None
    assert shared._staleness_info(cfg)["degraded"] is False
    assert describe_refresh_coverage(complete)[0] == "complete"


def test_advertisement_and_confirmed_deletion_do_not_create_false_gap(monkeypatch):
    from routes import shared

    rows = [{"resource_type": "ad"}, _resource(123), _resource(124)]
    refresh, database, cfg, captured, cached = _configure_real_chain(monkeypatch, rows, deleted=(123,))

    result = refresh._execute_feed_refresh()

    assert result["status"] == "success" and result["partial_sources"] == 0
    assert [entry.link for entry in captured[0][0]] == ["https://wallstreetcn.com/articles/124"]
    assert captured[0][1]["source_unrecognized"] == 0
    assert captured[0][1]["source_excluded_ads"] == captured[0][1]["source_excluded_deleted"] == 1
    assert len(cached) == 1 and database.get_feed_health()[0][9] is None
    assert shared._staleness_info(cfg)["degraded"] is False


def test_source_gap_and_pending_score_count_one_partial_source_until_both_recover(monkeypatch):
    from core import processor
    from routes import shared
    from services.ingestion_status import describe_refresh_coverage

    rows = [_resource(125, "https://evil.test/articles/125"), _resource(123), _resource(124)]
    refresh, database, cfg, captured, cached = _configure_real_chain(monkeypatch, rows)
    score = processor.process_entry
    pending = {"https://wallstreetcn.com/articles/124"}

    def score_or_defer(entry, *args, **kwargs):
        if entry.link in pending:
            return None, processor.SCORE_DEFERRED_REASON
        return score(entry, *args, **kwargs)

    monkeypatch.setattr(processor, "process_entry", score_or_defer)
    database.set_last_full_refresh_ts(123)
    partial = refresh._execute_feed_refresh()

    assert partial["partial_sources"] == partial["enabled_sources"] == 1
    assert partial["succeeded_sources"] == partial["failed_sources"] == 0
    assert partial["source_unrecognized_entries"] == partial["score_deferred_entries"] == 1
    assert captured[0][1]["passed"] == 1 and cached == []
    assert database.get_last_full_refresh_ts() == 123
    assert shared._staleness_info(cfg)["degraded"] is True
    state, text = describe_refresh_coverage(partial)
    assert state == "incomplete" and "未识别内容 1 条、待评分 1 条" in text

    rows.pop(0)
    pending.clear()
    complete = refresh._execute_feed_refresh()
    assert complete["status"] == "success" and len(captured[1][0]) == 2
    assert len(cached) == 1 and shared._staleness_info(cfg)["degraded"] is False


def test_all_unknown_rows_are_failed_and_cannot_advance_refresh(monkeypatch):
    from routes import shared

    rows = [_resource(123, "https://evil.test/articles/123")]
    refresh, database, cfg, captured, cached = _configure_real_chain(monkeypatch, rows)
    database.set_last_full_refresh_ts(123)

    result = refresh._execute_feed_refresh()

    assert result["status"] == "failed" and result["partial_sources"] == 0
    assert captured[0][0] == [] and cached == []
    assert database.get_last_full_refresh_ts() == 123
    assert database.get_feed_health()[0][4:6] == (0, 1)
    assert shared._staleness_info(cfg)["degraded"] is True
