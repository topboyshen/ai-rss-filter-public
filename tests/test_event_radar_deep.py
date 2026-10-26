import uuid
from datetime import datetime


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _SequenceConnection:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def execute(self, query, params=()):
        self.calls.append((query, params))
        return _Rows(self.responses.pop(0))


class _ExplainConnection:
    def __init__(self, connection):
        self.connection = connection
        self.plan = []

    def execute(self, query, params=()):
        self.plan = self.connection.execute(
            "EXPLAIN QUERY PLAN " + query, params
        ).fetchall()
        return _Rows([])


def _valid_contract(monkeypatch):
    import services.event_radar_query as radar

    revision_ids = [str(uuid.uuid4()) for _ in range(4)]
    baseline_at = int(datetime.fromisoformat("2026-01-05T15:00:00+08:00").timestamp())
    measured_at = int(datetime.fromisoformat("2026-01-06T15:00:00+08:00").timestamp())
    signal_at = baseline_at - 100
    created_at = measured_at + 1_000
    baseline_date = "2026-01-05"
    measured_date = "2026-01-06"

    monkeypatch.setattr(radar, "baseline_trade_date", lambda _: baseline_date)
    monkeypatch.setattr(
        radar,
        "horizon_trade_date",
        lambda date, offset: (
            measured_date if date == baseline_date and offset == 1 else None
        ),
    )
    monkeypatch.setattr(
        radar,
        "_market_date",
        lambda value: baseline_date if int(value) <= baseline_at + 1 else measured_date,
    )

    row = {
        "security_id": "SSE:600000",
        "method_version": radar.OUTCOME_METHOD_VERSION,
        "horizon": "T+1",
        "benchmark_security_id": radar.MARKET_BENCHMARK_SYMBOL,
        "data_version": radar.OUTCOME_DATA_VERSION_PREFIX + ":".join(revision_ids),
        "signal_available_at": signal_at,
        "baseline_at": baseline_at,
        "measured_at": measured_at,
        "observation_state": "complete",
        "data_quality": 1.0,
        "signal_ledger_id": 9,
        "ledger_available_at": signal_at,
        "ledger_recorded_at": signal_at,
        "frozen_captured_at": signal_at,
        "ledger_selected": 1,
        "ledger_similarity_copy": 0,
        "ledger_score_path": "stage2",
        "ledger_event_version": 1,
        "ledger_event_id": "event-1",
        "event_id": "event-1",
        "ledger_mapping_version": "mapping-v1",
        "mapping_version": "mapping-v1",
        "frozen_mapping_id": "mapping-1",
        "mapping_id": "mapping-1",
        "frozen_mapping_revision": 2,
        "mapping_revision": 2,
        "frozen_available_at": signal_at,
        "mapping_created_at": signal_at - 1,
        "created_at": created_at,
        "baseline_price": 10.0,
        "gross_return": 0.1,
        "benchmark_return": 0.05,
        "excess_return": 0.05,
        "mapping_direction": "bullish",
        "ledger_direction": "bullish",
        "direction_hit": 1,
    }
    revisions = {
        revision_ids[0]: {
            "symbol": "sh600000",
            "trade_date": baseline_date,
            "close_price": 10.0,
            "source_ts": baseline_at,
            "recorded_at": created_at - 10,
        },
        revision_ids[1]: {
            "symbol": "sh600000",
            "trade_date": measured_date,
            "close_price": 11.0,
            "source_ts": measured_at,
            "recorded_at": created_at - 9,
        },
        revision_ids[2]: {
            "symbol": radar.MARKET_BENCHMARK_SYMBOL,
            "trade_date": baseline_date,
            "close_price": 100.0,
            "source_ts": baseline_at + 1,
            "recorded_at": created_at - 8,
        },
        revision_ids[3]: {
            "symbol": radar.MARKET_BENCHMARK_SYMBOL,
            "trade_date": measured_date,
            "close_price": 105.0,
            "source_ts": measured_at + 1,
            "recorded_at": created_at - 7,
        },
    }
    return radar, row, revisions


