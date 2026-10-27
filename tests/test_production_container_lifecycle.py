"""Real migration/SQLite/files/flock; Docker is a narrow unit transport double."""
from copy import deepcopy

import pytest

from test_production_migration import prepared  # noqa: F401
from services import admission_fence as gate, production_migration as migration
from services import production_container_lifecycle as lifecycle


def container(identifier, name, role, image):
    return {"Id": identifier, "Image": image, "RestartCount": 0,
        "Config": {"User": "0:0", "Labels": {"com.docker.compose.service": name}, "Env": ["AI_FILTER_ROLE=" + role]},
        "HostConfig": {"GroupAdd": [], "PortBindings": {}}, "Mounts": [], "NetworkSettings": {"Networks": {}},
        "State": {"Status": "created", "Running": False, "Paused": False, "Restarting": False,
                  "Dead": False, "StartedAt": "0001-01-01T00:00:00Z", "FinishedAt": "0001-01-01T00:00:00Z"}}


@pytest.fixture
def setup_lifecycle(prepared, monkeypatch):  # noqa: F811
    p = prepared
    dirs = [p["directory"] / name for name in ("grants", "slots", "lifecycle")]
    for directory in dirs:
        directory.mkdir(mode=0o700)
    runtime_root, state, evidence = dirs
    rows = [container("1" * 64, "previous-api", "web", "sha256:" + "c" * 64),
            container("2" * 64, "previous-worker", "worker", "sha256:" + "c" * 64)]
    contract = {"app_services": ["candidate-api", "candidate-worker"], "review": {
        "existing_topology": lifecycle.private.runtime_topology(rows),
        "service_mapping": {"candidate-api": ["previous-api"], "candidate-worker": ["previous-worker"]}}}
    manifest = {"services": {"candidate-api": {"environment": {"AI_FILTER_ROLE": "web"}},
                             "candidate-worker": {"environment": {"AI_FILTER_ROLE": "worker"}}}}
    calls = []
    monkeypatch.setattr(lifecycle.private, "load", lambda *a, **kw: (contract, manifest, "e" * 64))
    monkeypatch.setattr(lifecycle.private, "preflight", lambda *a: None)
    monkeypatch.setattr(lifecycle.private, "inspect_project", lambda *a: deepcopy(rows))
    def compose(*args):
        command = args[-1]
        calls.append(command)
        assert "--no-recreate" in command and "up" not in command and "--remove-orphans" not in command
        rows.extend([container("3" * 64, "candidate-api", "web", p["permit"]["source"]["image_id"]),
                     container("4" * 64, "candidate-worker", "worker", p["permit"]["source"]["image_id"])])
    monkeypatch.setattr(lifecycle.private, "compose", compose)
    def verify(_session, _root, _state, _descriptor, identifier, role, *, running):
        row = next(r for r in rows if r["Id"] == identifier)
        gate._require(row["Image"] == p["permit"]["source"]["image_id"] and row["RestartCount"] == 0
                      and row["State"]["Running"] is running, "synthetic execution drift")
        return row
    monkeypatch.setattr(lifecycle.startup, "verify_container", verify)
    def run(command):
        calls.append(command)
        assert command == ["docker", "start", "3" * 64, "4" * 64]
        for row in rows[2:]:
            row["State"].update(Status="running", Running=True, StartedAt="2026-10-27T00:00:00Z")
        for path in runtime_root.glob("*.grant.json"):
            grant = gate._read(path)
            gate._write(state / f"{grant['slot']}.0.json", grant)
        return b""
    monkeypatch.setattr(lifecycle.private, "run", run)
    def query(_session, _root, _state, *, slot, attempt, operation_id):
        grant = gate._read(runtime_root / f"{slot}.0.grant.json")
        assert attempt == 0 and operation_id == grant["attempt_operation_id"]
        return {"status": "closed_initialized", "receipt_sha256": gate._hash(grant), "business_open": False}
    monkeypatch.setattr(lifecycle.startup, "query", query)
    paths = {"database": str(p["db"]), "config": p["permit"]["config"]["path"],
             "freeze": str(p["fence"].root), "state": str(state)}
    with migration.locked_session(p["root"], p["permit"]) as session:
        session.authorize()
        session.migrate()
        yield {**p, "session": session, "dirs": dirs, "paths": paths, "rows": rows,
               "calls": calls, "contract": contract, "manifest": manifest, "run": run}


def create(p):
    return lifecycle.create(p["session"], *p["dirs"], operation_id="9" * 64, paths=p["paths"], web_workers=2, threads=4)


