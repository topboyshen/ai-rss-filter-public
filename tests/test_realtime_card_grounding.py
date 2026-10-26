from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core.asset_mapping import map_article_to_assets
from sse_engine import _UNMAPPED_TITLE_NOTE, _card_ingest_time_label, _render_cards


INNOVATIVE_DRUG_TITLE = "中报窗口期机构密集调研，创新药步入业绩与国际化兑现期"
SOURCE_URL = "https://www.36kr.com/newsflashes/3934380360432776"
RULE_BACKGROUND_NOTE = (
    "仅按标题匹配主题；背景不是本条新闻的评分依据，"
    "示例不代表本文提及或已确认受益。"
)
INNOVATIVE_DRUG_BACKGROUND = (
    "研发、临床和国际化兑现信号可能改善创新药产业链的业绩预期与风险偏好"
)
RULE_POOL_SUMMARY = "主题背景／规则池（非本文点名、不代表受益）"
GENERIC_UNMAPPED_WATCH = "观察成交放大和价格趋势是否延续"
AI_STOCK_EXAMPLES = "中际旭创、新易盛、工业富联、浪潮信息、寒武纪"


def _ai_capacity_entry() -> dict:
    entry = _polluted_innovative_drug_entry()
    entry.update({
        "title": "AI算力订单增长带动服务器和光模块需求",
        "raw_title": "AI算力订单增长带动服务器和光模块需求",
        "link": "https://news.example/ai-capacity",
    })
    return entry


def _unmatched_entry() -> dict:
    entry = _polluted_innovative_drug_entry()
    entry.update({
        "title": "普通公司发布例行公告",
        "raw_title": "普通公司发布例行公告",
        "link": "https://news.example/routine-update",
    })
    return entry


def _assert_no_empty_rule_pool_fold(html: str) -> None:
    assert RULE_POOL_SUMMARY not in html
    assert "规则池示例：" not in html
    assert "等待更多来源证据" not in html
    assert GENERIC_UNMAPPED_WATCH not in html
    assert "暂无可信映射" not in html


def _polluted_innovative_drug_entry() -> dict:
    return {
        "title": (
            "[⭐8.5 📈 AI算力] "
            f"{INNOVATIVE_DRUG_TITLE} 💡算力需求扩张利好产业链"
        ),
        "raw_title": INNOVATIVE_DRUG_TITLE,
        "link": SOURCE_URL,
        "score": 8.5,
        "importance_score": 8.5,
        "direction": "看多",
        "tag": "AI算力",
        "logic": "算力需求扩张利好产业链",
        "assets": "成长风格、纳指映射",
        "sectors": "CPO、服务器、光模块、AI应用、半导体",
        "stocks": "中际旭创、新易盛、工业富联、浪潮信息、寒武纪",
        "score_reason": "产业趋势或订单变化可能提升成长股风险偏好",
        "watch": "观察订单兑现、业绩指引、海外龙头和成交额是否共振",
        "time": 2_000_000_000,
        "source": "36氪快讯",
        "feed_url": "http://rsshub:1200/36kr/newsflashes",
        "_selection_threshold": 7.5,
        "_realtime_section": "fresh_strong",
    }


def test_trusted_title_maps_innovative_drug_before_polluted_model_fields():
    mapping = map_article_to_assets(
        _polluted_innovative_drug_entry(),
        trusted_only=True,
    )

    assert mapping["mapped_theme"] == "创新药"
    assert "医药成长" in mapping["asset_classes"]
    assert "创新药" in mapping["sectors"]
    assert "CPO" not in mapping["sectors"]
    assert "中际旭创" not in mapping["stocks"]


def test_trusted_mapping_ignores_model_wrapped_title_when_raw_title_exists():
    entry = _polluted_innovative_drug_entry()
    entry["title"] = "[⭐8.5 📈 AI算力] 普通公司公告 💡GPU需求扩张"

    mapping = map_article_to_assets(entry, trusted_only=True)

    assert mapping["mapped_theme"] == "创新药"
    assert "CPO" not in mapping["sectors"]


