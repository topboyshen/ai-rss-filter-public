"""Concurrent scoring must share only identical, same-generation decisions.

Only the transport is fake. These tests exercise the real scorer, processor,
RSS loop, continuation journal and cache; no network or production data is used.
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from core import processor
from core import scorer as scorer_module
from core.scorer import ScoringResult
from db import database
from tests.test_natural_score_continuation import (  # noqa: F401
    FEED,
    LINK,
    PROMPT,
    SUMMARY,
    TITLE,
    _make_due,
    _record,
    pipeline as pipeline,
)


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _flight_waiters(scorer):
    return sum(
        getattr(flight, "waiters", 0)
        for flight in getattr(scorer, "_score_flights", {}).values()
    )


def _score(scorer, cfg, title=TITLE):
    return scorer.score_stage1_with_identity(title, SUMMARY, PROMPT, cfg)


def _response(score):
    return json.dumps({
        "score": score,
        "tag": "demand",
        "logic": "company reports stronger demand",
        "sentiment": "中性",
    })


def test_different_links_same_identity_share_transport_and_persist_separately(pipeline):
    p = pipeline
    p.control["mode"] = "recovered"
    entered, release = threading.Event(), threading.Event()
    p.control["pause"] = (entered, release)
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(p.run, LINK + "/owner")
        assert entered.wait(2)
        waiter = pool.submit(p.run, LINK + "/waiter")
        assert _wait_for(lambda: len(p.calls) >= 2 or _flight_waiters(p.scorer) == 1)
        release.set()
        assert owner.result(timeout=3)[1] == "scored"
        assert waiter.result(timeout=3)[1] == "scored"
    p.control["pause"] = None
    assert len(p.calls) == 1
    assert p.run(LINK + "/third")[1] == "scored"
    assert len(p.calls) == 1, "the sequential third arrival must reuse the score cache"
    for suffix in ("owner", "waiter", "third"):
        with database.get_connection() as connection:
            row = connection.execute(
                "SELECT score FROM backtest_signals WHERE link=?", (LINK + "/" + suffix,),
            ).fetchone()
        assert row is not None and row[0] == 8.0
    assert getattr(p.scorer, "_score_flights", {}) == {}


def test_registration_rechecks_cache_before_becoming_owner(pipeline, monkeypatch):
    p = pipeline
    p.control["mode"] = "recovered"
    original = p.scorer._cached_grounded_score
    entered, release = threading.Event(), threading.Event()
    calls = 0

    def pause_first_check(*args):
        nonlocal calls
        calls += 1
        result = original(*args)
        if calls == 1:
            entered.set()
            assert release.wait(2)
        return result

    monkeypatch.setattr(p.scorer, "_cached_grounded_score", pause_first_check)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(_score, p.scorer, p.cfg)
        assert entered.wait(2)
        key = p.scorer._cache_key(
            TITLE, SUMMARY, p.cfg, stage="stage1", prompt_text=PROMPT,
            routed_backends=p.scorer.backends_for("stage1", cfg=p.cfg),
        )
        p.scorer._set_cached_score(key, ScoringResult(
            (8.0, "demand", "company reports stronger demand", "中性"),
            "completed-old-owner",
        ))
        release.set()
        result = pending.result(timeout=3)
    assert result.model_name == "completed-old-owner"
    assert p.calls == []
    assert calls == 2


def test_owner_unexpected_exception_releases_waiter_without_duplicate(pipeline, monkeypatch):
    p = pipeline
    entered, release = threading.Event(), threading.Event()
    calls = []

    def fail(*_args, **_kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(2)
        raise KeyboardInterrupt("synthetic owner failure")

    monkeypatch.setattr(p.scorer, "_call_structured_llm", fail)

    def invoke():
        try:
            _score(p.scorer, p.cfg)
            return "result"
        except BaseException as exc:
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(invoke)
        assert entered.wait(2)
        waiter = pool.submit(invoke)
        assert _wait_for(lambda: len(calls) >= 2 or _flight_waiters(p.scorer) == 1)
        release.set()
        outcomes = [owner.result(timeout=3), waiter.result(timeout=3)]
    assert sorted(outcomes) == ["KeyboardInterrupt", "ScoreFlightRetryable"]
    assert len(calls) == 1
    assert p.scorer._score_flights == {}


def test_different_identities_and_stages_keep_independent_calls(pipeline):
    p = pipeline
    p.control["mode"] = "recovered"
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(
            lambda title: _score(p.scorer, p.cfg, title),
            [TITLE + " A", TITLE + " B"],
        ))
        stage1 = pool.submit(_score, p.scorer, p.cfg, TITLE + " review")
        stage2 = pool.submit(
            p.scorer.score_stage2_with_identity,
            TITLE + " review", SUMMARY, PROMPT, p.cfg,
        )
        assert stage1.result(timeout=3).values[0] == 8.0
        assert stage2.result(timeout=3).values[0] == 8.0
    assert len(p.calls) == 4


def test_clear_generation_isolates_old_owner_without_cache_overwrite(
    pipeline, monkeypatch,
):
    p = pipeline
    entered, release = threading.Event(), threading.Event()
    calls = []

    def transport(*_args, **_kwargs):
        calls.append(1)
        number = len(calls)
        if number == 1:
            entered.set()
            assert release.wait(3)
        return _response(8.0 if number == 1 else 9.0)

    monkeypatch.setattr(p.scorer, "_call_llm", transport)
    with ThreadPoolExecutor(max_workers=3) as pool:
        old_owner = pool.submit(_score, p.scorer, p.cfg)
        assert entered.wait(2)
        old_waiter = pool.submit(_score, p.scorer, p.cfg)
        assert _wait_for(lambda: len(calls) >= 2 or _flight_waiters(p.scorer) == 1)
        p.scorer.clear_runtime_cache()
        new_owner = pool.submit(_score, p.scorer, p.cfg)
        assert new_owner.result(timeout=3).values[0] == 9.0
        release.set()
        assert old_owner.result(timeout=3).values[0] == 8.0
        assert old_waiter.result(timeout=3).values[0] == 8.0
    assert _score(p.scorer, p.cfg).values[0] == 9.0
    assert len(calls) == 2
    assert p.scorer._score_flights == {}


def test_coordination_deadline_is_shared_across_link_and_score_wait(
    pipeline, monkeypatch,
):
    p = pipeline
    p.control["mode"] = "recovered"
    entered, release = threading.Event(), threading.Event()
    p.control["pause"] = (entered, release)
    waiter_link = LINK + "/deadline-waiter"
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(p.run, LINK + "/deadline-owner")
        assert entered.wait(2)
        with processor._entry_link_single_flight(waiter_link):
            started = time.monotonic()
            waiter = pool.submit(
                processor.process_entry,
                processor.feedparser.FeedParserDict(
                    title=TITLE, link=waiter_link, summary=SUMMARY,
                ),
                [], PROMPT, p.cfg,
                scorer=p.scorer, feed_url=FEED,
                coordination_deadline=started + 0.2,
            )
            time.sleep(0.08)
        outcome = waiter.result(timeout=1)
        elapsed = time.monotonic() - started
        release.set()
        assert owner.result(timeout=3)[1] == "scored"
    assert outcome == (None, processor.SCORE_DEFERRED_REASON)
    assert elapsed < 0.35, "the stage wait must use the remaining shared budget"
    assert len(p.calls) == 1


def test_natural_flight_deferral_reserves_once_per_round_then_reuses(
    pipeline, monkeypatch,
):
    p = pipeline
    p.control["mode"] = "recovered"
    monkeypatch.setattr(processor, "_ENTRY_LINK_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(scorer_module, "_COORDINATION_WAIT_SECONDS", 0.05)
    entered, release = threading.Event(), threading.Event()
    p.control["pause"] = (entered, release)
    waiter_link = LINK + "/natural-waiter"
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(p.run, LINK + "/natural-owner")
        assert entered.wait(2)
        waiter = pool.submit(p.run, waiter_link)
        try:
            assert waiter.result(timeout=1) == (None, processor.SCORE_DEFERRED_REASON)
        except BaseException:
            release.set()
            waiter.result(timeout=3)
            raise
        assert _record(p.cfg, waiter_link)["attempts"] == 1
        _make_due(p.cfg, waiter_link)
        assert p.resume()["deferred"] == 1
        assert _record(p.cfg, waiter_link)["attempts"] == 2
        release.set()
        assert owner.result(timeout=3)[1] == "scored"
    p.control["pause"] = None
    _make_due(p.cfg, waiter_link)
    assert p.resume()["completed"] == 1
    assert _record(p.cfg, waiter_link)["state"] == "completed"
    assert _record(p.cfg, waiter_link)["attempts"] == 2
    assert len(p.calls) == 1


def test_natural_link_wait_before_score_captures_zero_attempts(
    pipeline, monkeypatch,
):
    p = pipeline
    p.control["mode"] = "recovered"
    monkeypatch.setattr(processor, "_ENTRY_LINK_WAIT_SECONDS", 0.05)
    link = LINK + "/natural-link-wait"
    held, release = threading.Event(), threading.Event()

    def hold():
        with processor._entry_link_single_flight(link):
            held.set()
            assert release.wait(2)

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold)
        assert held.wait(2)
        outcome = p.run(link)
        release.set()
        holder.result(timeout=3)
    assert outcome == (None, processor.SCORE_DEFERRED_REASON)
    assert _record(p.cfg, link)["attempts"] == 0
    assert p.calls == []


def test_rss_feeds_share_identity_and_report_owner_waiter_call_counts(
    pipeline, monkeypatch,
):
    p = pipeline
    p.control["mode"] = "recovered"
    cfg = dict(p.cfg, cache_ttl=3600, rss_timeout=1, rss_max_fetch_attempts=1)
    monkeypatch.setattr(processor, "Scorer", lambda: p.scorer)
    monkeypatch.setattr(
        processor, "resolve_scoring_contract", lambda value, **_kwargs: value,
    )
    fake_transport = p.scorer._call_llm

    def counted_transport(*args, **kwargs):
        scorer_module._record_llm_attempt(kwargs.get("purpose", "stage1"))
        return fake_transport(*args, **kwargs)

    monkeypatch.setattr(p.scorer, "_call_llm", counted_transport)
    feeds = {
        FEED + "/one": LINK + "/rss-one",
        FEED + "/two": LINK + "/rss-two",
        FEED + "/three": LINK + "/rss-three",
    }

    def rss(url, *_args, **_kwargs):
        link = feeds[url]
        return (
            "<rss version='2.0'><channel><title>fixture</title><item>"
            f"<title>{TITLE}</title><link>{link}</link>"
            f"<description>{SUMMARY}</description></item></channel></rss>"
        ).encode()

    monkeypatch.setattr(processor, "_load_feed_bytes", rss)
    entered, release = threading.Event(), threading.Event()
    p.control["pause"] = (entered, release)

    def run(url):
        return processor.process_feed(
            url, prompt_text=PROMPT, cfg=cfg, persist_deferred=True,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(run, FEED + "/one")
        assert entered.wait(2)
        waiter = pool.submit(run, FEED + "/two")
        assert _wait_for(lambda: len(p.calls) >= 2 or _flight_waiters(p.scorer) == 1)
        release.set()
        first, second = owner.result(timeout=3), waiter.result(timeout=3)
    p.control["pause"] = None
    third = run(FEED + "/three")
    assert len(p.calls) == 1
    assert sorted(result[1]["api_calls"] for result in (first, second)) == [0, 1]
    assert third[1]["api_calls"] == 0
    assert all(result[2] is None for result in (first, second, third))
    assert all(result[1]["entry_errors"] == 0 for result in (first, second, third))
