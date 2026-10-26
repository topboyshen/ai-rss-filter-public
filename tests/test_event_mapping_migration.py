import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COORDINATOR = PROJECT_ROOT / "scripts" / "run_event_mapping_migration.py"
IMAGE_REF = "my-ai-filter:2026.08.24.15"
IMAGE_ID = "sha256:" + "b" * 64
OLD_IMAGE_REF = "my-ai-filter:2026.08.23.14"
OLD_IMAGE_ID = "sha256:" + "a" * 64
CONTRACT = "asset-map:event-mapping-store-v5:manifest-test:master-test"
INPUT_SNAPSHOT = "c" * 64


def _audit(**overrides):
    value = {
        "mapping_contract": CONTRACT,
        "active_equities": 5000,
        "security_master_fingerprint": "d" * 64,
        "input_evidence": 100,
        "input_snapshot_sha256": INPUT_SNAPSHOT,
        "old_current_mappings": 2,
        "old_current_groups": 2,
        "pending_evidence": 10,
        "pending_groups": 2,
        "dirty_groups": 2,
        "mapping_dirty_groups": 2,
        "exhausted_groups": 0,
        "migration_complete": False,
        "similarity_dirty_groups": 0,
        "similarity_dirty_snapshot_sha256": "e" * 64,
        "terminal_evidence": 90,
        "terminal_snapshot_sha256": "f" * 64,
        "current_rule_mappings": 20,
        "current_mapping_snapshot_sha256": "1" * 64,
        "merge_history_count": 4,
        "merge_history_snapshot_sha256": "2" * 64,
        "orphan_current_groups": 0,
        "foreign_key_violations": 0,
    }
    value.update(overrides)
    return value


def _mappings(**overrides):
    value = {
        "mapping_contract": CONTRACT,
        "active_equities": 5000,
        "processed_evidence": 10,
        "retired_mappings": 2,
        "failed_evidence": 0,
        "unreplayable_groups": 0,
        "exhausted_groups": 0,
        "deferred_groups": 1,
    }
    value.update(overrides)
    return value


def _graph(**overrides):
    value = {
        "merges_allowed": False,
        "qualified_merges": 0,
        "merged": 0,
        "deferred_merges": 0,
        "suppressed_merges": 0,
        "candidate_pairs": 0,
        "similarities": 0,
        "deleted_similarities": 0,
        "migrated_similarities": 0,
        "reconciled_similarities": 0,
        "graph_dirty_groups": 0,
        "graph_dirty_groups_acked": 0,
        "pending_similarity_writes": 0,
        "pending_similarity_deletes": 0,
        "pending_similarity_migrations": 0,
        "pending_similarity_updates": 0,
        "deferred_similarities": 0,
        "deferred_similarity_deletes": 0,
        "deferred_similarity_migrations": 0,
        "deferred_similarity_updates": 0,
    }
    value.update(overrides)
    return value


def test_migration_audit_status_is_content_free_and_stable_on_empty_db():
    from db import database
    from services.event_enrichment import event_mapping_migration_audit_status

    database.init_db()

    first = event_mapping_migration_audit_status()
    second = event_mapping_migration_audit_status()

    assert first == second
    assert first["input_evidence"] == 0
    assert first["input_snapshot_sha256"] == hashlib.sha256().hexdigest()
    assert first["similarity_dirty_groups"] == 0
    assert first["orphan_current_groups"] == 0
    assert first["foreign_key_violations"] == 0
    assert "claim_text" not in json.dumps(first)
    with database.get_read_only_connection() as conn:
        assert int(conn.execute("PRAGMA query_only").fetchone()[0]) == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO app_meta (k, v) VALUES ('forbidden', '1')")


def test_persistent_gate_blocks_ordinary_mapping_writes_and_allows_exact_token(
    monkeypatch, tmp_path
):
    from db import database

    gate = tmp_path / ".event-mapping-migration-required"
    token = "7" * 64
    gate.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "event_mapping_gate",
                "status": "prepared",
                "generation": "event-mapping-store-v5-provider-attribution-v1",
                "token": token,
                "target_image_ref": IMAGE_REF,
                "target_image_digest": IMAGE_ID,
                "created_at": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    gate.chmod(0o600)
    monkeypatch.setenv("AI_FILTER_MAPPING_MIGRATION_GATE_FILE", str(gate))
    monkeypatch.delenv("AI_FILTER_MAPPING_MIGRATION_BYPASS_TOKEN", raising=False)
    monkeypatch.setattr(
        database,
        "_resolved_real_event_mapping_candidates",
        lambda *_args, **_kwargs: [],
    )

    with pytest.raises(database.EventMappingMigrationGateError):
        database.upsert_real_event_mappings(
            None,
            event_id="unused",
            article_text="unused",
            source_uri="",
            direction="中性",
            mapping_version=CONTRACT,
            candidates=[],
        )

    monkeypatch.setenv("AI_FILTER_MAPPING_MIGRATION_BYPASS_TOKEN", token)
    assert (
        database.upsert_real_event_mappings(
            None,
            event_id="unused",
            article_text="unused",
            source_uri="",
            direction="中性",
            mapping_version=CONTRACT,
            candidates=[],
        )
        == []
    )
    gate.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "event_mapping_gate",
                "status": "complete",
                "generation": "event-mapping-store-v5-provider-attribution-v1",
                "target_image_ref": IMAGE_REF,
                "target_image_digest": IMAGE_ID,
                "manifest_hash": "8" * 64,
                "mapping_contract": CONTRACT,
                "input_snapshot_sha256": INPUT_SNAPSHOT,
                "verification_sha256": "9" * 64,
                "completed_at": 2,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("AI_FILTER_MAPPING_MIGRATION_BYPASS_TOKEN", raising=False)
    database._require_event_mapping_write_authorized(CONTRACT)
    database._require_event_mapping_write_authorized("asset-map:future")


def test_missing_persistent_gate_honors_enforcement(monkeypatch, tmp_path):
    from db import database

    gate = tmp_path / "missing-gate"
    monkeypatch.setenv("AI_FILTER_MAPPING_MIGRATION_GATE_FILE", str(gate))
    monkeypatch.setenv("AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE", "0")
    database._require_event_mapping_write_authorized(CONTRACT)

    monkeypatch.setenv("AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE", "1")
    with pytest.raises(database.EventMappingMigrationGateError):
        database._require_event_mapping_write_authorized(CONTRACT)

    monkeypatch.setenv("AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE", "invalid")
    with pytest.raises(database.EventMappingMigrationGateError):
        database._require_event_mapping_write_authorized(CONTRACT)


def test_persistent_gate_fails_closed_on_symlink(monkeypatch, tmp_path):
    from db import database

    target = tmp_path / "target"
    target.write_text("{}\n", encoding="utf-8")
    gate = tmp_path / "gate"
    gate.symlink_to(target)
    monkeypatch.setenv("AI_FILTER_MAPPING_MIGRATION_GATE_FILE", str(gate))

    with pytest.raises(database.EventMappingMigrationGateError):
        database._require_event_mapping_write_authorized(CONTRACT)

    assert target.read_text(encoding="utf-8") == "{}\n"


def test_bottom_event_mapping_mutation_boundary_cannot_bypass_prepared_gate(
    monkeypatch,
    tmp_path,
):
    from db import database
    from db.event_core import record_event_mapping_replay, upsert_event_security

    gate = tmp_path / ".event-mapping-migration-required"
    gate.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "event_mapping_gate",
                "status": "prepared",
                "generation": "event-mapping-store-v5-provider-attribution-v1",
                "token": "7" * 64,
                "target_image_ref": IMAGE_REF,
                "target_image_digest": IMAGE_ID,
                "created_at": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    gate.chmod(0o600)
    monkeypatch.setenv("AI_FILTER_MAPPING_MIGRATION_GATE_FILE", str(gate))
    monkeypatch.setenv("AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE", "1")
    monkeypatch.setenv(
        "AI_FILTER_MAPPING_MIGRATION_REQUIRED_GENERATION",
        "event-mapping-store-v5-provider-attribution-v1",
    )
    monkeypatch.delenv("AI_FILTER_MAPPING_MIGRATION_BYPASS_TOKEN", raising=False)
    connection = sqlite3.connect(tmp_path / "boundary.db")

    try:
        with pytest.raises(database.EventMappingMigrationGateError):
            upsert_event_security(
                connection,
                "event-1",
                "security-1",
                mapping_version=CONTRACT,
            )
        with pytest.raises(database.EventMappingMigrationGateError):
            record_event_mapping_replay(
                connection,
                "evidence-1",
                CONTRACT,
                evidence_version=1,
                root_event_id="event-1",
                root_event_version=1,
                input_fingerprint="1" * 64,
                output_fingerprint="2" * 64,
                status="no_match",
            )
    finally:
        connection.close()


def test_bottom_mapping_mutations_fail_before_database_access_without_gate_module(
    monkeypatch,
):
    import builtins

    from db.event_core import record_event_mapping_replay, upsert_event_security

    real_import = builtins.__import__

    def gate_unavailable(name, *args, **kwargs):
        if name == "db.event_mapping_gate":
            raise ModuleNotFoundError(
                "isolated verifier has no mapping gate",
                name=name,
            )
        return real_import(name, *args, **kwargs)

    class NoDatabaseAccess:
        def execute(self, *_args, **_kwargs):
            pytest.fail("mapping mutation touched the database before loading its gate")

    monkeypatch.setattr(builtins, "__import__", gate_unavailable)
    connection = NoDatabaseAccess()

    with pytest.raises(ModuleNotFoundError, match="no mapping gate"):
        upsert_event_security(
            connection,
            "event-1",
            "security-1",
            mapping_version=CONTRACT,
        )
    with pytest.raises(ModuleNotFoundError, match="no mapping gate"):
        record_event_mapping_replay(
            connection,
            "evidence-1",
            CONTRACT,
            evidence_version=1,
            root_event_id="event-1",
            root_event_version=1,
            input_fingerprint="1" * 64,
            output_fingerprint="2" * 64,
            status="no_match",
        )


