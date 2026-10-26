"""Single-event correction is explicit, content-pinned and rollback-safe."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import uuid

import pytest

from core.source_contract import ARTICLE_CLAIM_PREFIX, MAPPING_CLAIM_PREFIX
from db import database
from db.event_core import (
    merge_events, record_event_mapping_replay, resolve_or_create_event, upsert_event_evidence,
    upsert_event_security, upsert_security,
)
from ops import correct_event_mapping as correction


TITLE = "数字人民币跨境联通合作范围稳步拓展"
OLD_CONTRACT = "asset-map:title-only-v2:old-rules"


def _seed(conn, title, source):
    digest = hashlib.sha256(title.encode()).hexdigest()
    event = resolve_or_create_event(
        conn, {"article_url_sha256": source}, title=title, event_type="公司公告",
        direction="中性", priority=8, occurred_at=1_000, available_at=1_000,
        strategy_version="unchanged-strategy", model_version="test-model", now=1_000,
    )
    evidence = upsert_event_evidence(
        conn, event, source_uri=source, source_type="article", source_name="frozen source",
        claim_key=ARTICLE_CLAIM_PREFIX + digest, claim_text=title,
        excerpt="", snapshot_text=title, snapshot_hash=digest,
        stance="supports", reliability=0.8, observed_at=1_000, now=1_000,
    )
    mapping = upsert_event_security(
        conn, event, "BSE:920670", relation_type="direct", direction="中性",
        confidence=0.9, relevance=10, rationale="原文直接点名数字人。",
        mapping_version=OLD_CONTRACT, now=1_001,
    )
    database._bind_mapping_evidence(
        conn, event_id=event, mapping_id=mapping,
        security_id="BSE:920670", source_uri=source,
    )
    record_event_mapping_replay(
        conn, evidence, OLD_CONTRACT, evidence_version=1,
        root_event_id=event, root_event_version=1,
        input_fingerprint="a" * 64, output_fingerprint="b" * 64,
        status="mapped", mapped_count=1, now=1_001,
    )
    return event, evidence, mapping


def _manifest(conn, event, mapping):
    mapping_row = conn.execute("SELECT * FROM event_securities WHERE mapping_id = ?", (mapping,)).fetchone()
    fields = ("mapping_id", "security_id", "revision", "mapping_version", "relation_type")
    return {
        "schema": 1, "event_id": event, "event_version": 1,
        "mapping": {key: mapping_row[key] for key in fields},
        "evidence": [dict(row) for row in conn.execute(
            "SELECT evidence_id,version,source_uri,snapshot_hash FROM event_evidence "
            "WHERE event_id = ? ORDER BY evidence_id", (event,),
        )],
        "reason": "Currency phrase falsely recognized as a listed issuer; retire this association only.",
    }


@pytest.fixture
def seeded():
    database.init_db()
    with database.get_connection() as conn:
        upsert_security(conn, exchange="BSE", ticker="920670", name="数字人",
                        security_id="BSE:920670", asset_type="equity", now=100)
        event, evidence, mapping = _seed(conn, TITLE, "https://example.test/currency")
        other, _, other_mapping = _seed(conn, "数字人公告拟回购股份", "https://example.test/issuer")
        manifest = _manifest(conn, event, mapping)
        conn.commit()
    return Path(database.DB_PATH), manifest, evidence, other, other_mapping


def _dump(path):
    with sqlite3.connect(path) as conn:
        return list(conn.iterdump())


def _apply(path, manifest, tmp_path, *, plan=None):
    if plan is None:
        plan = correction.run_correction(path, manifest)["plan"]
    return correction.run_correction(
        path, manifest, apply=True, plan_sha256=plan["plan_sha256"],
        audit_path=tmp_path / "correction-audit.jsonl",
    )


def test_dry_run_is_read_only_stable_and_content_free(seeded):
    path, manifest, _, _, _ = seeded
    before = _dump(path)
    first = correction.run_correction(path, manifest)
    assert first == correction.run_correction(path, manifest)
    assert first["status"] == "dry_run"
    assert first["plan"]["evidence_count"] == 2
    assert first["plan"]["replay_evidence_count"] == 1
    assert TITLE not in json.dumps(first, ensure_ascii=False)
    assert _dump(path) == before


def test_apply_only_retires_approved_mapping_and_preserves_history(seeded, tmp_path):
    path, manifest, evidence, other, _ = seeded
    with database.get_connection() as conn:
        other_before = correction._record_state(conn, other)
        before = correction._record_state(conn, manifest["event_id"])
    result = _apply(path, manifest, tmp_path)
    assert result["status"] == "applied"
    assert result["result"] == {"processed": 1, "mapped": 0, "mapping_rows": 0, "retired": 1}
    with database.get_connection() as conn:
        after = correction._record_state(conn, manifest["event_id"])
        assert correction._record_state(conn, other) == other_before
        assert after["events"] == before["events"] and after["evidence"] == before["evidence"]
        assert len(after["ledger"]) == len(before["ledger"]) + 1
        current = conn.execute("SELECT COUNT(*) FROM event_securities WHERE event_id=? AND is_current=1",
                               (manifest["event_id"],)).fetchone()[0]
        assert current == 0
        assert all(row["evidence_id"] == evidence for row in after["ledger"])
    audit = Path(result["audit_path"])
    assert audit.stat().st_mode & 0o777 == 0o600
    assert [json.loads(line)["phase"] for line in audit.read_text().splitlines()] == [
        "prepared", "validated", "committed",
    ]
    after_dump = _dump(path)
    with pytest.raises(correction.CorrectionRefused, match="current mappings"):
        _apply(path, manifest, tmp_path)
    assert _dump(path) == after_dump


@pytest.mark.parametrize("case", ["event_version", "mapping_id", "security_id", "mapping_version",
                                 "revision", "source_uri", "snapshot_hash", "extra_evidence"])
def test_inexact_manifest_refuses_without_writes(seeded, case):
    path, manifest, _, _, _ = seeded
    manifest = deepcopy(manifest)
    if case == "event_version":
        manifest[case] = 2
    elif case in {"mapping_id", "security_id", "mapping_version", "revision"}:
        manifest["mapping"][case] = 2 if case == "revision" else (
            "asset-map:title-only-v2:wrong" if case == "mapping_version" else "wrong-id"
        )
    elif case == "extra_evidence":
        manifest["evidence"].pop()
    else:
        manifest["evidence"][0][case] = "0" * 64 if case == "snapshot_hash" else "https://example.test/wrong"
    before = _dump(path)
    with pytest.raises(correction.CorrectionRefused):
        correction.run_correction(path, manifest)
    assert _dump(path) == before


def test_new_evidence_between_review_and_apply_refuses(seeded, tmp_path):
    path, manifest, _, _, _ = seeded
    plan = correction.run_correction(path, manifest)["plan"]
    with database.get_connection() as conn:
        row = manifest["evidence"][0]
        upsert_event_evidence(
            conn, manifest["event_id"], source_uri="https://example.test/new-source",
            claim_key=ARTICLE_CLAIM_PREFIX + row["snapshot_hash"], claim_text=TITLE,
            snapshot_text=TITLE, snapshot_hash=row["snapshot_hash"], now=2_000,
        )
        conn.commit()
    before = _dump(path)
    with pytest.raises(correction.CorrectionRefused, match="evidence/source/hash"):
        _apply(path, manifest, tmp_path, plan=plan)
    assert _dump(path) == before and not (tmp_path / "correction-audit.jsonl").exists()


def test_changed_master_between_review_and_apply_refuses(seeded, tmp_path):
    path, manifest, _, _, _ = seeded
    plan = correction.run_correction(path, manifest)["plan"]
    with database.get_connection() as conn:
        upsert_security(conn, exchange="SSE", ticker="600030", name="中信证券",
                        security_id="SSE:600030", asset_type="equity", now=2_000)
        conn.commit()
    before = _dump(path)
    with pytest.raises(correction.CorrectionRefused, match="plan changed"):
        _apply(path, manifest, tmp_path, plan=plan)
    assert _dump(path) == before


def test_nonempty_resolution_is_not_an_authorized_correction(seeded):
    path, _, _, other, other_mapping = seeded
    with database.get_connection() as conn:
        manifest = _manifest(conn, other, other_mapping)
    with pytest.raises(correction.CorrectionRefused, match="still produces"):
        correction.run_correction(path, manifest)


def test_bound_original_title_can_veto_retirement_even_when_unbound_is_currency(seeded):
    path, manifest, _, _, _ = seeded
    title = "数字人公告拟回购股份"
    digest = hashlib.sha256(title.encode()).hexdigest()
    with database.get_connection() as conn:
        mapping = manifest["mapping"]["mapping_id"]
        upsert_event_evidence(
            conn, manifest["event_id"], source_uri="https://example.test/bound-real-issuer",
            claim_key=MAPPING_CLAIM_PREFIX + mapping + ":" + str(uuid.uuid4()),
            claim_text=title, snapshot_text=title, snapshot_hash=digest,
            mapping_id=mapping, security_id="BSE:920670", now=2_000,
        )
        manifest = _manifest(conn, manifest["event_id"], mapping)
        conn.commit()
    before = _dump(path)
    with pytest.raises(correction.CorrectionRefused, match="still produces"):
        correction.run_correction(path, manifest)
    assert _dump(path) == before


def test_merged_child_refuses(seeded):
    path, manifest, _, other, _ = seeded
    with database.get_connection() as conn:
        merge_events(conn, other, manifest["event_id"], reason="test bounded correction", now=2_000)
        manifest["event_version"] = conn.execute(
            "SELECT version FROM events WHERE event_id = ?", (manifest["event_id"],),
        ).fetchone()[0]
        conn.commit()
    before = _dump(path)
    with pytest.raises(correction.CorrectionRefused, match="merged event members"):
        correction.run_correction(path, manifest)
    assert _dump(path) == before


def test_failure_after_retirement_rolls_back_every_database_change(seeded, tmp_path, monkeypatch):
    path, manifest, _, _, _ = seeded
    before = _dump(path)
    def fail(*_args):
        raise RuntimeError("injected post-write failure")
    monkeypatch.setattr(correction, "_verify_after", fail)
    with pytest.raises(RuntimeError, match="injected"):
        _apply(path, manifest, tmp_path)
    assert _dump(path) == before
    phases = [json.loads(line)["phase"] for line in (tmp_path / "correction-audit.jsonl").read_text().splitlines()]
    assert phases == ["prepared", "rolled_back"]


def test_apply_needs_exact_digest_and_new_audit_file(seeded, tmp_path):
    path, manifest, _, _, _ = seeded
    before = _dump(path)
    with pytest.raises(correction.CorrectionRefused, match="dry-run plan digest"):
        correction.run_correction(path, manifest, apply=True)
    with pytest.raises(correction.CorrectionRefused, match="plan changed"):
        correction.run_correction(path, manifest, apply=True, plan_sha256="0" * 64,
                                  audit_path=tmp_path / "audit.jsonl")
    audit = tmp_path / "correction-audit.jsonl"
    audit.write_text("keep existing evidence")
    with pytest.raises(FileExistsError):
        _apply(path, manifest, tmp_path)
    assert audit.read_text() == "keep existing evidence" and _dump(path) == before


def test_existing_mapping_write_gate_is_not_bypassed(seeded, tmp_path, monkeypatch):
    path, manifest, _, _, _ = seeded
    monkeypatch.setenv("AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE", "1")
    monkeypatch.setenv("AI_FILTER_MAPPING_MIGRATION_GATE_FILE", str(tmp_path / "missing-gate"))
    before = _dump(path)
    with pytest.raises(database.EventMappingMigrationGateError):
        _apply(path, manifest, tmp_path)
    assert _dump(path) == before


def test_audit_parent_directory_and_file_are_durable_before_mutation(seeded, tmp_path, monkeypatch):
    path, manifest, _, _, _ = seeded
    observed = []
    original_fsync = correction.os.fsync
    original_apply = correction._apply
    def fsync(descriptor):
        observed.append("directory" if stat.S_ISDIR(correction.os.fstat(descriptor).st_mode) else "file")
        return original_fsync(descriptor)
    def apply(*args):
        assert observed[:2] == ["directory", "file"]
        observed.append("mutation")
        return original_apply(*args)
    monkeypatch.setattr(correction.os, "fsync", fsync)
    monkeypatch.setattr(correction, "_apply", apply)
    _apply(path, manifest, tmp_path)
    assert observed[:3] == ["directory", "file", "mutation"]


def test_audit_directory_fsync_failure_prevents_database_mutation(seeded, tmp_path, monkeypatch):
    path, manifest, _, _, _ = seeded
    before = _dump(path)
    def fail(_path):
        raise OSError("injected directory sync failure")
    monkeypatch.setattr(correction, "_sync_audit_directory", fail)
    with pytest.raises(OSError, match="directory sync"):
        _apply(path, manifest, tmp_path)
    assert _dump(path) == before


def test_explicit_existing_database_is_required(seeded, tmp_path):
    _, manifest, _, _, _ = seeded
    missing = tmp_path / "missing.db"
    with pytest.raises(correction.CorrectionRefused, match="explicit existing absolute"):
        correction.run_correction(missing, manifest)
    assert not missing.exists()
    with pytest.raises(correction.CorrectionRefused, match="explicit existing absolute"):
        correction.run_correction(Path("cache.db"), manifest)
    with pytest.raises(SystemExit) as exc:
        correction.main([])
    assert exc.value.code == 2


def test_cli_default_is_dry_run(seeded, tmp_path, capsys):
    path, manifest, _, _, _ = seeded
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps(manifest))
    before = _dump(path)
    assert correction.main(["--db", str(path), "--manifest", str(manifest_file)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"
    assert _dump(path) == before
