"""Asset mapping rules for turning news into tradable themes.

This module is deliberately local and deterministic. It gives the dashboard a
stable analyst layer before any paid market-data feed is connected.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class SecurityExposure:
    """Auditable deterministic exposure used by one narrowly scoped rule."""

    name: str
    relation_type: str
    relevance_weight: float
    rationale: str
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssetRule:
    theme: str
    keywords: tuple[str, ...]
    asset_classes: tuple[str, ...]
    sectors: tuple[str, ...]
    stocks: tuple[str, ...]
    watch: str
    rationale: str
    # Daily close proxy available in ``future_prices``.  ``None`` is explicit:
    # do not invent an A-share return for themes (for example USD liquidity)
    # whose assets are not collected by the close-price pipeline.
    backtest_symbol: str | None = None
    security_exposures: tuple[SecurityExposure, ...] = ()


ASSET_RULES: tuple[AssetRule, ...] = (
    AssetRule(
        theme="A股极端行情",
        keywords=("A股收盘", "沪指", "深成指", "创业板指", "科创50", "北证50", "沪深京", "股指期货"),
        asset_classes=("A股风险偏好", "股指期货", "小盘成长"),
        sectors=("高贝塔成长", "券商", "小微盘"),
        stocks=("沪深300ETF", "中证500ETF", "创业板ETF"),
        watch="观察成交额、跌停家数、指数修复力度与政策对冲信号",
        rationale="指数、市场广度与高贝塔板块同步走弱，反映A股风险偏好和流动性承压",
        backtest_symbol="sh000001",
    ),
    AssetRule(
        theme="扩内需政策",
        keywords=("扩内需", "扩大消费", "内需潜力", "促消费", "提振消费", "消费规划", "以旧换新"),
        asset_classes=("A股风险偏好", "消费风格", "顺周期"),
        sectors=("消费", "商贸零售", "家电", "汽车", "服务消费"),
        stocks=("消费ETF", "家电ETF", "汽车ETF"),
        watch="跟踪政策细则、居民收入预期、社零增速和消费板块成交反馈",
        rationale="国家级扩内需政策会影响消费预期、顺周期盈利与A股风险偏好",
        backtest_symbol="sz399001",
    ),
    AssetRule(
        theme="美元流动性",
        # 裸词“美元”也可能只是营收、估值或报价的计价单位；要求明确的
        # 美元体系/汇率锚点，不因同一标题包含金额而否决真实宏观事件。
        keywords=(
            "RRP", "美联储", "美债", "SOFR", "TGA", "隔夜逆回购协议",
            "美元流动性", "美元指数", "美元汇率", "美元实际利率", "美元短缺",
        ),
        asset_classes=("美元指数", "美债短端", "纳指", "黄金", "港股科技"),
        sectors=("黄金", "跨境ETF", "港股科技映射"),
        stocks=("华安黄金ETF", "博时黄金ETF", "恒生科技ETF"),
        watch="观察RRP是否连续变化，以及美债收益率、美元指数和黄金的联动",
        rationale="宏观流动性变量变化，可能影响全球风险偏好和估值环境",
    ),
    AssetRule(
        theme="国内宽信用",
        keywords=(
            "降准", "降息", "MLF", "LPR", "央行", "中国人民银行", "人民银行",
            "央行开展", "逆回购操作", "社融", "信贷", "逆周期",
        ),
        asset_classes=("A股风险偏好", "人民币资产", "国债期货"),
        sectors=("券商", "地产链", "银行", "建筑建材"),
        stocks=("东方财富", "中信证券", "保利发展", "招商银行"),
        watch="跟踪资金利率、社融信贷和成交量能否同步改善",
        rationale="政策或信用条件变化会影响估值修复和顺周期板块轮动",
        backtest_symbol="sh000001",
    ),
    AssetRule(
        theme="创新药",
        keywords=(
            "创新药", "创新医药", "新药研发", "药物研发", "新药获批",
            "新药上市", "新药申报", "新药临床", "1类新药", "ADC", "PD-1",
            "License-out", "授权出海",
        ),
        asset_classes=("医药成长", "创新药产业链", "港股生物科技"),
        sectors=("创新药", "生物制品", "CXO", "医疗研发服务"),
        # This theme rule is not security-level evidence.  Keep individual
        # names empty until the source itself or a quote/security resolver
        # supplies an auditable company mapping.
        stocks=(),
        watch="观察中报业绩、临床进展、授权出海交易、海外审批和医保放量是否兑现",
        rationale="研发、临床和国际化兑现信号可能改善创新药产业链的业绩预期与风险偏好",
    ),
    AssetRule(
        theme="AI算力",
        keywords=("OpenAI", "ChatGPT", "人工智能", "算力", "大模型", "GPU", "英伟达", "服务器", "光模块"),
        asset_classes=("成长风格", "纳指映射"),
        sectors=("CPO", "服务器", "光模块", "AI应用", "半导体"),
        stocks=("中际旭创", "新易盛", "工业富联", "浪潮信息", "寒武纪"),
        watch="观察订单兑现、业绩指引、海外龙头和成交额是否共振",
        rationale="产业趋势或订单变化可能提升成长股风险偏好",
        backtest_symbol="sz399006",
    ),
    AssetRule(
        theme="半导体国产化",
        keywords=("芯片", "半导体", "国产替代", "光刻", "存储", "HBM", "SSD", "DRAM"),
        asset_classes=("科技成长", "国产替代"),
        sectors=("半导体设备", "存储芯片", "材料", "封测"),
        stocks=("北方华创", "中微公司", "兆易创新", "江波龙", "北京君正"),
        watch="跟踪价格周期、库存变化、国产替代订单和业绩兑现",
        rationale="供需周期或国产化信号会影响科技主线弹性",
        backtest_symbol="sz399006",
    ),
    AssetRule(
        theme="贵金属",
        keywords=("黄金", "金价", "央行购金", "贵金属", "白银"),
        asset_classes=("黄金", "白银", "避险资产"),
        sectors=("黄金珠宝", "有色金属"),
        stocks=("山东黄金", "中金黄金", "紫金矿业", "赤峰黄金"),
        watch="观察美元实际利率、央行购金和黄金ETF持仓变化",
        rationale="避险需求和实际利率预期变化会直接影响贵金属定价",
    ),
    AssetRule(
        theme="资源安全",
        keywords=("关键矿产", "锑", "稀土", "战略资源", "矿业项目", "供应链重构", "资源安全"),
        asset_classes=("资源品", "国产替代", "供应链安全"),
        sectors=("小金属", "稀土永磁", "有色金属", "军工材料"),
        stocks=("湖南黄金", "华钰矿业", "北方稀土", "中国稀土", "厦门钨业"),
        watch="观察海外资源政策、出口管制、价格走势和国内替代订单",
        rationale="关键资源政策和供给扰动会影响上游材料定价与供应链安全溢价",
    ),
    AssetRule(
        theme="地产链",
        keywords=("地产", "房企", "商品房", "按揭", "城中村", "保交楼"),
        asset_classes=("顺周期", "信用风险偏好"),
        sectors=("房地产", "银行", "建材", "家电"),
        stocks=("保利发展", "万科A", "东方雨虹", "格力电器"),
        watch="观察销售高频、融资支持和政策执行强度",
        rationale="地产信用和销售变化会影响顺周期预期和金融资产定价",
        backtest_symbol="sh000001",
    ),
    AssetRule(
        theme="新能源建设",
        keywords=(
            "风电", "风机", "海上风电", "光伏", "组件", "逆变器",
            "新能源装机", "清洁能源", "核电", "核能", "生物质能",
        ),
        asset_classes=("新能源", "清洁电力", "能源装备"),
        sectors=("风电", "光伏", "核电", "储能", "电力建设"),
        stocks=(
            "金风科技", "运达股份", "明阳智能", "阳光电源",
            "中国核电", "中国广核", "中国能建", "南网储能",
        ),
        watch="跟踪新增装机、设备招标、项目开工、利用小时和产业链盈利兑现",
        rationale="风光核等清洁能源建设变化会沿装机、设备、运营和配套环节传导",
        security_exposures=(
            SecurityExposure(
                "金风科技", "direct", 1.00,
                "风电整机制造商；传导路径为风电装机增长→风机设备需求。",
                ("风电", "风机", "海风"),
            ),
            SecurityExposure(
                "运达股份", "direct", 0.98,
                "风电整机制造商；传导路径为风电装机增长→风机设备需求。",
                ("风电", "风机", "海风"),
            ),
            SecurityExposure(
                "明阳智能", "direct", 0.96,
                "风电整机与海上风电设备企业；传导路径为装机增长→设备需求。",
                ("风电", "风机", "海风"),
            ),
            SecurityExposure(
                "阳光电源", "ecosystem", 0.90,
                "光伏逆变器和储能环节企业；传导路径为光伏装机增长→电力转换与储能需求。",
                ("光伏", "逆变器", "储能"),
            ),
            SecurityExposure(
                "中国核电", "direct", 0.86,
                "核电运营企业；传导路径为核电建设与装机增长→运营资产规模变化。",
                ("核电", "核能"),
            ),
            SecurityExposure(
                "中国广核", "direct", 0.84,
                "核电运营企业；传导路径为核电建设与装机增长→运营资产规模变化。",
                ("核电", "核能"),
            ),
            SecurityExposure(
                "中国能建", "ecosystem", 0.76,
                "能源工程建设企业；传导路径为能源项目投资→勘察设计与工程建设需求。",
                ("能源项目", "能源投资", "新能源建设", "清洁能源"),
            ),
            SecurityExposure(
                "南网储能", "downstream", 0.72,
                "储能运营企业；传导路径为新能源占比提升→电网调节与储能需求。",
                ("储能", "风电光伏", "新能源装机"),
            ),
        ),
    ),
    AssetRule(
        theme="能源通胀",
        # “能源”不能单独触发：新能源、核能和能源规划都包含该词，
        # 但并不构成油气/煤炭价格冲击。
        keywords=("原油", "OPEC", "油价", "天然气", "煤炭", "燃油", "油气"),
        asset_classes=("原油", "通胀预期", "商品"),
        sectors=("石油石化", "煤炭", "化工", "航空"),
        stocks=("中国石油", "中国海油", "陕西煤业", "荣盛石化", "中国国航"),
        watch="观察库存、供给扰动、地缘风险和通胀预期变化",
        rationale="能源价格变化会传导至通胀、利润分配和风险偏好",
    ),
    AssetRule(
        theme="短线情绪",
        keywords=("龙虎榜", "涨停", "跌停", "异动", "成交额", "连板"),
        asset_classes=("小盘风格", "题材交易"),
        sectors=("活跃题材", "高换手板块"),
        stocks=("以当日龙虎榜和涨停池为准",),
        watch="观察次日承接、成交额和同题材扩散情况",
        rationale="交易热度变化反映活跃资金方向和风险偏好",
        backtest_symbol="sz399006",
    ),
)


_MAPPING_VERSION: str | None = None


def get_mapping_version() -> str:
    """Return a deterministic version for the complete mapping implementation.

    A backtest signal stores this explicit writer identity and source hash with
    the mapping result. Hashing
    the source file, rather than only ``ASSET_RULES``, also versions matching
    precedence and guard logic.  The value is cached for the lifetime of an
    immutable application image.
    """
    global _MAPPING_VERSION
    if _MAPPING_VERSION is not None:
        return _MAPPING_VERSION
    try:
        payload = Path(__file__).read_bytes()
    except OSError:
        # Source files are present in production images, but keep a deterministic
        # fallback for bytecode-only packaging and tests.
        payload = repr(ASSET_RULES).encode("utf-8")
    from core.source_contract import MAPPING_WRITER_MARKER

    _MAPPING_VERSION = (
        f"asset-map:{MAPPING_WRITER_MARKER}:"
        + hashlib.sha256(payload).hexdigest()[:12]
    )
    return _MAPPING_VERSION


def _contains_any(text: str, keywords: Iterable[str]) -> bool:
    upper_text = unicodedata.normalize("NFKC", str(text or "")).upper()
    for keyword in keywords:
        upper_keyword = unicodedata.normalize("NFKC", str(keyword or "")).upper()
        if not upper_keyword:
            continue
        if re.fullmatch(r"[A-Z0-9][A-Z0-9.+-]*", upper_keyword):
            if re.search(
                rf"(?<![A-Z0-9]){re.escape(upper_keyword)}(?![A-Z0-9])",
                upper_text,
            ):
                return True
            continue
        if upper_keyword in upper_text:
            return True
    return False


def _has_usd_fx_context(text: str) -> bool:
    """Keep dollar FX moves/pairs, not price amounts or a company's 兑付."""
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")))
    return bool(re.search(
        r"(?<![0-9零〇一二两三四五六七八九十百千万亿兆数几])"
        r"美元(?:小幅|大幅|持续|继续|全线|进一步)?(?:上涨|下跌|走强|走弱|升值|贬值)"
        r"|美元兑(?:日元|人民币|欧元|英镑|瑞郎|港元|澳元|纽元|加元)"
        r"|(?:日元|人民币|欧元|英镑|瑞郎|港元|澳元|纽元|加元)兑美元",
        compact,
    ))