@pytest.mark.parametrize(
    "title",
    [
        "生物医药公司高管辞职",
        "某医疗器械产品获FDA批准",
        "医疗服务公司披露常规临床数据",
        "公司仿制药药品获批",
        "连锁新药店开业",
        "连锁企业新药房开店",
    ],
)
def test_weak_medical_terms_do_not_create_innovative_drug_mapping(title):
    entry = _polluted_innovative_drug_entry()
    entry["title"] = title
    entry["raw_title"] = title

    mapping = map_article_to_assets(entry, trusted_only=True)

    assert mapping["mapped_theme"] != "创新药"
    assert mapping["mapping_confidence"] == 0.0


@pytest.mark.parametrize(
    "title",
    [
        "Retail sales growth slows",
        "Thailand raises rates",
        "Company said results improved",
    ],
)
def test_ascii_words_containing_ai_do_not_trigger_ai_mapping(title):
    entry = _polluted_innovative_drug_entry()
    entry["title"] = title
    entry["raw_title"] = title

    mapping = map_article_to_assets(entry, trusted_only=True)

    assert mapping["mapped_theme"] != "AI算力"
    assert mapping["mapping_confidence"] == 0.0


def test_explicit_openai_entity_can_trigger_ai_mapping():
    entry = _polluted_innovative_drug_entry()
    entry["title"] = "OpenAI扩大模型训练基础设施投入"
    entry["raw_title"] = entry["title"]

    mapping = map_article_to_assets(entry, trusted_only=True)

    assert mapping["mapped_theme"] == "AI算力"


def test_explicit_class_one_new_drug_can_trigger_innovative_drug_mapping():
    entry = _polluted_innovative_drug_entry()
    entry["title"] = "国产1类新药获批上市"
    entry["raw_title"] = entry["title"]

    mapping = map_article_to_assets(entry, trusted_only=True)

    assert mapping["mapped_theme"] == "创新药"



def test_rule_pool_background_block_is_collapsed_by_default():
    """Display-only: Glance 规则池块默认折叠，避免读者误判为本文点名受益。"""
    html = _render_cards([_polluted_innovative_drug_entry()])

    assert '<details style="margin-top:8px;">' in html
    assert 'open' not in html.split('<details', 1)[1].split('>', 1)[0]
    assert (
        '<summary style="cursor:pointer;color:var(--color-text-subdue,inherit);'
        'font-size:11px;font-weight:700;line-height:1.5;user-select:none;">'
        '主题背景／规则池（非本文点名、不代表受益）</summary>'
    ) in html
    assert "非本文点名、不代表受益" in html
    assert "规则池示例：未提供个股示例" in html
    assert RULE_BACKGROUND_NOTE in html
    assert f"主题背景（规则生成）：{INNOVATIVE_DRUG_BACKGROUND}" in html
    assert "</details>" in html



def test_stock_rule_pool_examples_live_inside_collapsed_details():
    """Real stock examples stay inside <details>; title/score stay outside."""
    entry = _polluted_innovative_drug_entry()
    entry.update({
        "title": "AI算力订单增长带动服务器和光模块需求",
        "raw_title": "AI算力订单增长带动服务器和光模块需求",
        "link": "https://news.example/ai-capacity-stocks",
    })
    stock_examples = "中际旭创、新易盛、工业富联、浪潮信息、寒武纪"
    title = "AI算力订单增长带动服务器和光模块需求"

    html = _render_cards([entry])

    before, _, rest = html.partition("<details")
    details_open, _, after_open = rest.partition(">")
    assert "open" not in details_open
    details_body, _, after = after_open.partition("</details>")

    assert f"规则池示例：{stock_examples}" in details_body
    assert "主题背景／规则池（非本文点名、不代表受益）" in details_body
    assert RULE_BACKGROUND_NOTE in details_body
    assert stock_examples not in before
    assert stock_examples not in after
    assert title in before
    assert "重要度 8.5/10" in after or "重要度 8.5/10" in before
    assert "重要度 8.5/10" not in details_body
    assert title not in details_body


