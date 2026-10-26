"""Homepage-only numeric formatting folds must not erase distinct claims."""

import pytest

from services import event_daily_report as daily
from services import event_home_digest as home
from test_event_daily_report import _block, _evidence, _event, _mapping, _report_event, _ts


HANS_TITLE = "大族激光：预计前三季度归母净利润21.2亿元-22.2亿元，同比增长145.55%-157.13%"
HANS_FORMAT_TITLE = "大族激光：预计前三季度归母净利润为21.20亿元-22.20亿元，同比增长145.55%-157.13%"


@pytest.fixture
def database():
    import db.database as database_module

    database_module.init_db()
    return database_module


def _seed(database, key, title, *, priority=8, hour=10, stance="supports"):
    return _report_event(
        database, key, title, f"https://news.example/{key}", at=_ts(15, hour),
        priority=priority, stance=stance,
    )


def test_real_hans_headlines_use_one_slot_and_backfill_without_rewriting(database, monkeypatch):
    representative = _seed(database, "hans-first", HANS_TITLE, priority=9, hour=9)
    duplicate = _seed(database, "hans-format", HANS_FORMAT_TITLE, priority=8.9, hour=11)
    independent = _seed(database, "other-first", "光库科技预计前三季度净利润增长90%", priority=8.8)
    backfill = _seed(database, "other-second", "甲公司获得乙公司项目订单", priority=8.7)
    _mapping(database, representative, "002008", "大族激光", "direct", at=_ts(15, 9))
    materialized = []
    original = daily._item_view

    def counted(*args, **kwargs):
        materialized.append(args[0]["root_ids"])
        return original(*args, **kwargs)

    monkeypatch.setattr(daily, "_item_view", counted)
    digest = home.load_home_event_digest(now=_ts(15, 12))
    assert [row["event_id"] for row in digest["highlights"]] == [representative, independent, backfill]
    assert len(materialized) == 3
    shown = digest["highlights"][0]
    assert shown["title"] == HANS_TITLE
    assert shown["sources"][0]["claim_text"] == HANS_TITLE
    assert shown["sources"][0]["url"] == "https://news.example/hans-first"
    assert shown["priority"] == 9
    assert shown["block"] == "pending"
    report = daily.load_daily_report("2026-10-15", now=_ts(15, 12))
    assert {row["event_id"] for row in _block(report, "pending")["entries"]} == {
        representative, duplicate, independent,
    }
    assert {row["title"] for row in _block(report, "pending")["entries"]} >= {
        HANS_TITLE, HANS_FORMAT_TITLE,
    }
    daily_representative = next(
        row for row in _block(report, "pending")["entries"] if row["event_id"] == representative
    )
    assert shown["companies"] == daily_representative["companies"]


def test_named_company_chip_stays_bound_to_the_representative_source(database):
    first = _seed(database, "named-first", "宁德时代预计净利润为5.00亿元", priority=9)
    _seed(database, "named-format", "宁德时代预计净利润5.0亿元", priority=8)
    _mapping(database, first, "300750", "宁德时代", "direct", at=_ts(15, 10))
    item = home.load_home_event_digest(now=_ts(15, 12))["highlights"][0]
    assert item["event_id"] == first
    assert item["title"] == "宁德时代预计净利润为5.00亿元"
    assert item["companies"] == ["宁德时代"]
    assert item["sources"][0]["url"] == "https://news.example/named-first"


