"""Private journal survives restarts without renewing input or score budgets."""
import copy
import json
import multiprocessing
import os
import stat

import pytest

from services import natural_score_continuations as journal

STRATEGY = "strategy-current"
LINK = "https://news.example/one"


def payload(**changes):
    return {
        "title": "公司发布经营信息", "summary": "原始摘要", "prompt": "行业提示词",
        "feed_url": "https://feeds.example/one", "few_shot_context": "",
        "published": [2026, 10, 20, 10, 5, 0, 1, 293, 0],
    } | changes


def read_record(strategy=STRATEGY, link=LINK):
    with journal.locked_record(strategy, link) as record:
        return None if record is None else record.data


def save_state(state, **changes):
    with journal.locked_record(STRATEGY, LINK) as record:
        data = record.data | changes | {"state": state}
        record.save(data)
        return record.data


def _child_busy(strategy, link, output):
    try:
        with journal.locked_record(strategy, link):
            output.put("unexpectedly acquired")
    except journal.ContinuationBusy:
        output.put("busy")


def test_absent_read_does_not_create_directory_and_can_enqueue_inside_context():
    directory = journal.journal_directory()
    with journal.locked_record(STRATEGY, LINK) as record:
        assert record is None and not directory.exists()
        assert journal.enqueue(STRATEGY, LINK, payload()) == "stored"
    assert read_record()["attempts"] == 1


def test_identity_matches_base_article_key_and_freezes_first_input():
    from db.database import _backtest_signal_key

    assert journal.identity(STRATEGY, LINK)[2] == _backtest_signal_key(LINK, STRATEGY)
    assert journal.enqueue(STRATEGY, LINK, payload()) == "stored"
    initial = read_record()
    assert journal.enqueue(STRATEGY, LINK, payload(title="另一标题"), 3) == "duplicate"
    assert read_record() == initial
    initial["payload"]["title"] = "changed copy"
    assert read_record()["payload"] == payload()
    assert journal.enqueue("another-strategy", LINK, payload()) == "stored"


def test_extended_source_payload_has_matching_identity_and_parsed_times():
    source = payload(link=LINK, published_parsed=[2026, 10, 20, 18, 5, 0, 1, 293, 0], updated_parsed=None)
    assert journal.enqueue(STRATEGY, LINK, source) == "stored"
    assert read_record()["payload"] == source
    assert journal.enqueue(STRATEGY, LINK + "/other", source) == "invalid"
    for parsed in ([1, 2], [True] * 9, "not-a-time"):
        assert journal.enqueue(STRATEGY, LINK + "/bad-time", payload(published_parsed=parsed)) == "invalid"


def test_files_and_directories_are_private_and_round_trip_unicode():
    assert journal.enqueue(STRATEGY, LINK, payload(summary="≤5亿元、±3%、🥳")) == "stored"
    directory = journal.journal_directory()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / ".locks").stat().st_mode) == 0o700
    for path in directory.rglob("*"):
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert read_record()["payload"]["summary"] == "≤5亿元、±3%、🥳"


def test_attempts_and_frozen_write_survive_a_fresh_process():
    assert journal.enqueue(STRATEGY, LINK, payload()) == "stored"
    save_state("commit_pending", attempts=3, frozen_result={"score": 4.0, "snapshot": {"scored_at": 10}})
    # A fresh spawned process has no inherited Python counters or thread locks.
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    process = context.Process(target=_child_read, args=(os.environ["AI_FILTER_DB_PATH"], output))
    process.start()
    process.join(10)
    assert process.exitcode == 0
    assert output.get(timeout=2) == {"attempts": 3, "state": "commit_pending", "score": 4.0}


def _child_read(database_path, output):
    os.environ["AI_FILTER_DB_PATH"] = database_path
    value = read_record()
    output.put({"attempts": value["attempts"], "state": value["state"], "score": value["frozen_result"]["score"]})


@pytest.mark.parametrize("field,value", [
    ("attempts", 0), ("attempts", True), ("attempts", 4),
    ("strategy_version", "other"), ("link", LINK + "/other"), ("created_at", 0),
    ("article_key", "0" * 64), ("schema", 2), ("state", "unknown"),
])
def test_save_rejects_identity_and_budget_changes(field, value):
    assert journal.enqueue(STRATEGY, LINK, payload()) == "stored"
    before = read_record()
    with journal.locked_record(STRATEGY, LINK) as record:
        with pytest.raises(journal.ContinuationInvalid):
            record.save(record.data | {field: value})
    assert read_record() == before