_LEXICALLY_AMBIGUOUS_SECURITY_NAMES = frozenset(
    {"动力源", "驱动力", "机器人", "农产品", "数字人"}
)
_INDUSTRY_NOUN_SECURITY_NAMES = frozenset({"机器人", "农产品", "数字人"})
_PREFIX_OVERLAP_SECURITY_LEFT_CONTEXT = {
    "立新能源": ("成", "设", "建", "独", "创"),
    # Country/region phrases are not the listed issuer 国新能源.
    "国新能源": ("中", "我", "全", "各"),
}
_PREFIX_OVERLAP_SECURITY_RIGHT_CONTEXT = {
    # A currency name is not the issuer, even in "A股数字人民币概念".
    "数字人": ("民币", "拟人化", "直播", "主播", "客服", "行业", "产业"),
}
# An immediately adjoining organization suffix extends the issuer short name
# into another entity. This is local grammar, not a headline-wide exclusion.
_ADJACENT_ORGANIZATION_SUFFIX_RE = re.compile(r"(?:行业|产业|工业|业)?(?:协会|联合会|商会)")
# Here 学 belongs to both 化学 and 学会. Do not generalize the single 会 suffix:
# 中国化学会同… / 中国化学会计… still denote the issuer, not this organization.
_CHEMICAL_SOCIETY_NAME_RE = re.compile(r"中国化学会(?!同|计)")
_ORGANIZATION_CODE_PREFIX_RE = re.compile(
    r"^[\[\]（）()【】「」『』“”‘’\"'《》〈〉{}:：]*(?:(?:证券|股票|a股)?代码[：:=]?)?"
    r"(?:(?:sh|sz|bj|sse|szse|bse|沪|深|京)[.:：]?)?$"
)
_AMBIGUOUS_ENTITY_LEFT_CONTEXT = (
    "上市公司",
    "公司",
    "股票",
    "个股",
    "证券",
    "a股",
)
_AMBIGUOUS_ENTITY_RIGHT_CONTEXT = (
    "公司",
    "股份",
    "公告",
    "披露",
    "发布",
    "签署",
    "中标",
    "获批",
    "获得",
    "收到",
    "拟",
    "回购",
    "分红",
    "增持",
    "减持",
    "停牌",
    "复牌",
    "涨停",
    "跌停",
    "股价",
    "董事",
    "股东",
    "净利润",
    "营收",
    "业绩",
    "盈利",
    "扭亏",
    "预计",
    "控股股东",
    "实际控制人",
    "上半年",
    "下半年",
    "一季度",
    "二季度",
    "三季度",
    "前三季度",
    "订单",
    "合同",
    "收购",
    "重组",
    "融资",
    "上市",
    "触及",
    "走出",
    "封板",
    "连板",
    "成交",
    "涨超",
    "跌超",
)
_INDUSTRY_NOUN_ENTITY_RIGHT_CONTEXT = (
    "公告",
    "披露",
    "发布公告",
    "拟",
    "回购",
    "分红",
    "增持",
    "减持",
    "停牌",
    "复牌",
    "涨停",
    "跌停",
    "股价",
    "净利润",
    "营收",
    "业绩",
    "控股股东",
    "实际控制人",
    "触及",
    "走出",
    "封板",
    "连板",
    "成交额",
    "涨超",
    "跌超",
)
_AMBIGUOUS_COMMON_LEFT_CONTEXT = (
    "澎湃",
    "增长",
    "利润",
    "业绩",
    "核心",
    "主要",
    "强劲",
    "创新",
    "发展",
    "需求",
    "经济",
    "重要",
    "关键",
    "潜在",
)
_BROKER_SECURITY_NAMES = frozenset(
    {
        "东方财富",
        "中国银河",
        "中金公司",
        "中信建投",
        "申万宏源",
        "国联民生",
        "国泰海通",
    }
)
_BROKER_CODE_QUALIFIER_RE = re.compile(
    r"(?:证券股份有限公司|证券有限公司|股份有限公司|有限公司|证券|研究所|研究院)?"
    r"[（(](?P<prefix>sh|sz|bj)?(?P<ticker>[0-9]{6})"
    r"(?:\.(?P<suffix>sh|sz|bj))?[）)]",
    re.IGNORECASE,
)
_BROKER_OWN_EVENT_PREFIXES = (
    "本公司",
    "公司",
    "公告",
    "披露",
    "截至",
    "累计",
    "净利润",
    "营业收入",
    "营收",
    "业绩",
    "股价",
    "盈利",
    "亏损",
    "控股股东",
    "实际控制人",
    "董事会",
    "监事会",
    "股东",
    "子公司",
    "全资子公司",
    "收到",
    "完成",
    "回购",
    "分红",
    "停牌",
    "复牌",
    "发行",
    "配股",
    "定增",
    "重组",
    "收购",
    "诉讼",
    "立案",
    "处罚",
)
_BROKER_PROFESSIONAL_ROLE_MARKERS = (
    "保荐书",
    "保荐机构",
    "核查意见",
    "财务顾问",
    "受托管理",
    "持续督导",
    "承销保荐",
)
_BROKER_RESEARCH_CONTENT_MARKERS = (
    "投资策略",
    "行业策略",
    "策略报告",
    "配置建议",
    "研究观点",
    "最新观点",
)
_RESEARCH_ATTRIBUTION_OPENERS = (
    "称",
    "认为",
    "表示",
    "指出",
    "预计",
    "预测",
    "建议",
    "看好",
    "维持",
    "首予",
    "上调",
    "下调",
    "点评",
    "研报",
    "研究报告",
    "发布研报",
    "发布研究报告",
    "给出",
    "只给",
    "喊出",
)
_DATA_PROVIDER_ATTRIBUTION_OPENERS = (
    "发布数据显示",
    "发布的数据显示",
    "监测数据显示",
    "统计数据显示",
    "数据统计显示",
    "发布数据称",
)
_DATA_PROVIDER_OPINION_RE = re.compile(
    r"^[：:](认为|看好|预计|预测|分析|指出|称)"
)
_DATA_PROVIDER_SOURCE_LEFT_CONTEXT = (
    "据",
    "根据",
    "援引",
    "来自",
    "由",
    "来源于",
    "来源：",
    "来源:",
)
_DATA_PROVIDER_SOURCE_OBJECT_RE = re.compile(
    r"^(?:发布|提供|披露|统计|监测)?的?"
    r"(?:数据|统计|行业报告|研究报告|报告|调查|监测|指数|榜单)"
)
_DATA_PROVIDER_OWN_EVENT_PERIOD = (
    r"(?:20\d{2}年|本年度|今年|明年|后年|全年|上半年|下半年|"
    r"一季度|二季度|三季度|四季度|前三季度|年度)"
)
_DATA_PROVIDER_OWN_EVENT_ACTION_RE = (
    r"(?:预计|实现|实现了|录得|完成|披露|公告|拟)?"
    r"(?:营业收入|营收|净利润|业绩|盈利|亏损|产量|销量|交付量|"
    r"订单|分红|回购|增持|减持|收购|中标|签署|投产|停产|股价)"
)
_DATA_PROVIDER_OWN_FINANCIAL_ACTION_RE = (
    r"(?:预计|实现|实现了|录得|完成|披露|公告|拟)?"
    r"(?:营业收入|营收|净利润|业绩|盈利|亏损|分红|回购)"
)
_DATA_PROVIDER_EXTERNAL_FORECAST_MARKERS = (
    "全球",
    "国际",
    "行业",
    "市场",
    "板块",
    "产业链",
    "指数",
    "价格",
    "金价",
    "铜价",
    "锂价",
)
_BROKER_ATTRIBUTION_LEFT_CONTEXT = (
    "嘉宾",
    "嘉宾：",
    "嘉宾:",
    "对话",
    "专访",
    "访谈",
    "连线",
)
_BROKER_PERSON_BYLINE_ROLES = (
    "分析师",
    "研究员",
    "经济学家",
    "策略师",
    "研究主管",
    "研究负责人",
    "首席",
)
# Requiring a common surname avoids treating a short corporate subject such as
# ``公告：`` as a person's name.  Explicit role-labelled bylines are handled
# separately and do not depend on this list.
_COMMON_CHINESE_SURNAMES = (
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华"
    "金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方"
    "俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅"
    "皮卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明"
    "臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路"
    "娄危江童颜郭梅盛林钟徐邱骆高夏蔡田樊胡凌霍虞万支柯管卢"
    "莫经房裘缪干解应宗丁宣邓郁单杭洪包诸左石崔吉龚程嵇邢裴"
    "陆荣翁荀羊甄曲封储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车"
    "侯宓蓬全班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶"
    "幸司韶郜黎蓟薄印宿白怀蒲台从鄂索咸籍赖卓蔺屠蒙池乔阴胥"
    "能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍郤璩桑桂濮牛寿通"
    "边扈燕冀郏浦尚农温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易"
    "慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东欧利师巩聂晁"
    "勾敖融冷訾辛阚那简饶空曾毋沙乜养鞠须丰巢关蒯相查后荆红"
    "游竺权逯盖益桓公"
)


