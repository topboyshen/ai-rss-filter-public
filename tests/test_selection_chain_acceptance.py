"""P0 acceptance: the final selection chain for capped low-value content.

Every case runs through ``processor.process_entry`` (real ordering, real
persistence) and records four components for one input:

    raw score -> post-adjustment score -> pass gate -> aggregation gate

The point is not to prove the current numbers are right.  #50 lowered several
sources to 4.5-5.0 and #52 caps low-value announcements at 5.0; each PR was
measured separately.  These cases pin the *combined* behaviour so a later
change cannot silently trade one gate for the other.

Product decision still open: is a capped low-value item *de-prioritised*
(may still be selected on a low-threshold source) or *excluded by default*
(no source may select it)?  ``test_capped_low_value_still_passes_article_path``
records the present answer (de-prioritised) so the decision cannot be made by
accident.
"""

import json

import pytest

from core import processor
from core.scorer import (
    SEMANTIC_RESOLUTION_INSUFFICIENT_INPUT,
    SEMANTIC_RESOLUTION_SAFE_NEUTRAL_CONTRACT,
    ScoringResult,
)
from db import database
from utils.helpers import FeedEntry

FEED = "https://feeds.example.test/selection-chain"
STRATEGY = "selection-chain-acceptance"


class _SummaryFeedEntry(FeedEntry):
    """Expose the source summary the way real feedparser entries do.

    ``FeedEntry`` stores the constructor summary in ``ai_summary`` and only
    ``getattr`` backs ``get``, so ``entry.get("summary")`` would be empty and
    the cap would never see the source text.
    """

    def __init__(self, title, link, summary=""):
        super().__init__(title, link, summary)
        self.summary = summary


class _FixedScorer:
    """One bound scoring result; no second stage, no summarisation."""

    def __init__(self, values, resolution="", model_name="acceptance-model"):
        self._result = ScoringResult(values, model_name, resolution)

    def score_stage1_with_identity(self, *_args, **_kwargs):
        return self._result

    def score_stage2_with_identity(self, *_args, **_kwargs):
        raise AssertionError("two-stage scoring is disabled for these cases")

    def summarize(self, *_args, **_kwargs):
        raise AssertionError("summaries are disabled for these cases")


def _cfg(pass_threshold, aggregation_threshold, *, time_bonus=None):
    cfg = {
        "scoring_strategy_version": STRATEGY,
        "threshold": {"pass": 6.0, "notify": 8.0, "similarity": 0.6},
        "similarity_window_hours": 72,
        "_blacklist_re": None,
        "cache_rejected_entries": True,
        "generate_summary": False,
        "summary_min_score": 1.0,
        "local_reject_rules": {"enabled": False, "rules": []},
        "fast_pass": {"enabled": False},
        "two_stage_scoring": {"enabled": False},
        "feeds": [
            {
                "url": FEED,
                "tier": "primary_news",
                "pass_threshold": pass_threshold,
                "aggregation_threshold": aggregation_threshold,
                "notify_threshold": 9.0,
            }
        ],
    }
    if time_bonus is not None:
        cfg["time_weight"] = {
            "enabled": True,
            "rules": [
                {"start_hour": 0, "end_hour": 24, "bonus": time_bonus, "label": "全天"}
            ],
        }
    return cfg


