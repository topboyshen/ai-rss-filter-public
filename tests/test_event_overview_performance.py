"""Detail reads must stay within the selected event's evidence and mappings."""

import sqlite3
import uuid

import pytest

from db import database
from services import event_radar_query as radar


class _MeasuredConnection:
    def __init__(self, connection):
        self.connection = connection
        self.queries = []

    def execute(self, query, parameters=()):
        self.queries.append((query, parameters))
        return self.connection.execute(query, parameters)

    def plans(self, marker):
        return [
            str(row[3])
            for query, parameters in self.queries
            if marker in query
            for row in self.connection.execute(
                "EXPLAIN QUERY PLAN " + query, parameters
            )
        ]


@pytest.fixture
def detail_connection():
    database.init_db()
    connection = sqlite3.connect(database.DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        for number in (1, 2):
            connection.execute(
                "INSERT INTO events(event_id, title, event_type, direction, "
                "priority, occurred_at, created_at, updated_at, first_seen_at, "
                "last_seen_at, available_at) VALUES (?, ?, 'news', 'bullish', "
                "5, 1, 1, 1, 1, 1, 1)",
                (str(uuid.UUID(int=number)), f"Event {number}"),
            )
        yield _MeasuredConnection(connection)
    finally:
        connection.close()


def _add_evidence(connection, event_number, numbers, *, observed_at=100):
    connection.executemany(
        "INSERT INTO event_evidence(evidence_id, event_id, source_uri, claim_key, "
        "claim_text, stance, observed_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'Selected evidence', 'supports', ?, ?, ?)",
        (
            (
                str(uuid.UUID(int=number)),
                str(uuid.UUID(int=event_number)),
                f"https://example.test/news/{number}",
                f"claim-{number}",
                observed_at,
                observed_at,
                observed_at,
            )
            for number in numbers
        ),
    )


def _add_mappings(connection, event_number, numbers):
    connection.executemany(
        "INSERT INTO securities(security_id, exchange, ticker, name, "
        "created_at, updated_at) VALUES (?, 'SSE', ?, 'Test equity', 1, 1)",
        ((f"SSE:{number:06d}", f"{number:06d}") for number in numbers),
    )
    connection.executemany(
        "INSERT INTO event_securities(mapping_id, event_id, security_id, "
        "relation_type, direction, relevance, confidence, mapping_version, "
        "revision, created_at) VALUES (?, ?, ?, 'direct', 'bullish', 8, 0.9, "
        "'mapping-v1', 1, 100)",
        (
            (
                str(uuid.UUID(int=number)),
                str(uuid.UUID(int=event_number)),
                f"SSE:{number:06d}",
            )
            for number in numbers
        ),
    )


def _measure_work(probe, read):
    blocks = 0

    def progress():
        nonlocal blocks
        blocks += 1
        return int(blocks >= 100_000)

    probe.queries.clear()
    probe.connection.set_progress_handler(progress, 100)
    try:
        result = read()
    finally:
        probe.connection.set_progress_handler(None, 0)
    return result, blocks * 100


def test_sources_work_does_not_grow_with_unrelated_evidence(detail_connection):
    probe = detail_connection
    _add_evidence(probe.connection, 1, [1])
    _add_evidence(probe.connection, 1, [20_002], observed_at=2_000)

    def read():
        return radar._load_sources(probe, str(uuid.UUID(int=1)), read_at=1_000)

    initial, initial_steps = _measure_work(probe, read)
    _add_evidence(probe.connection, 2, range(2, 20_002))
    grown, grown_steps = _measure_work(probe, read)

    assert grown == initial
    sources, total, truncated, summary = grown
    assert total == len(sources) == 1
    assert sources[0]["url"] == "https://example.test/news/1"
    assert not truncated
    assert summary == "Selected evidence"
    plan = probe.plans("deduplicated AS")
    diagnostic = (sqlite3.sqlite_version, initial_steps, grown_steps, plan)
    assert any(
        "SEARCH evidence USING INDEX idx_event_evidence_event_time (event_id=?)" in step
        for step in plan
    ), diagnostic
    assert not any(
        "SEARCH evidence USING INDEX idx_event_evidence_mapping" in step
        or "SCAN evidence" in step
        for step in plan
    ), diagnostic
    assert grown_steps <= initial_steps + 1_000, diagnostic


@pytest.mark.parametrize("has_mapping", [False, True])
def test_stocks_work_does_not_grow_with_unrelated_mappings(
    detail_connection, has_mapping
):
    probe = detail_connection
    if has_mapping:
        _add_mappings(probe.connection, 1, [1])

    def read():
        return radar._load_stocks(probe, str(uuid.UUID(int=1)), read_at=1_000)

    initial, initial_steps = _measure_work(probe, read)
    _add_mappings(probe.connection, 2, range(2, 20_002))
    grown, grown_steps = _measure_work(probe, read)

    assert grown == initial
    assert len(grown) == int(has_mapping)
    if has_mapping:
        assert grown[0]["security_id"] == "SSE:000001"
    plan = probe.plans("mapping.relevance")
    diagnostic = (sqlite3.sqlite_version, initial_steps, grown_steps, plan)
    assert sum(
        "SEARCH mapping USING INDEX uq_event_securities_current (event_id=?" in step
        for step in plan
    ) >= (2 if has_mapping else 1), diagnostic
    assert not any(
        "SCAN mapping" in step or ("AUTOMATIC" in step and "mapping" in step)
        for step in plan
    ), diagnostic
    assert grown_steps <= initial_steps + 1_000, diagnostic