def _broker_tail_has_own_subject(text: str) -> bool:
    value = text.lstrip("：:，,；;。.!！？?【[（(")
    if value.startswith(_BROKER_OWN_EVENT_PREFIXES):
        return True
    # A bare ``拟`` is not a company subject: research headlines frequently
    # begin with phrases such as ``拟上调行业盈利预测``.  Accept it only when it
    # leads to a concrete listed-company action.
    if re.match(
        r"^拟(?:于[^，,。；;]{0,12})?(?:回购|分红|发行|配股|定增|重组|收购|"
        r"注销|增持|减持|设立|转让|受让|出售|购买|投资|签署|中标)",
        value,
    ):
        return True
    if re.match(
        r"^被(?:证监会|监管机构|公安机关|法院)[^，,。；;]{0,16}"
        r"(?:立案|调查|处罚|采取监管措施)",
        value,
    ):
        return True
    # Enforcement headlines often put the causal phrase before the corporate
    # action: ``券商：因涉嫌信息披露违法，被证监会立案``.  Treat only a bounded
    # regulator/action grammar as the broker's own event; a generic
    # ``因行业景气...`` research byline remains rejected.
    if re.match(
        r"^(?:因|涉嫌)[^，,。；;]{0,48}[，,]?(?:被(?:证监会|监管机构|公安机关|法院)"
        r"[^，,。；;]{0,16})?(?:立案|调查|处罚|采取监管措施)",
        value,
    ):
        return True
    return bool(
        re.match(
            r"^(?:20\d{2}年(?:上半年|下半年|[一二三四]季度|前三季度)?|"
            r"上半年|下半年|一季度|二季度|三季度|"
            r"四季度|前三季度|年度)(?:实现|预计实现)?(?:业绩|净利润|营业收入|营收|"
            r"盈利|亏损|扭亏|分红|回购)",
            value,
        )
    )