def _run(
    case,
    *,
    title,
    raw_score,
    tag,
    logic,
    pass_threshold,
    aggregation_threshold,
    summary="",
    resolution="",
    time_bonus=None,
):
    """Drive one synthetic entry through the real selection path."""
    database.init_db()
    link = f"https://example.test/selection-chain/{case}"
    entry = _SummaryFeedEntry(title, link, summary=summary)
    scorer = _FixedScorer((raw_score, tag, logic, "⚖️"), resolution)
    outcome = processor.process_entry(
        entry,
        [],
        "prompt",
        _cfg(pass_threshold, aggregation_threshold, time_bonus=time_bonus),
        scorer=scorer,
        feed_url=FEED,
    )
    with database.get_connection() as conn:
        cache = conn.execute(
            "SELECT score, score_path FROM news_cache WHERE link = ?", (link,)
        ).fetchone()
        ledger = conn.execute(
            "SELECT score, selected, aggregation_threshold, score_path "
            "FROM backtest_signals WHERE link = ?",
            (link,),
        ).fetchone()
    assert cache is not None and ledger is not None, "selection path must persist evidence"

    adjusted = float(cache["score"])
    selected = bool(ledger["selected"])
    record = {
        "case": case,
        "raw_score": float(raw_score),
        "adjusted_score": adjusted,
        "pass_threshold": float(pass_threshold),
        "aggregation_threshold": float(aggregation_threshold),
        "score_path": cache["score_path"],
        "article_status": outcome[1],
        "selected": selected,
        "score_reason": (
            "non_substantive"
            if cache["score_path"] in {"insufficient_input", "llm_safe_neutral_contract"}
            else "capped_to_5.0"
            if adjusted == 5.0 and float(raw_score) > 5.0
            else "capped_to_5.0_after_time_bonus"
            if adjusted == 5.0 and float(raw_score) < 5.0
            else "unchanged"
        ),
        "article_reason": (
            f"{adjusted:.1f} >= pass {float(pass_threshold):.1f}"
            if outcome[1] != "filtered"
            else f"{adjusted:.1f} < pass {float(pass_threshold):.1f}"
        ),
        "selection_reason": (
            "non_substantive"
            if ledger["score_path"] in {"insufficient_input", "llm_safe_neutral_contract"}
            else f"{adjusted:.1f} >= aggregation {float(ledger['aggregation_threshold']):.1f}"
            if selected
            else f"{adjusted:.1f} < aggregation {float(ledger['aggregation_threshold']):.1f}"
        ),
    }
    # One line per sample so `pytest -s` prints the acceptance table directly.
    print("SELECTION_CHAIN " + json.dumps(record, ensure_ascii=False, sort_keys=True))
    return record, entry


@pytest.fixture(autouse=True)
def _quiet_side_effects(monkeypatch):
    monkeypatch.setattr(processor.DB, "record_score", lambda *_a, **_k: None)
    monkeypatch.setattr(processor, "send_telegram_notification", lambda *_a, **_k: None)


@pytest.mark.parametrize(
    "pass_threshold,expected_status",
    [
        (4.5, "scored"),  # 金十数据
        (4.7, "scored"),  # 同花顺7x24
        (4.8, "scored"),  # 华尔街见闻实时 / 第一财经 / A股实时
        (5.0, "scored"),  # 财联社 / 华尔街见闻全球 / 发改委 / 统计局
        (5.5, "filtered"),
    ],
)
def test_capped_low_value_still_passes_article_path(pass_threshold, expected_status):
    """The cap (5.0) sits above four live pass thresholds.

    Confirmed gap: the capped item never reaches the aggregation cards
    (aggregation 6.0) but still enters the passing article path on every
    source whose pass threshold is <= 5.0.  This test records the combined
    behaviour; it does not endorse it.
    """
    record, _ = _run(
        f"cap-pass-{pass_threshold}",
        title="高质量数据集和数据标注先行先试现场会在厦门市召开",
        raw_score=6.0,
        tag="数据政策",
        logic="会议召开，暂无具体市场影响",
        pass_threshold=pass_threshold,
        aggregation_threshold=6.0,
    )
    assert record["adjusted_score"] == 5.0
    assert record["score_reason"] == "capped_to_5.0"
    assert record["article_status"] == expected_status
    assert record["selected"] is False


def test_capped_score_never_reaches_aggregation_cards():
    record, _ = _run(
        "cap-aggregation",
        title="2026年中国资本市场学术年会在沪开幕",
        raw_score=6.0,
        tag="资本市场政策",
        logic="会议开幕",
        pass_threshold=4.8,
        aggregation_threshold=6.2,
    )
    assert record["article_status"] == "scored"
    assert record["selected"] is False
    assert record["selection_reason"] == "5.0 < aggregation 6.2"


