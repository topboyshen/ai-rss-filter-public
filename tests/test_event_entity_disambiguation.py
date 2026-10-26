import pytest

from core.asset_mapping import (
    is_direct_security_mention,
    map_article_to_assets,
)
from db.event_core import upsert_security


@pytest.mark.parametrize(
    ("text", "security_name"),
    (
        ("国家发改委：人工智能成为经济增长的澎湃动力源", "动力源"),
        ("公司利润大幅飙升，创新成为增长驱动力", "驱动力"),
        ("利润增长驱动力获得进一步增强", "驱动力"),
        ("创新成为核心动力源发布的重要抓手", "动力源"),
        ("中信证券：看好电子板块景气度回升", "中信证券"),
        ("中信证券（600030）：看好电子板块景气度回升", "中信证券"),
        ("中信证券(600030.SH)：汽车账期新规有望改善现金流", "中信证券"),
        ("中信证券（SH600030）认为汽车供应链回款有望改善", "中信证券"),
        ("中信证券（600030）分析师张三：看好电子板块", "中信证券"),
        ("中国银河证券股份有限公司（601881）：看好算力行业", "中国银河"),
        ("中国银河证券：Kimi K3开启多智能体时代", "中国银河"),
        ("中信证券认为A股有望进入修复阶段", "中信证券"),
        ("据中信证券研报，银行板块估值有望修复", "中信证券"),
        ("援引中国银河证券：Kimi K3开启多智能体时代", "中国银河"),
        ("中金公司：看好算力产业链景气度", "中金公司"),
        ("中信证券：美联储拟于下月降息", "中信证券"),
        ("中信证券：银行业净利润有望改善", "中信证券"),
        ("中信证券：拟上调银行业盈利预测", "中信证券"),
        ("国泰海通：看好券商板块估值修复", "国泰海通"),
        (
            "新筑股份：中信证券股份有限公司关于深圳证券交易所《关于成都市新筑路桥机械股份有限公司资产出售、发行股份及支付现金购买资产并募集配套资金申请的审核问询函回复》的核查意见",
            "中信证券",
        ),
        (
            "新筑股份：中信证券股份有限公司关于"
            + "深交所审核问询事项补充说明" * 12
            + "的核查意见",
            "中信证券",
        ),
        ("光大证券张宇生：医药板块中期已具备底部反转基础", "光大证券"),
        ("中信建投证券策略分析师夏凡捷：A股回调较为充分", "中信建投"),
        ("0728直通公司：欣旺达动力，嘉宾：申万宏源钱启敏", "申万宏源"),
        ("对话中金公司孙雷：投行必须加快转型", "中金公司"),
        ("连线广发证券刘晨明：市场有望迎来阶段性底部", "广发证券"),
        ("国泰海通方奕：市场企稳与转机或已不远", "国泰海通"),
        ("华泰证券港股策略：重视再平衡下的结构性机会", "华泰证券"),
        ("野村喊出万亿估值，东北证券只给一半", "东北证券"),
        (
            "申菱环境：中信建投证券股份有限公司关于公司可转债之发行保荐书",
            "中信建投",
        ),
        ("华泰证券机器人行业策略：量产节奏有望加快", "华泰证券"),
        ("全国农产品批发市场猪肉价格环比下降", "农产品"),
        ("外骨骼机器人市场规模超过16亿元", "机器人"),
        ("洛阳国资成立新能源产业发展公司", "立新能源"),
        ("多方参与设立新能源产业基金", "立新能源"),
        (
            "上海钢联发布数据显示，今日电池级碳酸锂价格上涨",
            "上海钢联",
        ),
        ("据紫金矿业提供的数据，全球铜产量下降", "紫金矿业"),
        ("根据紫金矿业发布的行业报告，金价上行", "紫金矿业"),
        ("来源：紫金矿业，2026全球铜市展望", "紫金矿业"),
        ("紫金矿业：预计国际金价明年上涨", "紫金矿业"),
        ("紫金矿业：预计贵州茅台净利润增长", "紫金矿业"),
        ("上海钢联发布数据显示上市公司盈利改善", "上海钢联"),
        ("上海钢联发布数据显示贵州茅台股价上涨", "上海钢联"),
        ("上海钢联发布数据显示今年销量增长", "上海钢联"),
        ("据紫金矿业提供的数据显示公司净利润增长", "紫金矿业"),
        ("由紫金矿业提供数据显示全球铜产量下降", "紫金矿业"),
        ("来源于紫金矿业的数据表明金价上行", "紫金矿业"),
        ("据紫金矿业发布报告显示金价上行", "紫金矿业"),
    ),
)
def test_direct_security_mention_rejects_common_nouns_and_research_bylines(
    text,
    security_name,
):
    assert not is_direct_security_mention(text, security_name)


