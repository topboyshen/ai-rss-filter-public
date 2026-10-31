"""Exact custody of one retained same-project gateway; no write authority.

Only immutable private plan/configuration evidence is loaded here. Live stop
and switch capabilities must still be supplied by the lifecycle caller.
"""
from __future__ import annotations

import hashlib
import importlib.util
import ipaddress
import json
from pathlib import Path
import re

KEY = "retained_gateway_custody"
KIND = "dedicated_same_project_gateway"
FILES = {"custody-gateway-plan.json": ("gateway_plan_path", "gateway_plan_sha256"),
         "custody-configuration-proof.json": ("old_configuration_proof_path", "old_configuration_proof_sha256")}
FIELDS = {"schema", "kind", "project", "service", "old_container_id", "old_gateway_identity",
          "old_topology_sha256", "gateway_plan_path", "gateway_plan_sha256",
          "old_configuration_proof_path", "old_configuration_proof_sha256", "ports"}


def require(value):
    if not value:
        raise ValueError("retained gateway custody differs")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def parsed(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result)
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _v: require(False))


def configuration_helper():
    # private_release_contract also runs as a standalone -I script. Never load
    # an ambient module in place of the source-bound sibling.
    path = Path(__file__).with_name("retained_gateway_configuration.py")
    spec = importlib.util.spec_from_file_location("reviewed_retained_gateway_configuration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def port_bindings(ports):
    require(isinstance(ports, list) and len(ports) == 2)
    result = []
    for port in ports:
        require(isinstance(port, dict) and set(port) == {"target", "host_ip", "published"}
                and port["target"] == "5000/tcp" and port["published"] == "5000")
        address = ipaddress.ip_address(port["host_ip"])
        require(address.version == 4 and (address.is_private or address.is_loopback)
                and not address.is_unspecified and not address.is_multicast
                and str(address) == port["host_ip"])
        result.append({"HostIp": str(address), "HostPort": "5000"})
    require(len({item["HostIp"] for item in result}) == 2)
    require(ports == sorted(ports, key=canonical))
    return sorted(result, key=lambda item: (item["HostIp"], item["HostPort"]))


def load(review, manifest, apps, *, read_private):
    """Validate exact declaration and real private original bytes, or no variant."""
    if KEY not in review:
        return None
    value = review[KEY]
    require(isinstance(value, dict) and set(value) == FIELDS and type(value["schema"]) is int
            and value["schema"] == 1 and value["kind"] == KIND
            and value["project"] == review["project"]
            and isinstance(value["service"], str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value["service"])
            and re.fullmatch(r"[0-9a-f]{64}", value["old_container_id"]))
    require(len(apps) == 2 and set(apps) == set(manifest["services"])
            and set(review["service_mapping"]) == set(apps)
            and value["service"] not in manifest["services"]
            and all(value["service"] not in names for names in review["service_mapping"].values()))
    mapped = [name for names in review["service_mapping"].values() for name in names]
    require(all(len(names) == 1 for names in review["service_mapping"].values())
            and len(set(mapped)) == 2)
    roles = [manifest["services"][name].get("environment", {}).get("AI_FILTER_ROLE") for name in apps]
    require(sorted(roles) == ["web", "worker"])
    topology = review["existing_topology"]
    require(set(topology) == set(mapped) | {value["service"]}
            and all(len(topology[name]) == 1 for name in mapped))
    original = topology.get(value["service"])
    require(isinstance(original, list) and len(original) == 1 and original[0]["id"] == value["old_container_id"]
            and digest(original[0]) == value["old_topology_sha256"]
            and original[0]["mounts"] == [] and original[0]["ports"] == value["ports"])
    require(sum(item["id"] == value["old_container_id"] for records in topology.values() for item in records) == 1)
    bindings = port_bindings(value["ports"])
    require(all(not service.get("ports") for service in manifest["services"].values()))
    files, inputs = {}, {}
    for name, (path_key, sha_key) in FILES.items():
        require(isinstance(value[path_key], str) and re.fullmatch(r"[0-9a-f]{64}", value[sha_key]))
        raw, metadata = read_private(value[path_key])
        require(hashlib.sha256(raw).hexdigest() == value[sha_key])
        files[name], inputs[name] = raw, metadata
    require(value["gateway_plan_path"] != value["old_configuration_proof_path"])
    plan, proof = (parsed(files[name]) for name in FILES)
    from scripts.production_gateway_switch import PLAN_KEYS, file_identity, render_config, source_sha256
    require(isinstance(plan, dict) and set(plan) == PLAN_KEYS and type(plan["schema"]) is int and plan["schema"] == 1
            and plan["source_sha256"] == source_sha256()
            and re.fullmatch(r"[0-9a-f]{64}", plan["operation_id"])
            and type(plan["wait_seconds"]) is int and 1 <= plan["wait_seconds"] <= 30
            and type(plan["max_polls"]) is int and 1 <= plan["max_polls"] <= 16
            and isinstance(plan["window"], dict) and set(plan["window"]) == {"not_before", "expires_at"}
            and all(type(item) is int for item in plan["window"].values())
            and review["approved_at"] <= plan["window"]["not_before"]
            < plan["window"]["expires_at"] <= review["expires_at"]
            and plan["api_alias"] == next(name for name in apps
                if manifest["services"][name]["environment"]["AI_FILTER_ROLE"] == "web"))
    new_config, new_bytes = file_identity(plan["new_config"]["path"])
    require(new_config == plan["new_config"] and new_bytes == render_config(plan["operation_id"], plan["api_alias"]))
    helper = configuration_helper()
    configuration = helper.validate(proof)
    require(proof["kind"] == helper.KIND and configuration["container_identity"] == value["old_gateway_identity"]
            and configuration["container_identity"]["id"] == value["old_container_id"]
            and plan["old_gateway_id"] == value["old_container_id"]
            and plan["old_gateway_identity"] == value["old_gateway_identity"]
            and plan["old_config"] == proof and plan["source_sha"] == review["source_sha"]
            and plan["network_id"] == configuration["network_id"]
            and configuration["container_identity"]["networks"] == original[0]["networks"])
    require(original[0]["image"] == configuration["container_identity"]["image_id"])
    require(set(original[0]["networks"]) <= {item["name"] for item in manifest.get("networks", {}).values()})
    return {"declaration": value, "plan": plan, "proof": proof, "configuration": configuration,
            "files": files, "inputs": inputs, "bindings": bindings, "sha256": digest(value)}


def verify_records(info, review, records):
    """Keep the whole old set; only this exact identity may change resource domain."""
    from scripts.production_gateway_switch import gateway_identity
    value = info["declaration"]
    require(all(row["Config"]["Labels"].get("com.docker.compose.oneoff", "False").lower() != "true"
                for row in records))
    rows = [row for row in records if row["Id"] == value["old_container_id"]]
    require(len(rows) == 1)
    row = rows[0]
    labels = row["Config"]["Labels"]
    require(labels.get("com.docker.compose.project") == review["project"]
            and labels.get("com.docker.compose.service") == value["service"]
            and labels.get("com.docker.compose.project.working_dir") == review["project_directory"]
            and labels.get("com.docker.compose.oneoff", "False").lower() != "true"
            and row["Mounts"] == [] and gateway_identity(row) == value["old_gateway_identity"])
    networks = row["NetworkSettings"]["Networks"]
    require(len(networks) == 1
            and next(iter(networks.values()))["NetworkID"] == info["configuration"]["network_id"])
    return row


def verify_legacy(info, row, legacy, *, private_digest):
    from scripts.production_legacy_takeover import LegacyTakeover, container_identity
    require(type(legacy) is LegacyTakeover)
    route = legacy.plan["gateway"]
    require(route["container_id"] == info["declaration"]["old_container_id"]
            and route["private_contract_sha256"] == private_digest
            and route["gateway_plan_sha256"] == info["declaration"]["gateway_plan_sha256"]
            and route["original"] == info["proof"])
    stopped = legacy.check_stopped()
    observed = stopped["containers"]["gateway"]
    state = row["State"]
    require(observed["identity"] == container_identity(row) and observed["running"] is False
            and observed["status"] == "exited" and observed["exit_code"] in {0, 143}
            and observed["exit_code"] == state["ExitCode"]
            and observed["finished_at"] == state["FinishedAt"]
            and state["FinishedAt"] not in {"", "0001-01-01T00:00:00Z"}
            and not state["Running"] and state["Status"] == "exited"
            and not any(state.get(key) for key in ("Restarting", "Paused", "Dead", "OOMKilled"))
            and stopped["writer_lock_held"] is True)
    return stopped


def verify_gateway(info, gateway, session):
    from scripts.production_gateway_switch import GatewaySwitch
    require(type(gateway) is GatewaySwitch and gateway.session is session
            and gateway.plan == info["plan"] and gateway.plan_sha == digest(info["plan"]))


def verify_receipt(info, receipt):
    require(receipt["status"] == "gateway_switched" and receipt["durable"] is True
            and receipt["operation_id"] == info["plan"]["operation_id"]
            and receipt["plan_sha256"] == digest(info["plan"])
            and receipt["old_gateway_retained"] == info["declaration"]["old_container_id"]
            and receipt["old_config"] == info["proof"]
            and sorted((item["binding"] for item in receipt["http"]), key=canonical)
            == sorted(info["bindings"], key=canonical))
    return digest(receipt)
