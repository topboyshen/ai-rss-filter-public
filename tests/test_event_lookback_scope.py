"""Scoping lookback work must not change any report evidence or display result."""

from datetime import date

import pytest

from services import event_daily_report as daily
from services import event_home_digest as home
from services import event_weekly_report as weekly
from test_event_daily_report import _block, _evidence, _event, _mapping, _merge, _report_event, _ts

DAY = "2026-10-12"
NOW = _ts(12, 20)


@pytest.fixture
def database():
    import db.database as database_module

    database_module.init_db()
    weekly.clear_weekly_cache()
    yield database_module
    weekly.clear_weekly_cache()


def _old_semantic_key(row):
    return (daily._normalize_headline(row["claim_text"]),
            daily._headline_correction_kind(row["claim_text"], row["stance"]))


def _old_order(item):
    return item["observed_at"], item["evidence_id"]


def _old_kind(item):
    return _old_semantic_key(item)[1]


def _old_index(rows, *, headline_keys=None):
    """Independent pre-optimization algorithm; intentionally ignore scope."""
    endpoints = {}
    for row in rows:
        key = _old_semantic_key(row)
        first, latest = endpoints.get(key, (row, row))
        endpoints[key] = (min(first, row, key=_old_order), max(latest, row, key=_old_order))
    result = {}
    for (headline, _kind), pair in endpoints.items():
        result[headline] = result.get(headline, ()) + pair
    return result


def _old_classify(group, lookback, *, headline_keys=None):
    """Keep the old key selection independent of the new shared helper."""
    today, prior = group["today"], group["prior"]
    keys = {daily._normalize_headline(group.get("identity_title", group["title"]))}
    keys |= {daily._normalize_headline(item["claim_text"]) for item in today}
    matched = [row for key in keys if key in lookback for row in lookback[key]]
    history = sorted({item["evidence_id"]: item for item in prior + matched}.values(), key=_old_order)
    known = {_old_semantic_key(item) for item in history}
    fresh = [item for item in today if _old_semantic_key(item) not in known]
    fresh_ids = {item["evidence_id"] for item in fresh}
    history_ids = {item["evidence_id"] for item in history}
    reposted = [item for item in today if item["evidence_id"] not in fresh_ids]
    corrections = sorted((item for item in fresh if _old_kind(item)), key=_old_order)
    denials = [item for item in corrections if _old_kind(item) == "denial"]
    cutoff = denials[-1]["observed_at"] if denials else None
    prior_support = [
        item for item in history + fresh
        if item["stance"] == "supports" and not _old_kind(item)
        and (item["evidence_id"] in history_ids or (
            cutoff is not None and item["observed_at"] < cutoff
        ))
    ]
    state = {
        "history": history, "fresh": fresh, "corrections": corrections, "denials": denials,
        "background_corrections": [
            item for item in history + reposted if _old_kind(item) == "denial"
        ],
        "prior_support": sorted(prior_support, key=_old_order),
        "pending_language": any(daily._PENDING_RE.search(item["claim_text"]) for item in fresh),
    }
    if corrections:
        state["block"] = "correction"
    elif not fresh:
        state["block"] = "repeat"
    elif state["pending_language"]:
        state["block"] = "pending"
    else:
        state["block"] = "advanced" if history else "new"
    return state


def _load(path, *, block=None, page=None):
    if path == "home":
        return home.load_home_event_digest(now=NOW)
    loader = daily.load_daily_report if path == "daily" else weekly.load_weekly_report
    return loader(DAY, now=NOW, block=block, page=page)


def _compare_old(path, monkeypatch, *, block=None, page=None):
    with monkeypatch.context() as old:
        old.setattr(daily, "_lookback_index", _old_index)
        old.setattr(daily, "_classify", _old_classify)
        weekly.clear_weekly_cache()
        expected = _load(path, block=block, page=page)
    weekly.clear_weekly_cache()
    actual = _load(path, block=block, page=page)
    assert actual == expected
    return actual


def _entries(report, path):
    if path == "home":
        return report["highlights"] + report["corrections"]
    return [item for block in report["blocks"] for item in block["entries"]]


def _seed(database, key, title, *, day=12, hour=9, priority=7, stance="supports"):
    return _report_event(
        database, key, title, f"https://news.example/{key}", at=_ts(day, hour),
        priority=priority, stance=stance,
    )


