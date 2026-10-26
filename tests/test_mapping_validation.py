from datetime import datetime
from zoneinfo import ZoneInfo

from services.mapping_validation import (
    _render_mapping_contract_ledger,
    render_mapping_validation_compact_html,
    render_mapping_validation_html,
    run_mapping_validation,
)


TZ = ZoneInfo("Asia/Shanghai")
EQUITY_ID = "SSE:688256"
EQUITY_SYMBOL = "sh688256"


def _ts(value: str) -> int:
    return int(datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp())


def _price(symbol: str, trade_date: str, close: float) -> dict:
    return {
        "symbol": symbol,
        "trade_date": trade_date,
        "name": symbol,
        "close_price": close,
        "open_price": close,
        "high_price": close * 1.01,
        "low_price": close * 0.99,
        "prev_close": close,
        "volume": 1000,
        "amount": 10000,
        "suspension_state": "trading",
        "limit_state": "not_at_limit",
        "tradability_state": "trading_ohlc",
        "change_amt": 0,
        "change_pct": 0,
        "ts": _ts(f"{trade_date} 15:00"),
    }


def _seed_equity(database) -> None:
    from db.event_core import upsert_security

    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id=EQUITY_ID,
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            industry="半导体",
            concepts=["AI算力"],
            now=_ts("2026-07-13 09:00"),
        )
        conn.commit()


def _signal(available_at: int, relation: str = "direct", direction: str = "看多") -> dict:
    return {
        "link": f"https://example.com/mapping-{relation}",
        "raw_title": "算力公司订单事件",
        "event_key": f"mapping-{relation}",
        "available_at": available_at,
        "scored_at": available_at,
        "published_at": available_at - 60,
        "score": 9.0,
        "direction": direction,
        "score_confidence": 0.8,
        "strategy_version": "strategy-v1",
        "mapping_version": "asset-map:v1",
        "mapped_theme": "AI算力",
        "backtest_symbol": "sz399006",
        "score_path": "stage2",
        "aggregation_threshold": 7.0,
        "selected": True,
        "security_candidates": [
            {
                "security_id": EQUITY_ID,
                "relation_type": relation,
                "direction": direction,
                "confidence": 0.96,
                "relevance": 10.0,
                "rationale": "原文与标的有明确关系",
            }
        ],
    }


def test_mapping_validation_uses_only_frozen_v5_and_withholds_small_sample(monkeypatch):
    import db.database as database
    from services.event_outcomes import materialize_event_outcomes

    available_at = _ts("2026-07-13 10:00")
    monkeypatch.setattr(database.time, "time", lambda: available_at)
    database.init_db()
    _seed_equity(database)
    assert database.store_backtest_signal(_signal(available_at))
    monkeypatch.setattr(database.time, "time", lambda: _ts("2026-07-14 16:00"))
    database.store_future_prices(
        [
            _price(EQUITY_SYMBOL, "2026-07-13", 100),
            _price(EQUITY_SYMBOL, "2026-07-14", 110),
            _price("sh000300", "2026-07-13", 1000),
            _price("sh000300", "2026-07-14", 1010),
        ]
    )
    assert materialize_event_outcomes("2026-07-14") == {
        "eligible": 1,
        "materialized": 1,
    }

    replay = run_mapping_validation(
        days=90,
        now=_ts("2026-07-14 14:00"),
        current_contract=("strategy-v1", "asset-map:v1"),
    )
    replay_t1 = next(
        row for row in replay["horizons"] if row["horizon"] == "T+1"
    )
    assert replay_t1["due_pairs"] == 0
    assert replay_t1["complete_pairs"] == 0

    result = run_mapping_validation(
        days=90,
        now=_ts("2026-07-14 16:00"),
        current_contract=("strategy-v1", "asset-map:v1"),
    )

    assert result["enabled"] is True
    assert result["status"] == "collecting"
    assert result["selected_signals"] == 1
    assert result["ledger_selected_signals"] == 1
    assert result["ledger_contract_count"] == 1
    assert result["archived_contract_selected_signals"] == 0
    assert result["historical_contracts"] == []
    assert result["frozen_signals"] == 1
    assert result["formal_pairs"] == 1
    assert result["directional_pairs"] == 1
    assert result["concept_pairs_excluded"] == 0
    t1 = next(row for row in result["horizons"] if row["horizon"] == "T+1")
    assert t1["due_pairs"] == 1
    assert t1["complete_pairs"] == 1
    assert t1["directional_complete_pairs"] == 1
    assert t1["publishable"] is False
    assert t1["excess_hit_rate"] is None
    assert t1["mean_signed_excess"] is None
    funnel = result["eligibility_funnel"]
    assert funnel["scope"] == "current_exact_contract"
    assert funnel["signals"] == {
        "selected": 1,
        "valid_frozen_mapping": 1,
        "without_valid_frozen_mapping": 0,
        "formal_mapped": 1,
        "concept_only": 0,
        "without_formal_mapping": 0,
    }
    assert funnel["pairs"] == {
        "formal": 1,
        "directional": 1,
        "evidence_backed": 1,
        "concept_excluded": 0,
    }
    funnel_t1 = next(
        row for row in funnel["outcomes"] if row["horizon"] == "T+1"
    )
    assert funnel_t1["due_pairs"] == 1
    assert funnel_t1["complete_pairs"] == 1
    assert funnel_t1["directional_complete_pairs"] == 1
    html = render_mapping_validation_html(result)
    assert "映射资格漏斗" in html
    assert "信号、映射对与事件结果是不同计量单位" in html


