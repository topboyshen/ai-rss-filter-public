"""Fixed resource predicates survive refusal; no Docker or native claim."""

from copy import deepcopy
import json
import stat

import pytest

from scripts import production_sender_bootstrap_native as native
from services import admission_fence as gate
from test_production_sender_bootstrap_direct131 import _host_harness, _host_pg_row


CHECKS = {
    "actual_fixed_image", "owned_label", "private_bridge", "exact_loopback_port", "memory", "pids",
    "cpu", "not_privileged", "no_new_privileges", "not_oom", "real_healthcheck",
}
PORT_DIAGNOSTICS = {
    "requested_expected", "runtime_expected", "requested_runtime_equal", "requested_is_null", "runtime_is_null",
    "requested_empty_mapping", "runtime_empty_mapping", "runtime_only_pg_null", "requested_has_extra_ports",
    "runtime_has_extra_ports", "requested_invalid_type", "runtime_invalid_type", "pg_running", "pg_created",
}


def probe(row, root):
    return native.loopback_pg_resources(
        row, "sha256:" + "a" * 64, "owner", "owned-private", 384 * 1024**2, 64,
        diagnostic_root=root)


def read_diagnostic(root):
    path = root / "LOOPBACK_PG_CHECKS.json"
    value = json.loads(path.read_bytes())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert set(value) == {"schema", "scope", "checks", "port_diagnostics", "passed", "execution_proof"}
    assert value["schema"] == 1 and value["scope"] == "loopback_pg_resource_predicates_only"
    assert value["execution_proof"] is False
    assert set(value["checks"]) == CHECKS
    assert all(type(item) is bool for item in value["checks"].values())
    assert set(value["port_diagnostics"]) == PORT_DIAGNOSTICS
    assert all(type(item) is bool for item in value["port_diagnostics"].values())
    return value


def test_success_preserves_fixed_checks_without_claiming_native_execution(tmp_path):
    row = _host_pg_row()
    original = deepcopy(row)
    assert all(probe(row, tmp_path).values())
    value = read_diagnostic(tmp_path)
    assert value["passed"] is True and all(value["checks"].values())
    assert row == original


@pytest.mark.parametrize("change, false_key", [
    ("Image", "actual_fixed_image"), ("owner", "owned_label"), ("network", "private_bridge"),
    ("port", "exact_loopback_port"), ("memory", "memory"), ("pids", "pids"), ("cpu", "cpu"),
    ("privileged", "not_privileged"), ("security", "no_new_privileges"), ("oom", "not_oom"),
    ("health", "real_healthcheck"),
])
def test_each_false_predicate_is_preserved_before_the_same_refusal(tmp_path, change, false_key):
    row = _host_pg_row()
    if change == "Image":
        row["Image"] = "PRIVATE_IMAGE_TOKEN"
    elif change == "owner":
        row["Config"]["Labels"][native.LABEL] = "PRIVATE_OWNER_TOKEN"
    elif change == "network":
        row["HostConfig"]["NetworkMode"] = "PRIVATE_NETWORK_PATH"
    elif change == "port":
        row["HostConfig"]["PortBindings"]["5432/tcp"][0]["HostPort"] = "PRIVATE_PORT_TOKEN"
    elif change == "memory":
        row["HostConfig"]["Memory"] *= 2
    elif change == "pids":
        row["HostConfig"]["PidsLimit"] *= 2
    elif change == "cpu":
        row["HostConfig"]["NanoCpus"] *= 2
    elif change == "privileged":
        row["HostConfig"]["Privileged"] = True
    elif change == "security":
        row["HostConfig"]["SecurityOpt"] = []
    elif change == "oom":
        row["State"]["OOMKilled"] = True
    else:
        row["Config"]["Healthcheck"]["Test"] = ["CMD-SHELL", "PRIVATE_ARGV_TOKEN"]
    row["Config"]["Env"] = ["SECRET=PRIVATE_ENV_TOKEN"]
    original = deepcopy(row)
    with pytest.raises(gate.AdmissionError, match="^exact loopback PG resource/topology binding differs$"):
        probe(row, tmp_path)
    value = read_diagnostic(tmp_path)
    assert value["passed"] is False
    assert [key for key, passed in value["checks"].items() if not passed] == [false_key]
    assert "PRIVATE_" not in json.dumps(value) and row == original
    assert not (tmp_path / "PROFILE_pg.json").exists()


def test_native_launch_preserves_refused_checks_without_unsafe_profile(tmp_path):
    row = _host_pg_row()
    row["HostConfig"]["MemorySwap"] *= 2
    harness = _host_harness(tmp_path, row)
    with pytest.raises(gate.AdmissionError, match="resource/topology"):
        harness.launch("pg", row["Image"], memory=384 * 1024**2, pids=64)
    assert read_diagnostic(tmp_path)["checks"]["memory"] is False
    assert not (tmp_path / "PROFILE_pg.json").exists()
    assert [args[0] for args in harness.commands] == ["run", "inspect"]