def test_public_card_recomputes_mapping_from_trusted_title_for_reported_url():
    html = _render_cards([_polluted_innovative_drug_entry()])

    assert INNOVATIVE_DRUG_TITLE in html
    assert SOURCE_URL in html
    assert "主题资产参考：医药成长、创新药产业链、港股生物科技" in html
    assert "相关板块：创新药、生物制品、CXO、医疗研发服务" in html
    assert "规则池示例：未提供个股示例" in html
    assert "待行情源补全" not in html
    assert "AI算力" not in html
    assert " · 创新药 · " not in html.split("<details", 1)[0]
    assert "标题匹配主题：创新药（规则标签，不是本文点名）" in html
    assert "统一强信号 ≥8.5" in html
    assert "CPO" not in html
    assert "中际旭创" not in html
    assert "算力需求扩张利好产业链" not in html
    assert f"主题背景（规则生成）：{INNOVATIVE_DRUG_BACKGROUND}" in html
    assert html.count(INNOVATIVE_DRUG_BACKGROUND) == 1
    assert RULE_BACKGROUND_NOTE in html
    assert "评分理由：" not in html
    assert "待核对事项：观察中报业绩、临床进展、授权出海交易、海外审批和医保放量是否兑现" in html
    assert "产业趋势或订单变化可能提升成长股风险偏好" not in html
    assert "观察订单兑现、业绩指引、海外龙头和成交额是否共振" not in html


def test_public_card_never_reuses_cross_theme_logic_when_tag_is_correct():
    entry = _polluted_innovative_drug_entry()
    entry["tag"] = "创新药"

    html = _render_cards([entry])

    assert " · 创新药 · " not in html.split("<details", 1)[0]
    assert "标题匹配主题：创新药（规则标签，不是本文点名）" in html
    assert "算力需求扩张利好产业链" not in html
    assert f"主题背景（规则生成）：{INNOVATIVE_DRUG_BACKGROUND}" in html
    assert html.count(INNOVATIVE_DRUG_BACKGROUND) == 1


def test_public_card_keeps_existing_ai_mapping_when_source_title_supports_it():
    entry = _polluted_innovative_drug_entry()
    entry.update({
        "title": "AI算力订单增长带动服务器和光模块需求",
        "raw_title": "AI算力订单增长带动服务器和光模块需求",
        "link": "https://news.example/ai-capacity",
    })

    html = _render_cards([entry])
    visible = html.split("<details", 1)[0]

    assert " · AI算力 · " not in visible
    assert "标题匹配主题：AI算力（规则标签，不是本文点名）" in html
    assert "主题资产参考：成长风格、纳指映射" in html
    assert "相关板块：CPO、服务器、光模块、AI应用、半导体" in html
    assert "规则池示例：中际旭创、新易盛、工业富联、浪潮信息、寒武纪" in html
    assert RULE_BACKGROUND_NOTE in html
    assert "代表个股：" not in html
    assert "评分理由：" not in html


@pytest.mark.parametrize("reading", [False, True])
def test_unmapped_title_skips_empty_rule_pool_fold_on_both_layouts(reading):
    html = _render_cards([_unmatched_entry()], reading=reading)

    assert _UNMAPPED_TITLE_NOTE in html
    assert html.count(_UNMAPPED_TITLE_NOTE) == 1
    _assert_no_empty_rule_pool_fold(html)
    assert "AI算力" not in html
    assert "算力需求扩张利好产业链" not in html
    assert "CPO" not in html
    assert "中际旭创" not in html
    assert "未提供个股示例" not in html
    assert RULE_BACKGROUND_NOTE not in html
    if reading:
        assert "方向与入选依据" in html
        assert 'class="home-news-context"' in html


@pytest.mark.parametrize("reading", [False, True])
def test_theme_without_stock_examples_keeps_rule_pool_fold_on_both_layouts(reading):
    html = _render_cards([_polluted_innovative_drug_entry()], reading=reading)
    rule_pool = html.split(RULE_POOL_SUMMARY, 1)[1]

    assert RULE_POOL_SUMMARY in html
    assert "open" not in html.split(RULE_POOL_SUMMARY, 1)[0].rsplit("<details", 1)[1]
    assert "规则池示例：未提供个股示例" in rule_pool
    assert f"主题背景（规则生成）：{INNOVATIVE_DRUG_BACKGROUND}" in rule_pool
    assert RULE_BACKGROUND_NOTE in rule_pool
    assert _UNMAPPED_TITLE_NOTE not in html
    if reading:
        assert "方向与入选依据" in html


