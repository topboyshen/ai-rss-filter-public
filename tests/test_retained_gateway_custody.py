"""Real private archives; Docker/live capabilities below remain unit doubles."""
from copy import deepcopy
import json
import os

import pytest

from scripts import private_release_contract as private, retained_gateway_custody as custody
from scripts import retained_gateway_configuration as configuration, production_gateway_switch as gateway
from scripts.production_legacy_takeover import LegacyTakeover, container_identity
from test_private_release_contract import fixture as private_fixture, reseal_inputs, save  # noqa: F401
from test_retained_gateway_configuration import archive_map, MAIN


def record(identifier, service, project, directory):
    return {"Id": identifier, "Image": configuration.IMAGE_ID, "Name": "/" + service, "RestartCount": 0,
        "Config": {"Labels": {"com.docker.compose.project": project, "com.docker.compose.service": service,
                              "com.docker.compose.project.working_dir": str(directory)}, "User": "", "Env": [],
                   "Entrypoint": None, "Cmd": ["nginx"]},
        "HostConfig": {"GroupAdd": [], "PortBindings": {}}, "Mounts": [],
        "State": {"Running": False, "Status": "exited", "StartedAt": "2026-10-31T01:00:00Z",
                  "FinishedAt": "2026-10-31T02:00:00Z", "ExitCode": 0,
                  "Paused": False, "Restarting": False, "Dead": False, "OOMKilled": False},
        "NetworkSettings": {"Networks": {"retained-net": {"NetworkID": "b" * 64}}}}


@pytest.fixture
def custody_case(private_fixture):  # noqa: F811
    f = private_fixture
    project, directory = f["review_data"]["project"], f["config"].parent
    services = {}
    for name, role in (("candidate-api", "web"), ("candidate-worker", "worker")):
        services[name] = {"image": private.IMAGE, "mem_limit": "768m", "pids_limit": 256,
            "user": f"{os.geteuid()}:{os.getegid()}", "command": ["python", "app.py"],
            "environment": {"AI_FILTER_CONFIG_PATH": "/app/config.yaml", "AI_FILTER_ROLE": role,
                            "LLM_API_KEY": "$" + "{LLM_API_KEY:?}"},
            "volumes": [{"type": "bind", "source": private.RUNTIME, "target": "/app/config.yaml", "read_only": True}],
            "networks": ["network"]}
    f["manifest_data"].update(services=services, volumes={},
        networks={"network": {"external": True, "name": "retained-net"}})
    review = f["review_data"]
    review["health_services"] = list(services)
    review["service_mapping"] = {"candidate-api": ["previous-api"], "candidate-worker": ["previous-worker"]}
    rows = [record(str(n) * 64, service, project, review["project_directory"])
            for n, service in ((1, "previous-api"), (2, "previous-worker"), (3, "previous-gateway"))]
    old = rows[-1]
    old["HostConfig"]["PortBindings"] = {"5000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5000"},
                                                    {"HostIp": "172.17.0.1", "HostPort": "5000"}]}
    archives = {target: save(directory / f"old-gateway-{n}.tar", raw)
                for n, (target, raw) in enumerate(archive_map().items())}
    proof = configuration.describe(old, "b" * 64, archives)
    proof_path = save(directory / "old-gateway-proof.json", proof)
    new_path = save(directory / "new-nginx.conf", gateway.render_config("9" * 64, "candidate-api"))
    new_config, _raw = gateway.file_identity(new_path)
    plan = {"schema": 1, "operation_id": "9" * 64, "source_sha256": gateway.source_sha256(),
            "source_sha": review["source_sha"],
            "window": {"not_before": review["approved_at"], "expires_at": review["expires_at"]},
            "old_gateway_id": old["Id"], "old_gateway_identity": gateway.gateway_identity(old),
            "old_config": proof, "new_config": new_config, "api_alias": "candidate-api", "network_id": "b" * 64,
            "wait_seconds": 3, "max_polls": 3}
    plan_path = save(directory / "gateway-plan.json", plan)
    review["existing_topology"] = private.runtime_topology(rows)
    review["topology"] = {name: {"mounts": service["volumes"], "networks": service["networks"], "ports": []}
                          for name, service in services.items()}
    review[custody.KEY] = {"schema": 1, "kind": custody.KIND, "project": project,
        "service": "previous-gateway", "old_container_id": old["Id"], "old_gateway_identity": gateway.gateway_identity(old),
        "old_topology_sha256": custody.digest(review["existing_topology"]["previous-gateway"][0]),
        "gateway_plan_path": str(plan_path), "gateway_plan_sha256": private.sha(plan_path.read_bytes()),
        "old_configuration_proof_path": str(proof_path), "old_configuration_proof_sha256": private.sha(proof_path.read_bytes()),
        "ports": review["existing_topology"]["previous-gateway"][0]["ports"]}
    reseal_inputs(f)
    return {**f, "rows": rows, "proof": proof, "plan": plan, "plan_path": plan_path, "proof_path": proof_path}