def test_correction_slot_folding_backfills_second_distinct_claim(database):
    first = _seed(database, "deny-first", "甲公司否认获得3.0亿元订单", priority=5)
    _seed(database, "deny-format", "甲公司否认获得3.00亿元订单", priority=4)
    second = _seed(database, "deny-second", "乙公司否认收购传闻", priority=3)
    _seed(database, "deny-third", "丙公司否认停产传闻", priority=2)
    digest = home.load_home_event_digest(now=_ts(15, 12))
    assert [row["event_id"] for row in digest["corrections"]] == [first, second]
    assert digest["corrections"][0]["title"] == "甲公司否认获得3.0亿元订单"
    report = daily.load_daily_report("2026-10-15", now=_ts(15, 12))
    assert len(_block(report, "correction")["entries"]) == 4


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (HANS_TITLE, HANS_FORMAT_TITLE),
        ("甲公司净利润为5.00元", "甲公司净利润5元"),
        ("甲公司归母净利润为21.20亿元，同比增长+5.00%", "甲公司归母净利润21.2亿元，同比增长+5%"),
        ("甲公司获得3.00万元订单", "甲公司获得3万元订单"),
        ("甲公司获得3.10万亿元订单", "甲公司获得3.1万亿元订单"),
    ],
)
def test_only_explicit_amount_and_percentage_formatting_folds(left, right):
    assert home._headline_slot_key(left) == home._headline_slot_key(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("大族激光预计净利润21.20亿元", "光库科技预计净利润21.20亿元"),
        ("大族激光预计前三季度净利润21.20亿元", "大族激光预计上半年净利润21.20亿元"),
        ("甲公司获得21.20亿元订单", "甲公司获得22.20亿元订单"),
        ("甲公司获得21.20亿元订单", "甲公司获得21.20万元订单"),
        ("甲公司获得21.20亿元订单", "甲公司获得21.20亿美元订单"),
        ("甲公司利润同比+5.00%", "甲公司利润同比-5.00%"),
        ("甲公司利润同比+ 5.00%", "甲公司利润同比- 5.00%"),
        ("甲公司利润同比+ 5.00%", "甲公司利润同比5.00%"),
        ("甲公司利润同比5.00%", "甲公司利润同比0.50%"),
        ("甲公司订单金额≤5.00亿元", "甲公司订单金额≥5.00亿元"),
        ("甲公司订单金额＜5.00亿元", "甲公司订单金额≥5.00亿元"),
        ("甲公司利润增长5.00%营收下降10.00%", "甲公司利润下降5.00%营收增长10.00%"),
        ("甲公司计划获得3.00亿元订单", "甲公司已获得3.00亿元订单"),
        ("甲公司未获得3.00亿元订单", "甲公司获得3.00亿元订单"),
        ("甲公司预计净利润为21.20亿元", "甲公司实现净利润为21.20亿元"),
        ("甲公司2026.10.10发布报告", "甲公司2026.10.1发布报告"),
        ("甲公司版本1.20发布", "甲公司版本1.2发布"),
        ("甲公司数量为21.20", "甲公司数量为21.2"),
        ("甲公司订单为3.00亿元", "甲公司订单3.00亿元"),
        ("甲公司获得3.00亿元订单？", "甲公司获得3.00亿元订单"),
        ("长实集团辟谣：绝无此事", "长实集团辟谣李嘉诚逝世：绝无此事"),
        ("长实集团否认李嘉诚逝世", "长实集团否认出售物业"),
    ],
)
def test_distinct_subject_facts_qualifiers_dates_and_unknown_denial_targets_remain(left, right):
    assert home._headline_slot_key(left) != home._headline_slot_key(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("大族激光预计净利润21.20亿元", "光库科技预计净利润21.20亿元"),
        ("大族激光预计前三季度净利润21.20亿元", "大族激光预计上半年净利润21.20亿元"),
        ("甲公司获得21.20亿元订单", "甲公司获得22.20亿元订单"),
        ("甲公司订单金额≤5.00亿元", "甲公司订单金额≥5.00亿元"),
        ("甲公司利润增长5.00%营收下降10.00%", "甲公司利润下降5.00%营收增长10.00%"),
        ("甲公司计划获得3.00亿元订单", "甲公司已获得3.00亿元订单"),
    ],
)
def test_distinct_eligible_claims_still_occupy_separate_home_slots(database, left, right):
    first = _seed(database, "distinct-first", left, priority=9)
    second = _seed(database, "distinct-second", right, priority=8)
    digest = home.load_home_event_digest(now=_ts(15, 12))
    assert [row["event_id"] for row in digest["highlights"]] == [first, second]
    assert [row["title"] for row in digest["highlights"]] == [left, right]