def _broker_tail_is_person_byline(text: str) -> bool:
    """Return whether broker-adjacent text is an analyst/person byline."""
    value = text.lstrip("：:，,；;。.!！？?【[（(")
    if not value or _broker_tail_has_own_subject(value):
        return False

    role_pattern = "|".join(map(re.escape, _BROKER_PERSON_BYLINE_ROLES))
    if re.match(
        rf"^[\u4e00-\u9fff]{{0,8}}(?:{role_pattern})"
        r"[\u4e00-\u9fff]{2,4}(?:[/／、][\u4e00-\u9fff]{2,4})*[：:]",
        value,
    ):
        return True

    person_name = rf"[{re.escape(_COMMON_CHINESE_SURNAMES)}][\u4e00-\u9fff]{{1,2}}"
    return bool(re.match(rf"^{person_name}(?:[/／、]{person_name})*[：:]", value))


def _data_provider_tail_has_own_event(
    tail: str,
    *,
    name: str,
    allow_operating_forecast: bool,
) -> bool:
    """Return whether attributed text is explicitly about the named issuer."""
    value = tail.lstrip("，,:：")
    self_subject = rf"(?:本公司|该公司|公司自身|其|{re.escape(name)})"
    if re.match(
        rf"^(?:{self_subject})(?:的)?"
        rf"(?:{_DATA_PROVIDER_OWN_EVENT_PERIOD})?"
        rf"{_DATA_PROVIDER_OWN_EVENT_ACTION_RE}",
        value,
    ):
        return True
    if re.match(
        rf"^{_DATA_PROVIDER_OWN_EVENT_PERIOD}"
        rf"{_DATA_PROVIDER_OWN_FINANCIAL_ACTION_RE}",
        value,
    ):
        return True
    if not allow_operating_forecast:
        return False
    forecast_clause = value[:32]
    if any(
        marker in forecast_clause
        for marker in _DATA_PROVIDER_EXTERNAL_FORECAST_MARKERS
    ):
        return False
    if re.match(
        rf"^{_DATA_PROVIDER_OWN_EVENT_PERIOD}"
        r"[^，,。；;！!？?\n]{0,12}(?:产量|销量|交付量)",
        value,
    ):
        return True
    return bool(
        re.match(
            r"^[^，,。；;！!？?\n]{1,16}"
            rf"{_DATA_PROVIDER_OWN_EVENT_PERIOD}"
            r"[^，,。；;！!？?\n]{0,6}(?:量产|投产)",
            value,
        )
    )


def _is_data_provider_attribution(
    compact_text: str,
    *,
    name: str,
    start: int,
    end: int,
) -> bool:
    """Reject a named data/research source that is not the event subject."""
    boundaries = "，,。；;！!？?\n"
    clause_start = max(
        (compact_text.rfind(boundary, 0, start) for boundary in boundaries),
        default=-1,
    ) + 1
    sentence_boundaries = "。；;！!？?\n"
    following_boundaries = [
        position
        for boundary in sentence_boundaries
        if (position := compact_text.find(boundary, end)) >= 0
    ]
    clause_end = min(following_boundaries, default=len(compact_text))
    left = compact_text[clause_start:start]
    tail = compact_text[end:clause_end]

    opener = next(
        (item for item in _DATA_PROVIDER_ATTRIBUTION_OPENERS if tail.startswith(item)),
        None,
    )
    if opener is not None:
        # Preserve only an explicitly anchored self-subject.  A loose marker
        # search would incorrectly treat ``上市公司净利润`` or another named
        # company's share price as an event about the data publisher.
        remainder = tail[len(opener) :].lstrip("，,:：")
        if _data_provider_tail_has_own_event(
            remainder,
            name=name,
            allow_operating_forecast=False,
        ):
            return False
        return True
    opinion = _DATA_PROVIDER_OPINION_RE.match(tail)
    if opinion is not None:
        return not _data_provider_tail_has_own_event(
            tail[opinion.end() :],
            name=name,
            allow_operating_forecast=opinion.group(1) in {"预计", "预测"},
        )
    if left.endswith(_DATA_PROVIDER_SOURCE_LEFT_CONTEXT):
        if not tail or tail.startswith(("，", ",")):
            return True
        return bool(_DATA_PROVIDER_SOURCE_OBJECT_RE.match(tail))
    return False


def _is_broker_research_attribution(
    compact_text: str,
    *,
    name: str,
    start: int,
    end: int,
) -> bool:
    """Identify a broker/research byline rather than an investee mention.

    A headline such as ``中信证券：看好电子板块`` names the research provider,
    not a stock affected by the event.  The same listed company remains a real
    entity in ``中信证券：2025年净利润增长`` or ``中信证券发布半年报``.
    """
    # Long exchange-review and sponsor documents often put ``核查意见`` or
    # ``保荐书`` hundreds of characters after the broker name.  Keep the scan
    # within the same sentence, but do not truncate it to a short headline
    # prefix or those professional-role markers become invisible.
    tail = compact_text[end : end + 512]
    left = compact_text[max(0, start - 24) : start]
    broker_identity = (
        name.endswith("证券")
        or name in _BROKER_SECURITY_NAMES
        or tail.startswith("证券")
    )
    if not broker_identity:
        return False

    # The security master may store ``中国银河`` while the byline spells out
    # ``中国银河证券``. Strip institutional qualifiers and an adjacent code
    # before classifying the following language. Ticker ownership is checked
    # separately by the code-level attribution guard.
    code_qualifier = _BROKER_CODE_QUALIFIER_RE.match(tail)
    qualified_tail = tail[code_qualifier.end() :] if code_qualifier else tail
    for qualifier in (
        "证券股份有限公司",
        "证券有限公司",
        "股份有限公司",
        "有限公司",
        "证券",
        "研究所",
        "研究院",
    ):
        if qualified_tail.startswith(qualifier):
            qualified_tail = qualified_tail[len(qualifier) :]
            break
    qualified_clause = re.split(r"[。；;！!？?\n]", qualified_tail, maxsplit=1)[0]

    if left.endswith(_BROKER_ATTRIBUTION_LEFT_CONTEXT):
        return True

    if qualified_clause.startswith(_RESEARCH_ATTRIBUTION_OPENERS):
        return True

    # An outlook describes the broker's market opinion, not its own business.
    # Anchor to the name-adjacent clause so a later outlook does not suppress
    # a genuine earnings/buyback announcement or a separate company mention.
    if re.match(
        r"^(?:在)?展望后市(?:时)?(?:认为|表示|指出|称|[：:，,])",
        qualified_clause,
    ):
        return True

    if any(
        marker in qualified_clause
        for marker in _BROKER_PROFESSIONAL_ROLE_MARKERS
    ):
        return True

    if any(
        marker in qualified_clause
        for marker in _BROKER_RESEARCH_CONTENT_MARKERS
    ):
        return True

    # ``券商名 + 人名/分析师职务：观点`` is an analyst attribution, not an
    # event about the listed broker.  Explicit corporate subjects/actions are
    # checked first so real earnings, buybacks and shareholder events survive.
    if _broker_tail_has_own_subject(qualified_clause):
        return False
    if _broker_tail_is_person_byline(qualified_clause):
        return True
    if re.match(r"^[^：:]{0,24}(?:策略|观点|展望|复盘)[：:]", qualified_clause):
        return True

    if qualified_clause.startswith(("：", ":")):
        after_colon = qualified_clause[1:]
        # A broker-name byline is research attribution by default.  Restore a
        # direct company mention only when the post-colon grammar starts with
        # an explicit corporate subject/action, not merely because words such
        # as “拟” or “净利润” occur later in an industry forecast.
        return not _broker_tail_has_own_subject(after_colon)

    if re.match(r"^(?:首席[^：:]{0,12}|观点)[：:]", qualified_clause):
        return True

    return False


