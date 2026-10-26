"""Frozen pre-optimization card helpers and deterministic work-count checks."""

import copy
import re
import unicodedata
from itertools import product
from random import Random
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest

from services import event_daily_report as daily
from services import provenance_shadow as provenance
from utils.helpers import plain_text_from_markup


def _legacy_normalize_headline(text):
    """Frozen baseline always translates signs after markup removal and NFKC."""
    value = unicodedata.normalize("NFKC", plain_text_from_markup(text or ""))
    value = value.translate(daily._SIGN_TRANSLATION).casefold()
    return " ".join(daily._KEY_TOKEN_RE.findall(value))


def _legacy_resolve_publisher(source_uri, source_name="", *, config=None):
    registry = provenance._normalize_registry(config)
    host = provenance._uri_host(source_uri)
    domain_matches = [
        (domain, metadata) for domain, metadata in registry["domains"].items()
        if host == domain or host.endswith("." + domain)
    ]
    if domain_matches:
        _match, metadata = max(domain_matches, key=lambda item: (len(item[0]), item[0]))
        group, source_class, scopes = metadata
        return dict(publisher_group=group, publisher_basis="registry_domain",
                    source_class=source_class, first_party_scopes=list(scopes))
    route_matches = []
    for path in provenance._trusted_route_candidates(source_uri, source_name):
        route_matches.extend(
            (route, metadata) for route, metadata in registry["routes"].items()
            if provenance._path_matches(path, route)
        )
    if route_matches:
        _match, metadata = max(route_matches, key=lambda item: (len(item[0]), item[0]))
        group, source_class, scopes = metadata
        return dict(publisher_group=group, publisher_basis="registry_route",
                    source_class=source_class, first_party_scopes=list(scopes))
    publisher_domain = provenance._registrable_domain(host)
    if publisher_domain:
        domain_group = f"domain:{publisher_domain}"
        if len(domain_group) > 128:
            domain_group = "domainhash:" + provenance._fingerprint({"domain": publisher_domain})
        return dict(publisher_group=domain_group, publisher_basis="registrable_domain",
                    source_class="unknown", first_party_scopes=[])
    return dict(publisher_group="unknown", publisher_basis="unknown",
                source_class="unknown", first_party_scopes=[])


def _legacy_outlet_key(item):
    identity = _legacy_resolve_publisher(
        daily._publisher_url(item["source_uri"]), daily._publisher_url(item["source_name"]),
        config=daily._PUBLISHER_REGISTRY,
    )
    key = str(identity["publisher_group"])
    return "" if key == "unknown" else key


def _legacy_publisher_summary(items):
    identified = [(_legacy_outlet_key(item), item["source_uri"]) for item in items]
    outlets = {key for key, _url in identified}
    unknown_links = {url for key, url in identified if not key}
    return len(outlets - {""}), len(unknown_links)


def _legacy_source_views(items, history, *, primary_source_id=""):
    by_url = {}
    for item in items:
        by_url.setdefault(item["source_uri"], []).append(item)

    def rank(item):
        return (item["evidence_id"] != primary_source_id, *daily._source_rank(item))

    chosen = [min(records, key=rank) for records in by_url.values()]
    chosen.sort(key=rank)
    views = []
    for item in chosen[:daily.MAX_SOURCES_PER_ITEM]:
        earlier = [
            row for row in by_url[item["source_uri"]] + history
            if row["source_uri"] == item["source_uri"]
            and row["observed_at"] <= item["observed_at"]
            and _legacy_normalize_headline(row["claim_text"]) != _legacy_normalize_headline(item["claim_text"])
        ]
        views.append({
            "url": daily._safe_external_url(item["source_uri"]),
            "source_name": item["source_name"], "claim_text": item["claim_text"],
            "stance": item["stance"], "correction_kind": daily._correction_kind(item),
            "outlet_known": bool(_legacy_outlet_key(item)),
            "same_link_previous": earlier[-1]["claim_text"] if earlier else "",
            "published_label": daily._published_label(item),
            "observed_label": f"入库 {daily._clock_label(item['observed_at'])}",
        })
    return views, max(0, len(chosen) - daily.MAX_SOURCES_PER_ITEM)


