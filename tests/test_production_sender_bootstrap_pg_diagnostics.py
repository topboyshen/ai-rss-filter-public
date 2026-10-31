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


def probe(row, root):
    return native.loopback_pg_resources(
        row, "sha256:" + "a" * 64, "owner", "owned-private", 384 * 1024**2, 64,
        diagnostic_root=root)


def read_diagnostic(root):
    path = root / "LOOPBACK_PG_CHECKS.json"
    value = json.loads(path.read_bytes())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert set(value) == {"schema", "scope", "checks", "passed", "execution_proof"}
    assert value["schema"] == 1 and value["scope"] == "loopback_pg_resource_predicates_only"
    assert value["execution_proof"] is False
    assert set(value["checks"]) == CHECKS
    assert all(type(item) is bool for item in value["checks"].values())
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