def test_mapping_validation_reports_prior_contracts_without_pooling_them(monkeypatch):
    import db.database as database
    from services.event_outcomes import materialize_event_outcomes

    old_at = _ts("2026-07-13 10:00")
    current_at = _ts("2026-07-14 10:00")
    clock = {"now": old_at}
    monkeypatch.setattr(database.time, "time", lambda: clock["now"])
    database.init_db()
    _seed_equity(database)
    assert database.store_backtest_signal({
        **_signal(old_at),
        "strategy_version": "strategy-v1:old",
        "mapping_version": "asset-map:old",
    })
    clock["now"] = current_at
    assert database.store_backtest_signal({
        **_signal(current_at),
        "strategy_version": "strategy-v1:new",
        "mapping_version": "asset-map:new",
    })
    clock["now"] = _ts("2026-07-14 16:00")
    database.store_future_prices(
        [
            _price(EQUITY_SYMBOL, "2026-07-13", 100),
            _price(EQUITY_SYMBOL, "2026-07-14", 110),
            _price("sh000300", "2026-07-13", 1000),
            _price("sh000300", "2026-07-14", 1010),
        ]
    )
    assert materialize_event_outcomes("2026-07-14") == {
        "eligible": 1,
        "materialized": 1,
    }

    result = run_mapping_validation(
        days=90,
        now=_ts("2026-07-14 16:00"),
        current_contract=("strategy-v1:new", "asset-map:new"),
    )

    assert result["strategy_version"] == "strategy-v1:new"
    assert result["mapping_version"] == "asset-map:new"
    assert result["selected_signals"] == 1
    assert result["ledger_selected_signals"] == 2
    assert result["ledger_contract_count"] == 2
    assert result["archived_contract_selected_signals"] == 1
    assert result["historical_contract_count"] == 1
    assert result["historical_contracts_truncated"] == 0
    assert result["eligibility_funnel"]["scope"] == "current_exact_contract"
    assert result["eligibility_funnel"]["signals"]["selected"] == 1
    old = result["historical_contracts"][0]
    assert old["strategy_version"] == "strategy-v1:old"
    assert old["mapping_version"] == "asset-map:old"
    assert old["reference_only"] is True
    assert old["selected_signals"] == 1
    assert old["formal_pairs"] == 1
    assert old["eligibility_funnel"]["scope"] == "historical_exact_contract"
    assert old["eligibility_funnel"]["signals"]["selected"] == 1
    old_t1 = next(row for row in old["horizons"] if row["horizon"] == "T+1")
    assert old_t1["due_pairs"] == 1
    assert old_t1["complete_pairs"] == 1
    assert old_t1["directional_complete_pairs"] == 1
    assert old_t1["excess_hit_rate"] is None
    current_t1 = next(
        row for row in result["horizons"] if row["horizon"] == "T+1"
    )
    assert current_t1["complete_pairs"] == 0