@pytest.mark.parametrize(
    ("left_qualifier", "right_qualifier"),
    [
        ("±", ""), ("∓", ""), ("≈", ""), ("≲", ""),
        ("±", "∓"), ("≈", "±"), ("<=", ">="), ("+ ", "- "),
    ],
)
def test_numeric_qualifiers_preserve_distinct_home_claims_and_backfill(
    database, left_qualifier, right_qualifier,
):
    left = f"甲公司利润同比{left_qualifier}5.00%"
    right = f"甲公司利润同比{right_qualifier}5.0%"
    first = _seed(database, "qualified-first", left, priority=9)
    second = _seed(database, "qualified-second", right, priority=8.9)
    duplicate = _seed(
        database, "qualified-format", f"甲公司利润同比{left_qualifier}5.000%", priority=8.8,
    )
    following_title = "乙公司获得丙公司项目订单"
    following = _seed(database, "qualified-following", following_title, priority=8.7)
    overflow = _seed(database, "qualified-overflow", "丁公司成立研发中心", priority=8.6)

    report = daily.load_daily_report("2026-10-15", now=_ts(15, 12))
    assert [row["event_id"] for row in _block(report, "new")["entries"]] == [
        first, second, duplicate, following, overflow,
    ]
    items = home.load_home_event_digest(now=_ts(15, 12))["highlights"]
    assert [row["event_id"] for row in items] == [first, second, following]
    assert [row["title"] for row in items] == [left, right, following_title]
    assert [row["sources"][0]["claim_text"] for row in items] == [left, right, following_title]
    assert [row["sources"][0]["url"] for row in items] == [
        "https://news.example/qualified-first",
        "https://news.example/qualified-second",
        "https://news.example/qualified-following",
    ]


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("甲公司利润同比5.00%≈", "甲公司利润同比5.0%"),
        ("ß公司利润同比≈5.00%", "ß公司利润同比5.0%"),
    ],
)
def test_numeric_qualifier_at_tail_and_after_casefold_expansion_keeps_its_slot(database, left, right):
    first = _seed(database, "symbol-position-first", left, priority=9)
    second = _seed(database, "symbol-position-second", right, priority=8.9)
    following = _seed(database, "symbol-position-following", "乙公司获得丙公司项目订单", priority=8.8)
    _seed(database, "symbol-position-overflow", "丁公司成立研发中心", priority=8.7)
    items = home.load_home_event_digest(now=_ts(15, 12))["highlights"]
    assert [row["event_id"] for row in items] == [first, second, following]
    assert [row["title"] for row in items[:2]] == [left, right]
    assert [row["sources"][0]["claim_text"] for row in items[:2]] == [left, right]
    assert [row["sources"][0]["url"] for row in items[:2]] == [
        "https://news.example/symbol-position-first", "https://news.example/symbol-position-second",
    ]


@pytest.mark.parametrize(
    ("qualifier", "format_qualifier"), [("±", "&plusmn;"), ("∓", "∓"), ("≈", "&#8776;"), ("~", "～")],
)
def test_same_numeric_qualifier_still_folds_decimal_formatting_and_backfills(
    database, qualifier, format_qualifier,
):
    title = f"甲公司利润同比{qualifier}5.00%"
    first = _seed(database, "same-qualifier-first", title, priority=9)
    duplicate = _seed(
        database, "same-qualifier-format", f"甲公司利润同比{format_qualifier}5.0%", priority=8.9,
    )
    following = _seed(database, "same-qualifier-following", "乙公司获得丙公司项目订单", priority=8.8)
    backfill = _seed(database, "same-qualifier-backfill", "丁公司成立研发中心", priority=8.7)

    report = daily.load_daily_report("2026-10-15", now=_ts(15, 12))
    assert [row["event_id"] for row in _block(report, "new")["entries"]] == [
        first, duplicate, following, backfill,
    ]
    items = home.load_home_event_digest(now=_ts(15, 12))["highlights"]
    assert [row["event_id"] for row in items] == [first, following, backfill]
    assert items[0]["title"] == title
    assert items[0]["sources"][0]["claim_text"] == title
    assert [row["sources"][0]["url"] for row in items] == [
        "https://news.example/same-qualifier-first",
        "https://news.example/same-qualifier-following",
        "https://news.example/same-qualifier-backfill",
    ]