@pytest.mark.parametrize("reading", [False, True])
def test_complete_mapping_keeps_collapsed_rule_pool_disclaimer_on_both_layouts(reading):
    html = _render_cards([_ai_capacity_entry()], reading=reading)
    visible, _, rest = html.partition(RULE_POOL_SUMMARY)

    assert RULE_POOL_SUMMARY in html
    assert "非本文点名、不代表受益" in html
    assert f"规则池示例：{AI_STOCK_EXAMPLES}" in rest
    assert AI_STOCK_EXAMPLES not in visible
    assert RULE_BACKGROUND_NOTE in rest
    assert _UNMAPPED_TITLE_NOTE not in html
    if reading:
        assert "方向与入选依据" in visible


def test_public_card_marks_unmatched_title_unavailable_instead_of_using_model_mapping():
    html = _render_cards([_unmatched_entry()])

    assert _UNMAPPED_TITLE_NOTE in html
    _assert_no_empty_rule_pool_fold(html)
    assert "AI算力" not in html
    assert "算力需求扩张利好产业链" not in html
    assert "CPO" not in html
    assert "中际旭创" not in html
    assert "未提供个股示例" not in html
    assert RULE_BACKGROUND_NOTE not in html


def test_public_card_does_not_treat_zero_confidence_region_fallback_as_mapping():
    entry = _polluted_innovative_drug_entry()
    entry.update({
        "title": "美国财政部官员出席例行活动",
        "raw_title": "美国财政部官员出席例行活动",
        "link": "https://news.example/routine-overseas-event",
    })

    html = _render_cards([entry])

    assert _UNMAPPED_TITLE_NOTE in html
    _assert_no_empty_rule_pool_fold(html)
    assert "海外宏观政策" not in html
    assert "算力需求扩张利好产业链" not in html


@pytest.mark.parametrize("direction", ["看多", "看空", "中性"])
def test_rule_background_preserves_score_direction_selection_and_input(direction):
    # Direction variants are synthetic boundary cases, not adjudicated news.
    entry = _polluted_innovative_drug_entry()
    entry["direction"] = direction
    before = deepcopy(entry)
    mapping_before = map_article_to_assets(entry, trusted_only=True)

    html = _render_cards([entry])

    assert entry == before
    assert map_article_to_assets(entry, trusted_only=True) == mapping_before
    assert INNOVATIVE_DRUG_TITLE in html
    assert SOURCE_URL in html
    assert "重要度 8.5/10" in html
    assert f">{direction}</span>" in html
    assert "近12小时 · 强信号" in html
    assert "统一强信号 ≥8.5" in html
    assert " · 创新药 · " not in html.split("<details", 1)[0]
    assert "标题匹配主题：创新药（规则标签，不是本文点名）" in html
    assert html.count(INNOVATIVE_DRUG_BACKGROUND) == 1
    assert html.count(RULE_BACKGROUND_NOTE) == 1
    assert "算力需求扩张利好产业链" not in html


def test_rule_background_and_reference_fields_are_html_escaped(monkeypatch):
    # Check the rendering boundary without depending on today's literal rules.
    entry = _polluted_innovative_drug_entry()
    before = deepcopy(entry)
    fields = {
        "mapped_theme": "创新药",
        "mapping_confidence": 0.8,
        "assets": "<script>asset</script>",
        "sectors": "<svg/onload=sector>",
        "stocks": "<img src=x onerror=stock>",
        "score_reason": "<script>background</script>",
        "watch": "<svg/onload=watch>",
    }
    monkeypatch.setattr(
        "sse_engine.enrich_article_analysis",
        lambda article, *, trusted_only: {**article, **fields},
    )

    html = _render_cards([entry])

    assert entry == before
    assert "主题资产参考：&lt;script&gt;asset&lt;/script&gt;" in html
    assert "相关板块：&lt;svg/onload=sector&gt;" in html
    assert "规则池示例：&lt;img src=x onerror=stock&gt;" in html
    assert "主题背景（规则生成）：&lt;script&gt;background&lt;/script&gt;" in html
    assert "待核对事项：&lt;svg/onload=watch&gt;" in html
    assert html.count("&lt;script&gt;background&lt;/script&gt;") == 1
    assert RULE_BACKGROUND_NOTE in html
    assert "<script>" not in html
    assert "<svg" not in html
    assert "<img" not in html


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_REVIEW_NOW = datetime(2026, 9, 18, 7, 10, tzinfo=_SHANGHAI)


