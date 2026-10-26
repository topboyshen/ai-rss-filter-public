import ast
import hashlib
import json
from pathlib import Path

import pytest

from db import database
from db.event_core import (
    merge_events,
    resolve_or_create_event,
    upsert_event_evidence,
    upsert_event_security,
    upsert_security,
)
from db.provenance_shadow import (
    PROVENANCE_SHADOW_SCHEMA_META_KEY,
    ProvenanceShadowSchemaError,
    init_provenance_shadow_schema,
    verify_provenance_shadow_schema,
)
from services.provenance_shadow import (
    ProvenanceShadowConfigError,
    load_event_provenance_shadow,
    project_provenance_shadow,
    provenance_projection_version,
    resolve_publisher,
)


REGISTRY = {
    "source_provenance": {
        "rules": [
            {
                "publisher_group": "cls",
                "domains": ["cls.cn"],
                "rss_routes": ["/cls"],
                "source_class": "newsroom",
            },
            {
                "publisher_group": "exchange",
                "domains": ["sse.com.cn"],
                "source_class": "official_record",
                "first_party_scopes": ["exchange"],
            },
        ]
    }
}


@pytest.fixture
def conn():
    database.init_db()
    with database.get_connection() as connection:
        yield connection


def _event(connection, key: str, *, now: int = 100) -> str:
    return resolve_or_create_event(
        connection,
        {"test": key},
        title=f"event {key}",
        event_type="policy",
        direction="bullish",
        priority=8,
        occurred_at=now - 10,
        strategy_version="score-contract",
        model_version="model",
        now=now,
    )


def _evidence(
    connection,
    event_id: str,
    key: str,
    *,
    host: str = "cls.cn",
    claim: str = "创新药海外授权交易进入业绩确认窗口期",
    stance: str = "supports",
    snapshot: str = "",
    mapping_id: str | None = None,
    security_id: str | None = None,
    now: int = 200,
) -> str:
    return upsert_event_evidence(
        connection,
        event_id,
        source_uri=f"https://{host}/news/{key}",
        source_name=host,
        claim_key=key,
        claim_text=claim,
        snapshot_hash=hashlib.sha256(snapshot.encode()).hexdigest() if snapshot else "",
        stance=stance,
        mapping_id=mapping_id,
        security_id=security_id,
        announcement_at=now - 5,
        observed_at=now,
        now=now,
    )


