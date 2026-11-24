"""P4: keep planned, opinion, negated, and mixed claims out of ``supports``."""

from pathlib import Path

import pytest

from routes.event_radar_routes import (
    _evidence_phase,
    _evidence_stance_label,
    _source_view,
)


@pytest.mark.parametrize(
    ("claim", "phase"),
    [
        ("捷捷微电：全资子公司拟投资15亿元建设车规级芯片生产线", "planned"),
        ("韩媒：三星电子与苹果接近签署DRAM长期供应协议", "planned"),
        ("朗特智能：拟收购成都奥捷通信技术有限公司股权", "planned"),
        ("药明康德：获美国国防部从1260H名单移除，共同撤诉约定已获批", "completed"),
        ("世界首个300兆瓦压缩空气储能电站并网发电", "completed"),
        ("中信建投：物理AI是人工智能下一波浪潮 建议聚焦优质环节", "neither"),
        # Negation must not turn a denied plan into a planned event.
        ("包钢股份：暂无对钢铁资产进行剥离或分拆的计划", "negated"),
        ("公司没有收购计划", "negated"),
        ("公司不计划收购", "negated"),
        ("公司否认收购计划", "negated"),
        ("公司不予考虑收购", "negated"),
        # Negated completion is not completion.
        ("项目尚未完成", "not_completed"),
        ("公司未签署协议", "not_completed"),
        # The main clause wins in a mixed headline.
        ("企业法庭就追加临时措施申请作出裁定，公司拟上诉", "completed"),
        ("公司拟收购成都奥捷通信股权，交易已获批", "planned"),
        ("公司已签署框架协议，拟后续投资", "completed"),
        # A contrast clause starts a new assertion; negation must not cross it.
        ("公司暂无计划但拟投资", "planned"),
        ("公司尚未完成但已签署部分协议", "completed"),
        # Keep contrast and future wording from creating false negation.
        ("公司未来计划收购", "planned"),
        ("公司不排除收购计划", "planned"),
        # Remaining P1 diagnostic vocabulary.
        ("美国加州正式起诉OpenAI，指控其模型测试疏于管控、危害公共安全", "completed"),
        ("光启技术：子公司签17.9亿元超材料航空结构产品采购框架合同", "completed"),
        ("公司签订框架协议", "completed"),
        ("复星医药：控股子公司多款药品新纳入2026年国家医保目录", "completed"),
        ("因算力不足，DeepSeek对App/网页版限额使用", "completed"),
        ("云南锗业：高品质磷化铟单晶片项目投资额增至2.7亿元", "completed"),
        ("2026年APEC工商界活动形成14份成果文件", "completed"),
        ("摩根大通：CUDA护城河正在瓦解，英伟达面临的真正挑战不止于CEO离职", "neither"),
        # Future forms outrank the completed marker they contain.
        ("项目将完成", "planned"),
        ("公司将纳入国家医保目录", "planned"),
        ("摩根大通：获批设立新分行", "completed"),
        ("摩根大通：拟设立新分行", "planned"),
        # "签" is a completion only in a signing context, not in "标签".
        ("公司未签合同", "not_completed"),
        ("欧盟讨论AI标签规则", "unknown"),
        # Q-sample action completions, with scope guards.
        ("英伟达(NVDA.O)官方社媒账号发布黄仁勋回归照片。", "completed"),
        ("华兴资本控股：任命葛小波为首席执行官", "completed"),
        ("金石资本集团：短暂停牌，以待刊发内幕消息公告", "completed"),
        ("国机精工：金刚石散热产品向GPU类客户送样验证工作已开展", "completed"),
        ("丁薛祥会见新加坡国务资政李显龙", "completed"),
        ("友邦吊顶：拟将集成吊顶业务相关资产划转至全资子公司", "planned"),
        ("利好刷屏！A股公司，密集发布", "unknown"),
        ("A股停牌提示：5股今日停牌", "unknown"),
        # Future forms outrank the completion word they contain.
        ("公司将发布业绩预告", "planned"),
        ("公司将任命新首席执行官", "planned"),
        ("公司将短暂停牌", "planned"),
        ("公司将开展送样验证", "planned"),
        ("公司将会见客户", "planned"),
        ("公司未发布公告", "not_completed"),
        # No explicit phase or opinion marker: stay unknown rather than guess.
        ("某公司新产品", "unknown"),
        ("", "unknown"),
    ],
)
def test_evidence_phase_uses_frozen_claim_text_only(claim, phase):
    assert _evidence_phase(claim) == phase


def test_planned_claim_is_not_labelled_as_support():
    assert _evidence_stance_label("supports", "某公司拟投资15亿元") == "计划（未完成）"
    assert _evidence_stance_label("supports", "券商建议关注三条主线") == "观点/背景"


def test_completed_claim_keeps_the_support_label():
    assert _evidence_stance_label("supports", "公司已获批生产许可") == "支持记录"


def test_negated_claim_is_not_labelled_as_planned_or_completed():
    assert _evidence_stance_label("supports", "公司暂无收购计划") == "否认/澄清"
    assert _evidence_stance_label("supports", "项目尚未完成") == "未完成/未发生"


def test_denial_is_never_relabelled_by_phase():
    assert _evidence_stance_label("contradicts", "公司拟投资15亿元") == "否认记录"


def test_source_view_exposes_phase_and_labels_each_observation():
    raw = {
        "claim_text": "某公司拟投资15亿元",
        "stance": "supports",
        "observations": [
            {"claim_text": "某公司拟投资15亿元", "stance": "supports"},
            {"claim_text": "该公司已获批", "stance": "supports"},
        ],
    }

    source = _source_view(raw, {})

    assert source["phase"] == "planned"
    assert source["phase_label"] == "计划（未完成）"
    assert source["stance_label"] == "计划（未完成）"
    assert [item["stance_label"] for item in source["observations"]] == [
        "计划（未完成）",
        "支持记录",
    ]


def test_source_view_marks_negated_and_mixed_observations():
    raw = {
        "claim_text": "企业法庭就追加临时措施申请作出裁定，公司拟上诉",
        "stance": "supports",
        "observations": [
            {"claim_text": "公司暂无收购计划", "stance": "supports"},
            {"claim_text": "项目尚未完成", "stance": "supports"},
        ],
    }

    source = _source_view(raw, {})

    assert source["phase"] == "completed"
    assert source["phase_label"] == "已完成表述"
    assert [item["phase_label"] for item in source["observations"]] == [
        "否认/澄清",
        "未完成/未发生",
    ]


def test_template_keeps_the_legacy_label_and_adds_the_phase_badge():
    template = (Path(__file__).resolve().parents[1] / "templates" / "event_radar.html").read_text(
        encoding="utf-8"
    )
    assert "source.stance_record_label" in template
    assert "source.phase_label" in template