def test_input_cannot_change_and_record_cannot_save_after_unlock():
    assert journal.enqueue(STRATEGY, LINK, payload()) == "stored"
    with journal.locked_record(STRATEGY, LINK) as record:
        with pytest.raises(journal.ContinuationInvalid, match="frozen input"):
            record.save(record.data | {"payload": payload(summary="changed")})
    with pytest.raises(journal.ContinuationInvalid, match="lock has been released"):
        record.save(record.data)


@pytest.mark.parametrize("state", sorted(journal.TERMINAL_STATES))
def test_terminal_tombstone_clears_bodies_and_prevents_revival(state):
    assert journal.enqueue(STRATEGY, LINK, payload()) == "stored"
    terminal = save_state(state, attempts=3, frozen_result={"score": 4})
    assert "payload" not in terminal and "frozen_result" not in terminal
    assert terminal["attempts"] == 3
    with journal.locked_record(STRATEGY, LINK) as record:
        record.save(record.data)  # No-op is harmless.
        with pytest.raises(journal.ContinuationInvalid, match="terminal"):
            record.save(record.data | {"state": "pending", "payload": payload()})
    assert journal.enqueue(STRATEGY, LINK, payload(title="new"), 0) == "duplicate"
    assert read_record() == terminal


def test_identity_and_directory_locks_are_nonblocking():
    assert journal.enqueue(STRATEGY, LINK, payload()) == "stored"
    with journal.locked_record(STRATEGY, LINK):
        with pytest.raises(journal.ContinuationBusy):
            with journal.locked_record(STRATEGY, LINK):
                pass
    with journal._lock(journal.journal_directory() / ".lock"):
        with pytest.raises(journal.ContinuationBusy):
            journal.enqueue(STRATEGY, LINK + "/new", payload())


def test_cross_process_lock_is_nonblocking():
    assert journal.enqueue(STRATEGY, LINK, payload()) == "stored"
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    with journal.locked_record(STRATEGY, LINK):
        process = context.Process(target=_child_busy, args=(STRATEGY, LINK, output))
        process.start()
        process.join(10)
        assert process.exitcode == 0 and output.get(timeout=2) == "busy"


def test_active_capacity_and_total_capacity_are_separate(monkeypatch):
    monkeypatch.setattr(journal, "MAX_ACTIVE_RECORDS", 1)
    monkeypatch.setattr(journal, "MAX_RECORDS", 2)
    assert journal.enqueue(STRATEGY, LINK, payload()) == "stored"
    assert journal.enqueue(STRATEGY, LINK + "/two", payload()) == "full"
    save_state("completed")
    assert journal.enqueue(STRATEGY, LINK + "/two", payload()) == "stored"
    assert journal.enqueue(STRATEGY, LINK + "/three", payload()) == "full"
    assert len(list(journal.journal_directory().glob("*.json"))) == 2


@pytest.mark.parametrize("bad", [None, [], {}, {"title": "missing"}, payload(api_key="secret"),
                                  payload(published=float("nan")), payload(published=float("inf"))])
def test_bad_payload_never_creates_a_record(bad):
    assert journal.enqueue(STRATEGY, LINK, bad) == "invalid"
    assert not journal.journal_directory().exists()


@pytest.mark.parametrize("counter", [-1, 4, True, 1.5, "1"])
def test_initial_counter_requires_a_bounded_integer(counter):
    assert journal.enqueue(STRATEGY, LINK, payload(), counter) == "invalid"
    assert not journal.journal_directory().exists()


def test_oversized_multibyte_input_is_not_silently_truncated():
    large = payload(summary="中" * journal.MAX_RECORD_BYTES)
    assert journal.enqueue(STRATEGY, LINK, large) == "invalid"
    assert not journal.journal_directory().exists()
    assert journal.enqueue(STRATEGY, LINK, payload()) == "stored"
    before = read_record()
    with journal.locked_record(STRATEGY, LINK) as record:
        with pytest.raises(journal.ContinuationInvalid, match="byte limit"):
            record.save(record.data | {"frozen_result": {"body": "中" * journal.MAX_RECORD_BYTES}})
    assert read_record() == before


