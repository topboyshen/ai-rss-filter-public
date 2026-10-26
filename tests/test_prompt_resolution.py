"""One prompt rule for collection, webhook and cold /filter.

Every case runs the real process path against an isolated temporary SQLite
database with a FAKE model.  The fake records the system prompt it receives and
the assertions read the prompt text it saw and the ``prompt_key`` actually
written to ``news_cache``; no real model, network or production database is
involved.

``prompt_key`` is stored only in ``news_cache``.  The append-only
``backtest_signals`` ledger has no ``prompt_key`` column, so these tests never
read a prompt name from it; the ledger test below only checks that old rows are
left untouched when the strategy version changes.

"Default prompt" here means: the configuration's own ``prompts.default`` takes
priority when present, otherwise the built-in ``default`` text.  A missing,
blank, non-string or unknown prompt name is stored as ``default`` and is served
that same body (the ``cfg-default`` / ``builtin-default`` cases below cover both
sides).  The explanatory docstrings in ``core/prompts.py`` that call this
"the built-in default" are left as they are on purpose: that file is part of the
strategy fingerprint, so even a comment-only edit there would start a new
scoring contract and rescore old links.
"""

import json
from email.utils import formatdate

import pytest
from flask import Flask

import core.processor as processor
import routes.feed_routes as feed_routes
import routes.webhook_routes as webhook_routes
import services.refresh as refresh
from core.prompts import (
    BUILTIN_PROMPTS,
    prompt_name_for_body,
    resolve_prompt,
)
from core.scorer import LLMBackend, Scorer
from db import database

_BODY = {name: f"<<{name} body>>" for name in ("default", "macro", "industry", "tech")}
_MATCHERS = [
    {"key": "macro", "pattern": "wallstreetcn|cls[/.-]"},
    {"key": "tech", "pattern": "(?<![a-z0-9])ai(?![a-z0-9])"},
    {"key": "ghost", "pattern": "ghostmatcher"},  # key names no prompt
]
_BASE = "http://rsshub:1200"
# (case id, configured feed fields, expected stored key)
_CONFIGURED = [
    ("own-prompt-beats-url-matcher", {"url": f"{_BASE}/wallstreetcn/live", "prompt": "industry"}, "industry"),
    ("own-prompt-without-matcher", {"url": f"{_BASE}/gov/csrc/news", "prompt": "macro"}, "macro"),
    ("missing-prompt-field-ignores-url-matcher", {"url": f"{_BASE}/cls/telegraph/watch"}, "default"),
    ("blank-prompt", {"url": f"{_BASE}/cls/blank", "prompt": ""}, "default"),
    ("unknown-prompt-name", {"url": f"{_BASE}/other/unknown", "prompt": "no-such-prompt"}, "default"),
    ("non-string-prompt", {"url": f"{_BASE}/other/number", "prompt": 7}, "default"),
]
_UNCONFIGURED = [
    ("matcher-macro", f"{_BASE}/cls/depth", "macro"),
    ("matcher-tech", "https://example.test/ai/news", "tech"),
    ("no-matcher", "https://example.test/plain", "default"),
    ("matcher-with-unknown-key", "https://ghostmatcher.test/x", "default"),
]


def _cfg(*, default_body=True, feeds=()):
    prompts = dict(_BODY)
    if not default_body:
        prompts.pop("default")
    return {
        "scoring_strategy_version": "prompt-resolution-test",
        "threshold": {"pass": 3, "similarity": 0.6, "notify": 99},
        "_blacklist_re": None,
        "cache_rejected_entries": True,
        "generate_summary": False,
        "similarity_window_hours": 72,
        "local_reject_rules": {"enabled": False},
        "fast_pass": {"enabled": False},
        "two_stage_scoring": {"enabled": False},
        "cache_ttl": 60,
        "output": {"max_items": 30},
        "prompts": prompts,
        "prompt_matchers": [dict(item) for item in _MATCHERS],
        "feeds": [
            {"name": f"f{i}", "enabled": True, **feed} for i, feed in enumerate(feeds)
        ],
    }


def _expected_body(cfg, key):
    return cfg["prompts"].get(key) or BUILTIN_PROMPTS[key]


