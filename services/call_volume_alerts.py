"""Call-volume, token/cost and new-dead-letter alerts for Prometheus.

Read-only.  One bounded, indexed aggregate over ``llm_call_logs`` (the table
records one row per physical model request) feeds three signals:

* ``call_volume`` -- the last *complete* hour's physical calls exceed
  ``ratio`` x baseline (and an absolute floor, so a quiet baseline of 2 calls
  cannot page on 4).  The baseline is the median of the same hour-of-day over
  the trailing ``baseline_days``; when fewer than ``min_baseline_samples``
  such hours were observed it falls back to the median of the trailing
  ``fallback_hours`` complete hours.  Hours inside the observed span with no
  rows count as 0 calls; hours before the first observed row are unknown and
  never count, so a freshly started database does not invent a zero baseline.
* ``cost`` / ``tokens`` -- optional absolute ceilings for the last complete
  hour (disabled when unset).
* ``dlq_new`` -- dead-letter files that *arrived* within the window.  Unlike
  the total DLQ gauge this clears on its own, so stale, already triaged dead
  letters cannot keep the alarm permanently on.

Configuration (all optional) lives under ``call_volume_alerts`` in the config;
it is not part of the scoring strategy fingerprint.
"""

from __future__ import annotations

import re
import statistics
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

HOUR = 3600
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "ratio": 1.5,
    "min_calls": 20,
    "baseline_days": 7,
    "min_baseline_samples": 3,
    "fallback_hours": 24,
    "min_fallback_samples": 6,
    "max_cost_per_hour": None,
    "max_tokens_per_hour": None,
    "dlq_new_window_seconds": HOUR,
}


def settings(cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULTS)
    raw = (cfg or {}).get("call_volume_alerts") if isinstance(cfg, Mapping) else None
    if isinstance(raw, Mapping):
        merged.update({k: v for k, v in raw.items() if k in DEFAULTS})
    return merged