def test_bottom_gate_binds_to_the_connection_database_not_live_environment(
    monkeypatch,
    tmp_path,
):
    from db import database
    from db.event_core import upsert_event_security

    live_data = tmp_path / "live"
    restore_data = tmp_path / "restore"
    live_data.mkdir()
    restore_data.mkdir()
    live_db = live_data / "cache.db"
    restore_db = restore_data / "cache.db"
    sqlite3.connect(live_db).close()
    connection = sqlite3.connect(restore_db)
    live_gate = live_data / ".event-mapping-migration-required"
    live_gate.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "event_mapping_gate",
                "status": "complete",
                "generation": "event-mapping-store-v5-provider-attribution-v1",
                "target_image_ref": IMAGE_REF,
                "target_image_digest": IMAGE_ID,
                "manifest_hash": "8" * 64,
                "mapping_contract": CONTRACT,
                "input_snapshot_sha256": INPUT_SNAPSHOT,
                "verification_sha256": "9" * 64,
                "completed_at": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    live_gate.chmod(0o600)
    monkeypatch.setenv("AI_FILTER_DB_PATH", str(live_db))
    monkeypatch.setenv("AI_FILTER_MAPPING_MIGRATION_GATE_FILE", str(live_gate))
    monkeypatch.setenv("AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE", "1")
    monkeypatch.setenv(
        "AI_FILTER_MAPPING_MIGRATION_REQUIRED_GENERATION",
        "event-mapping-store-v5-provider-attribution-v1",
    )

    try:
        with pytest.raises(
            database.EventMappingMigrationGateError,
            match="has not been activated",
        ):
            upsert_event_security(
                connection,
                "event-1",
                "security-1",
                mapping_version=CONTRACT,
            )
        with pytest.raises(
            database.EventMappingMigrationGateError,
            match="has not been activated",
        ):
            database.retire_stale_rule_event_mappings(
                connection,
                event_id="event-1",
                keep_mapping_ids=[],
                current_mapping_version=CONTRACT,
            )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "writer",
    ("real", "market", "retire"),
)
def test_all_mapping_write_entries_require_exact_generation_and_token(
    monkeypatch,
    tmp_path,
    writer,
):
    from db import database

    generation = "event-mapping-store-v5-provider-attribution-v1"
    token = "7" * 64
    gate = tmp_path / ".event-mapping-migration-required"

    class EmptyConnection:
        def __init__(self):
            self.rows = []

        def execute(self, statement, *_args, **_kwargs):
            if statement == "PRAGMA database_list":
                self.rows = [(0, "main", str(tmp_path / "mapping.db"))]
            else:
                self.rows = []
            return self

        def fetchall(self):
            return self.rows

    connection = EmptyConnection()
    monkeypatch.delenv("AI_FILTER_MAPPING_MIGRATION_GATE_FILE", raising=False)
    monkeypatch.setenv("AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE", "1")
    monkeypatch.setenv(
        "AI_FILTER_MAPPING_MIGRATION_REQUIRED_GENERATION",
        generation,
    )
    monkeypatch.setattr(
        database,
        "_resolved_real_event_mapping_candidates",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(database, "canonical_event_id", lambda _conn, event_id: event_id)

    def invoke():
        if writer == "real":
            return database.upsert_real_event_mappings(
                connection,
                event_id="event-1",
                article_text="",
                source_uri="",
                direction="中性",
                mapping_version=CONTRACT,
                candidates=[],
            )
        if writer == "market":
            return database.upsert_event_market_references(
                connection,
                event_id="event-1",
                source_uri="",
                direction="中性",
                mapping_version=CONTRACT,
                references=[],
            )
        return database.retire_stale_rule_event_mappings(
            connection,
            event_id="event-1",
            keep_mapping_ids=[],
            current_mapping_version=CONTRACT,
        )

    payload = {
        "version": 1,
        "kind": "event_mapping_gate",
        "status": "prepared",
        "generation": generation,
        "token": token,
        "target_image_ref": IMAGE_REF,
        "target_image_digest": IMAGE_ID,
        "created_at": 1,
    }
    gate.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    gate.chmod(0o600)

    monkeypatch.delenv("AI_FILTER_MAPPING_MIGRATION_BYPASS_TOKEN", raising=False)
    with pytest.raises(database.EventMappingMigrationGateError):
        invoke()

    monkeypatch.setenv("AI_FILTER_MAPPING_MIGRATION_BYPASS_TOKEN", token)
    assert invoke() in ([], 0)

    payload["generation"] = "event-mapping-store-v5-wrong"
    gate.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(database.EventMappingMigrationGateError):
        invoke()


@pytest.mark.parametrize(
    ("overrides", "failure_code"),
    (
        ({"active_equities": 0}, "security_master_empty"),
        ({"orphan_current_groups": 1}, "mapping_orphan_groups_present"),
        ({"foreign_key_violations": 1}, "mapping_foreign_key_violation"),
        ({"exhausted_groups": 1}, "mapping_exhausted_groups_present"),
        ({"mapping_contract": "invalid"}, "mapping_contract_invalid"),
        ({"input_snapshot_sha256": "invalid"}, "mapping_input_snapshot_invalid"),
        ({"active_equities": True}, "migration_audit_invalid"),
        ({"input_evidence": "100"}, "migration_audit_invalid"),
        ({"security_master_fingerprint": "invalid"}, "migration_audit_invalid"),
        ({"migration_complete": "false"}, "migration_audit_invalid"),
    ),
)
def test_strict_migration_audit_rejects_invalid_prewrite_evidence(
    monkeypatch,
    capsys,
    overrides,
    failure_code,
):
    import scripts.run_event_enrichment as runner

    monkeypatch.setattr(
        runner.database,
        "init_db",
        lambda: pytest.fail("read-only audit must not initialize the database"),
    )
    monkeypatch.setattr(
        runner,
        "event_mapping_migration_audit_status",
        lambda **kwargs: (
            _audit(**overrides)
            if kwargs == {"read_only": True}
            else pytest.fail("audit must request a read-only connection")
        ),
    )
    monkeypatch.setattr(
        runner,
        "enrich_event_security_mappings",
        lambda **_kwargs: pytest.fail("audit must not start a mapping writer"),
    )
    monkeypatch.setattr(
        runner,
        "resolve_event_graph",
        lambda **_kwargs: pytest.fail("audit must not start graph reconciliation"),
    )
    monkeypatch.setattr(sys, "argv", ["run_event_enrichment.py", "--migration-audit"])

    assert runner.main() == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["migration_phase"] == "audit"
    assert payload["failure_code"] == failure_code
    assert payload["migration_complete"] is False


def test_mapping_batch_reports_expected_progress_without_running_graph(
    monkeypatch, capsys
):
    import scripts.run_event_enrichment as runner

    before = _audit()
    after = _audit(
        old_current_mappings=1,
        old_current_groups=1,
        pending_evidence=5,
        pending_groups=1,
        dirty_groups=1,
        mapping_dirty_groups=1,
        terminal_evidence=95,
        terminal_snapshot_sha256="3" * 64,
        current_mapping_snapshot_sha256="4" * 64,
    )
    audits = iter((before, after))
    monkeypatch.setattr(runner.database, "init_db", lambda: None)
    monkeypatch.setattr(
        runner, "event_mapping_migration_audit_status", lambda: next(audits)
    )
    monkeypatch.setattr(
        runner, "enrich_event_security_mappings", lambda **kwargs: _mappings()
    )
    monkeypatch.setattr(
        runner,
        "resolve_event_graph",
        lambda **_kwargs: pytest.fail("mapping-only mode must not run the graph"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_event_enrichment.py",
            "--migration-mapping-batch",
            "--limit",
            "17",
            "--expected-mapping-contract",
            CONTRACT,
            "--expected-input-snapshot",
            INPUT_SNAPSHOT,
        ],
    )

    assert runner.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["migration_outcome"] == "progress"
    assert payload["point_of_no_return"] is True
    assert payload["mapping_contract"] == CONTRACT


def test_mapping_contract_change_fails_before_replay(monkeypatch, capsys):
    import scripts.run_event_enrichment as runner

    monkeypatch.setattr(runner.database, "init_db", lambda: None)
    monkeypatch.setattr(
        runner, "event_mapping_migration_audit_status", lambda: _audit()
    )
    monkeypatch.setattr(
        runner,
        "enrich_event_security_mappings",
        lambda **_kwargs: pytest.fail("contract mismatch must fail before replay"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_event_enrichment.py",
            "--migration-mapping-batch",
            "--expected-mapping-contract",
            "asset-map:other",
        ],
    )

    assert runner.main() == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["failure_code"] == "mapping_contract_changed"


@pytest.mark.parametrize(
    ("graph", "expected_rc", "expected_outcome", "failure_code"),
    (
        (
            _graph(
                merges_allowed=True,
                qualified_merges=1,
                merged=1,
                candidate_pairs=1,
            ),
            1,
            "progress",
            "",
        ),
        (
            _graph(merges_allowed=True),
            0,
            "complete",
            "",
        ),
        (
            _graph(
                merges_allowed=True,
                qualified_merges=1,
                deferred_merges=1,
            ),
            2,
            "failed",
            "merge_drain_stalled",
        ),
    ),
)
def test_merge_drain_batch_has_strict_bounded_exit_contract(
    monkeypatch,
    capsys,
    graph,
    expected_rc,
    expected_outcome,
    failure_code,
):
    import scripts.run_event_enrichment as runner

    calls = []
    monkeypatch.setattr(runner.database, "init_db", lambda: None)
    monkeypatch.setattr(
        runner,
        "resolve_event_graph",
        lambda **kwargs: calls.append(kwargs) or graph,
    )
    monkeypatch.setattr(
        runner,
        "event_mapping_migration_audit_status",
        lambda **_kwargs: pytest.fail("merge drain must not pin the mapping snapshot"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_event_enrichment.py", "--migration-merge-drain-batch"],
    )

    assert runner.main() == expected_rc
    payload = json.loads(capsys.readouterr().out)
    assert calls == [{"since": 0, "max_pairs": 100_000, "allow_merges": True}]
    assert payload["migration_outcome"] == expected_outcome
    assert payload["point_of_no_return"] is False
    if failure_code:
        assert payload["failure_code"] == failure_code


def test_similarity_batch_disables_merges_and_requires_dirty_queue_zero(
    monkeypatch, capsys
):
    import scripts.run_event_enrichment as runner

    complete = _audit(
        old_current_mappings=0,
        old_current_groups=0,
        pending_evidence=0,
        pending_groups=0,
        dirty_groups=0,
        mapping_dirty_groups=0,
        migration_complete=True,
    )
    before = {**complete, "similarity_dirty_groups": 1}
    after = {
        **complete,
        "similarity_dirty_groups": 0,
        "similarity_dirty_snapshot_sha256": "5" * 64,
    }
    audits = iter((before, after))
    calls = []
    monkeypatch.setattr(runner.database, "init_db", lambda: None)
    monkeypatch.setattr(
        runner, "event_mapping_migration_audit_status", lambda: next(audits)
    )
    monkeypatch.setattr(
        runner,
        "resolve_event_graph",
        lambda **kwargs: calls.append(kwargs) or _graph(),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_event_enrichment.py", "--migration-similarity-batch"],
    )

    assert runner.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert calls == [{"since": 0, "max_pairs": 100_000, "allow_merges": False}]
    assert payload["migration_outcome"] == "complete"
    assert payload["migration_complete"] is True


def test_similarity_batch_rejects_suppressed_merge_candidates(
    monkeypatch, capsys
):
    import scripts.run_event_enrichment as runner

    complete = _audit(
        old_current_mappings=0,
        old_current_groups=0,
        pending_evidence=0,
        pending_groups=0,
        dirty_groups=0,
        mapping_dirty_groups=0,
        migration_complete=True,
        similarity_dirty_groups=2,
    )
    audits = iter((complete, complete))
    monkeypatch.setattr(runner.database, "init_db", lambda: None)
    monkeypatch.setattr(
        runner, "event_mapping_migration_audit_status", lambda: next(audits)
    )
    monkeypatch.setattr(
        runner,
        "resolve_event_graph",
        lambda **_kwargs: _graph(
            deferred_merges=1,
            suppressed_merges=1,
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_event_enrichment.py", "--migration-similarity-batch"],
    )

    assert runner.main() == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["failure_code"] == "similarity_batch_stalled"
    assert payload["migration_complete"] is False


def test_similarity_batch_fails_if_merge_history_changes(monkeypatch, capsys):
    import scripts.run_event_enrichment as runner

    before = _audit(
        old_current_mappings=0,
        pending_evidence=0,
        dirty_groups=0,
        migration_complete=True,
    )
    after = {**before, "merge_history_count": 5}
    audits = iter((before, after))
    monkeypatch.setattr(runner.database, "init_db", lambda: None)
    monkeypatch.setattr(
        runner, "event_mapping_migration_audit_status", lambda: next(audits)
    )
    monkeypatch.setattr(runner, "resolve_event_graph", lambda **_kwargs: _graph())
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_event_enrichment.py", "--migration-similarity-batch"],
    )

    assert runner.main() == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["failure_code"] == "event_merge_history_changed"


def _migration_payload(phase, outcome, *, progress=False, zero_work=True):
    if phase == "merge_drain":
        return {
            "status": "ok",
            "mapping_scope": "all_history",
            "migration_phase": phase,
            "migration_outcome": outcome,
            "migration_complete": outcome == "complete",
            "point_of_no_return": False,
            "graph": _graph(
                merges_allowed=True,
                qualified_merges=1 if progress else 0,
                merged=1 if progress else 0,
                candidate_pairs=1 if progress else 0,
            ),
        }
    migration = _audit(
        old_current_mappings=1 if progress and phase == "mapping" else 0,
        old_current_groups=1 if progress and phase == "mapping" else 0,
        pending_evidence=1 if progress and phase == "mapping" else 0,
        pending_groups=1 if progress and phase == "mapping" else 0,
        dirty_groups=1 if progress and phase == "mapping" else 0,
        mapping_dirty_groups=1 if progress and phase == "mapping" else 0,
        migration_complete=not (progress and phase == "mapping"),
        similarity_dirty_groups=1 if progress and phase == "similarity" else 0,
    )
    payload = {
        "status": "ok",
        "mapping_scope": "all_history",
        "migration_phase": phase,
        "migration_outcome": outcome,
        "migration_complete": phase == "similarity" and outcome == "complete",
        "mapping_contract": CONTRACT,
        "input_snapshot_sha256": INPUT_SNAPSHOT,
        "point_of_no_return": phase != "audit",
        "mapping_migration": migration,
        "similarity_replay": {
            "dirty_groups": 1 if progress and phase == "similarity" else 0,
            "snapshot_sha256": "6" * 64,
        },
    }
    if phase == "audit":
        return payload
    if phase == "mapping":
        payload["mappings"] = _mappings(
            processed_evidence=1 if progress else 0,
            retired_mappings=1 if progress else 0,
            deferred_groups=1 if progress else 0,
        )
    else:
        payload["graph"] = _graph(
            candidate_pairs=1 if not zero_work else 0,
            similarities=1 if not zero_work else 0,
            pending_similarity_writes=1 if progress else 0,
            deferred_similarities=1 if progress else 0,
        )
    return payload


@pytest.fixture
def coordinator_env(tmp_path):
    root = tmp_path / "app"
    (root / "data").mkdir(parents=True)
    registry = tmp_path / "releases"
    manifests = registry / "manifests"
    manifests.mkdir(parents=True)
    manifest_source = (
        b"services:\n"
        b"  ai-filter-api:\n    image: ${IMAGE_REF:?}\n"
        b"  ai-filter-worker:\n    image: ${IMAGE_REF:?}\n"
        b"  event-enrichment:\n    profiles: [\"cron\"]\n"
        b"    image: ${IMAGE_REF:?}\n"
    )
    manifest_hash = hashlib.sha256(manifest_source).hexdigest()
    manifest = manifests / f"{manifest_hash}.compose.yml"
    manifest.write_bytes(manifest_source)
    (registry / "current.state").write_text(
        f"{OLD_IMAGE_REF}\n{OLD_IMAGE_ID}\n{manifest_hash}\n"
        "redis ai-filter-api ai-filter-worker\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    results = tmp_path / "results.jsonl"
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import time

log = pathlib.Path(os.environ["FAKE_DOCKER_LOG"])
with log.open("a", encoding="utf-8") as handle:
    handle.write(f"{os.environ.get('IMAGE_REF', '')}|{' '.join(sys.argv[1:])}\\n")
args = sys.argv[1:]
container_state = pathlib.Path(os.environ["FAKE_MIGRATION_CONTAINER_STATE"])
container_id = "a" * 64
if args[:2] == ["image", "inspect"]:
    print(os.environ["FAKE_IMAGE_ID"] if args[2] == os.environ["FAKE_TARGET_REF"] else os.environ["FAKE_OLD_IMAGE_ID"])
    raise SystemExit(0)
if args and args[0] == "inspect":
    if "{{.Id}}|{{.Image}}|{{.State.Status}}" in args:
        if not container_state.exists():
            raise SystemExit(1)
        value = json.loads(container_state.read_text(encoding="utf-8"))
        print(f"{container_id}|{value['image']}|{value['status']}")
    elif "{{.State.Status}}|{{.Image}}" in args:
        print(f"exited|{os.environ['IMAGE_REF']}")
    elif "{{.State.Health.Status}}|{{.Image}}" in args:
        print(f"healthy|{os.environ['IMAGE_REF']}")
    else:
        print("healthy")
    raise SystemExit(0)
if args and args[0] == "ps":
    if container_state.exists():
        print(container_id)
    raise SystemExit(0)
if args and args[0] in {"stop", "kill"}:
    if os.environ.get("FAKE_MIGRATION_CLEANUP_FAIL") == "1":
        raise SystemExit(1)
    if container_state.exists():
        value = json.loads(container_state.read_text(encoding="utf-8"))
        value["status"] = "exited"
        container_state.write_text(json.dumps(value), encoding="utf-8")
    raise SystemExit(0)
if args and args[0] == "rm":
    if os.environ.get("FAKE_MIGRATION_CLEANUP_FAIL") == "1":
        raise SystemExit(1)
    container_state.unlink(missing_ok=True)
    raise SystemExit(0)
if args and args[0] == "compose" and "config" in args:
    image = os.environ.get("IMAGE_REF", os.environ["FAKE_IMAGE_ID"])
    service = lambda: {
        "image": image,
        "environment": {
            "AI_FILTER_DB_PATH": "/app/data/cache.db",
            "AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE": "1",
            "AI_FILTER_MAPPING_MIGRATION_REQUIRED_GENERATION": "event-mapping-store-v5-provider-attribution-v1",
        },
        "volumes": [{
            "type": "bind",
            "source": str(pathlib.Path(os.environ["AI_FILTER_ROOT"]) / "data"),
            "target": "/app/data",
        }],
    }
    print(json.dumps({"services": {
        "ai-filter-api": service(),
        "ai-filter-worker": service(),
        "event-enrichment": service(),
    }}))
    raise SystemExit(0)
if args and args[0] == "compose" and "stop" in args:
    raise SystemExit(0)
if args and args[0] == "compose" and "up" in args:
    if os.environ.get("FAKE_UP_FAIL") == "1":
        raise SystemExit(87)
    raise SystemExit(0)
if args and args[0] == "compose" and "ps" in args:
    print("fake-container-id")
    raise SystemExit(0)
if args and args[0] == "compose" and "run" in args:
    name_index = args.index("--name") + 1
    container_payload = {
        "name": args[name_index],
        "image": os.environ["IMAGE_REF"],
        "status": "running",
    }
    if os.environ.get("FAKE_RUN_SLEEP") == "1":
        container_state.write_text(json.dumps(container_payload), encoding="utf-8")
        time.sleep(30)
    if os.environ.get("FAKE_LEAVE_MIGRATION_CONTAINER") == "1":
        container_state.write_text(json.dumps(container_payload), encoding="utf-8")
    queue = pathlib.Path(os.environ["FAKE_RESULTS_FILE"])
    lines = queue.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise SystemExit(91)
    item = json.loads(lines[0])
    temporary = queue.with_suffix(".tmp")
    temporary.write_text(
        "\\n".join(lines[1:]) + ("\\n" if len(lines) > 1 else ""),
        encoding="utf-8",
    )
    os.replace(temporary, queue)
    print(json.dumps(item["payload"], sort_keys=True))
    raise SystemExit(int(item["rc"]))
raise SystemExit(90)
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    state = tmp_path / "state" / "event-mapping-migration.json"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "AI_FILTER_ROOT": str(root),
            "RELEASE_REGISTRY_DIR": str(registry),
            "AI_FILTER_MAPPING_MIGRATION_STATE_FILE": str(state),
            "HEALTH_MAINTENANCE_FILE": str(tmp_path / "maintenance"),
            "MAPPING_MIGRATION_RELEASE_LOCK_TIMEOUT_SECONDS": "1",
            "FAKE_DOCKER_LOG": str(docker_log),
            "FAKE_IMAGE_ID": IMAGE_ID,
            "FAKE_OLD_IMAGE_ID": OLD_IMAGE_ID,
            "FAKE_TARGET_REF": IMAGE_REF,
            "FAKE_RESULTS_FILE": str(results),
            "FAKE_MIGRATION_CONTAINER_STATE": str(tmp_path / "migration-container.json"),
        }
    )
    return env, root, registry, manifest, state, docker_log, results


def _queue_results(path, *items):
    path.write_text(
        "".join(
            json.dumps({"rc": rc, "payload": payload}, sort_keys=True) + "\n"
            for rc, payload in items
        ),
        encoding="utf-8",
    )


def _prepend_result(path, rc, payload):
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(
        json.dumps({"rc": rc, "payload": payload}, sort_keys=True)
        + "\n"
        + existing,
        encoding="utf-8",
    )


def _run_coordinator(env, mode="execute", *extra):
    return subprocess.run(
        [sys.executable, str(COORDINATOR), mode, IMAGE_REF, *extra],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def _prepare_coordinator(env):
    prepared = _run_coordinator(env, "prepare")
    assert prepared.returncode == 0, (prepared.stdout, prepared.stderr)
    results = Path(env["FAKE_RESULTS_FILE"])
    queued = results.read_text(encoding="utf-8") if results.exists() else ""
    preflight = (
        json.dumps(
            {"rc": 0, "payload": _migration_payload("merge_drain", "complete")},
            sort_keys=True,
        )
        + "\n"
        + json.dumps(
            {"rc": 0, "payload": _migration_payload("audit", "ready")},
            sort_keys=True,
        )
    )
    results.write_text(preflight + "\n" + queued, encoding="utf-8")
    registry = Path(env["RELEASE_REGISTRY_DIR"])
    current = registry / "current.state"
    values = current.read_text(encoding="utf-8").splitlines()
    current.write_text(
        f"{IMAGE_REF}\n{IMAGE_ID}\n{values[2]}\n{values[3]}\n",
        encoding="utf-8",
    )
    return prepared


def _restore_recorded_source_release(env):
    state = json.loads(
        Path(env["AI_FILTER_MAPPING_MIGRATION_STATE_FILE"]).read_text(
            encoding="utf-8"
        )
    )
    source = state["source_release"]
    current = Path(env["RELEASE_REGISTRY_DIR"]) / "current.state"
    current.write_text(
        f"{source['image_ref']}\n{source['digest']}\n"
        f"{source['manifest_hash']}\n{source['health_services']}\n",
        encoding="utf-8",
    )


def test_coordinator_reaches_zero_work_and_binds_every_batch_to_digest(
    coordinator_env,
):
    env, _root, _registry, manifest, state, docker_log, results = coordinator_env
    _queue_results(
        results,
        (1, _migration_payload("mapping", "progress", progress=True)),
        (0, _migration_payload("mapping", "mapping_complete")),
        (0, _migration_payload("similarity", "complete", zero_work=False)),
        (0, _migration_payload("similarity", "complete", zero_work=True)),
    )
    _prepare_coordinator(env)

    completed = _run_coordinator(env)

    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["status"] == "complete"
    assert saved["batch_count"] == 6
    assert saved["point_of_no_return"] is True
    assert saved["automatic_rollback_allowed"] is False
    assert state.stat().st_mode & 0o777 == 0o600
    lines = docker_log.read_text(encoding="utf-8").splitlines()
    run_lines = [line for line in lines if "|compose " in line and " run " in line]
    assert len(run_lines) == 6
    assert "--migration-merge-drain-batch" in run_lines[0]
    assert "--migration-audit" in run_lines[1]
    assert "--migration-mapping-batch" in run_lines[2]
    assert "--migration-mapping-batch" in run_lines[3]
    assert "--migration-similarity-batch" in run_lines[4]
    assert "--migration-similarity-batch" in run_lines[5]
    assert all(line.startswith(f"{IMAGE_ID}|") for line in run_lines)
    assert all(f"-f {manifest}" in line and "--pull never" in line for line in run_lines)
    assert all("--expected-mapping-contract" not in line for line in run_lines[:2])
    assert all("--expected-mapping-contract" in line for line in run_lines[2:])
    assert all("--expected-input-snapshot" in line for line in run_lines[2:])
    assert "-e AI_FILTER_MAPPING_MIGRATION_BYPASS_TOKEN" not in run_lines[0]
    assert all(
        "-e AI_FILTER_MAPPING_MIGRATION_BYPASS_TOKEN" in line
        for line in run_lines[1:]
    )
    active_gate = Path(env["AI_FILTER_ROOT"]) / "data/.event-mapping-migration-required"
    completed_gate = json.loads(active_gate.read_text(encoding="utf-8"))
    assert completed_gate["status"] == "complete"
    assert (
        completed_gate["generation"]
        == "event-mapping-store-v5-provider-attribution-v1"
    )
    assert "token" not in completed_gate
    assert not Path(env["HEALTH_MAINTENANCE_FILE"]).exists()
    assert '"token"' not in state.read_text(encoding="utf-8")
    assert not results.read_text(encoding="utf-8")


def test_coordinator_drains_merges_before_pinning_audit_and_mapping_ponr(
    coordinator_env,
):
    env, _root, _registry, _manifest, state, docker_log, results = coordinator_env
    _prepare_coordinator(env)
    _queue_results(
        results,
        (1, _migration_payload("merge_drain", "progress", progress=True)),
        (0, _migration_payload("merge_drain", "complete")),
        (0, _migration_payload("audit", "ready")),
        (0, _migration_payload("mapping", "mapping_complete")),
        (0, _migration_payload("similarity", "complete")),
        (0, _migration_payload("similarity", "complete")),
    )

    completed = _run_coordinator(env)

    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["status"] == "complete"
    assert saved["rollback_compatible_graph_write_observed"] is True
    assert saved["merge_drain_merged"] == 1
    assert saved["point_of_no_return"] is True
    assert [item["phase"] for item in saved["history"][:4]] == [
        "merge_drain",
        "merge_drain",
        "audit",
        "mapping",
    ]
    assert all(
        item["point_of_no_return"] is False for item in saved["history"][:3]
    )
    assert saved["history"][3]["point_of_no_return"] is True
    run_lines = [
        line
        for line in docker_log.read_text(encoding="utf-8").splitlines()
        if "|compose " in line and " run " in line
    ]
    assert "--migration-merge-drain-batch" in run_lines[0]
    assert "--migration-merge-drain-batch" in run_lines[1]
    assert "--migration-audit" in run_lines[2]
    assert "--migration-mapping-batch" in run_lines[3]


def test_coordinator_rejects_false_complete_with_suppressed_merge(
    coordinator_env,
):
    env, root, _registry, _manifest, state, _docker_log, results = coordinator_env
    false_complete = _migration_payload("similarity", "complete")
    false_complete["graph"]["deferred_merges"] = 1
    false_complete["graph"]["suppressed_merges"] = 1
    _queue_results(
        results,
        (0, _migration_payload("mapping", "mapping_complete")),
        (0, false_complete),
    )
    _prepare_coordinator(env)

    failed = _run_coordinator(env)

    assert failed.returncode == 20
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["status"] == "failed"
    assert saved["phase"] == "similarity"
    assert saved["failure_code"] == "migration_result_invalid"
    assert saved.get("migration_complete") is not True
    gate = json.loads(
        (root / "data/.event-mapping-migration-required").read_text(
            encoding="utf-8"
        )
    )
    assert gate["status"] == "prepared"
    assert Path(env["HEALTH_MAINTENANCE_FILE"]).exists()
    assert not results.read_text(encoding="utf-8")


def test_coordinator_budget_exit_is_resumable_without_deploy(coordinator_env):
    env, _root, _registry, _manifest, state, docker_log, results = coordinator_env
    _queue_results(
        results,
        (1, _migration_payload("mapping", "progress", progress=True)),
    )
    _prepare_coordinator(env)

    bounded = _run_coordinator(env, "execute", "--max-batches", "3")

    assert bounded.returncode == 10, bounded.stdout
    first_state = json.loads(state.read_text(encoding="utf-8"))
    assert first_state["status"] == "progress"
    assert first_state["phase"] == "mapping"
    assert first_state["failure_code"] == "migration_batch_budget_exhausted"
    assert first_state["writers_stopped"] is True
    assert (Path(env["AI_FILTER_ROOT"]) / "data/.event-mapping-migration-required").exists()
    assert Path(env["HEALTH_MAINTENANCE_FILE"]).exists()
    before_resume_log = docker_log.read_text(encoding="utf-8")
    assert " up " not in before_resume_log
    preserved = state.read_bytes()
    duplicate = _run_coordinator(env, "execute")
    assert duplicate.returncode == 2
    assert state.read_bytes() == preserved
    _queue_results(
        results,
        (0, _migration_payload("mapping", "mapping_complete")),
        (0, _migration_payload("similarity", "complete")),
        (0, _migration_payload("similarity", "complete")),
    )

    resumed = _run_coordinator(env, "resume")

    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["status"] == "complete"
    assert saved["batch_count"] == 6
    assert saved["writers_stopped"] is False
    assert docker_log.read_text(encoding="utf-8").count(" up ") == 1
    assert not any("release.sh" in line for line in docker_log.read_text().splitlines())


def test_coordinator_treats_degraded_exit_one_as_failure_without_retry(
    coordinator_env,
):
    env, root, _registry, _manifest, state, docker_log, results = coordinator_env
    degraded = _migration_payload("mapping", "progress", progress=True)
    degraded["status"] = "degraded"
    degraded["mappings"]["failed_evidence"] = 1
    _queue_results(
        results,
        (1, degraded),
        (0, _migration_payload("mapping", "mapping_complete")),
    )
    _prepare_coordinator(env)

    failed = _run_coordinator(env)

    assert failed.returncode == 20
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["status"] == "failed"
    assert saved["automatic_rollback_allowed"] is False
    assert saved["writers_stopped"] is True
    assert saved["last_attempt"]["child_exit_code"] == 1
    assert re.fullmatch(r"[0-9a-f]{64}", saved["last_attempt"]["result_sha256"])
    assert len(results.read_text(encoding="utf-8").splitlines()) == 1
    gate = json.loads(
        (root / "data/.event-mapping-migration-required").read_text(
            encoding="utf-8"
        )
    )
    maintenance = json.loads(
        Path(env["HEALTH_MAINTENANCE_FILE"]).read_text(encoding="utf-8")
    )
    assert gate["status"] == "prepared"
    assert gate["token"] == maintenance["token"]
    assert " up " not in docker_log.read_text(encoding="utf-8")


def test_coordinator_refuses_busy_enrichment_lock_before_docker(coordinator_env):
    env, _root, registry, _manifest, state, docker_log, results = coordinator_env
    _queue_results(results, (0, _migration_payload("mapping", "mapping_complete")))
    _prepare_coordinator(env)
    docker_before = docker_log.read_bytes()
    lock_path = registry / "event-enrichment.lock"
    lock_path.touch(mode=0o600)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        busy = _run_coordinator(env)

    assert busy.returncode == 75
    assert "failure_code=migration_lock_busy" in busy.stdout
    assert json.loads(state.read_text(encoding="utf-8"))["status"] == "prepared"
    assert docker_log.read_bytes() == docker_before


def test_coordinator_refuses_state_symlink_without_touching_target(coordinator_env):
    env, _root, _registry, _manifest, state, docker_log, _results = coordinator_env
    target = state.parent / "unrelated.json"
    state.parent.mkdir()
    target.write_text("unchanged\n", encoding="utf-8")
    state.symlink_to(target)

    refused = _run_coordinator(env, "prepare")

    assert refused.returncode == 78
    assert target.read_text(encoding="utf-8") == "unchanged\n"
    assert not docker_log.exists()


def test_coordinator_stops_when_contract_changes_between_batches(coordinator_env):
    env, _root, _registry, _manifest, state, _docker_log, results = coordinator_env
    changed = _migration_payload("mapping", "mapping_complete")
    changed["mapping_contract"] = "asset-map:changed"
    changed["mapping_migration"]["mapping_contract"] = "asset-map:changed"
    _queue_results(
        results,
        (1, _migration_payload("mapping", "progress", progress=True)),
        (0, changed),
    )
    _prepare_coordinator(env)

    failed = _run_coordinator(env)

    assert failed.returncode == 20
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["status"] == "failed"
    assert saved["failure_code"] == "mapping_contract_changed"


def test_coordinator_does_not_overwrite_scheduled_enrichment_heartbeat(
    coordinator_env,
):
    env, _root, _registry, _manifest, _state, _docker_log, results = coordinator_env
    scheduled = Path(env["AI_FILTER_MAPPING_MIGRATION_STATE_FILE"]).with_name(
        "event-enrichment.json"
    )
    scheduled.parent.mkdir()
    scheduled.write_text('{"status":"ok"}\n', encoding="utf-8")
    scheduled.chmod(0o600)
    original = scheduled.read_bytes()
    _queue_results(
        results,
        (0, _migration_payload("mapping", "mapping_complete")),
        (0, _migration_payload("similarity", "complete")),
        (0, _migration_payload("similarity", "complete")),
    )
    _prepare_coordinator(env)

    result = _run_coordinator(env)

    assert result.returncode == 0
    assert scheduled.read_bytes() == original


def test_activation_resume_seals_prepared_gate_without_rerunning_batches(
    coordinator_env,
):
    env, root, _registry, _manifest, state, docker_log, results = coordinator_env
    _queue_results(
        results,
        (0, _migration_payload("mapping", "mapping_complete")),
        (0, _migration_payload("similarity", "complete")),
        (0, _migration_payload("similarity", "complete")),
    )
    _prepare_coordinator(env)
    env["FAKE_UP_FAIL"] = "1"
    failed = _run_coordinator(env)
    assert failed.returncode == 20
    verified = json.loads(state.read_text(encoding="utf-8"))
    assert verified["status"] == "activation_pending"
    assert verified["phase"] == "complete"
    assert verified["history"][-1]["coordinator_phase"] == "verify"
    env.pop("FAKE_UP_FAIL")
    run_count = docker_log.read_text(encoding="utf-8").count(" run ")

    resumed = _run_coordinator(env, "resume")

    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    completed = json.loads(state.read_text(encoding="utf-8"))
    assert completed["status"] == "complete"
    assert completed["writers_stopped"] is False
    assert docker_log.read_text(encoding="utf-8").count(" run ") == run_count
    gate = json.loads(
        (root / "data/.event-mapping-migration-required").read_text(
            encoding="utf-8"
        )
    )
    assert gate["status"] == "complete"
    assert "token" not in gate
    assert not Path(env["HEALTH_MAINTENANCE_FILE"]).exists()

    completed["status"] = "restoring_writers"
    state.write_text(json.dumps(completed, sort_keys=True) + "\n", encoding="utf-8")
    state.chmod(0o600)
    resumed_again = _run_coordinator(env, "resume")
    assert resumed_again.returncode == 0
    assert json.loads(state.read_text(encoding="utf-8"))["status"] == "complete"
    assert docker_log.read_text(encoding="utf-8").count(" run ") == run_count


def test_activation_failure_with_complete_gate_is_resumable_without_replay(
    coordinator_env,
):
    env, root, _registry, _manifest, state, docker_log, results = coordinator_env
    _queue_results(
        results,
        (0, _migration_payload("mapping", "mapping_complete")),
        (0, _migration_payload("similarity", "complete", zero_work=False)),
        (0, _migration_payload("similarity", "complete", zero_work=True)),
    )
    _prepare_coordinator(env)
    env["FAKE_UP_FAIL"] = "1"

    failed = _run_coordinator(env)

    assert failed.returncode == 20
    pending = json.loads(state.read_text(encoding="utf-8"))
    assert pending["status"] == "activation_pending"
    assert pending["phase"] == "complete"
    assert pending["migration_complete"] is True
    assert pending["writers_stopped"] is True
    gate_path = root / "data/.event-mapping-migration-required"
    gate_before = gate_path.read_bytes()
    assert json.loads(gate_before)["status"] == "complete"
    assert Path(env["HEALTH_MAINTENANCE_FILE"]).exists()
    run_count = docker_log.read_text(encoding="utf-8").count(" run ")

    env.pop("FAKE_UP_FAIL")
    resumed = _run_coordinator(env, "resume")

    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    assert json.loads(state.read_text(encoding="utf-8"))["status"] == "complete"
    assert gate_path.read_bytes() == gate_before
    assert not Path(env["HEALTH_MAINTENANCE_FILE"]).exists()
    assert docker_log.read_text(encoding="utf-8").count(" run ") == run_count


def test_resume_retries_valid_nonzero_outer_verify_completion(coordinator_env):
    env, _root, _registry, _manifest, state, _docker_log, results = coordinator_env
    _queue_results(
        results,
        (0, _migration_payload("mapping", "mapping_complete")),
        (0, _migration_payload("similarity", "complete")),
        (0, _migration_payload("similarity", "complete", zero_work=False)),
    )
    _prepare_coordinator(env)

    failed = _run_coordinator(env)

    assert failed.returncode == 20
    failed_state = json.loads(state.read_text(encoding="utf-8"))
    assert failed_state["status"] == "failed"
    assert failed_state["phase"] == "verify"
    assert (
        failed_state["failure_code"]
        == "migration_zero_work_verification_failed"
    )
    assert failed_state["history"][-1]["coordinator_phase"] == "verify"
    assert failed_state["history"][-1]["outcome"] == "complete"
    assert failed_state["last_result"]["graph"]["candidate_pairs"] == 1
    _queue_results(
        results,
        (0, _migration_payload("similarity", "complete", zero_work=True)),
    )

    resumed = _run_coordinator(env, "resume")

    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    completed = json.loads(state.read_text(encoding="utf-8"))
    assert completed["status"] == "complete"
    assert completed["history"][-1]["coordinator_phase"] == "verify"
    assert completed["history"][-1]["graph"]["candidate_pairs"] == 0


def test_abort_prepared_attempt_then_archives_it_before_reprepare(coordinator_env):
    env, root, _registry, _manifest, state, _docker_log, _results = coordinator_env
    prepared = _run_coordinator(env, "prepare")
    assert prepared.returncode == 0

    aborted = _run_coordinator(env, "abort")

    assert aborted.returncode == 0, (aborted.stdout, aborted.stderr)
    aborted_state = json.loads(state.read_text(encoding="utf-8"))
    assert aborted_state["status"] == "aborted"
    assert aborted_state["automatic_rollback_allowed"] is True
    assert not (root / "data/.event-mapping-migration-required").exists()
    assert not Path(env["HEALTH_MAINTENANCE_FILE"]).exists()

    prepared_again = _run_coordinator(env, "prepare")

    assert prepared_again.returncode == 0, (
        prepared_again.stdout,
        prepared_again.stderr,
    )
    assert json.loads(state.read_text(encoding="utf-8"))["status"] == "prepared"
    archives = list(state.parent.glob(f"{state.stem}.aborted.*{state.suffix}"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text(encoding="utf-8"))["status"] == "aborted"
    assert archives[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "damage",
    (
        "missing_write_attempted",
        "non_boolean_write_attempted",
        "missing_rollback_boundary",
        "false_rollback_boundary",
        "boolean_version",
        "non_string_status",
        "forged_history",
        "forged_phase",
    ),
)
def test_abort_rejects_tampered_prepared_state_fail_closed(
    coordinator_env,
    damage,
):
    env, root, _registry, _manifest, state, docker_log, _results = coordinator_env
    assert _run_coordinator(env, "prepare").returncode == 0
    damaged = json.loads(state.read_text(encoding="utf-8"))
    if damage == "missing_write_attempted":
        damaged.pop("write_attempted")
    elif damage == "non_boolean_write_attempted":
        damaged["write_attempted"] = 0
    elif damage == "missing_rollback_boundary":
        damaged.pop("automatic_rollback_allowed")
    elif damage == "false_rollback_boundary":
        damaged["automatic_rollback_allowed"] = False
    elif damage == "boolean_version":
        damaged["version"] = True
    elif damage == "non_string_status":
        damaged["status"] = {}
    elif damage == "forged_history":
        damaged["history"] = [
            {
                "batch": 1,
                "coordinator_phase": "mapping",
                "point_of_no_return": False,
            }
        ]
        damaged["batch_count"] = 1
    else:
        damaged["phase"] = "mapping"
    state.write_text(json.dumps(damaged, sort_keys=True) + "\n", encoding="utf-8")
    state.chmod(0o600)
    state_before = state.read_bytes()
    gate = root / "data/.event-mapping-migration-required"
    gate_before = gate.read_bytes()
    maintenance = Path(env["HEALTH_MAINTENANCE_FILE"])
    maintenance_before = maintenance.read_bytes()
    docker_before = docker_log.read_bytes()

    refused = _run_coordinator(env, "abort")

    assert refused.returncode == 2
    assert "failure_code=migration_abort_state_invalid" in refused.stdout
    assert state.read_bytes() == state_before
    assert gate.read_bytes() == gate_before
    assert maintenance.read_bytes() == maintenance_before
    assert docker_log.read_bytes() == docker_before


@pytest.mark.parametrize(
    "damage",
    (
        "missing_write_attempted",
        "false_rollback_boundary",
        "boolean_version",
        "non_string_status",
        "forged_history",
    ),
)
def test_execute_rejects_tampered_prepared_state_fail_closed(
    coordinator_env,
    damage,
):
    env, _root, _registry, _manifest, state, docker_log, _results = coordinator_env
    assert _run_coordinator(env, "prepare").returncode == 0
    damaged = json.loads(state.read_text(encoding="utf-8"))
    if damage == "missing_write_attempted":
        damaged.pop("write_attempted")
    elif damage == "false_rollback_boundary":
        damaged["automatic_rollback_allowed"] = False
    elif damage == "boolean_version":
        damaged["version"] = True
    elif damage == "non_string_status":
        damaged["status"] = {}
    else:
        damaged["history"] = [
            {
                "batch": 1,
                "coordinator_phase": "mapping",
                "point_of_no_return": False,
            }
        ]
        damaged["batch_count"] = 1
    state.write_text(json.dumps(damaged, sort_keys=True) + "\n", encoding="utf-8")
    state.chmod(0o600)
    state_before = state.read_bytes()
    docker_before = docker_log.read_bytes()

    refused = _run_coordinator(env, "execute")

    assert refused.returncode == 2
    assert "failure_code=migration_execute_state_invalid" in refused.stdout
    assert state.read_bytes() == state_before
    assert docker_log.read_bytes() == docker_before


def test_abort_rejects_forged_prewrite_history_after_source_restore(
    coordinator_env,
):
    env, root, _registry, _manifest, state, docker_log, _results = coordinator_env
    _prepare_coordinator(env)
    bounded = _run_coordinator(env, "execute", "--max-batches", "2")
    assert bounded.returncode == 10
    damaged = json.loads(state.read_text(encoding="utf-8"))
    assert damaged["phase"] == "mapping"
    assert damaged["point_of_no_return"] is False
    damaged["history"][-1]["outcome"] = "progress"
    state.write_text(json.dumps(damaged, sort_keys=True) + "\n", encoding="utf-8")
    state.chmod(0o600)
    _restore_recorded_source_release(env)
    state_before = state.read_bytes()
    gate = root / "data/.event-mapping-migration-required"
    gate_before = gate.read_bytes()
    maintenance = Path(env["HEALTH_MAINTENANCE_FILE"])
    maintenance_before = maintenance.read_bytes()
    docker_before = docker_log.read_bytes()

    refused = _run_coordinator(env, "abort")

    assert refused.returncode == 2
    assert "failure_code=migration_abort_state_invalid" in refused.stdout
    assert state.read_bytes() == state_before
    assert gate.read_bytes() == gate_before
    assert maintenance.read_bytes() == maintenance_before
    assert docker_log.read_bytes() == docker_before


def test_prepare_rejects_tampered_aborted_state_instead_of_archiving(
    coordinator_env,
):
    env, _root, _registry, _manifest, state, _docker_log, _results = coordinator_env
    assert _run_coordinator(env, "prepare").returncode == 0
    assert _run_coordinator(env, "abort").returncode == 0
    damaged = json.loads(state.read_text(encoding="utf-8"))
    damaged.pop("write_attempted")
    state.write_text(json.dumps(damaged, sort_keys=True) + "\n", encoding="utf-8")
    state.chmod(0o600)
    state_before = state.read_bytes()

    refused = _run_coordinator(env, "prepare")

    assert refused.returncode == 2
    assert "failure_code=migration_generation_already_exists" in refused.stdout
    assert state.read_bytes() == state_before
    assert not list(state.parent.glob(f"{state.stem}.aborted.*{state.suffix}"))


def test_abort_recovers_partial_preparing_intent(coordinator_env):
    env, root, _registry, _manifest, state, _docker_log, _results = coordinator_env
    assert _run_coordinator(env, "prepare").returncode == 0
    preparing = json.loads(state.read_text(encoding="utf-8"))
    preparing["status"] = "preparing"
    state.write_text(json.dumps(preparing, sort_keys=True) + "\n", encoding="utf-8")
    state.chmod(0o600)
    Path(env["HEALTH_MAINTENANCE_FILE"]).unlink()

    aborted = _run_coordinator(env, "abort")

    assert aborted.returncode == 0, (aborted.stdout, aborted.stderr)
    assert json.loads(state.read_text(encoding="utf-8"))["status"] == "aborted"
    assert not (root / "data/.event-mapping-migration-required").exists()


@pytest.mark.parametrize("prewrite_status", ("progress", "running"))
def test_prewrite_progress_can_abort_only_after_exact_source_is_restored(
    coordinator_env,
    prewrite_status,
):
    env, root, _registry, _manifest, state, _docker_log, results = coordinator_env
    _prepare_coordinator(env)
    progress = _run_coordinator(env, "execute", "--max-batches", "1")
    assert progress.returncode == 10
    progress_state = json.loads(state.read_text(encoding="utf-8"))
    assert progress_state["status"] == "progress"
    assert progress_state["point_of_no_return"] is False
    progress_state["status"] = prewrite_status
    state.write_text(json.dumps(progress_state, sort_keys=True) + "\n", encoding="utf-8")
    state.chmod(0o600)
    gate_before = (root / "data/.event-mapping-migration-required").read_bytes()
    maintenance_before = Path(env["HEALTH_MAINTENANCE_FILE"]).read_bytes()

    refused = _run_coordinator(env, "abort")
    assert refused.returncode == 3
    assert (root / "data/.event-mapping-migration-required").read_bytes() == gate_before
    assert Path(env["HEALTH_MAINTENANCE_FILE"]).read_bytes() == maintenance_before

    _restore_recorded_source_release(env)
    aborted = _run_coordinator(env, "abort")

    assert aborted.returncode == 0, (aborted.stdout, aborted.stderr)
    assert json.loads(state.read_text(encoding="utf-8"))["status"] == "aborted"
    remaining = [
        json.loads(line)
        for line in results.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert len(remaining) == 1
    assert remaining[0]["payload"]["migration_phase"] == "audit"


@pytest.mark.parametrize(
    ("field", "invalid"),
    (
        ("rollback_compatible_graph_write_observed", 1),
        ("merge_drain_merged", True),
        ("merge_drain_merged", -1),
    ),
)
def test_resume_rejects_invalid_merge_drain_evidence_state(
    coordinator_env,
    field,
    invalid,
):
    env, _root, _registry, _manifest, state, docker_log, _results = coordinator_env
    _prepare_coordinator(env)
    bounded = _run_coordinator(env, "execute", "--max-batches", "1")
    assert bounded.returncode == 10
    damaged = json.loads(state.read_text(encoding="utf-8"))
    damaged[field] = invalid
    state.write_text(json.dumps(damaged, sort_keys=True) + "\n", encoding="utf-8")
    state.chmod(0o600)
    before_lines = len(docker_log.read_text(encoding="utf-8").splitlines())

    refused = _run_coordinator(env, "resume")

    assert refused.returncode == 2
    assert "failure_code=migration_resume_state_invalid" in refused.stdout
    new_lines = docker_log.read_text(encoding="utf-8").splitlines()[before_lines:]
    assert not any("|compose " in line and " run " in line for line in new_lines)


@pytest.mark.parametrize(
    ("field", "invalid"),
    (
        ("target_digest", OLD_IMAGE_ID),
        ("write_attempted", 0),
        ("write_attempted", True),
    ),
)
def test_resume_rejects_unbound_target_or_write_boundary_state(
    coordinator_env,
    field,
    invalid,
):
    env, _root, _registry, _manifest, state, docker_log, _results = coordinator_env
    _prepare_coordinator(env)
    bounded = _run_coordinator(env, "execute", "--max-batches", "2")
    assert bounded.returncode == 10
    damaged = json.loads(state.read_text(encoding="utf-8"))
    assert damaged["phase"] == "mapping"
    assert damaged["point_of_no_return"] is False
    damaged[field] = invalid
    state.write_text(json.dumps(damaged, sort_keys=True) + "\n", encoding="utf-8")
    state.chmod(0o600)
    before_lines = len(docker_log.read_text(encoding="utf-8").splitlines())

    refused = _run_coordinator(env, "resume")

    assert refused.returncode == 2
    assert "failure_code=migration_resume_state_invalid" in refused.stdout
    new_lines = docker_log.read_text(encoding="utf-8").splitlines()[before_lines:]
    assert not any("|compose " in line and " run " in line for line in new_lines)


@pytest.mark.parametrize(
    ("phase", "max_batches"),
    (("similarity", 3), ("verify", 4), ("complete", 4)),
)
def test_resume_rejects_post_mapping_phase_before_write_boundary(
    coordinator_env,
    phase,
    max_batches,
):
    env, _root, _registry, _manifest, state, docker_log, results = coordinator_env
    _queue_results(
        results,
        (0, _migration_payload("mapping", "mapping_complete")),
        (0, _migration_payload("similarity", "complete")),
    )
    _prepare_coordinator(env)
    bounded = _run_coordinator(
        env,
        "execute",
        "--max-batches",
        str(max_batches),
    )
    assert bounded.returncode == 10
    damaged = json.loads(state.read_text(encoding="utf-8"))
    if phase == "complete":
        assert damaged["phase"] == "verify"
        damaged["phase"] = "complete"
        damaged["status"] = "verified"
        damaged["migration_complete"] = True
    else:
        assert damaged["phase"] == phase
    assert damaged["point_of_no_return"] is True
    damaged["point_of_no_return"] = False
    damaged["write_attempted"] = False
    damaged["automatic_rollback_allowed"] = True
    state.write_text(json.dumps(damaged, sort_keys=True) + "\n", encoding="utf-8")
    state.chmod(0o600)
    before_lines = len(docker_log.read_text(encoding="utf-8").splitlines())

    refused = _run_coordinator(env, "resume")

    assert refused.returncode == 2
    assert "failure_code=migration_resume_state_invalid" in refused.stdout
    new_lines = docker_log.read_text(encoding="utf-8").splitlines()[before_lines:]
    assert not any("|compose " in line and " run " in line for line in new_lines)


@pytest.mark.parametrize("phase", ("merge_drain", "audit"))
def test_resume_rejects_early_phase_after_write_boundary(coordinator_env, phase):
    env, _root, _registry, _manifest, state, docker_log, results = coordinator_env
    _prepare_coordinator(env)
    if phase == "merge_drain":
        _queue_results(
            results,
            (1, _migration_payload("merge_drain", "progress", progress=True)),
        )
    else:
        _queue_results(
            results,
            (0, _migration_payload("merge_drain", "complete")),
        )
    bounded = _run_coordinator(env, "execute", "--max-batches", "1")
    assert bounded.returncode == 10
    damaged = json.loads(state.read_text(encoding="utf-8"))
    assert damaged["phase"] == phase
    assert damaged["point_of_no_return"] is False
    damaged["point_of_no_return"] = True
    damaged["write_attempted"] = True
    damaged["automatic_rollback_allowed"] = False
    state.write_text(json.dumps(damaged, sort_keys=True) + "\n", encoding="utf-8")
    state.chmod(0o600)
    before_lines = len(docker_log.read_text(encoding="utf-8").splitlines())

    refused = _run_coordinator(env, "resume")

    assert refused.returncode == 2
    assert "failure_code=migration_resume_state_invalid" in refused.stdout
    new_lines = docker_log.read_text(encoding="utf-8").splitlines()[before_lines:]
    assert not any("|compose " in line and " run " in line for line in new_lines)


def test_resume_allows_mapping_phase_before_first_write_attempt(coordinator_env):
    env, _root, _registry, _manifest, state, _docker_log, results = coordinator_env
    _prepare_coordinator(env)
    bounded = _run_coordinator(env, "execute", "--max-batches", "2")
    assert bounded.returncode == 10
    prewrite = json.loads(state.read_text(encoding="utf-8"))
    assert prewrite["phase"] == "mapping"
    assert prewrite["point_of_no_return"] is False
    assert prewrite["write_attempted"] is False
    _queue_results(
        results,
        (0, _migration_payload("mapping", "mapping_complete")),
        (0, _migration_payload("similarity", "complete")),
        (0, _migration_payload("similarity", "complete")),
    )

    resumed = _run_coordinator(env, "resume")

    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    assert json.loads(state.read_text(encoding="utf-8"))["status"] == "complete"


@pytest.mark.parametrize(
    "damage",
    ("last_attempt", "last_audit_outcome", "zero_batch_history_prefix"),
)
def test_resume_rejects_forged_mapping_prewrite_boundary(
    coordinator_env,
    damage,
):
    env, _root, _registry, _manifest, state, docker_log, _results = coordinator_env
    _prepare_coordinator(env)
    bounded = _run_coordinator(env, "execute", "--max-batches", "2")
    assert bounded.returncode == 10
    damaged = json.loads(state.read_text(encoding="utf-8"))
    assert damaged["phase"] == "mapping"
    assert damaged["history"][-1]["phase"] == "audit"
    if damage == "last_attempt":
        damaged["last_attempt"]["outcome"] = "progress"
    elif damage == "last_audit_outcome":
        damaged["history"][-1]["outcome"] = "progress"
    else:
        prefix = dict(damaged["history"][0])
        prefix["batch"] = 0
        damaged["history"].insert(0, prefix)
    state.write_text(json.dumps(damaged, sort_keys=True) + "\n", encoding="utf-8")
    state.chmod(0o600)
    before_lines = len(docker_log.read_text(encoding="utf-8").splitlines())

    refused = _run_coordinator(env, "resume")

    assert refused.returncode == 2
    assert "failure_code=migration_resume_state_invalid" in refused.stdout
    new_lines = docker_log.read_text(encoding="utf-8").splitlines()[before_lines:]
    assert not any("|compose " in line and " run " in line for line in new_lines)


def test_resume_allows_mapping_prewrite_after_audit_classification_retry(
    coordinator_env,
):
    env, _root, _registry, _manifest, state, _docker_log, results = coordinator_env
    _prepare_coordinator(env)
    invalid_audit = _migration_payload("audit", "not_ready")
    _queue_results(
        results,
        (0, _migration_payload("merge_drain", "complete")),
        (0, invalid_audit),
    )
    failed = _run_coordinator(env, "execute", "--max-batches", "2")
    assert failed.returncode == 20
    failed_state = json.loads(state.read_text(encoding="utf-8"))
    assert failed_state["phase"] == "audit"
    assert failed_state["attempt_count"] == 2
    assert failed_state["batch_count"] == 1

    _queue_results(results, (0, _migration_payload("audit", "ready")))
    audit_resumed = _run_coordinator(env, "resume", "--max-batches", "1")
    assert audit_resumed.returncode == 10
    prewrite = json.loads(state.read_text(encoding="utf-8"))
    assert prewrite["phase"] == "mapping"
    assert prewrite["point_of_no_return"] is False
    assert prewrite["write_attempted"] is False
    assert prewrite["attempt_count"] == 3
    assert prewrite["batch_count"] == 2

    _queue_results(
        results,
        (0, _migration_payload("mapping", "mapping_complete")),
        (0, _migration_payload("similarity", "complete")),
        (0, _migration_payload("similarity", "complete")),
    )
    completed = _run_coordinator(env, "resume")

    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert json.loads(state.read_text(encoding="utf-8"))["status"] == "complete"


def test_resume_rejects_post_mapping_history_downgraded_to_prewrite_boundary(
    coordinator_env,
):
    env, _root, _registry, _manifest, state, docker_log, results = coordinator_env
    _queue_results(
        results,
        (0, _migration_payload("mapping", "mapping_complete")),
    )
    _prepare_coordinator(env)
    bounded = _run_coordinator(env, "execute", "--max-batches", "3")
    assert bounded.returncode == 10
    damaged = json.loads(state.read_text(encoding="utf-8"))
    assert damaged["phase"] == "similarity"
    assert damaged["history"][-1]["phase"] == "mapping"
    damaged["phase"] = "mapping"
    damaged["point_of_no_return"] = False
    damaged["write_attempted"] = False
    damaged["automatic_rollback_allowed"] = True
    state.write_text(json.dumps(damaged, sort_keys=True) + "\n", encoding="utf-8")
    state.chmod(0o600)
    before_lines = len(docker_log.read_text(encoding="utf-8").splitlines())

    refused = _run_coordinator(env, "resume")

    assert refused.returncode == 2
    assert "failure_code=migration_resume_state_invalid" in refused.stdout
    new_lines = docker_log.read_text(encoding="utf-8").splitlines()[before_lines:]
    assert not any("|compose " in line and " run " in line for line in new_lines)


def test_resume_rejects_similarity_state_downgraded_to_mapping_with_ponr(
    coordinator_env,
):
    env, _root, _registry, _manifest, state, docker_log, results = coordinator_env
    _queue_results(
        results,
        (0, _migration_payload("mapping", "mapping_complete")),
    )
    _prepare_coordinator(env)
    bounded = _run_coordinator(env, "execute", "--max-batches", "3")
    assert bounded.returncode == 10
    damaged = json.loads(state.read_text(encoding="utf-8"))
    assert damaged["phase"] == "similarity"
    assert damaged["point_of_no_return"] is True
    assert damaged["write_attempted"] is True
    assert damaged["history"][-1]["phase"] == "mapping"
    assert damaged["history"][-1]["outcome"] == "mapping_complete"
    damaged["phase"] = "mapping"
    state.write_text(json.dumps(damaged, sort_keys=True) + "\n", encoding="utf-8")
    state.chmod(0o600)
    before_lines = len(docker_log.read_text(encoding="utf-8").splitlines())

    refused = _run_coordinator(env, "resume")

    assert refused.returncode == 2
    assert "failure_code=migration_resume_state_invalid" in refused.stdout
    new_lines = docker_log.read_text(encoding="utf-8").splitlines()[before_lines:]
    assert not any("|compose " in line and " run " in line for line in new_lines)


def test_resume_rejects_reordered_successful_phase_history(coordinator_env):
    env, _root, _registry, _manifest, state, docker_log, results = coordinator_env
    _queue_results(
        results,
        (1, _migration_payload("mapping", "progress", progress=True)),
        (0, _migration_payload("mapping", "mapping_complete")),
    )
    _prepare_coordinator(env)
    bounded = _run_coordinator(env, "execute", "--max-batches", "4")
    assert bounded.returncode == 10
    damaged = json.loads(state.read_text(encoding="utf-8"))
    assert damaged["phase"] == "similarity"
    assert [item["phase"] for item in damaged["history"]] == [
        "merge_drain",
        "audit",
        "mapping",
        "mapping",
    ]
    audit = dict(damaged["history"][1])
    mapping = dict(damaged["history"][2])
    audit["batch"], mapping["batch"] = mapping["batch"], audit["batch"]
    audit["completed_at"], mapping["completed_at"] = (
        mapping["completed_at"],
        audit["completed_at"],
    )
    damaged["history"][1], damaged["history"][2] = mapping, audit
    state.write_text(json.dumps(damaged, sort_keys=True) + "\n", encoding="utf-8")
    state.chmod(0o600)
    before_lines = len(docker_log.read_text(encoding="utf-8").splitlines())

    refused = _run_coordinator(env, "resume")

    assert refused.returncode == 2
    assert "failure_code=migration_resume_state_invalid" in refused.stdout
    new_lines = docker_log.read_text(encoding="utf-8").splitlines()[before_lines:]
    assert not any("|compose " in line and " run " in line for line in new_lines)


def test_resume_rejects_forged_complete_before_outer_verify(coordinator_env):
    env, _root, _registry, _manifest, state, docker_log, results = coordinator_env
    _queue_results(
        results,
        (0, _migration_payload("mapping", "mapping_complete")),
        (0, _migration_payload("similarity", "complete")),
    )
    _prepare_coordinator(env)
    bounded = _run_coordinator(env, "execute", "--max-batches", "4")
    assert bounded.returncode == 10
    damaged = json.loads(state.read_text(encoding="utf-8"))
    assert damaged["phase"] == "verify"
    assert damaged["history"][-1]["coordinator_phase"] == "similarity"
    assert damaged["point_of_no_return"] is True
    assert damaged["write_attempted"] is True
    damaged["phase"] = "complete"
    damaged["status"] = "verified"
    damaged["migration_complete"] = True
    damaged["verified_at"] = int(time.time())
    state.write_text(json.dumps(damaged, sort_keys=True) + "\n", encoding="utf-8")
    state.chmod(0o600)
    before_lines = len(docker_log.read_text(encoding="utf-8").splitlines())

    refused = _run_coordinator(env, "resume")

    assert refused.returncode == 2
    assert "failure_code=migration_resume_state_invalid" in refused.stdout
    new_lines = docker_log.read_text(encoding="utf-8").splitlines()[before_lines:]
    assert not any("|compose " in line and " run " in line for line in new_lines)


def test_resume_rejects_nonzero_outer_verify_evidence(coordinator_env):
    env, _root, _registry, _manifest, state, docker_log, results = coordinator_env
    _queue_results(
        results,
        (0, _migration_payload("mapping", "mapping_complete")),
        (0, _migration_payload("similarity", "complete")),
        (0, _migration_payload("similarity", "complete")),
    )
    _prepare_coordinator(env)
    env["FAKE_UP_FAIL"] = "1"
    failed = _run_coordinator(env)
    assert failed.returncode == 20
    env.pop("FAKE_UP_FAIL")
    damaged = json.loads(state.read_text(encoding="utf-8"))
    assert damaged["status"] == "activation_pending"
    assert damaged["history"][-1]["coordinator_phase"] == "verify"
    damaged["history"][-1]["graph"]["candidate_pairs"] = 1
    damaged["last_result"]["graph"]["candidate_pairs"] = 1
    damaged["last_attempt"]["graph"]["candidate_pairs"] = 1
    state.write_text(json.dumps(damaged, sort_keys=True) + "\n", encoding="utf-8")
    state.chmod(0o600)
    before_lines = len(docker_log.read_text(encoding="utf-8").splitlines())

    refused = _run_coordinator(env, "resume")

    assert refused.returncode == 2
    assert "failure_code=migration_resume_state_invalid" in refused.stdout
    new_lines = docker_log.read_text(encoding="utf-8").splitlines()[before_lines:]
    assert not any("|compose " in line and " run " in line for line in new_lines)


@pytest.mark.parametrize("removed_kind", ("event_mapping_gate", "maintenance"))
def test_abort_is_idempotent_after_one_generation_file_was_removed(
    coordinator_env,
    removed_kind,
):
    env, root, _registry, _manifest, state, _docker_log, _results = coordinator_env
    assert _run_coordinator(env, "prepare").returncode == 0
    aborting = json.loads(state.read_text(encoding="utf-8"))
    aborting["status"] = "aborting"
    state.write_text(json.dumps(aborting, sort_keys=True) + "\n", encoding="utf-8")
    state.chmod(0o600)
    gate = root / "data/.event-mapping-migration-required"
    maintenance = Path(env["HEALTH_MAINTENANCE_FILE"])
    (gate if removed_kind == "event_mapping_gate" else maintenance).unlink()

    resumed_abort = _run_coordinator(env, "abort")

    assert resumed_abort.returncode == 0, (
        resumed_abort.stdout,
        resumed_abort.stderr,
    )
    assert json.loads(state.read_text(encoding="utf-8"))["status"] == "aborted"
    assert not gate.exists()
    assert not maintenance.exists()


def test_abort_refuses_post_write_state_without_removing_generation_files(
    coordinator_env,
):
    env, root, _registry, _manifest, state, _docker_log, results = coordinator_env
    _queue_results(
        results,
        (1, _migration_payload("mapping", "progress", progress=True)),
    )
    _prepare_coordinator(env)
    progress = _run_coordinator(env, "execute", "--max-batches", "3")
    assert progress.returncode == 10
    _restore_recorded_source_release(env)
    state_before = state.read_bytes()
    gate = root / "data/.event-mapping-migration-required"
    gate_before = gate.read_bytes()
    maintenance = Path(env["HEALTH_MAINTENANCE_FILE"])
    maintenance_before = maintenance.read_bytes()

    refused = _run_coordinator(env, "abort")

    assert refused.returncode == 2
    assert state.read_bytes() == state_before
    assert gate.read_bytes() == gate_before
    assert maintenance.read_bytes() == maintenance_before


def test_coordinator_rejects_aliased_operational_paths_before_docker(
    coordinator_env,
):
    env, root, _registry, _manifest, state, docker_log, _results = coordinator_env
    env["HEALTH_MAINTENANCE_FILE"] = str(
        root / "data/.event-mapping-migration-required"
    )

    refused = _run_coordinator(env, "prepare")

    assert refused.returncode == 78
    assert "failure_code=operational_paths_must_be_distinct" in refused.stdout
    assert not state.exists()
    assert not docker_log.exists()


def test_batch_timeout_proves_named_container_absent_before_resume(
    coordinator_env,
):
    env, _root, _registry, _manifest, state, docker_log, results = coordinator_env
    _queue_results(
        results,
        (0, _migration_payload("mapping", "mapping_complete")),
        (0, _migration_payload("similarity", "complete", zero_work=False)),
        (0, _migration_payload("similarity", "complete", zero_work=True)),
    )
    _prepare_coordinator(env)
    env["FAKE_RUN_SLEEP"] = "1"

    timed_out = _run_coordinator(env, "execute", "--batch-timeout", "1")

    assert timed_out.returncode == 124
    failed = json.loads(state.read_text(encoding="utf-8"))
    assert failed["status"] == "failed"
    assert failed["failure_code"] == "migration_child_timeout"
    assert failed["point_of_no_return"] is False
    assert not Path(env["FAKE_MIGRATION_CONTAINER_STATE"]).exists()
    calls = docker_log.read_text(encoding="utf-8")
    assert f"|stop --time 10 {'a' * 64}" in calls
    assert f"|rm --force {'a' * 64}" in calls

    env.pop("FAKE_RUN_SLEEP")
    resumed = _run_coordinator(env, "resume")
    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    assert json.loads(state.read_text(encoding="utf-8"))["status"] == "complete"


def test_resume_reconciles_exact_digest_orphan_before_starting_next_batch(
    coordinator_env,
):
    env, _root, _registry, _manifest, state, docker_log, results = coordinator_env
    _prepare_coordinator(env)
    bounded = _run_coordinator(env, "execute", "--max-batches", "1")
    assert bounded.returncode == 10
    saved = json.loads(state.read_text(encoding="utf-8"))
    marker = Path(env["FAKE_MIGRATION_CONTAINER_STATE"])
    marker.write_text(
        json.dumps(
            {
                "name": saved["migration_container_name"],
                "image": IMAGE_ID,
                "status": "running",
            }
        ),
        encoding="utf-8",
    )
    _queue_results(
        results,
        (0, _migration_payload("audit", "ready")),
        (0, _migration_payload("mapping", "mapping_complete")),
        (0, _migration_payload("similarity", "complete", zero_work=False)),
        (0, _migration_payload("similarity", "complete", zero_work=True)),
    )
    log_offset = len(docker_log.read_text(encoding="utf-8").splitlines())

    resumed = _run_coordinator(env, "resume")

    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    assert not marker.exists()
    new_calls = docker_log.read_text(encoding="utf-8").splitlines()[log_offset:]
    remove_index = next(
        index for index, line in enumerate(new_calls) if "|rm --force " in line
    )
    run_index = next(
        index for index, line in enumerate(new_calls) if "|compose " in line and " run " in line
    )
    assert remove_index < run_index


def test_resume_refuses_foreign_digest_container_without_running_batch(
    coordinator_env,
):
    env, _root, _registry, _manifest, state, docker_log, _results = coordinator_env
    _prepare_coordinator(env)
    bounded = _run_coordinator(env, "execute", "--max-batches", "1")
    assert bounded.returncode == 10
    saved = json.loads(state.read_text(encoding="utf-8"))
    marker = Path(env["FAKE_MIGRATION_CONTAINER_STATE"])
    marker.write_text(
        json.dumps(
            {
                "name": saved["migration_container_name"],
                "image": OLD_IMAGE_ID,
                "status": "running",
            }
        ),
        encoding="utf-8",
    )
    log_offset = len(docker_log.read_text(encoding="utf-8").splitlines())

    refused = _run_coordinator(env, "resume")

    assert refused.returncode == 20
    assert marker.exists()
    assert json.loads(state.read_text(encoding="utf-8"))["failure_code"] == (
        "migration_container_identity_mismatch"
    )
    new_calls = docker_log.read_text(encoding="utf-8").splitlines()[log_offset:]
    assert not any("|compose " in line and " run " in line for line in new_calls)


def test_surviving_batch_container_is_removed_and_result_is_not_accepted(
    coordinator_env,
):
    env, _root, _registry, _manifest, state, _docker_log, _results = coordinator_env
    _prepare_coordinator(env)
    env["FAKE_LEAVE_MIGRATION_CONTAINER"] = "1"

    failed = _run_coordinator(env)

    assert failed.returncode == 20
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["status"] == "failed"
    assert saved["failure_code"] == "migration_container_survived_client"
    assert saved["batch_count"] == 0
    assert saved["point_of_no_return"] is False
    assert not Path(env["FAKE_MIGRATION_CONTAINER_STATE"]).exists()
    env.pop("FAKE_LEAVE_MIGRATION_CONTAINER")


def test_resume_stops_before_batch_when_exact_orphan_cleanup_fails(
    coordinator_env,
):
    env, _root, _registry, _manifest, state, docker_log, _results = coordinator_env
    _prepare_coordinator(env)
    bounded = _run_coordinator(env, "execute", "--max-batches", "1")
    assert bounded.returncode == 10
    saved = json.loads(state.read_text(encoding="utf-8"))
    marker = Path(env["FAKE_MIGRATION_CONTAINER_STATE"])
    marker.write_text(
        json.dumps(
            {
                "name": saved["migration_container_name"],
                "image": IMAGE_ID,
                "status": "running",
            }
        ),
        encoding="utf-8",
    )
    env["FAKE_MIGRATION_CLEANUP_FAIL"] = "1"
    log_offset = len(docker_log.read_text(encoding="utf-8").splitlines())

    refused = _run_coordinator(env, "resume")

    assert refused.returncode == 20
    assert marker.exists()
    failed = json.loads(state.read_text(encoding="utf-8"))
    assert failed["status"] == "failed"
    assert failed["failure_code"] == "migration_container_cleanup_failed"
    new_calls = docker_log.read_text(encoding="utf-8").splitlines()[log_offset:]
    assert not any("|compose " in line and " run " in line for line in new_calls)