def test_time_bonus_cannot_lift_a_capped_score():
    """The cap is applied after the time weight, so the bonus cannot undo it."""
    record, _ = _run(
        "cap-time-bonus",
        title="佳士得上海拍卖落槌，总成交额1.18亿元人民币",
        raw_score=4.0,
        tag="艺术",
        logic="拍卖结果",
        pass_threshold=4.9,
        aggregation_threshold=6.0,
        time_bonus=2.0,
    )
    assert record["adjusted_score"] == 5.0
    assert record["score_reason"] == "capped_to_5.0_after_time_bonus"
    assert record["article_status"] == "scored"


def test_below_pass_above_aggregation_is_still_recorded_selected():
    """Latent upgrade path: the article gate and the aggregation gate disagree.

    Production configs keep aggregation >= pass, so this cannot fire today.
    If a future source lowers aggregation below pass, an item rejected for the
    article path is still recorded as selected and is eligible for the realtime
    cards.  The test fixes that behaviour before someone changes a threshold.
    """
    record, _ = _run(
        "aggregation-upgrade",
        title="某券商发布行业周报：关注三条主线",
        raw_score=5.4,
        tag="行业研究",
        logic="周报观点",
        pass_threshold=6.0,
        aggregation_threshold=5.0,
    )
    assert record["score_reason"] == "unchanged"
    assert record["article_status"] == "filtered"
    assert record["selected"] is True
    assert record["selection_reason"] == "5.4 >= aggregation 5.0"


def test_meeting_title_with_a_substantive_summary_keeps_its_score():
    """P3: the title still looks like a meeting, but the summary states a
    quantified policy action, so the cap no longer fires.  The item is still
    below this source's 7.5 aggregation floor and stays unselected."""
    record, _ = _run(
        "meeting-title-substantive-summary",
        title="2026年中国资本市场学术年会在沪开幕",
        raw_score=7.0,
        tag="资本市场政策",
        logic="会议开幕",
        pass_threshold=6.5,
        aggregation_threshold=7.5,
        summary="中国人民银行宣布全面降准0.5个百分点，释放长期资金约1万亿元。",
    )
    assert record["adjusted_score"] == 7.0
    assert record["article_status"] == "scored"
    assert record["selected"] is False


@pytest.mark.parametrize(
    "title",
    [
        "中国银行数字化转型研讨会在京召开",
        "金融科技论坛在上海举行",
    ],
)
def test_company_or_sector_word_bypasses_the_cap_without_a_concrete_event(title):
    """Risk: any 2+ character prefix plus 银行/科技/... disables the cap.

    Neither title states an order, launch, filing or quantified change, yet
    the company regex treats both as concrete and keeps the raw 6.0.
    """
    record, _ = _run(
        "company-word-bypass",
        title=title,
        raw_score=6.0,
        tag="行业动态",
        logic="会议或论坛举行",
        pass_threshold=6.5,
        aggregation_threshold=7.5,
    )
    assert record["adjusted_score"] == 6.0
    assert record["score_reason"] == "unchanged"


def test_insufficient_input_and_fixed_neutral_are_counted_separately(monkeypatch):
    """Both are unselected, but they must not share one bucket.

    The distribution counter (``DB.record_score``) must see neither; the
    score paths must differ so reports can separate "not enough input" from
    "model fixed a neutral".
    """
    recorded = []
    monkeypatch.setattr(processor.DB, "record_score", recorded.append)

    insufficient, _ = _run(
        "insufficient-input",
        title="A股午评来了！",
        raw_score=0.0,
        tag="信息不足",
        logic="只有栏目名，缺少具体事件",
        pass_threshold=4.5,
        aggregation_threshold=6.0,
        resolution=SEMANTIC_RESOLUTION_INSUFFICIENT_INPUT,
    )
    neutral, _ = _run(
        "fixed-neutral",
        title="某地举办春季花卉展览",
        raw_score=4.0,
        tag="其他",
        logic="来源未显示明确投资催化",
        pass_threshold=4.5,
        aggregation_threshold=6.0,
        resolution=SEMANTIC_RESOLUTION_SAFE_NEUTRAL_CONTRACT,
    )
    substantive, _ = _run(
        "substantive-control",
        title="某公司公告获得2.3亿元订单",
        raw_score=7.1,
        tag="公司公告",
        logic="新增订单",
        pass_threshold=4.5,
        aggregation_threshold=6.0,
    )

    assert insufficient["score_path"] == "insufficient_input"
    assert neutral["score_path"] == "llm_safe_neutral_contract"
    assert substantive["score_path"] == "llm"
    assert insufficient["article_status"] == neutral["article_status"] == "filtered"
    assert insufficient["selected"] is False and neutral["selected"] is False
    assert insufficient["score_reason"] == neutral["score_reason"] == "non_substantive"
    # Only the substantive score enters the distribution.
    assert recorded == [7.1]