def _ambiguous_name_has_entity_context(
    compact_text: str,
    *,
    name: str,
    start: int,
    end: int,
    allow_qualified_ticker: bool = False,
) -> bool:
    left = compact_text[max(0, start - 8) : start]
    right = compact_text[end : end + 24]
    if left.endswith(_AMBIGUOUS_ENTITY_LEFT_CONTEXT):
        return True
    # Parenthesized six-digit codes are an explicit security-style spelling.
    # The database layer still validates the code against the same master row.
    if re.match(r"^[（(][0-9]{6}[）)]", right):
        return True
    if allow_qualified_ticker and re.match(
        r"^[（(](?:sh|sz|bj)?[0-9]{6}(?:\.(?:sh|sz|bj))?[）)]", right,
    ):
        return True
    if left.endswith(_AMBIGUOUS_COMMON_LEFT_CONTEXT):
        return False
    semantic_right = right.lstrip("：:，,；;。.!！？?【[（(")
    if name in _INDUSTRY_NOUN_SECURITY_NAMES:
        separated = semantic_right != right
        if separated and semantic_right.startswith(
            ("公司",) + _INDUSTRY_NOUN_ENTITY_RIGHT_CONTEXT
        ):
            return True
        return semantic_right.startswith(_INDUSTRY_NOUN_ENTITY_RIGHT_CONTEXT)
    if semantic_right.startswith(_AMBIGUOUS_ENTITY_RIGHT_CONTEXT):
        return True
    return False


def _security_name_organization_end(
    compact_text: str, name: str, start: int, end: int,
) -> int:
    """End of an explicit organization covering this exact issuer-name span."""
    suffix = _ADJACENT_ORGANIZATION_SUFFIX_RE.match(compact_text, end)
    if suffix:
        return suffix.end()
    society = _CHEMICAL_SOCIETY_NAME_RE.match(compact_text, start) if name == "中国化学" else None
    return society.end() if society else 0


def _security_name_has_prefix_overlap(
    compact_text: str, name: str, start: int, end: int,
) -> bool:
    left_context = _PREFIX_OVERLAP_SECURITY_LEFT_CONTEXT.get(name, ())
    right_context = _PREFIX_OVERLAP_SECURITY_RIGHT_CONTEXT.get(name, ())
    return bool(
        (left_context and compact_text[:start].endswith(left_context))
        or (right_context and compact_text[end:].startswith(right_context))
        or _security_name_organization_end(compact_text, name, start, end)
    )


def is_direct_security_mention(
    text: str, security_name: str, *, allow_qualified_ticker: bool = False,
) -> bool:
    """Return whether a name occurrence denotes the listed-company entity.

    This is intentionally stricter than substring membership.  Chinese stock
    short names can also be ordinary nouns, while broker names often appear as
    research attribution.  Exact ticker matching has a separate attribution
    check before the database layer treats it as stronger evidence.
    Qualified ticker syntax is only enabled by master-validated callers.
    """
    compact_text = re.sub(r"\s+", "", str(text or "")).casefold()
    normalized_name = re.sub(r"\s+", "", str(security_name or "")).casefold()
    if not compact_text or not normalized_name:
        return False
    for occurrence in re.finditer(re.escape(normalized_name), compact_text):
        start, end = occurrence.span()
        if _security_name_has_prefix_overlap(compact_text, normalized_name, start, end):
            continue
        if (
            normalized_name in _LEXICALLY_AMBIGUOUS_SECURITY_NAMES
            and not _ambiguous_name_has_entity_context(
                compact_text,
                name=normalized_name,
                start=start,
                end=end,
                allow_qualified_ticker=allow_qualified_ticker,
            )
        ):
            continue
        if _is_broker_research_attribution(
            compact_text,
            name=normalized_name,
            start=start,
            end=end,
        ):
            continue
        if _is_data_provider_attribution(
            compact_text,
            name=normalized_name,
            start=start,
            end=end,
        ):
            continue
        return True
    return False


def _ticker_qualifies_organization(compact: str, span: tuple) -> bool:
    """An organization's attached number/market label is not an issuer ID."""
    # Inspect the organization beside this code, not the candidate issuer's
    # name: a code belonging to a different master row cannot rescue it either.
    for pattern in (_ADJACENT_ORGANIZATION_SUFFIX_RE, _CHEMICAL_SOCIETY_NAME_RE):
        for organization in pattern.finditer(compact, 0, span[0]):
            if _ORGANIZATION_CODE_PREFIX_RE.fullmatch(compact[organization.end():span[0]]):
                return True
    return False


def _ticker_span_has_identity_anchor(
    compact: str, span: tuple, name: str, exchange: str = ""
) -> bool:
    """Require issuer name, a security code label, or this row's market marker.

    Parentheses and generic product/tracking code labels are not identity.
    """
    if _ticker_qualifies_organization(compact, span):
        return False
    left_start = max(0, span[0] - 16)
    left = compact[left_start:span[0]]
    right = compact[span[1]:span[1] + 8]
    name_start = compact.rfind(name, left_start, span[0]) if name else -1
    # Validate the nearest occurrence, not another real issuer elsewhere in
    # the title rescuing a country/product phrase next to this number.
    if name_start >= 0 and (
        not _security_name_has_prefix_overlap(compact, name, name_start, name_start + len(name))
        and (name not in _LEXICALLY_AMBIGUOUS_SECURITY_NAMES
             or is_direct_security_mention(compact, name))
    ):
        return True
    if re.search(r"(?:证券|股票|a股)代码[：:=]?$", left):
        return True
    markets = {
        "SSE": ("sh", "sse", "沪"),
        "SZSE": ("sz", "szse", "深"),
        "BSE": ("bj", "bse", "京"),
    }.get(str(exchange or ""), ())
    if not markets:
        return False
    alt = "|".join(markets)
    if re.search(rf"(?:{alt})[.:：]?$", left):
        return True
    return bool(re.match(rf"^[.:：]?(?:{alt})", right))


