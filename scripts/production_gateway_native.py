"""Synthetic gateway preparation for the actual activation native harness.

This helper is not a deployment entry point. It requires the harness's actual
owned internal network, preserves the old fixed-image gateway and its config,
and never pulls images, changes resource budgets, or removes containers.
"""

from __future__ import annotations

from copy import deepcopy
import errno
import hashlib
import json
from pathlib import Path
import re
import socket
import sys
import time

from scripts import private_release_contract as private
from scripts import production_gateway_switch as gateway
from services import admission_fence as gate

_SCRIPTS = str(Path(__file__).resolve().parent)
_ADDED_PATH = _SCRIPTS not in sys.path
if _ADDED_PATH:
    sys.path.insert(0, _SCRIPTS)
try:
    from scripts.verify_miniflux_pause_native import LABEL, image_checks
finally:
    if _ADDED_PATH:
        sys.path.remove(_SCRIPTS)


OLD_CONFIG = b"""worker_processes 1;
pid /tmp/nginx.pid;
events { worker_connections 16; }
http { access_log /dev/stdout; error_log /dev/stderr warn;
  server { listen 5000; location = /health { return 200 'OLD'; } }
}
"""


def _port():
    # Kernel-selected loopback port; close before Docker claims it. A competing
    # claim makes create/start fail. There is no second choice or automatic retry.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _inspect(harness, identifier, *, final=False):
    return json.loads(harness.command(["inspect", identifier], timeout=3, final=final))[0]


def _create_old(harness, root, name, project, network, image, old_config, port, operation):
    role = "gateway-old"
    harness.names[role], harness.images[role] = name, image["Id"]
    # Independent preserve() sends TERM only; generic preserve may force-stop.
    identifier = harness.command(
        [
            "create",
            "--pull",
            "never",
            "--platform",
            "linux/amd64",
            "--name",
            name,
            "--label",
            LABEL + "=" + harness.owner,
            "--label",
            "com.docker.compose.project=" + project,
            "--label",
            "com.docker.compose.service=synthetic-old-gateway",
            "--network",
            network["Id"],
            "--network-alias",
            name,
            "--publish",
            "127.0.0.1:" + str(port) + ":5000/tcp",
            "--memory",
            str(128 * 1024**2),
            "--memory-swap",
            str(128 * 1024**2),
            "--pids-limit",
            "64",
            "--cpus",
            "1",
            "--security-opt",
            "no-new-privileges:true",
            "--restart",
            "no",
            "--no-healthcheck",
            "--mount",
            "type=bind,src=" + str(old_config) + ",dst=" + gateway.CONFIG_TARGET + ",readonly",
            "--entrypoint",
            "nginx",
            image["Id"],
            "-g",
            "daemon off;",
        ],
        timeout=3,
    ).strip()
    gate._require(gate.HEX64.fullmatch(identifier), "synthetic old gateway create ACK unknown")
    gate._write(root / "OLD_CREATED.json", {"id": identifier, "operation_id": operation})
    harness.command(["start", identifier], timeout=3)
    return identifier