def _seed_case(database, case):
    if case == "cross_root_repost":
        _seed(database, "old", "甲公司否认订单传闻", day=11)
        _seed(database, "copy", "甲公司否认订单传闻", priority=1)
        return "repeat"
    if case == "same_headline_changed_stance":
        _seed(database, "old", "甲公司获得项目订单", day=11)
        _seed(database, "deny", "甲公司获得项目订单", stance="contradicts", priority=1)
        return "correction"
    if case == "same_link_correction":
        event = _seed(database, "same-url", "甲公司获得项目订单", day=11)
        _evidence(database, event, "https://news.example/same-url", "甲公司否认订单传闻",
                  at=_ts(12, 10), stance="contradicts")
        return "correction"
    if case == "request":
        _seed(database, "prior-request", "交易所要求甲公司澄清", day=10)
        _seed(database, "copied-request", "交易所要求甲公司澄清", priority=8)
        _seed(database, "fresh-request", "交易所要求乙公司澄清", priority=8)
        return "correction"
    if case == "pending":
        _seed(database, "old-plan", "甲公司计划建设工厂", day=11)
        _seed(database, "copied-plan", "甲公司计划建设工厂", priority=8)
        _seed(database, "fresh-plan", "乙公司计划投资5亿元", priority=8)
        return "pending"
    if case == "empty_normalized_identity":
        event = _event(database, "empty-identity", "甲公司当前展示标题", at=_ts(10, 9))
        _evidence(database, event, "https://news.example/empty", "！！！", at=_ts(10, 9))
        _seed(database, "empty-external", "。", day=11, stance="contradicts")
        _evidence(database, event, "https://news.example/empty-update", "甲公司披露项目进展",
                  at=_ts(12, 9))
        return "advanced"
    if case == "mutable_metadata":
        event = _event(database, "mutable", "乙公司相同展示标题", at=_ts(10, 9))
        _evidence(database, event, "https://news.example/identity", "甲公司原始项目",
                  at=_ts(10, 9))
        _seed(database, "external-denial", "甲公司原始项目", day=11, stance="contradicts")
        _evidence(database, event, "https://news.example/update", "甲公司披露新增进展",
                  at=_ts(12, 9))
        _seed(database, "different", "乙公司相同展示标题", priority=7)
        return "advanced"
    if case == "merged_roots":
        root = _seed(database, "root", "宁德时代披露电池项目", day=10)
        member = _seed(database, "member", "宁德时代披露电池项目", day=11)
        _merge(database, member, root, at=_ts(11, 12))
        _evidence(database, member, "https://news.example/merged-update", "宁德时代电池正式投产",
                  at=_ts(12, 9))
        _mapping(database, root, "300750", "宁德时代", "direct", at=_ts(12, 9))
        return "advanced"
    if case == "folded_groups":
        first = _seed(database, "fold-a", "甲公司项目进展", day=10)
        second = _seed(database, "fold-b", "甲公司项目进展", day=11)
        _evidence(database, first, "https://news.example/fold-update", "甲公司披露项目进展",
                  at=_ts(12, 9))
        _evidence(database, second, "https://news.example/fold-denial", "甲公司否认订单传闻",
                  at=_ts(12, 10))
        _seed(database, "external", "甲公司否认订单传闻", day=11)
        return "advanced"
    raise AssertionError(case)


@pytest.mark.parametrize("path", ["daily", "home", "weekly"])
@pytest.mark.parametrize("case", [
    "cross_root_repost", "same_headline_changed_stance", "same_link_correction", "request", "pending",
    "mutable_metadata", "empty_normalized_identity", "merged_roots", "folded_groups",
])
def test_real_reports_equal_old_full_index(database, monkeypatch, path, case):
    expected_block = _seed_case(database, case)
    report = _compare_old(path, monkeypatch)
    entries = _entries(report, path)
    if expected_block == "repeat":
        assert entries == []
    else:
        assert any(item["block"] == expected_block for item in entries)
    if case == "same_link_correction":
        source = entries[0]["sources"][0]
        assert source["url"] == "https://news.example/same-url"
        assert source["claim_text"] == "甲公司否认订单传闻"
        if path != "home":
            assert source["same_link_previous"] == "甲公司获得项目订单"
    if case in {"mutable_metadata", "empty_normalized_identity"}:
        assert len(entries) == (2 if case == "mutable_metadata" else 1)
        advanced = next(item for item in entries if item["block"] == "advanced")
        if path == "home":
            assert advanced["background_note"] == "此前已有否认／更正，新增报道不代表推翻"
        else:
            assert advanced["previous"]["time_label"] == "2026-10-11 09:00"
    if case == "merged_roots":
        assert entries[0]["companies"] == ["宁德时代"]
        assert entries[0]["sources"][0]["url"] == "https://news.example/merged-update"


def _row(identifier, title, at, stance="supports"):
    return {"evidence_id": identifier, "claim_text": title, "observed_at": at,
            "stance": stance, "event_id": f"root-{identifier}"}