def _amount_like_code_spans(compact: str, code_spans: set, name: str) -> set:
    """Drop six-digit spans that are amounts or counts, not labelled tickers."""
    amount_spans = {
        span for span in code_spans
        if re.match(
            r"^(?:[.][0-9]+)?(?:万|亿)?(?:元|美元|欧元|日元|英镑|港元|港币|人民币|名|人|户|个|家|次|吨|股|位|例)",
            compact[span[1]:],
        )
    }
    labelled = set()
    for span in amount_spans:
        left = compact[max(0, span[0] - 16):span[0]]
        if name and left.endswith(name):
            labelled.add(span)
        elif re.search(r"(?:证券|股票|a股)?代码[：:=]?$", left):
            labelled.add(span)
        elif re.search(r"(?:sh|sz|bj|sse|szse|bse)[.:：]?$", left):
            labelled.add(span)
    return amount_spans - labelled


def is_direct_security_ticker_mention(
    text: str,
    security_name: str,
    ticker: str,
    *,
    exchange: str,
) -> bool:
    """Keep exact codes except occurrences qualifying the same broker's byline.

    Do not delete codes globally: a later issuer event may repeat the same
    code. Other issuers' codes and non-broker provider semantics are unchanged.
    """
    compact = re.sub(r"\s+", "", str(text or "")).casefold()
    name = re.sub(r"\s+", "", str(security_name or "")).casefold()
    code = str(ticker or "").strip()
    if not name or not re.fullmatch(r"[0-9]{6}", code):
        return False
    code_spans = {
        match.span()
        for match in re.finditer(rf"(?<!\d){re.escape(code)}(?!\d)", compact)
    }
    if not code_spans:
        return False
    code_spans -= _amount_like_code_spans(compact, code_spans, name)
    code_spans = {
        span for span in code_spans
        if _ticker_span_has_identity_anchor(compact, span, name, exchange)
    }
    if not code_spans:
        return False
    expected_market = {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}.get(exchange)
    for occurrence in re.finditer(re.escape(name), compact):
        qualifier = _BROKER_CODE_QUALIFIER_RE.match(compact, occurrence.end())
        if qualifier is None or qualifier.group("ticker") != code:
            continue
        if any(
            marker and marker != expected_market
            for marker in (qualifier.group("prefix"), qualifier.group("suffix"))
        ):
            continue
        if _is_broker_research_attribution(
            compact, name=name, start=occurrence.start(), end=occurrence.end()
        ):
            code_spans.discard(qualifier.span("ticker"))
    return bool(code_spans)


_A_SHARE_MARKET_ANCHORS = (
    "A股收盘", "沪指", "深成指", "创业板指", "科创50", "北证50",
    "沪深京", "三大指数", "全市场", "股指期货",
)
_A_SHARE_EXPLICIT_ANCHORS = (
    "A股", "沪指", "上证", "深成指", "深证", "创业板", "科创50", "北证50",
    "沪深", "中证", "沪深京",
)
_A_SHARE_STRESS_TAGS = (
    "极端行情", "大盘极端行情", "市场极端下行", "大盘急跌", "市场急跌",
)
_A_SHARE_DOWNSIDE_MARKERS = (
    "跌", "下跌", "跌超", "跌逾", "收跌", "下挫", "下行", "飘绿", "走弱", "跳水", "普跌", "跌停",
)
_A_SHARE_RECOVERY_PHRASES = (
    "止跌回升", "止跌企稳", "跌幅收窄", "收复失地", "由跌转涨", "翻红", "集体收涨", "收涨",
    "低开高走", "不跌反涨",
)
_CONSUMPTION_DEMAND_MARKERS = (
    "扩内需", "扩大消费", "内需潜力", "促消费", "提振消费", "消费规划",
    "消费“十五五”", "消费十五五", "以旧换新",
)
_NATIONAL_POLICY_ANCHORS = (
    "国务院", "国常会", "部委", "国家级", "中央", "政治局", "发改委",
    "商务部", "财政部", "工信部", "政策", "规划", "方案", "意见", "措施", "十五五",
)
_RENEWABLE_TECH_ANCHORS = (
    "风电", "风机", "海上风电", "海风", "光伏", "组件", "逆变器",
    "新能源装机", "清洁能源", "核电", "核能", "生物质能", "储能",
)
_RENEWABLE_SIGNAL_MARKERS = (
    "装机", "项目", "投资", "规划", "报告", "招标", "订单", "开工",
    "并网", "发电量", "产能", "增长", "增加", "超越", "突破", "扩张",
    "预亏", "亏损", "减产", "停产", "停建", "下滑",
)
_NEGATIVE_RENEWABLE_MARKERS = (
    "预亏", "亏损", "减产", "停产", "停建", "下滑", "下降", "削减", "取消",
)
_POSITIVE_RENEWABLE_MARKERS = (
    "增长", "增加", "超越", "突破", "扩张", "加快", "提速", "开工", "并网", "中标",
)

# Only publisher/source fields may drive a deterministic Briefing mapping.
# ``logic`` and ``summary`` are generated by an earlier model stage and must
# never be relabelled as local evidence after a grounding fallback.
_TRUSTED_ARTICLE_FIELDS = (
    "title", "raw_title", "source_keys", "link", "feed_url", "time",
    "created_at", "time_ts",
)

_FOREIGN_POLICY_ENTITIES = (
    "美联储", "日本央行", "日银", "欧洲央行", "欧央行", "英国央行",
    "英格兰银行", "加拿大央行", "澳洲联储", "澳大利亚央行",
    "新西兰联储", "韩国央行", "印度央行", "俄罗斯央行", "巴西央行",
    "瑞士央行", "瑞典央行", "挪威央行", "土耳其央行",
    "美国财政部", "日本财务省", "英国财政部", "加拿大财政部",
    "欧盟委员会", "美国国会预算办公室", "鲍威尔", "FOMC",
    "联邦公开市场委员会", "美国非农", "非农就业", "美国就业",
    "美国通胀", "美国CPI", "美国PCE", "美国核心PCE", "美国失业率",
)
_DOMESTIC_POLICY_ENTITIES = (
    "中国央行", "中国人民银行", "人民银行", "国务院", "国常会", "政治局",
    "中国财政部", "我国财政", "发改委", "国家发展改革委", "证监会",
    "金融监管总局", "国家金融监督管理总局", "外汇管理局", "中央汇金",
    "A股", "人民币",
)
_FOREIGN_POLICY_CONTEXT_RE = re.compile(
    r"(?:美国|日本|英国|加拿大|澳大利亚|新西兰|韩国|印度|俄罗斯|巴西|"
    r"瑞士|瑞典|挪威|土耳其|欧盟|欧元区).{0,8}"
    r"(?:央行|财政|货币政策|利率决议|加息|降息|预算|赤字|国债发行)"
)
_DOMESTIC_POLICY_CONTEXT_RE = re.compile(
    r"(?:中国|我国|国内|中央).{0,8}"
    r"(?:央行|财政|货币政策|利率|降准|降息|预算|稳增长)"
)

_MARKET_REFERENCE_PATTERNS: tuple[tuple[str, str, str, str], ...] = (
    ("上证50", "SSE", "000016", "上证50"),
    ("沪深300", "SSE", "000300", "沪深300"),
    ("中证500", "SSE", "000905", "中证500"),
    ("科创50", "SSE", "000688", "科创50"),
    ("创业板指", "SZSE", "399006", "创业板指"),
)