@pytest.fixture
def model(monkeypatch):
    """FAKE model behind the real Scorer; records every system prompt it gets."""
    database.init_db()
    seen = []
    backend = LLMBackend("fixture", "", "https://invalid.test", "fixture-model")
    monkeypatch.setattr(backend, "client", object())
    monkeypatch.setattr(Scorer, "backends_for", lambda self, *a, **kw: [backend])

    def fake(self, _backend, messages, *_args, **_kwargs):
        title = messages[1]["content"].split("标题：", 1)[1].splitlines()[0]
        seen.append({"title": title, "system": messages[0]["content"]})
        return json.dumps(
            {"score": 8, "tag": "公司公告", "logic": "甲公司已建设工厂", "sentiment": "看多"},
            ensure_ascii=False,
        )

    monkeypatch.setattr(Scorer, "_call_structured_llm", fake)
    monkeypatch.setattr(Scorer, "summarize", lambda *_a, **_k: "")
    Scorer().clear_runtime_cache()
    return seen


def _title(tag):
    return f"甲公司已建设工厂{tag}"


def _rss(url, tag):
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        f"<item><title>{_title(tag)}</title><link>https://invalid.test/{tag}</link>"
        f"<description>{_title(tag)}，公司公告披露新增产能与订单细节。</description>"
        f"<pubDate>{formatdate(usegmt=True)}</pubDate></item></channel></rss>"
    ).encode()


@pytest.fixture
def feed_bytes(monkeypatch):
    """Serve one article per URL; the article title carries a per-URL tag."""
    tags = {}

    def load(url, *_args, **_kwargs):
        return _rss(url, tags.setdefault(url, f"x{len(tags)}"))

    monkeypatch.setattr(processor, "_load_feed_bytes", load)
    return tags


def _stored_keys(link_tag):
    link = f"https://invalid.test/{link_tag}"
    with database.get_connection() as conn:
        cached = conn.execute(
            "SELECT prompt_key FROM news_cache WHERE link = ?", (link,)
        ).fetchall()
    return [r[0] for r in cached]


def _assert_scored_with(model, tag, cfg, expected_key):
    calls = [c for c in model if c["title"] == _title(tag)]
    assert len(calls) == 1, calls
    body = _expected_body(cfg, expected_key)
    assert calls[0]["system"].startswith(body)
    # The model never saw a different configured prompt's text.
    for other, text in {**BUILTIN_PROMPTS, **cfg["prompts"]}.items():
        if text != body and text not in BUILTIN_PROMPTS.values():
            assert text not in calls[0]["system"], other
    # ``prompt_key`` lives only in the mutable ``news_cache`` read model.
    assert _stored_keys(tag) == [expected_key]


def _run_webhook(cfg, url, tag):
    task = {
        "feed_url": url,
        "entries": [{"title": _title(tag), "url": f"https://invalid.test/{tag}",
                     "content": f"{_title(tag)}，公司公告披露新增产能与订单细节。"}],
    }
    return webhook_routes._score_in_background(task)


@pytest.mark.parametrize("default_body", [True, False], ids=["cfg-default", "builtin-default"])
@pytest.mark.parametrize("case,feed,expected", _CONFIGURED, ids=[c[0] for c in _CONFIGURED])
def test_webhook_configured_feed(monkeypatch, model, default_body, case, feed, expected):
    cfg = _cfg(default_body=default_body, feeds=[feed])
    monkeypatch.setattr(webhook_routes, "_cfg", lambda: cfg)
    assert _run_webhook(cfg, feed["url"], "w1") is True
    _assert_scored_with(model, "w1", cfg, expected)


@pytest.mark.parametrize("case,url,expected", _UNCONFIGURED, ids=[c[0] for c in _UNCONFIGURED])
def test_webhook_unconfigured_feed(monkeypatch, model, case, url, expected):
    cfg = _cfg(feeds=[_CONFIGURED[0][1]])
    monkeypatch.setattr(webhook_routes, "_cfg", lambda: cfg)
    assert _run_webhook(cfg, url, "w2") is True
    _assert_scored_with(model, "w2", cfg, expected)