# ---------------------------------------------------------------------------
# P1: the same chain, continued into the surfaces a reader actually sees.
# These are the queries the dashboard calls: the per-feed observation stream
# (no score floor), the briefing (feed aggregation floor) and the integration
# outbox that feeds Quant.
# ---------------------------------------------------------------------------


def _preview_row(cfg, link):
    rows = database.get_recent_entries_for_feed_preview(
        cfg, hours=24 * 7, limit=20, feed_url=FEED, order_by="recent"
    )
    return next((row for row in rows if row[0] == link), None)


def _briefing_row(cfg, link):
    from services.intelligence_query import query_briefing_tuples

    rows = query_briefing_tuples(cfg, limit=20, hours=24 * 7)
    return next((row for row in rows if row[0] == link), None)


def _outbox_row(link):
    with database.get_connection() as conn:
        return conn.execute(
            "SELECT outbox.score, outbox.selected, outbox.score_path "
            "FROM integration_signal_outbox AS outbox "
            "JOIN backtest_signals AS ledger ON ledger.id = outbox.ledger_signal_id "
            "WHERE ledger.link = ? ORDER BY outbox.seq DESC LIMIT 1",
            (link,),
        ).fetchone()


def test_capped_item_is_browsable_but_absent_from_the_briefing():
    """5.0 on a 4.8-pass source: observation stream yes, highlighted entry no."""
    record, entry = _run(
        "outlet-capped",
        title="2026年中国资本市场学术年会在沪开幕",
        raw_score=6.0,
        tag="资本市场政策",
        logic="会议开幕",
        pass_threshold=4.8,
        aggregation_threshold=6.0,
    )
    cfg = _cfg(4.8, 6.0)

    preview = _preview_row(cfg, entry.link)
    outbox = _outbox_row(entry.link)

    assert record["adjusted_score"] == 5.0
    assert preview is not None and preview[3] == 5.0
    assert _briefing_row(cfg, entry.link) is None
    # The outbox carries the row so Quant can audit it; it is not highlighted.
    assert outbox is not None and outbox["selected"] == 0 and outbox["score"] == 5.0


def test_item_at_the_aggregation_threshold_is_browsable_and_highlighted():
    record, entry = _run(
        "outlet-high",
        title="某公司公告获得2.3亿元订单",
        raw_score=7.1,
        tag="公司公告",
        logic="新增订单",
        pass_threshold=4.8,
        aggregation_threshold=6.0,
    )
    cfg = _cfg(4.8, 6.0)

    preview = _preview_row(cfg, entry.link)
    briefing = _briefing_row(cfg, entry.link)
    outbox = _outbox_row(entry.link)

    assert record["selected"] is True
    assert preview is not None and preview[3] == 7.1
    assert briefing is not None and briefing[3] == 7.1
    assert outbox is not None and outbox["selected"] == 1