def test_due_fifo_limit_and_expiration_do_not_refresh_creation_time(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(journal.time, "time", lambda: clock[0])
    assert journal.enqueue(STRATEGY, LINK, payload()) == "stored"
    clock[0] = 1100
    assert journal.enqueue(STRATEGY, LINK + "/second", payload()) == "stored"
    with journal.locked_record(STRATEGY, LINK) as record:
        record.save(record.data | {"next_attempt_at": 1200, "updated_at": 1100})
    assert [row["link"] for row in journal.pending(STRATEGY, now=1160)] == [LINK + "/second"]
    assert [row["link"] for row in journal.pending({"scoring_strategy_version": STRATEGY}, limit=1, now=1200)] == [LINK]
    assert journal.pending(STRATEGY, now=1000 + journal.TTL_SECONDS) == [read_record(link=LINK + "/second")]
    assert read_record()["state"] == "expired" and read_record()["created_at"] == 1000
    assert "payload" not in read_record()
    assert journal.enqueue(STRATEGY, LINK, payload()) == "duplicate"


def test_contract_change_tombstones_pending_work_not_other_contract_execution():
    assert journal.enqueue(STRATEGY, LINK, payload()) == "stored"
    assert journal.pending("new-contract") == []
    assert read_record()["state"] == "contract_changed"
    assert journal.enqueue(STRATEGY, LINK, payload()) == "duplicate"
    assert journal.enqueue("new-contract", LINK, payload()) == "stored"
    assert len(journal.pending("new-contract", now=read_record("new-contract")["next_attempt_at"])) == 1


@pytest.mark.parametrize("body", ["{bad", '{"schema":1,"schema":1}', "{}", "[]"])
def test_corrupt_records_are_retained_and_fail_closed(body):
    assert journal.enqueue(STRATEGY, LINK, payload()) == "stored"
    path = next(journal.journal_directory().glob("*.json"))
    path.write_text(body)
    with pytest.raises(journal.ContinuationInvalid):
        journal.pending(STRATEGY)
    with pytest.raises(journal.ContinuationInvalid):
        read_record()
    assert journal.enqueue(STRATEGY, LINK, payload()) == "invalid"
    assert path.read_text() == body


@pytest.mark.parametrize("field", ["created_at", "updated_at", "next_attempt_at"])
@pytest.mark.parametrize("missing", [False, True])
def test_stored_time_missing_or_null_is_retained_and_fails_closed(field, missing):
    assert journal.enqueue(STRATEGY, LINK, payload()) == "stored"
    path = next(journal.journal_directory().glob("*.json"))
    data = json.loads(path.read_bytes())
    if missing:
        del data[field]
    else:
        data[field] = None
    body = json.dumps(data)
    path.write_text(body)
    for read in (lambda: journal.pending(STRATEGY), lambda: journal.overview(STRATEGY), read_record):
        with pytest.raises(journal.ContinuationInvalid, match="time is required"):
            read()
    assert journal.enqueue(STRATEGY, LINK, payload()) == "invalid"
    assert path.read_text() == body


def test_unknown_file_and_symlink_are_not_followed(tmp_path):
    assert journal.enqueue(STRATEGY, LINK, payload()) == "stored"
    directory = journal.journal_directory()
    (directory / "unknown.json").write_text("{}")
    with pytest.raises(journal.ContinuationInvalid, match="filename"):
        journal.pending(STRATEGY)
    (directory / "unknown.json").unlink()
    target = tmp_path / "outside.json"
    target.write_text("untouched")
    path = next(directory.glob("*.json"))
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(OSError):
        read_record()
    assert target.read_text() == "untouched"


def test_atomic_write_failure_preserves_old_record_and_cleans_temporary(monkeypatch):
    assert journal.enqueue(STRATEGY, LINK, payload()) == "stored"
    before = read_record()

    def fail_replace(*_args):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(journal.os, "replace", fail_replace)
    with journal.locked_record(STRATEGY, LINK) as record:
        with pytest.raises(OSError, match="synthetic"):
            record.save(record.data | {"attempts": 2})
    assert read_record() == before
    assert not list(journal.journal_directory().glob(".tmp-*"))


def test_frozen_result_json_is_bounded_and_cannot_include_configuration():
    assert journal.enqueue(STRATEGY, LINK, payload()) == "stored"
    with journal.locked_record(STRATEGY, LINK) as record:
        before = copy.deepcopy(record.data)
        for value in ({"cfg": {"llm": "secret"}}, {"nested": {"api_key": "secret"}}, {"score": float("inf")}):
            with pytest.raises(journal.ContinuationInvalid):
                record.save(record.data | {"frozen_result": value})
            assert record.data == before
    assert json.loads(next(journal.journal_directory().glob("*.json")).read_bytes()) == before


def test_defaults_are_hard_limits():
    assert journal.MAX_RECORDS == 4096 and journal.MAX_ACTIVE_RECORDS == 128
    assert journal.MAX_ATTEMPTS == 3 and journal.MAX_RECORD_BYTES == 256 * 1024
    assert journal.TTL_SECONDS == 86400
    with pytest.raises(journal.ContinuationInvalid):
        journal.pending(STRATEGY, limit=21)
    with pytest.raises(journal.ContinuationInvalid):
        journal.pending(STRATEGY, now=float("nan"))


def test_overview_is_readonly_and_includes_work_beyond_batch_limit(monkeypatch):
    assert journal.overview(STRATEGY)["total"] == 0
    assert not journal.journal_directory().exists()
    monkeypatch.setattr(journal.time, "time", lambda: 1000)
    for number in range(21):
        assert journal.enqueue(STRATEGY, LINK + f"/{number}", payload()) == "stored"
    assert len(journal.pending(STRATEGY, now=1060)) == 20
    before = {path.name: path.read_bytes() for path in journal.journal_directory().glob("*.json")}
    counts = journal.overview(STRATEGY, now=1060)
    assert counts["total"] == counts["active"] == counts["due"] == 21
    assert counts["terminal"] == counts["expired"] == counts["exhausted"] == 0
    assert journal.overview(STRATEGY, now=1000 + journal.TTL_SECONDS)["due"] == 0
    assert journal.overview("another-strategy", now=1001)["due"] == 0
    assert {path.name: path.read_bytes() for path in journal.journal_directory().glob("*.json")} == before


@pytest.mark.parametrize("attempts,delay", [(0, 60), (1, 60), (2, 120), (3, 180)])
def test_new_work_has_a_persistent_minimum_retry_delay(monkeypatch, attempts, delay):
    monkeypatch.setattr(journal.time, "time", lambda: 1000)
    assert journal.enqueue(STRATEGY, LINK, payload(), attempts) == "stored"
    assert read_record()["next_attempt_at"] == 1000 + delay
    assert journal.pending(STRATEGY, now=1000 + delay - 0.001) == []
    assert len(journal.pending(STRATEGY, now=1000 + delay)) == 1


def test_full_or_invalid_enqueues_do_not_create_new_identity_locks(monkeypatch):
    monkeypatch.setattr(journal, "MAX_ACTIVE_RECORDS", 1)
    assert journal.enqueue(STRATEGY, LINK, payload()) == "stored"
    directory = journal.journal_directory()
    for number in range(100):
        assert journal.enqueue(STRATEGY, LINK + f"/{number}", payload()) == "full"
        assert journal.enqueue(STRATEGY, LINK + f"/bad/{number}", payload(link="wrong")) == "invalid"
    assert len(list((directory / ".locks").iterdir())) == 1


def test_failed_initial_writes_cannot_grow_lock_files_without_bound(monkeypatch):
    monkeypatch.setattr(journal, "MAX_RECORDS", 2)
    monkeypatch.setattr(journal, "_write", lambda *_args: (_ for _ in ()).throw(OSError("failed initial write")))
    for number in range(2):
        with pytest.raises(OSError, match="failed initial"):
            journal.enqueue(STRATEGY, LINK + f"/{number}", payload())
    assert journal.enqueue(STRATEGY, LINK + "/overflow", payload()) == "full"
    assert len(list((journal.journal_directory() / ".locks").iterdir())) == 2
