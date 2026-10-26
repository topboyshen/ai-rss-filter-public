"""Frozen pre-optimization semantics and bounded work; no network or models."""

from collections import Counter
from itertools import product
import random
import re
import unicodedata

import pytest

from services import event_daily_report as daily
from services import event_weekly_report as weekly


_OLD_SIGN_TRANSLATION = str.maketrans({
    "\u2212": "-", "\u2013": "-", "\u2014": "-",
    "\u2266": "≤", "\u2267": "≥", "\u2a7d": "≤", "\u2a7e": "≥",
})
_OLD_KEY_TOKEN_RE = re.compile(
    r"[+-]?\d+(?:[.,]\d+)*(?:[%‰‱]|bp|bps)?|[^\W\d_]+|[%‰‱$¥€£°]|[<>=≤≥≠]"
)
_OLD_CORRECTION_CUE_RE = re.compile(
    r"澄清|问询|关注函|监管|交易所|[上深北港]交所|证监[会局]|"
    r"否认|辟谣|不实|失实|不属实|更正|勘误|没有此事|纯属谣言|子虚乌有|并非事实"
)


def _old_normalize_headline(text):
    """Frozen unconditional translation, including its numeric-token rules."""
    value = unicodedata.normalize("NFKC", daily.plain_text_from_markup(text or ""))
    value = value.translate(_OLD_SIGN_TRANSLATION).casefold()
    return " ".join(_OLD_KEY_TOKEN_RE.findall(value))


def _old_headline_correction_kind(claim_text, stance):
    """The original entry point, deliberately without the new cue shortcut."""
    if stance == "contradicts":
        return "denial"
    kinds = {
        daily._clause_correction_kind(clause)
        for clause in re.split(r"[，,。；;！？!?]", claim_text)
    }
    return "denial" if "denial" in kinds else "request" if "request" in kinds else ""


def _old_daily_classify(group, lookback):
    """Frozen daily stage formerly called by the weekly classifier."""
    today, prior = group["today"], group["prior"]
    headline_keys = daily._matching_headline_keys(group)
    matched = [
        row for key in headline_keys
        if key in lookback for row in lookback[key]
    ]
    history = sorted(
        {item["evidence_id"]: item for item in prior + matched}.values(),
        key=daily._ingest_order,
    )
    known = {daily._semantic_key(item) for item in history}
    fresh = [item for item in today if daily._semantic_key(item) not in known]
    fresh_ids = {item["evidence_id"] for item in fresh}
    history_ids = {item["evidence_id"] for item in history}
    reposted = [item for item in today if item["evidence_id"] not in fresh_ids]
    corrections = sorted(
        (item for item in fresh if daily._correction_kind(item)), key=daily._ingest_order,
    )
    denials = [item for item in corrections if daily._correction_kind(item) == "denial"]
    support_cutoff = denials[-1]["observed_at"] if denials else None
    prior_support = [
        item for item in history + fresh
        if item["stance"] == "supports" and not daily._correction_kind(item)
        and (item["evidence_id"] in history_ids or (
            support_cutoff is not None and item["observed_at"] < support_cutoff
        ))
    ]
    return {
        "history": history,
        "fresh": fresh,
        "corrections": corrections,
        "denials": denials,
        "background_corrections": [
            item for item in history + reposted if daily._correction_kind(item) == "denial"
        ],
        "prior_support": sorted(prior_support, key=daily._ingest_order),
        "pending_language": any(daily._PENDING_RE.search(item["claim_text"]) for item in fresh),
    }