def test_revision_contract_accepts_exact_frozen_contract(monkeypatch):
    radar, row, revisions = _valid_contract(monkeypatch)

    assert radar._revision_contract_is_valid(row, revisions) is True


def test_revision_contract_rejects_invalid_quality_and_tampered_return(monkeypatch):
    radar, row, revisions = _valid_contract(monkeypatch)

    invalid_quality = dict(row, data_quality=2.0)
    tampered = dict(row, gross_return=0.2)

    assert radar._revision_contract_is_valid(invalid_quality, revisions) is False
    assert radar._revision_contract_is_valid(tampered, revisions) is False


def test_historical_outcome_stats_deduplicates_ids_and_dates(monkeypatch):
    import services.event_radar_query as radar

    candidates = [
        {
            "outcome_id": "outcome-a",
            "data_version": radar.OUTCOME_DATA_VERSION_PREFIX
            + "rev-a:rev-b:rev-c:rev-d",
            "measured_at": 1_700_000_000,
            "direction_hit": 1,
        },
        {
            "outcome_id": "outcome-a",
            "data_version": radar.OUTCOME_DATA_VERSION_PREFIX
            + "rev-a:rev-b:rev-c:rev-d",
            "measured_at": 1_700_000_000,
            "direction_hit": 1,
        },
        {
            "outcome_id": "outcome-b",
            "data_version": radar.OUTCOME_DATA_VERSION_PREFIX
            + "rev-a:rev-b:rev-c:rev-d",
            "measured_at": 1_700_086_400,
            "direction_hit": 0,
        },
    ]
    conn = _SequenceConnection([candidates])
    monkeypatch.setattr(
        radar, "_load_price_revisions", lambda conn, ids: {key: {} for key in ids}
    )
    monkeypatch.setattr(
        radar, "_revision_contract_is_valid", lambda candidate, revisions: True
    )
    monkeypatch.setattr(
        radar,
        "_market_date",
        lambda value: "2026-01-05" if value == 1_700_000_000 else "2026-01-06",
    )
    current = {
        "signal_available_at": 1_800_000_000,
        "security_id": "SSE:600000",
        "horizon": "T+1",
        "method_version": radar.OUTCOME_METHOD_VERSION,
        "benchmark_security_id": radar.MARKET_BENCHMARK_SYMBOL,
        "relation_type": "direct",
        "ledger_strategy_version": "strategy-v1",
        "ledger_mapping_version": "mapping-v1",
    }

    assert radar._historical_outcome_stats(conn, current) == (2, 2, 1)
    assert len(conn.calls) == 1


def test_historical_outcome_stats_without_cutoff_is_empty():
    from services.event_radar_query import _historical_outcome_stats

    conn = _SequenceConnection([])
    assert _historical_outcome_stats(conn, {"signal_available_at": None}) == (0, 0, 0)
    assert conn.calls == []