@pytest.mark.parametrize(
    ("text", "security_name"),
    (
        ("动力源公告：公司签署重大合同", "动力源"),
        ("动力源：拟签署数据中心电源合同", "动力源"),
        ("驱动力（838275）发布年度业绩", "驱动力"),
        ("中信证券发布2026年半年度业绩", "中信证券"),
        ("中信证券（600030）：公司拟回购股份", "中信证券"),
        ("中信证券(600030.SH)：上半年净利润同比增长", "中信证券"),
        ("中国银河证券股份有限公司（601881）：公司披露半年报", "中国银河"),
        ("中信证券（600030）：看好汽车板块。中信证券公告拟回购股份", "中信证券"),
        ("中信证券：2025年净利润同比增长", "中信证券"),
        ("中信证券：上半年实现净利润同比增长", "中信证券"),
        ("中信证券：拟于8月回购公司股份", "中信证券"),
        ("中国银河公告拟回购股份", "中国银河"),
        ("市场消息显示中信证券股价上涨", "中信证券"),
        ("贵州茅台研报：渠道库存保持稳定", "贵州茅台"),
        ("动力源：上半年业绩同比增长", "动力源"),
        ("动力源：控股股东完成增持", "动力源"),
        ("动力源预计上半年扭亏为盈", "动力源"),
        ("机器人：公司拟回购股份", "机器人"),
        ("机器人触及涨停，成交额超过10亿元", "机器人"),
        ("农产品公告拟收购子公司股权", "农产品"),
        ("立新能源走出8天7板", "立新能源"),
        ("国泰海通：截至7月末累计回购1亿元股份", "国泰海通"),
        ("国盛证券：因涉嫌信息披露违法违规，被证监会立案", "国盛证券"),
        ("中信证券与光谷金控签署框架合作协议", "中信证券"),
        ("中金公司首席经济学家彭文生到龄退休，缪延亮接任", "中金公司"),
        ("方正证券敲定董事会换届方案，将迎一进一出", "方正证券"),
        ("西南证券原二级专务涉嫌严重违纪违法接受审查调查", "西南证券"),
        ("券商股上涨，中信证券涨停。另一事件：行业需求回暖", "中信证券"),
        ("紫金矿业发布半年度业绩报告", "紫金矿业"),
        ("紫金矿业：预计上半年净利润增长", "紫金矿业"),
        ("紫金矿业公告拟回购股份", "紫金矿业"),
        ("紫金矿业股价涨停，主力资金净买入", "紫金矿业"),
        ("邓晓峰重返紫金矿业前十大股东", "紫金矿业"),
        ("紫金矿业与某公司签署合作协议", "紫金矿业"),
        ("紫金矿业：预计全年矿产铜产量增长10%", "紫金矿业"),
        ("宁德时代：预计今年电池销量增长20%", "宁德时代"),
        ("宁德时代：预计麒麟电池明年量产", "宁德时代"),
        ("比亚迪：预计今年新能源汽车销量增长", "比亚迪"),
        ("紫金矿业发布数据显示本公司上半年净利润增长", "紫金矿业"),
        ("紫金矿业发布数据显示，其年度营收增长", "紫金矿业"),
    ),
)
def test_direct_security_mention_preserves_explicit_company_events(
    text,
    security_name,
):
    assert is_direct_security_mention(text, security_name)


@pytest.mark.parametrize(
    "text",
    (
        "A股主要指数集体上涨，银行股普跌。兴业证券展望后市认为全球科技股"
        "波动有望缓解，宏观流动性改善将为科技板块提供支撑。",
        "兴业证券展望后市：科技板块有望迎来修复窗口",
        "兴业证券展望后市，认为银行业净利润有望改善",
        "兴业证券在展望后市时表示，上市公司盈利预期有望改善",
    ),
)
def test_broker_market_outlook_is_not_a_direct_investee_mention(text):
    assert not is_direct_security_mention(text, "兴业证券")


@pytest.mark.parametrize(
    "text",
    (
        "兴业证券发布半年度业绩公告，董事长展望后市",
        "兴业证券：拟回购公司股份，并展望后市",
        "兴业证券展望后市认为科技行情有望修复。兴业证券公告净利润增长。",
    ),
)
def test_broker_own_event_survives_a_separate_market_outlook(text):
    assert is_direct_security_mention(text, "兴业证券")