@pytest.mark.parametrize("scope", [None, set(), {"甲公司获得订单"}, {""}, {"交易所要求乙公司澄清"}])
def test_scoped_index_preserves_each_meanings_first_last_and_ties(monkeypatch, scope):
    rows = [
        _row("s-last", "甲公司获得订单", 8), _row("d-last", "甲公司获得订单", 7, "contradicts"),
        _row("s-first-b", "甲公司获得订单", 1), _row("d-first", "甲公司获得订单", 3, "contradicts"),
        _row("s-first-a", "甲公司获得订单", 1), _row("empty", "！！！", 5),
        _row("q-last", "交易所要求乙公司澄清", 9), _row("q-first", "交易所要求乙公司澄清", 2),
        _row("ignored", "无关公司否认收购传闻", 6),
    ]
    expected = {key: value for key, value in _old_index(rows).items()
                if scope is None or key in scope}
    calls = []
    original = daily._correction_kind

    def counted(row):
        calls.append(row["evidence_id"])
        return original(row)

    monkeypatch.setattr(daily, "_correction_kind", counted)
    actual = daily._lookback_index(rows, headline_keys=scope)
    assert actual == expected
    assert calls == [row["evidence_id"] for row in rows
                     if scope is None or daily._normalize_headline(row["claim_text"]) in scope]
    if scope == {"甲公司获得订单"}:
        assert [row["evidence_id"] for row in actual["甲公司获得订单"]] == [
            "s-first-a", "s-last", "d-first", "d-last",
        ]


@pytest.mark.parametrize("identity", ["", "！！！"])
def test_empty_identity_is_not_replaced_with_mutable_display_title(identity):
    rows = [_row("empty-prior", "。", 1, "contradicts"),
            _row("display-prior", "当前展示标题", 2)]
    group = {"identity_title": identity, "title": "当前展示标题", "prior": [],
             "today": [_row("fresh", "甲公司披露项目进展", 3)]}
    scoped = daily._lookback_index(rows, headline_keys=daily._matching_headline_keys(group))
    actual = daily._classify(group, scoped)
    assert actual == _old_classify(group, _old_index(rows))
    assert [row["evidence_id"] for row in actual["history"]] == ["empty-prior"]
    assert actual["block"] == "advanced"
    assert len(actual["background_corrections"]) == 1


def test_precomputed_group_keys_do_not_cross_contaminate_other_groups_history():
    rows = [_row("old-a", "甲公司获得订单", 1, "contradicts"),
            _row("old-b", "交易所要求乙公司澄清", 2)]
    groups = [
        {"root_ids": ["a"], "priority": 8, "title": "甲公司最新标题",
         "identity_title": "甲公司获得订单", "prior": [],
         "today": [_row("new-a", "甲公司披露项目进展", 3)]},
        {"root_ids": ["b"], "priority": 7, "title": "乙公司最新标题",
         "identity_title": "交易所要求乙公司澄清", "prior": [],
         "today": [_row("new-b", "乙公司否认收购传闻", 4)]},
    ]
    index = _old_index(rows)
    keys = [daily._matching_headline_keys(group) for group in groups]
    actual = daily._cheap_daily_items(groups, index, group_keys=keys)
    assert actual == daily._cheap_daily_items(groups, index)
    for group in groups:
        identity = group["root_ids"][0]
        state = actual[1][identity][1]
        assert state == _old_classify(group, index)
        assert [row["evidence_id"] for row in state["history"]] == [f"old-{identity}"]


@pytest.mark.parametrize("path", ["daily", "home", "weekly"])
@pytest.mark.parametrize(("before", "after", "expected_count"), [
    ("≤5亿元", "≥5亿元", 1), ("＜1万亿", "≥1万亿", 1), ("1.5亿元", "15亿元", 1),
    ("+5%", "-5%", 1), ("5万元", "5亿元", 1),
    # The baseline daily identity already folds these symbols, unlike the
    # later homepage near-duplicate selector. This optimization must not
    # silently change that separate, pre-existing semantic boundary.
    ("±5亿元", "∓5亿元", 0), ("≈5亿元", "5亿元", 0),
])
def test_numeric_and_symbol_identity_results_are_unchanged(
    database, monkeypatch, path, before, after, expected_count,
):
    _seed(database, "amount-before", f"甲公司披露金额{before}", day=11)
    _seed(database, "amount-after", f"甲公司披露金额{after}")
    report = _compare_old(path, monkeypatch)
    entries = _entries(report, path)
    assert len(entries) == expected_count
    assert all(item["block"] == "new" for item in entries)


