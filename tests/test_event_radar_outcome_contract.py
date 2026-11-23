"""P4: explain an empty market tab instead of only saying data may be missing."""

import sqlite3
from pathlib import Path

import pytest

from services.event_radar_query import _OUTCOME_CONTRACT_NOTES, _outcome_contract_state

_SCHEMA = """
CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    merged_into TEXT
);
CREATE TABLE backtest_signals (
    id INTEGER PRIMARY KEY,
    event_id TEXT,
    selected INTEGER,
    available_at INTEGER,
    score_path TEXT
);
CREATE TABLE backtest_signal_securities (
    ledger_signal_id INTEGER,
    mapping_id TEXT,
    event_id TEXT,
    security_id TEXT,
    mapping_revision INTEGER
);
CREATE TABLE event_securities (
    mapping_id TEXT,
    event_id TEXT,
    security_id TEXT,
    revision INTEGER,
    relation_type TEXT
);
"""


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO events (event_id, merged_into) VALUES ('event-1', NULL)")
    return conn


def _signal(
    conn: sqlite3.Connection,
    *,
    signal_id: int = 1,
    event_id: str = "event-1",
    selected: int = 1,
    score_path: str = "llm",
    available_at: int = 10,
) -> None:
    conn.execute(
        "INSERT INTO backtest_signals "
        "(id, event_id, selected, available_at, score_path) VALUES (?, ?, ?, ?, ?)",
        (signal_id, event_id, selected, available_at, score_path),
    )


def _frozen(
    conn: sqlite3.Connection,
    *,
    ledger_signal_id: int = 1,
    mapping_id: str = "mapping-1",
    event_id: str = "event-1",
    security_id: str = "security-1",
    revision: int = 1,
) -> None:
    conn.execute(
        "INSERT INTO backtest_signal_securities "
        "(ledger_signal_id, mapping_id, event_id, security_id, mapping_revision) "
        "VALUES (?, ?, ?, ?, ?)",
        (ledger_signal_id, mapping_id, event_id, security_id, revision),
    )


def _mapping(
    conn: sqlite3.Connection,
    *,
    mapping_id: str = "mapping-1",
    event_id: str = "event-1",
    security_id: str = "security-1",
    revision: int = 1,
    relation_type: str = "concept",
) -> None:
    conn.execute(
        "INSERT INTO event_securities "
        "(mapping_id, event_id, security_id, revision, relation_type) VALUES (?, ?, ?, ?, ?)",
        (mapping_id, event_id, security_id, revision, relation_type),
    )


def test_empty_without_a_selected_signal_names_the_reason():
    conn = _connection()
    _signal(conn, selected=0)

    state, note = _outcome_contract_state(conn, "event-1", 100)

    assert state == "no_selected_signal"
    assert note == _OUTCOME_CONTRACT_NOTES["no_selected_signal"]
    assert "不生成结果" in note


@pytest.mark.parametrize("relation_type", ["concept", "benchmark_proxy"])
def test_only_non_direct_frozen_contract_names_no_direct(relation_type):
    conn = _connection()
    _signal(conn)
    _frozen(conn)
    _mapping(conn, relation_type=relation_type)

    state, note = _outcome_contract_state(conn, "event-1", 100)

    assert state == "no_direct_contract"
    assert note == _OUTCOME_CONTRACT_NOTES["no_direct_contract"]


def test_revised_concept_mapping_stays_no_direct_not_mismatch():
    conn = _connection()
    _signal(conn)
    _frozen(conn, mapping_id="mapping-1", revision=1)
    _mapping(conn, mapping_id="mapping-1", revision=1, relation_type="concept")
    _mapping(conn, mapping_id="mapping-2", revision=2, relation_type="concept")

    state, note = _outcome_contract_state(conn, "event-1", 100)

    assert state == "no_direct_contract"
    assert note == _OUTCOME_CONTRACT_NOTES["no_direct_contract"]


def test_missing_frozen_mapping_is_a_contract_mismatch():
    conn = _connection()
    _signal(conn)
    _mapping(conn, mapping_id="mapping-2", revision=2, relation_type="concept")

    state, note = _outcome_contract_state(conn, "event-1", 100)

    assert state == "contract_mismatch"
    assert note == _OUTCOME_CONTRACT_NOTES["contract_mismatch"]
    assert "不估算补齐" in note


def test_direct_frozen_contract_with_empty_result_is_a_mismatch():
    conn = _connection()
    _signal(conn)
    _frozen(conn)
    _mapping(conn, relation_type="direct")

    state, note = _outcome_contract_state(conn, "event-1", 100)

    assert state == "contract_mismatch"
    assert note == _OUTCOME_CONTRACT_NOTES["contract_mismatch"]


def test_non_formal_selected_signal_is_not_a_contract():
    conn = _connection()
    _signal(conn, score_path="similarity")

    state, note = _outcome_contract_state(conn, "event-1", 100)

    assert state == "no_selected_signal"
    assert note == _OUTCOME_CONTRACT_NOTES["no_selected_signal"]


def test_merged_child_signal_is_counted_for_the_root_event():
    conn = _connection()
    conn.execute("INSERT INTO events (event_id, merged_into) VALUES ('event-2', 'event-1')")
    _signal(conn, event_id="event-2")
    _frozen(conn, event_id="event-2")
    _mapping(conn, event_id="event-2")

    state, _note = _outcome_contract_state(conn, "event-1", 100)

    assert state == "no_direct_contract"


def test_template_prefers_the_contract_note_over_the_vague_copy():
    template = (Path(__file__).resolve().parents[1] / "templates" / "event_radar.html").read_text(
        encoding="utf-8"
    )
    assert "outcome_contract_note or" in template
