from copy import deepcopy

import yaml
from pathlib import Path

from core.prompts import (
    BUILTIN_PROMPTS,
    get_prompt,
    get_prompt_for_url,
    resolve_prompt,
)


def _cfg():
    return yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config.yaml").read_text(
            encoding="utf-8"
        )
    )


def _key_for(url: str, cfg: dict) -> str:
    selected = get_prompt_for_url(url, cfg)
    named = {**BUILTIN_PROMPTS, **(cfg.get("prompts") or {})}
    for key, text in named.items():
        if selected == text:
            return key
    return "default"


def _feed(cfg: dict, name: str) -> dict:
    return next(item for item in cfg["feeds"] if item.get("name") == name)


def test_yicai_url_does_not_match_tech_via_embedded_ai():
    cfg = _cfg()
    yicai = _feed(cfg, "第一财经·最新")
    assert yicai["prompt"] == "industry"
    assert _key_for(yicai["url"], cfg) != "tech"
    assert _key_for("https://www.yicai.com/news/103368510.html", cfg) != "tech"
    assert _key_for("http://rsshub:1200/yicai/latest?limit=8", cfg) != "tech"
    # URL matcher is not the feed.prompt field; yicai falls through to default.
    assert _key_for(yicai["url"], cfg) == "default"


def test_gelonghui_and_wallstreetcn_url_routing_stay_distinct_from_feed_prompt():
    cfg = _cfg()
    gelonghui = _feed(cfg, "格隆汇")
    wallstreet = next(
        item
        for item in cfg["feeds"]
        if "wallstreetcn" in str(item.get("url") or "")
        and "a-stock" in str(item.get("url") or "")
    )
    assert gelonghui["prompt"] == "industry"
    assert wallstreet["prompt"] == "industry"
    assert _key_for(gelonghui["url"], cfg) == "default"
    assert _key_for("https://www.gelonghui.com/p/6684902", cfg) == "default"
    assert _key_for(wallstreet["url"], cfg) == "macro"
    assert _key_for("https://wallstreetcn.com/livenews/3166902", cfg) == "macro"


def test_explicit_tech_sources_still_select_tech_prompt():
    cfg = _cfg()
    assert _key_for("https://www.leiphone.com/category/ai", cfg) == "tech"
    assert _key_for("https://www.ithome.com/ai/", cfg) == "tech"
    assert _key_for("https://example.com/人工智能政策", cfg) == "tech"
    assert _key_for("https://example.com/半导体", cfg) == "tech"
    assert _key_for("https://example.com/topic/ai/news", cfg) == "tech"
    assert _key_for("https://ai.example.com/x", cfg) == "tech"


def test_feed_prompt_lookup_is_unchanged_and_not_inferred_from_yicai_url():
    cfg = _cfg()
    yicai = _feed(cfg, "第一财经·最新")
    assert get_prompt(yicai["prompt"], cfg) == BUILTIN_PROMPTS["industry"]
    assert get_prompt_for_url(yicai["url"], deepcopy(cfg)) != BUILTIN_PROMPTS["industry"]
    assert get_prompt_for_url(yicai["url"], deepcopy(cfg)) == BUILTIN_PROMPTS["default"]


def test_jin10_uses_its_feed_prompt_not_the_url_matcher():
    cfg = _cfg()
    name, body = resolve_prompt("http://ai-filter-web:5000/jin10/rss", cfg)
    assert name == "industry"
    assert body == BUILTIN_PROMPTS["industry"]
    assert get_prompt_for_url("http://ai-filter-web:5000/jin10/rss", cfg) == BUILTIN_PROMPTS["default"]


def test_every_configured_feed_prompt_name_resolves_to_a_body():
    cfg = _cfg()
    for feed in cfg["feeds"]:
        key = feed.get("prompt")
        if not key:
            continue
        name, body = resolve_prompt(feed["url"], cfg)
        assert name == key
        assert body == BUILTIN_PROMPTS[key]
