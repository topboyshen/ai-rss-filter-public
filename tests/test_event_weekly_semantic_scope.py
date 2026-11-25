"""Weekly semantic work is scoped without changing any reconstructed result.

All evidence is synthetic and uses the temporary database from conftest. The
reference keeps the pre-optimization full index and duplicated daily/weekly
classification, so equality is checked against an independent old algorithm.
"""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from services import event_daily_report as daily
from services import event_weekly_report as weekly
from test_event_daily_report import _block, _evidence, _mapping, _ts
from test_event_lookback_scope import (
    _old_classify,
    _old_index,
    _old_semantic_key,
    _seed,
    _seed_case,
)

WEEK = "2026-10-12"
NOW = _ts(12, 20)


@pytest.fixture
def database():
    import db.database as module

    module.init_db()
    weekly.clear_weekly_cache()
    yield module
    weekly.clear_weekly_cache()


def _reference_week(group, lookback, *, headline_keys=None):
    """Freeze the old weekly postprocessing, including its strict time cutoff."""
    state = _old_classify(group, lookback)
    known = {_old_semantic_key(row) for row in state["history"]}
    fresh = [row for row in group["today"] if _old_semantic_key(row) not in known]
    first = {}
    for row in sorted(fresh, key=daily._ingest_order):
        first.setdefault(_old_semantic_key(row), row)
    state["fresh"] = list(first.values())
    state["corrections"] = [row for row in state["fresh"] if _old_semantic_key(row)[1]]
    state["denials"] = [
        row for row in state["corrections"] if _old_semantic_key(row)[1] == "denial"
    ]
    if state["denials"]:
        cutoff = state["denials"][-1]["observed_at"]
        state["prior_support"] = sorted(
            [row for row in state["history"] + fresh
             if row["stance"] == "supports" and not _old_semantic_key(row)[1]
             and row["observed_at"] < cutoff],
            key=daily._ingest_order,
        )
    state["background_corrections"] = sorted(
        [row for row in state["history"] + group["today"]
         if _old_semantic_key(row) in known and _old_semantic_key(row)[1] == "denial"],
        key=daily._ingest_order,
    )
    state["pending_language"] = any(
        daily._PENDING_RE.search(row["claim_text"]) for row in state["fresh"]
    )
    if state["corrections"]:
        state["block"] = "correction"
    elif not state["fresh"]:
        state["block"] = "repeat"
    elif state["pending_language"]:
        state["block"] = "pending"
    else:
        state["block"] = "advanced" if state["history"] else "new"
    return state


def _load(*, block=None, page=None):
    return weekly.load_weekly_report(WEEK, now=NOW, block=block, page=page)


def _compare_reference(monkeypatch, *, block=None, page=None):
    with monkeypatch.context() as baseline:
        baseline.setattr(daily, "_lookback_index", _old_index)
        baseline.setattr(weekly, "_classify_week", _reference_week)
        expected = _load(block=block, page=page)
    actual = _load(block=block, page=page)
    assert actual == expected
    return actual


@pytest.mark.parametrize("case", [
    "cross_root_repost", "same_headline_changed_stance", "same_link_correction",
    "request", "pending", "mutable_metadata", "empty_normalized_identity",
    "merged_roots", "folded_groups",
])
def test_real_weekly_build_matches_preoptimization_complete_report(database, monkeypatch, case):
    expected_block = _seed_case(database, case)
    report = _compare_reference(monkeypatch)
    entries = [item for block in report["blocks"] for item in block["entries"]]
    if expected_block == "repeat":
        assert entries == [] and report["repeat_event_count"] == 1
    else:
        assert any(item["block"] == expected_block for item in entries)
    if case in {"mutable_metadata", "empty_normalized_identity"}:
        advanced = next(item for item in entries if item["block"] == "advanced")
        assert advanced["previous"]["time_label"] == "2026-10-11 09:00"
        assert any("此前已有否认/更正" in note for note in advanced["uncertainties"])
    if case == "same_link_correction":
        assert entries[0]["sources"][0]["same_link_previous"] == "甲公司获得项目订单"
    if case == "merged_roots":
        assert entries[0]["companies"] == ["宁德时代"]


