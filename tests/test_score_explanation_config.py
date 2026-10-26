"""Two-stage scoring settings are part of the cache/quality strategy identity."""

from copy import deepcopy
import importlib

import pytest

config = importlib.import_module("core.config")


@pytest.mark.parametrize(
    "changed",
    [
        {"enabled": False},
        {"stage2_min_score": 8},
        {"stage2_on_fast_pass_hint": False},
        {"stage2_max_score": 9},
        {"stage2_on_similar_higher_score": False},
        {"high_value_source_tiers": ["official"]},
        {"new_nested_option": {"enabled": True}},
    ],
)
def test_two_stage_settings_change_strategy_identity(monkeypatch, changed):
    monkeypatch.setattr(config, "_strategy_code_fingerprint", lambda: "fixed-code")
    original = {"threshold": 6, "two_stage_scoring": {"enabled": True}}
    updated = deepcopy(original)
    updated["two_stage_scoring"].update(changed)
    assert config._strategy_fingerprint(original) != config._strategy_fingerprint(
        updated
    )


def test_two_stage_mapping_order_does_not_change_identity(monkeypatch):
    monkeypatch.setattr(config, "_strategy_code_fingerprint", lambda: "fixed-code")
    first = {"two_stage_scoring": {"enabled": True, "review_band": [5, 8]}}
    second = {"two_stage_scoring": {"review_band": [5, 8], "enabled": True}}
    assert config._strategy_fingerprint(first) == config._strategy_fingerprint(second)
