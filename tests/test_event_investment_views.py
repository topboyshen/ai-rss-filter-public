from copy import deepcopy
from pathlib import Path

import pytest
from flask import Flask

from routes import event_radar_routes as radar


def _model(tab="overview", outcomes=None):
    return {
        "events": [],
        "tab": tab,
        "read_at": 1000,
        "pagination": {"limit": 30},
        "detail": {
            "event": {
                "event_id": "test-event",
                "title": "待核对的产业事件",
                "direction": "看多",
                "available_at": 200,
                "occurred_at": 100,
            },
            "stocks": [],
            "outcomes": outcomes or [],
        },
    }


def _render(monkeypatch, model):
    app = Flask(
        __name__,
        template_folder=str(Path(radar.__file__).resolve().parents[1] / "templates"),
    )
    monkeypatch.setattr(radar, "_cfg", lambda: {"feeds": []})
    monkeypatch.setattr(radar, "release_version", lambda: "local-test")
    before = deepcopy(model)
    with app.test_request_context("/dashboard/events"):
        result = radar._render_model(model, None).get_data(as_text=True)
    assert model == before
    return result


def _outcome(horizon, name="同名股票", **overrides):
    row = {
        "horizon": horizon,
        "security": {"name": name, "security_id": f"security-{name}"},
        "mapping": {"mapping_id": "mapping-a", "revision": 1},
        "benchmark": {"is_market_standard": True, "name": "沪深300"},
        "gross_return": -0.05,
        "excess_return": -0.02,
        "market_benchmark_return": -0.03,
        "market_excess_return": -0.02,
        "direction_hit": False,
        "materialized": True,
        "method_supported": True,
        "mapping_current": True,
        "data_status": "complete_close_only",
    }
    row.update(overrides)
    return row


def test_horizon_groups_preserve_each_security_mapping_and_result():
    outcomes = [
        _outcome("T+20", name="stock-b"),
        _outcome("T+1", mapping={"mapping_id": "mapping-a", "revision": 1}),
        _outcome("T+1", mapping={"mapping_id": "mapping-b", "revision": 2}),
        _outcome("T+3", name="stock-c"),
        _outcome("T+5", name="stock-d", mapping_current=False),
        _outcome("legacy", method_supported=False),
        _outcome(None),
    ]
    before = deepcopy(outcomes)
    groups = radar._investment_horizon_views(outcomes)
    assert [group["key"] for group in groups] == ["short", "swing", "other"]
    assert groups[0]["outcomes"] == outcomes[1:5]
    assert groups[1]["outcomes"] == outcomes[:1]
    assert groups[2]["outcomes"] == outcomes[5:]
    assert sum(len(group["outcomes"]) for group in groups) == len(outcomes)
    assert outcomes == before


def test_overview_has_questions_not_invented_outcomes_or_stock_recommendations(
    monkeypatch,
):
    html = _render(monkeypatch, _model())
    assert "短线关注" in html and "波段关注" in html
    assert "概览未读取市场结果，不判断是否到期、上涨或下跌" in html
    assert "待人工核对的研究问题" in html
    assert "暂无可展示的个股映射，不能据此猜测受益股票" in html
    assert "不新增评分 · 不生成买卖指令" in html
    assert "尚未到期" not in html
    assert 'href="#results-' not in html
    assert "tab=reaction" in html


def test_market_groups_show_losses_and_preserve_legacy_and_missing_states(monkeypatch):
    model = _model(
        "market",
        [
            _outcome("T+1", name="短线失败样本"),
            _outcome(
                "T+20",
                name="波段样本",
                data_status="missing_asset_close",
                gross_return=None,
            ),
            _outcome("old-horizon", name="旧记录", mapping_current=False),
        ],
    )
    model["detail"]["outcomes_truncated"] = True
    html = _render(monkeypatch, model)
    assert 'id="results-short"' in html and 'id="results-swing"' in html
    assert 'id="results-other"' in html
    assert (
        html.index('class="outcome-name">短线失败样本')
        < html.index('class="outcome-name">波段样本')
        < html.index('class="outcome-name">旧记录')
    )
    assert "−5.00%" in html and "−2.00%" in html
    assert "未命中" in html
    assert "缺少个股/证券收盘价" in html
    assert "个股映射已被后续规则退役" in html
    assert "仅展示前 3 条当前有效修订" in html
    assert "不跨股票或期限汇总" in html
    assert "不把事后涨幅当作可赚收益" in html