def _claim_state(connection, event_id: str, version: str) -> str:
    row = connection.execute(
        "SELECT state FROM event_claim_state_shadow "
        "WHERE root_event_id = ? AND projection_version = ?",
        (event_id, version),
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_schema_is_exact_idempotent_and_marker_guarded(conn):
    init_provenance_shadow_schema(conn)
    verify_provenance_shadow_schema(conn)
    assert conn.execute(
        "SELECT v FROM app_meta WHERE k = ?",
        (PROVENANCE_SHADOW_SCHEMA_META_KEY,),
    ).fetchone()[0] == "1"

    traced = []
    conn.set_trace_callback(traced.append)
    init_provenance_shadow_schema(conn)
    conn.set_trace_callback(None)
    assert not [
        sql
        for sql in traced
        if sql.lstrip().upper().startswith(("CREATE ", "INSERT ", "UPDATE ", "DELETE "))
    ]

    conn.execute("DROP INDEX idx_provenance_shadow_publisher")
    with pytest.raises(ProvenanceShadowSchemaError, match="index"):
        verify_provenance_shadow_schema(conn)


def test_invalid_registry_fails_before_installing_schema(conn):
    with pytest.raises(ProvenanceShadowConfigError):
        provenance_projection_version({"source_provenance": None})
    with pytest.raises(ProvenanceShadowConfigError):
        project_provenance_shadow(
            conn,
            config={
                "rules": [
                    {
                        "publisher_group": "same",
                        "domains": ["a.example"],
                    },
                    {
                        "publisher_group": "different",
                        "domains": ["a.example"],
                    },
                ]
            },
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%provenance_shadow%'"
    ).fetchone()[0] == 0


def test_first_party_scope_json_bound_is_checked_before_schema_install(conn):
    scopes_at_limit = [f"{index:02d}" + "x" * 124 for index in range(31)]
    assert len(
        json.dumps(
            scopes_at_limit,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    ) == 4_000
    allowed = {
        "rules": [
            {
                "publisher_group": "issuer",
                "domains": ["issuer.example"],
                "source_class": "issuer_first_party",
                "first_party_scopes": scopes_at_limit,
            }
        ]
    }
    assert provenance_projection_version(allowed)

    over_limit = {
        "rules": [
            {
                **allowed["rules"][0],
                "first_party_scopes": [scopes_at_limit[0] + "y", *scopes_at_limit[1:]],
            }
        ]
    }
    with pytest.raises(ProvenanceShadowConfigError, match="serialized form"):
        project_provenance_shadow(conn, config=over_limit)
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%provenance_shadow%'"
    ).fetchone()[0] == 0


def test_publisher_resolution_is_offline_specific_and_spoof_safe():
    direct = resolve_publisher("https://news.cls.cn/a", config=REGISTRY)
    routed = resolve_publisher(
        "http://127.0.0.1:1200/cls/telegraph", config=REGISTRY
    )
    feed_routed = resolve_publisher(
        "https://article.example/a",
        "http://rsshub:1200/cls/telegraph",
        config=REGISTRY,
    )
    spoof = resolve_publisher("https://evil.example/cls/telegraph", config=REGISTRY)
    spoofed_feed_host = resolve_publisher(
        "https://article.example/a",
        "https://rsshub.evil.com/cls/telegraph",
        config=REGISTRY,
    )
    spoofed_plain_route = resolve_publisher(
        "https://article.example/a",
        "/cls/telegraph",
        config=REGISTRY,
    )
    suffix = resolve_publisher("https://quote.foo.com.cn/a", config=REGISTRY)

    assert direct["publisher_group"] == "cls"
    assert direct["publisher_basis"] == "registry_domain"
    assert routed["publisher_group"] == "cls"
    assert routed["publisher_basis"] == "registry_route"
    assert feed_routed["publisher_group"] == "cls"
    assert feed_routed["publisher_basis"] == "registry_route"
    assert spoof["publisher_group"] == "domain:evil.example"
    assert spoofed_feed_host["publisher_group"] == "domain:article.example"
    assert spoofed_feed_host["publisher_basis"] == "registrable_domain"
    assert spoofed_plain_route["publisher_group"] == "domain:article.example"
    assert spoofed_plain_route["publisher_basis"] == "registrable_domain"
    assert suffix["publisher_group"] == "domain:foo.com.cn"
    assert provenance_projection_version(REGISTRY) == provenance_projection_version(
        {
            "rules": list(reversed(REGISTRY["source_provenance"]["rules"])),
        }
    )


def test_conservative_claim_states_and_strict_reader(conn):
    multi = _event(conn, "multi")
    _evidence(conn, multi, "m1", host="cls.cn", snapshot="m1")
    _evidence(conn, multi, "m2", host="other.example", snapshot="m2", now=201)

    copied = _event(conn, "copied", now=110)
    _evidence(conn, copied, "c1", host="cls.cn", snapshot="same", now=210)
    _evidence(conn, copied, "c2", host="other.example", snapshot="same", now=211)

    contested = _event(conn, "contested", now=120)
    _evidence(conn, contested, "x1", host="cls.cn", snapshot="x1", now=220)
    _evidence(
        conn,
        contested,
        "x2",
        host="other.example",
        stance="contradicts",
        snapshot="x2",
        now=221,
    )

    unknown = _event(conn, "unknown", now=130)
    _evidence(conn, unknown, "u1", host="127.0.0.1", now=230)
    _evidence(conn, unknown, "u2", host="localhost", now=231)

    result = project_provenance_shadow(conn, config=REGISTRY, now=300)
    assert result["status"] == "ok"
    assert result["complete"] is True
    assert result["processed_roots"] == 4
    version = str(result["projection_version"])
    assert _claim_state(conn, multi, version) == "multi_independent_support"
    assert _claim_state(conn, copied, version) == "single_support"
    assert _claim_state(conn, contested, version) == "contested"
    assert _claim_state(conn, unknown, version) == "single_support"

    aggregate = load_event_provenance_shadow(conn, multi, config=REGISTRY)
    assert aggregate["status"] == "ready"
    assert aggregate["raw_evidence_count"] == 2
    assert aggregate["publisher_group_count"] == 2
    assert aggregate["lineage_unit_count"] == 2
    assert aggregate["claim_state_counts"]["multi_independent_support"] == 1
    assert set(aggregate) == {
        "status", "reason", "projection_version", "root_event_version",
        "raw_evidence_count", "projected_evidence_count", "publisher_group_count",
        "lineage_unit_count", "first_party_evidence_count",
        "official_record_evidence_count", "claim_count", "claim_state_counts",
        "latest_evidence_at", "computed_at",
    }


def test_mapping_bound_evidence_is_excluded_and_core_counts_are_invariant(conn):
    event_id = _event(conn, "mapping")
    _evidence(conn, event_id, "base")
    security_id = upsert_security(
        conn, exchange="SSE", ticker="600000", name="浦发银行", now=200
    )
    mapping_id = upsert_event_security(
        conn,
        event_id,
        security_id,
        relation_type="direct",
        direction="bullish",
        confidence=0.9,
        relevance=9,
        mapping_version="mapping-contract",
        now=201,
    )
    _evidence(
        conn,
        event_id,
        "mapped",
        mapping_id=mapping_id,
        security_id=security_id,
        now=202,
    )
    protected_tables = (
        "events",
        "event_evidence",
        "event_securities",
        "backtest_signals",
        "backtest_signal_securities",
        "news_cache",
    )
    before = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in protected_tables
    }
    project_provenance_shadow(conn, config=REGISTRY, now=300)
    after = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in protected_tables
    }
    assert after == before
    assert load_event_provenance_shadow(conn, event_id, config=REGISTRY)[
        "raw_evidence_count"
    ] == 1