def trusted_article_view(article: dict) -> dict:
    """Copy the bounded source-owned fields accepted by local Briefing rules."""
    if not isinstance(article, dict):
        return {}
    trusted = {
        key: article.get(key)
        for key in _TRUSTED_ARTICLE_FIELDS
        if key in article
    }
    # ``news_cache.title`` is a presentation field and may contain the model's
    # score/tag/logic wrapper.  Once the publisher-owned ``raw_title`` exists,
    # it is the only title text allowed to drive a trusted asset mapping.
    raw_title = str(article.get("raw_title") or "").strip()
    if raw_title:
        trusted["title"] = raw_title
        trusted["raw_title"] = raw_title
    return trusted


def _source_owned_text(value: dict | str) -> str:
    if isinstance(value, dict):
        return " ".join(
            str(value.get(key) or "") for key in ("title", "raw_title")
        ).replace(" ", "")
    return str(value or "").replace(" ", "")


def classify_policy_region(value: dict | str) -> str:
    """Classify explicit macro-policy entities before any theme keyword match.

    The result is one of ``domestic``, ``foreign``, ``mixed`` or ``unknown``.
    Generated reasoning is deliberately excluded for mapping and filter use.
    """
    text = _source_owned_text(value)
    if not text:
        return "unknown"
    folded_text = text.casefold()
    foreign = any(
        entity.casefold() in folded_text for entity in _FOREIGN_POLICY_ENTITIES
    ) or bool(
        _FOREIGN_POLICY_CONTEXT_RE.search(text)
    )
    domestic = any(
        entity.casefold() in folded_text for entity in _DOMESTIC_POLICY_ENTITIES
    ) or bool(
        _DOMESTIC_POLICY_CONTEXT_RE.search(text)
    )
    # Unqualified institution names in a Chinese domestic feed retain their
    # conventional domestic meaning, but never override an explicit country.
    if not foreign and any(entity in text for entity in ("央行", "财政部")):
        domestic = True
    if foreign and domestic:
        return "mixed"
    if foreign:
        return "foreign"
    if domestic:
        return "domestic"
    return "unknown"


def _article_text(value: dict | str) -> tuple[str, str]:
    if isinstance(value, dict):
        text = " ".join(
            str(value.get(key) or "")
            for key in ("title", "raw_title", "tag", "logic", "summary")
        )
        return text.replace(" ", ""), str(value.get("tag") or "")
    return str(value or "").replace(" ", ""), ""


def is_a_share_market_stress(value: dict | str) -> bool:
    """Identify an actual broad A-share sell-off, not a sector decline or rebound."""
    text, tag = _article_text(value)
    stress_tag = _contains_any(tag, _A_SHARE_STRESS_TAGS)
    if not _contains_any(text, _A_SHARE_EXPLICIT_ANCHORS):
        return False
    # Mask phrases whose terminal state is recovery. This prevents the bare
    # character “跌” in “止跌回升” from turning a positive close into a sell-off.
    downside_text = text
    downside_text = re.sub(
        r"(?:未出现|并未|并非|没有|不再|未)(?:明显)?(?:普跌|下跌|走弱|跳水|跌)",
        "平稳",
        downside_text,
    )
    for phrase in _A_SHARE_RECOVERY_PHRASES:
        downside_text = downside_text.replace(phrase, "回升")
    # A closing reversal overrides an earlier intraday low unless a later
    # explicit closing decline is present.
    positive_reversal = re.search(
        r"(?:但|后|随后|尾盘|收盘).{0,16}(?:回升|翻红|收涨|由跌转涨|低开高走)",
        text,
    )
    if positive_reversal and not re.search(r"收跌", text[positive_reversal.end():]):
        return False
    has_downside = _contains_any(downside_text, _A_SHARE_DOWNSIDE_MARKERS)
    if not has_downside:
        return False
    strong_market_downside = _contains_any(
        downside_text,
        (
            "全市场下跌", "全市场普跌", "三大指数集体下挫", "三大指数集体收跌",
            "三大指数下跌", "个股普跌", "沪深京普跌", "千股跌停",
        ),
    )
    if strong_market_downside:
        return True
    # Match within one clause so a falling chip sector cannot be attached to
    # indices that closed higher in the preceding clause.
    clauses = re.split(r"[，,。；;！!？?：:\n]", downside_text)
    downside_clauses = []
    for clause in clauses:
        if not _contains_any(clause, _A_SHARE_EXPLICIT_ANCHORS):
            continue
        if _contains_any(clause, _A_SHARE_DOWNSIDE_MARKERS):
            downside_clauses.append(clause)
    if not downside_clauses:
        return False
    if stress_tag:
        return True
    severe_words = (
        "大跌", "暴跌", "重挫", "集体下挫", "集体收跌", "同步下跌", "普跌", "跌停",
    )
    for clause in downside_clauses:
        if _contains_any(clause, severe_words):
            return True
        percentages = re.findall(r"(?:跌|下跌|收跌)(?:超|逾)?(\d+(?:\.\d+)?)%", clause)
        if any(float(value) >= 1.0 for value in percentages):
            return True
    return False


def is_consumption_policy(value: dict | str) -> bool:
    """Require both a consumption-demand signal and a public-policy anchor."""
    text, _tag = _article_text(value)
    return (
        classify_policy_region(value) not in {"foreign", "mixed"}
        and _contains_any(text, _CONSUMPTION_DEMAND_MARKERS)
        and _contains_any(text, _NATIONAL_POLICY_ANCHORS)
    )


def is_renewable_energy_development(value: dict | str) -> bool:
    """Require a clean-energy technology and a concrete operating signal."""
    text, _tag = _article_text(value)
    return (
        _contains_any(text, _RENEWABLE_TECH_ANCHORS)
        and _contains_any(text, _RENEWABLE_SIGNAL_MARKERS)
    )


def extract_market_references(value: dict | str) -> list[dict[str, str]]:
    """Return explicit index references without inventing a particular ETF."""
    text, _tag = _article_text(value)
    references: list[dict[str, str]] = []
    for token, exchange, ticker, name in _MARKET_REFERENCE_PATTERNS:
        if token not in text:
            continue
        references.append(
            {
                "name": name,
                "exchange": exchange,
                "ticker": ticker,
                "asset_type": "index",
                "reference_type": "explicit_mention",
            }
        )
    if "ETF" in text.upper():
        references.append(
            {
                "name": "ETF（原文未指定基金代码）",
                "exchange": "",
                "ticker": "",
                "asset_type": "fund_category",
                "reference_type": "product_class",
            }
        )
    return references


def _rule_allowed_for_region(rule: AssetRule, region: str) -> bool:
    return not (
        region in {"foreign", "mixed"}
        and rule.theme in {"国内宽信用", "扩内需政策"}
    )


def match_asset_rule(text: str) -> AssetRule | None:
    text = str(text or "")
    region = classify_policy_region(text)
    if _contains_any(text, ("央行购金", "央行增持黄金", "增持黄金储备")):
        return next(rule for rule in ASSET_RULES if rule.theme == "贵金属")
    priority_themes = ("A股极端行情", "扩内需政策", "新能源建设", "资源安全")
    for theme in priority_themes:
        rule = next((candidate for candidate in ASSET_RULES if candidate.theme == theme), None)
        if not rule or not _rule_allowed_for_region(rule, region):
            continue
        if theme == "A股极端行情":
            if is_a_share_market_stress(text):
                return rule
            continue
        if theme == "扩内需政策":
            if is_consumption_policy(text):
                return rule
            continue
        if theme == "新能源建设":
            if is_renewable_energy_development(text):
                return rule
            continue
        if _contains_any(text, rule.keywords):
            return rule
    for rule in ASSET_RULES:
        if rule.theme in priority_themes:
            continue
        if not _rule_allowed_for_region(rule, region):
            continue
        if _contains_any(text, rule.keywords) or (
            rule.theme == "美元流动性" and _has_usd_fx_context(text)
        ):
            return rule
    return None