@pytest.mark.parametrize("path", ["daily", "home", "weekly"])
def test_unrelated_history_skips_correction_work_in_every_report_path(database, monkeypatch, path):
    ignored = ["无关甲公司否认收购", "无关乙公司否认订单", "交易所要求无关丙公司澄清"]
    for number, title in enumerate(ignored):
        _seed(database, f"ignored-{number}", title, day=11)
    _seed(database, "today", "宁德时代发布新电池")
    expected = _compare_old(path, monkeypatch)
    calls = []
    original = daily._correction_kind

    def counted(row):
        calls.append(row["claim_text"])
        return original(row)

    monkeypatch.setattr(daily, "_correction_kind", counted)
    weekly.clear_weekly_cache()
    report = _load(path)
    assert report == expected
    assert _entries(report, path)[0]["title"] == "宁德时代发布新电池"
    assert not set(ignored) & set(calls)


@pytest.mark.parametrize("path", ["daily", "home", "weekly"])
def test_no_today_evidence_preserves_empty_result_without_any_lookback_semantic_scan(database, monkeypatch, path):
    title = "甲公司否认收购传闻"
    _seed(database, "old-only", title, day=11)
    report = _compare_old(path, monkeypatch)
    assert _entries(report, path) == []
    calls = []
    original = daily._correction_kind

    def counted(row):
        calls.append(row["claim_text"])
        return original(row)

    monkeypatch.setattr(daily, "_correction_kind", counted)
    weekly.clear_weekly_cache()
    assert _load(path) == report
    assert calls == []


@pytest.mark.parametrize("path", ["daily", "home"])
def test_candidate_headline_keys_are_normalized_once_per_group(database, monkeypatch, path):
    _seed(database, "once-a", "甲公司发布新产品")
    _seed(database, "once-b", "乙公司发布新产品")
    normalize_calls = []
    original_keys = daily._matching_headline_keys
    original_normalize = daily._normalized_key

    def counted_normalize(text):
        normalize_calls.append(text)
        return original_normalize(text)

    def counted_keys(group):
        with monkeypatch.context() as phase:
            phase.setattr(daily, "_normalized_key", counted_normalize)
            return original_keys(group)

    monkeypatch.setattr(daily, "_matching_headline_keys", counted_keys)
    report = _load(path)
    assert {item["title"] for item in _entries(report, path)} == {"甲公司发布新产品", "乙公司发布新产品"}
    assert sorted(normalize_calls) == ["乙公司发布新产品"] * 2 + ["甲公司发布新产品"] * 2


@pytest.mark.parametrize("path", ["daily", "home", "weekly"])
@pytest.mark.parametrize("cap", ["today", "lookback"])
def test_raw_read_truncation_and_coverage_equal_old_algorithm(database, monkeypatch, path, cap):
    for number in range(4):
        _seed(database, f"old-{number}", f"无关公司{number}否认收购", day=11)
        _seed(database, f"new-{number}", f"公司{number}发布新产品")
    module = weekly if path == "weekly" else daily
    key = "MAX_WEEK_EVIDENCE_ROWS" if path == "weekly" else "MAX_DAY_EVIDENCE_ROWS"
    monkeypatch.setattr(module, key if cap == "today" else "MAX_LOOKBACK_EVIDENCE_ROWS", 2)
    report = _compare_old(path, monkeypatch)
    assert report["data_state"] == "truncated"
    assert "证据不足" in report["data_state_label"]


@pytest.mark.parametrize("path", ["daily", "weekly"])
def test_page_two_keeps_all_sources_associations_and_navigation(database, monkeypatch, path):
    for number in range(23):
        event = _seed(database, f"page-{number}", f"宁德时代披露项目{number}，金额{number + 10}亿元",
                      priority=9 - number / 100)
        _mapping(database, event, "300750", "宁德时代", "direct", at=_ts(12, 9))
    report = _compare_old(path, monkeypatch, block="new", page=2)
    block = _block(report, "new")
    assert len(block["entries"]) == 3
    assert [item["priority"] for item in block["entries"]] == [8.8, 8.79, 8.78]
    assert all(item["companies"] == ["宁德时代"] for item in block["entries"])
    assert {item["sources"][0]["url"] for item in block["entries"]} == {
        f"https://news.example/page-{number}" for number in range(20, 23)
    }
    assert report["overview_url"] == f"/dashboard/events/{path}?{'date' if path == 'daily' else 'week'}={DAY}"
    assert report["pagination_block"] == "new"


def test_prepared_candidates_and_history_equal_old_algorithm_before_display_cap(database, monkeypatch):
    _seed_case(database, "mutable_metadata")
    _seed_case(database, "folded_groups")
    for number in range(25):
        _seed(database, f"candidate-{number}", f"公司{number}发布新品", priority=1 + number / 10)
    with database.get_connection() as conn:
        with monkeypatch.context() as old:
            old.setattr(daily, "_lookback_index", _old_index)
            old.setattr(daily, "_classify", _old_classify)
            expected = daily.prepare_daily_report(conn, date(2026, 10, 12), NOW)
        actual = daily.prepare_daily_report(conn, date(2026, 10, 12), NOW)
    assert actual == expected
    assert len(actual[2]) > 20
