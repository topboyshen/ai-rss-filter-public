"""Host-only manifest variant; no Docker, migration or native claim."""
from copy import deepcopy
import hashlib

import pytest

from test_production_sender_bootstrap import prepared  # noqa: F401
from test_production_sender_host_loopback_plan import capability
from scripts import production_sender_bootstrap as bootstrap
from scripts import sender_host_loopback_pg as local
from scripts.private_release_contract import ContractError
from services import admission_fence as gate


@pytest.fixture
def host_case(request):
    p = request.getfixturevalue("prepared")
    p.plan["network"].update(Name="host", Driver="host")
    p.service.pop("networks")
    p.service.update(network_mode="host", volumes=[])
    p.plan["mount_identities"] = {}
    p.manifest["networks"] = {}
    dsn = p.service["environment"]["DATABASE_URL"]
    p.plan["postgres_transport"] = capability(hashlib.sha256(dsn.encode()).hexdigest())
    p.old["Mounts"] = p.row["Mounts"] = []
    p.old["NetworkSettings"]["Networks"] = {"host": {"NetworkID": p.plan["network"]["Id"]}}
    p.old["HostConfig"]["NetworkMode"] = p.row["HostConfig"]["NetworkMode"] = "host"
    p.plan["compose"] = p.freeze("host-compose.json", gate._encoded(p.manifest))
    return p


def test_host_manifest_is_explicit_and_created_does_not_claim_live_namespace(host_case, monkeypatch):
    p = host_case
    calls = []
    monkeypatch.setattr(local.HostLoopbackPostgres, "check_sender", lambda self, row: calls.append(deepcopy(row)))
    created = bootstrap.create(p.legacy, p.plan, p.root)
    assert created["status"] == "created_unstarted" and calls == []
    bootstrap.start(p.legacy, p.plan, p.root)
    assert len(calls) == 1 and calls[0]["State"]["Running"] is True
    assert all(not any(x in command for x in ("rm", "kill", "restart")) for command in p.calls)


@pytest.mark.parametrize("change", ["alias", "both_network_fields", "port", "mount", "no_capability", "capability_dsn",
                                    "bridge_mode", "extra_service", "external_host_network", "root_writable", "capacity"])
def test_host_variant_cannot_relax_existing_restrictions(host_case, change):
    p = host_case
    if change == "alias":
        p.service.pop("network_mode")
        p.service["networks"] = ["sender"]
    elif change == "both_network_fields":
        p.service["networks"] = ["sender"]
    elif change == "port":
        p.service["ports"] = [{"target": 8080, "protocol": "tcp", "host_ip": "127.0.0.1", "published": 8080}]
    elif change == "mount":
        p.service["volumes"] = [{"type": "bind", "source": str(p.root), "target": "/tmp", "read_only": True}]
    elif change == "no_capability":
        del p.plan["postgres_transport"]
    elif change == "capability_dsn":
        p.plan["postgres_transport"]["dsn_sha256"] = "0" * 64
    elif change == "bridge_mode":
        p.service["network_mode"] = "bridge"
    elif change == "extra_service":
        p.manifest["services"]["other"] = deepcopy(p.service)
    elif change == "external_host_network":
        p.manifest["networks"] = {"sender": {"name": "host", "external": True}}
    elif change == "root_writable":
        p.service["read_only"] = False
    else:
        p.service["pids_limit"] = 257
    with pytest.raises((ValueError, gate.AdmissionError)):
        bootstrap._manifest(p.plan, gate._encoded(p.manifest), {})
    assert not any(command[0] == "compose" for command in p.calls)


def test_component_reconstructed_from_actual_runtime_host_mode(host_case):
    p = host_case
    p.row["State"].update(Running=True, StartedAt="2026-10-31T00:00:00Z")
    result = bootstrap._component(p.row)
    assert result["mounts"] == [] and "external_postgres" not in result
    assert result["host_loopback_postgres"]["network_mode"] == "host"
    assert result["host_loopback_postgres"]["dsn_sha256"] == p.plan["postgres_transport"]["dsn_sha256"]


def test_live_guard_failure_does_not_report_started(host_case, monkeypatch):
    p = host_case
    bootstrap.create(p.legacy, p.plan, p.root)
    def reject(self, row):
        raise ValueError("synthetic host namespace changed after start")
    monkeypatch.setattr(local.HostLoopbackPostgres, "check_sender", reject)
    with pytest.raises(ValueError, match="host namespace changed"):
        bootstrap.start(p.legacy, p.plan, p.root)
    assert (p.root / "start.intent.json").is_file()
    assert not (p.root / "started.json").exists()
    with pytest.raises(ContractError, match="runtime image identity mismatch"):
        bootstrap.start(p.legacy, p.plan, p.root)
    assert [x[0] for x in p.calls].count("start") == 1


@pytest.mark.parametrize("key", ["PGHOSTADDR", "PGPORT", "PGSERVICE", "PGOPTIONS", "PGPASSWORD"])
def test_host_manifest_and_runtime_component_reject_pg_overrides(host_case, key):
    p = host_case
    p.service["environment"][key] = "synthetic-no-secret"
    with pytest.raises(gate.AdmissionError, match="environment overrides"):
        bootstrap._manifest(p.plan, gate._encoded(p.manifest), {})
    p.row["Config"]["Env"].append(key + "=synthetic-no-secret")
    with pytest.raises(gate.AdmissionError, match="environment overrides"):
        bootstrap._component(p.row)
    assert not any(command[0] == "compose" for command in p.calls)