def _legacy_is_named(name, texts, known_names):
    target = daily._compact(name)
    if not target:
        return False
    longer = [known for known in known_names if len(known) > len(target) and target in known]
    for text in texts:
        compact = daily._compact(text)
        if target not in compact or not daily.is_direct_security_mention(text, name):
            continue
        covered = [
            (match.start(), match.end()) for known in longer
            for match in re.finditer(re.escape(known), compact)
        ]
        for match in re.finditer(re.escape(target), compact):
            if not any(a <= match.start() and match.end() <= b for a, b in covered):
                return True
    return False


def _legacy_association_view(group, known_names):
    texts = group.get(
        "association_texts", [group["title"]] + [item["claim_text"] for item in group["today"]],
    )
    companies, rule_ids = [], set()
    for row in group["associations"]:
        name, relation = row["name"].strip(), row["relation_type"]
        if relation in daily._COMPANY_RELATIONS and _legacy_is_named(name, texts, known_names):
            if name not in companies:
                companies.append(name)
        elif relation in daily._COMPANY_RELATIONS or relation in daily._RULE_RELATIONS:
            rule_ids.add(row["security_id"])
    company_ids = {
        row["security_id"] for row in group["associations"] if row["name"].strip() in companies
    }
    return {"companies": companies[:5], "rule_association_count": len(rule_ids - company_ids)}


def _legacy_uncertainties(group, state):
    notes = [daily._publisher_note(*_legacy_publisher_summary(group["today"] + state["history"]))]
    if state["pending_language"] and state["block"] != "correction":
        notes.append("标题含计划/传闻/预期措辞，事项尚未发生或未经确认")
    if state["block"] == "correction":
        notes.extend(daily._correction_notes(state))
    if state["background_corrections"]:
        when = daily._clock_label(state["background_corrections"][-1]["observed_at"])
        notes.append(f"此前已有否认/更正记录（{when}，仅作背景），新的报道不推翻该否认")
    return notes


def _legacy_item_view(group, state, known_names, *, period_label="今日", primary_source_id=""):
    sources, more = _legacy_source_views(
        group["today"], state["history"], primary_source_id=primary_source_id,
    )
    identified, _unknown = _legacy_publisher_summary(group["today"] + state["history"])
    previous = state["history"][-1] if state["history"] else None
    earliest = state["history"][0] if len(state["history"]) > 1 else None
    return {
        "block": state["block"],
        "correction_kind": "denial" if state["denials"] else "request" if state["corrections"] else "",
        "event_id": group["root_ids"][0], "title": group["title"],
        "event_type": group["event_type"], "priority": group["priority"],
        "priority_label": f"{group['priority']:.1f}",
        "change": daily._change_text(group, state, period_label=period_label),
        "previous": None if previous is None else {
            "claim_text": previous["claim_text"], "time_label": daily._clock_label(previous["observed_at"]),
        },
        "earliest": None if earliest is None else {
            "claim_text": earliest["claim_text"], "time_label": daily._clock_label(earliest["observed_at"]),
        },
        "sources": sources, "sources_more": more, "report_count": len(group["today"]),
        "merged_group_count": len(group["root_ids"]),
        "uncertainties": _legacy_uncertainties(group, state),
        "next_check": daily._next_check(state, identified),
        "latest_at": max(item["observed_at"] for item in group["today"]),
        **_legacy_association_view(group, known_names),
    }


def _row(number, claim="测试公司发布公告", *, url=None, at=None, stance="supports", source=""):
    return dict(evidence_id=f"e{number}", event_id="event", claim_text=claim, stance=stance,
                source_uri=url or f"https://news.example/{number}", source_name=source,
                observed_at=100 + number if at is None else at, announcement_at=None)


@pytest.mark.parametrize("url,source", [
    ("https://www.cls.cn/detail/123", ""),
    ("https://news.example.com.cn/a", ""),
    ("http://rsshub:1200/cls/telegraph", ""),
    ("http://localhost:1200/wallstreetcn/live", ""),
    ("https://evil.example/cls/telegraph", "http://rsshub:1200/cls/telegraph"),
    ("https://evil.example/cls/telegraph", "/cls/telegraph"),
    ("https://user:password@www.cls.cn/a", ""),
    ("https://www.cls.cn:invalid/a", ""),
    ("https://www.cls.cn/%2e%2e/a", ""),
    ("https://www.cls.cn/\n/a", ""),
    ("javascript:alert(1)", ""),
    ("", "http://rsshub:1200/cls/telegraph"),
])
def test_prepared_publisher_preserves_default_and_safe_outlet_identity(url, source):
    config = daily._PUBLISHER_REGISTRY
    registry = provenance._normalize_registry(config)
    frozen = copy.deepcopy(registry)
    expected = _legacy_resolve_publisher(url, source, config=config)
    assert provenance.resolve_publisher(url, source, config=config) == expected
    assert provenance._resolve_publisher_with_registry(url, source, registry=registry) == expected
    assert registry == frozen
    row = _row(1, url=url, source=source)
    # _row supplies a default for empty URLs; compare exactly the same row.
    with daily._report_text_cache():
        assert daily._outlet_key(row) == _legacy_outlet_key(row)