def test_webhook_yaml_host_alias_uses_the_configured_prompt(monkeypatch, model):
    feed = {"url": f"{_BASE}/wallstreetcn/live", "prompt": "industry"}
    cfg = _cfg(feeds=[feed])
    cfg["_runtime_feed_url_aliases"] = {feed["url"]: ["http://127.0.0.1:1200/wallstreetcn/live"]}
    monkeypatch.setattr(webhook_routes, "_cfg", lambda: cfg)
    assert _run_webhook(cfg, "http://127.0.0.1:1200/wallstreetcn/live", "w3") is True
    _assert_scored_with(model, "w3", cfg, "industry")


@pytest.mark.parametrize("case,feed,expected", _CONFIGURED, ids=[c[0] for c in _CONFIGURED])
def test_collection_configured_feed(monkeypatch, model, feed_bytes, case, feed, expected):
    cfg = _cfg(feeds=[feed])
    monkeypatch.setattr(refresh, "_cfg", lambda: cfg)
    assert refresh._execute_feed_refresh()["status"] == "success"
    _assert_scored_with(model, feed_bytes[feed["url"]], cfg, expected)


@pytest.mark.parametrize("case,feed,expected", _CONFIGURED, ids=[c[0] for c in _CONFIGURED])
def test_cold_filter_configured_feed(monkeypatch, model, feed_bytes, case, feed, expected):
    cfg = _cfg(feeds=[feed])
    client = _filter_client(monkeypatch, cfg)
    assert client.get("/filter", query_string={"url": feed["url"]}).status_code == 200
    _assert_scored_with(model, feed_bytes[feed["url"]], cfg, expected)


@pytest.mark.parametrize("case,url,expected", _UNCONFIGURED, ids=[c[0] for c in _UNCONFIGURED])
def test_cold_filter_unconfigured_feed(monkeypatch, model, feed_bytes, case, url, expected):
    cfg = _cfg(feeds=[_CONFIGURED[0][1]])
    client = _filter_client(monkeypatch, cfg)
    assert client.get("/filter", query_string={"url": url}).status_code == 200
    _assert_scored_with(model, feed_bytes[url], cfg, expected)


def test_cold_filter_multi_resolves_every_source_independently(monkeypatch, model, feed_bytes):
    configured = [c[1] for c in _CONFIGURED[:5]]
    cfg = _cfg(feeds=configured)
    client = _filter_client(monkeypatch, cfg)
    urls = [f["url"] for f in configured] + [u[1] for u in _UNCONFIGURED]
    response = client.get("/filter/multi", query_string={"urls": ",".join(urls)})
    assert response.status_code == 200
    expected = [c[2] for c in _CONFIGURED[:5]] + [u[2] for u in _UNCONFIGURED]
    for url, key in zip(urls, expected):
        _assert_scored_with(model, feed_bytes[url], cfg, key)


def _filter_client(monkeypatch, cfg):
    monkeypatch.setattr(feed_routes, "check_api_key", lambda: True)
    monkeypatch.setattr(feed_routes, "validate_target_url", lambda *_a: (True, ""))
    monkeypatch.setattr(feed_routes, "_cfg", lambda: cfg)
    monkeypatch.setattr(feed_routes, "get_cached", lambda *_a: None)
    monkeypatch.setattr(feed_routes, "set_cached", lambda *_a: None)
    monkeypatch.setattr(feed_routes.DB, "get_entries_for_cached_rss", lambda *_a, **_k: [])
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(feed_routes.feed_bp)
    return app.test_client()


# ── the shared rule itself ───────────────────────────────────────────────


@pytest.mark.parametrize("case,feed,expected", _CONFIGURED, ids=[c[0] for c in _CONFIGURED])
def test_resolve_prompt_returns_the_name_of_the_body_it_returns(case, feed, expected):
    cfg = _cfg(feeds=[feed])
    name, body = resolve_prompt(feed["url"], cfg)
    assert name == expected
    assert body == _expected_body(cfg, name)


@pytest.mark.parametrize("case,url,expected", _UNCONFIGURED, ids=[c[0] for c in _UNCONFIGURED])
def test_resolve_prompt_unconfigured(case, url, expected):
    cfg = _cfg()
    assert resolve_prompt(url, cfg) == (expected, _expected_body(cfg, expected))
    assert resolve_prompt("", cfg)[0] == "default"