def test_generated_observation_states_builds_complete_horizons(monkeypatch):
    import services.event_radar_query as radar

    mapping = {
        "ledger_signal_id": 11,
        "signal_event_id": "event-1",
        "event_version": 1,
        "available_at": 100,
        "recorded_at": 100,
        "strategy_version": "strategy-v1",
        "score_path": "stage2",
        "signal_mapping_version": "mapping-v1",
        "frozen_mapping_id": "mapping-1",
        "frozen_event_id": "event-1",
        "frozen_security_id": "SSE:600000",
        "frozen_mapping_revision": 2,
        "frozen_available_at": 100,
        "captured_at": 100,
        "mapping_event_id": "event-1",
        "security_id": "SSE:600000",
        "revision": 2,
        "relation_type": "direct",
        "direction": "bullish",
        "rationale": "直接影响",
        "mapping_version": "mapping-v1",
        "mapping_created_at": 99,
        "mapping_is_current": 1,
        "exchange": "SSE",
        "ticker": "600000",
        "name": "浦发银行",
        "asset_type": "equity",
    }
    baseline = "2026-01-05"
    measured_dates = {
        offset: f"2026-01-{5 + offset:02d}" for offset in radar.OUTCOME_HORIZON_DAYS
    }
    all_dates = {baseline, *measured_dates.values()}
    prices = [
        {"symbol": symbol, "trade_date": date, "close_price": 10.0,
         "source_ts": int(datetime.fromisoformat(date + "T15:00:00+08:00").timestamp())}
        for symbol in ("sh600000", radar.MARKET_BENCHMARK_SYMBOL)
        for date in all_dates
    ]
    conn = _SequenceConnection([[mapping], prices])
    monkeypatch.setattr(radar, "baseline_trade_date", lambda _: baseline)
    monkeypatch.setattr(
        radar, "horizon_trade_date", lambda _, offset: measured_dates[offset]
    )
    timestamps = {date: int(datetime.fromisoformat(date + "T15:00:00+08:00").timestamp()) for date in all_dates}
    monkeypatch.setattr(radar, "_market_close_timestamp", lambda date: timestamps[date])
    monkeypatch.setattr(
        radar,
        "_tradability_for_symbol",
        lambda *args, **kwargs: {
            "tradability_status": "tradable",
            "suspension_status": "not_suspended",
            "limit_status": "normal",
        },
    )

    result = radar._load_generated_observation_states(
        conn, "event-1", read_at=max(timestamps.values()) + 1_000, excluded_series=set()
    )

    assert len(result) == len(radar.OUTCOME_HORIZON_DAYS)
    assert {item["horizon"] for item in result} == {
        f"T+{offset}" for offset in radar.OUTCOME_HORIZON_DAYS
    }
    assert all(
        item["data_status"] == "complete_pending_materialization" for item in result
    )
    assert all(item["materialized"] is False for item in result)


def test_generated_observation_states_rejects_non_exact_snapshot():
    import services.event_radar_query as radar

    invalid = {
        "relation_type": "concept",
        "available_at": 100,
        "recorded_at": 100,
        "captured_at": 100,
    }
    conn = _SequenceConnection([[invalid]])

    assert (
        radar._load_generated_observation_states(
            conn, "event-1", read_at=1_000, excluded_series=set()
        )
        == []
    )


def test_event_page_uses_correlated_equity_filter_for_bounded_work():
    import services.event_radar_query as radar

    conn = _SequenceConnection([[]])
    items, next_cursor, has_more = radar._load_event_page(
        conn,
        cursor=(900, 7.5, "event-cursor"),
        limit=8,
        has_stocks=True,
        read_at=1_000,
        min_priority=6.0,
        require_non_concept_stock=True,
        excluded_event_types=("rumor", "noise"),
    )

    sql = conn.calls[0][0]
    assert items == []
    assert next_cursor is None
    assert has_more is False
    assert "candidate_group" in sql
    assert "equity_event_group" not in sql
    assert (
        "candidate_group AS grouped "
        " CROSS JOIN event_securities AS mapping "
        " INDEXED BY uq_event_securities_current" in sql
    )
    assert "LIMIT 1" in sql
    assert "public_group" in sql
    assert "any_evidence" in sql
    assert (
        "event_evidence AS any_evidence "
        " INDEXED BY idx_event_evidence_event_time" in sql
    )
    assert (
        "event_evidence AS public_evidence "
        " INDEXED BY idx_event_evidence_event_time" in sql
    )
    assert "OR EXISTS (" in sql
    assert "HAVING" not in sql
    assert conn.calls[0][1] == (
        1_000,
        *radar._NON_CONCEPT_RELATION_SQL_VALUES,
        *radar._PUBLIC_SCORE_PATH_VALUES,
        6.0,
        "rumor",
        "noise",
        900,
        7.5,
        "event-cursor",
        9,
        *radar._PUBLIC_SCORE_PATH_VALUES,
    )


