"""Forward writes and explicit isolated legacy migration keep distinct IDs.

Store generation (event-mapping-store-v7 + semantic manifest) stays fixed across
keyword edits. New writes fold get_mapping_version() into a rules-<hash> segment
so post-change outputs cannot share an id with pre-change v7 ledger rows, while
outdated-root seeding keys same-generation acceptance (generation + live master)
so rules-fingerprint drift is not queued but master fingerprint changes are.
"""

from __future__ import annotations

import hashlib

from db.event_core import (
    record_event_mapping_replay,
    upsert_event_security,
    upsert_security,
)
from services import event_enrichment
from services.event_enrichment import (
    EVENT_MAPPING_CONTRACT_VERSION,
    EVENT_MAPPING_SEMANTIC_FINGERPRINT,
    enrich_event_security_mappings,
)


def _seed_legacy_event(*args, **kwargs):
    """Only explicit isolated migration tests opt into historical inputs."""
    from tests.test_event_enrichment import _seed_event

    return _seed_event(*args, **kwargs, legacy=True)


def _generation_prefix() -> str:
    return (
        f"asset-map:{EVENT_MAPPING_CONTRACT_VERSION}:"
        f"manifest-{EVENT_MAPPING_SEMANTIC_FINGERPRINT}"
    )


def test_enrichment_mapping_version_includes_asset_rules_fingerprint(monkeypatch):
    from db import database

    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        conn.commit()
        before, _ = event_enrichment._mapping_contract_version(conn)

    monkeypatch.setattr(
        event_enrichment,
        "get_mapping_version",
        lambda: "asset-map:ruleschange01",
    )
    with database.get_connection() as conn:
        after, _ = event_enrichment._mapping_contract_version(conn)

    assert before.startswith(_generation_prefix() + ":")
    assert after.startswith(_generation_prefix() + ":")
    assert f":manifest-{EVENT_MAPPING_SEMANTIC_FINGERPRINT}:" in after
    assert ":rules-ruleschange01:" in after
    assert ":master-" in after
    assert after != before
    # Store schema label alone is not a complete written identity.
    assert after != _generation_prefix()
    assert "ruleschange01" in after
    assert EVENT_MAPPING_CONTRACT_VERSION == "event-mapping-store-v7"


def test_same_generation_rules_hash_drift_does_not_seed_outdated_roots(monkeypatch):
    from db import database

    database.init_db()
    monkeypatch.setattr(
        event_enrichment,
        "get_mapping_version",
        lambda: "asset-map:liverules001",
    )

    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        live_contract, _ = event_enrichment._mapping_contract_version(conn, include_legacy=True)
        live_master = live_contract.rsplit(":master-", 1)[1]
        same_gen_event = _seed_legacy_event(
            conn,
            "rules-hash-same-gen",
            "寒武纪发布新一代人工智能算力芯片",
            "https://example.test/rules-hash-same-gen",
            1_000,
        )
        # Pre-change v7 ledger shape: same generation + live master, no rules-.
        upsert_event_security(
            conn,
            same_gen_event,
            "SSE:688256",
            relation_type="direct",
            direction="看多",
            confidence=0.9,
            relevance=10.0,
            rationale="pre-rules-suffix v7 ledger row",
            mapping_version=f"{_generation_prefix()}:master-{live_master}",
            now=1_100,
        )
        cross_gen_event = _seed_legacy_event(
            conn,
            "rules-hash-cross-gen",
            "寒武纪发布第二代人工智能算力芯片",
            "https://example.test/rules-hash-cross-gen",
            1_200,
        )
        upsert_event_security(
            conn,
            cross_gen_event,
            "SSE:688256",
            relation_type="direct",
            direction="看多",
            confidence=0.9,
            relevance=10.0,
            rationale="older generation must still migrate",
            mapping_version="asset-map:event-mapping-store-v6:legacy",
            now=1_300,
        )
        conn.execute("DELETE FROM event_mapping_replay_dirty_roots")
        old_mappings, old_groups = event_enrichment._old_current_mapping_counts(
            conn, live_contract
        )
        seeded_groups, seeded_rows = event_enrichment._seed_outdated_mapping_roots(
            conn,
            live_contract,
            dirty_at=2_000,
            include_legacy=True,
        )
        dirty = {
            str(row[0]): str(row[1])
            for row in conn.execute(
                "SELECT root_event_id, reason FROM event_mapping_replay_dirty_roots"
            ).fetchall()
        }
        conn.commit()

    assert ":rules-liverules001:" in live_contract
    assert live_contract.startswith(_generation_prefix() + ":")
    assert old_mappings == 1
    assert old_groups == 1
    assert seeded_groups == 1
    assert seeded_rows == 1
    assert same_gen_event not in dirty
    assert dirty.get(cross_gen_event) == "mapping_contract_migration"