@pytest.mark.parametrize("outcomes", [[], [_outcome("T+1")]])
def test_empty_period_is_not_claimed_to_have_no_opportunities(monkeypatch, outcomes):
    model = _model("market", outcomes)
    model["detail"]["outcomes_truncated"] = bool(outcomes)
    html = _render(monkeypatch, model)
    if outcomes:
        assert "可能尚未到期、缺少数据或受展示条数限制" in html
        assert "不代表该周期没有机会" in html
    else:
        assert "本页尚无可展示的市场结果" in html
        assert 'href="#results-' not in html


def test_new_views_escape_event_stock_names_and_legacy_horizons(monkeypatch):
    xss = '<svg/onload=alert("horizon")>'
    model = _model("market", [_outcome(xss, name=xss)])
    model["detail"]["event"]["title"] = xss
    model["detail"]["stocks"] = [{"name": xss, "relations": []}]
    html = _render(monkeypatch, model)
    assert xss not in html
    assert "&lt;svg/onload=" in html
    assert 'id="results-other"' in html


def test_similar_tab_does_not_imply_a_current_investment_assessment(monkeypatch):
    html = _render(monkeypatch, _model("similar"))
    assert 'id="investment-title"' not in html
    assert 'id="news-kind"' not in html


def _headline_detail(title):
    detail = _model()["detail"]
    detail["event"]["title"] = title
    detail["sources"] = [{
        "evidence_id": "frozen-source",
        "url": "https://example.com/frozen-article",
        "claim_text": title,
        "stance": "supports",
    }]
    return detail


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("均普智能：子公司获得亿元订单", "catalyst_candidate"),
        ("均普智能获全球头部Tier 1智能底盘总成装配项目订单", "catalyst_candidate"),
        ("中信证券公告上半年净利润增长30%", "catalyst_candidate"),
        ("某公司新药获批", "catalyst_candidate"),
        ("中信银行创历史新高", "price_recap"),
        ("新易盛跌超5%", "price_recap"),
        ("某公司股价上涨5%", "price_recap"),
        ("光刻机板块再度走强", "price_recap"),
        ("银行板块震荡拉升", "price_recap"),
        ("某公司获得亿元订单，股价涨停", "mixed"),
        ("央行宣布降准，银行板块涨停", "background"),
        ("兴业证券展望后市：看好经济复苏", "background"),
        ("财政部发布政策解读", "background"),
        ("某公司预计股价上涨5%", "unknown"),
        ("某公司拟签订亿元合同", "unknown"),
        ("某公司计划获得亿元订单", "unknown"),
        ("某公司未能获得亿元订单", "unknown"),
        ("某公司未中标亿元订单", "unknown"),
        ("某公司没有获得亿元订单", "unknown"),
        ("某公司项目未投产", "unknown"),
        ("传闻某公司获得亿元订单", "unknown"),
        ("某公司否认获得亿元订单", "unknown"),
        ("某公司尚未签订亿元合同", "unknown"),
        ("去年曾获得亿元订单，今日回顾", "unknown"),
        ("复盘：中信银行创历史新高", "unknown"),
        ("铜箔价格上涨5%", "unknown"),
        ("某公司产量创历史新高", "unknown"),
        ("某公司营收上涨5%", "unknown"),
        ("银行板块业绩走强", "unknown"),
        ("某公司ROE创历史新高", "unknown"),
        ("某公司roa创历史新高", "unknown"),
        ("某公司毛利率创历史新高", "unknown"),
        ("某公司净利润率创历史新高", "unknown"),
        ("LME期铜创历史新高", "unknown"),
        ("LME期铜创下历史新高", "unknown"),
        ("国际油价涨超5%", "unknown"),
        ("现货黄金创历史新高", "unknown"),
        ("苹果手机售价2999美元", "unknown"),
        ("某公司获得100亿元订单", "catalyst_candidate"),
        ("某公司公告业绩：同比上涨30%", "catalyst_candidate"),
        ("某公司公告业绩:同比上涨30%", "catalyst_candidate"),
        ("某公司ROE：创历史新高", "unknown"),
        ("铜箔价格：上涨5%", "unknown"),
        ("某公司公告业绩：股价上涨30%", "mixed"),
        ("某公司公告业绩：股价涨停", "mixed"),
        ("某公司公告业绩：股价跌停", "mixed"),
        ("某公司ROE创新高，股价下跌5%", "price_recap"),
        ("LME期铜创历史新高；某公司股价大涨", "price_recap"),
        ("某公司公告业绩。新易盛跌超5%", "mixed"),
        ("银河证券公告上半年净利润增长30%", "catalyst_candidate"),
        ("银河证券股价上涨5%", "price_recap"),
        ("待核对的产业事件", "unknown"),
    ],
)
def test_news_kind_uses_only_conservative_source_headline_cues(title, expected):
    detail = _headline_detail(title)
    before = deepcopy(detail)
    assert radar._news_kind_view(detail)["key"] == expected
    assert detail == before


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("折叠机“三国杀”大战！苹果史上最贵新机要来了，顶配或达2999美元", "unknown"),
        ("多家头部厂商密集上调产品价格 电池涨价潮加速蔓延", "unknown"),
        ("国产GPU“双子星”大跌，市场在担心什么？", "unknown"),
        ("上半年上市券商盈利能力迎系统性修复，ROE重回十年高位", "unknown"),
        ("港股早报｜LME期铜创下历史新高 中国AI大模型调用量连续十九周领跑", "unknown"),
        ("国家水网建设全面提速 上半年25项重大水利工程开工", "unknown"),
        ("汽车行业“账期新规”落地 破解产业链资金难题", "background"),
        ("银河证券：AI应用价值持续兑现，国产超节点有望迎放量拐点", "unknown"),
    ],
)
def test_news_kind_fixed_diagnostic_headlines_remain_conservative(title, expected):
    # A policy topic is not proof of novelty, effectiveness or a stock beneficiary.
    assert radar._news_kind_view(_headline_detail(title))["key"] == expected