@pytest.mark.parametrize("qualifier", ["±", "∓", "≈"])
@pytest.mark.parametrize("context_field", ["fresh", "history"])
def test_numeric_qualifiers_in_context_survive_equal_primary_titles(database, qualifier, context_field):
    day = 14 if context_field == "history" else 15
    first_title = f"甲公司利润同比{qualifier}5.00%"
    second_title = "甲公司利润同比5.0%"
    first = _report_event(
        database, "qualified-context-first", first_title, "https://news.example/context-first",
        at=_ts(day, 9), priority=9,
    )
    second = _report_event(
        database, "qualified-context-second", second_title, "https://news.example/context-second",
        at=_ts(day, 9), priority=8.9,
    )
    latest_title = "甲公司披露利润进展"
    _evidence(database, first, "https://news.example/latest-first", latest_title, at=_ts(15, 11))
    _evidence(database, second, "https://news.example/latest-second", latest_title, at=_ts(15, 11))
    following_title = "乙公司获得丙公司项目订单"
    following = _seed(database, "qualified-context-following", following_title, priority=8.8)
    _seed(database, "qualified-context-overflow", "丁公司成立研发中心", priority=8.7)

    items = home.load_home_event_digest(now=_ts(15, 12))["highlights"]
    assert [row["event_id"] for row in items] == [first, second, following]
    assert [row["title"] for row in items] == [latest_title, latest_title, following_title]
    assert [row["sources"][0]["claim_text"] for row in items] == [
        latest_title, latest_title, following_title,
    ]
    assert [row["sources"][0]["url"] for row in items] == [
        "https://news.example/latest-first",
        "https://news.example/latest-second",
        "https://news.example/qualified-context-following",
    ]
    expected_block = "advanced" if context_field == "history" else "new"
    assert [row["block"] for row in items[:2]] == [expected_block, expected_block]
    if context_field == "history":
        report = daily.load_daily_report("2026-10-15", now=_ts(15, 12))
        assert [row["previous"]["claim_text"] for row in _block(report, "advanced")["entries"]] == [
            first_title, second_title,
        ]


def test_real_cheung_kong_short_denial_is_not_assumed_same_specific_claim(database):
    first = _seed(database, "cheung-short", "长实集团辟谣：绝无此事", priority=3, hour=11)
    second = _seed(database, "cheung-specific", "长实集团辟谣李嘉诚逝世：绝无此事", priority=2)
    digest = home.load_home_event_digest(now=_ts(15, 12))
    assert [row["event_id"] for row in digest["corrections"]] == [first, second]
    assert [row["sources"][0]["url"] for row in digest["corrections"]] == [
        "https://news.example/cheung-short", "https://news.example/cheung-specific",
    ]


def test_slot_identity_uses_primary_original_claim_not_mutable_event_title(database):
    first = _event(database, "metadata-one", "旧事件元数据一", at=_ts(15, 9), priority=9)
    second = _event(database, "metadata-two", "旧事件元数据二", at=_ts(15, 10), priority=8)
    _evidence(database, first, "https://news.example/primary-one", HANS_TITLE, at=_ts(15, 9))
    _evidence(database, second, "https://news.example/primary-two", HANS_FORMAT_TITLE, at=_ts(15, 10))
    digest = home.load_home_event_digest(now=_ts(15, 12))
    assert [row["event_id"] for row in digest["highlights"]] == [first]
    assert digest["highlights"][0]["title"] == HANS_TITLE
    assert digest["highlights"][0]["sources"][0]["url"] == "https://news.example/primary-one"


