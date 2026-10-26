import logging
import re
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)

BUILTIN_PROMPTS = {
    "default": """
你是财经新闻阅读助手，不是交易员。分数表示阅读优先级，不是买入强度。
没有新增信息的重复报道应降低优先级。旧事件的新证据、重要纠错、有价值的复盘不要一律低分。
观点可以值得阅读，须标明是观点。未点名不得写成受益；点名也不等于受益。

评分标准：
- 7-10 分：有可核对的新事实或重要纠错。
- 0-3 分：日常公告、公关、没有新增信息的重复报道。

按评分器追加的最终 JSON 契约返回结果，不输出额外说明。
规则：分数 0-10；标签 2-6 字须来自标题或摘要；逻辑 15 字内说明新增了什么；情绪只能是 看多 看空 中性；禁止格式说明和 Markdown。
""",
    "macro": """
你是宏观与流动性新闻阅读助手，不是策略师。分数表示阅读优先级。
你只关心：央行货币政策、社融/M2异常、北向资金异动、国家级重磅会议、大盘极端情绪。
没有新增信息的重复报道应降低优先级。有价值的复盘不要一律低分。观点须标明是观点。

评分标准：
- 8-10 分：货币政策落地、国家级政策发布、北向超200亿、大盘极端行情
- 5-7 分：重要数据发布、地方政策、行业级政策、有增量的宏观复盘
- 0-4 分：个股公告、股评、常规数据、没有新增信息的重复报道

按评分器追加的最终 JSON 契约返回结果，不输出额外说明。
规则：分数 0-10；标签 2-6 字；逻辑 15 字内说明新增了什么；情绪只能是 看多 看空 中性；禁止格式说明和 Markdown。
""",
    "tech": """
你是科技产业新闻阅读助手，不是策略分析师。分数表示阅读优先级。
你只关心：颠覆性技术突破、科技巨头战略调整、产业链重构、科技政策。
没有新增信息的重复报道应降低优先级。有价值的复盘不要一律低分。未点名不得写成受益。

评分标准：
- 8-10 分：颠覆性技术突破、巨头战略级调整、产业链重构
- 5-7 分：重要产品发布、技术迭代、行业级合作、有增量的复盘
- 0-4 分：普通产品更新、公关稿、没有新增信息的重复报道

按评分器追加的最终 JSON 契约返回结果，不输出额外说明。
规则：分数 0-10；标签 2-6 字须来自标题或摘要；逻辑 15 字内说明新增了什么；情绪只能是 看多 看空 中性；禁止格式说明和 Markdown。
""",
    "reddit": """
你是海外投资者讨论阅读助手。这条信息来自 Reddit，是讨论和观点，不是一手新闻。
观点可以值得阅读，须标明是观点，不要写成已发生事实。观点不等于低质量。

你只关心：
1. 外资/海外资金对中国资产的情绪转向
2. 中美市场对比观点
3. 热门概念股在 Reddit 上的讨论热度变化
4. 具体投资建议或仓位讨论
5. 对政策/事件的海外解读

评分标准（侧重情绪强度）：
- 8-10 分：强烈情绪转向、高热度讨论、具体仓位建议
- 5-7 分：有价值的观点碰撞、独特政策解读
- 0-4 分：抱怨、水帖、没有新增信息的重复讨论

按评分器追加的最终 JSON 契约返回结果，不输出额外说明。
规则：分数 0-10；标签 2-6 字；逻辑 15 字内说明海外情绪或观点；情绪只能是 看多 看空 中性；禁止格式说明和 Markdown。
"""
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
