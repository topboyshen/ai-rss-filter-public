"""Retained-container configuration under the actual local writer-lock protocol."""
from copy import deepcopy
import io
import json
from pathlib import Path

import pytest

from scripts import production_legacy_takeover as legacy
from scripts import retained_gateway_configuration as configuration
from services import admission_fence as gate
import test_production_legacy_takeover as legacy_fixtures
from test_retained_gateway_configuration import archive_map

case, save = legacy_fixtures.case, legacy_fixtures.save


@pytest.fixture
def archive_case(case, monkeypatch):
    p = case
    row = p.state.rows["gateway"]
    row["Image"], row["Name"], row["Mounts"] = configuration.IMAGE_ID, "/retained-web", []
    row["NetworkSettings"]["Networks"]["retained"]["NetworkID"] = "e" * 64
    data, archives = archive_map(), {}
    for index, (target, raw) in enumerate(data.items()):
        path = p.path.parent / ("gateway-archive-" + str(index) + ".tar")
        path.write_bytes(raw)
        path.chmod(0o600)
        archives[target] = path
    proof = configuration.describe(row, "e" * 64, archives)
    p.plan["containers"]["gateway"] = legacy.container_identity(row)
    p.plan["gateway"].update(original=proof, gateway_plan_sha256="f" * 64)
    save(p.path, p.plan)
    p.data, p.archive_calls = data, []
    def actual(_self, binary, route):
        p.archive_calls.append(row["State"]["Running"])
        def inspect(identifier):
            assert identifier == row["Id"]
            return deepcopy(row)
        def archive(identifier, path, consume):
            assert identifier == row["Id"]
            return consume(io.BytesIO(p.data[path]).read)
        return configuration.verify(route["original"], inspect(row["Id"]), inspect=inspect, archive=archive)
    monkeypatch.setattr(legacy.NativeTransport, "gateway_configuration", actual)
    monkeypatch.setattr(legacy.NativeTransport, "gateway_config", lambda *_a: pytest.fail("archive path must never exec old gateway"))
    return p


def test_archive_branch_retains_originals_and_rechecks_after_stop_and_during_live_query(archive_case):
    p = archive_case
    originals = {path: Path(record["path"]).read_bytes()
                 for path, record in p.plan["gateway"]["original"]["archives"].items()}
    with legacy.locked_takeover(p.path, p.evidence) as session:
        assert session.stop()["status"] == "registered_legacy_runtime_stopped"
        assert session.check_stopped()["writer_lock_held"] is True
        assert p.archive_calls == [True, False, False]
        retained = gate._read(p.evidence / "gateway.configuration.original.json")
        assert retained == p.plan["gateway"]["original"]
        assert not (p.evidence / "gateway.original").exists()
    for target, record in p.plan["gateway"]["original"]["archives"].items():
        assert Path(record["path"]).read_bytes() == originals[target]


def test_changed_live_configuration_stops_before_any_cron_or_term_write(archive_case):
    p = archive_case
    p.data[configuration.FILE_PATHS[2]] = archive_map(default=b"server { listen 5001; }\n")[configuration.FILE_PATHS[2]]
    with legacy.locked_takeover(p.path, p.evidence) as session:
        with pytest.raises(gate.AdmissionError, match="archive bytes changed"):
            session.stop()
    assert p.state.calls == [] and list(p.evidence.iterdir()) == []


def test_changed_stopped_configuration_cannot_authorize_future_lifecycle(archive_case):
    p = archive_case
    with legacy.locked_takeover(p.path, p.evidence) as session:
        session.stop()
        calls = deepcopy(p.state.calls)
        p.data[configuration.FILE_PATHS[2]] = archive_map(default=b"server { listen 5001; }\n")[configuration.FILE_PATHS[2]]
        with pytest.raises(gate.AdmissionError, match="archive bytes changed"):
            session.check_stopped()
        assert p.state.calls == calls


