from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from services import backtest

DAY = 86400
WINDOW = 72 * 3600
NOW = int(datetime(2026, 9, 7, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())


def _run(monkeypatch, observations):
    rows = [
        {
            "link": f"signal-{index}",
            "score": 9.0,
            "created_at": timestamp,
            "available_at": timestamp,
            "direction": "中性",
            "mapped_theme": theme,
            "_signal_snapshot": True,
        }
        for index, (timestamp, theme) in enumerate(observations)
    ]
    before = deepcopy(rows)
    monkeypatch.setattr(backtest.time, "time", lambda: NOW)
    monkeypatch.setattr(backtest, "load_config", lambda: {})
    monkeypatch.setattr(backtest, "get_mapping_version", lambda: "test-mapping")
    monkeypatch.setattr(backtest, "load_bridge_preview", lambda *_: None)
    monkeypatch.setattr(backtest, "_same_day_snapshot", lambda *_: None)
    monkeypatch.setattr(backtest, "_future_snapshot", lambda *_: None)
    monkeypatch.setattr(
        backtest,
        "_load_backtest_samples",
        lambda *_: (rows, [], {"data_source": "signal_snapshots"}),
    )
    result = backtest.run_score_backtest(days=90)
    assert rows == before
    assert result["theme_observation_as_of"] == NOW
    return result, result["buckets"][0]


def test_pending_signals_do_not_enter_either_side_of_recurrence_rate(monkeypatch):
    result, bucket = _run(
        monkeypatch,
        [
            (NOW - 4 * DAY, "mature-hit"),
            (NOW - 5 * DAY, "mature-miss"),
            (NOW - 2 * DAY, "mature-hit"),
            (NOW - 3600, "pending-hit"),
            (NOW - 1800, "pending-hit"),
        ],
    )
    assert result["deduped_samples"] == bucket["samples"] == 5
    assert bucket["theme_follow_samples"] == 2
    assert bucket["theme_follow_pending_samples"] == 3
    assert bucket["theme_follow_rate"] == 50.0
    assert bucket["asset_t1_direction_samples"] == 0
    assert result["optimization"]["can_tune_thresholds"] is False


@pytest.mark.parametrize("age,mature", [(WINDOW - 1, 0), (WINDOW, 1), (WINDOW + 1, 1)])
def test_observation_maturity_boundary(monkeypatch, age, mature):
    _, bucket = _run(monkeypatch, [(NOW - age, "only")])
    assert bucket["theme_follow_samples"] == mature
    assert bucket["theme_follow_pending_samples"] == 1 - mature
    assert bucket["theme_follow_rate"] == (0.0 if mature else None)


@pytest.mark.parametrize(
    "gap,rate", [(0, 0.0), (1, 50.0), (WINDOW, 50.0), (WINDOW + 1, 0.0)]
)
def test_recurrence_interval_is_strictly_future_and_at_most_72_hours(
    monkeypatch, gap, rate
):
    later = NOW - WINDOW
    _, bucket = _run(monkeypatch, [(later - gap, "theme"), (later, "theme")])
    assert bucket["theme_follow_samples"] == 2
    assert bucket["theme_follow_pending_samples"] == 0
    assert bucket["theme_follow_rate"] == rate


def test_all_pending_is_unknown_even_if_a_theme_already_recurred(monkeypatch):
    _, bucket = _run(monkeypatch, [(NOW - DAY, "theme"), (NOW, "theme")])
    assert bucket["samples"] == 2
    assert bucket["theme_follow_samples"] == bucket["theme_follow_unique_days"] == 0
    assert bucket["theme_follow_pending_samples"] == 2
    assert bucket["theme_follow_rate"] is None
    assert bucket["theme_follow_sample_sufficient"] is False


def test_same_second_signals_keep_the_strictly_later_evidence(monkeypatch):
    _, bucket = _run(
        monkeypatch,
        [(NOW - 4 * DAY, "theme"), (NOW - 4 * DAY, "theme"), (NOW - 2 * DAY, "theme")],
    )
    assert bucket["theme_follow_samples"] == 2
    assert bucket["theme_follow_rate"] == 100.0


def test_future_dated_article_is_not_available_recurrence_evidence(monkeypatch):
    _, bucket = _run(monkeypatch, [(NOW - WINDOW, "theme"), (NOW + 1, "theme")])
    assert bucket["theme_follow_rate"] == 0.0
    assert bucket["theme_follow_samples"] == 1


@pytest.mark.parametrize("mature_days,sufficient", [(29, False), (30, True)])
def test_display_uses_mature_days_not_pending_signal_days(
    monkeypatch, mature_days, sufficient
):
    observations = [(NOW - (index + 4) * DAY, "theme") for index in range(mature_days)]
    if mature_days == 29:
        observations.append((observations[0][0] + 1, "theme"))
    observations.append((NOW - DAY, "theme"))
    result, bucket = _run(monkeypatch, observations)
    assert bucket["theme_follow_samples"] == 30
    assert bucket["theme_follow_pending_samples"] == 1
    assert bucket["theme_follow_unique_days"] == mature_days
    assert bucket["unique_signal_days"] == mature_days + 1
    assert bucket["theme_follow_sample_sufficient"] is sufficient
    html = backtest.render_backtest_html(result)
    assert "已观察满72小时 n=30" in html
    assert f"{mature_days}个成熟信号自然日" in html
    assert "待观察 1条（不计成功或失败）" in html
    assert ("72小时主题复现</span><b>100.0%" in html) is sufficient
    assert ("已达统计门槛" in html) is sufficient
    # A display-ready descriptive metric must never unlock research eligibility.
    assert "调参研究资格：调参样本待积累" in html
    assert result["optimization"]["can_tune_thresholds"] is False


def test_mature_signal_days_use_shanghai_midnight(monkeypatch):
    midnight = int(datetime(2026, 9, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
    _, bucket = _run(monkeypatch, [(midnight - 1, "a"), (midnight, "b")])
    assert bucket["theme_follow_unique_days"] == 2


def test_old_result_without_maturity_counts_cannot_publish_old_rate(monkeypatch):
    result, bucket = _run(
        monkeypatch, [(NOW - (index + 4) * DAY, "theme") for index in range(30)]
    )
    for key in tuple(bucket):
        if key.startswith("theme_follow_") and key != "theme_follow_rate":
            del bucket[key]
    html = backtest.render_backtest_html(result)
    assert "72小时主题复现</span><b>待积累" in html
    assert "待观察 未知条" in html