def _old_ingress(harness, root, identifier, image, port, *, confirmed=False):
    row = _inspect(harness, identifier)
    expected = {"5000/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]}
    requested = row["HostConfig"].get("PortBindings")
    actual = row["NetworkSettings"].get("Ports")
    # Docker may also list the pinned image's exposed, unpublished port as null.
    published = {key: value for key, value in actual.items() if value is not None} if isinstance(actual, dict) else None
    identity = (row["Id"] == identifier and row["Image"] == image["Id"]
                and row["State"].get("Running") is True and row["State"].get("Status") == "running"
                and not any(row["State"].get(key, False) for key in ("OOMKilled", "Restarting", "Paused", "Dead"))
                and row["RestartCount"] == 0 and row["Config"]["Labels"].get(LABEL) == harness.owner)
    profile = {"schema": 1, "container_id": identifier, "image_id": image["Id"], "identity_matches": identity,
               "expected_binding": expected, "requested_matches": requested == expected,
               "actual_field_present": "Ports" in row["NetworkSettings"], "actual_matches": published == expected,
               "actual_mapping_sha256": gate._hash(actual), "synthetic_only": True}
    gate._write(root / ("OLD_INGRESS_CONFIRMED.json" if confirmed else "OLD_INGRESS_PROFILE.json"), profile)
    gate._require(identity, "old synthetic gateway execution changed")
    gate._require(requested == expected and published == expected, "old synthetic actual host port mapping absent or changed")
    return row


def _http_error(error):
    classes = {errno.ECONNREFUSED: "connection_refused", errno.ECONNRESET: "connection_reset",
               errno.ECONNABORTED: "connection_aborted", errno.ETIMEDOUT: "timeout",
               errno.ENETUNREACH: "network_unreachable", errno.EHOSTUNREACH: "host_unreachable",
               errno.EACCES: "permission_denied", errno.EPERM: "permission_denied"}
    number = getattr(error, "errno", None)
    number = number if type(number) is int and number in classes else None
    return {"class": "timeout" if isinstance(error, TimeoutError) else classes.get(number, "unclassified"),
            "errno": number, "message_sha256": hashlib.sha256(str(error).encode()).hexdigest()}


def _old_http(harness, root, port, operation):
    transport = gateway.NativeTransport()
    started = time.monotonic()
    deadline = started + 10
    if getattr(harness, "phase_deadline", None) is not None:
        deadline = min(deadline, harness.phase_deadline)
    transport.deadline = min(transport.deadline, deadline)
    attempts = []

    def healthy():
        gate._require(len(attempts) < 64 and time.monotonic() < deadline, "old HTTP diagnostic/phase bound")
        attempt = {"index": len(attempts) + 1, "stage": "host_http", "started_seconds": time.monotonic() - started}
        attempts.append(attempt)
        try:
            actual = transport.health("127.0.0.1", port, operation)
        except (ConnectionError, OSError) as error:
            attempt.update(outcome="raised", **_http_error(error))
            return None
        except Exception as error:
            attempt.update(outcome="raised", **_http_error(error))
            raise
        finally:
            attempt["elapsed_seconds"] = time.monotonic() - started - attempt["started_seconds"]
        attempt.update(outcome="response", status=actual["status"] if type(actual["status"]) is int else None)
        gate._require(
            actual["status"] == 200
            and actual["body_sha256"] == hashlib.sha256(b"OLD").hexdigest()
            and actual["operation"] is None
            and actual["upstream"] is None,
            "old synthetic HTTP did not reach the original fixed gateway",
        )
        return actual

    def preserve():
        value = {"schema": 1, "operation_id": operation, "target": {"host": "127.0.0.1", "port": port},
                 "wait_seconds": 10, "elapsed_seconds": time.monotonic() - started, "attempts": attempts,
                 "detailed_transport_stage": "not_observed", "synthetic_only": True}
        gate._require(len(gate._encoded(value)) <= 32768, "old HTTP diagnostics too large")
        gate._write(root / "OLD_HTTP_OBSERVATIONS.json", value)

    try:
        observed = harness.wait(healthy, seconds=10)
    except BaseException as error:
        try:
            preserve()
        except BaseException as evidence_error:
            raise error from evidence_error
        raise
    preserve()
    return observed


def _observe_old(harness, root, identifier, image, port, operation):
    prior = getattr(harness, "phase_deadline", None)
    deadline = time.monotonic() + 10
    harness.phase_deadline = min(prior, deadline) if prior is not None else deadline
    try:
        initial = _old_ingress(harness, root, identifier, image, port)
        observed = _old_http(harness, root, port, operation)
        before = _old_ingress(harness, root, identifier, image, port, confirmed=True)
        gate._require(time.monotonic() < harness.phase_deadline, "old ingress observation exceeded phase budget")
        gate._require(gateway.gateway_identity(before) == gateway.gateway_identity(initial)
                      and before["State"] == initial["State"]
                      and before["NetworkSettings"] == initial["NetworkSettings"], "old gateway changed during HTTP observation")
    finally:
        harness.phase_deadline = prior
    gate._write(
        root / "OLD_HTTP.json",
        {
            "id": identifier,
            "http": observed,
            "identity": gateway.gateway_identity(before),
            "actual_profile": harness.safe_inspect(before),
            "synthetic_only": True,
        },
    )
    gate._write(root / "OLD_TERM_INTENT.json", {"id": identifier, "signal": "TERM"})
    harness.command(["kill", "--signal", "TERM", identifier], timeout=3)

    def stopped():
        row = _inspect(harness, identifier)
        gate._require(
            gateway.gateway_identity(row) == gateway.gateway_identity(before) and not row["State"].get("OOMKilled"),
            "old gateway changed during natural stop",
        )
        if row["State"]["Running"]:
            return None
        gate._require(
            row["State"]["Status"] == "exited" and row["State"]["ExitCode"] in {0, 143},
            "old gateway did not stop naturally",
        )
        return row

    after = harness.wait(stopped, seconds=15)
    return after


def prepare(harness, p):
    directory = private.safe_path(p["directory"])
    gate._directory(directory)
    gate._require(
        directory.is_relative_to(private.safe_path(harness.output))
        and re.fullmatch(r"[0-9a-f]{32}", harness.owner)
        and "gateway_preparation" not in p
        and not (directory / "sealed").exists(),
        "gateway fixture must precede seal inside this owned synthetic run",
    )
    gate._require(
        set(p["manifest"]["services"]) == {"ai-filter-api", "ai-filter-worker"},
        "gateway fixture must retain exactly two candidate app services",
    )
    network = json.loads(harness.command(["network", "inspect", harness.network], timeout=3))[0]
    gate._require(
        network["Name"] == harness.network
        and network["Internal"] is True
        and network["Labels"].get(LABEL) == harness.owner
        and network["Driver"] == "bridge"
        and network["Scope"] == "local"
        and gate.HEX64.fullmatch(network["Id"]),
        "actual owned internal network required",
    )
    image = json.loads(harness.command(["image", "inspect", gateway.NGINX_REF], timeout=3))[0]
    image_checks(image, gateway.NGINX_REF)
    operation = hashlib.sha256(("synthetic-gateway:" + harness.owner).encode()).hexdigest()
    name, project = harness.prefix + "-gateway-old", harness.prefix + "-gateway-old-project"
    gate._require(
        project != p["project"] and project != gateway.gateway_name(operation).removesuffix("-gateway"),
        "old, new gateway and application projects must differ",
    )
    root = directory / "gateway-native"
    root.mkdir(mode=0o700)
    p["gateway_native_root"] = root
    evidence = root / "switch"
    evidence.mkdir(mode=0o700)
    old_config, new_config = root / "old-nginx.conf", root / "new-nginx.conf"
    gateway._save_raw(old_config, OLD_CONFIG)
    gateway._save_raw(new_config, gateway.render_config(operation, "ai-filter-api"))
    port = _port()
    gate._write(
        root / "PREPARE_INTENT.json",
        {
            "operation_id": operation,
            "owner": harness.owner,
            "network_id": network["Id"],
            "image_id": image["Id"],
            "old_name": name,
            "old_project": project,
            "host_binding": {"HostIp": "127.0.0.1", "HostPort": str(port)},
            "synthetic_only": True,
        },
    )
    identifier = _create_old(harness, root, name, project, network, image, old_config, port, operation)
    after = _observe_old(harness, root, identifier, image, port, operation)
    template = {
        "schema": 1,
        "operation_id": operation,
        "source_sha256": gateway.source_sha256(),
        "source_sha": p["source"]["source_sha"],
        "old_gateway_id": identifier,
        "old_gateway_identity": gateway.gateway_identity(after),
        "old_config": gateway.file_identity(old_config)[0],
        "new_config": gateway.file_identity(new_config)[0],
        "api_alias": "ai-filter-api",
        "network_id": network["Id"],
        "wait_seconds": 10,
        "max_polls": 10,
    }
    gate._write(
        root / "PREPARED.json",
        {
            "template": template,
            "old_state": after["State"],
            "old_gateway_retained": True,
            "gateway_switched": False,
            "production_access": False,
        },
    )
    gateway._save_raw(root / "manifest.before.json", p["manifest_path"].read_bytes())
    manifest = deepcopy(p["manifest"])
    for service in manifest["services"].values():
        service["environment"].update(
            {
                "AI_FILTER_TRUSTED_PROXY_HOST": gateway.gateway_name(operation),
                "AI_FILTER_WEB_BASE_URL": "http://" + gateway.gateway_name(operation) + ":5000",
            }
        )
    gate._write(p["manifest_path"], manifest)
    p["manifest"] = manifest
    p["gateway_preparation"], p["gateway_evidence"] = template, evidence
    return template


def bind_plan(p, permit):
    """Freeze only the later actual permit window; no Docker mutation."""
    gate._require(
        "gateway_plan" not in p and p["gateway_preparation"]["source_sha"] == permit["source"]["revision"],
        "one source-bound synthetic gateway plan required",
    )
    template = deepcopy(p["gateway_preparation"])
    template["window"] = {"not_before": permit["not_before"], "expires_at": permit["expires_at"]}
    gate._require(
        set(template) == gateway.PLAN_KEYS and template["source_sha256"] == gateway.source_sha256(),
        "gateway adapter source changed after fixture preparation",
    )
    for field in ("old_config", "new_config"):
        gate._require(gateway.file_identity(template[field]["path"])[0] == template[field], "gateway original changed")
    path = Path(p["gateway_evidence"]).parent / "PLAN.json"
    gate._require(not gate._present(path), "gateway native plan already attempted")
    gate._write(path, template)
    p["gateway_plan"] = template
    return template


def preserve(harness, p):
    """Only this operation's gateways: capture then TERM, never rm or KILL."""
    if "gateway_native_root" not in p:
        return
    root = Path(p["gateway_native_root"])
    if not gate._present(root / "PREPARE_INTENT.json"):
        return
    original = gateway.runtime.read_confirmed(root / "PREPARE_INTENT.json")
    old = _inspect(harness, original["old_name"], final=True)
    gate._require(
        old["Name"] == "/" + original["old_name"]
        and old["Image"] == original["image_id"]
        and old["Config"]["Labels"].get(LABEL) == harness.owner == original["owner"],
        "synthetic old gateway identity changed; retain without stop",
    )
    rows = [("old", old)]
    transport = gateway.NativeTransport()
    if "gateway_plan" in p:
        for purpose in ("config-test", "gateway"):
            row = transport.find(original["operation_id"], purpose)
            if row is None:
                continue
            expected_name = gateway.gateway_name(original["operation_id"]) + (
                "-config-test" if purpose == "config-test" else ""
            )
            gate._require(
                row["Name"] == "/" + expected_name
                and row["Config"]["Labels"].get("io.ai-filter.gateway.plan") == gate._hash(p["gateway_plan"]),
                "synthetic gateway identity changed; retain without stop",
            )
            rows.append((purpose, row))
    for purpose, row in rows:
        gate._write(root / ("FINAL_" + purpose + ".json"), harness.safe_inspect(row))
        harness.command(["logs", row["Id"]], timeout=3, final=True)
        if row["State"]["Running"]:
            term = root / ("OLD_TERM_INTENT.json" if purpose == "old" else "FINAL_TERM_" + purpose + ".json")
            if not gate._present(term) and not gate._present(term.with_suffix(".json.pending")):
                gate._write(term, {"id": row["Id"], "signal": "TERM"})
                harness.command(["kill", "--signal", "TERM", row["Id"]], timeout=3, final=True)
            deadline = time.monotonic() + 15
            for _ in range(30):
                gate._require(time.monotonic() < deadline, "synthetic gateway preserve deadline")
                actual = _inspect(harness, row["Id"], final=True)
                gate._require(time.monotonic() < deadline, "synthetic gateway observation exceeded deadline")
                if not actual["State"]["Running"]:
                    gate._write(root / ("STOPPED_" + purpose + ".json"), actual["State"])
                    break
                time.sleep(min(0.25, max(0, deadline - time.monotonic())))
            else:
                raise gate.AdmissionUncertain("synthetic gateway stop unconfirmed; no forced cleanup")
