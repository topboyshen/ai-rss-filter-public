"""Call-volume / cost / new-dead-letter alerts: thresholds, baselines, wiring."""
import os
import time
from contextlib import contextmanager

import pytest

from services import call_volume_alerts as cva

H = 3600
NOW = 1_800_000_000 - (1_800_000_000 % H) + 600  # 10 minutes into an hour
TARGET = NOW // H - 1  # last complete hour


def opts(**kw):
    o = dict(cva.DEFAULTS)
    o.update(kw)
    return o


def rows_for(counts):
    return [(h, n, n * 100, n * 0.01) for h, n in counts.items()]


def week(baseline_value, target_value, days=7):
    counts = {TARGET - 24 * d: baseline_value for d in range(1, days + 1)}
    counts[TARGET] = target_value
    return counts


# --- baseline ---------------------------------------------------------------

def test_same_hour_median_over_trailing_week():
    counts = {TARGET - 24 * d: v for d, v in zip(range(1, 8), [10, 40, 20, 30, 1000, 20, 25])}
    first = (TARGET - 24 * 7) * H
    baseline, method, n = cva.compute_baseline(counts, TARGET, first // H, opts())
    assert (baseline, method, n) == (25.0, "same_hour_median", 7)  # median resists the 1000 spike


def test_hours_inside_span_without_rows_count_as_zero():
    counts = {TARGET - 24: 30, TARGET - 48: 30}  # 5 other days had no calls at that hour
    baseline, method, n = cva.compute_baseline(counts, TARGET, TARGET - 24 * 7, opts())
    assert (baseline, method, n) == (0.0, "same_hour_median", 7)


def test_sparse_history_falls_back_to_trailing_hours():
    first_hour = TARGET - 30  # only ~1 day of history: 1 same-hour sample
    counts = {TARGET - k: 10 for k in range(1, 25)}
    baseline, method, n = cva.compute_baseline(counts, TARGET, first_hour, opts())
    assert (baseline, method, n) == (10.0, "trailing_median", 24)


def test_too_little_history_gives_no_baseline_and_no_alert():
    first_hour = TARGET - 3
    counts = {TARGET - 1: 1, TARGET - 2: 1, TARGET: 500}
    baseline, method, _ = cva.compute_baseline(counts, TARGET, first_hour, opts())
    assert baseline is None and method == "insufficient"
    result = cva.evaluate(rows_for(counts), first_hour * H, NOW, opts())
    assert result["volume_alert"] is False


def test_empty_database_never_alerts():
    result = cva.evaluate([], None, NOW, opts())
    assert result["baseline"] is None and result["volume_alert"] is False


# --- thresholds -------------------------------------------------------------

@pytest.mark.parametrize(("observed", "fires"), [(150, False), (151, True), (100, False), (400, True)])
def test_volume_alert_fires_only_above_ratio_times_baseline(observed, fires):
    counts = week(100, observed)
    result = cva.evaluate(rows_for(counts), (TARGET - 24 * 7) * H, NOW, opts())
    assert result["baseline"] == 100.0
    assert result["volume_alert"] is fires


def test_absolute_floor_suppresses_tiny_baselines():
    counts = week(2, 10)  # 5x baseline, but only 10 calls
    first = (TARGET - 24 * 7) * H
    assert cva.evaluate(rows_for(counts), first, NOW, opts())["volume_alert"] is False
    assert cva.evaluate(rows_for(counts), first, NOW, opts(min_calls=5))["volume_alert"] is True


def test_current_partial_hour_is_ignored():
    counts = week(100, 100)
    counts[TARGET + 1] = 10_000  # in-progress hour
    result = cva.evaluate(rows_for(counts), (TARGET - 24 * 7) * H, NOW, opts())
    assert result["calls"] == 100 and result["volume_alert"] is False


def test_cost_and_token_ceilings_fire_above_not_below_and_default_off():
    counts = week(100, 100)  # target: tokens 10_000, cost 1.0
    first = (TARGET - 24 * 7) * H
    r = cva.evaluate(rows_for(counts), first, NOW, opts())
    assert r["cost_alert"] is False and r["token_alert"] is False  # disabled by default
    assert cva.evaluate(rows_for(counts), first, NOW, opts(max_cost_per_hour=0.99))["cost_alert"] is True
    assert cva.evaluate(rows_for(counts), first, NOW, opts(max_cost_per_hour=1.0))["cost_alert"] is False
    assert cva.evaluate(rows_for(counts), first, NOW, opts(max_tokens_per_hour=9_999))["token_alert"] is True
    assert cva.evaluate(rows_for(counts), first, NOW, opts(max_tokens_per_hour=10_000))["token_alert"] is False


def test_disabled_config_silences_everything():
    counts = week(100, 1000)
    r = cva.evaluate(rows_for(counts), (TARGET - 24 * 7) * H, NOW,
                     opts(enabled=False, max_cost_per_hour=0), dlq_new=3)
    assert not (r["volume_alert"] or r["cost_alert"] or r["dlq_new_alert"])


def test_settings_merge_ignores_unknown_keys():
    s = cva.settings({"call_volume_alerts": {"ratio": 2.0, "bogus": 1}})
    assert s["ratio"] == 2.0 and "bogus" not in s


# --- dead letters -----------------------------------------------------------

def _dead_letter_name(char: str, arrived: float) -> str:
    return f"{char * 64}.5.{int(arrived * 1e9)}.json"


def test_only_newly_arrived_dead_letters_alert(tmp_path):
    now = time.time()
    old = tmp_path / _dead_letter_name("a", now - 3 * 86400)  # stale, already triaged
    new = tmp_path / _dead_letter_name("b", now - 60)
    for p in (old, new, tmp_path / "note.txt", tmp_path / f"{'c' * 64}.5.json"):
        p.write_text("{}")
    assert cva.count_new_dead_letters(tmp_path, 3600, now) == 1
    assert cva.count_new_dead_letters(tmp_path, 3600, now + 7200) == 0  # window passed
    assert cva.count_new_dead_letters(tmp_path / "missing", 3600, now) == 0


# --- real SQLite + Prometheus wiring -----------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    from db import database

    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "cache.db"))
    database.init_db()
    return database