def test_mapping_validation_does_not_infer_current_contract_from_old_rows(monkeypatch):
    import db.database as database

    old_at = _ts("2026-07-13 10:00")
    monkeypatch.setattr(database.time, "time", lambda: old_at)
    database.init_db()
    _seed_equity(database)
    assert database.store_backtest_signal({
        **_signal(old_at),
        "strategy_version": "strategy-v1:old",
        "mapping_version": "asset-map:old",
    })

    result = run_mapping_validation(
        days=90,
        now=_ts("2026-07-14 16:00"),
        current_contract=("strategy-v1:new", "asset-map:new"),
    )

    assert result["strategy_version"] == "strategy-v1:new"
    assert result["mapping_version"] == "asset-map:new"
    assert result["selected_signals"] == 0
    assert result["formal_pairs"] == 0
    assert result["status"] == "collecting"
    assert result["optimization"]["stage"] == "insufficient"
    assert result["archived_contract_selected_signals"] == 1
    assert [row["strategy_version"] for row in result["historical_contracts"]] == [
        "strategy-v1:old"
    ]


def test_concept_mapping_is_counted_but_excluded_from_formal_validity(monkeypatch):
    import db.database as database

    available_at = _ts("2026-07-13 10:00")
    monkeypatch.setattr(database.time, "time", lambda: available_at)
    database.init_db()
    _seed_equity(database)
    assert database.store_backtest_signal(
        _signal(available_at, relation="concept", direction="中性")
    )

    result = run_mapping_validation(
        days=90,
        now=_ts("2026-07-14 16:00"),
        current_contract=("strategy-v1", "asset-map:v1"),
    )

    assert result["selected_signals"] == 1
    assert result["formal_pairs"] == 0
    assert result["directional_pairs"] == 0
    assert result["concept_pairs_excluded"] == 1
    assert result["eligibility_funnel"]["signals"]["concept_only"] == 1


def test_mapping_funnel_splits_unfrozen_concept_and_formal_signals(monkeypatch):
    import db.database as database

    available_at = _ts("2026-07-13 10:00")
    monkeypatch.setattr(database.time, "time", lambda: available_at)
    database.init_db()
    _seed_equity(database)

    unfrozen = {
        **_signal(available_at),
        "link": "https://example.com/mapping-unfrozen",
        "event_key": "mapping-unfrozen",
        "security_candidates": [],
    }
    assert database.store_backtest_signal(unfrozen)
    assert database.store_backtest_signal(_signal(available_at, relation="concept"))
    assert database.store_backtest_signal(_signal(available_at, relation="direct"))

    result = run_mapping_validation(
        days=90,
        now=_ts("2026-07-14 16:00"),
        current_contract=("strategy-v1", "asset-map:v1"),
    )

    assert result["eligibility_funnel"]["signals"] == {
        "selected": 3,
        "valid_frozen_mapping": 2,
        "without_valid_frozen_mapping": 1,
        "formal_mapped": 1,
        "concept_only": 1,
        "without_formal_mapping": 2,
    }
    html = render_mapping_validation_html(result)
    assert "无有效冻结关系" in html
    assert "仅概念关系" in html


def test_mapping_validation_does_not_backfill_late_evidence_state(monkeypatch):
    import db.database as database

    available_at = _ts("2026-07-13 10:00")
    monkeypatch.setattr(database.time, "time", lambda: available_at)
    database.init_db()
    _seed_equity(database)
    assert database.store_backtest_signal(_signal(available_at))
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM event_evidence WHERE mapping_id IS NOT NULL"
        ).fetchone()[0] >= 1
        conn.execute(
            "UPDATE event_evidence SET stance = 'supports', updated_at = ? "
            "WHERE mapping_id IS NOT NULL",
            (available_at + 600,),
        )
        conn.commit()

    result = run_mapping_validation(
        days=90,
        now=_ts("2026-07-14 16:00"),
        current_contract=("strategy-v1", "asset-map:v1"),
    )

    assert result["formal_pairs"] == 1
    assert result["evidence_backed_pairs"] == 0
    assert result["evidence_coverage"] == 0.0


