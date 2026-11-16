"""A refresh reports the journal snapshot left after its own feed write-backs."""
from services import natural_score_continuations as journal
from services import natural_scoring
from services.ingestion_status import describe_refresh_coverage

STRATEGY = "strategy-current"
FEED = "https://feed.invalid/remaining"
LINK = "https://article.invalid/remaining"


def _payload():
    return {
        "title": "company reports stronger demand",
        "summary": "Original publisher context retained.",
        "prompt": "Use the original publisher context.",
        "feed_url": FEED,
        "few_shot_context": "",
        "published_parsed": [2026, 10, 20, 10, 5, 0, 1, 293, 0],
    }


def _configure(monkeypatch, process_feed):
    import services.refresh as refresh

    cfg = {
        "feeds": [{"url": FEED, "name": "source"}],
        "scoring_strategy_version": STRATEGY,
        "output": {"max_items": 0},
    }
    recorded = []
    monkeypatch.setattr(refresh, "_cfg", lambda: cfg)
    monkeypatch.setattr(
        refresh, "resolve_prompt", lambda *_args, **_kwargs: ("default", "prompt")
    )
    monkeypatch.setattr(refresh, "process_feed", process_feed)
    monkeypatch.setattr(refresh, "flush_stats", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(refresh, "cleanup_old_entries", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        refresh.DB,
        "record_refresh_attempt",
        lambda result, successful_at=None: recorded.append((result, successful_at)),
    )
    return refresh, recorded


def test_refresh_reports_deferrals_created_by_its_own_feeds(monkeypatch):
    def process_feed(_url, _prompt, _name, _cfg, **_kwargs):
        assert journal.enqueue(STRATEGY, LINK, _payload()) == "stored"
        return [], {
            "total": 1, "passed": 0, "api_calls": 0, "score_deferred": 1,
        }, None, None

    refresh, recorded = _configure(monkeypatch, process_feed)

    result = refresh._execute_feed_refresh()

    assert result["score_continuations"]["remaining"]["active"] == 1
    assert result["score_continuations"]["errors"] == 0
    state, text = describe_refresh_coverage(result)
    assert state == "incomplete"
    assert "待续作 1 条" in text
    assert recorded == [(result, None)]


def test_unreadable_post_batch_journal_is_unknown_not_stale(monkeypatch):
    stale = {
        "attempted": 0, "completed": 0, "deferred": 0, "terminal_unscored": 0,
        "errors": 0, "remaining": {"total": 0, "active": 0, "due": 0, "terminal": 0},
    }

    def process_feed(*_args, **_kwargs):
        return [], {"total": 1, "passed": 1, "api_calls": 0}, None, None

    refresh, recorded = _configure(monkeypatch, process_feed)
    monkeypatch.setattr(refresh, "resume_continuations", lambda *_args, **_kwargs: dict(stale))
    monkeypatch.setattr(refresh, "remaining_overview", lambda _cfg: None)

    result = refresh._execute_feed_refresh()

    assert result["score_continuations"]["remaining"] is None
    assert result["score_continuations"]["errors"] == 1
    assert result["status"] == "partial"
    assert describe_refresh_coverage(result)[0] == "unknown"
    assert recorded == [(result, None)]


def test_refresh_without_continuations_keeps_the_legacy_shape(monkeypatch):
    def process_feed(*_args, **_kwargs):
        return [], {"total": 1, "passed": 1, "api_calls": 0}, None, None

    refresh, recorded = _configure(monkeypatch, process_feed)
    monkeypatch.setattr(refresh, "_cfg", lambda: {
        "feeds": [{"url": FEED, "name": "source"}], "output": {"max_items": 0},
    })

    result = refresh._execute_feed_refresh()

    assert "remaining" not in result["score_continuations"]
    assert result["status"] == "success"
    assert describe_refresh_coverage(result)[0] == "complete"
    assert recorded == [(result, result["finished_at"])]


def test_remaining_overview_reads_the_real_journal():
    assert journal.enqueue(STRATEGY, LINK, _payload()) == "stored"

    remaining = natural_scoring.remaining_overview({"scoring_strategy_version": STRATEGY})

    assert remaining["total"] == 1
    assert remaining["active"] == 1
    assert remaining["terminal"] == 0


def test_remaining_overview_fails_closed_when_journal_cannot_be_read(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise OSError("journal busy")

    monkeypatch.setattr(natural_scoring.store, "overview", unavailable)

    assert natural_scoring.remaining_overview(
        {"scoring_strategy_version": STRATEGY}
    ) is None


def test_remaining_overview_absent_without_strategy_or_journal():
    assert natural_scoring.remaining_overview({}) is None


def test_remaining_overview_empty_strategy_with_journal_is_unknown():
    assert journal.enqueue(STRATEGY, LINK, _payload()) == "stored"

    assert natural_scoring.remaining_overview({}) is None