def test_event_page_public_evidence_plan_is_keyed_by_event():
    from db import database
    import services.event_radar_query as radar

    database.init_db()
    with database.get_connection() as conn:
        probe = _ExplainConnection(conn)
        items, next_cursor, has_more = radar._load_event_page(
            probe,
            cursor=None,
            limit=8,
            has_stocks=True,
            read_at=1_000,
            min_priority=6.0,
            require_non_concept_stock=True,
            excluded_event_types=("noise",),
        )

    details = [str(row[3]) for row in probe.plan]
    assert items == []
    assert next_cursor is None
    assert has_more is False
    assert any(
        "SEARCH any_evidence USING INDEX idx_event_evidence_event_time" in detail
        for detail in details
    )
    assert any(
        "SEARCH public_evidence USING INDEX idx_event_evidence_event_time" in detail
        for detail in details
    )
    assert any(
        "SEARCH mapping USING INDEX uq_event_securities_current (event_id=?)"
        in detail
        for detail in details
    )
    assert not any(
        "SEARCH any_evidence USING INDEX idx_event_evidence_mapping" in detail
        or "SEARCH public_evidence USING INDEX idx_event_evidence_mapping" in detail
        for detail in details
    )
    assert not any(
        "SCAN mapping" in detail
        or ("AUTOMATIC" in detail and "mapping" in detail)
        for detail in details
    )


def test_page_stock_chips_searches_current_mappings_by_event():
    from db import database
    import services.event_radar_query as radar

    database.init_db()
    root_event_id = str(uuid.uuid4())
    with database.get_connection() as conn:
        probe = _ExplainConnection(conn)
        result = radar._load_page_stock_chips(
            probe,
            [root_event_id],
            read_at=1_000,
            evidence_backed_only=True,
        )

    details = [str(row[3]) for row in probe.plan]
    assert result == {root_event_id: []}
    assert any(
        "SEARCH mapping USING INDEX uq_event_securities_current (event_id=?)"
        in detail
        for detail in details
    )
    assert not any("SCAN mapping" in detail for detail in details)


def test_tradability_for_symbol_without_symbol_is_empty(monkeypatch):
    import services.event_radar_query as radar

    monkeypatch.setattr(radar, "tradable_baseline_trade_date", lambda _: "2026-01-05")
    conn = _SequenceConnection([])

    assert (
        radar._tradability_for_symbol(
            conn, symbol="", signal_available_at=100, read_at=200
        )
        == {}
    )
    assert conn.calls == []


def test_tradability_for_symbol_reports_missing_open(monkeypatch):
    import services.event_radar_query as radar

    monkeypatch.setattr(radar, "tradable_baseline_trade_date", lambda _: "2026-01-05")
    conn = _SequenceConnection([[]])

    result = radar._tradability_for_symbol(
        conn, symbol="sh600000", signal_available_at=100, read_at=200
    )

    assert result["tradability_status"] == "missing_open"
    assert result["tradable_baseline_price"] is None
    assert result["tradable_baseline_at"] is not None


def test_tradability_for_symbol_normalizes_valid_revision(monkeypatch):
    import services.event_radar_query as radar

    monkeypatch.setattr(radar, "tradable_baseline_trade_date", lambda _: "2026-01-05")
    conn = _SequenceConnection(
        [
            [
                {
                    "open_price": 10.5,
                    "suspension_state": "not_suspended",
                    "limit_state": "normal",
                    "tradability_state": "tradable",
                }
            ]
        ]
    )

    result = radar._tradability_for_symbol(
        conn, symbol="SH600000", signal_available_at=100, read_at=200
    )

    assert result["tradable_baseline_price"] == 10.5
    assert result["tradability_status"] == "tradable"
    assert conn.calls[0][1][0] == "sh600000"


def test_tradability_for_symbol_rejects_invalid_open_price(monkeypatch):
    import services.event_radar_query as radar

    monkeypatch.setattr(radar, "tradable_baseline_trade_date", lambda _: "2026-01-05")
    conn = _SequenceConnection(
        [
            [
                {
                    "open_price": "not-a-price",
                    "suspension_state": "unknown",
                    "limit_state": "unknown",
                    "tradability_state": "unverified",
                }
            ]
        ]
    )

    result = radar._tradability_for_symbol(
        conn, symbol="sh600000", signal_available_at=100, read_at=200
    )

    assert result["tradable_baseline_price"] is None
    assert result["tradability_status"] == "unverified"
