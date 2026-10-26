from copy import deepcopy

import yaml
from pathlib import Path

from core.prompts import get_prompt, get_prompt_for_url


def _cfg():
    return yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config.yaml").read_text(
            encoding="utf-8"
        )
    )


def _key_for(url: str, cfg: dict) -> str:
    selected = get_prompt_for_url(url, cfg)
    for key, text in cfg["prompts"].items():
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
    assert get_prompt(yicai["prompt"], cfg) == cfg["prompts"]["industry"]
    assert get_prompt_for_url(yicai["url"], deepcopy(cfg)) != cfg["prompts"]["industry"]
    assert get_prompt_for_url(yicai["url"], deepcopy(cfg)) == cfg["prompts"]["default"]
