"""Terminal unscored work must be explicit, not a pending or complete claim."""
import pytest

from services.ingestion_status import describe_refresh_coverage


def _attempt(**changes):
    return {
        "version": 1, "started_at": 1000, "finished_at": 1001,
        "status": "partial", "enabled_sources": 1, "succeeded_sources": 0,
        "partial_sources": 1, "failed_sources": 0,
        "source_unrecognized_entries": 0, "score_deferred_entries": 0,
        "score_terminal_unscored_entries": 1,
        "failures": [{"source": "合成来源", "error": "not public"}], **changes,
    }


def test_terminal_source_is_incomplete_and_never_described_as_pending():
    state, text = describe_refresh_coverage(_attempt(), now=1002)
    assert state == "incomplete"
    assert "终止未评分 1 条" in text and "待评分 0 条" in text
    assert "待评分 1 条" not in text and "not public" not in text


@pytest.mark.parametrize("value", [-1, True, "1", None])
def test_invalid_terminal_counter_never_claims_completeness(value):
    state, _ = describe_refresh_coverage(_attempt(score_terminal_unscored_entries=value), now=1002)
    assert state == "unknown"


def test_success_with_terminal_unscored_source_is_rejected():
    state, _ = describe_refresh_coverage(
        _attempt(status="success", succeeded_sources=1, partial_sources=0), now=1002,
    )
    assert state == "unknown"


@pytest.mark.parametrize("continuations,expected", [
    ({"terminal_unscored": 1}, "终止未评分 1 条"),
    ({"deferred": 1}, "本批递延 1 条"),
    ({"errors": 1}, "执行异常 1 条"),
    ({"remaining": {"active": 1}}, "待续作 1 条"),
])
def test_continuation_only_partial_is_explicit_not_unknown(continuations, expected):
    attempt = _attempt(succeeded_sources=1, partial_sources=0,
                       score_terminal_unscored_entries=0, failures=[],
                       score_continuations=continuations)
    state, text = describe_refresh_coverage(attempt, now=1002)
    assert state == "incomplete" and "续作批次" in text and expected in text
    assert describe_refresh_coverage({**attempt, "status": "success"}, now=1002)[0] == "unknown"


@pytest.mark.parametrize("continuations", [
    None, [], {"remaining": None}, {"remaining": {"active": True}},
    {"terminal_unscored": "1"}, {"terminal_unscored": -1}, {"errors": 1.5},
])
def test_invalid_continuation_counter_is_unknown(continuations):
    assert describe_refresh_coverage(_attempt(score_continuations=continuations), now=1002)[0] == "unknown"


def test_historical_terminal_totals_do_not_claim_a_new_failure_or_a_new_completion():
    attempt = _attempt(
        status="success", succeeded_sources=1, partial_sources=0, failures=[],
        score_terminal_unscored_entries=0,
        score_continuations={"terminal_unscored": 0, "remaining": {"active": 0, "expired": 1}},
    )
    state, text = describe_refresh_coverage(attempt, now=1002)
    assert state == "complete"
    assert "待续作" not in text and "终止未评分" not in text
