"""Joint actual SQLite initializer + raw drain + old/new frozen file locks.

Docker/Miniflux producer identity remains a protocol fixture here. The native
runner separately collects actual Docker identity; real PG is a separate job.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import time

import pytest

import test_frozen_cutover as fixtures
from services import admission_fence as gate
from services import controlled_startup as startup


def _temporary_case(monkeypatch):
    # The evidence archive may live under RUNNER_TEMP, outside the isolation
    # initializer's allowed temporary roots. It is an archive, not a live DB.
    case = fixtures.FrozenCutoverTests()
    with monkeypatch.context() as environment:
        environment.delenv("AI_FILTER_CUTOVER_EVIDENCE_DIR", raising=False)
        case.setUp()
    assert Path(tempfile.gettempdir()).resolve() in case.storage.db.resolve().parents
    return case


def _retain_case(case, archive):
    if archive:
        shutil.copytree(case.root, Path(archive) / case.root.name)


def test_archive_parent_never_becomes_live_startup_database(monkeypatch, tmp_path):
    archive = tmp_path / "private-archive"
    archive.mkdir(mode=0o700)
    monkeypatch.setenv("AI_FILTER_CUTOVER_EVIDENCE_DIR", str(archive))
    case = _temporary_case(monkeypatch)
    assert archive not in case.storage.db.resolve().parents
    before = case.storage.db.read_bytes()
    _retain_case(case, archive)
    assert (archive / case.root.name / "cache.db").read_bytes() == before
    assert case.storage.db.read_bytes() == before


def test_actual_initialization_inside_retained_drain_locks(monkeypatch):
    archive = os.environ.get("AI_FILTER_CUTOVER_EVIDENCE_DIR")
    case = _temporary_case(monkeypatch)
    root, db_path = case.root, case.storage.db.resolve()
    from db import database
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    monkeypatch.setenv("AI_FILTER_DB_PATH", str(db_path))
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("external network prohibited"))
    new_root, binding = root / "new-fence", case.plan["new_binding"]
    now = int(time.time())
    gate.provision(new_root, binding, {"not_before": now - 1, "expires_at": now + 120,
                                      "max_admissions": 10, "max_bytes": 10000})
    fence = gate.Fence(new_root, binding)
    permit = {"schema": 1, "scope": "isolation", "run_id": case.plan["run_id"],
              "operation_id": case.plan["startup_operation_id"], "manifest_sha256": gate._hash(fence._manifest()),
              "binding": binding, "runtime_sources_sha256": startup.runtime_sources_sha256(),
              "db_path": str(db_path), "roles": {"web": "1" * 64, "worker": "2" * 64},
              "not_before": now - 1, "expires_at": now + 120}
    monkeypatch.setenv("AI_FILTER_ADMISSION_MODE", "enforced")
    monkeypatch.setenv("AI_FILTER_ADMISSION_ROOT", str(new_root))
    monkeypatch.setenv("AI_FILTER_ADMISSION_BINDING", json.dumps(binding))
    monkeypatch.setenv(startup.ENV_OPERATION, permit["operation_id"])

    class ActualStartup:
        operation_id = permit["operation_id"]

        def __init__(self):
            self.binding = binding
            self.started = 0

        def start_frozen(self, intent_sha256):
            self.started += 1
            startup.authorize(fence, permit)
            startup.migrate(fence, self.operation_id)
            import app
            monkeypatch.setenv("AI_FILTER_ROLE", "web")
            app.initialize_app()
            monkeypatch.setenv("AI_FILTER_ROLE", "worker")
            startup.initialize_runtime_database("worker", database.init_db, "worker_database_initialize")
            startup.require_startup_complete(fence)
            assert app.app.test_client().post("/not-a-producer-bypass").status_code == 503
            self.result = {"status": "completed_frozen", "binding": binding, "operation_id": self.operation_id,
                           "intent_sha256": intent_sha256, "durable": True, "business_open": False}
            return self.result

        def query_frozen(self, intent_sha256):
            startup.query_migration(fence, self.operation_id)
            assert self.result["intent_sha256"] == intent_sha256
            return self.result

    driver = ActualStartup()
    try:
        controller = fixtures.cutover.FrozenCutover(case.plan, case.runtime, case.fence, driver,
                                                    root / "joint-evidence", postgres=case.storage.sender)
        result = controller.run()
        assert result["status"] == "completed_frozen" and not result["business_open"]
        assert fence.snapshot()["phase"] == case.fence.snapshot()["phase"] == "frozen"
        assert controller.query() == result and driver.started == 1
        with pytest.raises(gate.AdmissionClosed):
            fence.reserve_lease("ordinary_work_still_closed")
    finally:
        for key in list(startup._OWNERS):
            if key[0] == str(new_root):
                fd, _ = startup._OWNERS.pop(key)
                os.close(fd)
        # Preserve both success and failure without running startup against an
        # archive outside approved temp roots or weakening production checks.
        _retain_case(case, archive)