def test_publisher_registry_is_lazy_fresh_nested_and_cleared_after_failure(monkeypatch):
    original, registries = daily._normalize_publisher_registry, []

    def counted(config):
        value = original(config)
        registries.append(value)
        return value

    monkeypatch.setattr(daily, "_normalize_publisher_registry", counted)
    row = _row(1)
    with daily._report_text_cache():
        assert registries == []
    with daily._report_text_cache():
        daily._publisher_summary([row] * 20)
        assert len(registries) == 1
        with pytest.raises(RuntimeError):
            with daily._report_text_cache():
                daily._outlet_key(row)
                raise RuntimeError("nested")
        daily._outlet_key(row)
        assert len(registries) == 2
    replacement = {"rules": [{"publisher_group": "replacement", "domains": ["news.example"]}]}
    monkeypatch.setattr(daily, "_PUBLISHER_REGISTRY", replacement)
    with daily._report_text_cache():
        assert daily._outlet_key(row) == "replacement"
    assert len(registries) == 3
    assert len({id(value) for value in registries}) == 3
    assert daily._REPORT_PUBLISHER_STATE.get() is None


def test_parallel_reports_have_distinct_publisher_registries(monkeypatch):
    original, registries, lock, barrier = daily._normalize_publisher_registry, [], Lock(), Barrier(2)

    def counted(config):
        value = original(config)
        with lock:
            registries.append(value)
        return value

    def request():
        with daily._report_text_cache():
            first = daily._outlet_key(_row(1))
            barrier.wait(timeout=5)
            return first, daily._outlet_key(_row(2))

    monkeypatch.setattr(daily, "_normalize_publisher_registry", counted)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(request) for _ in range(2)]
        answers = [future.result(timeout=10) for future in futures]
    assert answers[0] == answers[1]
    assert len(registries) == 2 and registries[0] is not registries[1]


@pytest.mark.parametrize("primary", ["", "e1", "e3"])
def test_source_view_preserves_order_corrections_and_last_list_history(primary):
    url = "https://news.example/revision"
    rows = [
        _row(1, "测试公司计划扩大产能", url=url, at=300),
        _row(2, "测试公司否认扩大产能", url=url, at=200, stance="contradicts"),
        _row(3, "测试公司扩大产能", at=500),
        *[_row(number, f"独立新闻{number}") for number in range(4, 11)],
    ]
    history = [
        _row(20, "按时间最新的旧标题", url=url, at=190),
        _row(21, "未来标题不作旧标题", url=url, at=900),
        _row(22, "列表最后的旧标题", url=url, at=120),
    ]
    expected = _legacy_source_views(rows, history, primary_source_id=primary)
    with daily._report_text_cache():
        result = daily._source_views(rows, history, primary_source_id=primary)
    assert result == expected
    revised = next(item for item in result[0] if item["url"] == url)
    assert revised["same_link_previous"] == "列表最后的旧标题"
    assert result[1] == 4


def test_source_history_is_indexed_once_and_unrelated_headlines_are_not_normalized(monkeypatch):
    class History(list):
        iterations = 0

        def __iter__(self):
            self.iterations += 1
            return super().__iter__()

    rows = [_row(number) for number in range(1, 6)]
    history = History(_row(number + 1000, f"无关标题{number}") for number in range(2000))
    calls, original = [], daily._normalized_key

    def counted(text):
        calls.append(text)
        return original(text)

    monkeypatch.setattr(daily, "_normalized_key", counted)
    daily._source_views(rows, history)
    assert history.iterations == 1
    assert len(calls) == 10  # Selected title plus its own same-URL row.
    assert not any(text.startswith("无关标题") for text in calls)