def test_diagnostic_is_exclusively_created_and_old_original_is_unchanged(tmp_path):
    probe(_host_pg_row(), tmp_path)
    path = tmp_path / "LOOPBACK_PG_CHECKS.json"
    original = path.read_bytes(), native.export._metadata(path.stat())
    with pytest.raises(FileExistsError):
        probe(_host_pg_row(), tmp_path)
    assert (path.read_bytes(), native.export._metadata(path.stat())) == original


def test_only_explicit_diagnostic_is_exported_not_actual_profile(tmp_path):
    tmp_path.chmod(0o700)
    probe(_host_pg_row(), tmp_path)
    native._save(tmp_path / "RESULT.json", {"status": "unconfirmed", "errors": []})
    native._save(tmp_path / "PROFILE_pg.json", {"inspect": "PRIVATE_INSPECT_TOKEN"})
    packet = native.safe_export(tmp_path.resolve())
    assert set(packet["files"]) == {"RESULT.json", "LOOPBACK_PG_CHECKS.json"}
    assert packet["files"]["LOOPBACK_PG_CHECKS.json"]["content"] == read_diagnostic(tmp_path)
    assert "PRIVATE_INSPECT_TOKEN" not in json.dumps(packet)


def test_exact_running_port_diagnostic_is_positive_without_changing_the_guard(tmp_path):
    row = _host_pg_row()
    row["State"].update(Running=True, Status="running")
    original = deepcopy(row)
    probe(row, tmp_path)
    port = read_diagnostic(tmp_path)["port_diagnostics"]
    assert {key for key, value in port.items() if value} == {
        "requested_expected", "runtime_expected", "requested_runtime_equal", "pg_running"}
    assert row == original


@pytest.mark.parametrize("runtime, state, expected_true", [
    ({"5432/tcp": None}, {"Running": True, "Status": "running"}, {"runtime_only_pg_null", "pg_running"}),
    ({"5432/tcp": None}, {"Running": False, "Status": "created"}, {"runtime_only_pg_null", "pg_created"}),
    ({}, {"Running": True, "Status": "running"}, {"runtime_empty_mapping", "pg_running"}),
    (None, {"Running": False, "Status": "created"}, {"runtime_is_null", "pg_created"}),
    ({"5432/tcp": [], "PRIVATE_PORT_TOKEN": None}, {"Running": True}, {"runtime_has_extra_ports", "pg_running"}),
    ("PRIVATE_RAW_PORT_TOKEN", {"Running": True}, {"runtime_invalid_type", "pg_running"}),
    (["PRIVATE_RAW_PORT_TOKEN"], {"Running": "PRIVATE_STATE_TOKEN"}, {"runtime_invalid_type"}),
])
def test_runtime_port_refusal_is_classified_from_same_inspect_without_values_or_commands(
        tmp_path, runtime, state, expected_true):
    row = _host_pg_row()
    row["NetworkSettings"]["Ports"] = runtime
    row["State"].update(state)
    row["Config"]["Env"] = ["PRIVATE_SECRET_TOKEN"]
    original = deepcopy(row)
    harness = _host_harness(tmp_path, row)
    with pytest.raises(gate.AdmissionError, match="^exact loopback PG resource/topology binding differs$"):
        harness.launch("pg", row["Image"], memory=384 * 1024**2, pids=64)
    diagnostic = read_diagnostic(tmp_path)
    assert {key for key, value in diagnostic["port_diagnostics"].items() if value} == {
        "requested_expected", *expected_true}
    assert diagnostic["checks"]["exact_loopback_port"] is False and diagnostic["passed"] is False
    assert row == original and "PRIVATE_" not in json.dumps(diagnostic)
    assert [args[0] for args in harness.commands] == ["run", "inspect"]
    assert not (tmp_path / "PROFILE_pg.json").exists()


@pytest.mark.parametrize("value, expected_true", [
    (None, {"requested_is_null", "runtime_is_null", "requested_runtime_equal"}),
    ({}, {"requested_empty_mapping", "runtime_empty_mapping", "requested_runtime_equal"}),
    ({"5432/tcp": None}, {"runtime_only_pg_null", "requested_runtime_equal"}),
    ({"5432/tcp": [], "PRIVATE_PORT_TOKEN": []},
     {"requested_has_extra_ports", "runtime_has_extra_ports", "requested_runtime_equal"}),
    ("PRIVATE_RAW_TOKEN", {"requested_invalid_type", "runtime_invalid_type", "requested_runtime_equal"}),
])
def test_matching_but_unsafe_port_maps_remain_refused(tmp_path, value, expected_true):
    row = _host_pg_row()
    row["HostConfig"]["PortBindings"] = deepcopy(value)
    row["NetworkSettings"]["Ports"] = deepcopy(value)
    with pytest.raises(gate.AdmissionError, match="resource/topology"):
        probe(row, tmp_path)
    diagnostic = read_diagnostic(tmp_path)
    assert {key for key, item in diagnostic["port_diagnostics"].items() if item} == expected_true
    assert diagnostic["passed"] is False and diagnostic["checks"]["exact_loopback_port"] is False
    assert "PRIVATE_" not in json.dumps(diagnostic)
