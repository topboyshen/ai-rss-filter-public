"""Feed configuration and strategy-filter helpers split out of db.database.

Pure functions of the loaded config: no database connection, no module state.
``db.database`` re-exports every name, so ``from db.database import ...`` and
``database.<name>`` (including monkeypatching through the facade for callers
outside this module) keep working unchanged.
"""

from typing import Any, Dict, List, Optional

def _strategy_where_clause(cfg: Optional[Dict], params: List[Any]) -> str:
    """按评分策略过滤缓存行。

    完整版本形如 manual:fingerprint。指纹随配置微调会变，但 manual 段不变时
    仍应展示历史高分（与 Briefing 跨策略读取一致）；仅 manual 段变化时才隔离。
    """
    strategy_version = str((cfg or {}).get("scoring_strategy_version") or "")
    if not strategy_version:
        return ""
    if ":" in strategy_version:
        manual_prefix = strategy_version.split(":", 1)[0] + ":"
        params.append(f"{manual_prefix}%")
        return " AND strategy_version LIKE ?"
    params.append(strategy_version)
    return " AND strategy_version = ?"


def _exact_strategy_where_clause(cfg: Optional[Dict], params: List[Any]) -> str:
    """Isolate inference inputs by the complete effective strategy identity.

    Read models intentionally retain manual-prefix compatibility so dashboards
    can show history across static configuration fingerprints.  Similarity
    candidates are model inputs, not a read model: reusing a title scored under
    another few-shot/profile suffix would silently import the previous scoring
    contract into the current run.
    """
    strategy_version = str((cfg or {}).get("scoring_strategy_version") or "")
    if not strategy_version:
        return ""
    params.append(strategy_version)
    return " AND strategy_version = ?"


def _feed_config_for_url(cfg: Optional[Dict], feed_url: str) -> Dict[str, Any]:
    if not cfg or not feed_url:
        return {}
    for feed in cfg.get("feeds", []) or []:
        if feed.get("url") == feed_url:
            return feed
    aliases = cfg.get("_runtime_feed_url_aliases", {}) or {}
    if isinstance(aliases, dict):
        for canonical, originals in aliases.items():
            family = [str(canonical or "")]
            if isinstance(originals, (list, tuple)):
                family.extend(str(item or "") for item in originals)
            if feed_url not in family:
                continue
            for feed in cfg.get("feeds", []) or []:
                if feed.get("url") == canonical:
                    return feed
    return {}


def get_feed_config(cfg: Optional[Dict], feed_url: str) -> Dict[str, Any]:
    """按精确 URL 或配置加载时记录的受控别名获取 feed 配置。"""
    return _feed_config_for_url(cfg, feed_url)


def canonical_feed_url(cfg: Optional[Dict], feed_url: str = "") -> str:
    """Normalize only exact, configuration-generated runtime URL aliases."""
    value = str(feed_url or "")
    aliases = (cfg or {}).get("_runtime_feed_url_aliases", {}) or {}
    if not isinstance(aliases, dict):
        return value
    for canonical, originals in aliases.items():
        canonical = str(canonical or "")
        if not canonical:
            continue
        family = [canonical]
        if isinstance(originals, (list, tuple)):
            family.extend(str(item or "") for item in originals)
        if value in family:
            return canonical
    return value


def equivalent_feed_urls(cfg: Optional[Dict], feed_url: str = "") -> List[str]:
    """Return the exact URL family recorded during a controlled host rewrite."""
    urls: List[str] = []
    if feed_url:
        urls.append(feed_url)
    aliases = (cfg or {}).get("_runtime_feed_url_aliases", {}) or {}
    if isinstance(aliases, dict):
        for canonical, originals in aliases.items():
            family = [str(canonical or "")]
            if isinstance(originals, (list, tuple)):
                family.extend(str(item or "") for item in originals)
            family = [item for item in family if item]
            if any(item in urls for item in family):
                urls.extend(item for item in family if item not in urls)
    return urls
