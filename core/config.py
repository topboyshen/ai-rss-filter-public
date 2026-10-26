import hashlib
import json
import os
import re
from copy import deepcopy

import yaml

# 支持本地开发、Docker bind-mount 或显式环境变量覆盖
_DEFAULT_CONFIG_CANDIDATES = (
    "/app/config.yaml",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml"),
    "/root/ai-rss-filter/config.yaml",
)
CONFIG_PATH = os.environ.get("AI_FILTER_CONFIG_PATH") or next(
    (path for path in _DEFAULT_CONFIG_CANDIDATES if os.path.exists(path)),
    _DEFAULT_CONFIG_CANDIDATES[-1],
)

DEFAULTS = {
    "threshold": {"pass": 7, "notify": 9, "similarity": 0.42},
    "max_entries": 15,
    "max_workers": 5,
    "cache_ttl": 600,
    # 0 表示关闭；Dashboard 页面自动重载间隔（秒）
    "dashboard_auto_refresh_seconds": 0,
    "similarity_window_hours": 72,
    "rss_timeout": 10,
    # 单次源 HTTP 拉取最大尝试次数（含首次），连接抖动时有效；>=2
    "rss_max_fetch_attempts": 3,
    # RSS/Atom 可被正常解析但连续无可用条目时，标记数据质量退化；不影响容器健康。
    "feed_health": {"empty_degraded_after": 3},
    # 0=自动：约 2*cache_ttl；>0 为「距上次全量拉源超此秒数则标 stale」
    "staleness_warn_seconds": 0,
    "blacklist": [
        "新股申购", "中签结果", "分红派息", "股权登记",
        "限售解禁", "股东减持", "回购进度", "业绩快报",
        "业绩预告", "中报预告", "年报预告", "季报预告",
        "股票期权", "股票激励", "增发上市", "IPO",
        "转债申购", "可转债", "配股配息", "除权除息",
        "停牌复牌", "风险提示", "交易异动"
    ],
    "prompts": {},
    "prompt_matchers": [],
    "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
    "security": {
        "api_key": "",
        "allow_query_api_key": False,
        # SSRF: production accepts only exact configured feed endpoints.
        "allow_unconfigured_feed_urls": False,
        "cors_allowed_origins": "*",
    },
    "highlight_keywords": [],
    "output": {"max_items": 10},
    "local_scoring": {"enabled": True, "rules": [], "default_score": 5, "default_tag": "未知"},
    "multi_max_workers": 3,
    # 未过线条目是否写入 news_cache，避免同一条反复调用 LLM（仅按 link 去重）
    "cache_rejected_entries": True,
    # 仅当 local_scoring 有规则命中且该规则分数 <= 此值时跳过 LLM（0=关闭快筛）
    "local_fast_reject_max_rule_score": 0,
    # HTTP 成功但严格内容校验失败时，默认不追加模型重试；生产配置可显式开启。
    "llm_semantic_repair": {"enabled": False},
    # 低于此分数不生成 AI 摘要（少一次 API）；0=与关闭摘要相同效果由 generate_summary 控制
    "summary_min_score": 7.0,
    "backtest": {"signal_retention_days": 400},
    # One shared default for the dashboard, public card and /briefing route.
    # Explicit ?hours= remains supported for historical/extended views.
    "briefing": {"default_hours": 24},
    # Briefing assembly is a read-side presentation policy.  It deliberately
    # stays outside the scoring strategy fingerprint so changing quotas never
    # creates a new score cohort.
    "briefing_assembly": {
        "enabled": False,
        "candidate_multiplier": 4,
        "max_themes": 4,
        "max_articles_per_theme": 3,
        "max_share_ratio": 0.4,
        "reserved_non_head_slots": 1,
        "grounding": {
            "enabled": False,
            "min_distinct_hits": 2,
            "require_strong_hit": True,
            "validate_direction": True,
            "on_fail": "deterministic_fallback",
        },
    },
    "scoring_strategy_version": "",
    "feeds": [],
}


def _merge(base, override):
    for k, v in override.items():
        if isinstance(v, dict) and k in base and isinstance(base[k], dict):
            _merge(base[k], v)
        else:
            base[k] = v


def _strategy_code_fingerprint() -> str:
    """把核心评分代码纳入指纹，防止代码改了但配置没变时复用旧分数。"""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rel_paths = [
        "core/processor.py",
        "services/natural_scoring.py",
        "services/natural_score_continuations.py",
        "services/publisher_feeds.py",
        "core/scorer.py",
        "core/score_evidence.py",
        "scoring_provenance.py",
        "core/prompts.py",
        "core/user_feedback.py",
    ]
    h = hashlib.sha256()
    for rel_path in rel_paths:
        path = os.path.join(base_dir, rel_path)
        try:
            with open(path, "rb") as f:
                h.update(rel_path.encode("utf-8"))
                h.update(f.read())
        except OSError:
            continue
    return h.hexdigest()[:12]


