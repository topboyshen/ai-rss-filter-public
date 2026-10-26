import math

from core.briefing_assembly import (
    assemble_briefing_candidates,
    briefing_policy_fingerprint,
    normalize_briefing_policy,
)


def _cfg(**overrides):
    policy = {
        "enabled": True,
        "max_themes": 4,
        "max_articles_per_theme": 3,
        "max_share_ratio": 0.4,
        "reserved_non_head_slots": 1,
    }
    policy.update(overrides)
    return {"briefing_assembly": policy}


def _article(name, theme, score, created_at, event_id=""):
    return {
        "title": name,
        "tag": theme,
        "score": score,
        "created_at": created_at,
        "event_id": event_id,
        "link": f"https://example.com/{name}",
    }


def test_policy_normalization_is_bounded_and_does_not_mutate_config():
    cfg = {
        "briefing_assembly": {
            "enabled": "yes",
            "max_themes": -9,
            "max_articles_per_theme": 9999,
            "max_share_ratio": math.inf,
            "reserved_non_head_slots": -2,
        }
    }
    original = {"briefing_assembly": dict(cfg["briefing_assembly"])}

    policy = normalize_briefing_policy(cfg)

    assert policy == {
        "enabled": True,
        "max_themes": 1,
        "max_articles_per_theme": 100,
        "max_share_ratio": 0.4,
        "reserved_non_head_slots": 0,
    }
    assert cfg == original
    assert normalize_briefing_policy(None)["enabled"] is False


def test_policy_fingerprint_is_independent_of_scoring_config_but_covers_theme_order():
    first = {
        **_cfg(),
        "threshold": {"pass": 7},
        "briefing_themes": [
            {"name": "AI 算力链", "keywords": ["AI", "算力"]},
            {"name": "芯片国产化", "keywords": ["芯片"]},
        ],
    }
    scoring_only_change = {**first, "threshold": {"pass": 9}}
    reordered = {
        **first,
        "briefing_themes": list(reversed(first["briefing_themes"])),
    }

    assert briefing_policy_fingerprint(first) == briefing_policy_fingerprint(scoring_only_change)
    assert briefing_policy_fingerprint(first) != briefing_policy_fingerprint(reordered)
    assert len(briefing_policy_fingerprint(first)) == 64


def test_disabled_policy_preserves_order_identity_and_skips_resolver():
    articles = [
        _article("low", "A", 1, 1),
        _article("high", "B", 10, 10),
        _article("middle", "C", 5, 5),
    ]

    def forbidden_resolver(_article):
        raise AssertionError("disabled assembly must not classify articles")

    selected, audit = assemble_briefing_candidates(
        articles,
        {"briefing_assembly": {"enabled": False}},
        limit=2,
        theme_resolver=forbidden_resolver,
    )

    assert selected == articles[:2]
    assert selected[0] is articles[0]
    assert selected[1] is articles[1]
    assert audit["selected_count"] == 2
    assert audit["share_cap"] is None


def test_enabled_policy_ranks_by_score_time_then_stable_event_identity():
    articles = [
        _article("later", "A", 8, 200, "z"),
        _article("event-z", "A", 9, 100, "z"),
        _article("event-a", "A", 9, 100, "a"),
        _article("lower", "A", 7, 999, "x"),
    ]
    selected, audit = assemble_briefing_candidates(
        articles,
        _cfg(max_themes=1, max_articles_per_theme=10, max_share_ratio=1),
        limit=10,
    )

    assert [article["title"] for article in selected] == [
        "event-a", "event-z", "later", "lower"
    ]
    assert audit["selected_primary_themes"] == ["A"] * 4