def load(f):
    return custody.load(f["review_data"], f["manifest_data"], list(f["manifest_data"]["services"]), read_private=private.private_file)


def test_private_seal_preserves_both_originals_and_full_custody_inventory(custody_case):
    f = custody_case
    contract_path = private.seal(**{key: f[key] for key in ("root", "config", "manifest", "environment", "review", "output")})
    contract, manifest, _digest = private.load(contract_path, f["root"])
    names = {"custody-gateway-plan.json", "custody-configuration-proof.json"}
    assert names <= set(contract["files"]) and names <= set(contract["inputs"])
    assert (f["output"] / "custody-gateway-plan.json").read_bytes() == f["plan_path"].read_bytes()
    assert (f["output"] / "custody-configuration-proof.json").read_bytes() == f["proof_path"].read_bytes()
    assert set(manifest["services"]) == {"candidate-api", "candidate-worker"}
    assert set(contract["review"]["existing_topology"]) == {"previous-api", "previous-worker", "previous-gateway"}
    with f["plan_path"].open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(private.ContractError, match="source input changed"):
        private.load(contract_path, f["root"])


def test_preflight_moves_exact_ports_only_after_whole_topology_check(custody_case, monkeypatch):
    f, calls = custody_case, []
    assert load(f)["bindings"] == [{"HostIp": "127.0.0.1", "HostPort": "5000"}, {"HostIp": "172.17.0.1", "HostPort": "5000"}]
    monkeypatch.setattr(private, "inspect_project", lambda _c: deepcopy(f["rows"]))
    def run(args):
        calls.append(args)
        assert args == ["docker", "network", "inspect", "retained-net"]
        return json.dumps([{"Name": "retained-net", "Id": "b" * 64}]).encode()
    monkeypatch.setattr(private, "run", run)
    contract = {"review": f["review_data"], "app_services": list(f["manifest_data"]["services"])}
    private.preflight(contract, f["manifest_data"])
    assert len(calls) == 1
    f["rows"][0]["HostConfig"]["PortBindings"] = {"9999/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9999"}]}
    with pytest.raises(private.ContractError, match="topology drift"):
        private.preflight(contract, f["manifest_data"])
    assert len(calls) == 1


@pytest.mark.parametrize("mutation", [
    lambda f: f["review_data"][custody.KEY].update(unreviewed=True),
    lambda f: f["review_data"][custody.KEY].update(old_container_id="4" * 64),
    lambda f: f["review_data"][custody.KEY].update(project="other"),
    lambda f: f["review_data"][custody.KEY].update(gateway_plan_sha256="0" * 64),
    lambda f: f["review_data"][custody.KEY].update(old_configuration_proof_sha256="0" * 64),
    lambda f: f["review_data"]["service_mapping"]["candidate-api"].append("previous-gateway"),
    lambda f: f["manifest_data"]["services"]["candidate-api"].update(ports=[{"target": 5000}]),
    lambda f: f["manifest_data"]["services"]["candidate-worker"]["environment"].update(AI_FILTER_ROLE="web"),
    lambda f: f["review_data"]["existing_topology"].update(extra=[deepcopy(f["review_data"]["existing_topology"]["previous-api"][0])]),
])
def test_ambiguous_or_unsealed_variants_never_become_custody(custody_case, mutation):
    mutation(custody_case)
    with pytest.raises(ValueError):
        load(custody_case)


@pytest.mark.parametrize("mutation", [
    lambda row: row.update(Id="4" * 64),
    lambda row: row.update(RestartCount=1),
    lambda row: row["Config"]["Labels"].update({"com.docker.compose.project": "shared-npm"}),
    lambda row: row["Config"]["Labels"].update({"com.docker.compose.oneoff": "True"}),
    lambda row: row["NetworkSettings"]["Networks"]["retained-net"].update(NetworkID="c" * 64),
    lambda row: row.update(Mounts=[{"Destination": "/unreviewed"}]),
])
def test_service_name_does_not_replace_exact_runtime(custody_case, mutation):
    f, held = custody_case, load(custody_case)
    mutation(f["rows"][-1])
    with pytest.raises(ValueError):
        custody.verify_records(held, f["review_data"], f["rows"])