@pytest.mark.parametrize("last_claim,last_time,expected", [
    ("列表最后的不同旧标题", 50, "列表最后的不同旧标题"),
    ("列表最后的不同旧标题", 200, "接近尾部的旧标题"),
    ("测试公司发布公告", 50, "接近尾部的旧标题"),
    ("测试公司发布公告！", 50, "接近尾部的旧标题"),
])
def test_source_history_stops_when_last_qualifying_revision_is_known(
    monkeypatch, last_claim, last_time, expected,
):
    url = "https://news.example/revisions"
    rows = [_row(1, "测试公司发布公告", url=url, at=100)]
    history = [_row(index + 100, f"很长的旧标题{index}" + "项目进展" * 50, url=url, at=20)
               for index in range(60000)]
    history += [_row(70000, "接近尾部的旧标题", url=url, at=40),
                _row(70001, last_claim, url=url, at=last_time)]
    frozen = _legacy_source_views(rows, history)
    calls, original = [], daily._normalized_key

    def counted(text):
        calls.append(text)
        return original(text)

    monkeypatch.setattr(daily, "_normalized_key", counted)
    with daily._report_text_cache():
        actual = daily._source_views(rows, history)
    assert actual == frozen
    assert actual[0][0]["same_link_previous"] == expected
    assert len(calls) <= 3  # Selected headline, then only the last one/two candidates.
    assert not any(text.startswith("很长的旧标题") for text in calls)


def test_source_revision_reverse_search_falls_back_to_same_day_rows_and_preserves_empty_history():
    url = "https://news.example/revision"
    rows = [_row(1, "较早的当日标题", url=url, at=30),
            _row(2, "最后不同的当日标题", url=url, at=20),
            _row(3, "选中的否认标题", url=url, at=100, stance="contradicts")]
    for history in [[], [_row(4, "未来标题", url=url, at=200)],
                    [_row(4, "选中的否认标题", url=url, at=50)]]:
        assert daily._source_views(rows, history) == _legacy_source_views(rows, history)
        assert daily._source_views(rows, history)[0][0]["same_link_previous"] == "最后不同的当日标题"
    unchanged = [_row(1, "测试公司发布公告", url=url, at=100)]
    history = [_row(2, "测试公司发布公告！", url=url, at=50),
               _row(3, "未来的不同标题", url=url, at=200)]
    actual = daily._source_views(unchanged, history)
    assert actual == _legacy_source_views(unchanged, history)
    assert actual[0][0]["same_link_previous"] == ""


@pytest.mark.parametrize("name,text,known,expected", [
    ("数字人", "数字人民币试点扩大", ("数字人",), False),
    ("数字人", "上市公司数字人发布年度业绩", ("数字人",), True),
    ("国新能源", "中国新能源产业发展论坛召开", ("国新能源",), False),
    ("国新能源", "国新能源发布年度业绩", ("国新能源",), True),
    ("中国化学", "中国化学会发布研究报告", ("中国化学",), False),
    ("中国化学", "中国化学公司签订工程合同", ("中国化学",), True),
    ("中国建筑", "中国建筑业协会召开会议", ("中国建筑",), False),
    ("中国建筑", "中国建筑签订工程合同", ("中国建筑",), True),
    ("平安", "平安银行发布业绩", ("平安", "平安银行"), False),
    ("平安", "平安银行发布业绩，平安另行签约", ("平安", "平安银行"), True),
    ("平安", "平安银行平安", ("平安", "平安银行"), True),
    ("中信证券", "中信证券：行业需求有望提升", ("中信证券",), False),
    ("中信证券", "中信证券公司发布年度业绩", ("中信证券",), True),
    ("ＡＢＣ", "ＡＢＣ公司发布公告", ("abc",), True),
    ("ABC", "ＡＢＣ公司发布公告", ("abc",), False),
    ("", "测试公司公告", (), False),
])
def test_named_entity_positive_negative_and_unicode_equivalence(name, text, known, expected):
    known_names = frozenset(known)
    assert _legacy_is_named(name, [text], known_names) is expected
    assert daily._is_named(name, [text], known_names) is expected


def test_prepared_coverage_matches_oracle_for_overlapping_and_repeated_terms():
    texts = ["甲乙丙甲乙，乙丙丁甲乙丙丁，甲乙公司发布业绩", "甲乙丙甲乙"]
    known = frozenset({"甲乙", "甲乙丙", "乙丙丁", "甲乙丙丁", "乙丙", "丙丁"})
    prepared = daily._AssociationTexts(texts, known)
    for name in [*known, "乙", "甲", "丙", "丁", "甲乙公司", "无关公司"]:
        assert daily._is_named(name, prepared, known) == _legacy_is_named(name, texts, known)


