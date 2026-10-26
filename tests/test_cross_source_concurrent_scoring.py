"""Two sources delivering one link concurrently must score and persist once.

Production 2026-10-04: the same wallstreetcn link arrived via two feeds in
parallel webhook tasks.  Both passed the unlocked cache/ledger pre-checks,
both called the model, and the slower writer was rejected by the frozen
ledger/cache guards.  The guards are correct; the duplicate scoring is not.
"""

import re
import threading
import time
from types import SimpleNamespace

import pytest

from core import processor
from db import database
from scoring_provenance import SAFE_NEUTRAL_CONTRACT_VALUES
from utils.helpers import FeedEntry


LINK = "https://example.com/livenews/concurrent"
STRATEGY = "concurrent:fixed-contract"


def _outcome(score, *, resolution="", model="test-stage1"):
    values = (
        SAFE_NEUTRAL_CONTRACT_VALUES
        if resolution
        else (score, "海外无关消息", "海外政治事件与A股制度无关", "⚖️")
    )
    return SimpleNamespace(
        values=values, model_name=model, semantic_resolution=resolution
    )


class OverlappingScorer:
    """Hold each stage-1 call until a concurrent peer arrives (or a timeout)."""

    def __init__(self, outcome, barrier, calls):
        self.outcome = outcome
        self.barrier = barrier
        self.calls = calls

    def score_stage1_with_identity(self, *_args, **_kwargs):
        self.calls.append(self.outcome.model_name)
        try:
            self.barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return self.outcome

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
            "mapped_theme": "",
            "backtest_symbol": None,
            "mapping_confidence": 0.0,
            "security_candidates": [],
            "market_references": [],
        },
    )
    monkeypatch.setattr(processor, "get_mapping_version", lambda: "mapping:test")
    monkeypatch.setattr(
        processor,
        "send_telegram_notification",
        lambda *_a, **_k: pytest.fail("fixture must not send a notification"),
    )
    return {
        "_blacklist_re": re.compile(r"$^"),
        "threshold": {"pass": 6.0, "notify": 99.0, "similarity": 0.9},
        "scoring_strategy_version": STRATEGY,
        "few_shot_feedback": {"enabled": False},
        "generate_summary": False,
        # Different per-source thresholds give each source a different
        # score explanation (score_threshold / score_margin) for one link.
        "feeds": [
            {"url": "feed-global", "pass_threshold": 6.5, "aggregation_threshold": 6.5},
            {"url": "feed-live", "pass_threshold": 6.0, "aggregation_threshold": 6.0},
        ],
    }


def _entry():
    entry = FeedEntry("巴西总统选举首轮投票开始", LINK, "来源摘要")
    entry.published_parsed = time.gmtime(time.time() - 60)
    return entry