def test_first_party_metadata_cannot_claim_subject_binding(conn):
    config = {
        "rules": [
            {
                "publisher_group": "issuer",
                "domains": ["issuer.example"],
                "source_class": "issuer_first_party",
                "first_party_scopes": ["issuer"],
            }
        ]
    }
    event_id = _event(conn, "unbound-first-party")
    _evidence(conn, event_id, "issuer", host="issuer.example")
    result = project_provenance_shadow(conn, config=config, now=300)
    aggregate = load_event_provenance_shadow(conn, event_id, config=config)
    assert aggregate["status"] == "ready"
    assert aggregate["first_party_evidence_count"] == 0
    assert conn.execute(
        "SELECT first_party_support_count FROM event_claim_state_shadow "
        "WHERE root_event_id = ? AND projection_version = ?",
        (event_id, result["projection_version"]),
    ).fetchone()[0] == 0


def test_projection_is_bounded_idempotent_and_repairs_merge_roots(conn):
    roots = []
    for index in range(3):
        root = _event(conn, f"batch-{index}", now=100 + index)
        _evidence(conn, root, f"batch-{index}", now=200 + index)
        roots.append(root)

    first = project_provenance_shadow(
        conn, config=REGISTRY, evidence_scan_limit=1, max_roots=1, now=300
    )
    assert first["processed_roots"] == 1
    assert first["has_more"] is True
    assert first["complete"] is False
    for now in (301, 302, 303):
        project_provenance_shadow(
            conn, config=REGISTRY, evidence_scan_limit=1, max_roots=1, now=now
        )
    idle = project_provenance_shadow(
        conn, config=REGISTRY, evidence_scan_limit=1, max_roots=1, now=304
    )
    assert idle["processed_roots"] == 0
    assert idle["complete"] is True

    source, target = roots[0], roots[1]
    merge_events(conn, source, target, reason="duplicate", actor="test", now=400)
    repaired = project_provenance_shadow(conn, config=REGISTRY, now=401)
    assert repaired["processed_roots"] == 1
    assert load_event_provenance_shadow(conn, target, config=REGISTRY)["status"] == "ready"
    version = str(repaired["projection_version"])
    assert conn.execute(
        "SELECT COUNT(*) FROM event_claim_state_shadow "
        "WHERE root_event_id = ? AND projection_version = ?",
        (source, version),
    ).fetchone()[0] == 0