@pytest.mark.parametrize(
    "title",
    [
        "某公司：获得亿元订单消息不实",
        "某公司：签订亿元合同的报道不实",
        "某公司：获得亿元订单？没有此事",
        "某公司：未实际获得亿元订单",
        "某公司：未正式签署亿元合同",
        "某公司：未曾取得新药许可",
        "某公司公告已签署亿元合同，但订单消息不实，股价上涨5%",
    ],
)
def test_news_kind_explicit_denials_are_not_confirmed_catalysts(title):
    detail = _headline_detail(title)
    before = deepcopy(detail)
    assert radar._news_kind_view(detail)["key"] == "unknown"
    assert detail == before


@pytest.mark.parametrize(
    "title",
    [
        "某公司已实际获得亿元订单",
        "某公司正式签署亿元合同",
        "某公司公告上半年净利润增长30%，无重大不实记载",
        "某公司获得亿元订单，客户没有要求变更交期",
        "某公司新规格产品获得亿元订单",
        "某公司新规格药物获批",
        "某公司按新规划完成项目投产",
    ],
)
def test_news_kind_does_not_treat_every_negative_word_as_a_denial(title):
    assert radar._news_kind_view(_headline_detail(title))["key"] == "catalyst_candidate"


@pytest.mark.parametrize(
    "title",
    [
        "汽车行业账期新规正式实施",
        "行业账期新规预计明年落地",
        "行业监管办法公开征求意见",
        "保障中小企业款项支付条例解读",
    ],
)
def test_news_kind_policy_topic_does_not_assert_implementation_or_beneficiaries(title):
    view = radar._news_kind_view(_headline_detail(title))
    assert view["key"] == "background"
    assert "不能直接当作" in view["note"]
    assert "个股传导仍待核对" in view["note"]


