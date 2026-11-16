"""db.database keeps re-exporting the feed-config helpers moved to db.feed_config."""

from db import database, feed_config

MOVED = (
    "_strategy_where_clause",
    "_exact_strategy_where_clause",
    "_feed_config_for_url",
    "get_feed_config",
    "canonical_feed_url",
    "equivalent_feed_urls",
)


def test_facade_reexports_the_same_objects():
    for name in MOVED:
        assert getattr(database, name) is getattr(feed_config, name), name


def test_thresholds_still_resolve_feed_config_through_the_facade(monkeypatch):
    cfg = {"threshold": {"pass": 7, "notify": 9}, "feeds": [{"url": "u", "pass_threshold": 5}]}
    assert database.get_feed_thresholds(cfg, "u") == {"pass": 5.0, "notify": 9.0, "aggregation": 5.0}
    monkeypatch.setattr(database, "get_feed_config", lambda *_a: {"pass_threshold": 3})
    assert database.pass_threshold_for_feed(cfg, "u") == 3.0
