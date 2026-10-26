"""Identity is an extra reuse veto, not a mapping or a wider scoring rule."""

import json
import re
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from feedparser import FeedParserDict

from core import processor
from core.scorer import LLMBackend, Scorer
from db import database
from db.event_core import upsert_security

OLD = "中国中车董事长因个人原因辞职"
NEW = "中国中铁董事长因个人原因辞职"
OLD_LOGIC = "董事长辞职增加治理不确定性"


@pytest.fixture
def live_path(monkeypatch):
    # A fresh singleton prevents unrelated scorer tests' instance stubs from
    # shadowing this fixture's class-level fake transport (and is restored).
    monkeypatch.setattr(Scorer, "_instance", None)
    database.init_db()
    with database.get_connection() as conn:
        for ticker, name, aliases in (
            ("601766", "中国中车", ["中车集团"]),
            ("601390", "中国中铁", []),
            ("601988", "中国银行", []),
        ):
            upsert_security(conn, exchange="SSE", ticker=ticker, name=name,
                            aliases=aliases, security_id=f"SSE:{ticker}", now=100)
        conn.commit()
    cfg = {
        "scoring_strategy_version": "issuer-reuse-guard-test",
        "threshold": {"pass": 5, "notify": 8, "similarity": 0.6},
        "_blacklist_re": re.compile(r"$^"), "cache_rejected_entries": True,
        "generate_summary": False, "similarity_window_hours": 72,
        "local_reject_rules": {"enabled": False}, "fast_pass": {"enabled": False},
        "two_stage_scoring": {"enabled": False}, "llm_score_cache": {"enabled": False},
        "prompts": {"industry": "仅基于原文判断阅读价值"},
        "feeds": [{"url": "https://fixture.test/feed", "prompt": "industry", "tier": "primary_news"}],
    }
    calls, notices = [], []
    backend = LLMBackend("fixture", "", "https://invalid.test", "fixture-model")
    monkeypatch.setattr(backend, "client", object())
    monkeypatch.setattr(Scorer, "backends_for", lambda self, *a, **kw: [backend])

    def fake(self, _backend, messages, *_args, **kwargs):
        title = messages[1]["content"].split("标题：", 1)[1].splitlines()[0]
        calls.append((title, kwargs.get("purpose")))
        return json.dumps({"score": 9 if title == OLD else 6, "tag": "公司治理",
            "logic": OLD_LOGIC if title == OLD else "人事变更需跟踪后续影响",
            "sentiment": "看空"}, ensure_ascii=False)

    monkeypatch.setattr(Scorer, "_call_structured_llm", fake)
    monkeypatch.setattr(processor, "send_telegram_notification", lambda *a: notices.append(a))
    Scorer().clear_runtime_cache()
    return SimpleNamespace(cfg=cfg, calls=calls, notices=notices)


def run_entry(state, title, link, recent=(), **kwargs):
    entry = FeedParserDict(title=title, link=link, summary="")
    result = processor.process_entry(entry, list(recent), state.cfg["prompts"]["industry"],
        state.cfg, scorer=Scorer(), feed_url="https://fixture.test/feed", **kwargs)
    return entry, result


def test_different_issuer_real_process_scores_and_persists_its_own_result(live_path):
    state = live_path
    run_entry(state, OLD, "https://fixture.test/old")
    recent = database.get_recent_entries(cfg=state.cfg)
    old_notices = len(state.notices)
    entry, (output, reason) = run_entry(state, NEW, "https://fixture.test/new", recent)
    assert reason == "scored" and output is entry
    assert [item[0] for item in state.calls] == [OLD, NEW]
    with database.get_read_only_connection() as conn:
        cached = conn.execute("SELECT score,logic,score_path FROM news_cache WHERE link=?",
                              (entry.link,)).fetchone()
        signals = conn.execute("SELECT score,score_path,is_similarity_copy FROM backtest_signals "
                               "WHERE link=? AND signal_stage='base'", (entry.link,)).fetchall()
    assert cached[0] == 6 and OLD_LOGIC not in cached[1] and cached[2] == "llm"
    assert [tuple(row) for row in signals] == [(6, "llm", 0)]
    assert len(state.notices) == old_notices


@pytest.mark.parametrize("title", [OLD, "财联社：中国中车董事长因个人原因辞职"])
def test_same_title_source_reprint_still_reuses_without_model(live_path, title):
    state = live_path
    run_entry(state, OLD, "https://fixture.test/old")
    recent = database.get_recent_entries(cfg=state.cfg)
    _, result = run_entry(state, title, "https://fixture.test/reprint", recent, notify_threshold=99)
    assert result == (None, "similarity")
    assert len(state.calls) == 1


def _rows():
    return [
        ("SSE:601766", "中国中车", "601766", "SSE", '["中车集团"]'),
        ("SSE:601390", "中国中铁", "601390", "SSE", "[]"),
        ("SSE:601988", "中国银行", "601988", "SSE", "[]"),
        ("SSE:600001", "甲公司", "600001", "SSE", "[]"),
        ("SSE:600002", "乙公司", "600002", "SSE", "[]"),
    ]