def legacy_capability(f, digest="e" * 64):
    # Exact class, but explicitly not a real retained OS lock/native proof.
    cap = LegacyTakeover.__new__(LegacyTakeover)
    cap.plan = {"gateway": {"container_id": f["rows"][-1]["Id"], "private_contract_sha256": digest,
        "gateway_plan_sha256": f["review_data"][custody.KEY]["gateway_plan_sha256"], "original": f["proof"]}}
    cap.check_stopped = lambda: {"containers": {"gateway": {"running": False, "status": "exited", "exit_code": 0,
        "finished_at": f["rows"][-1]["State"]["FinishedAt"], "identity": container_identity(f["rows"][-1])}},
        "writer_lock_held": True}
    return cap


def test_live_capability_and_raw_plan_hash_are_not_caller_receipts(custody_case):
    f, held = custody_case, load(custody_case)
    cap = legacy_capability(f)
    custody.verify_legacy(held, f["rows"][-1], cap, private_digest="e" * 64)
    with pytest.raises(ValueError):
        custody.verify_legacy(held, f["rows"][-1], {"stopped": True}, private_digest="e" * 64)
    cap.plan["gateway"]["gateway_plan_sha256"] = custody.digest(f["plan"])
    with pytest.raises(ValueError):
        custody.verify_legacy(held, f["rows"][-1], cap, private_digest="e" * 64)


@pytest.mark.parametrize("state,value", [("Running", True), ("Status", "created"), ("OOMKilled", True),
                                        ("ExitCode", 137), ("FinishedAt", "0001-01-01T00:00:00Z")])
def test_exit_alone_is_not_normal_stop_proof(custody_case, state, value):
    f, held = custody_case, load(custody_case)
    cap = legacy_capability(f)
    f["rows"][-1]["State"][state] = value
    with pytest.raises(ValueError):
        custody.verify_legacy(held, f["rows"][-1], cap, private_digest="e" * 64)


def test_no_declaration_does_not_read_or_expand_old_contract():
    assert custody.load({}, {}, [], read_private=lambda _p: pytest.fail("unexpected read")) is None


def bind_root_policy(f, *, user="0:0", cap_drop=None):
    old = f["rows"][-1]
    old["Config"]["User"] = user
    old["HostConfig"].update(Memory=128 * 1024**2, MemorySwap=128 * 1024**2, PidsLimit=64,
        NanoCpus=0, ReadonlyRootfs=False, SecurityOpt=["no-new-privileges:true"], IpcMode="private",
        CapDrop=["ALL"] if cap_drop is None else cap_drop)
    directory = f["config"].parent
    archives = {target: save(directory / f"root-gateway-{number}.tar", raw)
                for number, (target, raw) in enumerate(archive_map(main=b"user root;\n" + MAIN).items())}
    proof = configuration.describe(old, "b" * 64, archives)
    f["proof"] = proof
    f["plan"].update(old_config=proof, old_gateway_identity=gateway.gateway_identity(old), worker_policy=gateway.ROOT_WORKER_POLICY)
    save(f["proof_path"], proof)
    config_path = f["plan"]["new_config"]["path"]
    save(directory / "new-nginx.conf", gateway.render_config("9" * 64, "candidate-api", worker_policy=gateway.ROOT_WORKER_POLICY))
    f["plan"]["new_config"] = gateway.file_identity(config_path)[0]
    save(f["plan_path"], f["plan"])
    review, declaration = f["review_data"], f["review_data"][custody.KEY]
    review["existing_topology"] = private.runtime_topology(f["rows"])
    declaration.update(old_gateway_identity=gateway.gateway_identity(old),
        old_topology_sha256=custody.digest(review["existing_topology"][declaration["service"]][0]),
        old_configuration_proof_sha256=private.sha(f["proof_path"].read_bytes()),
        gateway_plan_sha256=private.sha(f["plan_path"].read_bytes()))
    reseal_inputs(f)
    return f


def test_root_worker_variant_seals_exact_template_and_rederives_actual_execution(custody_case):
    f = bind_root_policy(custody_case)
    held = load(f)
    assert held["plan"]["worker_policy"] == gateway.ROOT_WORKER_POLICY
    assert custody.verify_records(held, f["review_data"], f["rows"]) == f["rows"][-1]
    contract_path = private.seal(**{key: f[key] for key in ("root", "config", "manifest", "environment", "review", "output")})
    contract, _manifest, _digest = private.load(contract_path, f["root"])
    assert contract["review"][custody.KEY]["gateway_plan_sha256"] == private.sha(f["plan_path"].read_bytes())
    assert (f["output"] / "custody-gateway-plan.json").read_bytes() == f["plan_path"].read_bytes()