@pytest.mark.parametrize("change", ["missing_plan", "extra_key", "wrong_plan", "wrong_old_id"])
def test_archive_route_requires_the_exact_new_variant_before_lock_or_mutation(archive_case, change):
    p = archive_case
    route = p.plan["gateway"]
    if change == "missing_plan":
        del route["gateway_plan_sha256"]
    elif change == "extra_key":
        route["verified"] = True
    elif change == "wrong_plan":
        route["gateway_plan_sha256"] = "not-a-hash"
    else:
        route["original"]["container_identity"]["id"] = "b" * 64
    save(p.path, p.plan)
    with pytest.raises(gate.AdmissionError):
        legacy.load_plan(p.path)
    assert p.state.calls == [] and list(p.evidence.iterdir()) == []


def test_native_gateway_inspects_and_all_four_archives_share_one_deadline(monkeypatch):
    identifier, row, seen = "a" * 64, {"Id": "a" * 64}, []
    monkeypatch.setattr(legacy.time, "monotonic", lambda: 10.0)
    def capture(argv, **options):
        assert argv == ["/usr/bin/docker", "inspect", identifier]
        seen.append(("inspect", options["deadline"]))
        assert options == {"deadline": 13.0, "max_bytes": legacy.MAX_CRON}
        return json.dumps([row]).encode()
    def archive(actual_id, path, consume, *, deadline):
        assert actual_id == identifier and path in configuration.ARCHIVE_PATHS
        seen.append(("archive", deadline))
        return consume(lambda size: b"")
    def verify(_proof, initial, *, inspect, archive):
        assert initial == row and inspect(identifier) == row
        for path in configuration.ARCHIVE_PATHS:
            archive(identifier, path, lambda _read: {})
        assert inspect(identifier) == row
        return {"status": "synthetic_exact"}
    monkeypatch.setattr(legacy.bounded, "_capture", capture)
    monkeypatch.setattr(configuration, "read_native_archive", archive)
    monkeypatch.setattr(configuration, "verify", verify)
    native = legacy.NativeTransport()
    result = native.gateway_configuration("/usr/bin/docker", {"container_id": identifier, "original": {}})
    assert result == {"status": "synthetic_exact"}
    assert [kind for kind, _deadline in seen] == ["inspect", "inspect", *["archive"] * 4, "inspect"]
    assert {deadline for _kind, deadline in seen} == {13.0}


def mime_case(p):
    from test_retained_gateway_configuration import mime_archive_map
    p.data = mime_archive_map()
    paths = {}
    for index, (target, raw) in enumerate(p.data.items()):
        path = p.path.parent / f"mime-archive-{index}.tar"
        path.write_bytes(raw)
        path.chmod(0o600)
        paths[target] = path
    row = p.state.rows["gateway"]
    p.plan["gateway"]["original"] = configuration.describe(row, "e" * 64, paths,
                                                          profile=configuration.MIME_ONLY_PROFILE)
    p.plan["gateway"]["config_target"] = configuration.FILE_PATHS[0]
    save(p.path, p.plan)
    return p


def test_explicit_mime_route_rechecks_exact_two_before_and_after_stop(archive_case):
    p = mime_case(archive_case)
    with legacy.locked_takeover(p.path, p.evidence) as session:
        assert session.stop()["status"] == "registered_legacy_runtime_stopped"
        assert session.check_stopped()["writer_lock_held"] is True
    assert p.archive_calls == [True, False, False]
    retained = gate._read(p.evidence / "gateway.configuration.original.json")
    assert retained["profile"] == configuration.MIME_ONLY_PROFILE
    assert set(retained["archives"]) == set(configuration.MIME_ONLY_PATHS)


@pytest.mark.parametrize("profile", [configuration.DEFAULT_PROFILE, configuration.MIME_ONLY_PROFILE])
def test_configuration_route_target_is_exact_to_its_profile(archive_case, profile):
    p = mime_case(archive_case) if profile == configuration.MIME_ONLY_PROFILE else archive_case
    p.plan["gateway"]["config_target"] = (configuration.FILE_PATHS[2]
        if profile == configuration.MIME_ONLY_PROFILE else configuration.FILE_PATHS[0])
    save(p.path, p.plan)
    with pytest.raises(gate.AdmissionError):
        legacy.load_plan(p.path)
    assert p.state.calls == [] and p.archive_calls == []
