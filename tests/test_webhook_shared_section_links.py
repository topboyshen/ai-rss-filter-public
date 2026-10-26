"""Shared section-page URLs must not collapse distinct webhook items.

Production 2026-10-14: RSSHub ``/cls/telegraph`` emits no <link>, so Miniflux
delivered every 财联社电报 item with url ``https://www.cls.cn/telegraph``.
``link`` is the article identity downstream, so each new telegraph reused the
first item's cached score (never scored) and all evidence attached to one
event.  These tests drive the real webhook receiver and the real processor
against an isolated SQLite database with a counting fake model.
"""

import hashlib
import hmac
import json
import re
from types import SimpleNamespace

import pytest
from flask import Flask

import routes.webhook_routes as webhook_routes
from core import processor
from db import database

SECTION = "https://www.cls.cn/telegraph"
FEED = "http://127.0.0.1:1200/cls/telegraph"
STRATEGY = "shared-section:fixed-contract"
SECRET = "test-secret"


class CountingScorer:
    def __init__(self, calls):
        self.calls = calls

    def score_stage1_with_identity(self, title, *_args, **_kwargs):
        self.calls.append(title)
        return SimpleNamespace(
            values=(3.0, "电报", "与持仓无关的普通快讯", "⚖️"),
            model_name="fake-stage1",
            semantic_resolution="",
        )

    def score_stage2_with_identity(self, *_args, **_kwargs):
        pytest.fail("fixture must not reach stage 2")

    def _fallback_local_score(self, *_args, **_kwargs):
        pytest.fail("fixture must not fall back")


@pytest.fixture
def cfg(monkeypatch):
    database.init_db()
    monkeypatch.setattr(processor, "find_similar_article", lambda *_a, **_k: (None, 0))
    monkeypatch.setattr(
        processor,
        "map_article_to_assets",
        lambda _article: {
            "mapped_theme": "", "backtest_symbol": None, "mapping_confidence": 0.0,
            "security_candidates": [], "market_references": [],
        },
    )
    monkeypatch.setattr(processor, "get_mapping_version", lambda: "mapping:test")
    monkeypatch.setattr(
        processor, "send_telegram_notification",
        lambda *_a, **_k: pytest.fail("fixture must not notify"),
    )
    config = {
        "_blacklist_re": re.compile(r"$^"),
        "threshold": {"pass": 6.0, "notify": 99.0, "similarity": 0.9},
        "scoring_strategy_version": STRATEGY,
        "few_shot_feedback": {"enabled": False},
        "generate_summary": False,
        "feeds": [
            {"url": FEED, "name": "财联社电报", "pass_threshold": 6.0, "aggregation_threshold": 6.0},
        ],
    }
    monkeypatch.setattr(webhook_routes, "_cfg", lambda: config)
    monkeypatch.setattr(webhook_routes, "resolve_scoring_contract", lambda c, **_k: c)
    monkeypatch.setattr(webhook_routes, "resolve_prompt", lambda *_a: ("industry", "fixed prompt"))
    monkeypatch.setattr(webhook_routes, "_find_notify_threshold", lambda *_a: 99.0)
    monkeypatch.setenv("MINIFLUX_WEBHOOK_SECRET", SECRET)
    return config


@pytest.fixture
def deliver(cfg, monkeypatch):
    """POST a signed Miniflux payload; return the durable task it queued."""
    app = Flask(__name__)
    app.register_blueprint(webhook_routes.webhook_bp)
    queued = []
    monkeypatch.setattr(
        webhook_routes, "_submit_webhook_delivery",
        lambda task, *_a, **_k: queued.append(task) or ("stored", {"state": "accepted"}),
    )

    def _deliver(entries):
        body = json.dumps({"feed": {"feed_url": FEED}, "entries": entries}).encode()
        sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        with app.test_client() as client:
            resp = client.post(
                "/webhook", data=body,
                headers={"X-Miniflux-Signature": sig, "Content-Type": "application/json"},
            )
        assert resp.status_code in (200, 202), resp.get_data(as_text=True)
        return queued[-1]

    return _deliver


def _score(task, monkeypatch, calls):
    monkeypatch.setattr(webhook_routes, "Scorer", lambda: CountingScorer(calls))
    assert webhook_routes._score_in_background(task) is not False


def _telegraph(entry_hash, title, entry_id):
    return {"id": entry_id, "hash": entry_hash, "title": title, "url": SECTION,
            "content": f"财联社电报：{title}"}


def _cache_rows():
    with database.get_connection() as conn:
        return conn.execute(
            "SELECT link, raw_title, event_id FROM news_cache WHERE link LIKE ? ORDER BY raw_title",
            (SECTION + "%",),
        ).fetchall()


