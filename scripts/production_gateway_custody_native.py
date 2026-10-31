"""Actual same-project retained-gateway fixture for private-host activation.

This opt-in synthetic case is deliberately separate from the old RO-bind
gateway case and from actual .125 image compatibility. Its old app/sender
processes are fixed Python TERM fixtures, never claimed to be old business
binaries. No system cron, production storage, model, pull or forced cleanup is
used here. The caller must retain the original private-host absolute budget.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from scripts import private_release_contract as private
from scripts import production_activation_fixture as fixture
from scripts import production_gateway_native as ordinary_gateway
from scripts import production_gateway_switch as gateway
from scripts import production_legacy_takeover as legacy
from scripts import production_native_deadline as native_deadline
from scripts import production_sender_bootstrap_native as bootstrap_native
from scripts import retained_gateway_configuration as configuration
from scripts.verify_miniflux_pause_native import LABEL
from services import admission_fence as gate


HOSTS = ("127.0.0.1", "127.0.0.2")
OLD_SERVICE = "previous-gateway"
FOUR_NGINX_CONFIG = b"""user root;
worker_processes 1;
pid /tmp/nginx.pid;
events { worker_connections 16; }
http {
  include /etc/nginx/mime.types;
  access_log /dev/stdout;
  error_log /dev/stderr warn;
  include /etc/nginx/conf.d/*.conf;
}
"""
DEFAULT_CONFIG = b"server { listen 5000; location = /health { return 200 'OLD'; } }\n"
# The retained predecessor starts in the observed production include shape.
# Inactive image files are neither inferred absent nor admitted to this proof.
NGINX_CONFIG = FOUR_NGINX_CONFIG.replace(b"  include /etc/nginx/conf.d/*.conf;", b"  " + DEFAULT_CONFIG.rstrip())


def require_private_host(harness):
    from scripts.production_activation_host_native import PrivateHostContext
    context = getattr(harness, "private_host_context", None)
    gate._require(type(context) is PrivateHostContext and os.geteuid() == 0,
                  "custody case requires the attested real private daemon-host namespace")
    context.check()
    gate._require(native_deadline.STATE.get() is not None,
                  "custody case must inherit the original private-host absolute deadline")
    return context


def old_manifest(p, python_image, nginx_image):
    """Exact three services; old Nginx config is in its writable layer, no bind."""
    previous, names = fixture.previous_manifest(p, python_image)
    for service in previous["services"].values():
        service["entrypoint"] = ["python", "-I", "-B", "-c", bootstrap_native.OLD_PROCESS]
        service["command"] = []
        service["labels"] = {LABEL: p["custody_owner"], "io.ai-filter.synthetic-custody": p["custody_owner"]}
    encoded = base64.b64encode(NGINX_CONFIG).decode("ascii")
    initialize = ("printf '%s' " + encoded + " | base64 -d > /etc/nginx/nginx.conf; "
                  "chmod 0644 /etc/nginx/nginx.conf; exec nginx -g 'daemon off;'")
    previous["services"][OLD_SERVICE] = {
        "image": nginx_image, "container_name": p["custody_old_name"], "user": "0:0",
        "entrypoint": ["/bin/sh", "-ec"], "command": [initialize],
        "mem_limit": "128m", "memswap_limit": "128m", "pids_limit": 64, "cpus": 1,
        "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "restart": "no",
        "healthcheck": {"disable": True}, "networks": ["isolated"],
        "labels": {LABEL: p["custody_owner"], "io.ai-filter.synthetic-custody": p["custody_owner"]},
        "ports": [{"target": 5000, "published": "5000", "host_ip": host, "protocol": "tcp"} for host in HOSTS]}
    return previous, names


def capture_archive(transport, identifier, container_path, path):
    """Tee only parser-requested bytes; invalid originals remain for diagnosis."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    error = None
    try:
        def consume(read):
            def tee(size):
                raw = read(size)
                gate._require(type(raw) is bytes and len(raw) <= size,
                              "native archive transport over-read")
                offset = 0
                while offset < len(raw):
                    written = os.write(fd, raw[offset:])
                    gate._require(written > 0, "native archive original short write")
                    offset += written
                return raw
            return configuration.parse_archive(tee, container_path)
        result = transport.archive(identifier, container_path, consume)
    except BaseException as primary:
        error = primary
        raise
    finally:
        try:
            os.fsync(fd)  # Invalid consumed headers are also original evidence.
            gate._sync_directory(path.parent)
        except BaseException as secondary:
            if error is None:
                raise
            error.add_note("Native archive failure preservation: " + type(secondary).__name__)
        finally:
            os.close(fd)
    gate._require(path.stat().st_size == result["bytes"] <= configuration.MAX_ARCHIVE,
                  "native archive original size differs")
    return result


def _inspect(harness, identifier, *, final=False):
    row = json.loads(harness.command(["inspect", identifier], timeout=3, final=final))[0]
    gate._require(row["Id"] == identifier, "exact synthetic container identity required")
    return row


def _running_old(p, rows):
    by_service = {row["Config"]["Labels"].get("com.docker.compose.service"): row for row in rows}
    gate._require(len(rows) == len(by_service) == 3 and set(by_service) == set(p["custody_old_services"])
                  and all(row["Config"]["Labels"].get("com.docker.compose.project") == p["project"]
                          and row["Config"]["Labels"].get("com.docker.compose.project.working_dir") == str(p["paths"]["project"])
                          and row["Config"]["Labels"].get("io.ai-filter.synthetic-custody") == p["custody_owner"]
                          and row["State"]["Running"] is True and row["State"]["Status"] == "running"
                          and row["RestartCount"] == 0
                          and not any(row["State"].get(flag) for flag in ("Paused", "Dead", "OOMKilled", "Restarting"))
                          for row in rows), "exact three running same-project predecessor roles required")
    old = by_service[OLD_SERVICE]
    expected = {"5000/tcp": [{"HostIp": host, "HostPort": "5000"} for host in HOSTS]}
    actual = {key: value for key, value in old["NetworkSettings"]["Ports"].items() if value is not None}
    gate._require(p["custody_image_id"] in configuration.IMAGE_IDS and old["Image"] == p["custody_image_id"] and old["Name"] == "/" + p["custody_old_name"] and old["Mounts"] == []
                  and old["HostConfig"]["PortBindings"] == expected and actual == expected,
                  "retained gateway must have zero mounts and both exact fixed original ports")
    return by_service


def verify_four_archive_profile(harness, p, image_id, network_id):
    """One no-port config-test: retain the default profile's real four archives.

    This is not another switch. It uses the original inherited absolute budget,
    exact fixed image and owned private network, and never publishes a port.
    The exited container stays in the existing owned evidence/cleanup inventory.
    """
    gate._require(native_deadline.STATE.get() is not None,
                  "default archive case must inherit the private-host deadline")
    root = p["gateway_native_root"] / "default-four-profile"
    root.mkdir(mode=0o700)
    encoded = [base64.b64encode(raw).decode("ascii") for raw in (FOUR_NGINX_CONFIG, DEFAULT_CONFIG)]
    initialize = ("printf '%s' " + encoded[0] + " | base64 -d > /etc/nginx/nginx.conf; "
                  "printf '%s' " + encoded[1] + " | base64 -d > /etc/nginx/conf.d/default.conf; "
                  "chmod 0644 /etc/nginx/nginx.conf /etc/nginx/conf.d/default.conf; exec nginx -t")
    role = "gateway-four-profile"
    row = harness.launch(role, image_id, ["--entrypoint", "/bin/sh", "--user", "0:0", "--cap-drop=ALL",
                                        "--restart", "no"], ["-ec", initialize], memory=128 * 1024**2, pids=64)
    gate._require(row["Config"].get("User") == "0:0" and row["HostConfig"].get("CapDrop") == ["ALL"]
                  and row["Config"].get("Entrypoint") == ["/bin/sh"]
                  and row["Config"].get("Cmd") == ["-ec", initialize],
                  "default archive config-test execution protection changed")
    original = ordinary_gateway._cleanup_identity(row)
    identifier = row["Id"]

    def exited():
        current = _inspect(harness, identifier)
        gate._require(ordinary_gateway._cleanup_identity(current) == original and current["Mounts"] == []
                      and not current["HostConfig"].get("PortBindings")
                      and not any(current["NetworkSettings"].get("Ports", {}).values()),
                      "default archive config-test identity or zero-port boundary changed")
        state = current["State"]
        if state.get("Running") is True and state.get("Status") == "running":
            gate._require(not any(state.get(flag) for flag in ("Paused", "Dead", "Restarting", "OOMKilled")),
                          "default archive config-test entered an uncertain running state")
            return False
        ordinary_gateway._normal_cleanup_exit(current, original, purpose="config-test")
        return current

    stopped = harness.wait(exited, seconds=3)
    from scripts.verify_miniflux_pause_native import resource_checks
    checks = resource_checks(stopped, image_id, harness.owner, harness.network, 128 * 1024**2, 64)
    transport = gateway.NativeTransport()
    transport.deadline = min(transport.deadline, time.monotonic() + native_deadline.clamp(10))
    archives = {}
    for number, path in enumerate(configuration.ARCHIVE_PATHS):
        archives[path] = root / ("archive-%02d.tar" % number)
        capture_archive(transport, identifier, path, archives[path])
    proof = configuration.describe(stopped, network_id, archives)
    configuration.verify(proof, _inspect(harness, identifier),
                         inspect=lambda exact: _inspect(harness, exact), archive=transport.archive)
    # An archive proof alone never substitutes for confirmed normal exit.
    ordinary_gateway._normal_cleanup_exit(_inspect(harness, identifier), original, purpose="config-test")
    fixture.save(root / "CONFIGURATION.json", proof)
    result = {"schema": 1, "status": "passed", "profile": configuration.DEFAULT_PROFILE,
              "container_id": identifier, "configuration_sha256": gate._hash(proof),
              "archive_count": len(proof["archives"]),
              "ports": [binding for rows in (stopped["HostConfig"].get("PortBindings") or {}).values() for binding in rows],
              "mounts": stopped["Mounts"],
              "normal_exit_code": stopped["State"]["ExitCode"], "resource_checks": checks,
              "memory": stopped["HostConfig"]["Memory"], "pids": stopped["HostConfig"]["PidsLimit"],
              "synthetic_only": True}
    fixture.save(root / "RESULT.json", result)
    p["custody_default_profile"] = result
    return result


def prepare_and_seal(harness, p):
    """Retain actual mime-only old runtime plus the default four-file proof."""
    require_private_host(harness)
    p["custody_owner"], p["custody_old_name"] = harness.owner, harness.prefix + "-gateway-old"
    network = json.loads(harness.command(["network", "inspect", harness.network], timeout=3))[0]
    from scripts.production_activation_host_native import require_harness_network
    require_harness_network(harness, network)
    image = json.loads(harness.command(["image", "inspect", gateway.NGINX_REF], timeout=3))[0]
    from scripts.verify_miniflux_pause_native import image_checks
    image_checks(image, gateway.NGINX_REF)
    configuration.validate_image(image)
    p["custody_image_id"] = image["Id"]
    root = p["directory"] / "gateway-native"
    root.mkdir(mode=0o700)
    p["gateway_native_root"] = root
    verify_four_archive_profile(harness, p, image["Id"], network["Id"])
    evidence = root / "switch"
    evidence.mkdir(mode=0o700)
    now = int(time.time())
    p["custody_window"] = {"not_before": now - 1, "expires_at": now + 600}
    op = gate._hash({"synthetic-retained-gateway": harness.owner})
    manifest = deepcopy(p["manifest"])
    for service in manifest["services"].values():
        service["environment"].update({"AI_FILTER_TRUSTED_PROXY_HOST": gateway.gateway_name(op),
            "AI_FILTER_WEB_BASE_URL": "http://" + gateway.gateway_name(op) + ":5000"})
    gate._write(p["manifest_path"], manifest)
    p["manifest"] = manifest
    previous, names = old_manifest(p, harness.python_id, image["Id"])
    p["custody_old_services"] = tuple(previous["services"])
    old_path = p["directory"] / "previous.json"
    fixture.save(old_path, previous)
    gate._write(root / "PREPARE_INTENT.json", {"operation_id": op, "owner": harness.owner,
        "network_id": network["Id"], "image_id": image["Id"], "old_name": p["custody_old_name"],
        "old_project": p["project"], "host_bindings": [{"HostIp": host, "HostPort": "5000"} for host in HOSTS],
        "synthetic_only": True})
    # Compose gets exactly the owned project/network. Fixed-port conflicts fail;
    # no alternate port, unrelated container stop, or retry is available.
    private.run(["docker", "compose", "--project-name", p["project"], "--project-directory", str(p["paths"]["project"]),
                 "-f", str(old_path), "up", "-d", "--no-build", "--pull", "never"])
    rows = private.inspect_project({"review": {"project": p["project"], "project_directory": str(p["paths"]["project"])}})
    by_service = _running_old(p, rows)
    p["previous_containers"] = {row["Id"]: row for row in rows}
    for service, row in by_service.items():
        # Only exact validated owned rows join the existing private namespace
        # resource inventory. They do not join generic stop/KILL cleanup.
        role = "custody-" + service
        harness.names[role], harness.images[role] = row["Name"].removeprefix("/"), row["Image"]
    for name in names.values():
        identifier = by_service[name]["Id"]
        harness.wait(lambda identifier=identifier: harness.command(
            ["exec", identifier, "python", "-I", "-c", bootstrap_native.TERM_READY], timeout=3).strip() == "1",
            seconds=3, readiness=True)
    transport = gateway.NativeTransport()
    transport.deadline = min(transport.deadline, time.monotonic() + native_deadline.clamp(10))
    for host in HOSTS:
        def healthy(host=host):
            try:
                result = transport.health(host, 5000, op)
            except (ConnectionError, OSError):
                return False
            gate._require(result["status"] == 200 and result["body_sha256"] == hashlib.sha256(b"OLD").hexdigest()
                          and result["operation"] is None and result["upstream"] is None,
                          "old fixed host port did not reach exact synthetic gateway")
            return True
        harness.wait(healthy, seconds=3)
    old = by_service[OLD_SERVICE]
    archives = {}
    for number, path in enumerate(configuration.archive_paths(configuration.MIME_ONLY_PROFILE)):
        archives[path] = root / ("archive-%02d.tar" % number)
        capture_archive(transport, old["Id"], path, archives[path])
    proof = configuration.describe(old, network["Id"], archives, profile=configuration.MIME_ONLY_PROFILE)
    configuration.verify(proof, _inspect(harness, old["Id"]), inspect=lambda identifier: _inspect(harness, identifier),
                         archive=transport.archive)
    proof_path = root / "CONFIGURATION.json"
    gate._write(proof_path, proof)
    new_config = root / "new-nginx.conf"
    gateway._save_raw(new_config, gateway.render_config(op, "ai-filter-api"))
    plan = {"schema": 1, "operation_id": op, "source_sha256": gateway.source_sha256(),
        "source_sha": p["source"]["source_sha"], "old_gateway_id": old["Id"],
        "old_gateway_identity": gateway.gateway_identity(old), "old_config": proof,
        "new_config": gateway.file_identity(new_config)[0], "api_alias": "ai-filter-api", "network_id": network["Id"],
        "wait_seconds": 10, "max_polls": 10, "window": p["custody_window"]}
    plan_path = root / "PLAN.json"
    gate._write(plan_path, plan)
    gate._require(set(plan) == gateway.PLAN_KEYS, "complete custody plan required before seal")
    topology = {name: {"mounts": service["volumes"], "networks": service["networks"], "ports": []}
                for name, service in manifest["services"].items()}
    observed = private.runtime_topology(rows)
    ports = observed[OLD_SERVICE][0]["ports"]
    review = {**p["source"], "approved": True, "approved_by": "synthetic-retained-custody-workflow",
        "approved_at": now - 1, "expires_at": now + 600,
        "runtime_config_sha256": private.sha(p["config"].read_bytes()),
        "input_manifest_sha256": private.sha(p["manifest_path"].read_bytes()),
        "environment_sha256": private.sha(p["environment"].read_bytes()), "strategy": p["strategy"],
        "project": p["project"], "project_directory": str(p["paths"]["project"]), "health_services": ["ai-filter-api"],
        "existing_topology": observed, "service_mapping": {name: [names[name]] for name in names}, "topology": topology,
        "retained_gateway_custody": {"schema": 1, "kind": "dedicated_same_project_gateway", "project": p["project"],
            "service": OLD_SERVICE, "old_container_id": old["Id"], "old_gateway_identity": gateway.gateway_identity(old),
            "old_topology_sha256": gate._hash(observed[OLD_SERVICE][0]), "gateway_plan_path": str(plan_path),
            "gateway_plan_sha256": private.sha(plan_path.read_bytes()), "old_configuration_proof_path": str(proof_path),
            "old_configuration_proof_sha256": private.sha(proof_path.read_bytes()), "ports": ports}}
    from scripts.private_control_subpaths import capture
    control = {kind: capture(p["paths"][path], target, p["paths"]["data"]) for kind, path, target in (
        ("state", "slots", "/app/data/startup_slots"), ("freeze", "fence", "/app/data/admission_fence"))}
    review["control_subpaths"] = {name: deepcopy(control) for name in names}
    review_path = p["directory"] / "review.json"
    fixture.save(review_path, review)
    p["gateway_plan"], p["gateway_evidence"] = plan, evidence
    p["custody_roles"] = {role: by_service[names["ai-filter-" + role]] for role in ("api", "worker")}
    p["custody_roles"]["gateway"] = old
    sealed = private.seal(harness.repo, p["config"], p["manifest_path"], p["environment"], review_path, p["directory"] / "sealed")
    fixture.save(root / "SEALED_TOPOLOGY.json", {"old_ids": sorted(p["previous_containers"]),
        "old_gateway_mount_count": 0, "old_gateway_ports": ports, "same_project_old_roles": 3,
        "new_app_services": sorted(manifest["services"]), "actual_archives": True,
        "closure_profile": configuration.MIME_ONLY_PROFILE, "default_four_profile": p["custody_default_profile"],
        "legacy_stop_proven": False, "synthetic_only": True, "actual_125_compatibility_proven": False})
    return sealed


def _create_old_sender(harness, p, directory):
    name = harness.prefix + "-custody-old-sender"
    fixture.save(directory / "SENDER_CREATE_INTENT.json", {"name": name, "image_id": harness.python_id,
        "owner": harness.owner, "memory": 768 * 1024**2, "pids": 256, "synthetic_only": True})
    identifier = harness.command(["create", "--pull=never", "--platform=linux/amd64", "--name", name,
        "--label", LABEL + "=" + harness.owner, "--network", harness.network,
        "--memory", str(768 * 1024**2), "--memory-swap", str(768 * 1024**2), "--pids-limit", "256",
        "--cpus=1", "--security-opt=no-new-privileges", "--read-only", "--cap-drop=ALL", "--user", "0:0",
        "--restart", "no", harness.python_id, "python", "-I", "-B", "-c", bootstrap_native.OLD_PROCESS], timeout=3).strip()
    gate._require(gate.HEX64.fullmatch(identifier), "owned old sender create ACK unknown")
    row = _inspect(harness, identifier)
    gate._require(row["Image"] == harness.python_id and row["Config"]["Labels"].get(LABEL) == harness.owner,
                  "owned old sender create identity changed")
    p["custody_roles"]["sender"] = row  # Preserve exact ID even if start ACK is lost.
    harness.names["custody-old-sender"], harness.images["custody-old-sender"] = name, harness.python_id
    fixture.save(directory / "SENDER_CREATED.json", {"id": identifier, "image_id": row["Image"]})
    harness.command(["start", identifier], timeout=3)
    from scripts.verify_miniflux_pause_native import resource_checks
    row = _inspect(harness, identifier)
    resource_checks(row, harness.python_id, harness.owner, harness.network, 768 * 1024**2, 256)
    p["custody_roles"]["sender"] = row
    harness.wait(lambda: harness.command(["exec", identifier, "python", "-I", "-c", bootstrap_native.TERM_READY],
                                        timeout=3).strip() == "1", seconds=3, readiness=True)
    return row


def legacy_plan(harness, p, daemon, private_digest, candidate_image):
    """Real registered file-cron and four runtime roles, never a fake receipt."""
    directory = p["directory"] / "legacy"
    directory.mkdir(mode=0o700)
    _create_old_sender(harness, p, directory)
    record = bootstrap_native._record
    refresh = record(directory / "refresh", b"#!/bin/sh\nexit 0\n", 0o700)
    before = ("# synthetic file-only cron\n* * * * * " + refresh["path"] + "\n").encode()
    lock = directory / "cron.lock"
    bootstrap_native._save(lock, b"")
    binary = record(directory / "crontab-fixture", bootstrap_native.cron_command(directory / "cron.current"), 0o700)
    bootstrap_native._save(directory / "cron.current", before)
    flock = legacy._record(Path("/usr/bin/flock"), executable=True)[0]
    writer = record(directory / "writer", legacy.writer_script(str(lock), binary["path"], flock["path"]), 0o700)
    op, now = gate._hash({"synthetic-legacy-custody": harness.owner}), int(time.time())
    line_hash = private.sha(before.splitlines(keepends=True)[1])
    after = before.splitlines(keepends=True)[0] + ("# controlled-legacy-stop " + op + " " + line_hash + "\n").encode()
    plan = {"schema": 1, "operation_id": op, "source_sha256": legacy.source_sha256(),
        "host_boot_id": legacy.NativeTransport().host_boot(), "approved_by": "owned-synthetic-file-cron-only",
        "not_before": now - 1, "expires_at": p["custody_window"]["expires_at"],
        "docker": legacy._record(Path("/usr/bin/docker"), executable=True)[0], "wait_seconds": 5, "max_polls": 20,
        "containers": {role: legacy.container_identity(value) for role, value in p["custody_roles"].items()},
        "gateway": {"original": p["gateway_plan"]["old_config"], "container_id": p["gateway_plan"]["old_gateway_id"],
            "config_target": configuration.legacy_config_target(p["gateway_plan"]["old_config"]), "candidate_service": "ai-filter-api",
            "candidate_image_id": candidate_image, "private_contract_sha256": private_digest,
            "gateway_plan_sha256": private.sha((p["gateway_native_root"] / "PLAN.json").read_bytes())},
        "cron": {"lock": legacy._lock_identity(lock), "binary": binary, "flock": flock, "writer": writer,
            "before": record(directory / "cron.original", before), "after_sha256": private.sha(after),
            "entries": [{"line": 1, "sha256": line_hash, "executable": refresh}], "daemon": legacy._process(daemon.pid)}}
    inventory = {"schema": 1, "scope": "registered-root-cron-writers", "host_boot_id": plan["host_boot_id"],
        "lock": plan["cron"]["lock"], "writer": writer, "adapter_source_sha256": plan["source_sha256"],
        "registered_writers": [writer["path"], str(Path(legacy.__file__).resolve())],
        "external_root_mutation": "forbidden-during-reviewed-window", "expires_at": plan["expires_at"],
        "reviewed_by": "owned-synthetic-file-cron-only"}
    plan["cron"]["writer_inventory"] = record(directory / "writers.json", inventory)
    path = directory / "PLAN.json"
    bootstrap_native._save(path, plan)
    legacy.load_plan(path)
    return path


def preserve(harness, p):
    """Retain exact originals; TERM only, no rm/-f/stop-with-KILL fallback."""
    directory = p["directory"] / "custody-final"
    directory.mkdir(mode=0o700)
    rows = private.inspect_project({"review": {"project": p["project"], "project_directory": str(p["paths"]["project"])}})
    allowed = set(p.get("previous_containers", {})) | set(p.get("custody_new_containers", {}).values())
    gate._require(all(row["Id"] in allowed for row in rows), "unknown project container retained without cleanup")
    if "sender" in p.get("custody_roles", {}):
        rows.append(_inspect(harness, p["custody_roles"]["sender"]["Id"], final=True))
    for row in rows:
        identifier = row["Id"]
        original_identity = ordinary_gateway._cleanup_identity(row)
        fixture.save(directory / (identifier + ".json"), harness.safe_inspect(row))
        harness.command(["logs", identifier], timeout=3, final=True)
        current = _inspect(harness, identifier, final=True)
        gate._require(ordinary_gateway._cleanup_identity(current) == original_identity, "owned cleanup identity changed")
        if current["State"]["Running"]:
            gate._require(not any(current["State"].get(key) for key in ("Paused", "Dead", "Restarting", "OOMKilled")),
                          "owned cleanup running state uncertain")
            fixture.save(directory / (identifier + ".term.json"), {"id": identifier, "signal": "TERM"})
            harness.command(["kill", "--signal", "TERM", identifier], timeout=3, final=True)
            def stopped():
                current = _inspect(harness, identifier, final=True)
                gate._require(ordinary_gateway._cleanup_identity(current) == original_identity,
                              "owned cleanup identity changed")
                if current["State"]["Running"]:
                    return False
                return ordinary_gateway._normal_cleanup_exit(current, original_identity, purpose="owned")
            state = harness.wait(stopped, seconds=15)
        else:
            state = ordinary_gateway._normal_cleanup_exit(current, original_identity, purpose="owned")
        fixture.save(directory / (identifier + ".stopped.json"), state)


def completed_proof(p, capability):
    """An actual retained capability and persisted runtime proof, never a seal alone."""
    gate._require(type(capability) is legacy.LegacyTakeover, "live original legacy capability required")
    stopped = capability.check_stopped()
    verified = gate._read(p["paths"]["lifecycle"] / "active-verified.json")
    old_gateway = p["gateway_plan"]["old_gateway_id"]
    old_apps = sorted(set(p["previous_containers"]) - {old_gateway})
    default_profile = p["custody_default_profile"]
    gate._require(p["gateway_plan"]["old_config"].get("profile") == configuration.MIME_ONLY_PROFILE
                  and default_profile["status"] == "passed" and default_profile["profile"] == configuration.DEFAULT_PROFILE
                  and default_profile["archive_count"] == 4 and default_profile["normal_exit_code"] == 0
                  and default_profile["ports"] == [] and default_profile["mounts"] == [],
                  "actual default four-archive profile proof missing")
    gate._require(stopped["writer_lock_held"] is True and len(old_apps) == 2
                  and verified["status"] == "actual_runtime_verified" and verified["durable"] is True
                  and verified["containers"] == p["custody_new_containers"]
                  and verified["retained_previous_ids"] == old_apps
                  and verified["retained_gateway_custody"]["old_gateway_id"] == old_gateway,
                  "actual same-project custody activation proof incomplete")
    result = {"schema": 1, "status": "passed", "old_project_roles": 3,
        "old_app_ids": old_apps, "old_gateway_id": old_gateway, "old_gateway_mounts": 0,
        "old_gateway_ports": gateway._ports(p["custody_roles"]["gateway"]),
        "actual_configuration_closure_sha256": p["gateway_plan"]["old_config"]["closure_sha256"],
        "closure_profile": configuration.MIME_ONLY_PROFILE, "default_four_profile": default_profile,
        "legacy_stop_sha256": gate._hash(stopped), "active_runtime_proof_sha256": gate._hash(verified),
        "new_app_containers": p["custody_new_containers"], "actual_gateway_and_live_legacy": True,
        "synthetic_only": True, "actual_125_compatibility_proven": False, "system_cron_access": False,
        "production_access": False, "real_models": 0}
    fixture.save(p["directory"] / "ACTUAL_CUSTODY_PROOF.json", result)
    return result


def _preserve_scenario(harness, p, daemon, primary):
    """Keep the first failure while still attempting every existing finalizer.

    An empty/partly-created predecessor project is still an error, never a
    successful drain. Its cleanup error must not replace the earlier operation
    error that explains why the project was never created.
    """
    def gateway_originals():
        if "gateway_native_root" in p:
            ordinary_gateway.preserve(harness, p)

    def daemon_exit():
        if daemon is not None:
            daemon.terminate()
            daemon.wait(timeout=native_deadline.clamp(3))

    failure = primary
    with native_deadline.final_phase():
        for label, finalize in (("gateway", gateway_originals), ("project", lambda: preserve(harness, p)),
                                ("anchor", p["anchor"].close), ("daemon", daemon_exit)):
            try:
                finalize()
            except BaseException as secondary:
                if failure is None:
                    failure = secondary
                else:
                    failure.add_note("custody finalizer " + label + " failed: " + type(secondary).__name__
                                     + " sha256=" + hashlib.sha256(str(secondary).encode()).hexdigest())
    if primary is None and failure is not None:
        raise failure


def scenario(harness, output):
    from scripts import verify_production_activation_native as activation
    from scripts.production_sender_native_bridge import NativeControlBridge
    from production_sender_driver import SenderActivationDriver
    require_private_host(harness)
    harness.stage("custody_never_opened_synthetic_storage_not_actual125")
    p = fixture.prepare_disk(output / "receiver", private.source_identity(harness.repo), harness.network, harness.python_id)
    p["activation_operation"] = activation.operation("receiver-activation-custody")
    daemon, primary = None, None
    try:
        harness.sender_contract = p["strategy"]
        harness.stage("custody_actual_sender_first_closed")
        harness.launch_sender()
        driver = activation.closed_initialization(harness, p)
        harness.stage("custody_same_project_zero_mount_originals_and_seal")
        sealed = prepare_and_seal(harness, p)
        contract, _manifest, digest = private.load(sealed, harness.repo, require_fresh=True)
        harness.stage("custody_actual_source_bound_application_image")
        image = harness.private_host_context.build_candidate(p, sealed, digest)
        activation.startup_native.probe_candidate(contract, sealed.parent, image, p["directory"],
            deadline=harness.private_host_context.plan["active_deadline"])
        permit = fixture.migration_permit(p, sealed, image)
        # New synthetic permission is issued once with the pre-seal exact window;
        # no already-used permission, production approval or source is rewritten.
        permit.update(p["custody_window"])
        daemon = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(600)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        path = legacy_plan(harness, p, daemon, digest, image)
        evidence = p["directory"] / "legacy-stop"
        evidence.mkdir(mode=0o700)
        with legacy.locked_takeover(path, evidence) as capability:
            stopped = capability.stop()
            gate._require(stopped["status"] == "registered_legacy_runtime_stopped", "actual four-role legacy stop unconfirmed")
            fixture.save(p["directory"] / "ACTUAL_LEGACY_STOP.json", stopped)
            migration_root = p["directory"] / "migration"
            with NativeControlBridge(harness) as bridge:
                native_driver = SenderActivationDriver(driver.plan,
                    bridge.transport(p["directory"] / "synthetic-control-token.json"), driver.postgres, driver.root)
                try:
                    active = activation.retained_native_phases(harness, p, driver, native_driver, permit,
                                                              migration_root, legacy=capability)
                    activation.complete_native_supervision(harness, p, active, native_driver, migration_root)
                    completed_proof(p, capability)
                except BaseException:
                    activation.abort_native_guardian(p)
                    raise
    except BaseException as error:
        primary = error
        raise
    finally:
        _preserve_scenario(harness, p, daemon, primary)