def test_equal_current_wording_does_not_hide_one_groups_denial_background(database):
    first = _report_event(
        database, "has-denial", "甲公司启动研发项目", "https://news.example/old-first",
        at=_ts(14, 9), priority=9,
    )
    _evidence(database, first, "https://news.example/old-denial", "甲公司否认获得订单", at=_ts(14, 11))
    second = _report_event(
        database, "no-denial", "甲公司成立研发中心", "https://news.example/old-second",
        at=_ts(14, 10), priority=8,
    )
    _evidence(database, first, "https://news.example/current-first", "甲公司披露利润3.00亿元", at=_ts(15, 10))
    _evidence(database, second, "https://news.example/current-second", "甲公司披露利润3.0亿元", at=_ts(15, 10))
    items = home.load_home_event_digest(now=_ts(15, 12))["highlights"]
    assert [row["event_id"] for row in items] == [first, second]
    assert items[0]["background_note"] == "此前已有否认／更正，新增报道不代表推翻"
    assert items[1]["background_note"] == ""
    assert [row["block"] for row in items] == ["advanced", "advanced"]


def test_pending_existing_context_is_not_folded_into_first_seen_claim(database):
    first = _report_event(
        database, "has-context", "甲公司举行技术交流", "https://news.example/context",
        at=_ts(14, 9), priority=9, stance="context",
    )
    _evidence(database, first, "https://news.example/updated-plan", "甲公司计划获得3.00亿元订单", at=_ts(15, 10))
    second = _seed(database, "new-plan", "甲公司计划获得3.0亿元订单", priority=8)
    items = home.load_home_event_digest(now=_ts(15, 12))["highlights"]
    assert [row["event_id"] for row in items] == [first, second]
    assert items[0]["change"].startswith("已有事件出现新的计划/传闻/预期表述")
    assert items[1]["change"].startswith("首次出现")


def test_equal_denials_preserve_whether_an_earlier_support_was_linked(database):
    first = _report_event(
        database, "has-support", "甲公司获得项目订单", "https://news.example/prior-support",
        at=_ts(14, 9), priority=9,
    )
    second = _report_event(
        database, "context-only", "甲公司举行技术交流", "https://news.example/prior-context",
        at=_ts(14, 10), priority=8, stance="context",
    )
    _evidence(database, first, "https://news.example/deny-with-support", "甲公司否认获得3.00亿元订单", at=_ts(15, 10))
    _evidence(database, second, "https://news.example/deny-no-support", "甲公司否认获得3.0亿元订单", at=_ts(15, 10))
    items = home.load_home_event_digest(now=_ts(15, 12))["corrections"]
    assert [row["event_id"] for row in items] == [first, second]
    assert items[0]["change"].startswith("此前有 1 条支持记录")
    assert items[1]["change"].startswith("今日出现否认/更正")


def test_equal_primary_titles_do_not_hide_different_other_fresh_facts(database):
    first = _seed(database, "fresh-three", "甲公司获得3亿元订单", priority=9, hour=9)
    second = _seed(database, "fresh-eight", "甲公司获得8亿元订单", priority=8, hour=9)
    _evidence(
        database, first, "https://news.example/three-progress", "甲公司披露订单进展", at=_ts(15, 11),
    )
    _evidence(
        database, second, "https://news.example/eight-progress", "甲公司披露订单进展", at=_ts(15, 11),
    )
    items = home.load_home_event_digest(now=_ts(15, 12))["highlights"]
    assert [row["event_id"] for row in items] == [first, second]
    assert [row["title"] for row in items] == ["甲公司披露订单进展", "甲公司披露订单进展"]
    assert [row["sources"][0]["url"] for row in items] == [
        "https://news.example/three-progress", "https://news.example/eight-progress",
    ]
    assert all(row["block"] == "new" for row in items)