def test_first_configured_keyword_match_owns_one_primary_theme():
    cfg = {
        **_cfg(max_share_ratio=1),
        "briefing_themes": [
            {"name": "AI 存储", "keywords": ["HBM"]},
            {"name": "芯片国产化", "keywords": ["HBM", "芯片"]},
        ],
    }
    articles = [_article("HBM 芯片扩产", "AI算力", 9, 100)]

    _selected, audit = assemble_briefing_candidates(articles, cfg, limit=5)

    assert audit["selected_primary_themes"] == ["AI 存储"]
    assert audit["theme_counts"] == {"AI 存储": 1}


def test_theme_bucket_ignores_model_owned_logic_and_summary():
    cfg = {
        **_cfg(max_share_ratio=1),
        "briefing_themes": [
            {"name": "AI 存储", "keywords": ["HBM", "算力"]},
        ],
    }
    article = _article("普通公司公告", "FastPass", 9, 100)
    article.update({
        "raw_title": "普通公司公告",
        "logic": "HBM 算力重大利好",
        "summary": "模型称其属于 AI 存储",
    })

    _selected, audit = assemble_briefing_candidates([article], cfg, limit=1)

    assert audit["selected_primary_themes"] == ["其他高分情报"]


def test_single_theme_overflow_is_a_quota_exclusion_not_a_limit_exclusion():
    articles = [
        _article(f"A{i}", "A", 10 - i / 10, 100 - i)
        for i in range(10)
    ]

    selected, audit = assemble_briefing_candidates(
        articles,
        _cfg(max_themes=4, max_articles_per_theme=3, max_share_ratio=1),
        limit=12,
    )

    assert len(selected) == 3
    assert audit["excluded"] == {
        "target_limit": 0,
        "theme_limit": 0,
        "theme_cap": 7,
    }


def test_theme_count_per_theme_and_target_share_caps_are_all_enforced():
    articles = []
    for theme_index, theme in enumerate(("A", "B", "C", "D")):
        for item_index in range(5):
            articles.append(_article(
                f"{theme}{item_index}",
                theme,
                10 - theme_index - item_index / 10,
                100 - item_index,
            ))
    selected, audit = assemble_briefing_candidates(
        articles,
        _cfg(
            max_themes=3,
            max_articles_per_theme=5,
            max_share_ratio=0.4,
            reserved_non_head_slots=0,
        ),
        limit=10,
    )

    assert audit["eligible_themes"] == ["A", "B", "C"]
    assert audit["target_slots"] == 10
    assert audit["share_cap"] == 4
    assert audit["selected_count"] == 10
    assert max(audit["theme_counts"].values()) == 4
    assert not any(article["tag"] == "D" for article in selected)


def test_reserved_non_head_slots_bounds_the_head_theme_without_reordering_output():
    articles = [
        *[_article(f"A{i}", "A", 10 - i / 10, 100 - i) for i in range(6)],
        *[_article(f"B{i}", "B", 5 - i / 10, 50 - i) for i in range(3)],
    ]
    selected, audit = assemble_briefing_candidates(
        articles,
        _cfg(
            max_themes=2,
            max_articles_per_theme=10,
            max_share_ratio=1,
            reserved_non_head_slots=2,
        ),
        limit=5,
    )

    assert [article["tag"] for article in selected] == ["A", "A", "A", "B", "B"]
    assert audit["theme_counts"] == {"A": 3, "B": 2}
    assert audit["effective_reserved_non_head_slots"] == 2


def test_custom_resolver_accepts_existing_cfg_keyword_signature():
    articles = [
        _article("one", "ignored", 8, 10),
        _article("two", "ignored", 7, 9),
    ]
    seen_cfg = []

    def resolver(article, cfg=None):
        seen_cfg.append(cfg)
        return "even" if article["title"] == "two" else "odd"

    _selected, audit = assemble_briefing_candidates(
        articles,
        _cfg(max_share_ratio=1),
        limit=2,
        theme_resolver=resolver,
    )

    assert audit["selected_primary_themes"] == ["odd", "even"]
    assert seen_cfg and all(value is not None for value in seen_cfg)