def test_enrichment_write_stamps_rules_segment_under_v7_generation(monkeypatch):
    from db import database
    from tests.test_event_enrichment import _seed_event

    database.init_db()
    monkeypatch.setattr(
        event_enrichment,
        "get_mapping_version",
        lambda: "asset-map:writestamp01",
    )
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        _seed_event(
            conn,
            "rules-write-stamp",
            "寒武纪发布新一代人工智能算力芯片",
            "https://example.test/rules-write-stamp",
            1_000,
        )
        conn.commit()

    result = enrich_event_security_mappings(since=0, limit=10)
    assert result["failed_evidence"] == 0
    assert ":rules-writestamp01:" in result["mapping_contract"]
    assert (
        f":manifest-{EVENT_MAPPING_SEMANTIC_FINGERPRINT}:"
        in result["mapping_contract"]
    )
    assert f":{EVENT_MAPPING_CONTRACT_VERSION}:" in result["mapping_contract"]
    assert ":writer-title-only-v2:" in result["mapping_contract"]

    with database.get_connection() as conn:
        versions = [
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT mapping_version FROM event_securities "
                "WHERE is_current = 1 AND lower(relation_type) <> 'benchmark_proxy'"
            ).fetchall()
        ]
    assert versions == [result["mapping_contract"]]



