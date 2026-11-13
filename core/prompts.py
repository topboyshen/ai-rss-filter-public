import logging
import re
from typing import Any, Dict, Tuple
from scoring_provenance import INSUFFICIENT_INPUT_PROMPT

logger = logging.getLogger(__name__)

RESEARCH_READING_RUBRIC = f"""
你是财经研究阅读助手，不是交易员。分数表示研究阅读优先级，不是买入强度、涨跌预测或可交易性。
只根据标题和摘要已写明的内容判断，不补外部事实。不要因为文章没有直接可交易催化，就把有具体事实的条目压到 4 分或固定中性。

【四道闸门】5 分及以上必须同时满足：
1. 点名主体：公司/行业/机构/国家/政策/资产可识别。
2. 新增量：有新的数字、事件、政策、交易、治理变化、数据或分析，不是复述旧闻。
3. 研究关联：能说明对业务、行业、宏观、政策或市场结构的意义，不只是价格动了。
4. 有据可查：事实来自标题/摘要；不把推测写成已发生；未点名不写成受益。
1-3 任一不满足，最高 4 分。第 4 条因摘要截断无法确认时，5-7 分可以给，但 logic 必须写“待核查”。

【分数】
9-10：重大、高置信的新事实，影响面广；首发/主要来源或重大更新。
7-8：重要且具体的新事实，研究价值明确。
5-6：具体但增量的更新，值得跟踪。
3-4：在范围内但低价值：常规、无新增、重复、单一公司无研究角度。
0-2：范围外或无实质：只有指数/ETF 点位、私生活、广告、主体不可识别。

【范围】
核心：AI、科技、半导体、算力、软件硬件、数据中心、云；公司基本面、治理、并购、订单、产能、资本开支；科技政策、监管、供应链。
邻近：宏观、地缘能源、医药、市场结构，必须有具体主体+机制/影响。
排除：只有指数/ETF 点位、常规终值、访问行程、私生活、公关、无新增重复、主体不可识别。

【重复事件】
按文章相对事件的增量评分：首发/主要来源 8-10；后续新细节/分析 5-7；纯转载/复述 3-4。不要因为同一事件重要就给所有转载高分。

【输入不足】
{INSUFFICIENT_INPUT_PROMPT}
4 分只表示“在范围内但低价值”，不表示“不确定”。
"""

_SOURCE_STYLES = {
    "default": "",
    "industry": "来源可能是多行业频道；按新闻本身识别主题，不把非科技新闻强行归入 AI、芯片或半导体。",
    "macro": "来源可能是宏观/流动性频道；注意数据口径、同比/环比、预期差；不因海外或非央行内容一律降级。",
    "tech": "来源可能是科技频道；注意技术/产品/供应链细节；公司治理、政策、地缘有具体事实时照常评分。",
    "reddit": "来源是 Reddit 讨论/观点；须标明是观点，不作为已核事实或独立确认；观点不等于低质量。",
}

BUILTIN_PROMPTS = {
    key: RESEARCH_READING_RUBRIC + (("\n\n" + style) if style else "")
    for key, style in _SOURCE_STYLES.items()
}


def get_prompt(key, cfg):
    return (cfg.get("prompts") or {}).get(key) or BUILTIN_PROMPTS.get(
        key, BUILTIN_PROMPTS["default"]
    )


def _prompt_name(key: Any, cfg: Dict[str, Any]) -> str:
    """Name of the body ``get_prompt(key, cfg)`` really returns.

    A configured or built-in name is kept.  Anything else (missing, blank,
    unknown) is served the built-in default text, so its name is ``default``.
    """
    if isinstance(key, str) and (
        (cfg.get("prompts") or {}).get(key) or key in BUILTIN_PROMPTS
    ):
        return key
    return "default"


def _matched_prompt_key(url: str, cfg: Dict[str, Any]) -> str:
    """First ``prompt_matchers`` key matching ``url``; ``default`` when none."""
    url_lower = url.lower()
    if "_compiled_prompt_matchers" not in cfg:
        out = []
        for matcher in cfg.get("prompt_matchers", []):
            pat = matcher.get("pattern", "")
            key = matcher.get("key", "default")
            try:
                out.append((re.compile(pat, re.IGNORECASE), key))
            except re.error as e:
                logger.warning("prompt_matchers 无效 pattern %r: %s", pat, e)
        cfg["_compiled_prompt_matchers"] = out
    for pattern_re, key in cfg["_compiled_prompt_matchers"]:
        if pattern_re.search(url_lower):
            return key
    return "default"


def get_prompt_for_url(url: str, cfg: Dict[str, Any]) -> str:
    """按 URL 选 prompt 模板；在 cfg 上懒编译并缓存 _compiled_prompt_matchers。"""
    return get_prompt(_matched_prompt_key(url, cfg), cfg)


def resolve_prompt(feed_url: str, cfg: Dict[str, Any]) -> Tuple[str, str]:
    """The one scoring-prompt rule: return ``(name, body)`` actually used.

    Collection, webhook scoring, the cold ``/filter`` paths and the persisted
    ``prompt_key`` all call this, so the stored key is always the name of the
    body handed to the model.

    * A feed present in the configuration uses its own ``prompt``.  A missing,
      blank or unknown ``prompt`` is served the built-in ``default`` text and
      is stored as ``default`` (never as the unknown name, never via URL
      matchers).
    * A feed absent from the configuration is chosen by ``prompt_matchers``
      (``default`` when none match); an unknown matcher key is stored as
      ``default`` for the same reason.

    This file is part of the strategy fingerprint, so a release that changes
    the selected text is a new scoring contract: links already cached under the
    old contract are scored again when they reappear.  Post-release model call
    volume and rescoring cost have not been measured.
    """
    from db.database import get_feed_config

    feed_cfg = get_feed_config(cfg, feed_url) if feed_url else {}
    if feed_cfg:
        name = _prompt_name(feed_cfg.get("prompt"), cfg)
    else:
        name = _prompt_name(_matched_prompt_key(str(feed_url or ""), cfg), cfg)
    return name, get_prompt(name, cfg)


def prompt_name_for_body(body: str, feed_url: str, cfg: Dict[str, Any]) -> str:
    """Name of the prompt whose text is ``body``; ``custom`` if none matches.

    The persisted ``prompt_key`` must name the text sent to the model, so a
    caller that supplies text other than what ``resolve_prompt`` returns for
    the feed is recorded by the configured or built-in prompt it equals, or
    as ``custom`` -- never as the feed's configured name.
    """
    name, resolved = resolve_prompt(feed_url, cfg)
    if body == resolved:
        return name
    named = {**BUILTIN_PROMPTS, **{k: v for k, v in (cfg.get("prompts") or {}).items() if v}}
    for candidate, text in named.items():
        if body == text:
            return candidate
    return "custom"