def test_prepared_interval_coverage_matches_frozen_oracle_across_small_alphabets(monkeypatch):
    # Isolate the longer-name interval algebra; the real guard's positive and
    # negative entity rules are exercised independently above.
    monkeypatch.setattr(daily, "is_direct_security_mention", lambda _text, _name: True)
    random = Random(20261019)
    vocabulary = ["".join(chars) for length in range(1, 5)
                  for chars in product("甲乙丙", repeat=length)]
    for _case in range(100):
        known = frozenset(random.sample(vocabulary, 20))
        names = random.sample(vocabulary, 12)
        texts = ["".join(random.choices("甲乙丙", k=40)) for _ in range(3)]
        prepared = daily._AssociationTexts(texts, known, targets={name: name for name in names})
        for name in names:
            assert daily._is_named(name, prepared, known) == _legacy_is_named(name, texts, known)


def test_association_view_preserves_company_order_cap_dedup_and_rule_ids():
    names = [f"测试企业{number}" for number in range(8)]
    associations = [dict(name=name, relation_type="direct", security_id=f"s{number}")
                    for number, name in enumerate(names)]
    associations.extend([
        dict(name=names[0], relation_type="concept", security_id="s0"),
        dict(name="未被点名公司", relation_type="affected", security_id="unmentioned"),
        dict(name=names[1], relation_type="direct", security_id="alias"),
        dict(name="忽略关系", relation_type="irrelevant", security_id="ignore"),
    ])
    group = dict(title="、".join(names) + "发布业绩", today=[], associations=associations)
    known = frozenset([*names, "未被点名公司"])
    expected = _legacy_association_view(group, known)
    assert daily._association_view(group, known) == expected
    assert expected == {"companies": names[:5], "rule_association_count": 1}


def test_4000_associations_prepare_each_text_once_and_call_guard_only_on_matching_text(monkeypatch):
    names = [f"测试企业{number:04d}" for number in range(4000)]
    texts = ["没有点名任何公司的行业观察", f"{names[-1]}公司发布年度业绩"]
    group = dict(title=texts[0], today=[], association_texts=[*texts, texts[0]], associations=[
        dict(name=name, relation_type="direct", security_id=f"s{number}")
        for number, name in enumerate(names)
    ])
    compact_calls, guard_calls = [], []
    compact, guard = daily._compact, daily.is_direct_security_mention

    def counted_compact(text):
        compact_calls.append(text)
        return compact(text)

    def counted_guard(text, name):
        guard_calls.append((text, name))
        return guard(text, name)

    monkeypatch.setattr(daily, "_compact", counted_compact)
    monkeypatch.setattr(daily, "is_direct_security_mention", counted_guard)
    result = daily._association_view(group, frozenset(names))
    assert result == {"companies": [names[-1]], "rule_association_count": 3999}
    assert len(compact_calls) == len(names) + len(texts)
    assert guard_calls == [(texts[-1], names[-1])]


def test_one_candidate_does_not_search_all_market_names_in_each_text(monkeypatch):
    searched, original = [], daily._compact

    class CountedText(str):
        def __contains__(self, value):
            searched.append(value)
            return super().__contains__(value)

    monkeypatch.setattr(daily, "_compact", lambda text: CountedText(original(text)))
    texts = ["主体公司发布公告", "主体发布业绩"]
    group = dict(title=texts[0], today=[], association_texts=texts, associations=[
        dict(name="主体", relation_type="direct", security_id="s1"),
    ])
    known = frozenset({"主体", "主体公司", *(f"其他上市企业{number:04d}" for number in range(5000))})
    assert daily._association_view(group, known) == {"companies": ["主体"], "rule_association_count": 0}
    assert set(searched) == {"主体"}
    assert len(searched) == 2


def test_repeated_self_mentions_do_not_materialize_occurrence_spans_or_later_titles(monkeypatch):
    texts = ["测试" * 490 + f"发布公告{number}" for number in range(400)]
    known = frozenset({"测试"})
    prepared = daily._AssociationTexts(texts, known, targets={"测试": "测试"})
    calls, original = [], daily._compact

    def counted(text):
        calls.append(text)
        return original(text)

    monkeypatch.setattr(daily, "_compact", counted)
    assert daily._is_named("测试", prepared, known)
    assert calls == [texts[0]]
    assert sum(value is not None for value in prepared.prepared) == 1
    assert prepared.prepared[0] == (texts[0], texts[0])
    assert prepared.longer_by_target == {"测试": ()}