@pytest.mark.parametrize(
    "when, expected",
    [
        (datetime(2026, 9, 18, 0, 0, tzinfo=_SHANGHAI), "收录 今天 00:00"),
        (datetime(2026, 9, 18, 1, 56, tzinfo=_SHANGHAI), "收录 今天 01:56"),
        (datetime(2026, 9, 17, 23, 59, tzinfo=_SHANGHAI), "收录 昨天 23:59"),
        (datetime(2026, 9, 17, 23, 49, tzinfo=_SHANGHAI), "收录 昨天 23:49"),
        (datetime(2026, 9, 16, 15, 35, tzinfo=_SHANGHAI), "收录 09-16 15:35"),
        (datetime(2026, 9, 19, 8, 0, tzinfo=_SHANGHAI), "收录 09-19 08:00"),
    ],
)
def test_card_ingest_time_label_is_ingest_clock_not_publisher_time(when, expected):
    now = int(_REVIEW_NOW.timestamp())
    assert _card_ingest_time_label(int(when.timestamp()), now_ts=now) == expected
    assert not expected.startswith("发布")


def test_card_ingest_time_label_hides_missing_or_invalid_stamps():
    now = int(_REVIEW_NOW.timestamp())
    assert _card_ingest_time_label(0, now_ts=now) == ""
    assert _card_ingest_time_label(None, now_ts=now) == ""
    assert _card_ingest_time_label("created", now_ts=now) == ""


def test_realtime_card_visible_meta_shows_ingest_day_not_bare_clock(monkeypatch):
    monkeypatch.setattr("sse_engine.time.time", lambda: _REVIEW_NOW.timestamp())
    entry = _polluted_innovative_drug_entry()
    entry["time"] = int(datetime(2026, 9, 17, 23, 49, tzinfo=_SHANGHAI).timestamp())
    html = _render_cards([entry])
    visible = html.split("<details", 1)[0]
    assert "收录 昨天 23:49" in visible
    assert "发布 昨天 23:49" not in html
    # Bare HH:MM remains only as part of the labeled ingest clock.
    assert visible.count("23:49") == 1


def test_safe_neutral_card_shows_record_backed_evidence_note():
    entry = _polluted_innovative_drug_entry()
    entry.update({
        "score": 4.0,
        "tag": "其他",
        "logic": "来源未显示明确投资催化",
        "score_path": "llm_safe_neutral_grounding",
        "safe_neutral_reason": "facts",
    })
    html = _render_cards([entry])
    assert "保守处理说明：事实表述未通过现有来源校验" in html
    assert "保守处理说明：具体原因未完整记录" not in html

    entry["safe_neutral_reason"] = "theme"
    assert "保守处理说明：主题关联未通过现有来源校验" in _render_cards([entry])

    entry["score_path"] = "llm_safe_neutral_contract"
    entry["safe_neutral_reason"] = "contract"
    assert "保守处理说明：输出格式或字段未通过校验" in _render_cards([entry])

    entry["score_path"] = "llm_safe_neutral_initial"
    entry["safe_neutral_reason"] = "initial"
    assert "保守处理说明：初次输出为保守中性，具体原因未记录" in _render_cards([entry])


def test_safe_neutral_card_without_record_says_reason_not_fully_recorded():
    entry = _polluted_innovative_drug_entry()
    entry.update({
        "score": 4.0,
        "logic": "来源未显示明确投资催化",
        "score_path": "llm_safe_neutral_grounding",
        "safe_neutral_reason": "",
    })
    html = _render_cards([entry])
    assert "保守处理说明：具体原因未完整记录" in html


def test_normal_formal_card_has_no_evidence_note():
    html = _render_cards([_polluted_innovative_drug_entry()])
    assert "保守处理说明：" not in html
