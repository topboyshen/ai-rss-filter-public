"""The read-surface score-path registry must stay the single source of truth."""

import pytest

from scoring_provenance import (
    DEFAULT_FEED_SCORE_COUNTS,
    FEED_SCORE_CATEGORY_KEYS,
    FEED_SCORE_CATEGORY_LABELS,
    RECOGNIZED_SCORE_PATHS,
    SCORE_PATH_SPECS,
    score_path_reading_label,
)

DIRECT_CATEGORIES = {
    "similarity",
    "safe_contract",
    "safe_grounding",
    "insufficient_input",
    "market_flash",
}


def test_registry_covers_every_recognized_path_exactly_once():
    assert set(SCORE_PATH_SPECS) == set(RECOGNIZED_SCORE_PATHS)
    assert len(SCORE_PATH_SPECS) == len(RECOGNIZED_SCORE_PATHS)


def test_every_spec_category_has_a_declared_count():
    categories = {
        spec.category for spec in SCORE_PATH_SPECS.values() if spec.category
    }
    assert categories <= set(DEFAULT_FEED_SCORE_COUNTS)


def test_default_counts_follow_the_label_order():
    assert tuple(DEFAULT_FEED_SCORE_COUNTS) == FEED_SCORE_CATEGORY_KEYS
    assert tuple(FEED_SCORE_CATEGORY_KEYS) == tuple(
        key for key, _label in FEED_SCORE_CATEGORY_LABELS
    )
    assert all(value == 0 for value in DEFAULT_FEED_SCORE_COUNTS.values())


@pytest.mark.parametrize("path", sorted(RECOGNIZED_SCORE_PATHS))
def test_every_path_has_a_reading_label(path):
    label = score_path_reading_label(path)
    assert label
    assert label != "未知路径"


def test_unknown_reading_path_never_guesses():
    assert score_path_reading_label("something_new") == "未知路径"
    assert score_path_reading_label(None) == "未知路径"


def test_dashboard_category_comes_from_the_registry():
    import routes.dashboard_routes as dashboard

    for path, spec in SCORE_PATH_SPECS.items():
        if spec.category in DIRECT_CATEGORIES:
            assert dashboard._feed_score_category(path, 3.0, 6) == spec.category
