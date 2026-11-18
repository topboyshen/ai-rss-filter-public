"""P4: keep planned and opinion claims out of the "supports" label."""

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
        # No explicit phase or opinion marker: stay unknown rather than guess.
        ("摩根大通：CUDA护城河正在瓦解", "unknown"),
        ("某公司发布新产品", "unknown"),
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


def test_template_keeps_the_legacy_label_and_adds_the_phase_badge():
    template = (Path(__file__).resolve().parents[1] / "templates" / "event_radar.html").read_text(
        encoding="utf-8"
    )
    assert "source.stance_record_label" in template
    assert "source.phase_label" in template