@pytest.mark.parametrize("paging", [(None, None), ("new", 2)])
def test_real_overview_and_second_page_keep_full_evidence_and_hidden_groups(
    database, monkeypatch, paging,
):
    for index in range(23):
        event_id = _seed(
            database, f"page-{index}", f"宁德时代披露项目{index}，金额{index + 10}亿元",
            priority=9 - index / 100,
        )
        _mapping(database, event_id, "300750", "宁德时代", "direct", at=_ts(12, 9))
    _seed(database, "low-old", "低分公司披露经营数据", day=11, priority=1)
    _seed(database, "low-repost", "低分公司披露经营数据", priority=1)
    _seed(database, "low-new", "低分公司披露新增数据", priority=1)
    _seed(database, "low-denial", "低分公司否认订单传闻", priority=1)
    report = _compare_reference(monkeypatch, block=paging[0], page=paging[1])
    new = _block(report, "new")
    assert new["hidden_low_priority"] == 1 and new["total"] == 24
    assert report["repeat_event_count"] == report["repeat_evidence_count"] == 1
    assert all(item["companies"] == ["宁德时代"] for item in new["entries"])
    if paging[0]:
        assert len(new["entries"]) == 3 and new["pagination"]["page"] == 2
        assert new["pagination"]["start_index"] == 21
        assert new["pagination"]["end_index"] == 23
        assert {item["sources"][0]["url"] for item in new["entries"]} == {
            f"https://news.example/page-{index}" for index in range(20, 23)
        }
        assert report["overview_url"] == f"/dashboard/events/weekly?week={WEEK}"
    else:
        assert len(new["entries"]) == 20
        assert _block(report, "correction")["entries"][0]["priority"] == 1


@pytest.mark.parametrize("before,after,changes", [
    ("≤5亿元", "≥5亿元", 1), ("＜1万亿", "≥1万亿", 1),
    ("1.5亿元", "15亿元", 1), ("+5%", "-5%", 1), ("5万元", "5亿元", 1),
    # Preserve the existing daily/weekly identity rule, not the different
    # homepage near-duplicate selector's treatment of these qualifiers.
    ("±5亿元", "∓5亿元", 0), ("≈5亿元", "5亿元", 0),
])
def test_numeric_identity_classification_matches_full_index(database, monkeypatch, before, after, changes):
    _seed(database, "prior-number", f"甲公司披露金额{before}", day=11)
    _seed(database, "new-number", f"甲公司披露金额{after}")
    report = _compare_reference(monkeypatch)
    assert _block(report, "new")["total"] == changes
    assert report["repeat_event_count"] == 1 - changes


def _row(identifier, title, at, stance="supports"):
    return {"evidence_id": identifier, "claim_text": title, "observed_at": at,
            "stance": stance, "event_id": f"event-{identifier}"}


def _state_case(case):
    title = "甲公司披露订单进展"
    prior = [_row("h", title, 1)]
    today = [_row("t", "甲公司新增投产进展", 4)]
    lookback = []
    if case == "fresh_duplicates":
        prior = []
        today = [_row("z", title, 4), _row("a", title, 3), _row("b", title, 3)]
    elif case == "same_time_support_denial":
        prior = []
        today = [_row("z-support", title, 4), _row("a-denial", title, 4, "contradicts")]
    elif case == "old_denial_repost":
        prior = [_row("h", title, 1, "contradicts")]
        today = [_row("repeat", title, 5, "contradicts"), *today]
    elif case == "support_to_denial":
        today = [_row("denial", title, 4, "contradicts")]
    elif case == "request_and_denial":
        title = "交易所要求甲公司澄清"
        prior = []
        today = [_row("request", title, 3), _row("denial", title, 4, "contradicts")]
    elif case == "same_id_endpoints":
        lookback = [prior[0], prior[0], _row("other", title, 2, "contradicts")]
    elif case == "prior_support_later_than_denial":
        prior = [_row("h", title, 7)]
        today = [_row("denial", title, 4, "contradicts")]
    elif case == "empty_identity":
        title = "！！！"
        prior = []
        lookback = [_row("empty", "。", 1, "contradicts")]
    elif case == "same_key_first_and_last":
        lookback = [_row("s1", title, 1), _row("d", title, 2, "contradicts"),
                    _row("s2", title, 3)]
    else:
        raise AssertionError(case)
    group = {"title": "当前展示标题", "identity_title": title, "prior": prior, "today": today}
    return group, _old_index(lookback)


