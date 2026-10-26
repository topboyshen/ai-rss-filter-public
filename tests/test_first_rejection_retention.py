"""First-rejection retention: real process path, fake model, isolated SQLite.

The fake model sits behind the real Scorer, validator, processor and database;
nothing here calls a real model, the network or a production database.
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import services.first_rejection_retention as retention
import services.score_evidence_capture as legacy_capture
from core import config as core_config
from core.processor import process_entry
from core.scorer import LLMBackend, Scorer
from db import database
from db.first_rejection_evidence import (
    COLUMNS,
    TABLE,
    insert_evidence,
    purge_expired,
)

ROOT = Path(__file__).resolve().parents[1]
TITLE = "甲公司已建设工厂"
SUMMARY = "<p>当时实际来源摘要</p>"
NEUTRAL = json.dumps(
    {"score": 4, "tag": "其他", "logic": "来源未显示明确投资催化", "sentiment": "中性"},
    ensure_ascii=False,
)
# Grounded in neither title nor summary: the validator rejects it.
BAD = json.dumps(
    {"score": 8, "tag": "公司公告", "logic": "乙公司获得50亿元订单", "sentiment": "看多"},
    ensure_ascii=False,
)
LINK = "https://invalid.test/a?token=SECRET-LINK-TOKEN"
ON = {"enabled": True}


class FeedEntry(SimpleNamespace):
    def get(self, key, default=None):
        return getattr(self, key, default)


def base_cfg(retention_cfg=None, **extra):
    cfg = {
        "scoring_strategy_version": "retention-test-contract",
        "threshold": {"pass": 3, "similarity": 0.6, "notify": 99},
        "_blacklist_re": None,
        "cache_rejected_entries": True,
        "generate_summary": False,
        "similarity_window_hours": 72,
        "local_reject_rules": {"enabled": False},
        "fast_pass": {"enabled": False},
        "two_stage_scoring": {"enabled": False},
        "feeds": [],
    }
    if retention_cfg is not None:
        cfg["first_rejection_retention"] = retention_cfg
    cfg.update(extra)
    return cfg


@pytest.fixture
def model(monkeypatch):
    """Two fake backends: the first rejects, the second answers the fixed neutral."""
    database.init_db()
    scorer = Scorer()
    scorer.clear_runtime_cache()
    first = LLMBackend("first", "", "https://invalid.test", "first-model", priority=1)
    second = LLMBackend("second", "", "https://invalid.test", "second-model", priority=2)
    for backend in (first, second):
        monkeypatch.setattr(backend, "client", object())
    # Patch the class, never the (possibly shared) instance: monkeypatch cannot
    # undo an instance attribute that merely shadows a class method.
    monkeypatch.setattr(Scorer, "backends_for", lambda self, *a, **kw: [first, second])
    monkeypatch.setattr(Scorer, "summarize", lambda *a, **k: "")
    state = SimpleNamespace(
        scorer=scorer, calls=[], outputs={"first": BAD, "second": NEUTRAL}
    )

    def fake(self, backend, messages, *args, **kwargs):
        state.calls.append(backend.name)
        return state.outputs[backend.name]

    monkeypatch.setattr(Scorer, "_call_structured_llm", fake)
    return state


def run(model, cfg, link=LINK, title=TITLE, summary=SUMMARY):
    entry = FeedEntry(title=title, link=link, summary=summary)
    result = process_entry(
        entry, [], "SYSTEM-PROMPT-MARKER", cfg, scorer=model.scorer,
        feed_url="https://invalid.test/feed",
    )
    return entry, result


def rows(flush=True):
    if flush:
        assert retention.flush(10)
    with database.get_connection() as conn:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {TABLE} ORDER BY id")]


def stats():
    return dict(retention.STATS)


def delta(before):
    return {k: v - before[k] for k, v in retention.STATS.items() if v != before[k]}


def make_row(**over):
    row = {name: "x" for name in COLUMNS}
    row.update(
        schema_version=1, created_at=int(time.time()), stage="stage1",
        accepted_is_fixed_neutral=0, replayable=1, replay_limit="", row_bytes=100,
        dedupe_key=hashlib.sha256(os.urandom(8)).hexdigest(),
    )
    row.update(over)
    return row


# ── schema, triggers, expiry ─────────────────────────────────────────────


def test_schema_is_additive_idempotent_and_triggers_exist(model):
    database.init_db()
    database.init_db()
    with database.get_connection() as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE tbl_name = ?", (TABLE,))}
    assert {TABLE, f"trg_{TABLE}_no_update", f"trg_{TABLE}_expiry_only"} <= names
    # No link, message or key column exists.
    with database.get_connection() as conn:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({TABLE})")}
    assert not cols & {"link", "url", "messages", "api_key", "prompt", "config"}


def test_append_only_and_seven_day_floor(model):
    old = int(time.time()) - 8 * 86400
    with database.get_connection() as conn:
        assert insert_evidence(conn, make_row(created_at=int(time.time())),
                               max_rows=10, max_total_bytes=10**6)
        assert insert_evidence(conn, make_row(created_at=old),
                               max_rows=10, max_total_bytes=10**6)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(f"UPDATE {TABLE} SET stage = 'stage2'")
        young = conn.execute(
            f"SELECT id, dedupe_key FROM {TABLE} WHERE created_at > ?", (old + 1,)
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="seven days"):
            conn.execute(f"DELETE FROM {TABLE} WHERE id = ?", (young["id"],))
        assert conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 2


@pytest.mark.parametrize(
    "configured,ages_days,survivors",
    [
        (None, (1, 6, 8, 40), 2),      # default 7 days
        (1, (1, 6, 8, 40), 2),         # cannot go below the 7-day floor
        (14, (1, 10, 13, 15, 40), 3),  # a longer window is honoured
        (30, (1, 29, 31, 60), 2),      # the longest allowed window
        (999, (1, 29, 31, 60), 1),     # out of range => the 7-day default
    ],
)
def test_cleanup_expires_only_old_rows_and_is_trigger_compatible(
    model, configured, ages_days, survivors
):
    now = int(time.time())
    with database.get_connection() as conn:
        for age in ages_days:
            insert_evidence(conn, make_row(created_at=now - int(age * 86400) - 60),
                            max_rows=99, max_total_bytes=10**6)
        cfg = base_cfg({"retention_days": configured} if configured else {})
        database.cleanup_old_entries(conn, cfg)  # must not raise with triggers present
        conn.commit()
        left = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    assert left == survivors


def test_retention_floor_holds_in_every_layer(model):
    assert database._first_rejection_retention_days({"first_rejection_retention": {"retention_days": 1}}) == 7
    assert database._first_rejection_retention_days({"first_rejection_retention": {"retention_days": 99}}) == 7  # invalid => default
    assert database._first_rejection_retention_days({"first_rejection_retention": "bad"}) == 7
    five_days = int(time.time()) - 5 * 86400
    with database.get_connection() as conn:
        insert_evidence(conn, make_row(created_at=five_days), max_rows=9, max_total_bytes=10**6)
        assert purge_expired(conn, 1) == 0  # the helper clamps ...
        with pytest.raises(sqlite3.IntegrityError):  # ... and the trigger refuses anyway
            conn.execute(f"DELETE FROM {TABLE}")


def test_expiry_failure_never_aborts_the_rest_of_cleanup(model):
    class Boom:
        total_changes = 0

        def execute(self, *_a):
            raise sqlite3.OperationalError("database is locked")

    assert purge_expired(Boom(), 7) == 0


# ── switch off: zero writes, no capture/row/queue/thread (not zero overhead) ──


@pytest.mark.parametrize("cfg_value", [None, {}, {"enabled": False}, {"enabled": "true"}, "on"])
def test_switch_off_means_no_write_no_thread_no_capture(monkeypatch, model, cfg_value):
    monkeypatch.setattr(retention, "_WRITER", None)
    for name in ("submit", "_write_row", "_writer", "build_rows"):
        monkeypatch.setattr(retention, name, lambda *a, **k: pytest.fail("retention ran"))
    cfg = base_cfg()
    if cfg_value is not None:
        cfg["first_rejection_retention"] = cfg_value
    entry, (result, reason) = run(model, cfg)
    assert reason == "filtered" and model.calls == ["first", "second"]
    assert not hasattr(entry, retention.PENDING_ATTR)
    assert retention._WRITER is None
    assert WRITER_THREADS() == []
    assert rows(flush=False) == []


def WRITER_THREADS():
    return [t for t in threading.enumerate() if t.name == retention.WRITER_THREAD_NAME]


def opts(**values):
    return retention.retention_options(
        {"first_rejection_retention": {"enabled": True, **values}}
    )


def test_hard_maxima_and_invalid_values_fall_back_to_the_safe_default():
    default = opts()
    assert default["max_rows"] == 20000 and default["max_total_bytes"] == 64 * 1024 * 1024
    assert default["source_max_chars"] == 20000 and default["retention_days"] == 7
    # above the approved ceilings => default (= the ceiling), never a larger value
    for key, over in (("max_rows", 20001), ("max_rows", 200000),
                      ("max_total_bytes", 64 * 1024 * 1024 + 1),
                      ("max_total_bytes", 512 * 1024 * 1024),
                      ("source_max_chars", 20001), ("source_max_bytes", 80001),
                      ("row_max_bytes", 262145), ("retention_days", 31)):
        assert opts(**{key: over})[key] == default[key], (key, over)
    # configuration can lower them
    low = opts(max_rows=5, max_total_bytes=2 * 1024 * 1024, source_max_chars=1000)
    assert (low["max_rows"], low["max_total_bytes"], low["source_max_chars"]) == (
        5, 2 * 1024 * 1024, 1000)


BAD_VALUES = [float("inf"), float("-inf"), float("nan"), -5, 0, True, False, "abc",
              "100", None, [], {}, 10**30, 1e308, 7.5]


@pytest.mark.parametrize("bad", BAD_VALUES, ids=repr)
@pytest.mark.parametrize(
    "key", ["retention_days", "source_max_chars", "source_max_bytes", "row_max_bytes",
            "max_rows", "max_total_bytes", "write_timeout_ms", "queue_max"],
)
def test_invalid_numbers_fall_back_to_the_default_and_never_raise(key, bad):
    got = opts(**{key: bad})  # int(float("inf")) would raise OverflowError
    assert got == opts()
    assert database._first_rejection_retention_days(
        {"first_rejection_retention": {"retention_days": bad}}) == 7


def test_hard_caps_also_hold_inside_the_insert_statement(monkeypatch, model):
    import db.first_rejection_evidence as evidence

    monkeypatch.setattr(evidence, "MAX_ROWS_HARD", 2)
    with database.get_connection() as conn:
        written = [insert_evidence(conn, make_row(), max_rows=10**9, max_total_bytes=10**12)
                   for _ in range(3)]
    assert written == [True, True, False]
    monkeypatch.setattr(evidence, "MAX_ROWS_HARD", 100)
    monkeypatch.setattr(evidence, "MAX_TOTAL_BYTES_HARD", 250)
    with database.get_connection() as conn:
        assert not insert_evidence(conn, make_row(row_bytes=100), max_rows=10**9,
                                   max_total_bytes=10**12)  # 200 + 100 > 250


# ── what a row binds ─────────────────────────────────────────────────────


def test_row_binds_input_rejected_output_reason_final_result_and_versions(monkeypatch, model):
    monkeypatch.setenv("AI_FILTER_VERSION", "my-ai-filter:test-release")
    entry, (result, reason) = run(model, base_cfg(ON))
    assert reason == "filtered" and model.calls == ["first", "second"]
    [row] = rows()
    assert row["stage"] == "stage1" and row["replayable"] == 1 and row["replay_limit"] == ""
    # the exact input the validator saw
    assert row["input_title"] == TITLE and row["input_source"] == "当时实际来源摘要"
    assert row["input_source_chars"] == 8
    # the first rejected output and its parse, plus the rejection
    assert json.loads(row["rejected_raw"]) == json.loads(BAD)
    assert json.loads(row["rejected_parsed"])[:3] == [8.0, "公司公告", "乙公司获得50亿元订单"]
    assert (row["rejected_backend"], row["rejected_attempt"]) == ("first", "initial")
    assert row["failure_reason"] == "ungrounded_score_facts"
    assert row["fact_failure_kind"] == "event_fact_failure"
    # what ended up stored
    later = json.loads(row["later_attempts"])
    final = json.loads(row["final_handling"])
    assert any(a["backend_name"] == "second" and a["accepted"] for a in later)
    assert final["stage_outcome"]["accepted"] and final["stage_outcome"]["is_fixed_safe_neutral"]
    assert final["stored"]["score_path"] == entry.scoring_meta["score_path"]
    assert final["stored"]["event_id"] == entry.event_id == row["event_id"]
    assert row["accepted_is_fixed_neutral"] == 1
    # run revision and validator version, not just the strategy name
    assert row["run_revision"] == "my-ai-filter:test-release"
    assert row["strategy_version"] == "retention-test-contract"
    files = json.loads(row["validator_files"])
    assert "core/scorer.py" in files and row["validator_version"].startswith("v2:")
    # bound to the ledger row by its (hash-derived) key, without the link
    with database.get_connection() as conn:
        key = conn.execute("SELECT signal_key FROM backtest_signals").fetchone()[0]
    assert row["signal_key"] == key


def test_accepted_first_attempt_writes_nothing(model):
    model.outputs["first"] = json.dumps(
        {"score": 8, "tag": "公司公告", "logic": "甲公司已建设工厂", "sentiment": "看多"},
        ensure_ascii=False,
    )
    run(model, base_cfg(ON))
    assert rows() == []


def test_runtime_cache_reuse_is_not_a_new_attempt(model):
    run(model, base_cfg(ON))
    assert len(rows()) == 1
    run(model, base_cfg(ON))  # same strategy: cached, no attempts
    assert len(rows()) == 1


# ── independent of the legacy capture directory ──────────────────────────


def test_works_when_legacy_capture_is_full_and_its_hook_is_not_started(
    monkeypatch, model, tmp_path
):
    cohort = tmp_path / "full-cohort"
    cohort.mkdir(mode=0o700)
    for index in range(500):
        (cohort / f"{index:064x}.json").write_text("{}")
    (cohort / ".capture.complete").write_text("complete\n")
    monkeypatch.setattr(
        legacy_capture, "_publish_capture",
        lambda *a, **k: pytest.fail("legacy capture must not start"),
    )
    cfg = base_cfg(
        ON, score_evidence_capture={"enabled": True, "directory": str(cohort)}
    )
    entry, _ = run(model, cfg)
    assert not hasattr(entry, "_score_evidence_pending")  # legacy hook never started
    assert len(rows()) == 1
    assert len(list(cohort.glob("*.json"))) == 500


def test_legacy_and_retention_share_one_capture_without_interfering(model, tmp_path):
    cohort = tmp_path / "open-cohort"
    cfg = base_cfg(ON, score_evidence_capture={"enabled": True, "directory": str(cohort)})
    run(model, cfg)
    assert len(rows()) == 1
    [record] = [json.loads(p.read_text()) for p in cohort.glob("*.json")]
    assert record["attempts"][0]["raw_response"] == BAD


# ── never block scoring on the write (async + bounded; latency not benchmarked) ──


def scored_state():
    with database.get_connection() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT score, score_path, strategy_version FROM news_cache")]


def test_failing_writer_is_a_counted_skip_and_scoring_is_identical(monkeypatch, model):
    entry_ok, result_ok = run(model, base_cfg(), link="https://invalid.test/ok")
    baseline = scored_state()

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(retention, "insert_evidence", boom)
    before = stats()
    entry, result = run(model, base_cfg(ON), link="https://invalid.test/ok2")
    assert retention.flush(10)
    assert delta(before) == {"failed_locked": 1}
    assert result[1] == result_ok[1]
    assert scored_state()[0] == baseline[0] and len(scored_state()) == 2
    assert rows() == []


def test_hook_exception_is_swallowed_and_scoring_is_unchanged(monkeypatch, model):
    monkeypatch.setattr(
        retention, "build_rows", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    entry, (result, reason) = run(model, base_cfg(ON))
    assert reason == "filtered" and rows() == []


def test_slow_writer_does_not_slow_scoring(monkeypatch, model):
    gate = threading.Event()
    started = threading.Event()

    def slow(*a, **k):
        started.set()
        gate.wait(30)

    monkeypatch.setattr(retention, "_write_row", slow)
    try:
        t0 = time.monotonic()
        entry, (result, reason) = run(model, base_cfg(ON))
        elapsed = time.monotonic() - t0
        assert reason == "filtered"
        assert elapsed < 2.0  # the writer is stuck for 30 s; scoring did not wait
        assert started.wait(5)
        assert rows(flush=False) == []
    finally:
        gate.set()
    assert retention.flush(10)


def test_full_queue_drops_and_counts_without_blocking(monkeypatch, model):
    gate = threading.Event()
    monkeypatch.setattr(retention, "_write_row", lambda *a, **k: gate.wait(30))
    monkeypatch.setattr(retention, "_WRITER", None)
    cfg = base_cfg({"enabled": True, "queue_max": 1})
    before = stats()
    try:
        t0 = time.monotonic()
        for index in range(6):
            run(model, cfg, link=f"https://invalid.test/q{index}")
        assert time.monotonic() - t0 < 5.0
        assert delta(before).get("dropped_queue_full", 0) >= 3
    finally:
        gate.set()
    assert retention.flush(10)


def test_locked_database_write_is_bounded_by_the_timeout(model, tmp_path):
    db_path = database.DB_PATH
    blocker = sqlite3.connect(db_path, timeout=1, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    options = opts(write_timeout_ms=100)
    before = stats()
    try:
        t0 = time.monotonic()
        retention._write_row(db_path, options, make_row())
        elapsed = time.monotonic() - t0
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert elapsed < 1.5
    assert delta(before) == {"failed_locked": 1}
    assert rows(flush=False) == []


# ── idempotency / single flight ──────────────────────────────────────────


def pending_of(entry):
    return getattr(entry, retention.PENDING_ATTR)


def test_same_attempt_replayed_by_a_retry_is_stored_once(model):
    entry, _ = run(model, base_cfg(ON))
    assert len(rows()) == 1
    options = opts()
    capture = {
        "article_id": "a", "strategy_version": "retention-test-contract", "input_hash": "h",
        "source": {"raw_title": TITLE, "summary": "s"}, "evidence_complete": True,
        "attempts": [dict(stage="stage1", attempt="initial", accepted=False, request_hash="r",
                          backend_name="b", model_name="m", raw_response=BAD,
                          parsed=[8.0, "t", "l", "x"], failure_reason="ungrounded_score_facts",
                          source_in_request=True)],
    }
    snap = {"event_id": "e", "link": LINK, "strategy_version": "s", "revision_key": "base"}
    built = retention.build_rows(capture, options, snap)
    before = stats()
    for _ in range(3):  # a webhook retry rebuilds and resubmits the same attempt
        retention.submit(database.DB_PATH, options, built[0])
    assert retention.flush(10)
    assert delta(before) == {"inserted": 1, "duplicate": 2}
    # A genuinely different model output is a different attempt: a new row.
    capture["attempts"][0]["raw_response"] = BAD.replace("50", "60")
    again = retention.build_rows(capture, options, snap)
    retention.submit(database.DB_PATH, options, again[0])
    assert retention.flush(10)
    assert len(rows()) == 3


def test_concurrent_submitters_of_one_attempt_write_one_row(model):
    options = opts()
    row = make_row()
    threads = [
        threading.Thread(target=retention.submit, args=(database.DB_PATH, options, row))
        for _ in range(8)
    ]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert retention.flush(10)
    assert len(rows()) == 1


def test_single_flight_same_link_scores_and_stores_once(model):
    cfg = base_cfg(ON)
    results = []

    def go():
        results.append(run(model, cfg)[1][1])

    threads = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(results) == 4
    assert model.calls.count("first") == 1  # #175: only one thread scored the link
    assert len(rows()) == 1


# ── ledger identity ──────────────────────────────────────────────────────


def ledger_identity():
    with database.get_connection() as conn:
        signals = [tuple(r) for r in conn.execute(
            "SELECT signal_key, strategy_version, score, score_path, direction "
            "FROM backtest_signals ORDER BY signal_key")]
        cache = [tuple(r) for r in conn.execute(
            "SELECT link, strategy_version, score, score_path, direction, tag, logic "
            "FROM news_cache ORDER BY link")]
    return signals, cache


def test_ledger_identity_is_unchanged_by_the_switch(monkeypatch, model, tmp_path):
    results = {}
    for name, cfg in (("off", base_cfg()), ("on", base_cfg(ON))):
        path = str(tmp_path / f"{name}.db")
        monkeypatch.setattr(database, "DB_PATH", path)
        database.init_db()
        model.scorer.clear_runtime_cache()
        run(model, cfg)
        assert retention.flush(10)
        results[name] = ledger_identity()
    assert results["on"] == results["off"] and results["on"][0]


def test_strategy_fingerprint_is_independent_of_the_switch_and_files():
    cfg = {"threshold": {"pass": 7}, "feeds": []}
    on = dict(cfg, first_rejection_retention={"enabled": True, "max_rows": 5})
    assert core_config._strategy_fingerprint(cfg) == core_config._strategy_fingerprint(on)
    tracked = (ROOT / "core" / "config.py").read_text()
    for new_file in ("first_rejection_retention", "first_rejection_evidence",
                     "replay_first_rejection"):
        assert new_file not in tracked.split("rel_paths = [")[1].split("]")[0]


# ── caps ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "option,change,limit",
    [
        ("source", {"summary": "长" * 20001}, "source_over_cap"),
        ("source_bytes", {"summary": "长" * 4000, "cap": {"source_max_bytes": 4000}}, "source_over_cap"),
        ("title", {"title": "题" * 2001}, "title_over_cap"),
    ],
)
def test_over_cap_input_is_marked_not_fully_replayable(model, option, change, limit):
    extra = dict(change)
    cap = extra.pop("cap", {})
    summary = extra.pop("summary", SUMMARY)
    title = extra.pop("title", TITLE)
    # keep the rejected output ungrounded whatever the long text is
    cfg = base_cfg({"enabled": True, **cap})
    run(model, cfg, title=title, summary=summary)
    [row] = rows()
    assert row["replayable"] == 0 and limit in row["replay_limit"].split(",")
    full = summary if isinstance(summary, str) else SUMMARY
    if "source" in option:
        # stored prefix is within the cap and the original size/hash are kept
        assert len(row["input_source"]) <= 20000
        assert row["input_source_chars"] == len(full)
        assert len(row["input_source"].encode()) <= (cap.get("source_max_bytes") or 80000)
    assert row["failure_reason"]  # the rejection itself is still recorded


def test_oversized_raw_output_is_marked_not_fully_replayable(model):
    model.outputs["first"] = json.dumps(
        {"score": 8, "tag": "公司公告", "logic": "乙" * 5000, "sentiment": "看多"},
        ensure_ascii=False,
    )
    run(model, base_cfg(ON))
    [row] = rows()
    assert row["replayable"] == 0 and "raw_over_cap" in row["replay_limit"]
    assert len(row["rejected_raw"]) == 4096


def test_within_cap_input_is_replayable_and_unmodified(model):
    long_text = "甲公司已建设工厂。" * 2000  # 18,000 characters
    run(model, base_cfg(ON), summary=long_text)
    [row] = rows()
    assert row["replayable"] == 1 and row["input_source"] == long_text
    assert row["input_source_chars"] == 18000


def test_row_byte_cap_skips_instead_of_storing_partial_rows(model):
    before = stats()
    run(model, base_cfg({"enabled": True, "row_max_bytes": 16384}), summary="长" * 9000)
    assert rows() == [] and delta(before) == {"skipped_row_cap": 1}


def test_total_volume_caps_skip_and_count(model):
    cfg = base_cfg({"enabled": True, "max_rows": 1})
    before = stats()
    run(model, cfg, link="https://invalid.test/v1")
    run(model, cfg, link="https://invalid.test/v2")
    assert len(rows()) == 1
    assert delta(before).get("inserted") == 1 and delta(before).get("skipped_volume_cap") == 1
    # the byte cap is evaluated in the same statement
    with database.get_connection() as conn:
        assert not insert_evidence(conn, make_row(row_bytes=10**6),
                                   max_rows=99, max_total_bytes=500)


# ── privacy ──────────────────────────────────────────────────────────────


def test_no_secrets_link_or_request_messages_are_stored(monkeypatch, model):
    secrets = {
        "KIMI_CODE_API_KEY": "sk-kimi-SECRET-1",
        "SILICONFLOW_API_KEY": "sk-silicon-SECRET-2",
        "LLM_API_KEY": "sk-llm-SECRET-3",
        "AI_FILTER_API_KEY": "filter-key-SECRET-4",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    cfg = base_cfg(ON, llm={"api_key": "cfg-key-SECRET-5"})
    run(model, cfg)
    blob = json.dumps(rows(), ensure_ascii=False)
    for needle in (*secrets.values(), "cfg-key-SECRET-5", "SECRET-LINK-TOKEN",
                   "invalid.test/a", "SYSTEM-PROMPT-MARKER"):
        assert needle not in blob
    # Documented, deliberate: article text and model output are stored.
    assert TITLE in blob and "乙公司获得50亿元订单" in blob


# ── replay tool ──────────────────────────────────────────────────────────


@pytest.fixture
def replay_tool():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "replay_first_rejection", ROOT / "scripts" / "replay_first_rejection.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_replay_reproduces_the_recorded_verdict_under_the_same_validator(replay_tool, model):
    run(model, base_cfg(ON))
    assert retention.flush(10)
    report = replay_tool.replay_rows(
        replay_tool.read_rows(database.DB_PATH), code_root=None, git_repo=None
    )
    assert [r["status"] for r in report] == ["match"]
    assert report[0]["validator_same"] is True and report[0]["parsed_equal"] is True
    assert report[0]["later_attempts_rejudged"] == 1  # the accepted second-backend answer


def corrupted(model, **changes):
    run(model, base_cfg(ON))
    [row] = rows()
    row.update(changes)
    return row


def test_replay_flags_a_verdict_mismatch_under_the_same_validator(replay_tool, model):
    row = corrupted(model, failure_reason="ungrounded_score_theme")
    [report] = replay_tool.replay_rows([row], code_root=None, git_repo=None)
    assert report["status"] == "MISMATCH_SAME_VALIDATOR"
    assert report["recorded"]["reason"] == "ungrounded_score_theme"
    assert report["replayed"]["reason"] == "ungrounded_score_facts"


def test_replay_rejudges_later_attempts_and_flags_a_changed_outcome(replay_tool, model):
    run(model, base_cfg(ON))
    [row] = rows()
    later = json.loads(row["later_attempts"])
    later[0]["failure_reason"] = "ungrounded_score_theme"  # recorded as rejected, replays accepted
    row["later_attempts"] = json.dumps(later)
    [report] = replay_tool.replay_rows([row], code_root=None, git_repo=None)
    assert report["status"] == "MISMATCH_SAME_VALIDATOR" and not report["parsed_equal"]


def test_replay_reports_validator_drift_instead_of_a_misjudgement(replay_tool, model):
    run(model, base_cfg(ON))
    [row] = rows()
    drifted = {k: ("0" * 64 if k == "core/scorer.py" else v)
               for k, v in json.loads(row["validator_files"]).items()}
    row["validator_files"] = json.dumps(drifted)
    row["failure_reason"] = "ungrounded_score_theme"
    [report] = replay_tool.replay_rows([row], code_root=None, git_repo=None)
    assert report["status"] == "differs_other_validator" and not report["validator_same"]
    row["failure_reason"] = "ungrounded_score_facts"
    [report] = replay_tool.replay_rows([row], code_root=None, git_repo=None)
    assert report["status"] == "match_other_validator"


def test_replay_skips_not_fully_replayable_rows(replay_tool, model):
    run(model, base_cfg(ON), summary="长" * 20001)
    assert retention.flush(10)
    [report] = replay_tool.replay_rows(replay_tool.read_rows(database.DB_PATH),
                                       code_root=None, git_repo=None)
    assert report["status"] == "not_replayable" and "source_over_cap" in report["replay_limit"]


def test_replay_cli_exit_status_and_read_only_database(replay_tool, model, capsys):
    run(model, base_cfg(ON))
    assert retention.flush(10)
    assert replay_tool.main(["--db", database.DB_PATH]) == 0
    assert '"match": 1' in capsys.readouterr().out
    with sqlite3.connect(f"file:{database.DB_PATH}?mode=ro", uri=True) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(f"DELETE FROM {TABLE}")


def test_replay_extracts_the_recorded_commit_and_refuses_non_commits(replay_tool, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    (repo / "marker.txt").write_text("v1")
    for cmd in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "c"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, env=env)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True).stdout.strip()
    out = replay_tool.extract_revision(str(repo), sha, tmp_path / "x")
    assert (out / "marker.txt").read_text() == "v1"
    assert replay_tool.extract_revision(str(repo), "my-ai-filter:2026.10", tmp_path / "y") is None
    assert replay_tool.extract_revision(str(repo), "0" * 40, tmp_path / "z") is None


# ── fix 1: configuration anomalies / init failures never interrupt scoring ────


def scoring_outcome(entry_and_result):
    entry, (result, reason) = entry_and_result
    return reason, scored_state()


@pytest.mark.parametrize("bad", BAD_VALUES, ids=repr)
@pytest.mark.parametrize("key", ["max_rows", "retention_days", "source_max_chars",
                                 "write_timeout_ms", "queue_max", "max_total_bytes"])
def test_bad_config_never_interrupts_scoring_on_the_real_path(model, key, bad):
    baseline = scoring_outcome(run(model, base_cfg(), link="https://invalid.test/base"))
    model.scorer.clear_runtime_cache()
    before = list(model.calls)
    got = scoring_outcome(run(model, base_cfg({"enabled": True, key: bad}),
                              link="https://invalid.test/cfg"))
    assert got[0] == baseline[0] == "filtered"
    assert model.calls[len(before):] == ["first", "second"]  # scored normally
    assert got[1][0] == baseline[1][0] and len(got[1]) == 2
    [row] = rows()  # and the evidence row was still written, with defaults
    assert row["replayable"] == 1


def test_yaml_inf_and_nan_literals_do_not_break_scoring(model):
    import yaml

    loaded = yaml.safe_load("first_rejection_retention:\n  enabled: true\n"
                            "  max_rows: .inf\n  retention_days: .nan\n  queue_max: -.inf\n")
    assert loaded["first_rejection_retention"]["max_rows"] == float("inf")
    reason = run(model, base_cfg(**loaded))[1][1]
    assert reason == "filtered" and len(rows()) == 1
    with database.get_connection() as conn:  # cleanup path with the same config
        database.cleanup_old_entries(conn, base_cfg(**loaded))


@pytest.mark.parametrize("exc", [OverflowError("inf"), ValueError("x"), RuntimeError("boom"),
                                 MemoryError()])
def test_init_that_raises_is_isolated_and_scoring_completes(monkeypatch, model, exc):
    def explode(cfg):
        raise exc

    monkeypatch.setattr(legacy_capture, "retention_options", explode)
    entry, (result, reason) = run(model, base_cfg(ON))
    assert reason == "filtered" and model.calls == ["first", "second"]
    assert not hasattr(entry, retention.PENDING_ATTR)
    assert scored_state() and rows(flush=False) == []


def test_init_failure_does_not_disable_the_legacy_capture(monkeypatch, model, tmp_path):
    monkeypatch.setattr(legacy_capture, "retention_options",
                        lambda cfg: (_ for _ in ()).throw(OverflowError("inf")))
    cohort = tmp_path / "cohort"
    run(model, base_cfg(ON, score_evidence_capture={"enabled": True, "directory": str(cohort)}))
    assert len(list(cohort.glob("*.json"))) == 1


# ── fix 3: conflict writes never change an existing row ───────────────────────

CONFLICT_VERBS = {
    "insert-or-replace": "INSERT OR REPLACE INTO {t} ({c}) VALUES ({v})",
    "replace-into": "REPLACE INTO {t} ({c}) VALUES ({v})",
    "insert-or-ignore": "INSERT OR IGNORE INTO {t} ({c}) VALUES ({v})",
    "plain-insert": "INSERT INTO {t} ({c}) VALUES ({v})",
    "upsert-do-update": ("INSERT INTO {t} ({c}) VALUES ({v}) "
                         "ON CONFLICT(dedupe_key) DO UPDATE SET input_title = 'HIJACK'"),
    "insert-or-fail": "INSERT OR FAIL INTO {t} ({c}) VALUES ({v})",
    "insert-or-rollback": "INSERT OR ROLLBACK INTO {t} ({c}) VALUES ({v})",
}


def raw_connection(recursive):
    conn = sqlite3.connect(database.DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA recursive_triggers={'ON' if recursive else 'OFF'}")
    assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == int(recursive)
    return conn


@pytest.mark.parametrize("recursive", [False, True], ids=["recursive-off", "recursive-on"])
@pytest.mark.parametrize("age_days", [0, 3, 8, 400])
@pytest.mark.parametrize("verb", sorted(CONFLICT_VERBS))
@pytest.mark.parametrize("conflict_on", ["dedupe_key", "id"])
def test_a_conflicting_write_never_changes_an_existing_row(
    model, recursive, age_days, verb, conflict_on
):
    conn = raw_connection(recursive)
    try:
        original = make_row(created_at=int(time.time()) - age_days * 86400 - 30,
                            input_title="ORIGINAL", dedupe_key="K1")
        assert insert_evidence(conn, original, max_rows=9, max_total_bytes=10**6)
        before = dict(conn.execute(f"SELECT * FROM {TABLE}").fetchone())
        attempt = make_row(input_title="REPLACEMENT", created_at=int(time.time()),
                           dedupe_key="K1" if conflict_on == "dedupe_key" else "K2")
        columns = list(COLUMNS) + (["id"] if conflict_on == "id" else [])
        values = [attempt[c] for c in COLUMNS] + ([before["id"]] if conflict_on == "id" else [])
        sql = CONFLICT_VERBS[verb].format(
            t=TABLE, c=", ".join(columns), v=", ".join("?" for _ in columns))
        try:
            conn.execute(sql, values)
        except sqlite3.IntegrityError:
            pass  # a refusal is fine; a change is not
        rows_now = [dict(r) for r in conn.execute(f"SELECT * FROM {TABLE}")]
        assert rows_now == [before]
    finally:
        conn.close()


@pytest.mark.parametrize("recursive", [False, True], ids=["recursive-off", "recursive-on"])
def test_non_conflicting_writes_and_idempotent_inserts_still_work(model, recursive):
    conn = raw_connection(recursive)
    try:
        assert insert_evidence(conn, make_row(dedupe_key="A"), max_rows=9, max_total_bytes=10**6)
        assert not insert_evidence(conn, make_row(dedupe_key="A", input_title="again"),
                                   max_rows=9, max_total_bytes=10**6)  # idempotent, no error
        assert insert_evidence(conn, make_row(dedupe_key="B"), max_rows=9, max_total_bytes=10**6)
        conn.execute(
            f"INSERT OR REPLACE INTO {TABLE} ({', '.join(COLUMNS)}) VALUES "
            f"({', '.join('?' for _ in COLUMNS)})",
            tuple(make_row(dedupe_key="C")[c] for c in COLUMNS))  # new key: a plain insert
        titles = [r[0] for r in conn.execute(f"SELECT dedupe_key FROM {TABLE} ORDER BY id")]
        assert titles == ["A", "B", "C"]
    finally:
        conn.close()


@pytest.mark.parametrize("recursive", [False, True], ids=["recursive-off", "recursive-on"])
def test_expiry_delete_of_old_rows_stays_allowed_and_young_delete_stays_blocked(model, recursive):
    conn = raw_connection(recursive)
    now = int(time.time())
    try:
        for key, age in (("old", 8), ("older", 40), ("young", 2)):
            insert_evidence(conn, make_row(dedupe_key=key, created_at=now - age * 86400 - 60),
                            max_rows=9, max_total_bytes=10**6)
        with pytest.raises(sqlite3.IntegrityError, match="seven days"):
            conn.execute(f"DELETE FROM {TABLE} WHERE dedupe_key = 'young'")
        assert purge_expired(conn, 7) == 2  # the controlled expiry path
        assert [r[0] for r in conn.execute(f"SELECT dedupe_key FROM {TABLE}")] == ["young"]
        # an expired key can be written again afterwards (a fresh, new row)
        assert insert_evidence(conn, make_row(dedupe_key="old"), max_rows=9, max_total_bytes=10**6)
    finally:
        conn.close()


def test_duplicate_through_the_writer_is_counted_as_duplicate(model):
    options = opts()
    row = make_row(dedupe_key="DUP")
    before = stats()
    retention.submit(database.DB_PATH, options, row)
    retention.submit(database.DB_PATH, options, dict(row, input_title="other"))
    assert retention.flush(10)
    assert delta(before) == {"inserted": 1, "duplicate": 1}
    assert rows()[0]["input_title"] == "x"


# ── fix 4: replay exit codes ──────────────────────────────────────────────────


def make_db(tmp_path, *rows_):
    path = str(tmp_path / "replay.db")
    conn = sqlite3.connect(path)
    from db.first_rejection_evidence import init_first_rejection_storage

    init_first_rejection_storage(conn.cursor())
    for index, row in enumerate(rows_):
        row = {k: v for k, v in row.items() if k != "id"}
        row["dedupe_key"] = f"{index}-{row['dedupe_key']}"
        assert insert_evidence(conn, row, max_rows=99, max_total_bytes=10**7)
    conn.close()
    return path


def variants(model):
    run(model, base_cfg(ON))
    [good] = rows()
    return {
        "match": good,
        "mismatch": dict(good, failure_reason="ungrounded_score_theme"),
        "not_replayable": dict(good, replayable=0, replay_limit="source_over_cap"),
    }


def test_exit_code_precedence_table(replay_tool):
    code = replay_tool.exit_code_for
    st = lambda *names: [{"status": n} for n in names]  # noqa: E731
    assert code(st("match", "match_other_validator", "differs_other_validator")) == 0
    assert code(st()) == 0
    assert code(st("match", "not_replayable")) == 3
    assert code(st("match", "not_replayable", "MISMATCH_SAME_VALIDATOR")) == 1
    assert code(st("match", "not_replayable", "MISMATCH_SAME_VALIDATOR", "error")) == 2
    assert code(st("error")) == 2
    assert code(st("match", "error", "not_replayable")) == 2


def test_mixed_results_report_every_row_and_exit_with_the_highest_precedence(
    replay_tool, model, tmp_path
):
    v = variants(model)
    empty = tmp_path / "empty-code-root"
    empty.mkdir()
    reports = (
        replay_tool.replay_rows([v["match"]], code_root=None, git_repo=None)
        + replay_tool.replay_rows([v["mismatch"]], code_root=None, git_repo=None)
        + replay_tool.replay_rows([v["not_replayable"]], code_root=None, git_repo=None)
        + replay_tool.replay_rows([v["match"]], code_root=empty, git_repo=None)  # cannot run
    )
    assert [r["status"] for r in reports] == [
        "match", "MISMATCH_SAME_VALIDATOR", "not_replayable", "error"]
    assert replay_tool.exit_code_for(reports) == 2
    assert replay_tool.exit_code_for(reports[:3]) == 1
    assert replay_tool.exit_code_for([reports[0], reports[2]]) == 3
    assert replay_tool.exit_code_for(reports[:1]) == 0


@pytest.mark.parametrize(
    "names,expected",
    [(["match"], 0), (["not_replayable"], 3), (["mismatch"], 1),
     (["match", "mismatch", "not_replayable"], 1)],
)
def test_cli_exit_codes(replay_tool, model, tmp_path, capsys, names, expected):
    v = variants(model)
    db = make_db(tmp_path, *[v[n] for n in names])
    assert replay_tool.main(["--db", db]) == expected
    assert f"exit_code {expected}" in capsys.readouterr().out


def test_cli_execution_error_is_non_zero_even_with_other_good_rows(
    replay_tool, model, tmp_path, capsys
):
    v = variants(model)
    db = make_db(tmp_path, v["match"])
    empty = tmp_path / "empty"
    empty.mkdir()
    assert replay_tool.main(["--db", db, "--code-root", str(empty)]) == 2  # was 0 before
    assert "error" in capsys.readouterr().out
    assert replay_tool.main(["--db", str(tmp_path / "missing.db")]) == 2


def test_cli_json_carries_the_exit_code_and_summary(replay_tool, model, tmp_path, capsys):
    v = variants(model)
    db = make_db(tmp_path, v["match"], v["not_replayable"])
    assert replay_tool.main(["--db", db, "--json"]) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["exit_code"] == 3 and out["summary"] == {"match": 1, "not_replayable": 1}
    assert len(out["rows"]) == 2


def test_cli_process_exit_status_is_the_code(model, tmp_path):
    v = variants(model)
    db = make_db(tmp_path, v["mismatch"])
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "replay_first_rejection.py"),
                           "--db", db], capture_output=True, text=True, cwd=str(ROOT))
    assert done.returncode == 1


# ── wording guards: what the docs and --help state must stay true ─────────


def test_migration_creates_exactly_three_triggers_and_one_explicit_index():
    from db.first_rejection_evidence import init_first_rejection_storage

    conn = sqlite3.connect(":memory:")
    init_first_rejection_storage(conn.cursor())
    objects = {(kind, name) for kind, name in conn.execute(
        "SELECT type, name FROM sqlite_master WHERE name LIKE '%first_rejection%'")}
    triggers = {name for kind, name in objects if kind == "trigger"}
    assert triggers == {
        f"trg_{TABLE}_no_conflict_write", f"trg_{TABLE}_no_update", f"trg_{TABLE}_expiry_only"}
    assert ("index", f"idx_{TABLE}_created") in objects


def test_replay_help_says_exit_zero_does_not_prove_full_reproduction(replay_tool, capsys):
    with pytest.raises(SystemExit) as stop:
        replay_tool.main(["--help"])
    assert stop.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "does NOT prove all historical verdicts reproduced" in text
    assert "differs_other_validator" in text and "empty table" in text