def _insert(database, counts):
    with database.get_connection() as conn:
        for hour, n in counts.items():
            conn.executemany(
                "INSERT INTO llm_call_logs (timestamp, purpose, total_tokens, estimated_cost) "
                "VALUES (?, 'score', 100, 0.01)",
                [(hour * H + 10 + i,) for i in range(n)],
            )
        conn.commit()


def test_collect_reads_llm_call_logs_through_the_timestamp_index(db, tmp_path):
    _insert(db, week(20, 61))
    result = cva.collect({}, now=NOW, connect=db.get_connection, dead_letter_dir=tmp_path / "dl")
    assert result["calls"] == 61 and result["baseline"] == 20.0
    assert result["tokens"] == 6100 and result["cost"] == pytest.approx(0.61)
    assert result["volume_alert"] is True
    with db.get_connection() as conn:
        plan = " ".join(r[3] for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT timestamp / 3600 AS h, COUNT(*) FROM llm_call_logs "
            "WHERE timestamp >= ? AND timestamp < ? GROUP BY h", (0, 1)))
    assert "USING INDEX" in plan or "USING COVERING INDEX" in plan


def test_prometheus_lines_export_alert_gauges(db, tmp_path):
    _insert(db, week(20, 20))
    dl = tmp_path / "dl"
    dl.mkdir()
    (dl / _dead_letter_name("e", time.time() - 30)).write_text("{}")
    lines = cva.prometheus_lines({}, now=time.time(), connect=db.get_connection, dead_letter_dir=dl)
    values = dict(line.split(" ", 1) for line in lines if not line.startswith("#"))
    assert values["ai_filter_llm_alerts_available"] == "1"
    assert values["ai_filter_dead_letter_new"] == "1"
    assert values["ai_filter_dead_letter_new_alert"] == "1"
    assert "ai_filter_llm_call_volume_alert" in values


def test_prometheus_lines_never_raise():
    @contextmanager
    def broken():
        raise RuntimeError("db down")
        yield

    lines = cva.prometheus_lines({}, connect=broken)
    values = dict(line.split(" ", 1) for line in lines if not line.startswith("#"))
    assert values["ai_filter_llm_alerts_available"] == "0"
    assert values["ai_filter_llm_call_volume_alert"] == "0"


def test_metrics_endpoint_includes_call_volume_gauges():
    import routes.admin_routes as admin

    src = open(admin.__file__, encoding="utf-8").read()
    assert "call_volume_prometheus(c)" in src


@pytest.mark.parametrize("gauge", [
    "ai_filter_llm_call_volume_alert", "ai_filter_llm_cost_alert", "ai_filter_dead_letter_new_alert",
])
def test_ops_check_fails_on_new_alert_gauges_and_tolerates_old_images(monkeypatch, tmp_path, gauge):
    from ops import ai_filter_ops_check as monitor
    from tests.test_ops_monitor import _healthy_metrics, _settings

    settings = _settings(tmp_path)
    monkeypatch.setenv("AI_FILTER_API_KEY", "k")
    monkeypatch.setattr(monitor, "_http_get", lambda *_a, **_k: (200, _healthy_metrics(**{gauge: 1})))
    result = monitor.check_metrics(settings, 1)
    assert result.ok is False and gauge.removeprefix("ai_filter_") in result.detail
    monkeypatch.setattr(monitor, "_http_get", lambda *_a, **_k: (200, _healthy_metrics(**{gauge: 0})))
    assert monitor.check_metrics(settings, 1).ok is True
    monkeypatch.setattr(monitor, "_http_get", lambda *_a, **_k: (200, _healthy_metrics()))
    assert monitor.check_metrics(settings, 1).ok is True  # absent on older images