@pytest.mark.parametrize("code_qualifier", ["", "（600030）", "(600030.SH)"])
def test_theme_candidates_do_not_promote_broker_byline_to_direct_relation(code_qualifier):
    research = map_article_to_assets(
        {
            "title": f"中信证券{code_qualifier}：看好银行板块估值修复",
            "tag": "国内宽信用",
            "summary": "LPR变化有望改善顺周期预期。",
            "direction": "看多",
        }
    )
    own_event = map_article_to_assets(
        {
            "title": f"中信证券{code_qualifier}发布半年度业绩公告",
            "tag": "国内宽信用",
            "summary": "公司披露营业收入与净利润。",
            "direction": "看多",
        }
    )

    research_candidate = next(
        item for item in research["security_candidates"] if item["name"] == "中信证券"
    )
    own_event_candidate = next(
        item for item in own_event["security_candidates"] if item["name"] == "中信证券"
    )
    assert research_candidate["relation_type"] == "concept"
    assert research_candidate["confidence"] < 0.9
    assert own_event_candidate["relation_type"] == "direct"
    assert own_event_candidate["confidence"] >= 0.9


def test_database_direct_mentions_use_entity_context_and_keep_exact_tickers():
    from db import database

    database.init_db()
    with database.get_connection() as conn:
        for exchange, ticker, name in (
            ("SSE", "600405", "动力源"),
            ("BSE", "838275", "驱动力"),
            ("SSE", "600030", "中信证券"),
            ("SSE", "601377", "兴业证券"),
            ("SSE", "601881", "中国银河"),
            ("SZSE", "300024", "机器人"),
            ("SZSE", "000061", "农产品"),
            ("SZSE", "001258", "立新能源"),
            ("SSE", "601211", "国泰海通"),
            ("SZSE", "300226", "上海钢联"),
            ("SSE", "601899", "紫金矿业"),
        ):
            upsert_security(
                conn,
                exchange=exchange,
                ticker=ticker,
                name=name,
                security_id=f"{exchange}:{ticker}",
                asset_type="equity",
                aliases=[name, ticker],
                now=100,
            )
        conn.commit()

        false_matches = database._direct_equity_mentions(
            conn,
            "国家发改委称人工智能是澎湃动力源；利润增长驱动力增强。"
            "中信证券：看好电子板块。中国银河证券：Kimi K3加速迭代。"
            "外骨骼机器人需求上升，全国农产品价格回落。"
            "多方参与设立新能源产业基金。国泰海通：看好券商修复。",
            limit=10,
        )
        assert false_matches == []

        provider_matches = database._direct_equity_mentions(
            conn,
            "上海钢联发布数据显示，今日MMLC电池级碳酸锂价格上涨。"
            "据紫金矿业提供的数据，全球铜产量下降。",
            limit=10,
        )
        assert provider_matches == []

        assert database._direct_equity_mentions(
            conn,
            "兴业证券展望后市认为全球科技股波动有望缓解。",
        ) == []
        own_broker = database._direct_equity_mentions(
            conn,
            "兴业证券发布半年度业绩公告。",
        )
        assert [item["security_id"] for item in own_broker] == ["SSE:601377"]
        explicit_broker_ticker = database._direct_equity_mentions(
            conn,
            "证券代码601377：公司拟回购股份。",
        )
        assert [item["security_id"] for item in explicit_broker_ticker] == [
            "SSE:601377"
        ]

        exact_ticker_match = database._direct_equity_mentions(
            conn,
            "上海钢联（300226）发布数据显示，电池级碳酸锂价格上涨。",
            limit=10,
        )
        assert [
            (item["security_id"], item["confidence"])
            for item in exact_ticker_match
        ] == [("SZSE:300226", 0.96)]

        name_match = database._direct_equity_mentions(
            conn,
            "动力源公告公司签署数据中心电源合同。",
            limit=10,
        )
        assert [(item["ticker"], item["confidence"]) for item in name_match] == [
            ("600405", 0.90)
        ]

        ticker_match = database._direct_equity_mentions(
            conn,
            "证券代码600030发布半年度业绩。",
            limit=10,
        )
        assert [(item["name"], item["confidence"]) for item in ticker_match] == [
            ("中信证券", 0.96)
        ]


def test_database_direct_mentions_only_validate_indexed_text_candidates(
    monkeypatch,
):
    from core import asset_mapping
    from db import database

    rows = [
        (
            f"SSE:{index:06d}",
            f"样本公司{index:04d}",
            f"{index:06d}",
            "SSE",
        )
        for index in range(5_876)
    ]
    rows.append(("SSE:688256", "目标公司", "688256", "SSE"))
    checked_names = []
    monkeypatch.setattr(database, "_equity_name_rows", lambda _conn: rows)
    monkeypatch.setattr(
        asset_mapping,
        "is_direct_security_mention",
        lambda _text, name, **_kwargs: checked_names.append(name) or name == "目标公司",
    )

    matches = database._direct_equity_mentions(
        object(),
        "目标公司公告新一代产品进入交付期。",
        limit=10,
    )

    assert [item["security_id"] for item in matches] == ["SSE:688256"]
    assert checked_names == ["目标公司"]