def _evidence_rows():
    with database.get_connection() as conn:
        return conn.execute(
            "SELECT source_uri, event_id, claim_text FROM event_evidence "
            "WHERE source_uri LIKE ? ORDER BY claim_text",
            (SECTION + "%",),
        ).fetchall()


def test_distinct_items_on_shared_section_url_are_scored_and_stored_separately(
    deliver, monkeypatch
):
    calls = []
    a = _telegraph("hash-a", "SK海力士预计AI基础设施规模增长20倍", 101)
    b = _telegraph("hash-b", "贝莱德减持恒瑞医药H股至5.74%", 102)
    _score(deliver([a]), monkeypatch, calls)
    _score(deliver([b]), monkeypatch, calls)

    assert len(calls) == 2  # each item reached the (fake) model exactly once
    cache = _cache_rows()
    assert len(cache) == 2
    assert len({row[0] for row in cache}) == 2
    assert all(row[0] != SECTION for row in cache)
    events = {row[2] for row in cache}
    assert len(events) == 2 and None not in events
    evidence = _evidence_rows()
    assert len({row[0] for row in evidence}) == 2
    assert len({row[1] for row in evidence}) == 2


def test_same_hash_redelivered_is_idempotent(deliver, monkeypatch):
    calls = []
    item = _telegraph("hash-same", "SK海力士预计AI基础设施规模增长20倍", 201)
    first = deliver([item])
    second = deliver([dict(item, status="read")])  # Miniflux re-sends, body differs
    assert first["entries"][0]["url"] == second["entries"][0]["url"]
    _score(first, monkeypatch, calls)
    _score(second, monkeypatch, calls)
    assert len(calls) == 1
    assert len(_cache_rows()) == 1


def test_same_item_across_section_url_variants_has_one_durable_identity(deliver, monkeypatch):
    calls = []
    item = _telegraph("hash-canonical", "国际航线运力恢复", 301)
    variants = (SECTION, "http://www.cls.cn/telegraph", SECTION + "/",
                "https://WWW.CLS.CN/telegraph/")
    tasks = [deliver([dict(item, url=url)]) for url in variants]
    for task in tasks:
        _score(task, monkeypatch, calls)

    assert len(calls) == 1
    expected = SECTION + "?ai_filter_item=h-hash-canonical"
    assert {task["entries"][0]["url"] for task in tasks} == {expected}
    cache = _cache_rows()
    evidence = _evidence_rows()
    assert len(cache) == 1 and cache[0][0] == expected
    assert len(evidence) == 1 and evidence[0][0] == expected
    assert cache[0][2] == evidence[0][1] and cache[0][2] is not None


def test_title_fallback_distinguishes_long_titles_after_display_clip(deliver, monkeypatch):
    calls = []
    prefix = "甲" * 512
    titles = (prefix + "乙公司公告", prefix + "丙公司公告")
    tasks = [deliver([{"title": title, "url": SECTION, "content": "x"}]) for title in titles]
    assert {task["entries"][0]["title"] for task in tasks} == {prefix}
    for task in tasks:
        _score(task, monkeypatch, calls)

    assert len(calls) == 2
    expected = {SECTION + "?ai_filter_item=t-" + hashlib.sha256(title.encode()).hexdigest()[:32]
                for title in titles}
    assert {task["entries"][0]["url"] for task in tasks} == expected
    cache = _cache_rows()
    evidence = _evidence_rows()
    assert len(cache) == 2 and {row[0] for row in cache} == expected
    assert len({row[2] for row in cache}) == 2
    assert {row[0] for row in evidence} == expected
    assert {row[1] for row in evidence} == {row[2] for row in cache}


def test_same_long_cleaned_title_remains_idempotent(deliver, monkeypatch):
    calls = []
    title = "甲" * 512 + "乙公司公告"
    first = deliver([{"title": f"<b>{title}</b>", "url": SECTION, "content": "x"}])
    second = deliver([{"title": title, "url": SECTION, "content": "y"}])
    _score(first, monkeypatch, calls)
    _score(second, monkeypatch, calls)
    expected = SECTION + "?ai_filter_item=t-" + hashlib.sha256(title.encode()).hexdigest()[:32]
    assert first["entries"][0]["url"] == second["entries"][0]["url"] == expected
    assert len(calls) == 1 and len(_cache_rows()) == 1 and len(_evidence_rows()) == 1