def map_article_to_assets(article: dict, *, trusted_only: bool = False) -> dict:
    source_article = trusted_article_view(article) if trusted_only else article
    tag_text = str(source_article.get("tag") or "").replace(" ", "")
    primary_fields = (
        ("title", "raw_title")
        if trusted_only
        else ("title", "raw_title", "tag", "logic")
    )
    mapping_fields = primary_fields if trusted_only else (*primary_fields, "summary")
    primary_text = " ".join(
        str(source_article.get(key) or "") for key in primary_fields
    )
    full_text = " ".join(
        str(source_article.get(key) or "") for key in mapping_fields
    )
    market_references = extract_market_references(source_article)
    region = classify_policy_region(source_article)
    # Structural signals must win over a broad model tag.  For example an A 股
    # sell-off often mentions CPO/芯片 among laggards, but it is still a market
    # risk event rather than an AI industry signal.
    rule = None
    mapping_confidence = 0.0
    mapping_relevance: float | None = None
    if is_a_share_market_stress(source_article):
        rule = next(candidate for candidate in ASSET_RULES if candidate.theme == "A股极端行情")
        mapping_confidence, mapping_relevance = 0.95, 9.5
    elif is_consumption_policy(source_article):
        rule = next(candidate for candidate in ASSET_RULES if candidate.theme == "扩内需政策")
        mapping_confidence, mapping_relevance = 0.95, 9.5
    elif is_renewable_energy_development(source_article):
        rule = next(candidate for candidate in ASSET_RULES if candidate.theme == "新能源建设")
        mapping_confidence, mapping_relevance = 0.92, 9.2
    elif _contains_any(full_text, ("央行购金", "央行增持黄金", "增持黄金储备")):
        rule = next(candidate for candidate in ASSET_RULES if candidate.theme == "贵金属")
        mapping_confidence, mapping_relevance = 0.9, 9.0
    else:
        rule = match_asset_rule(primary_text)
        if rule:
            mapping_confidence, mapping_relevance = 0.8, 8.0
    if rule and rule.theme not in {
        "A股极端行情", "扩内需政策", "新能源建设", "资源安全"
    }:
        rule = None
        mapping_confidence, mapping_relevance = 0.0, None
    if tag_text and not rule:
        for candidate in ASSET_RULES:
            if not _rule_allowed_for_region(candidate, region):
                continue
            theme_text = candidate.theme.replace(" ", "")
            # A specific theme label may be extended (e.g. “AI算力链”), but a
            # generic tag such as “流动性” must never match “美元流动性”.
            if theme_text == tag_text or theme_text in tag_text:
                rule = candidate
                if theme_text == tag_text:
                    mapping_confidence, mapping_relevance = 0.9, 9.0
                else:
                    mapping_confidence, mapping_relevance = 0.85, 8.5
                break
    if not rule:
        rule = match_asset_rule(primary_text)
        if rule:
            mapping_confidence, mapping_relevance = 0.8, 8.0
    if not rule:
        rule = match_asset_rule(full_text)
        if rule:
            mapping_confidence, mapping_relevance = 0.65, 6.0
    if not rule:
        tag = str(source_article.get("tag") or "主题").strip() or "主题"
        if region == "foreign":
            tag = "海外宏观政策"
        elif region == "mixed":
            tag = "海外宏观与市场联动"
        return {
            "mapped_theme": tag,
            "asset_classes": [] if trusted_only else ["A股风险偏好"],
            "sectors": [] if trusted_only else [f"{tag}相关板块"],
            "stocks": [],
            # An unmatched label is still useful for grouping, but it is not
            # evidence that the article predicts the broad A-share index.
            # Keep the asset result unavailable and let the backtest expose
            # its separately labelled benchmark series instead.
            "backtest_symbol": None,
            "mapping_confidence": 0.0,
            "mapping_relevance": None,
            "security_candidates": [],
            "market_references": market_references,
            "watch": (
                "观察成交放大和价格趋势是否延续"
                if trusted_only
                else "观察是否出现多源确认、成交放大和价格趋势延续"
            ),
            "score_reason": "高分情报进入同一主题，可能代表资金关注度提升",
        }
    direction = str(source_article.get("direction") or "中性").strip() or "中性"
    if rule.theme == "新能源建设":
        if _contains_any(full_text, _NEGATIVE_RENEWABLE_MARKERS):
            direction = "看空"
        elif _contains_any(full_text, _POSITIVE_RENEWABLE_MARKERS):
            direction = "看多"
    exposures = {
        exposure.name: exposure for exposure in rule.security_exposures
    }
    security_candidates = []
    for stock_name in rule.stocks:
        name = str(stock_name or "").strip()
        if (
            not name
            or name.endswith("ETF")
            or name.startswith("以当日")
        ):
            continue
        exposure = exposures.get(name)
        if exposure and exposure.keywords and not _contains_any(
            full_text, exposure.keywords
        ):
            continue
        directly_named = is_direct_security_mention(" ".join(str(source_article.get(key) or "") for key in ("title", "raw_title")), name)
        relation_type = (
            "direct" if directly_named
            else exposure.relation_type if exposure
            else "concept"
        )
        relevance_weight = exposure.relevance_weight if exposure else 1.0
        confidence = (
            max(0.9, mapping_confidence)
            if directly_named
            else min(0.88, mapping_confidence * 0.86 * relevance_weight)
        )
        relevance = (
            10.0
            if directly_named
            else min(
                9.2,
                float(mapping_relevance or 0.0) * relevance_weight,
            )
        )
        security_candidates.append(
            {
                "name": name,
                "relation_type": relation_type,
                "direction": direction,
                # A deterministic theme rule does not have a calibrated price
                # target.  Keep the signed gap unknown instead of converting
                # importance or direction into a fabricated percentage.
                "expected_gap": None,
                "confidence": round(confidence, 4),
                "relevance": round(relevance, 4),
                "rationale": (
                    f"原文直接点名{name}；事件命中{rule.theme}。"
                    if directly_named
                    else (
                        f"{exposure.rationale} 原文命中{rule.theme}，需结合股票级证据复核。"
                        if exposure
                        else f"{name}属于{rule.theme}规则股票池；需结合原文证据复核传导。"
                    )
                ),
            }
        )
    return {
        "mapped_theme": rule.theme,
        "asset_classes": list(rule.asset_classes),
        "sectors": list(rule.sectors),
        "stocks": list(rule.stocks),
        "backtest_symbol": rule.backtest_symbol,
        # These are deterministic mapping-quality dimensions derived from the
        # matched rule/provenance.  They are deliberately independent of the
        # article scorer's threshold-distance ``score_confidence``.
        "mapping_confidence": mapping_confidence,
        "mapping_relevance": mapping_relevance,
        "security_candidates": security_candidates,
        "market_references": market_references,
        "watch": rule.watch,
        "score_reason": rule.rationale,
    }


def format_mapping(mapping: dict) -> dict[str, object]:
    return {
        "assets": "、".join(mapping.get("asset_classes") or []),
        "sectors": "、".join(mapping.get("sectors") or []),
        "stocks": "、".join(mapping.get("stocks") or []),
        "backtest_symbol": str(mapping.get("backtest_symbol") or ""),
        "mapping_confidence": float(mapping.get("mapping_confidence") or 0.0),
        "mapping_relevance": mapping.get("mapping_relevance"),
        "watch": str(mapping.get("watch") or ""),
        "score_reason": str(mapping.get("score_reason") or ""),
        "mapped_theme": str(mapping.get("mapped_theme") or ""),
    }