def test_prompt_name_for_body_never_claims_the_feed_prompt_for_other_text():
    feed = {"url": f"{_BASE}/gov/csrc/news", "prompt": "macro"}
    cfg = _cfg(feeds=[feed])
    assert prompt_name_for_body(_BODY["macro"], feed["url"], cfg) == "macro"
    # Another configured prompt's text is reported under its own name.
    assert prompt_name_for_body(_BODY["tech"], feed["url"], cfg) == "tech"
    assert prompt_name_for_body(BUILTIN_PROMPTS["macro"], feed["url"], {"prompts": {}}) == "macro"
    # Text that is no configured prompt is not recorded as the feed's prompt.
    assert prompt_name_for_body("hand written", feed["url"], cfg) == "custom"
    assert prompt_name_for_body("", feed["url"], cfg) == "custom"


def test_caller_supplied_foreign_text_is_stored_as_what_was_sent(monkeypatch, model):
    feed = {"url": f"{_BASE}/gov/csrc/news", "prompt": "macro"}
    cfg = _cfg(feeds=[feed])
    cfg = webhook_routes.resolve_scoring_contract(cfg, register_snapshot=True)
    entry = type("E", (), {"title": _title("c1"), "link": "https://invalid.test/c1",
                           "summary": f"{_title('c1')}，公司公告披露新增产能与订单细节。",
                           "get": lambda self, k, d=None: getattr(self, k, d)})()
    processor.process_entry(entry, [], "free text prompt", cfg, feed_url=feed["url"])
    assert model[0]["system"].startswith("free text prompt")
    assert _stored_keys("c1") == ["custom"]


# ── strategy contract: a new strategy version rescored, old ledger rows kept ──


def test_new_strategy_version_rescores_and_leaves_old_ledger_rows_untouched(
    monkeypatch, model
):
    feed = {"url": f"{_BASE}/wallstreetcn/live", "prompt": "industry"}
    old = _cfg(feeds=[feed])
    old["scoring_strategy_version"] = "contract-old"
    new = _cfg(feeds=[feed])
    new["scoring_strategy_version"] = "contract-new"
    link = "https://invalid.test/s1"

    def ledger(version):
        with database.get_connection() as conn:
            conn.row_factory = None
            return conn.execute(
                "SELECT * FROM backtest_signals WHERE link = ? AND strategy_version = ?",
                (link, version),
            ).fetchall()

    monkeypatch.setattr(webhook_routes, "_cfg", lambda: old)
    assert _run_webhook(old, feed["url"], "s1") is True
    old_rows = ledger("contract-old")
    assert len(old_rows) == 1 and len(model) == 1
    # Same contract again: reused, no second model call, ledger unchanged.
    assert _run_webhook(old, feed["url"], "s1") is True
    assert len(model) == 1 and ledger("contract-old") == old_rows

    monkeypatch.setattr(webhook_routes, "_cfg", lambda: new)
    assert _run_webhook(new, feed["url"], "s1") is True
    # New contract: the link is scored again (one more model call)...
    assert len(model) == 2
    with database.get_connection() as conn:
        version = conn.execute(
            "SELECT strategy_version FROM news_cache WHERE link = ?", (link,)
        ).fetchone()[0]
    assert version == "contract-new"
    # ...as a new ledger row; the old row is not rebound or rewritten.
    assert ledger("contract-old") == old_rows
    assert len(ledger("contract-new")) == 1


def test_process_feed_without_explicit_prompt_uses_the_shared_rule(
    monkeypatch, model, feed_bytes
):
    feed = {"url": f"{_BASE}/wallstreetcn/live", "prompt": "industry"}
    cfg = _cfg(feeds=[feed])
    processor.process_feed(feed["url"], "", cfg=cfg, check_output_cache=False)
    _assert_scored_with(model, feed_bytes[feed["url"]], cfg, "industry")


def test_cold_filter_on_yaml_host_alias_uses_the_configured_prompt(
    monkeypatch, model, feed_bytes
):
    feed = {"url": f"{_BASE}/wallstreetcn/live", "prompt": "industry"}
    alias = "http://127.0.0.1:1200/wallstreetcn/live"
    cfg = _cfg(feeds=[feed])
    cfg["_runtime_feed_url_aliases"] = {feed["url"]: [alias]}
    client = _filter_client(monkeypatch, cfg)
    assert client.get("/filter", query_string={"url": alias}).status_code == 200
    _assert_scored_with(model, feed_bytes[alias], cfg, "industry")