def test_same_generation_old_terminals_do_not_repend_when_rules_segment_added(
    monkeypatch,
):
    """All-history pending must accept pre-rules- same-gen terminals.

    Outdated seeding already keys the generation prefix. Pending selection and
    migration-complete previously required full mapping_version equality, so
    adding ``rules-`` under the same v7+manifest falsely bumped pending and
    cleared migration_complete even with zero dirty-queue seed.
    """
    from db import database

    database.init_db()
    monkeypatch.setattr(
        event_enrichment,
        "get_mapping_version",
        lambda: "asset-map:liverules002",
    )

    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        live_contract, _ = event_enrichment._mapping_contract_version(conn, include_legacy=True)
        master_fp = live_contract.rsplit(":master-", 1)[1]
        old_terminal = f"{_generation_prefix()}:master-{master_fp}"
        assert ":rules-" in live_contract
        assert ":rules-" not in old_terminal
        assert old_terminal.startswith(_generation_prefix() + ":")

        event_ids = []
        for index in range(2):
            event_id = _seed_legacy_event(
                conn,
                f"same-gen-terminal-{index}",
                f"寒武纪发布第{index + 1}代人工智能算力芯片",
                f"https://example.test/same-gen-terminal-{index}",
                1_000 + index,
            )
            evidence = conn.execute(
                "SELECT evidence_id, version FROM event_evidence WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            root_version = conn.execute(
                "SELECT version FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()[0]
            record_event_mapping_replay(
                conn,
                evidence[0],
                old_terminal,
                evidence_version=int(evidence[1]),
                root_event_id=event_id,
                root_event_version=int(root_version),
                input_fingerprint=hashlib.sha256(
                    f"same-gen-input-{index}".encode()
                ).hexdigest(),
                output_fingerprint=hashlib.sha256(
                    f"same-gen-output-{index}".encode()
                ).hexdigest(),
                status="mapped",
                mapped_count=1,
                failure_code="",
                now=2_000,
            )
            event_ids.append(event_id)

        conn.execute("DELETE FROM event_mapping_replay_dirty_roots")
        old_mappings, old_groups = event_enrichment._old_current_mapping_counts(
            conn, live_contract
        )
        pending_roots, exhausted = event_enrichment._pending_mapping_roots(
            conn,
            cutoff=0,
            limit=100,
            mapping_contract=live_contract,
            include_legacy=True,
        )
        status = event_enrichment._event_mapping_migration_status(
            conn,
            mapping_contract=live_contract,
            active_equities=1,
        )
        conn.commit()

    assert old_mappings == 0
    assert old_groups == 0
    assert pending_roots == []
    assert exhausted == set()
    assert status["pending_evidence"] == 0
    assert status["pending_groups"] == 0
    assert status["old_current_mappings"] == 0
    assert status["migration_complete"] is True
    assert set(event_ids).isdisjoint(set(pending_roots))


def test_master_fingerprint_change_still_pends_same_generation_terminals(
    monkeypatch,
):
    """Same-gen acceptance must not wildcard away security-master drift."""
    from db import database

    database.init_db()
    monkeypatch.setattr(
        event_enrichment,
        "get_mapping_version",
        lambda: "asset-map:liverules003",
    )

    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        live_contract, _ = event_enrichment._mapping_contract_version(conn, include_legacy=True)
        live_master = live_contract.rsplit(":master-", 1)[1]
        old_terminal = f"{_generation_prefix()}:master-{live_master}"
        changed_live = live_contract.rsplit(":master-", 1)[0] + ":master-" + ("c" * 64)

        event_id = _seed_legacy_event(
            conn,
            "master-fp-still-pends",
            "寒武纪发布新一代人工智能算力芯片",
            "https://example.test/master-fp-still-pends",
            1_000,
        )
        evidence = conn.execute(
            "SELECT evidence_id, version FROM event_evidence WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        root_version = conn.execute(
            "SELECT version FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0]
        record_event_mapping_replay(
            conn,
            evidence[0],
            old_terminal,
            evidence_version=int(evidence[1]),
            root_event_id=event_id,
            root_event_version=int(root_version),
            input_fingerprint=hashlib.sha256(b"master-pend-input").hexdigest(),
            output_fingerprint=hashlib.sha256(b"master-pend-output").hexdigest(),
            status="mapped",
            mapped_count=1,
            failure_code="",
            now=2_000,
        )
        conn.execute("DELETE FROM event_mapping_replay_dirty_roots")
        pending_roots, _exhausted = event_enrichment._pending_mapping_roots(
            conn,
            cutoff=0,
            limit=100,
            mapping_contract=changed_live,
            include_legacy=True,
        )
        status = event_enrichment._event_mapping_migration_status(
            conn,
            mapping_contract=changed_live,
            active_equities=1,
        )
        conn.commit()

    assert event_id in pending_roots
    assert status["pending_evidence"] == 1
    assert status["migration_complete"] is False


def test_same_generation_failed_exhausted_counts_toward_migration_gate(monkeypatch):
    """Same-gen failed/attempts>=max must not report migration_complete.

    Pending exclusion already accepts same-gen failed terminals, so exhausted
    counting must use the same predicate; exact mapping_version equality left a
    hole where pending=0 and exhausted=0 falsely completed the gate.
    """
    from db import database

    database.init_db()
    monkeypatch.setattr(
        event_enrichment,
        "get_mapping_version",
        lambda: "asset-map:liverules004",
    )

    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        live_contract, _ = event_enrichment._mapping_contract_version(conn, include_legacy=True)
        master_fp = live_contract.rsplit(":master-", 1)[1]
        old_failed = f"{_generation_prefix()}:master-{master_fp}"
        assert ":rules-" in live_contract
        assert ":rules-" not in old_failed

        event_id = _seed_legacy_event(
            conn,
            "same-gen-failed-exhausted",
            "寒武纪发布新一代人工智能算力芯片",
            "https://example.test/same-gen-failed-exhausted",
            1_000,
        )
        evidence = conn.execute(
            "SELECT evidence_id, version FROM event_evidence WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        root_version = conn.execute(
            "SELECT version FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0]
        input_fp = hashlib.sha256(b"same-gen-failed-input").hexdigest()
        output_fp = hashlib.sha256(b"same-gen-failed-output").hexdigest()
        for attempt_now in (2_000, 2_001, 2_002):
            record_event_mapping_replay(
                conn,
                evidence[0],
                old_failed,
                evidence_version=int(evidence[1]),
                root_event_id=event_id,
                root_event_version=int(root_version),
                input_fingerprint=input_fp,
                output_fingerprint=output_fp,
                status="failed",
                mapped_count=0,
                failure_code="ValueError",
                now=attempt_now,
            )

        attempts = conn.execute(
            "SELECT status, attempts FROM event_mapping_replay_ledger "
            "WHERE evidence_id = ?",
            (evidence[0],),
        ).fetchone()
        assert tuple(attempts) == ("failed", 3)

        conn.execute("DELETE FROM event_mapping_replay_dirty_roots")
        pending_roots, exhausted = event_enrichment._pending_mapping_roots(
            conn,
            cutoff=0,
            limit=100,
            mapping_contract=live_contract,
            include_legacy=True,
        )
        status = event_enrichment._event_mapping_migration_status(
            conn,
            mapping_contract=live_contract,
            active_equities=1,
        )
        conn.commit()

    assert event_id not in pending_roots
    assert status["pending_evidence"] == 0
    assert event_id in exhausted
    assert status["exhausted_groups"] == 1
    assert status["migration_complete"] is False


def test_master_fingerprint_change_queues_out_of_window_current_mappings(
    monkeypatch,
):
    """Master drift must dirty-queue out-of-window current mappings.

    All-history pending (cutoff=0) can see master-mismatched terminals, but the
    regular recent-window path only processes durable dirty roots. Same-gen
    acceptance for outdated seeding must therefore treat live-master mismatch as
    outdated so queue inserts are > 0 under a non-zero cutoff.
    """
    from db import database

    database.init_db()
    monkeypatch.setattr(
        event_enrichment,
        "get_mapping_version",
        lambda: "asset-map:liverules005",
    )

    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        live_contract, _ = event_enrichment._mapping_contract_version(conn, include_legacy=True)
        live_master = live_contract.rsplit(":master-", 1)[1]
        # Out-of-window relative to a recent cutoff of 5_000.
        event_id = _seed_legacy_event(
            conn,
            "master-fp-out-of-window",
            "寒武纪发布新一代人工智能算力芯片",
            "https://example.test/master-fp-out-of-window",
            1_000,
        )
        old_mapping_version = f"{_generation_prefix()}:master-{live_master}"
        upsert_event_security(
            conn,
            event_id,
            "SSE:688256",
            relation_type="direct",
            direction="看多",
            confidence=0.9,
            relevance=10.0,
            rationale="same-gen current mapping under prior master image",
            mapping_version=old_mapping_version,
            now=1_100,
        )
        changed_live = (
            live_contract.rsplit(":master-", 1)[0] + ":master-" + ("c" * 64)
        )
        assert old_mapping_version.startswith(_generation_prefix() + ":")
        assert ":master-" + live_master in old_mapping_version
        assert changed_live.rsplit(":master-", 1)[1] != live_master

        conn.execute("DELETE FROM event_mapping_replay_dirty_roots")
        old_mappings, old_groups = event_enrichment._old_current_mapping_counts(
            conn, changed_live
        )
        seeded_groups, seeded_rows = event_enrichment._seed_outdated_mapping_roots(
            conn,
            changed_live,
            dirty_at=2_000,
            include_legacy=True,
        )
        dirty = {
            str(row[0]): str(row[1])
            for row in conn.execute(
                "SELECT root_event_id, reason FROM event_mapping_replay_dirty_roots"
            ).fetchall()
        }
        # Non-zero cutoff: recent-window scan alone cannot see observed_at=1000.
        recent_roots, _exhausted = event_enrichment._pending_mapping_roots(
            conn,
            cutoff=5_000,
            limit=100,
            mapping_contract=changed_live,
            include_legacy=True,
        )
        status = event_enrichment._event_mapping_migration_status(
            conn,
            mapping_contract=changed_live,
            active_equities=1,
        )
        conn.commit()

    assert old_mappings == 1
    assert old_groups == 1
    assert seeded_groups == 1
    assert seeded_rows == 1
    assert dirty.get(event_id) == "mapping_contract_migration"
    assert event_id in recent_roots
    assert status["old_current_mappings"] == 1
    assert status["dirty_groups"] == 1
    assert status["migration_complete"] is False