def test_non_substantive_states_reach_no_outlet_and_keep_distinct_labels():
    """Insufficient input and fixed neutral are absent from every reader surface."""
    insufficient, insufficient_entry = _run(
        "outlet-insufficient",
        title="A股午评来了！",
        raw_score=0.0,
        tag="信息不足",
        logic="只有栏目名，缺少具体事件",
        pass_threshold=4.5,
        aggregation_threshold=6.0,
        resolution=SEMANTIC_RESOLUTION_INSUFFICIENT_INPUT,
    )
    neutral, neutral_entry = _run(
        "outlet-fixed-neutral",
        title="某地举办春季花卉展览",
        raw_score=4.0,
        tag="其他",
        logic="来源未显示明确投资催化",
        pass_threshold=4.5,
        aggregation_threshold=6.0,
        resolution=SEMANTIC_RESOLUTION_SAFE_NEUTRAL_CONTRACT,
    )
    cfg = _cfg(4.5, 6.0)

    for entry in (insufficient_entry, neutral_entry):
        assert _preview_row(cfg, entry.link) is None
        assert _briefing_row(cfg, entry.link) is None
        assert _outbox_row(entry.link) is None

    from routes.dashboard_routes import _feed_score_category
    from scoring_provenance import score_path_reading_label

    assert insufficient["score_path"] == "insufficient_input"
    assert neutral["score_path"] == "llm_safe_neutral_contract"
    # The source page counts the two states in different buckets and labels.
    assert _feed_score_category("insufficient_input", 0.0, 6.0) == "insufficient_input"
    assert _feed_score_category("llm_safe_neutral_contract", 4.0, 6.0) == "safe_contract"
    assert score_path_reading_label("insufficient_input") == "信息不足（0分）"
    assert score_path_reading_label("llm_safe_neutral_contract") == "保守中性"


def test_time_bonus_cap_keeps_the_displayed_score_and_explanation_consistent():
    """After the time bonus is capped away, the page shows 5.0 with the same tag/logic."""
    record, entry = _run(
        "outlet-time-cap",
        title="佳士得上海拍卖落槌，总成交额1.18亿元人民币",
        raw_score=4.0,
        tag="艺术",
        logic="拍卖结果",
        pass_threshold=4.9,
        aggregation_threshold=6.0,
        time_bonus=2.0,
    )
    cfg = _cfg(4.9, 6.0, time_bonus=2.0)

    preview = _preview_row(cfg, entry.link)
    with database.get_connection() as conn:
        cache = conn.execute(
            "SELECT score, tag, logic, score_path FROM news_cache WHERE link = ?",
            (entry.link,),
        ).fetchone()

    from scoring_provenance import score_path_reading_label

    assert record["score_reason"] == "capped_to_5.0_after_time_bonus"
    assert preview is not None
    assert preview[3] == cache["score"] == 5.0
    assert (preview[4], preview[5]) == (cache["tag"], cache["logic"]) == ("艺术", "拍卖结果")
    assert score_path_reading_label(cache["score_path"]) == "模型评分"


def test_cached_feed_route_separates_observation_stream_from_high_score(monkeypatch):
    """HTTP acceptance: the same source exposes the capped item only in preview."""
    from urllib.parse import quote

    import app as app_module
    import routes.feed_routes as feed_routes

    capped_title = "2026年中国资本市场学术年会在沪开幕"
    high_title = "某公司公告获得2.3亿元订单"
    _run(
        "route-capped",
        title=capped_title,
        raw_score=6.0,
        tag="资本市场政策",
        logic="会议开幕",
        pass_threshold=4.8,
        aggregation_threshold=6.0,
    )
    _run(
        "route-high",
        title=high_title,
        raw_score=7.1,
        tag="公司公告",
        logic="新增订单",
        pass_threshold=4.8,
        aggregation_threshold=6.0,
    )
    cfg = _cfg(4.8, 6.0)
    monkeypatch.setattr(feed_routes, "_cfg", lambda: cfg)
    # DNS for a synthetic host is out of scope here; the allowlist match is
    # already exercised by the route's own tests.
    monkeypatch.setattr(feed_routes, "validate_target_url", lambda url, cfg: (True, ""))

    with app_module.app.test_client() as client:
        observation = client.get(f"/cached_feed?url={quote(FEED)}&preview=true&limit=20")
        high_score = client.get(f"/cached_feed?url={quote(FEED)}&limit=20")

    assert observation.status_code == 200
    assert high_score.status_code == 200
    assert capped_title in observation.get_data(as_text=True)
    assert capped_title not in high_score.get_data(as_text=True)
    assert high_title in observation.get_data(as_text=True)
    assert high_title in high_score.get_data(as_text=True)