def _median(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def compute_baseline(
    hourly: Mapping[int, float],
    target_hour: int,
    first_hour: int | None,
    opts: Mapping[str, Any],
) -> tuple[float | None, str, int]:
    """Return (baseline, method, samples) for ``target_hour`` (an epoch hour index).

    ``hourly`` maps epoch-hour index -> count for hours that had rows.
    """
    if first_hour is None:
        return None, "insufficient", 0
    same_hour = [
        target_hour - 24 * day
        for day in range(1, int(opts["baseline_days"]) + 1)
        if target_hour - 24 * day >= first_hour
    ]
    if len(same_hour) >= int(opts["min_baseline_samples"]):
        values = [float(hourly.get(h, 0)) for h in same_hour]
        return _median(values), "same_hour_median", len(values)
    trailing = [
        target_hour - offset
        for offset in range(1, int(opts["fallback_hours"]) + 1)
        if target_hour - offset >= first_hour
    ]
    if len(trailing) >= int(opts["min_fallback_samples"]):
        values = [float(hourly.get(h, 0)) for h in trailing]
        return _median(values), "trailing_median", len(values)
    return None, "insufficient", max(len(same_hour), len(trailing))


def evaluate(
    rows: Iterable[tuple[int, int, int, float]],
    first_timestamp: int | None,
    now: float,
    opts: Mapping[str, Any],
    dlq_new: int = 0,
) -> dict[str, Any]:
    """Pure evaluation. ``rows`` = (epoch_hour, calls, tokens, cost)."""
    calls: dict[int, float] = {}
    tokens: dict[int, float] = {}
    cost: dict[int, float] = {}
    for hour, n, tok, usd in rows:
        calls[int(hour)] = float(n or 0)
        tokens[int(hour)] = float(tok or 0)
        cost[int(hour)] = float(usd or 0.0)
    target = int(now) // HOUR - 1  # last complete hour
    first_hour = None if first_timestamp is None else int(first_timestamp) // HOUR
    baseline, method, samples = compute_baseline(calls, target, first_hour, opts)
    observed = calls.get(target, 0.0)
    ratio = float(opts["ratio"])
    volume_alert = bool(
        opts.get("enabled", True)
        and baseline is not None
        and observed >= float(opts["min_calls"])
        and observed > ratio * baseline
    )
    max_cost, max_tokens = opts.get("max_cost_per_hour"), opts.get("max_tokens_per_hour")
    cost_alert = bool(
        opts.get("enabled", True) and max_cost is not None
        and cost.get(target, 0.0) > float(max_cost)
    )
    token_alert = bool(
        opts.get("enabled", True) and max_tokens is not None
        and tokens.get(target, 0.0) > float(max_tokens)
    )
    return {
        "hour_start": target * HOUR,
        "calls": observed,
        "tokens": tokens.get(target, 0.0),
        "cost": cost.get(target, 0.0),
        "baseline": baseline,
        "baseline_method": method,
        "baseline_samples": samples,
        "ratio": ratio,
        "volume_alert": volume_alert,
        "cost_alert": cost_alert,
        "token_alert": token_alert,
        "dlq_new": int(dlq_new),
        "dlq_new_alert": bool(opts.get("enabled", True) and dlq_new > 0),
    }


_DEAD_LETTER_NAME = re.compile(r"^[0-9a-f]{64}\.[0-9]+\.([0-9]+)\.json$")


def dead_letter_arrival_seconds(name: str) -> float | None:
    """Arrival time embedded by the writer (``<digest>.<attempts>.<time_ns>.json``).

    The name is immutable, so copying, restoring or touching an old dead letter
    cannot make it look new.  Names without the timestamp (legacy two-part names,
    notes, archives) are never treated as new.
    """
    match = _DEAD_LETTER_NAME.match(name)
    if match is None:
        return None
    return int(match.group(1)) / 1e9


def count_new_dead_letters(directory: Path, window_seconds: float, now: float) -> int:
    """Count dead letters that arrived within the window -- never the total."""
    try:
        names = [entry.name for entry in directory.iterdir()]
    except OSError:
        return 0
    cutoff = now - float(window_seconds)
    count = 0
    for name in names:
        arrived = dead_letter_arrival_seconds(name)
        if arrived is not None and cutoff <= arrived <= now + 300:
            count += 1
    return count


def load_hourly_rows(conn, since: int, until: int):
    """Bounded aggregate over the timestamp index; never a full-table scan."""
    rows = conn.execute(
        "SELECT timestamp / 3600 AS h, COUNT(*), COALESCE(SUM(total_tokens), 0), "
        "COALESCE(SUM(estimated_cost), 0) FROM llm_call_logs "
        "WHERE timestamp >= ? AND timestamp < ? GROUP BY h",
        (int(since), int(until)),
    ).fetchall()
    first = conn.execute(
        "SELECT MIN(timestamp) FROM llm_call_logs WHERE timestamp >= ?", (int(since),)
    ).fetchone()
    return rows, (first[0] if first else None)


def collect(cfg: Mapping[str, Any] | None, *, now: float | None = None,
            connect=None, dead_letter_dir: Path | None = None) -> dict[str, Any]:
    opts = settings(cfg)
    now = time.time() if now is None else now
    window_hours = max(int(opts["baseline_days"]) * 24, int(opts["fallback_hours"])) + 2
    since = (int(now) // HOUR - window_hours) * HOUR
    until = (int(now) // HOUR) * HOUR
    if connect is None:
        from db.database import get_connection as connect
    with connect() as conn:
        rows, first = load_hourly_rows(conn, since, until)
    if dead_letter_dir is None:
        dead_letter_dir = Path(__file__).resolve().parents[1] / "data" / "webhook_spool" / "dead-letter"
    dlq_new = count_new_dead_letters(dead_letter_dir, opts["dlq_new_window_seconds"], now)
    return evaluate(rows, first, now, opts, dlq_new)


def _gauge(name: str, help_text: str, value: float) -> list[str]:
    return [f"# HELP {name} {help_text}", f"# TYPE {name} gauge", f"{name} {value}"]


def prometheus_lines(cfg: Mapping[str, Any] | None, **kwargs) -> list[str]:
    try:
        result = collect(cfg, **kwargs)
        available = 1
    except Exception:  # metrics must never break the endpoint
        result, available = evaluate([], None, time.time(), settings(cfg)), 0
    baseline = result["baseline"]
    lines: list[str] = []
    lines += _gauge("ai_filter_llm_alerts_available", "Call-volume alert inputs readable", available)
    lines += _gauge("ai_filter_llm_calls_last_hour", "Physical model calls in the last complete hour", result["calls"])
    lines += _gauge("ai_filter_llm_calls_baseline", "Baseline calls/hour (-1 = insufficient history)",
                    -1 if baseline is None else baseline)
    lines += _gauge("ai_filter_llm_tokens_last_hour", "Tokens in the last complete hour", result["tokens"])
    lines += _gauge("ai_filter_llm_cost_last_hour", "Estimated cost in the last complete hour", result["cost"])
    lines += _gauge("ai_filter_llm_call_volume_alert", "Last-hour calls exceed ratio x baseline", int(result["volume_alert"]))
    lines += _gauge("ai_filter_llm_cost_alert", "Last-hour cost or tokens exceed the configured ceiling",
                    int(result["cost_alert"] or result["token_alert"]))
    lines += _gauge("ai_filter_dead_letter_new", "Dead letters that arrived within the window", result["dlq_new"])
    lines += _gauge("ai_filter_dead_letter_new_alert", "A new dead letter arrived within the window",
                    int(result["dlq_new_alert"]))
    return lines