def _run_concurrently(cfg, outcomes):
    barrier = threading.Barrier(len(outcomes), timeout=0.75)
    calls = []
    results = {}
    start = threading.Barrier(len(outcomes))

    def run(feed_url, outcome):
        scorer = OverlappingScorer(outcome, barrier, calls)
        start.wait()
        try:
            results[feed_url] = processor.process_entry(
                _entry(),
                [],
                "fixed prompt",
                cfg,
                scorer=scorer,
                feed_url=feed_url,
            )
        except Exception as exc:  # captured for outcome assertions
            results[feed_url] = exc

    threads = [
        threading.Thread(target=run, args=(feed_url, outcome))
        for feed_url, outcome in outcomes.items()
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    return results, calls


def _rows():
    with database.get_connection() as conn:
        cache = conn.execute(
            "SELECT * FROM news_cache WHERE link = ?", (LINK,)
        ).fetchall()
        signals = conn.execute(
            "SELECT * FROM backtest_signals WHERE link = ?", (LINK,)
        ).fetchall()
    return [dict(row) for row in cache], [dict(row) for row in signals]


def _assert_single_clean_outcome(results, calls):
    errors = {k: v for k, v in results.items() if isinstance(v, BaseException)}
    assert errors == {}
    assert len(calls) == 1, calls
    reasons = sorted(reason for _entry, reason in results.values())
    assert reasons == ["filtered", "filtered"]
    cache, signals = _rows()
    assert len(cache) == 1
    assert len(signals) == 1
    winner = signals[0]
    assert cache[0]["score_path"] == winner["score_path"]
    assert cache[0]["feed_url"] == winner["feed_url"]
    assert cache[0]["score_explanation_token"] == winner["score_explanation_token"]
    assert cache[0]["strategy_version"] == STRATEGY
    # The per-link wait registry does not leak entries.
    assert processor._entry_link_slots == {}
    return cache[0], winner


def test_cross_source_same_link_scores_once_with_differing_explanations(cfg):
    results, calls = _run_concurrently(
        cfg,
        {
            "feed-global": _outcome(0.0, model="kimi-test"),
            "feed-live": _outcome(0.0, model="kimi-test"),
        },
    )
    cache, signal = _assert_single_clean_outcome(results, calls)
    assert signal["score_path"] == "llm"
    assert signal["score"] == 0.0
    expected_threshold = 6.5 if signal["feed_url"] == "feed-global" else 6.0
    assert signal["score_threshold"] == expected_threshold
    assert cache["score_threshold"] == expected_threshold


def test_cross_source_same_link_does_not_overwrite_with_safe_neutral(cfg):
    results, calls = _run_concurrently(
        cfg,
        {
            "feed-global": _outcome(0.0, model="kimi-test"),
            "feed-live": _outcome(
                4.0, resolution="safe_neutral_grounding", model="qwen-test"
            ),
        },
    )
    cache, signal = _assert_single_clean_outcome(results, calls)
    # Whichever source won, its frozen path is the only persisted path.
    expected_path = (
        "llm" if calls == ["kimi-test"] else "llm_safe_neutral_grounding"
    )
    assert signal["score_path"] == expected_path
    assert cache["score_path"] == expected_path


def test_different_links_still_score_in_parallel(cfg):
    barrier = threading.Barrier(2, timeout=5)
    calls = []
    results = {}

    def run(index):
        entry = FeedEntry(f"并行新闻{index}", f"{LINK}/{index}", "来源摘要")
        entry.published_parsed = time.gmtime(time.time() - 60)
        scorer = OverlappingScorer(_outcome(0.0), barrier, calls)
        results[index] = processor.process_entry(
            entry, [], "fixed prompt", cfg, scorer=scorer, feed_url="feed-live"
        )

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    # Both calls met at the barrier, so the per-link guard did not serialize
    # unrelated links.
    assert not barrier.broken
    assert len(calls) == 2
    assert sorted(reason for _e, reason in results.values()) == [
        "filtered",
        "filtered",
    ]



def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _same_link_waiters():
    slot = processor._entry_link_slots.get(LINK)
    return slot[1] if slot else 0


class SignallingScorer:
    """Report entry, then hold until the peer is queued on the same link."""

    def __init__(self, score, calls, entered, proceed):
        self.score = score
        self.calls = calls
        self.entered = entered
        self.proceed = proceed

    def score_stage1_with_identity(self, *_args, **_kwargs):
        self.calls.append(self.score)
        self.entered.set()
        assert self.proceed.wait(5), "peer never queued behind the link lock"
        return _outcome(self.score)

    def score_stage2_with_identity(self, *_args, **_kwargs):
        pytest.fail("fixture must not reach stage 2")

    def _fallback_local_score(self, *_args, **_kwargs):
        pytest.fail("fixture must not fall back")


def test_prefetched_batch_cache_is_refreshed_after_same_link_wait(cfg):
    """A batch snapshot taken before the peer committed must not hide reuse.

    The strict source (6.5) scores 6.2 and filters it.  The looser source
    (6.0) prefetched its batch cache before that commit; after its same-link
    wait it must reuse the committed score as a qualifying article instead of
    being filtered by the ledger pre-check, and must not call the model.
    """
    calls = []
    entered = threading.Event()
    proceed = threading.Event()
    results = {}

    def strict():
        scorer = SignallingScorer(6.2, calls, entered, proceed)
        results["feed-global"] = processor.process_entry(
            _entry(), [], "fixed prompt", cfg, scorer=scorer,
            feed_url="feed-global",
        )

    # The looser source's batch snapshot predates the strict commit.
    snapshot = database.get_cached_entries_by_links([LINK])
    assert snapshot == {}

    def loose():
        scorer = OverlappingScorer(_outcome(9.9), threading.Barrier(1), calls)
        results["feed-live"] = processor.process_entry(
            _entry(), [], "fixed prompt", cfg, scorer=scorer,
            feed_url="feed-live", link_cache=snapshot,
        )

    first = threading.Thread(target=strict)
    first.start()
    assert entered.wait(5)
    second = threading.Thread(target=loose)
    second.start()
    assert _wait_for(lambda: _same_link_waiters() == 2)
    proceed.set()
    for thread in (first, second):
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert calls == [6.2]
    assert results["feed-global"] == (None, "filtered")
    entry, reason = results["feed-live"]
    assert reason == "cache"
    assert entry is not None and entry.title.startswith("[⭐6.2")
    with database.get_connection() as conn:
        sources = {
            row[0]
            for row in conn.execute(
                "SELECT feed_url FROM article_sources WHERE link = ?", (LINK,)
            )
        }
        signals = conn.execute(
            "SELECT score, feed_url, revision_key, selected, score_path "
            "FROM backtest_signals WHERE link = ? ORDER BY id",
            (LINK,),
        ).fetchall()
        cache_feed = conn.execute(
            "SELECT feed_url FROM news_cache WHERE link = ?", (LINK,)
        ).fetchone()[0]
    # Source sighting recorded; the primary cache source label is unchanged.
    assert sources == {"feed-global", "feed-live"}
    assert cache_feed == "feed-global"
    # One scored base row plus the existing looser-source selection upgrade
    # of that same frozen score -- not a second scoring.
    assert [tuple(row) for row in signals] == [
        (6.2, "feed-global", "base", 0, "llm"),
        (6.2, "feed-live", "cache_selection:1", 1, "llm"),
    ]
    assert processor._entry_link_slots == {}


def test_same_link_wait_timeout_proceeds_while_holder_still_holds(
    cfg, monkeypatch, caplog
):
    """The wait is bounded: a stuck holder cannot block a waiter forever."""
    monkeypatch.setattr(processor, "_ENTRY_LINK_WAIT_SECONDS", 0.1)
    held = threading.Event()
    release = threading.Event()
    waiter_done = threading.Event()
    observed = {}

    def hold():
        with processor._entry_link_single_flight(LINK):
            held.set()
            # Keep the lock until the waiter has finished (bounded only by a
            # test-level guard so an unbounded wait fails instead of hanging).
            observed["waiter_finished_while_held"] = waiter_done.wait(5)
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(5)

    calls = []
    result = {}

    def wait_then_score():
        scorer = OverlappingScorer(_outcome(0.0), threading.Barrier(1), calls)
        result["value"] = processor.process_entry(
            _entry(), [], "fixed prompt", cfg, scorer=scorer, feed_url="feed-live"
        )
        waiter_done.set()

    with caplog.at_level("WARNING", logger=processor.logger.name):
        waiter = threading.Thread(target=wait_then_score)
        waiter.start()
        waiter.join(timeout=5)
        waiter_alive = waiter.is_alive()
        holder_alive = holder.is_alive()
        slots_while_held = _same_link_waiters()
        release.set()
        holder.join(timeout=5)
        waiter.join(timeout=5)

    assert not waiter_alive, "same-link wait was not bounded"
    assert holder_alive, "holder released before the waiter proceeded"
    assert observed["waiter_finished_while_held"] is True
    assert slots_while_held == 1
    assert result["value"] == (None, "filtered")
    assert calls == ["test-stage1"]
    assert any(
        "same-link wait timed out" in record.getMessage()
        and record.levelname == "WARNING"
        for record in caplog.records
    )
    assert not holder.is_alive()
    assert processor._entry_link_slots == {}