@pytest.mark.parametrize("old,new,reuse", [
    (OLD, NEW, False),
    (OLD, "中国中车董事长辞职", True),
    (OLD, "中国中车集团董事长因个人原因辞职", False),
    ("中国银行董事长辞职", "中国银行业协会会长辞职", False),
    ("甲公司与乙公司达成合作", "乙公司与甲公司达成合作", False),
    ("甲公司与乙公司达成合作", "甲公司与乙公司深化合作", False),
    ("央行发布货币政策公告", "央行发布货币政策公告。", True),
    ("央行流动性安排", "央行流动性安排进展", False),
    ("未知主体董事长辞职", "另个主体董事长辞职", False),
    ("媒体发布星海科技董事长辞职消息", "媒体发布远山科技董事长辞职消息", False),
    ("新华社发布甲公司董事长辞职消息", "新华社发布乙公司董事长辞职消息", False),
    ("甲公司取得乙公司订单", "甲公司获得乙公司订单", True),
    ("中国中车签订合同", "中车集团签订合同", False),
    ("SH601766董事长辞职", "SH601390董事长辞职", False),
])
def test_identity_veto_never_bypasses_old_facts(monkeypatch, old, new, reuse):
    monkeypatch.setattr(processor.DB, "get_similarity_identity_rows", lambda: _rows())
    ctx = SimpleNamespace(raw_title=new, source_summary="", feed_url="",
        cfg={"threshold": {"similarity": 0.1}}, entry=SimpleNamespace(link="new-link"))
    row = ("old-link", old, old, 9.0, "公司公告", "相关事项值得跟踪", "中性")
    persist = Mock(return_value=(None, "similarity"))
    monkeypatch.setattr(processor, "_persist_similar_entry", persist)
    info, terminal = processor._process_entry_similarity(ctx, [row], 99)
    assert info is not None and info["score"] == 9
    assert (terminal is not None) is reuse
    assert persist.call_count == int(reuse)


@pytest.mark.parametrize("rows", [None, []])
def test_missing_master_rescores_nonidentical_titles(live_path, monkeypatch, rows):
    state = live_path
    run_entry(state, OLD, "https://fixture.test/old")
    monkeypatch.setattr(processor.DB, "get_similarity_identity_rows", lambda: rows)
    _, (_output, reason) = run_entry(state, "中国中车董事长辞职", "https://fixture.test/new",
                                   database.get_recent_entries(cfg=state.cfg))
    assert reason == "scored" and len(state.calls) == 2


def test_many_candidates_share_one_lazy_master_snapshot(monkeypatch):
    load = Mock(return_value=_rows())
    monkeypatch.setattr(processor.DB, "get_similarity_identity_rows", load)
    snapshot = processor._SimilarityIdentitySnapshot()
    assert snapshot.compare(OLD, OLD) is not None
    load.assert_not_called()
    for _ in range(30):
        assert snapshot.compare(OLD, NEW) is None
        assert snapshot.compare(OLD, "中国中车董事长辞职") is not None
    load.assert_called_once_with()


def test_same_issuer_paraphrase_real_process_still_reuses(live_path):
    state = live_path
    run_entry(state, OLD, "https://fixture.test/old")
    _, result = run_entry(state, "中国中车董事长辞职", "https://fixture.test/reprint",
                          database.get_recent_entries(cfg=state.cfg), notify_threshold=99)
    assert result == (None, "similarity")
    assert len(state.calls) == 1


def test_read_only_master_failure_returns_unknown(monkeypatch):
    def unavailable():
        raise sqlite3.OperationalError("fixture read-only master unavailable")
    monkeypatch.setattr(database, "get_read_only_connection", unavailable)
    assert database.get_similarity_identity_rows() is None


@pytest.mark.parametrize("rows,old,new", [
    (_rows() + [("SSE:600010", "另一公司", "600010", "SSE", '["中国中车"]')],
     OLD, "中国中车董事长辞职"),
    (_rows(), "证券代码601766董事长辞职", "证券代码601390董事长辞职"),
    (_rows(), "SH601766董事长辞职", "SZ601766董事长辞职"),
    (_rows(), "中国中车(601766)董事长辞职", "中国中车(601390)董事长辞职"),
])
def test_ambiguous_or_changed_security_identity_never_reuses(monkeypatch, rows, old, new):
    monkeypatch.setattr(processor.DB, "get_similarity_identity_rows", lambda: rows)
    snapshot = processor._SimilarityIdentitySnapshot()
    from core.score_evidence import event_reuse_conflicts
    assert snapshot.compare(new, old) is None or event_reuse_conflicts(new, old)


def test_identity_comparison_never_truncates_the_company_list(monkeypatch):
    names = [f"公司{chr(65 + i)}" for i in range(26)]
    rows = [(f"SSE:{600000 + i}", name, str(600000 + i), "SSE", "[]")
            for i, name in enumerate(names)]
    monkeypatch.setattr(processor.DB, "get_similarity_identity_rows", lambda: rows)
    snapshot = processor._SimilarityIdentitySnapshot()
    old = "，".join(names[:25]) + "董事长辞职"
    new = "，".join([*names[:24], names[25]]) + "董事长辞职"
    assert snapshot.compare(old, new) is None
    assert snapshot.compare(old, old) is not None


def test_actual_feed_shares_master_snapshot_across_entries(live_path, monkeypatch):
    state = live_path
    run_entry(state, OLD, "https://fixture.test/old")
    state.cfg["max_entries"] = 10
    entries = [FeedParserDict(title=NEW, link=f"https://fixture.test/new-{i}", summary="")
               for i in range(4)]
    monkeypatch.setattr(processor, "_load_feed_bytes", lambda *a: b"fixture-rss")
    monkeypatch.setattr(processor.feedparser, "parse", lambda _raw: FeedParserDict(entries=entries, feed={}))
    original = database.get_similarity_identity_rows
    load = Mock(side_effect=original)
    monkeypatch.setattr(processor.DB, "get_similarity_identity_rows", load)
    passed, stats, error, _feed = processor.process_feed(
        "https://fixture.test/feed", cfg=state.cfg, check_output_cache=False)
    assert not error and len(passed) == 1  # Final same-title output dedup is unchanged.
    assert stats["entry_errors"] == 0
    assert len(state.calls) == 5  # Each independent link was actually rescored.
    load.assert_called_once_with()