def test_create_grant_start_query_preserves_previous_ids(setup_lifecycle):
    p = setup_lifecycle
    previous = deepcopy(p["rows"])
    created = create(p)
    assert created["containers"] == {"web": "3" * 64, "worker": "4" * 64}
    assert all(not r["State"]["Running"] for r in p["rows"])
    result = lifecycle.start(p["session"], *p["dirs"], operation_id="9" * 64)
    assert result["status"] == "closed_initialized" and not result["business_open"]
    assert p["rows"][:2] == previous
    assert lifecycle.query(p["session"], *p["dirs"], operation_id="9" * 64) == result
    assert len(p["calls"]) == 2
    with pytest.raises(gate.AdmissionError, match="already attempted"):
        lifecycle.start(p["session"], *p["dirs"], operation_id="9" * 64)


@pytest.mark.parametrize("field,value", [("Running", True), ("Paused", True), ("Restarting", True), ("Status", "dead")])
def test_previous_must_be_stopped_before_any_create(setup_lifecycle, field, value):
    p = setup_lifecycle
    p["rows"][0]["State"][field] = value
    with pytest.raises(gate.AdmissionError, match="safely stopped"):
        create(p)
    assert not p["calls"] and not (p["dirs"][2] / "create.intent.json").exists()


def test_create_ack_loss_never_recreates_and_retains_evidence(setup_lifecycle, monkeypatch):
    p = setup_lifecycle
    original = lifecycle.private.compose
    def lost(*args):
        original(*args)
        raise TimeoutError("synthetic create ACK unknown")
    monkeypatch.setattr(lifecycle.private, "compose", lost)
    with pytest.raises(TimeoutError):
        create(p)
    assert (p["dirs"][2] / "create.intent.json").exists() and len(p["rows"]) == 4
    with pytest.raises(gate.AdmissionError, match="already attempted"):
        create(p)
    assert len(p["calls"]) == 1


def test_start_ack_loss_query_confirms_without_second_start(setup_lifecycle, monkeypatch):
    p = setup_lifecycle
    create(p)
    def lost(command):
        p["run"](command)
        raise TimeoutError("synthetic start ACK unknown")
    monkeypatch.setattr(lifecycle.private, "run", lost)
    with pytest.raises(TimeoutError):
        lifecycle.start(p["session"], *p["dirs"], operation_id="9" * 64)
    with pytest.raises(gate.AdmissionError, match="already attempted"):
        lifecycle.start(p["session"], *p["dirs"], operation_id="9" * 64)
    result = lifecycle.query(p["session"], *p["dirs"], operation_id="9" * 64)
    assert result["status"] == "closed_initialized" and len(p["calls"]) == 2


@pytest.mark.parametrize("drift", ["old_restart", "old_running", "new_identity", "new_restart", "foreign", "grant"])
def test_identity_changes_block_start_without_replacing_anything(setup_lifecycle, drift):
    p = setup_lifecycle
    create(p)
    if drift == "old_restart":
        p["rows"][0]["RestartCount"] = 1
    elif drift == "old_running":
        p["rows"][0]["State"]["Running"] = True
    elif drift == "new_identity":
        p["rows"][2]["Id"] = "a" * 64
    elif drift == "new_restart":
        p["rows"][2]["RestartCount"] = 1
    elif drift == "foreign":
        p["rows"].append(container("a" * 64, "foreign", "web", "sha256:" + "b" * 64))
    else:
        path = p["dirs"][0] / "web-1.0.grant.json"
        grant = gate._read(path)
        gate._write(path, {**grant, "container_id": "a" * 64})
    with pytest.raises(gate.AdmissionError):
        lifecycle.start(p["session"], *p["dirs"], operation_id="9" * 64)
    assert len(p["calls"]) == 1


def test_partial_grant_failure_preserves_all_prior_material(setup_lifecycle, monkeypatch):
    p = setup_lifecycle
    original = lifecycle.startup.grant
    def fail(*args, **kwargs):
        if kwargs["slot"] == "web-2":
            raise OSError("synthetic directory sync failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(lifecycle.startup, "grant", fail)
    with pytest.raises(OSError):
        create(p)
    assert (p["dirs"][0] / "web-1.0.grant.json").exists()
    assert not (p["dirs"][2] / "created.json").exists()
    with pytest.raises(gate.AdmissionError, match="already attempted"):
        create(p)
    assert len(p["calls"]) == 1