# ---------------------------------------------------------------------------
# P2: the selection decision must not depend on every feed keeping its
# aggregation floor above the 5.0 cap.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case,title,raw_score,time_bonus",
    [
        ("guard-capped", "2026年中国资本市场学术年会在沪开幕", 6.0, None),
        ("guard-time-capped", "佳士得上海拍卖落槌，总成交额1.18亿元人民币", 4.0, 2.0),
    ],
)
def test_capped_item_stays_unselected_even_when_the_floor_allows_it(
    case, title, raw_score, time_bonus
):
    """A future aggregation floor of 5.0 must not turn capped content into a highlight."""
    record, entry = _run(
        case,
        title=title,
        raw_score=raw_score,
        tag="艺术",
        logic="低价值会议或拍卖",
        pass_threshold=4.5,
        aggregation_threshold=5.0,
        time_bonus=time_bonus,
    )
    cfg = _cfg(4.5, 5.0, time_bonus=time_bonus)

    assert record["adjusted_score"] == 5.0
    assert record["selected"] is False
    assert _preview_row(cfg, entry.link) is not None
    outbox = _outbox_row(entry.link)
    assert outbox is not None and outbox["selected"] == 0


def test_uncapped_five_is_still_selected_at_a_five_floor():
    """The guard must only cover capped results, not every 5.0 score."""
    record, entry = _run(
        "guard-control",
        title="某公司公告获得2.3亿元订单",
        raw_score=5.0,
        tag="公司公告",
        logic="新增订单",
        pass_threshold=4.5,
        aggregation_threshold=5.0,
    )
    cfg = _cfg(4.5, 5.0)

    assert record["adjusted_score"] == 5.0
    assert record["selected"] is True
    assert _briefing_row(cfg, entry.link) is not None


# ---------------------------------------------------------------------------
# P3: a meeting title with a quantified fact in the summary is not routine.
# ---------------------------------------------------------------------------


def test_meeting_title_with_a_quantified_summary_is_not_capped():
    record, entry = _run(
        "p3-meeting-substantive",
        title="2026年中国资本市场学术年会在沪开幕",
        raw_score=7.0,
        tag="资本市场政策",
        logic="会议开幕",
        pass_threshold=6.5,
        aggregation_threshold=6.5,
        summary="中国人民银行宣布全面降准0.5个百分点，释放长期资金约1万亿元。",
    )
    cfg = _cfg(6.5, 6.5)

    assert record["adjusted_score"] == 7.0
    assert record["article_status"] == "scored"
    assert record["selected"] is True
    assert _briefing_row(cfg, entry.link) is not None


@pytest.mark.parametrize(
    "case,title,tag,summary",
    [
        ("p3-meeting-discussion", "2026年中国资本市场学术年会在沪开幕", "资本市场政策",
         "会议围绕资本市场改革展开讨论。"),
        ("p3-meeting-investment", "2026年中国资本市场学术年会在沪开幕", "资本市场政策",
         "会议强调要扩大有效投资。"),
        ("p3-meeting-date", "2026年中国资本市场学术年会在沪开幕", "资本市场政策",
         "会议于2026年11月17日召开。"),
        ("p3-auction-count", "佳士得上海拍卖落槌，总成交额1.18亿元人民币", "艺术",
         "本场拍卖共上拍200件拍品。"),
    ],
)
def test_meeting_or_auction_title_without_a_quantified_summary_stays_capped(
    case, title, tag, summary
):
    record, entry = _run(
        case,
        title=title,
        raw_score=7.0,
        tag=tag,
        logic="会议或拍卖",
        pass_threshold=4.9,
        aggregation_threshold=6.0,
        summary=summary,
    )
    cfg = _cfg(4.9, 6.0)

    assert record["adjusted_score"] == 5.0
    assert record["article_status"] == "scored"
    assert record["selected"] is False
    assert _preview_row(cfg, entry.link) is not None