def _old_classify_week(group, lookback):
    """Frozen weekly stage, including the raw-fresh support override."""
    state = _old_daily_classify(group, lookback)
    known = {daily._semantic_key(row) for row in state["history"]}
    fresh = [row for row in group["today"] if daily._semantic_key(row) not in known]
    first = {}
    for row in sorted(fresh, key=daily._ingest_order):
        first.setdefault(daily._semantic_key(row), row)
    state["fresh"] = list(first.values())
    state["corrections"] = [row for row in state["fresh"] if daily._correction_kind(row)]
    state["denials"] = [
        row for row in state["corrections"] if daily._correction_kind(row) == "denial"
    ]
    if state["denials"]:
        cutoff = state["denials"][-1]["observed_at"]
        state["prior_support"] = sorted(
            [row for row in state["history"] + fresh
             if row["stance"] == "supports" and not daily._correction_kind(row)
             and row["observed_at"] < cutoff],
            key=daily._ingest_order,
        )
    state["background_corrections"] = sorted(
        [row for row in state["history"] + group["today"]
         if daily._semantic_key(row) in known and daily._correction_kind(row) == "denial"],
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


def _row(identity, text, observed_at, stance="supports"):
    return {
        "evidence_id": identity, "claim_text": text,
        "observed_at": observed_at, "stance": stance,
    }


def _group(today, prior=(), title="显示标题", **extra):
    return {"title": title, "today": list(today), "prior": list(prior), **extra}


@pytest.mark.parametrize("group,block", [
    (_group([_row("a", "公司获得订单", 2)]), "new"),
    (_group([_row("a", "公司公布进度", 2)], [_row("b", "公司获得订单", 1)]), "advanced"),
    (_group([_row("a", "公司计划获得订单", 2)]), "pending"),
    (_group([_row("a", "公司否认获得订单", 2)]), "correction"),
    (_group([_row("a", "公司获得订单", 2)], [_row("b", "公司获得订单", 1)]), "repeat"),
])
def test_all_weekly_blocks_match_frozen_classifier(group, block):
    expected = _old_classify_week(group, {})
    assert expected["block"] == block
    assert weekly._classify_week(group, {}) == expected


def test_raw_fresh_reposts_and_strict_first_denial_cutoff_survive():
    group = _group([
        _row("late-repost", "公司否认订单", 10),
        _row("equal", "同秒支持", 6),
        _row("support-b", "公司获得订单", 3),
        _row("denial-b", "公司更正订单金额", 6),
        _row("support-a", "公司获得订单", 2),
        _row("denial-a", "公司否认订单", 4),
        _row("after", "稍后支持", 7),
    ], [_row("history", "以前报道", 1), _row("future-history", "较晚记录", 8)])
    expected = _old_classify_week(group, {})
    assert [row["evidence_id"] for row in expected["prior_support"]] == [
        "history", "support-a", "support-b",
    ]
    assert [row["evidence_id"] for row in expected["denials"]] == ["denial-a", "denial-b"]
    assert weekly._classify_week(group, {}) == expected


def test_no_denial_keeps_history_id_membership_and_duplicate_evidence_order():
    # The matched endpoint overwrites prior's value for the same ID, while a
    # different fresh row with that ID remains eligible for prior_support.
    group = _group([
        _row("shared", "另一个新事实", 5), _row("today", "全新支持", 5),
    ], [_row("shared", "旧支持", 1), _row("other", "另一旧支持", 1)],
        title="匹配标题")
    lookback = {daily._normalized_key("匹配标题"): (
        _row("shared", "覆盖旧支持", 2), _row("shared", "覆盖旧支持", 2),
    )}
    expected = _old_classify_week(group, lookback)
    assert [row["claim_text"] for row in expected["history"]] == ["另一旧支持", "覆盖旧支持"]
    assert [row["evidence_id"] for row in expected["prior_support"]] == [
        "other", "shared", "shared",
    ]
    assert weekly._classify_week(group, lookback) == expected


def test_empty_normalized_key_and_stance_distinctions_keep_history_background():
    group = _group([
        _row("empty", "!!!", 3),
        _row("denial", "公司发布消息", 4, "contradicts"),
        _row("request-denial", "交易所要求公司说明", 5, "contradicts"),
        _row("old-denial-repost", "旧消息", 6, "contradicts"),
    ], [_row("support", "公司发布消息", 1)], title="!!!")
    lookback = {
        "": (_row("old-empty", "???", 1),),
        daily._normalized_key("交易所要求公司说明"): (
            _row("request", "交易所要求公司说明", 2),
        ),
        daily._normalized_key("旧消息"): (_row("old-denial", "旧消息", 1, "contradicts"),),
    }
    expected = _old_classify_week(group, lookback)
    assert [row["evidence_id"] for row in expected["fresh"]] == ["denial", "request-denial"]
    assert [row["evidence_id"] for row in expected["background_corrections"]] == [
        "old-denial", "old-denial-repost",
    ]
    keys = daily._matching_headline_keys(group)
    assert "" in keys
    assert weekly._classify_week(group, lookback, headline_keys=keys) == expected


def test_mixed_duplicates_and_endpoint_values_match_frozen_classifier():
    rng = random.Random(20261019)
    texts = [
        "!!!", "公司获得订单", "公司获得订单！", "公司计划获得订单",
        "公司否认订单", "交易所要求公司说明", "公司回复问询函",
        "公司发布澄清公告", "公司净利润≤5亿元", "公司净利润≥5亿元",
    ]
    for _ in range(80):
        rows = [
            _row(str(rng.randrange(9)), rng.choice(texts), rng.randrange(10),
                 rng.choice(["supports", "supports", "contradicts", "mentions"]))
            for _ in range(30)
        ]
        group = _group(rows[10:20], rows[:10], title=rng.choice(texts),
                       identity_title=rng.choice(texts))
        lookback = daily._lookback_index(rows[20:])
        expected = _old_classify_week(group, lookback)
        assert weekly._classify_week(group, lookback) == expected
        keys = daily._matching_headline_keys(group)
        filtered = daily._lookback_index(rows[20:], headline_keys=keys)
        assert weekly._classify_week(group, filtered, headline_keys=keys) == expected


def test_history_over_text_cache_capacity_keeps_rows_without_full_unrelated_tokenization(monkeypatch):
    history = [
        _row(f"h{index:05}", f"公司{index}披露项目进度" + "项目进度" * 80, index)
        for index in range(daily._TEXT_CACHE_SIZE + 9)
    ]
    today = [_row("today", "新项目获批", 20_000)]
    group = _group(today, history)
    keys = daily._matching_headline_keys(group)
    original = daily._semantic_key
    calls = Counter()
    token_counts = Counter()
    pattern = daily._KEY_TOKEN_RE

    class CountedPattern:
        def findall(self, text):
            token_counts["full"] += 1
            return pattern.findall(text)

        def finditer(self, text):
            token_counts["historical"] += 1
            for match in pattern.finditer(text):
                token_counts["tokens"] += 1
                yield match

    def counted(row):
        calls[row["evidence_id"]] += 1
        return original(row)

    def unexpected_matching(_group):
        raise AssertionError("precomputed headline keys must be reused")

    monkeypatch.setattr(daily, "_semantic_key", counted)
    monkeypatch.setattr(daily, "_matching_headline_keys", unexpected_matching)
    monkeypatch.setattr(daily, "_KEY_TOKEN_RE", CountedPattern())
    with daily._report_text_cache():
        state = weekly._classify_week(group, {}, headline_keys=keys)
    assert state["block"] == "advanced"
    assert len(state["history"]) == len(history)
    assert calls == {"today": 1}
    assert token_counts == {"full": 1, "historical": len(history), "tokens": len(history)}


@pytest.mark.parametrize("current_count", [0, 1, 64, 65, 100])
def test_membership_algorithm_boundary_keeps_frozen_weekly_state(current_count):
    prior = [_row(f"h{i}", f"公司{i}净利润≤5亿元", i) for i in range(120)]
    prior += [_row("denial", "旧澄清公告", 121), _row("empty", "!!!", 122)]
    today = [_row(f"t{i}", f"公司{i}净利润≦５亿元", 1000 + i) for i in range(current_count)]
    today += [_row("empty-repost", "???", 2000), _row("old-denial", "旧澄清公告", 2001)]
    group = _group(today, prior)
    with daily._report_text_cache():
        assert weekly._classify_week(group, {}) == _old_classify_week(group, {})


def test_history_membership_compares_full_tokens_and_kind_not_prefixes():
    texts = [
        "公司Ａ净利润≦５亿元", "公司A净利润≤5亿元", "公司A净利润≤5亿元增加",
        "公司A净利润≤5亿元！", "公司A净利润≥5亿元", "公司A净利润≤5.0亿元",
        "公司A净利润≤+5亿元", "公司A净利润≤-5亿元", "公司A净利润≤5万元",
        "<b>公司Ａ</b>净利润&le;５亿元", "!!!", "???", "公司 A 净利润 ≤ 5 亿元",
        "公司A净利润≤5亿元，交易所要求澄清", "公司A净利润≤5亿元，发布澄清公告",
    ]
    for stance in ["supports", "contradicts", "mentions"]:
        prior = [_row(f"h{i}", text, i, stance) for i, text in enumerate(texts)]
        for i, text in enumerate(texts):
            for today_stance in ["supports", "contradicts", "mentions"]:
                group = _group([_row("now", text, 100, today_stance)], prior[:i] + prior[i + 1:])
                assert weekly._classify_week(group, {}) == _old_classify_week(group, {})


@pytest.mark.parametrize("target_count", [0, 1, 64, 65])
def test_exact_distinct_target_count_chooses_equivalent_bounded_algorithm(monkeypatch, target_count):
    history = [_row(f"h{i}", f"公司{i}披露利润≤5亿元", i) for i in range(70)]
    targets = {(_old_normalize_headline(row["claim_text"]), "") for row in history[:target_count]}
    assert len(targets) == target_count
    calls = []
    original = daily._normalized_key

    def counted(text):
        calls.append(text)
        return original(text)

    monkeypatch.setattr(daily, "_normalized_key", counted)
    kinds, known = weekly._weekly_history_keys(history, targets)
    assert kinds == [(row, "") for row in history]
    assert known == targets
    assert len(calls) == (len(history) if target_count > 64 else 0)


def test_plain_history_shortcut_preserves_every_unicode_whitespace_and_full_normalization():
    whitespace = "".join(chr(code) for code in range(0x110000) if chr(code).isspace())
    texts = [
        whitespace, whitespace + "Ａ公司" + whitespace + "利润−５％" + whitespace,
        "公司\u0301\u0327金额≦５亿元", "a\u030a\u0327\ufb03İẞ", "甲" * 1000 + "\u0301尾部",
        "&amp;amp;amp;lt;b&amp;amp;amp;gt;Ａ公司", "<script>隐藏</script>公司利润≤5亿元",
        "<b>公司</b>\xa0利润&minus;５％", "公司<b 未闭合标签金额−5亿元",
        "公司\x00利润\x1c同比\x85增长\u2028５％", "", None, 0, 12345,
    ]
    rng = random.Random(20261021)
    alphabet = whitespace + "−–—≦≧⩽⩾Ａa\u0301\u0327<>&;012.5%澄清公司"
    texts.extend("".join(rng.choice(alphabet) for _ in range(rng.randrange(150))) for _ in range(1000))
    for text in texts:
        assert weekly._weekly_plain_text(text) == daily.plain_text_from_markup(text)
        actual = " ".join(_OLD_KEY_TOKEN_RE.findall(weekly._weekly_headline_key_text(text)))
        assert actual == _old_normalize_headline(text)


def test_plain_history_skips_markup_parser_only_without_both_marker_types(monkeypatch):
    original = daily.plain_text_from_markup
    calls = []

    def counted(text):
        calls.append(text)
        return original(text)

    monkeypatch.setattr(daily, "plain_text_from_markup", counted)
    plain = "公司\xa0\t披露  项目进度" + "项目进度" * 250
    assert weekly._weekly_plain_text(plain) == original(plain)
    assert weekly._weekly_headline_key_text(plain) == daily._headline_key_text(plain)
    assert calls == [plain]  # Only the explicit shared-helper oracle parsed it.
    calls.clear()
    for text in ("公司金额<5亿元", "公司披露&amp;amp;进度", "<b>公司</b>", None, 12345):
        assert weekly._weekly_plain_text(text) == original(text)
    assert calls == ["公司金额<5亿元", "公司披露&amp;amp;进度", "<b>公司</b>", None, 12345]


def test_history_cue_union_skips_shared_classifier_only_for_definite_negatives(monkeypatch):
    original = daily._correction_kind
    calls = []

    def counted(row):
        calls.append(row["evidence_id"])
        return original(row)

    monkeypatch.setattr(daily, "_correction_kind", counted)
    history = [
        _row("long-support", "公司披露项目正式投产" + "合成说明" * 250, 1),
        _row("stance-denial", "公司披露项目正式投产" + "合成说明" * 250, 2, "contradicts"),
    ]
    kinds, known = weekly._weekly_history_keys(history, set())
    assert kinds == [(history[0], ""), (history[1], "denial")]
    assert known == set()
    assert calls == []
    for index, cue in enumerate(daily._CORRECTION_CUE_WORDS):
        row = _row(str(index), "公司披露" + cue + "事项", index)
        assert weekly._history_correction_kind(row) == _old_headline_correction_kind(
            row["claim_text"], row["stance"],
        )
    assert calls == [str(index) for index in range(len(daily._CORRECTION_CUE_WORDS))]


def test_history_correction_filter_keeps_unicode_and_adjacent_clause_readings():
    rng = random.Random(20261022)
    clauses = [
        "公司披露进度", "如果公司发布澄清公告将有帮助", "希望公司予以澄清",
        "公司回复问询函", "公司收到监管工作函后披露进展", "交易所要求公司说明",
        "公司称消息不实", "证监会公布例行安排", "新希望发布澄清公告",
    ]
    for _ in range(1500):
        noise = "".join(chr(rng.randrange(0x110000)) for _ in range(rng.randrange(80)))
        text = noise + rng.choice(clauses) + rng.choice(["，", ";", "。", "!", "\n"]) + rng.choice(clauses)
        stance = rng.choice(["supports", "contradicts", "context"])
        assert weekly._history_correction_kind(_row("x", text, 0, stance)) == (
            _old_headline_correction_kind(text, stance)
        )


def test_correction_fast_path_covers_every_positive_regex_branch():
    strong = [
        "否认", "辟谣", "不实", "失实", "不属实", "更正", "勘误",
        "没有此事", "纯属谣言", "子虚乌有", "并非事实",
    ]
    regulators = ["交易所", "上交所", "深交所", "北交所", "港交所", "证监会", "证监局", "监管"]
    cases = [(word, "denial") for word in strong]
    cases += [
        (regulator + "要求公司" + action, "request")
        for regulator, action in product(regulators, ["说明", "核实", "回复", "解释", "澄清"])
    ]
    cases += [(regulator + "问询", "request") for regulator in regulators]
    cases += [(letter, "request") for letter in ["问询函", "关注函", "监管工作函", "监管函"]]
    cases += [(directive + "公司澄清", "request") for directive in ["要求", "责令", "督促", "请"]]
    cases += [("请公司回复" + letter, "request") for letter in ["问询函", "关注函", "监管函"]]
    cases += [(word, "denial") for word in [
        "澄清公告", "澄清声明", "澄清说明", "发布相关澄清", "予以澄清", "澄清称", "澄清：", "澄清:",
    ]]
    for text, expected in cases:
        assert _old_headline_correction_kind(text, "supports") == expected, text
        assert daily._headline_correction_kind(text, "supports") == expected, text


@pytest.mark.parametrize("text", [
    "", "公司公告项目正式投产", "公司计划下月推出新服务", "公司披露利润同比增长10%",
    "英文ABC数字123！" * 200, "公司获得订单，随后公布业绩；近期投产。",
])
def test_headlines_without_cues_skip_clause_rules(monkeypatch, text):
    assert _old_headline_correction_kind(text, "supports") == ""

    def unexpected_clause(_text):
        raise AssertionError("a cue-free headline must skip clause regular expressions")

    monkeypatch.setattr(daily, "_clause_correction_kind", unexpected_clause)
    assert daily._headline_correction_kind(text, "supports") == ""
    assert daily._headline_correction_kind(text, "contradicts") == "denial"


def test_cue_false_positives_and_adjacent_clauses_keep_old_reading():
    clauses = [
        "公司公布进展", "如果公司发布澄清公告将有助于了解情况", "希望公司予以澄清",
        "公司回复问询函", "公司收到监管工作函后披露进展", "交易所要求公司说明",
        "公司称消息不实", "证监会公布例行安排", "新希望发布澄清公告",
    ]
    for left, right, separator, stance in product(
        clauses, clauses, ["，", ";", "。", "!", "\n"], ["supports", "contradicts"],
    ):
        text = left + separator + right
        assert daily._headline_correction_kind(text, stance) == _old_headline_correction_kind(
            text, stance,
        )


def test_every_unicode_symbol_and_punctuation_matches_frozen_normalization():
    # Exhaust the Unicode symbol/punctuation categories, rather than sampling
    # only ASCII punctuation. Each sign appears beside an actual amount.
    for codepoint in range(0x110000):
        sign = chr(codepoint)
        if unicodedata.category(sign)[0] in {"S", "P"}:
            text = f"公司金额{sign}1.5亿元同比{sign}5%"
            assert daily._normalize_headline(text) == _old_normalize_headline(text), hex(codepoint)


@pytest.mark.parametrize("text", [
    "公司利润−5%，收入–5亿元，现金流—1.5亿元",
    "投资金额≦5亿、≧5亿、⩽5亿、⩾5亿",
    "公司净利润≤5亿元，净利润≥5亿元，成交额<1万亿，成交额>1万亿",
    "同比+5%、-5%、±5%、∓5%、≈5%、≠5%、1.5%、15%",
    "利率变动-5bp、+5bps、5‰、5‱、５％，金额$5、¥5、€5、£5",
    "金额不低于5亿元、不超过5亿元、5万元、5亿元、1,234.5亿元",
    "Ａ公司净利润＜５亿元，Ｂ公司净利润≧５亿元，温度５°",
    "<em>公司</em>利润&minus;5% &amp; 收入&lt;5亿元",
    "&lt;strong&gt;公司&lt;/strong&gt;披露Ａ股金额≦1.5亿元",
    "公司\xa0净利润\t≤5亿元\n现金流\u2003≥5亿元",
    "公司披露<b 未闭合标签金额−5亿元",
    "公司披露<script>干扰−5%</script>净利润+5%",
    "", None, 12345,
])
def test_numeric_qualifiers_markup_and_width_keep_frozen_normalization(text):
    assert daily._normalize_headline(text) == _old_normalize_headline(text)


def test_random_unicode_normalization_and_literal_cues_equal_frozen_rules():
    rng = random.Random(20261020)
    edge_characters = "−–—≦≧⩽⩾≤≥≠±∓≈<>+-0123456789.%‰‱ＡＢＣ\xa0\n\t&;"
    cues = [
        "澄清", "问询", "关注函", "监管", "交易所", "上交所", "深交所", "北交所", "港交所",
        "证监会", "证监局", "否认", "辟谣", "不实", "失实", "不属实", "更正", "勘误",
        "没有此事", "纯属谣言", "子虚乌有", "并非事实",
    ]
    for _ in range(1000):
        text = "".join(
            rng.choice(edge_characters) if rng.randrange(3) == 0 else chr(rng.randrange(0x110000))
            for _ in range(rng.randrange(1001))
        )
        if rng.randrange(2):
            position = rng.randrange(len(text) + 1)
            text = text[:position] + rng.choice(cues) + text[position:]
        assert daily._normalize_headline(text) == _old_normalize_headline(text)
        assert any(cue in text for cue in daily._CORRECTION_CUE_WORDS) == bool(
            _OLD_CORRECTION_CUE_RE.search(text)
        )
        for stance in ("supports", "contradicts", "context"):
            assert daily._headline_correction_kind(text, stance) == _old_headline_correction_kind(
                text, stance,
            )


def test_no_mapped_signs_bypass_translation_but_mapped_signs_still_use_it(monkeypatch):
    lookups = []

    class CountedTranslation(dict):
        def __getitem__(self, codepoint):
            lookups.append(codepoint)
            return super().__getitem__(codepoint)

    monkeypatch.setattr(daily, "_SIGN_TRANSLATION", CountedTranslation(_OLD_SIGN_TRANSLATION))
    text = "公司披露净利润≤1.5亿元同比+5%" + "合成说明" * 240
    assert daily._normalize_headline(text) == _old_normalize_headline(text)
    assert lookups == []
    text = "公司披露净利润−5%，收入≧1.5亿元"
    assert daily._normalize_headline(text) == _old_normalize_headline(text)
    assert ord("−") in lookups and ord("≧") in lookups


def test_literal_cue_scan_stops_on_first_match_and_stance_takes_priority():
    probes = []

    class CountedTitle(str):
        def __contains__(self, cue):
            probes.append(cue)
            return super().__contains__(cue)

    assert daily._headline_correction_kind(CountedTitle("公司披露正式进展"), "supports") == ""
    assert tuple(probes) == daily._CORRECTION_CUE_WORDS
    probes.clear()
    assert daily._headline_correction_kind(CountedTitle("澄清公告"), "supports") == "denial"
    assert probes == ["澄清"]
    probes.clear()
    assert daily._headline_correction_kind(CountedTitle("公司披露正式进展"), "contradicts") == "denial"
    assert probes == []