@pytest.mark.parametrize(
    "title",
    ["某公司获得亿元订单", "汽车行业账期新规正式实施"],
)
@pytest.mark.parametrize(
    "change",
    [
        "no_sources", "other_title", "context", "refutes", "contradicts",
        "no_id", "no_url", "truncated", "contradiction", "legacy_refutation",
    ],
)
def test_news_kind_requires_bound_supported_untruncated_evidence(change, title):
    detail = _headline_detail(title)
    source = detail["sources"][0]
    if change == "no_sources":
        detail["sources"] = []
    elif change == "other_title":
        source["claim_text"] = "另一公司获得亿元订单"
    elif change in {"context", "refutes", "contradicts"}:
        source["stance"] = change
    elif change == "no_id":
        source.pop("evidence_id")
    elif change == "no_url":
        source.pop("url")
    elif change == "truncated":
        detail["sources_truncated"] = True
    else:
        detail["sources"].append({
            "claim_text": "公司否认订单",
            "stance": "contradicts" if change == "contradiction" else "refutes",
        })
    assert radar._news_kind_view(detail)["key"] == "unknown"


def test_news_kind_does_not_invent_evidence_from_summary_or_results():
    detail = _headline_detail("待核对的产业事件")
    detail["summary"] = "某公司获得亿元订单，股价涨停"
    detail["sources"][0]["excerpt"] = detail["summary"]
    detail["event"]["logic"] = detail["summary"]
    for gross_return in [-0.5, 0.5, None]:
        detail["outcomes"] = [_outcome("T+1", gross_return=gross_return)]
        assert radar._news_kind_view(detail)["key"] == "unknown"
    detail["sources"] = []
    detail["event"]["title"] = detail["summary"]
    assert radar._news_kind_view(detail)["key"] == "unknown"


@pytest.mark.parametrize("tab", ["overview", "market", "reaction"])
def test_news_kind_renders_same_unverified_candidate_for_both_period_views(
    monkeypatch, tab,
):
    model = _model(tab)
    model["detail"] = _headline_detail("均普智能获得亿元订单")
    html = _render(monkeypatch, model)
    assert 'data-news-kind="catalyst_candidate"' in html
    assert "新催化候选（首发未核验）" in html
    assert "不使用生成摘要或后续收益" in html
    assert "不是整个合并事件的定性" in html


def test_news_kind_bound_headline_is_escaped_and_recap_is_not_forecast(monkeypatch):
    title = '<svg/onload=alert("kind")>某公司股价上涨5%'
    model = _model()
    model["detail"] = _headline_detail(title)
    html = _render(monkeypatch, model)
    assert title not in html
    assert "&lt;svg/onload=" in html
    assert 'data-news-kind="price_recap"' in html
    assert "不代表后续还会同向涨跌" in html


@pytest.mark.parametrize("tab", ["overview", "market", "reaction"])
def test_policy_headline_renders_background_without_inventing_stock_mapping(monkeypatch, tab):
    model = _model(tab)
    model["detail"] = _headline_detail("汽车行业“账期新规”落地 破解产业链资金难题")
    html = _render(monkeypatch, model)
    assert 'data-news-kind="background"' in html
    assert "政策 / 宏观 / 观点背景" in html
    assert "暂无可展示的个股映射，不能据此猜测受益股票" in html
    assert "个股传导仍待核对" in html
    assert 'data-news-kind="catalyst_candidate"' not in html