@pytest.mark.parametrize("identity,expected_key", [
    ({"hash": "hash-authoritative", "id": 401}, "h-hash-authoritative"),
    ({"id": 402}, "i-402"),
    ({"hash": "", "id": 403}, "i-403"),
])
def test_upstream_identity_dominates_changed_long_title(identity, expected_key, deliver, monkeypatch):
    calls = []
    prefix = "甲" * 512
    tasks = [deliver([dict(identity, title=prefix + suffix, url=SECTION, content="x")])
             for suffix in ("乙公司公告", "丙公司公告")]
    for task in tasks:
        _score(task, monkeypatch, calls)
    assert {task["entries"][0]["url"] for task in tasks} == {
        SECTION + "?ai_filter_item=" + expected_key
    }
    assert len(calls) == 1 and len(_cache_rows()) == 1 and len(_evidence_rows()) == 1


def test_real_article_links_are_unchanged(deliver):
    url = "https://wallstreetcn.com/livenews/3176652"
    task = deliver([{"id": 1, "hash": "h", "title": "蚂蚁灵光内测", "url": url, "content": "x"}])
    assert task["entries"][0]["url"] == url
    for other in ("https://www.cls.cn/detail/2501470", "https://www.cls.cn/telegraph/123",
                  "https://www.cls.cn/telegraph?id=9"):
        task = deliver([{"id": 2, "hash": "h", "title": "t", "url": other, "content": "x"}])
        assert task["entries"][0]["url"] == other


@pytest.mark.parametrize("url", [
    "https://www.cls.cn/telegraph?id=9",
    "https://www.cls.cn/telegraph?ai_filter_item=publisher-value",
    "https://www.cls.cn/telegraph#publisher-fragment",
    "https://www.cls.cn/telegraph/123",
    "https://www.cls.cn:8443/telegraph",
    "https://www.cls.cn:invalid/telegraph",
    "https://other.example/telegraph",
])
def test_only_explicit_section_identity_is_rewritten(url, deliver):
    task = deliver([{"id": 501, "hash": "hash-outside", "title": "t", "url": url,
                     "content": "x"}])
    assert task["entries"][0]["url"] == url


def test_section_match_is_explicit_and_configurable(cfg, deliver):
    variants = ["https://WWW.cls.cn/telegraph/", "http://www.cls.cn/telegraph"]
    for url in variants:
        link = deliver([{"id": 3, "hash": "hx", "title": "t", "url": url, "content": "x"}])
        assert link["entries"][0]["url"].endswith("?ai_filter_item=h-hx")
    cfg["webhook_shared_section_urls"] = ["https://EXAMPLE.com/flash/"]
    task = deliver([{"id": 4, "hash": "hy", "title": "t", "url": SECTION, "content": "x"}])
    assert task["entries"][0]["url"] == SECTION
    task = deliver([{"id": 5, "hash": "hz", "title": "t", "url": "https://example.com/flash",
                     "content": "x"}])
    assert task["entries"][0]["url"] == "https://example.com/flash?ai_filter_item=h-hz"
    for url in ("http://example.com/flash/", "https://EXAMPLE.COM/flash"):
        task = deliver([{"id": 5, "hash": "hz", "title": "t", "url": url, "content": "x"}])
        assert task["entries"][0]["url"] == "https://example.com/flash?ai_filter_item=h-hz"


def test_key_falls_back_to_id_then_title(deliver):
    by_id = deliver([{"id": 77, "title": "t", "url": SECTION, "content": "x"}])
    assert by_id["entries"][0]["url"] == SECTION + "?ai_filter_item=i-77"
    by_title = deliver([{"title": "电报标题", "url": SECTION, "content": "x"}])
    digest = hashlib.sha256("电报标题".encode()).hexdigest()[:32]
    assert by_title["entries"][0]["url"] == f"{SECTION}?ai_filter_item=t-{digest}"


def test_synthetic_link_survives_every_link_normalisation(deliver):
    link = deliver([_telegraph("hash-n", "t", 9)])["entries"][0]["url"]
    other = deliver([_telegraph("hash-m", "t", 10)])["entries"][0]["url"]
    # Article identity (event lookup key) drops fragments; a query survives.
    assert database._normalized_article_identity(link) != database._normalized_article_identity(SECTION)
    assert database._normalized_article_identity(link) != database._normalized_article_identity(other)
    assert database._safe_evidence_url(link)
    frag = SECTION + "#h-hash-n"
    assert database._normalized_article_identity(frag) == database._normalized_article_identity(SECTION)


def test_fix_stays_outside_the_scoring_strategy_fingerprint():
    from core import config as core_config

    base = {"threshold": {"pass": 6.0}, "feeds": []}
    with_override = dict(base, webhook_shared_section_urls=["https://example.com/flash"])
    assert core_config._strategy_fingerprint(base) == core_config._strategy_fingerprint(with_override)
    src = open(core_config.__file__, encoding="utf-8").read()
    fingerprint_body = src.split("def _strategy_code_fingerprint", 1)[1].split("def _strategy_fingerprint", 1)[0]
    assert "routes/webhook_routes.py" not in fingerprint_body