def test_scoring_fingerprint_ignores_alert_config():
    from core import config as core_config

    base = {"threshold": {"pass": 6.0}, "feeds": []}
    assert core_config._strategy_fingerprint(base) == core_config._strategy_fingerprint(
        dict(base, call_volume_alerts={"ratio": 3})
    )


def test_stale_dlq_keeps_old_total_alert_off_while_new_item_alert_fires(tmp_path, monkeypatch, db):
    """Old behaviour: any DLQ file -> ai_filter_worker_queue_alert 1 forever."""
    import routes.admin_routes as admin
    from services import worker_health

    spool = tmp_path / "spool"
    dl = spool / "dead-letter"
    dl.mkdir(parents=True)
    stale = dl / f"{'a' * 64}.5.{int((time.time() - 6 * 86400) * 1e9)}.json"
    stale.write_text("{}")
    old = time.time() - 6 * 86400
    os.utime(stale, (old, old))
    monkeypatch.setattr(worker_health, "_spool_dir", lambda: spool)

    queue = admin._worker_queue_metrics()
    assert queue["dead_letter"] == 1 and queue["alert"] is False
    quiet = dict(line.split(" ", 1) for line in cva.prometheus_lines(
        {}, connect=db.get_connection, dead_letter_dir=dl) if not line.startswith("#"))
    assert quiet["ai_filter_dead_letter_new_alert"] == "0"

    (dl / f"{'b' * 64}.5.{time.time_ns()}.json").write_text("{}")  # a fresh failure
    assert admin._worker_queue_metrics()["alert"] is False
    loud = dict(line.split(" ", 1) for line in cva.prometheus_lines(
        {}, connect=db.get_connection, dead_letter_dir=dl) if not line.startswith("#"))
    assert loud["ai_filter_dead_letter_new_alert"] == "1"


def test_ops_notification_names_the_new_alert_reasons():
    from ops import ai_filter_ops_check as monitor

    text = monitor._notification_text("failure", [monitor.CheckResult(
        "metrics", False, "red gauges=llm_call_volume_alert,dead_letter_new_alert,llm_cost_alert")], 1)
    assert "metrics_reasons=llm_call_volume_alert,dead_letter_new_alert,llm_cost_alert" in text


@pytest.mark.parametrize("extra,expected", [
    # new image: queue alert already excludes DLQ, trust it
    ({"ai_filter_dead_letter_new_alert": 0, "ai_filter_worker_queue_alert": 1}, True),
    ({"ai_filter_dead_letter_new_alert": 0, "ai_filter_worker_queue_alert": 0}, False),
    # old image, stale DLQ only -> retired trigger, ignored
    ({"ai_filter_worker_queue_alert": 1, "ai_filter_worker_queue_dead_letter": 15}, False),
    # old image, real queue problems still alert
    ({"ai_filter_worker_queue_alert": 1, "ai_filter_worker_queue_dead_letter": 15,
      "ai_filter_worker_queue_stale": 1}, True),
    ({"ai_filter_worker_queue_alert": 1, "ai_filter_worker_queue_invalid_files": 2}, True),
    ({"ai_filter_worker_queue_alert": 1}, True),
])
def test_ops_check_worker_queue_alert_is_old_image_compatible(extra, expected):
    from ops import ai_filter_ops_check as monitor

    assert monitor._worker_queue_alert_active(extra) is expected


def _alert_gauge(db, dead_letter_dir):
    values = dict(line.split(" ", 1) for line in cva.prometheus_lines(
        {}, connect=db.get_connection, dead_letter_dir=dead_letter_dir) if not line.startswith("#"))
    return values["ai_filter_dead_letter_new_alert"]


def test_old_dead_letter_never_retriggers_only_new_one_alerts(tmp_path, db):
    """Total DLQ never alerts: old present + none new -> 0; one new -> 1."""
    dl = tmp_path / "dead-letter"
    dl.mkdir()
    now = time.time()
    old = dl / _dead_letter_name("a", now - 6 * 86400)
    old.write_text("{}")
    os.utime(old, (now, now))  # copied/restored/touched: mtime looks fresh
    (dl / f"{'d' * 64}.5.json").write_text("{}")  # legacy name without arrival time
    assert _alert_gauge(db, dl) == "0"
    assert _alert_gauge(db, dl) == "0"  # repeated scrapes stay quiet
    (dl / f"{'b' * 64}.5.{time.time_ns()}.json").write_text("{}")
    assert _alert_gauge(db, dl) == "1"