@pytest.mark.parametrize("user,cap_drop", [("", ["ALL"]), ("101:101", ["ALL"]), ("root", ["ALL"]), ("0:0", []), ("0:0", ["CHOWN"])])
def test_root_custody_field_and_rebound_static_proof_are_not_actual_execution_authority(custody_case, user, cap_drop):
    f = bind_root_policy(custody_case, user=user, cap_drop=cap_drop)
    held = load(f)  # Static sealed files cannot prove the runtime's worker policy.
    from services import admission_fence as gate
    with pytest.raises(gate.AdmissionError, match="actual retained worker policy differs"):
        custody.verify_records(held, f["review_data"], f["rows"])


def test_custody_root_plan_cannot_substitute_default_new_template(custody_case):
    f = bind_root_policy(custody_case)
    save(f["config"].parent / "new-nginx.conf", gateway.render_config("9" * 64, "candidate-api"))
    f["plan"]["new_config"] = gateway.file_identity(f["plan"]["new_config"]["path"])[0]
    save(f["plan_path"], f["plan"])
    f["review_data"][custody.KEY]["gateway_plan_sha256"] = private.sha(f["plan_path"].read_bytes())
    reseal_inputs(f)
    with pytest.raises(ValueError, match="retained gateway custody differs"):
        load(f)


def rebind_mime_profile(f):
    from test_retained_gateway_configuration import mime_archive_map
    directory = f["config"].parent
    archives = {target: save(directory / f"mime-gateway-{number}.tar", raw)
                for number, (target, raw) in enumerate(mime_archive_map().items())}
    proof = configuration.describe(f["rows"][-1], "b" * 64, archives, profile=configuration.MIME_ONLY_PROFILE)
    f["proof"] = proof
    f["plan"]["old_config"] = proof
    save(f["proof_path"], proof)
    save(f["plan_path"], f["plan"])
    declaration = f["review_data"][custody.KEY]
    declaration["old_configuration_proof_sha256"] = private.sha(f["proof_path"].read_bytes())
    declaration["gateway_plan_sha256"] = private.sha(f["plan_path"].read_bytes())
    reseal_inputs(f)
    return f


def test_private_custody_seal_preserves_explicit_mime_profile_and_originals(custody_case):
    f = rebind_mime_profile(custody_case)
    held = load(f)
    assert held["proof"]["profile"] == held["configuration"]["profile"] == configuration.MIME_ONLY_PROFILE
    assert len(held["configuration"]["files"]) == 2 and held["plan"]["old_config"] == held["proof"]
    contract_path = private.seal(**{key: f[key] for key in ("root", "config", "manifest", "environment", "review", "output")})
    contract, _manifest, _digest = private.load(contract_path, f["root"])
    assert (f["output"] / "custody-configuration-proof.json").read_bytes() == f["proof_path"].read_bytes()
    assert contract["review"][custody.KEY]["old_configuration_proof_sha256"] == private.sha(f["proof_path"].read_bytes())


@pytest.mark.parametrize("fault", ["plan_profile", "proof_profile", "proof_source", "old_review_hash", "old_plan_hash"])
def test_custody_mime_profile_cannot_cross_sealed_plan_source_or_review(custody_case, fault):
    original = deepcopy(custody_case["review_data"][custody.KEY])
    f = rebind_mime_profile(custody_case)
    declaration = f["review_data"][custody.KEY]
    if fault == "plan_profile":
        f["plan"]["old_config"] = deepcopy(f["proof"])
        f["plan"]["old_config"].pop("profile")
        save(f["plan_path"], f["plan"])
        declaration["gateway_plan_sha256"] = private.sha(f["plan_path"].read_bytes())
    elif fault in {"proof_profile", "proof_source"}:
        if fault == "proof_profile":
            f["proof"].pop("profile")
        else:
            f["proof"]["source_sha256"] = "f" * 64
        save(f["proof_path"], f["proof"])
        declaration["old_configuration_proof_sha256"] = private.sha(f["proof_path"].read_bytes())
    elif fault == "old_review_hash":
        declaration["old_configuration_proof_sha256"] = original["old_configuration_proof_sha256"]
    else:
        declaration["gateway_plan_sha256"] = original["gateway_plan_sha256"]
    reseal_inputs(f)
    from services import admission_fence as gate
    with pytest.raises((ValueError, gate.AdmissionError)):
        load(f)