@pytest.mark.parametrize("case", [
    "fresh_duplicates", "same_time_support_denial", "old_denial_repost", "support_to_denial",
    "request_and_denial", "same_id_endpoints", "prior_support_later_than_denial",
    "empty_identity", "same_key_first_and_last",
])
def test_weekly_state_matches_original_including_duplicate_and_endpoint_order(case):
    group, lookback = _state_case(case)
    assert weekly._classify_week(group, lookback) == _reference_week(group, lookback)
    if case == "same_time_support_denial":
        assert weekly._classify_week(group, lookback)["prior_support"] == []
    if case == "fresh_duplicates":
        assert [row["evidence_id"] for row in weekly._classify_week(group, lookback)["fresh"]] == ["a"]


def test_weekly_classification_computes_history_kinds_and_today_keys_once(monkeypatch):
    group, lookback = _state_case("old_denial_repost")
    expected = _reference_week(group, lookback)
    history_calls = Counter()
    semantic_calls = Counter()
    history_kind = weekly._history_correction_kind
    semantic_key = daily._semantic_key

    def count_history(row):
        history_calls[row["evidence_id"]] += 1
        return history_kind(row)

    def count_semantic(row):
        semantic_calls[row["evidence_id"]] += 1
        return semantic_key(row)

    monkeypatch.setattr(weekly, "_history_correction_kind", count_history)
    monkeypatch.setattr(daily, "_semantic_key", count_semantic)
    assert weekly._classify_week(group, lookback) == expected
    assert history_calls == Counter({"h": 1})
    assert semantic_calls == Counter({"repeat": 1, "t": 1})


@pytest.mark.parametrize("current_count", [0, 3])
def test_unrelated_lookback_never_enters_semantic_classification(database, monkeypatch, current_count):
    for index in range(48):
        _seed(database, f"unrelated-{index}", f"无关公司{index}否认订单传闻", day=11)
    for index in range(current_count):
        _seed(database, f"fresh-{index}", f"本周公司{index}新增项目")
    expected = _compare_reference(monkeypatch)
    seen = []
    original = daily._correction_kind

    def counted(row):
        seen.append(row["claim_text"])
        return original(row)

    monkeypatch.setattr(daily, "_correction_kind", counted)
    assert _load() == expected
    assert not any(title.startswith("无关公司") for title in seen)
    if not current_count:
        assert seen == []


def test_matching_keys_are_precomputed_once_for_every_folded_group_before_display_cap(database, monkeypatch):
    _seed(database, "same-a", "甲公司项目进展", priority=7)
    _seed(database, "same-b", "甲公司项目进展", priority=8)
    _seed(database, "low", "低分公司项目进展", priority=1)
    _seed(database, "empty", "！！！", priority=1)
    for index in range(23):
        _seed(database, f"large-{index}", f"公司{index}新增项目", priority=9)
    calls = []
    original = daily._matching_headline_keys

    def counted(group):
        calls.append(tuple(group["root_ids"]))
        return original(group)

    monkeypatch.setattr(daily, "_matching_headline_keys", counted)
    report = _load()
    assert report["event_group_count"] == len(calls) == 26
    assert report["duplicate_groups_folded"] == 1
    assert len(set(calls)) == len(calls)
    assert _block(report, "new")["hidden_low_priority"] == 2


def test_real_build_cache_does_not_hide_new_stance_in_later_request(database, monkeypatch):
    event = _seed(database, "support", "甲公司披露订单进展")
    first = _compare_reference(monkeypatch)
    assert _block(first, "new")["total"] == 1
    _evidence(database, event, "https://news.example/later-denial", "甲公司披露订单进展",
              at=_ts(12, 10), stance="contradicts")
    second = _compare_reference(monkeypatch)
    assert _block(second, "correction")["entries"][0]["correction_kind"] == "denial"
    assert _block(first, "new")["total"] == 1
    assert daily._REPORT_TEXT_CACHE.get() is None


def test_parallel_weekly_requests_have_independent_semantic_context(database, monkeypatch):
    _seed_case(database, "same_headline_changed_stance")
    expected = _compare_reference(monkeypatch)
    barrier = Barrier(2)
    original = weekly._classify_week

    def overlap(group, lookback, **kwargs):
        barrier.wait(timeout=10)
        result = original(group, lookback, **kwargs)
        assert daily._REPORT_TEXT_CACHE.get() is not None
        return result

    monkeypatch.setattr(weekly, "_classify_week", overlap)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_load) for _ in range(2)]
        reports = [future.result(timeout=20) for future in futures]
    assert reports == [expected, expected]
    reports[0]["blocks"][0]["entries"].append({"event_id": "caller-local-mutation"})
    assert reports[0] != reports[1]
    assert reports[1] == expected
    assert daily._REPORT_TEXT_CACHE.get() is None