def test_equal_primary_titles_do_not_hide_different_prior_facts(database):
    first = _report_event(
        database, "history-three", "甲公司获得3亿元订单", "https://news.example/history-three",
        at=_ts(14, 9), priority=9,
    )
    second = _report_event(
        database, "history-eight", "甲公司获得8亿元订单", "https://news.example/history-eight",
        at=_ts(14, 9), priority=8,
    )
    _evidence(
        database, first, "https://news.example/three-latest", "甲公司披露订单进展", at=_ts(15, 11),
    )
    _evidence(
        database, second, "https://news.example/eight-latest", "甲公司披露订单进展", at=_ts(15, 11),
    )
    items = home.load_home_event_digest(now=_ts(15, 12))["highlights"]
    assert [row["event_id"] for row in items] == [first, second]
    assert [row["title"] for row in items] == ["甲公司披露订单进展", "甲公司披露订单进展"]
    assert all(row["block"] == "advanced" for row in items)
    report = daily.load_daily_report("2026-10-15", now=_ts(15, 12))
    assert [row["previous"]["claim_text"] for row in _block(report, "advanced")["entries"]] == [
        "甲公司获得3亿元订单", "甲公司获得8亿元订单",
    ]


@pytest.mark.parametrize("long_field", ["primary", "fresh", "history"])
def test_read_model_headline_cap_never_proves_claim_equivalence(database, long_field):
    from db.event_core import upsert_event_evidence

    event_ids = []
    for index, amount in enumerate(("3.0", "3.00")):
        event_id = _event(database, f"clipped-{index}", f"不同事件{index}", at=_ts(14, 9), priority=9-index)
        event_ids.append(event_id)
        # Equal known prefixes after format folding, but different objects
        # beyond the daily model's 1000-character cap must not be guessed.
        long_title = f"甲公司获得{amount}亿元订单" + ("，" * 1100) + f"，交易对手为公司{index}"
        observed_at = _ts(14 if long_field == "history" else 15, 9)
        with database.get_connection() as conn:
            upsert_event_evidence(
                conn, event_id, source_uri=f"https://news.example/clipped-{index}",
                claim_key=f"clipped-{index}", claim_text=long_title,
                observed_at=observed_at, now=observed_at,
            )
        if long_field != "primary":
            _evidence(
                database, event_id, f"https://news.example/short-primary-{index}",
                f"甲公司披露利润{amount}亿元", at=_ts(15, 11),
            )
    items = home.load_home_event_digest(now=_ts(15, 12))["highlights"]
    assert [item["event_id"] for item in items] == event_ids


def _candidate(event_id, title, block, kind):
    row = {
        "claim_text": title, "observed_at": 1, "evidence_id": event_id,
        "stance": "contradicts" if kind == "denial" else "supports",
    }
    state = {
        "block": block, "fresh": [row],
        "denials": [row] if kind == "denial" else [],
        "corrections": [row] if kind else [],
        "history": [], "prior_support": [], "background_corrections": [],
    }
    return ({"title": title}, state)


@pytest.mark.parametrize(
    ("left_block", "left_kind", "right_block", "right_kind", "title"),
    [
        ("new", "", "advanced", "", "甲公司获得3.00亿元订单"),
        ("new", "", "pending", "", "甲公司计划获得3.00亿元订单"),
        ("correction", "request", "correction", "denial", "交易所要求甲公司澄清3.00亿元订单"),
    ],
)
def test_block_and_correction_kind_are_part_of_slot_identity(
    left_block, left_kind, right_block, right_kind, title,
):
    items = [
        {"event_id": "first", "block": left_block, "correction_kind": left_kind},
        {"event_id": "second", "block": right_block, "correction_kind": right_kind},
    ]
    candidates = {
        "first": _candidate("first", title, left_block, left_kind),
        "second": _candidate("second", title, right_block, right_kind),
    }
    assert home._select_distinct_items(items, candidates, 3) == items
