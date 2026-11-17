from core.processor import _apply_score_adjustments, _cap_low_value_announcement


def test_caps_conference_speech_auction_and_principle_research():
    cases = [
        ("佳士得上海拍卖落槌，总成交额1.18亿元人民币", "艺术"),
        ("高质量数据集和数据标注先行先试现场会在厦门市召开", "数据政策"),
        ("AI进化速递 | 吴清：深化人工智能与资本市场双向赋能研究", "AI政策"),
        ("2026年中国资本市场学术年会在沪开幕，周小川作开幕演讲，吴清朱忠明致辞", "资本市场政策"),
    ]
    for title, tag in cases:
        score, _, _ = _cap_low_value_announcement(6.0, tag, "低价值会议或表态", title)
        assert score == 5.0, title


def test_keeps_concrete_company_or_action():
    cases = [
        ("3个新进医保药就有1个江苏造，制药大省如何让国谈药加速进医院", "医药政策"),
        ("整机22万个零部件全部国产化 大型无人货运飞机W5000试飞成功", "科技"),
        ("高盛：将中国H股评级上调至“超配”", "其他"),
        ("伊朗称霍尔木兹海峡以东一艘违规油轮触雷起火", "地缘能源"),
        ("马斯克：未来的星舰飞行任务将把超过50太比特/秒的星链带宽送入轨道。", "其他"),
        ("XX公司董事会会议审议通过回购议案", "公司治理"),
    ]
    for title, tag in cases:
        score, _, _ = _cap_low_value_announcement(6.0, tag, "具体事实", title)
        assert score == 6.0, title


def test_does_not_raise_low_scores():
    score, _, _ = _cap_low_value_announcement(4.0, "艺术", "低价值", "佳士得上海拍卖落槌")
    assert score == 4.0


def test_apply_score_adjustments_caps_after_time_weight():
    score, _, _, capped = _apply_score_adjustments(
        6.0, "艺术", "低价值", {}, "", "佳士得上海拍卖落槌", False
    )
    assert score == 5.0
    assert capped is True


def test_apply_score_adjustments_does_not_report_an_untouched_score_as_capped():
    score, _, _, capped = _apply_score_adjustments(
        5.0, "公司公告", "具体事实", {}, "", "某公司公告获得2.3亿元订单", False
    )
    assert score == 5.0
    assert capped is False