def test_deleted_base_evidence_rebuilds_or_clears_shadow(conn):
    event_id = _event(conn, "deleted-base")
    first_id = _evidence(conn, event_id, "delete-first", now=200)
    second_id = _evidence(conn, event_id, "delete-second", now=201)
    initial = project_provenance_shadow(conn, config=REGISTRY, now=300)
    version = str(initial["projection_version"])
    assert load_event_provenance_shadow(conn, event_id, config=REGISTRY)[
        "raw_evidence_count"
    ] == 2

    conn.execute("DELETE FROM event_evidence WHERE evidence_id = ?", (first_id,))
    conn.commit()
    partial = project_provenance_shadow(conn, config=REGISTRY, now=301)
    partial_view = load_event_provenance_shadow(conn, event_id, config=REGISTRY)
    assert partial["complete"] is True
    assert partial["processed_roots"] == 1
    assert partial_view["status"] == "ready"
    assert partial_view["raw_evidence_count"] == 1
    assert partial_view["projected_evidence_count"] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM event_evidence_provenance_shadow "
        "WHERE root_event_id = ? AND projection_version = ?",
        (event_id, version),
    ).fetchone()[0] == 1

    conn.execute("DELETE FROM event_evidence WHERE evidence_id = ?", (second_id,))
    conn.commit()
    cleared = project_provenance_shadow(conn, config=REGISTRY, now=302)
    cleared_view = load_event_provenance_shadow(conn, event_id, config=REGISTRY)
    assert cleared["complete"] is True
    assert cleared["processed_roots"] == 1
    assert (cleared_view["status"], cleared_view["reason"]) == (
        "pending",
        "no_base_evidence",
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM event_evidence_provenance_shadow "
        "WHERE root_event_id = ? AND projection_version = ?",
        (event_id, version),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM event_claim_state_shadow "
        "WHERE root_event_id = ? AND projection_version = ?",
        (event_id, version),
    ).fetchone()[0] == 0


def test_oversized_root_cannot_starve_a_later_bounded_root(conn):
    oversized = _event(conn, "oversized", now=100)
    conn.execute("BEGIN")
    for index in range(2_001):
        _evidence(
            conn,
            oversized,
            f"oversized-{index}",
            host="oversized.example",
            now=200 + index,
        )
    conn.commit()
    bounded = _event(conn, "bounded-after-oversized", now=3_000)
    _evidence(
        conn,
        bounded,
        "bounded-after-oversized",
        host="bounded.example",
        now=3_001,
    )

    traced = []
    conn.set_trace_callback(traced.append)
    try:
        result = project_provenance_shadow(
            conn,
            config=REGISTRY,
            evidence_scan_limit=2,
            max_roots=2,
            max_group_evidence=2_000,
            max_total_evidence=2_000,
            now=4_000,
        )
    finally:
        conn.set_trace_callback(None)

    assert result["status"] == "ok"
    assert result["complete"] is False
    assert result["has_more"] is True
    assert result["deferred_oversized_roots"] == 1
    assert result["processed_roots"] == 1
    assert result["processed_evidence"] == 1
    bounded_base_reads = [
        sql
        for sql in traced
        if "SELECT evidence_id, event_id, source_uri" in sql
        and "FROM event_evidence" in sql
    ]
    assert bounded_base_reads
    assert all("LIMIT 2001" in sql.upper() for sql in bounded_base_reads)
    assert load_event_provenance_shadow(conn, bounded, config=REGISTRY)[
        "status"
    ] == "ready"
    reader_trace = []
    conn.set_trace_callback(reader_trace.append)
    try:
        oversized_state = load_event_provenance_shadow(
            conn, oversized, config=REGISTRY
        )
    finally:
        conn.set_trace_callback(None)
    assert (oversized_state["status"], oversized_state["reason"]) == (
        "pending",
        "input_bound_exceeded",
    )
    reader_base_reads = [
        sql
        for sql in reader_trace
        if "SELECT evidence_id, event_id, source_uri" in sql
        and "FROM event_evidence" in sql
    ]
    assert reader_base_reads
    assert all("LIMIT 2001" in sql.upper() for sql in reader_base_reads)


def test_reader_fails_closed_for_missing_or_incomplete_projection(conn):
    event_id = _event(conn, "pending")
    _evidence(conn, event_id, "pending")
    missing = load_event_provenance_shadow(conn, event_id, config=REGISTRY)
    assert (missing["status"], missing["reason"]) == (
        "pending",
        "schema_unavailable",
    )
    init_provenance_shadow_schema(conn)
    incomplete = load_event_provenance_shadow(conn, event_id, config=REGISTRY)
    assert (incomplete["status"], incomplete["reason"]) == (
        "pending",
        "input_incomplete",
    )


def test_shadow_is_outside_scoring_fingerprint_inputs():
    source = (Path(__file__).resolve().parents[1] / "core" / "config.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    strategy_literals = {
        node.value
        for node in ast.walk(functions["_strategy_fingerprint"])
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    code_literals = {
        node.value
        for node in ast.walk(functions["_strategy_code_fingerprint"])
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "source_provenance" not in strategy_literals
    assert "services/provenance_shadow.py" not in code_literals