def test_longer_coverage_streams_occurrences_and_stops_on_first_uncovered_name(monkeypatch):
    calls, original = {}, daily.re.finditer

    def counted(pattern, text):
        for match in original(pattern, text):
            calls[pattern] = calls.get(pattern, 0) + 1
            yield match

    monkeypatch.setattr(daily.re, "finditer", counted)
    assert daily._has_uncovered_name("甲", "甲，" + "甲乙" * 10000, ("甲乙",))
    assert calls == {"甲": 1, "甲乙": 1}


@pytest.mark.parametrize("block", ["new", "advanced", "pending", "correction"])
def test_complete_card_matches_frozen_helper_and_computes_summary_once(monkeypatch, block):
    today = [_row(1, "测试公司计划扩产"), _row(2, "测试公司计划扩产", source="http://rsshub/cls/telegraph")]
    history = [] if block == "new" else [_row(3, "测试公司已有项目", at=80)]
    group = dict(title="测试公司计划扩产", today=today, root_ids=["event", "merged"],
                 event_type="company", priority=7.5, associations=[
                     dict(name="测试公司", relation_type="direct", security_id="s1"),
                 ])
    denial = _row(4, "测试公司否认扩产", stance="contradicts")
    state = dict(block=block, history=history, fresh=today, denials=[denial] if block == "correction" else [],
                 corrections=[denial] if block == "correction" else [], prior_support=history,
                 pending_language=block == "pending", background_corrections=[], requests=[])
    known = frozenset({"测试公司"})
    expected = _legacy_item_view(group, state, known, period_label="本周", primary_source_id="e2")
    original, calls = daily._publisher_summary, []

    def counted(items):
        rows = list(items)
        calls.append(rows)
        return original(rows)

    monkeypatch.setattr(daily, "_publisher_summary", counted)
    with daily._report_text_cache():
        actual = daily._item_view(group, state, known, period_label="本周", primary_source_id="e2")
    assert actual == expected
    assert calls == [today + history]


@pytest.mark.parametrize("reverse_history", [False, True])
def test_complete_card_preserves_publishers_from_all_unmatched_history(reverse_history):
    """History skipped by semantic matching still affects sources and outlet notes."""
    shared = "https://news.example/revision"
    today = [_row(1, "公司本周新进展", at=300, url=shared)]
    unknown = "http://rsshub:1200/unregistered/article"
    history = [
        _row(20, "同链接旧标题", at=200, url=shared),
        _row(21, "无关历史一", at=100, url=unknown),
        # Same raw link, but only this row has a trusted publisher route.
        # A known observation must not remove its unknown-link observation.
        _row(22, "无关历史二", at=110, url=unknown,
             source="http://rsshub:1200/cls/telegraph"),
        _row(23, "无关历史三", at=120, url=unknown),
        _row(24, "无关历史否认项目消息", at=130,
             url="https://wallstreetcn.com/articles/old"),
        *[_row(30 + index, f"不同前缀历史{index}", at=140 + index,
               url=f"https://publisher{index}.example/old") for index in range(12)],
    ]
    if reverse_history:
        history.reverse()
    group = dict(title="公司本周新进展", today=today, root_ids=["event"],
                 event_type="company", priority=8.0, associations=[])
    background = next(row for row in history if row["evidence_id"] == "e24")
    state = dict(block="advanced", history=history, fresh=today, corrections=[], denials=[],
                 prior_support=[], pending_language=False, background_corrections=[background])
    expected = _legacy_item_view(group, state, frozenset(), period_label="本周")
    identified, unknown_links = _legacy_publisher_summary(today + history)
    assert identified > daily.MAX_SOURCES_PER_ITEM
    assert unknown_links == 1
    with daily._report_text_cache():
        actual = daily._item_view(group, state, frozenset(), period_label="本周")
        assert daily._publisher_summary(iter(today + history)) == (identified, unknown_links)
    assert actual == expected
    assert actual["sources"][0]["same_link_previous"] == "同链接旧标题"
    assert any("此前已有否认/更正" in note for note in actual["uncertainties"])