def test_mapping_validation_html_escapes_contract_and_hides_immature_rate():
    xss = '<svg/onload=alert(1)>'
    result = {
        "enabled": True,
        "status": "collecting",
        "status_label": "数据积累中",
        "started_at": _ts("2026-07-13 10:00"),
        "strategy_version": xss,
        "mapping_version": xss,
        "selected_signals": 1,
        "formal_pairs": 1,
        "directional_pairs": 1,
        "evidence_coverage": 100.0,
        "formal_mapping_coverage": 100.0,
        "unique_events": 1,
        "unique_securities": 1,
        "concept_pairs_excluded": 0,
        "legacy_v4_outcomes_excluded": 337,
        "historical_contracts": [
            {
                "strategy_version": xss,
                "mapping_version": xss,
                "actual_start_date": "2026-07-13",
                "actual_end_date": "2026-07-13",
                "selected_signals": 1,
                "frozen_signals": 1,
                "formal_pairs": 1,
                "directional_pairs": 1,
                "concept_pairs_excluded": 0,
                "reference_only": True,
                "status": "collecting",
                "horizons": [],
            }
        ],
        "relation_rows": [],
        "horizons": [
            {
                "horizon": "T+1",
                "due_pairs": 1,
                "complete_pairs": 1,
                "directional_complete_pairs": 1,
                "independent_events": 1,
                "independent_result_days": 1,
                "publishable": False,
                "excess_hit_rate": 100.0,
                "mean_signed_excess": 99.0,
            }
        ],
    }

    full = render_mapping_validation_html(result)
    compact = render_mapping_validation_compact_html(result)

    assert xss not in full
    assert "&lt;svg/onload=alert(1)&gt;" in full
    assert "100.0% (95%CI" not in full
    assert "暂不展示" in full
    assert "个股映射后验" in compact
    assert "T+1 后验进度：已结算方向样本 1/100" in full
    assert "T+1 后验进度：已结算方向样本 1/100" in compact
    assert "方向映射 1/100" not in full
    assert "已结算证券≥10" in full
    assert "正式映射 / 方向假设" in full
    assert '<details class="mapping-contract-history">' in full
    assert '<details class="mapping-contract-history" open' not in full
    assert "历史契约 1 个 · 旧契约入选 1 条" in full
    history_start = full.index('<details class="mapping-contract-history">')
    assert full.index("当前正式") < history_start
    assert history_start < full.index("历史参考")
    assert ".mapping-validity-note{font-size:11px" in full


def test_mapping_contract_ledger_omits_empty_history_section():
    html = _render_mapping_contract_ledger({
        "strategy_version": "strategy-v1",
        "mapping_version": "asset-map:v1",
        "selected_signals": 3,
        "frozen_signals": 3,
        "formal_pairs": 2,
        "directional_pairs": 2,
        "historical_contracts": [],
        "horizons": [],
    })

    assert "当前正式" in html
    assert '<details class="mapping-contract-history">' not in html


def test_mapping_contract_ledger_discloses_truncated_history():
    html = _render_mapping_contract_ledger({
        "strategy_version": "strategy-v2",
        "mapping_version": "asset-map:v2",
        "selected_signals": 4,
        "frozen_signals": 4,
        "formal_pairs": 3,
        "directional_pairs": 3,
        "historical_contract_count": 10,
        "historical_contracts_truncated": 2,
        "archived_contract_selected_signals": 89,
        "historical_contracts": [
            {
                "strategy_version": "strategy-v1",
                "mapping_version": "asset-map:v1",
                "selected_signals": 7,
                "frozen_signals": 7,
                "formal_pairs": 6,
                "directional_pairs": 5,
                "horizons": [],
            }
        ],
        "horizons": [],
    })

    assert "历史契约 10 个 · 旧契约入选 89 条" in html
    assert "另有 2 个更早契约未载入本页" in html
