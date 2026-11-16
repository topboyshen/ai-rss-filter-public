"""ops/retire_shared_section_event.py: only flips is_current on one section event."""

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from db import database
from db.event_core import (
    resolve_or_create_event, upsert_event_evidence, upsert_event_security, upsert_security,
)
from ops import retire_shared_section_event as tool

SECTION = "https://www.cls.cn/telegraph"
VERSION = "asset-map:event-mapping-store-v7:manifest-" + "9" * 64 + ":master-" + "2" * 64


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _event(conn, link, titles, securities):
    kind, key = database._normalized_article_identity(link)
    event = resolve_or_create_event(
        conn, {kind: key}, title=titles[-1], event_type="news", direction="中性", priority=2,
        occurred_at=1_000, available_at=1_000, strategy_version="s", model_version="m", now=1_000)
    for title in titles:
        upsert_event_evidence(
            conn, event, source_uri=link, source_type="article", claim_key="article_snapshot:" + _sha(title),
            claim_text=title, snapshot_text=title, snapshot_hash=_sha(title), observed_at=1_000, now=1_000)
    mappings = []
    for security in securities:
        mapping = upsert_event_security(
            conn, event, security, relation_type="concept", direction="中性", confidence=0.5,
            relevance=5, rationale="title hit", mapping_version=VERSION, now=1_001)
        mappings.append(mapping)
        upsert_event_evidence(
            conn, event, source_uri=link, source_type="article",
            claim_key=f"mapping:{mapping}:{uuid.uuid4()}", claim_text=titles[0],
            snapshot_text=titles[0], snapshot_hash=_sha(titles[0]), mapping_id=mapping,
            security_id=security, observed_at=1_002, now=1_002)
    return event, mappings


@pytest.fixture
def world():
    database.init_db()
    with database.get_connection() as conn:
        for ticker, name in (("600111", "北方稀土"), ("300308", "中际旭创"), ("600030", "中信证券")):
            upsert_security(conn, exchange="SSE", ticker=ticker, name=name, security_id=f"SSE:{ticker}",
                            asset_type="equity", now=100)
        bad, bad_maps = _event(conn, SECTION, ["稀土快讯", "光模块快讯", "沃勒讲话"], ["SSE:600111", "SSE:300308"])
        good, good_maps = _event(conn, "https://wallstreetcn.com/livenews/1", ["中信证券回购"], ["SSE:600030"])
        conn.execute("DELETE FROM event_mapping_replay_dirty_roots")
        conn.execute("DELETE FROM event_similarity_replay_dirty_roots")
        conn.commit()
    return {"path": Path(database.DB_PATH), "bad": bad, "bad_maps": bad_maps, "good": good,
            "good_maps": good_maps}


def _dump(path, event):
    with sqlite3.connect(path) as conn:
        return tool._state(conn, event)


def _plan(world, event=None):
    with sqlite3.connect(world["path"]) as conn:
        return tool.build_plan(conn, event or world["bad"], world["path"])[0]


def test_dry_run_is_read_only_and_lists_only_current_mappings(world, capsys):
    before = world["path"].read_bytes()
    assert tool.main(["--db", str(world["path"]), "--event-id", world["bad"]]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert {row["mapping_id"] for row in plan["retire"]} == set(world["bad_maps"])
    assert plan["section_url"] == SECTION and plan["evidence_count"] == 5
    assert world["path"].read_bytes() == before


def test_apply_flips_only_target_mappings_and_exports_affected_rows(world, tmp_path):
    good_before = _dump(world["path"], world["good"])
    bad_before = _dump(world["path"], world["bad"])
    plan = _plan(world)
    result = tool.apply(world["path"], world["bad"], plan["plan_sha256"],
                        tmp_path / "backup.json", tmp_path / "audit.json")
    assert result["retired"] == 2
    after = _dump(world["path"], world["bad"])
    assert all(row["is_current"] == 0 for row in after["mappings"])
    assert [dict(r, is_current=0) for r in bad_before["mappings"]] == after["mappings"]
    for key in ("events", "identities", "evidence"):
        assert after[key] == bad_before[key]
    assert after["mapping_dirty"] == [] and len(after["similarity_dirty"]) == 1
    assert _dump(world["path"], world["good"]) == good_before
    backup = json.loads((tmp_path / "backup.json").read_text())
    assert len(backup["rows"]["evidence"]) == 5 and len(backup["rows"]["mappings"]) == 2
    assert hashlib.sha256((tmp_path / "backup.json").read_bytes()).hexdigest() == result["backup_sha256"]
    assert json.loads((tmp_path / "audit.json").read_text())["retired"] == 2


def test_refuses_a_normal_article_event(world):
    with pytest.raises(tool.RetireRefused, match="shared section"):
        _plan(world, world["good"])


def test_refuses_drift_after_review_and_writes_nothing(world, tmp_path):
    plan = _plan(world)
    with database.get_connection() as conn:
        upsert_event_evidence(
            conn, world["bad"], source_uri=SECTION, source_type="article", claim_key="article_snapshot:x",
            claim_text="新快讯", snapshot_text="新快讯", snapshot_hash=_sha("新快讯"), observed_at=2_000, now=2_000)
        conn.commit()
    before = _dump(world["path"], world["bad"])
    with pytest.raises(tool.RetireRefused, match="reviewed plan"):
        tool.apply(world["path"], world["bad"], plan["plan_sha256"], tmp_path / "b.json", tmp_path / "a.json")
    assert _dump(world["path"], world["bad"]) == before
    assert not (tmp_path / "b.json").exists()


def test_refuses_evidence_from_another_source(world):
    with database.get_connection() as conn:
        upsert_event_evidence(
            conn, world["bad"], source_uri="https://other.test/x", source_type="article",
            claim_key="article_snapshot:y", claim_text="别处", snapshot_text="别处",
            snapshot_hash=_sha("别处"), observed_at=2_000, now=2_000)
        conn.commit()
    with pytest.raises(tool.RetireRefused, match="non-section"):
        _plan(world)


def test_refuses_existing_output_files(world, tmp_path):
    (tmp_path / "b.json").write_text("{}")
    with pytest.raises(tool.RetireRefused, match="must be new"):
        tool.apply(world["path"], world["bad"], _plan(world)["plan_sha256"], tmp_path / "b.json",
                   tmp_path / "a.json")


@pytest.mark.parametrize("sql", [
    "UPDATE event_evidence SET stance = 'context'",
    "UPDATE events SET priority = 1",
    "DELETE FROM event_evidence",
    "INSERT INTO event_securities (mapping_id) VALUES ('x')",
    "UPDATE event_securities SET relevance = 1",
    "DROP TABLE events",
])
def test_authorizer_denies_out_of_scope_writes(world, sql):
    with sqlite3.connect(world["path"]) as conn:
        conn.set_authorizer(tool._authorizer(insert_mappings=False))
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)


def test_rollback_restores_current_mappings_as_new_revisions(world, tmp_path):
    plan = _plan(world)
    tool.apply(world["path"], world["bad"], plan["plan_sha256"], tmp_path / "b.json", tmp_path / "a.json")
    result = tool.rollback(world["path"], tmp_path / "b.json", tmp_path / "r.json")
    assert len(result["restored"]) == 2
    current = [r for r in _dump(world["path"], world["bad"])["mappings"] if r["is_current"]]
    assert sorted(r["security_id"] for r in current) == ["SSE:300308", "SSE:600111"]
    assert all(r["revision"] == 2 for r in current)