def _strategy_fingerprint(cfg: dict) -> str:
    """根据影响评分/过滤输出的配置和代码自动生成短指纹。"""
    keys = [
        "threshold",
        "blacklist",
        "fast_pass",
        "two_stage_scoring",
        "local_reject_rules",
        "local_fast_reject_max_rule_score",
        "local_scoring",
        "similarity_window_hours",
        "similarity_rescore_keywords",
        "prompts",
        "prompt_matchers",
        "llm",
        "llm_routing",
        "llm_semantic_repair",
        "llm_score_cache",
        "few_shot_feedback",
        "quiet_mode",
        "time_weight",
        "summary_min_score",
        "generate_summary",
        "feeds",
    ]
    payload = {key: cfg.get(key) for key in keys if key in cfg}
    payload["strategy_code"] = _strategy_code_fingerprint()
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def load_config():
    # Every load starts from an independent tree. A shallow copy lets nested
    # YAML overrides mutate DEFAULTS and makes removed keys survive reloads.
    cfg = deepcopy(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f)
        if user_cfg is None:
            user_cfg = {}
        if not isinstance(user_cfg, dict):
            raise TypeError("配置文件顶层必须是 YAML mapping")
        _merge(cfg, user_cfg)
    _apply_secrets_from_env(cfg)
    manual_version = str(cfg.get("scoring_strategy_version") or "").strip()
    # Runtime-only host rewrites must not change the strategy identity. Compute
    # from the canonical YAML URLs first, then expose container-reachable URLs.
    auto_version = _strategy_fingerprint(cfg)
    cfg["scoring_strategy_fingerprint"] = auto_version
    cfg["scoring_strategy_version"] = f"{manual_version}:{auto_version}" if manual_version else auto_version
    _apply_rsshub_base_url_from_env(cfg)
    return cfg


def _apply_feed_base_url_from_env(
    cfg: dict,
    env_name: str,
    local_bases: tuple[str, ...],
) -> None:
    base_url = (os.environ.get(env_name) or "").strip().rstrip("/")
    if not base_url:
        return

    for feed in cfg.get("feeds") or []:
        url = str(feed.get("url") or "")
        for local_base in local_bases:
            if url != local_base and not url.startswith(local_base + "/"):
                continue
            rewritten = base_url + url[len(local_base):]
            if rewritten != url:
                aliases = cfg.setdefault("_runtime_feed_url_aliases", {})
                originals = aliases.setdefault(rewritten, [])
                if url not in originals:
                    originals.append(url)
            feed["url"] = rewritten
            break


def _apply_rsshub_base_url_from_env(cfg: dict) -> None:
    """Docker 部署时将本地服务地址改写为容器内服务名。"""
    # This map is generated from exact rewrites on every load.  Keeping it out
    # of the YAML and outside the strategy fingerprint lets database queries
    # find rows written before/after a container-host rewrite without guessing
    # that unrelated hosts with the same path are equivalent.
    cfg["_runtime_feed_url_aliases"] = {}
    _apply_feed_base_url_from_env(
        cfg,
        "AI_FILTER_RSSHUB_BASE_URL",
        ("http://127.0.0.1:1200", "http://localhost:1200"),
    )
    _apply_feed_base_url_from_env(
        cfg,
        "AI_FILTER_WEB_BASE_URL",
        ("http://127.0.0.1:5000", "http://localhost:5000"),
    )


def _apply_secrets_from_env(cfg: dict) -> None:
    """环境变量覆盖敏感项（不写入文件，优先于 config.yaml）。"""
    se = cfg.setdefault("security", {})
    for env_name in ("AI_FILTER_API_KEY", "API_KEY"):
        v = (os.environ.get(env_name) or "").strip()
        if v:
            se["api_key"] = v
            break
    v = (os.environ.get("AI_FILTER_ALLOW_QUERY_API_KEY") or "").strip().lower()
    if v:
        se["allow_query_api_key"] = v in ("1", "true", "yes", "on")
    v = (os.environ.get("AI_FILTER_ALLOW_UNCONFIGURED_FEED_URLS") or "").strip().lower()
    if v:
        se["allow_unconfigured_feed_urls"] = v in ("1", "true", "yes", "on")
    v = (os.environ.get("AI_FILTER_CORS_ALLOWED_ORIGINS") or "").strip()
    if v:
        se["cors_allowed_origins"] = "*" if v == "*" else [x.strip() for x in v.split(",") if x.strip()]
    tg = cfg.setdefault("telegram", {})
    if (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip():
        tg["bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if (os.environ.get("TELEGRAM_CHAT_ID") or "").strip():
        tg["chat_id"] = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


class ConfigHolder:
    """持有运行时配置，支持热重载"""
    def __init__(self):
        self._cfg = load_config()
        self._blacklist_re = re.compile("|".join(self._cfg["blacklist"])) if self._cfg["blacklist"] else re.compile("$^")

    @property
    def cfg(self):
        return self._cfg

    @property
    def blacklist_re(self):
        return self._blacklist_re

    def reload(self):
        # Publish the configuration and its derived regex as one validated pair.
        # An exception leaves the previous runtime state untouched.
        new_cfg = load_config()
        new_regex = re.compile("|".join(new_cfg["blacklist"])) if new_cfg["blacklist"] else re.compile("$^")
        self._cfg, self._blacklist_re = new_cfg, new_regex
        return new_cfg


# 全局单例
config = ConfigHolder()
