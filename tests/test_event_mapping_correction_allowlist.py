"""Exact-row allowlist for correcting legacy-contract mappings.

Only nine synthetic, test-installed rows may be retired through the legacy path; every other
legacy, merged, oversized or drifted row stays refused, and nothing outside the
approved ``event_securities.is_current`` flip is ever written.
"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

import pytest

from core.source_contract import is_current_mapping_writer
from db import database
from db.event_core import (
    merge_events, record_event_mapping_replay, resolve_or_create_event, upsert_event_evidence,
    upsert_event_security, upsert_security,
)
from ops import correct_event_mapping as correction
from ops import correct_event_mapping_allowlist as allowlist

LEGACY = "asset-map:event-mapping-store-v7:manifest-" + "9" * 64 + ":master-" + "2" * 64
CURRENCY = "我国新能源汽车产业规模已超千万辆级"
ISSUER_TITLE = "A股天然气概念盘初走强，国新能源涨停。"
TARGET = "SSE:600617"
INSURER = "SSE:601336"
ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = ROOT / "tests" / "fixtures" / "legacy_correction_manifests"


def _example_entries():
    examples = json.loads((MANIFESTS.parent / "legacy_correction_examples.json").read_text())
    assert examples["synthetic_only"] is True
    entries = []
    for value in examples["entries"]:
        value["group"] = tuple(tuple(pair) for pair in value["group"])
        value["retained_mappings"] = tuple(tuple(pair) for pair in value["retained_mappings"])
        entries.append(allowlist.LegacyCorrection(**value))
    return tuple(entries)


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _seed_securities(conn):
    for exchange, ticker, name in (
        ("SSE", "600617", "国新能源"), ("SSE", "601336", "新华保险"),
        ("SSE", "601985", "中国核电"), ("SSE", "600030", "中信证券"),
    ):
        upsert_security(conn, exchange=exchange, ticker=ticker, name=name,
                        security_id=f"{exchange}:{ticker}", asset_type="equity", now=100)


def _evidence(conn, event, title, *, source, body=""):
    snapshot = title + ("\n\n" + body if body else "")
    return upsert_event_evidence(
        conn, event, source_uri=source, source_type="article", source_name="legacy feed",
        claim_key="article_snapshot:" + _sha(snapshot), claim_text=title, excerpt=body,
        snapshot_text=snapshot, snapshot_hash=_sha(snapshot), stance="supports",
        reliability=0.5, observed_at=1_000, now=1_000,
    )


def _new_event(conn, title, source):
    return resolve_or_create_event(
        conn, {"article_url_sha256": _sha(source)}, title=title, event_type="公司公告",
        direction="看多", priority=8, occurred_at=1_000, available_at=1_000,
        strategy_version="unchanged-strategy", model_version="test-model", now=1_000,
    )


def _seed(conn, name, *, title=CURRENCY, members=0, extra=0, retained=(), body="",
          securities=(TARGET,)):
    """One legacy-contract event shaped like a production row; returns (event, target mapping)."""
    root = _new_event(conn, title, f"https://example.test/{name}/root")
    _evidence(conn, root, title, source=f"https://example.test/{name}/root", body=body)
    for index in range(extra):
        _evidence(conn, root, title, source=f"https://example.test/{name}/extra/{index}")
    mappings = {}
    for security in (*securities, *retained):
        mapping = upsert_event_security(
            conn, root, security, relation_type="direct", direction="中性", confidence=0.9,
            relevance=10, rationale="legacy rule hit", mapping_version=LEGACY, now=1_001,
        )
        mappings[security] = mapping
        upsert_event_evidence(
            conn, root, source_uri=f"https://example.test/{name}/root", source_type="article",
            claim_key=f"mapping:{mapping}:{uuid.uuid4()}", claim_text=title, excerpt=body,
            snapshot_text=title + ("\n\n" + body if body else ""),
            snapshot_hash=_sha(title + ("\n\n" + body if body else "")), mapping_id=mapping,
            security_id=security, now=1_002,
        )
    first = _evidence_row(conn, root)
    record_event_mapping_replay(
        conn, first, LEGACY, evidence_version=1, root_event_id=root, root_event_version=1,
        input_fingerprint="a" * 64, output_fingerprint="b" * 64, status="mapped",
        mapped_count=len(mappings), now=1_003,
    )
    for index in range(members):
        member = _new_event(conn, title, f"https://example.test/{name}/member/{index}")
        _evidence(conn, member, title, source=f"https://example.test/{name}/member/{index}")
        merge_events(conn, member, root, reason="duplicate publisher story", now=1_100 + index)
    return root, mappings[securities[0]]


def _evidence_row(conn, event):
    return conn.execute(
        "SELECT evidence_id FROM event_evidence WHERE event_id = ? AND mapping_id IS NULL "
        "ORDER BY evidence_id LIMIT 1", (event,)).fetchone()[0]


def _entry(conn, event, mapping_id):
    mapping = dict(conn.execute("SELECT * FROM event_securities WHERE mapping_id = ?",
                                (mapping_id,)).fetchone())
    group = correction.canonical_event_group(conn, event)
    versions = {row[0]: row[1] for row in conn.execute(
        f"SELECT event_id, version FROM events WHERE event_id IN ({','.join('?' * len(group))})", group)}
    evidence = [dict(row) for member in group for row in conn.execute(
        "SELECT * FROM event_evidence WHERE event_id = ? ORDER BY evidence_id", (member,))]
    retained = tuple(sorted(
        (row[0], row[1]) for row in conn.execute(
            "SELECT mapping_id, revision FROM event_securities WHERE event_id = ? AND is_current = 1 "
            "AND mapping_id <> ?", (event, mapping_id))))
    security = conn.execute("SELECT exchange, ticker FROM securities WHERE security_id = ?",
                            (mapping["security_id"],)).fetchone()
    return allowlist.LegacyCorrection(
        event_id=event, event_version=versions[event], mapping_id=mapping_id,
        revision=mapping["revision"], mapping_version=mapping["mapping_version"],
        security_id=mapping["security_id"], security_label=f"{security[0]}:{security[1]}",
        group=tuple((member, versions[member]) for member in group),
        evidence_count=len(evidence), evidence_sha256=allowlist.evidence_sha256(evidence),
        retained_mappings=retained,
    )


def _manifest(conn, entry):
    mapping = conn.execute("SELECT * FROM event_securities WHERE mapping_id = ?",
                           (entry.mapping_id,)).fetchone()
    evidence = [dict(row) for member, _v in entry.group for row in conn.execute(
        "SELECT evidence_id, version, source_uri, snapshot_hash FROM event_evidence "
        "WHERE event_id = ? ORDER BY evidence_id", (member,))]
    return {
        "schema": 1, "event_id": entry.event_id, "event_version": entry.event_version,
        "mapping": {key: mapping[key] for key in (
            "mapping_id", "security_id", "revision", "mapping_version", "relation_type")},
        "evidence": evidence,
        "reason": "Retire the false direct issuer mapping only; the title never names the issuer.",
    }


def _install(monkeypatch, entries):
    monkeypatch.setattr(allowlist, "LEGACY_ALLOWLIST", {entry.key: entry for entry in entries})


def _clean_dirty(conn):
    conn.execute("DELETE FROM event_mapping_replay_dirty_roots")
    conn.execute("DELETE FROM event_similarity_replay_dirty_roots")


def _dump(path):
    with sqlite3.connect(path) as conn:
        return list(conn.iterdump())


def _table(path, table):
    with sqlite3.connect(path) as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()  # noqa: S608


def _add_signal_rows(conn, event, mapping_id, security_id):
    """A ledger signal, its immutable mapping snapshot and an outbox row for the event."""
    mapping = conn.execute("SELECT revision FROM event_securities WHERE mapping_id = ?",
                           (mapping_id,)).fetchone()
    version = conn.execute("SELECT version FROM events WHERE event_id = ?", (event,)).fetchone()[0]
    cursor = conn.execute(
        "INSERT INTO backtest_signals (signal_key, link, available_at, scored_at, recorded_at, "
        "score, aggregation_threshold, event_id, event_version) VALUES (?, ?, 2000, 2000, 2000, "
        "8, 7, ?, ?)", (f"sig-{event}", f"https://example.test/{event}", event, version))
    signal = cursor.lastrowid
    conn.execute(
        "INSERT INTO backtest_signal_securities (ledger_signal_id, mapping_id, event_id, "
        "security_id, mapping_revision, signal_available_at, captured_at) "
        "VALUES (?, ?, ?, ?, ?, 2000, 2000)",
        (signal, mapping_id, event, security_id, mapping["revision"]))
    conn.execute(
        "INSERT INTO integration_signal_outbox (ledger_signal_id, event_id, available_at, "
        "recorded_at, enqueued_at, score, direction, score_confidence, strategy_version, "
        "mapping_version, mapped_theme, score_path, revision_key, signal_stage, revision_seq, "
        "revision_state_key, independence_id, aggregation_threshold, selected, "
        "is_similarity_copy, domain_event_id, event_version) VALUES (?, ?, 2000, 2000, 2000, 8, "
        "'看多', 0.9, 's', 'm', '', '', 'base', 'base', 0, '', 'i', 7, 1, 0, ?, ?)",
        (signal, f"outbox-{event}", event, version))


@pytest.fixture
def world(monkeypatch):
    """Nine shaped legacy rows + a genuine issuer row + an unrelated security row."""
    database.init_db()
    shapes = [
        dict(), dict(members=2), dict(), dict(body="工业和信息化部表示，将制定产业发展规划。"),
        dict(), dict(members=2),
        dict(members=2, title="新华保险投资40亿元参与博枫新中国新能源基金",
             securities=(TARGET,), retained=(INSURER,)),
        dict(extra=26, securities=(TARGET,), retained=("SSE:601985",),
             title="能源内参｜中国新能源汽车销量占比突破60%；核电项目获批"),
        dict(),
    ]
    with database.get_connection() as conn:
        _seed_securities(conn)
        rows = [_seed(conn, f"row{index}", **shape) for index, shape in enumerate(shapes)]
        genuine, genuine_mapping = _seed(conn, "genuine", title=ISSUER_TITLE)
        other, other_mapping = _seed(conn, "other", title="中信证券发布回购公告",
                                     securities=("SSE:600030",))
        _clean_dirty(conn)
        entries = [_entry(conn, event, mapping) for event, mapping in rows]
        manifests = [_manifest(conn, entry) for entry in entries]
        genuine_manifest = _manifest(conn, _entry(conn, genuine, genuine_mapping))
        for event, mapping, security in ((rows[0][0], rows[0][1], TARGET), (genuine, genuine_mapping, TARGET),
                                         (other, other_mapping, "SSE:600030")):
            _add_signal_rows(conn, event, mapping, security)
        conn.commit()
    _install(monkeypatch, entries)
    return {"path": Path(database.DB_PATH), "entries": entries, "manifests": manifests,
            "rows": rows, "genuine": (genuine, genuine_mapping), "genuine_manifest": genuine_manifest,
            "other": (other, other_mapping)}


def _apply(path, manifest, tmp_path, name):
    plan = correction.run_correction(path, manifest)["plan"]
    return correction.run_correction(
        path, manifest, apply=True, plan_sha256=plan["plan_sha256"],
        audit_path=tmp_path / f"audit-{name}.jsonl")


def _current(path):
    with sqlite3.connect(path) as conn:
        return {row[0] for row in conn.execute(
            "SELECT mapping_id FROM event_securities WHERE is_current = 1")}


def test_nine_allowlisted_rows_retire_and_nothing_else_changes(world, tmp_path):
    path = world["path"]
    targets = {mapping for _event, mapping in world["rows"]}
    before_current = _current(path)
    untouched = {name: _table(path, name) for name in (
        "events", "event_evidence", "event_mapping_replay_ledger", "securities",
        "backtest_signals", "backtest_signal_securities", "integration_signal_outbox")}
    assert len(targets) == 9 and targets <= before_current
    for index, manifest in enumerate(world["manifests"]):
        result = _apply(path, manifest, tmp_path, index)
        assert result["status"] == "applied"
        assert result["result"] == {"retired": 1, "mapped": 0, "mapping_rows": 0,
                                    "ledger_rows": 0, "processed": 0}
        audit = (tmp_path / f"audit-{index}.jsonl").read_text().splitlines()
        assert [json.loads(line)["phase"] for line in audit] == ["prepared", "validated", "committed"]
    # Exactly the nine approved rows left the current set: the genuine issuer row,
    # the retained 新华保险/中国核电 mappings and the unrelated security remain.
    assert before_current - _current(path) == targets and _current(path) < before_current
    assert world["genuine"][1] in _current(path) and world["other"][1] in _current(path)
    with sqlite3.connect(path) as conn:
        retained = [row[0] for row in conn.execute(
            "SELECT s.exchange||':'||s.ticker FROM event_securities m JOIN securities s "
            "ON s.security_id = m.security_id WHERE m.is_current = 1 AND m.mapping_id IN "
            "(SELECT mapping_id FROM event_securities WHERE event_id IN (?, ?))",
            (world["rows"][6][0], world["rows"][7][0]))]
        assert sorted(retained) == [INSURER, "SSE:601985"]
        assert conn.execute("SELECT COUNT(*) FROM event_mapping_replay_dirty_roots").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM event_similarity_replay_dirty_roots").fetchone()[0] == 9
    for name, rows in untouched.items():
        assert _table(path, name) == rows, f"{name} must be byte-for-byte unchanged"


def test_dry_run_never_writes_and_reports_downstream_without_touching_it(world):
    path = world["path"]
    before = _dump(path)
    for manifest in world["manifests"]:
        first = correction.run_correction(path, manifest)
        assert first == correction.run_correction(path, manifest)
        assert first["plan"]["effect"] == "retire_one_allowlisted_legacy_mapping_no_ledger_no_new_rows"
        assert first["downstream_read_only"]["written_by_correction"] == 0
    first = correction.run_correction(path, world["manifests"][0])
    assert first["downstream_read_only"]["signals_on_group_events"] == 1
    assert first["downstream_read_only"]["outbox_rows_on_group_events"] == 1
    assert first["downstream_read_only"]["signal_security_snapshots_for_target_mapping"] == 1
    assert _dump(path) == before


def test_dry_run_opens_the_database_read_only(world, monkeypatch):
    opened = []
    real = sqlite3.connect

    def spy(target, *args, **kwargs):
        opened.append(str(target))
        return real(target, *args, **kwargs)
    monkeypatch.setattr(correction.sqlite3, "connect", spy)
    correction.run_correction(world["path"], world["manifests"][0])
    assert opened and all(target.endswith("?mode=ro") for target in opened)
    with pytest.raises(sqlite3.OperationalError):
        sqlite3.connect(f"{world['path'].as_uri()}?mode=ro", uri=True).execute(
            "UPDATE event_securities SET is_current = 0")


def test_member_event_and_oversized_group_are_handled_only_by_exact_entry(world):
    big = world["entries"][7]
    merged = world["entries"][1]
    assert big.evidence_count > correction.MAX_EVIDENCE and len(merged.group) == 3
    assert correction.run_correction(world["path"], world["manifests"][7])["status"] == "dry_run"
    assert correction.run_correction(world["path"], world["manifests"][1])["status"] == "dry_run"


def test_unlisted_rows_keep_every_original_refusal(world, monkeypatch):
    path, manifest = world["path"], world["manifests"][0]
    before = _dump(path)
    _install(monkeypatch, [])
    with pytest.raises(correction.CorrectionRefused, match="legacy mapping is outside scope"):
        correction.run_correction(path, manifest)
    # A retained, legitimate issuer mapping on an allowlisted event is not allowlisted itself.
    _install(monkeypatch, world["entries"])
    event, _ = world["rows"][6]
    with database.get_connection() as conn:
        mapping_id = conn.execute(
            "SELECT m.mapping_id FROM event_securities m WHERE m.event_id = ? AND m.is_current = 1 "
            "AND m.security_id = ?", (event, INSURER)).fetchone()[0]
        retained = _manifest(conn, _entry(conn, event, mapping_id))
    with pytest.raises(correction.CorrectionRefused, match="legacy mapping is outside scope"):
        correction.run_correction(path, retained)
    # The genuine 国新能源 row (legacy contract) is not allowlisted either.
    with pytest.raises(correction.CorrectionRefused, match="legacy mapping is outside scope"):
        correction.run_correction(path, world["genuine_manifest"])
    assert _dump(path) == before


@pytest.mark.parametrize("field", ["event_version", "revision", "mapping_id", "mapping_version",
                                   "security_id"])
def test_manifest_must_match_the_exact_allowlist_key(world, field):
    manifest = deepcopy(world["manifests"][0])
    if field == "event_version":
        manifest[field] = 99
    elif field == "revision":
        manifest["mapping"][field] = 99
    else:
        manifest["mapping"][field] = manifest["mapping"][field] + "-x"
    before = _dump(world["path"])
    with pytest.raises(correction.CorrectionRefused):
        correction.run_correction(world["path"], manifest)
    assert _dump(world["path"]) == before


def test_manifest_evidence_count_must_equal_the_allowlisted_group(world):
    manifest = deepcopy(world["manifests"][7])
    manifest["evidence"].pop()
    with pytest.raises(correction.CorrectionRefused, match="allowlisted group"):
        correction.run_correction(world["path"], manifest)


def _drift(conn, kind, event):
    if kind == "new_evidence":
        _evidence(conn, event, CURRENCY, source="https://example.test/new")
    elif kind == "evidence_column":
        conn.execute("UPDATE event_evidence SET reliability = 0.99 WHERE event_id = ? AND "
                     "mapping_id IS NULL", (event,))
    elif kind == "event_version":
        conn.execute("UPDATE events SET version = version + 1 WHERE event_id = ?", (event,))
    elif kind == "newer_revision":
        upsert_event_security(conn, event, TARGET, relation_type="direct", direction="看空",
                              confidence=0.5, relevance=5, rationale="changed",
                              mapping_version=LEGACY, now=3_000)
    elif kind == "retained_changed":
        upsert_event_security(conn, event, INSURER, relation_type="direct", direction="看空",
                              confidence=0.5, relevance=5, rationale="changed",
                              mapping_version=LEGACY, now=3_000)
    elif kind == "extra_current_mapping":
        upsert_event_security(conn, event, "SSE:600030", relation_type="direct", direction="中性",
                              confidence=0.9, relevance=10, rationale="new", mapping_version=LEGACY,
                              now=3_000)
    elif kind == "new_member":
        member = _new_event(conn, CURRENCY, "https://example.test/late-member")
        _evidence(conn, member, CURRENCY, source="https://example.test/late-member")
        merge_events(conn, member, event, reason="late duplicate", now=3_000)
    elif kind == "member_current_mapping":
        member = conn.execute("SELECT event_id FROM events WHERE merged_into = ?", (event,)).fetchone()[0]
        row = dict(conn.execute("SELECT * FROM event_securities WHERE event_id = ? AND is_current = 1 "
                                "AND security_id = ?", (event, TARGET)).fetchone())
        row.update(mapping_id=str(uuid.uuid4()), event_id=member, revision=1)
        conn.execute(f"INSERT INTO event_securities ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                     tuple(row.values()))
    elif kind == "pending_replay":
        conn.execute("INSERT OR REPLACE INTO event_mapping_replay_dirty_roots "
                     "(root_event_id, reason, dirty_at) VALUES (?, 'test', 3000)", (event,))
    elif kind == "master_changed":
        upsert_security(conn, exchange="SSE", ticker="600999", name="招商证券",
                        security_id="SSE:600999", asset_type="equity", now=3_000)


# kind -> (entry fields that keep the reviewed value, the one check that must refuse)
DRIFTS = {
    "new_evidence": (("evidence_count", "evidence_sha256"), "allowlisted group"),
    "evidence_column": (("evidence_sha256",), "content changed since review"),
    "event_version": (("event_version",), "root identity/version changed"),
    "newer_revision": ((), "current mappings differ"),
    "retained_changed": (("retained_mappings",), "other current mappings changed"),
    "extra_current_mapping": (("retained_mappings",), "other current mappings changed"),
    "new_member": (("group", "evidence_count", "evidence_sha256"), "merged-member set or versions changed"),
    "member_current_mapping": ((), "merged member holds a current mapping"),
    "pending_replay": ((), "pending mapping replay work"),
}


@pytest.mark.parametrize("kind", sorted(DRIFTS))
def test_any_drift_from_the_reviewed_state_fails_closed_without_writes(world, kind, tmp_path, monkeypatch):
    """Only the checked aspect stays at its reviewed value, so only its own guard can refuse."""
    keep, message = DRIFTS[kind]
    event, mapping = world["rows"][6]  # a merged group with one retained mapping
    old = world["entries"][6]
    with database.get_connection() as conn:
        _drift(conn, kind, event)
        fresh = _entry(conn, event, mapping)
        reviewed = allowlist.LegacyCorrection(**{
            **fresh.__dict__, **{field: getattr(old, field) for field in keep}})
        manifest = _manifest(conn, reviewed)
        conn.commit()
    _install(monkeypatch, [reviewed])
    before = _dump(world["path"])
    with pytest.raises(correction.CorrectionRefused, match=message):
        correction.run_correction(world["path"], manifest)
    with pytest.raises(correction.CorrectionRefused):
        correction.run_correction(world["path"], manifest, apply=True, plan_sha256="0" * 64,
                                  audit_path=tmp_path / "audit-drift.jsonl")
    assert _dump(world["path"]) == before and not (tmp_path / "audit-drift.jsonl").exists()


def test_snapshot_body_that_names_the_issuer_blocks_the_correction(world, monkeypatch):
    """The frozen snapshot is re-resolved too, not only its title."""
    with database.get_connection() as conn:
        event, mapping = _seed(conn, "body-hit", body="国新能源今日公告拟回购股份。")
        _clean_dirty(conn)
        entry = _entry(conn, event, mapping)
        manifest = _manifest(conn, entry)
        conn.commit()
    _install(monkeypatch, [entry])
    with pytest.raises(correction.CorrectionRefused, match="still produces"):
        correction.run_correction(world["path"], manifest)


def test_evidence_changing_between_dry_run_and_apply_fails_closed(world, tmp_path):
    manifest = world["manifests"][1]
    plan = correction.run_correction(world["path"], manifest)["plan"]
    event = world["rows"][1][0]
    with database.get_connection() as conn:
        _evidence(conn, event, CURRENCY, source="https://example.test/arrived-after-review")
        conn.commit()
    before = _dump(world["path"])
    with pytest.raises(correction.CorrectionRefused, match="evidence"):
        correction.run_correction(world["path"], manifest, apply=True,
                                  plan_sha256=plan["plan_sha256"],
                                  audit_path=tmp_path / "audit-late.jsonl")
    assert _dump(world["path"]) == before and not (tmp_path / "audit-late.jsonl").exists()


def test_master_change_between_dry_run_and_apply_fails_closed(world, tmp_path):
    manifest = world["manifests"][0]
    plan = correction.run_correction(world["path"], manifest)["plan"]
    with database.get_connection() as conn:
        _drift(conn, "master_changed", world["rows"][0][0])
        conn.commit()
    before = _dump(world["path"])
    with pytest.raises(correction.CorrectionRefused, match="plan changed"):
        correction.run_correction(world["path"], manifest, apply=True,
                                  plan_sha256=plan["plan_sha256"],
                                  audit_path=tmp_path / "audit-master.jsonl")
    assert _dump(world["path"]) == before


def test_fixed_resolver_must_still_find_no_target_association(world, monkeypatch):
    """An allowlisted row whose title really names the issuer is refused anyway."""
    with database.get_connection() as conn:
        event, mapping = _seed(conn, "really-issuer", title=ISSUER_TITLE)
        _clean_dirty(conn)
        entry = _entry(conn, event, mapping)
        manifest = _manifest(conn, entry)
        conn.commit()
    _install(monkeypatch, [*world["entries"], entry])
    before = _dump(world["path"])
    with pytest.raises(correction.CorrectionRefused, match="still produces"):
        correction.run_correction(world["path"], manifest)
    assert _dump(world["path"]) == before


def test_retained_mapping_cannot_hide_an_unmapped_new_association(world, monkeypatch):
    """Multi-mapping rows may keep only the exact securities that are still mapped."""
    with database.get_connection() as conn:
        event, mapping = _seed(conn, "extra-hit", title="新华保险与中信证券联合公告", retained=(INSURER,))
        _clean_dirty(conn)
        entry = _entry(conn, event, mapping)
        manifest = _manifest(conn, entry)
        conn.commit()
    _install(monkeypatch, [*world["entries"], entry])
    with pytest.raises(correction.CorrectionRefused, match="still produces"):
        correction.run_correction(world["path"], manifest)


def test_write_gate_is_not_bypassed_on_the_legacy_path(world, tmp_path, monkeypatch):
    monkeypatch.setenv("AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE", "1")
    monkeypatch.setenv("AI_FILTER_MAPPING_MIGRATION_GATE_FILE", str(tmp_path / "missing-gate"))
    before = _dump(world["path"])
    with pytest.raises(database.EventMappingMigrationGateError):
        _apply(world["path"], world["manifests"][0], tmp_path, "gate")
    assert _dump(world["path"]) == before


def test_failure_after_the_flip_rolls_everything_back(world, tmp_path, monkeypatch):
    def fail(*_args):
        raise RuntimeError("injected post-write failure")
    monkeypatch.setattr(correction, "_verify_after_legacy", fail)
    before = _dump(world["path"])
    with pytest.raises(RuntimeError, match="injected"):
        _apply(world["path"], world["manifests"][0], tmp_path, "rollback")
    assert _dump(world["path"]) == before
    phases = [json.loads(line)["phase"]
              for line in (tmp_path / "audit-rollback.jsonl").read_text().splitlines()]
    assert phases == ["prepared", "rolled_back"]


def test_a_stray_write_to_another_table_is_detected_and_rolled_back(world, tmp_path, monkeypatch):
    original = correction.enrichment._mark_mapping_group_similarity_dirty

    def also_touch_signals(conn, root_id, *, dirty_at):
        conn.execute("UPDATE backtest_signals SET score = score + 1")
        return original(conn, root_id, dirty_at=dirty_at)
    monkeypatch.setattr(correction.enrichment, "_mark_mapping_group_similarity_dirty",
                        also_touch_signals)
    before = _dump(world["path"])
    with pytest.raises(sqlite3.Error):
        _apply(world["path"], world["manifests"][0], tmp_path, "stray")
    assert _dump(world["path"]) == before


def test_entries_over_the_hard_evidence_ceiling_are_refused(world, monkeypatch):
    entry = world["entries"][7]
    bigger = allowlist.LegacyCorrection(**{**entry.__dict__, "evidence_count":
                                           allowlist.LEGACY_EVIDENCE_CEILING + 1})
    _install(monkeypatch, [bigger])
    manifest = deepcopy(world["manifests"][7])
    with pytest.raises(correction.CorrectionRefused):
        correction.run_correction(world["path"], manifest)


def test_synthetic_allowlist_is_exactly_nine_examples_and_runtime_is_empty():
    assert allowlist.LEGACY_CORRECTIONS == ()
    assert allowlist.LEGACY_ALLOWLIST == {}
    entries = _example_entries()
    assert len(entries) == 9 and len({entry.key for entry in entries}) == 9
    assert {entry.mapping_id[:8] for entry in entries} == {
        "20000001", "20000002", "20000003", "20000004", "20000005", "20000006",
        "20000007", "20000008", "20000009"}
    assert {entry.security_label for entry in entries} == {TARGET}
    assert {entry.event_id[:8] for entry in entries} == {
        "10000001", "10000002", "10000003", "10000004", "10000005", "10000006",
        "10000007", "10000008", "10000009"}
    # The one event that really names 国新能源 (and its mapping) must never be listed.
    assert not any(entry.event_id.startswith("1fffffff") or entry.mapping_id.startswith("2fffffff")
                   for entry in entries)
    assert all(entry.evidence_count <= allowlist.LEGACY_EVIDENCE_CEILING for entry in entries)
    assert all(not is_current_mapping_writer(entry.mapping_version) for entry in entries)


def test_synthetic_manifests_match_only_the_test_installed_allowlist(monkeypatch):
    entries = _example_entries()
    _install(monkeypatch, entries)
    files = sorted(MANIFESTS.glob("*.json"))
    assert len(files) == 9
    by_event = {entry.event_id[:8]: entry for entry in entries}
    for file in files:
        manifest = json.loads(file.read_text())
        correction.validate_manifest(manifest)
        entry = by_event[file.stem]
        assert entry.key == (manifest["event_id"], manifest["mapping"]["mapping_id"],
                             manifest["mapping"]["revision"], manifest["mapping"]["mapping_version"])
        assert len(manifest["evidence"]) == entry.evidence_count
        assert manifest["mapping"]["security_id"] == entry.security_id
        assert len(file.read_bytes()) <= 32_768


def test_runtime_refuses_all_shipped_synthetic_manifests():
    assert allowlist.LEGACY_ALLOWLIST == {}
    for file in sorted(MANIFESTS.glob("*.json")):
        manifest = json.loads(file.read_text())
        assert "SYNTHETIC TEST EXAMPLE ONLY" in manifest["reason"]
        with pytest.raises(correction.CorrectionRefused, match="legacy mapping is outside scope"):
            correction.validate_manifest(manifest)
