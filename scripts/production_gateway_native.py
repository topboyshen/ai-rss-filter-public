"""Synthetic gateway preparation for the actual activation native harness.

This helper is not a deployment entry point. It requires the harness's actual
owned internal network, preserves the old fixed-image gateway and its config,
and never pulls images, changes resource budgets, or removes containers.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
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

DIAGNOSTIC_BYTES = 32768


def _json_bound(value):
    pending, visited = [(value, 0)], 0
    while pending:
        row, depth = pending.pop()
        visited += 1
        if visited > 512 or depth > 16 or (type(row) in {dict, list} and len(row) > 512):
            return "shape_bound"
        if isinstance(row, str) and len(row) > DIAGNOSTIC_BYTES:
            return "byte_bound"
        if type(row) is dict:
            pending.extend((item, depth + 1) for pair in row.items() for item in pair)
        elif type(row) is list:
            pending.extend((item, depth + 1) for item in row)
        elif row is not None and type(row) not in {str, bool, int, float}:
            return "encoding_invalid"
        elif type(row) is int and row.bit_length() > 256:
            return "shape_bound"
    return None


def _value_proof(value):
    """Hash bounded JSON without making untrusted values public diagnostic text."""
    digest, total = hashlib.sha256(), 0
    bound = _json_bound(value)
    if bound:
        return {"complete": False, "reason": bound, "sha256": None}
    try:
        chunks = json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False).iterencode(value)
        for text in chunks:
            if len(text) > DIAGNOSTIC_BYTES:
                return {"complete": False, "reason": "byte_bound", "sha256": None}
            raw = text.encode()
            total += len(raw)
            if total > DIAGNOSTIC_BYTES:
                return {"complete": False, "reason": "byte_bound", "sha256": None}
            digest.update(raw)
    except (TypeError, ValueError):
        return {"complete": False, "reason": "encoding_invalid", "sha256": None}
    return {"complete": True, "bytes": total, "sha256": digest.hexdigest()}


def _mapping_diagnostic(value, *, present=True):
    proof = _value_proof(value)
    kind = "missing" if not present else "null" if value is None else "mapping" if type(value) is dict else "invalid"
    result = {"present": present, "kind": kind, "shape_valid": False, "proof": proof}
    if not present or value is None:
        return result
    if type(value) is not dict or len(value) > 8 or not proof["complete"]:
        return result
    entries = {}
    for name, rows in value.items():
        if not isinstance(name, str) or not re.fullmatch(r"[1-9][0-9]{0,4}/(?:tcp|udp)", name) or int(name.split("/")[0]) > 65535:
            return result
        if rows is None:
            entries[name] = None
            continue
        if type(rows) is not list or len(rows) > 8:
            return result
        bindings = []
        for row in rows:
            if type(row) is not dict or set(row) != {"HostIp", "HostPort"}:
                return result
            host, port = row["HostIp"], row["HostPort"]
            if not isinstance(host, str) or not isinstance(port, str) or not re.fullmatch(r"[1-9][0-9]{0,4}", port) or int(port) > 65535:
                return result
            # Only loopback/unspecified literals are meaningful public values.
            bindings.append({"host": host if host in {"127.0.0.1", "0.0.0.0", "::1", "::"} else "other",
                             "host_proof": _value_proof(host), "port": int(port)})
        entries[name] = bindings
    return {**result, "shape_valid": True, "entries": entries}


def _startup_diagnostic(row):
    state = row.get("State") if type(row.get("State")) is dict else {}
    started = state.get("StartedAt")
    status = state.get("Status")
    valid_started = isinstance(started, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z", started)
    return {"started_at": started if valid_started else None, "started_at_proof": _value_proof(started),
            "restart_count": row.get("RestartCount") if type(row.get("RestartCount")) is int and 0 <= row["RestartCount"] <= 2**31 else None,
            "pid": state.get("Pid") if type(state.get("Pid")) is int and 0 <= state["Pid"] <= 2**31 else None,
            "status": status if isinstance(status, str) and status in {"created", "running", "exited", "paused", "restarting", "dead"} else None,
            "flags": {key: state[key] if type(state.get(key)) is bool else None
                      for key in ("Running", "OOMKilled", "Restarting", "Paused", "Dead")}}


def _diagnostic_original(path, value):
    """Selected observations only; O_EXCL keeps failures and partial originals."""
    gate._directory(path.parent)
    raw = gate._encoded(value)
    gate._require(len(raw) <= DIAGNOSTIC_BYTES, "old ingress diagnostic byte bound")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    gate._sync_directory(path.parent)
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _record_ingress(root, row, profile, requested, actual, *, confirmed):
    phase = "after_http" if confirmed else "before_http"
    observation = {"schema": 1, "phase": phase, "selected_observation_not_full_inspect": True,
                   "startup": _startup_diagnostic(row),
                   "requested": _mapping_diagnostic(requested, present="PortBindings" in row["HostConfig"]),
                   "actual": _mapping_diagnostic(actual, present="Ports" in row["NetworkSettings"]),
                   "networks_proof": _value_proof(row["NetworkSettings"].get("Networks")), "synthetic_only": True}
    record = {**profile, "phase": phase, "observation": observation,
              "diagnostic_only_not_execution_proof": True, "diagnostic_durability_confirmed": False}
    try:
        record["observation_original"] = _diagnostic_original(
            root / ("OLD_INGRESS_AFTER.original.json" if confirmed else "OLD_INGRESS_BEFORE.original.json"), observation)
        stored = {key: value for key, value in record.items() if key != "diagnostic_durability_confirmed"}
        record["profile_original"] = _diagnostic_original(
            root / ("OLD_INGRESS_CONFIRMED.json" if confirmed else "OLD_INGRESS_PROFILE.json"), stored)
    except BaseException:
        try:
            _emit_ingress_diagnostic(record)
        except BaseException:
            pass  # Failed reporting cannot replace the original write/sync error.
        raise
    record["diagnostic_durability_confirmed"] = True
    _emit_ingress_diagnostic(record)


def _emit_ingress_diagnostic(record):
    raw = gate._encoded(record)
    gate._require(len(raw) <= DIAGNOSTIC_BYTES, "old ingress stdout byte bound")
    print("gateway_ingress_observation=" + raw.decode(), flush=True)


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
               "actual_mapping_sha256": _value_proof(actual)["sha256"], "synthetic_only": True}
    _record_ingress(root, row, profile, requested, actual, confirmed=confirmed)
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
    isolated = network["Internal"] is True
    if not isolated:
        from scripts.production_activation_host_native import require_harness_network
        require_harness_network(harness, network)
        isolated = True
    gate._require(
        network["Name"] == harness.network
        and isolated
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


def _cleanup_identity(row):
    """Stable original identity across TERM; process exit is checked separately."""
    return {key: deepcopy(row[key]) for key in (
        "Id", "Name", "Image", "Config", "HostConfig", "Mounts", "RestartCount")}, row["State"]["StartedAt"]


def _normal_cleanup_exit(row, original, *, purpose):
    gate._require(_cleanup_identity(row) == original, "owned cleanup identity changed")
    state = row["State"]
    finished = state.get("FinishedAt")
    try:
        parsed = datetime.fromisoformat(finished.replace("Z", "+00:00")) if isinstance(finished, str) else None
        valid_time = (parsed is not None and parsed.tzinfo is not None
                      and parsed.utcoffset() == timezone.utc.utcoffset(parsed)
                      and parsed.timestamp() > 0)
    except (ValueError, OverflowError, OSError):
        valid_time = False
    gate._require(state.get("Running") is False and state.get("Status") == "exited"
                  and type(state.get("ExitCode")) is int
                  and state["ExitCode"] in ({0} if purpose == "config-test" else {0, 143})
                  and not any(state.get(key) for key in ("Paused", "Dead", "Restarting", "OOMKilled"))
                  and valid_time, "normal owned process exit unconfirmed")
    return state


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
        original_identity = _cleanup_identity(row)
        gate._write(root / ("FINAL_" + purpose + ".json"), harness.safe_inspect(row))
        harness.command(["logs", row["Id"]], timeout=3, final=True)
        actual = _inspect(harness, row["Id"], final=True)
        gate._require(_cleanup_identity(actual) == original_identity, "owned cleanup identity changed")
        if actual["State"]["Running"]:
            gate._require(not any(actual["State"].get(key) for key in ("Paused", "Dead", "Restarting", "OOMKilled")),
                          "owned cleanup running state uncertain")
            term = root / ("OLD_TERM_INTENT.json" if purpose == "old" else "FINAL_TERM_" + purpose + ".json")
            if not gate._present(term) and not gate._present(term.with_suffix(".json.pending")):
                gate._write(term, {"id": row["Id"], "signal": "TERM"})
                harness.command(["kill", "--signal", "TERM", row["Id"]], timeout=3, final=True)
            deadline = time.monotonic() + 15
            for _ in range(30):
                gate._require(time.monotonic() < deadline, "synthetic gateway preserve deadline")
                actual = _inspect(harness, row["Id"], final=True)
                gate._require(time.monotonic() < deadline, "synthetic gateway observation exceeded deadline")
                gate._require(_cleanup_identity(actual) == original_identity, "owned cleanup identity changed")
                if not actual["State"]["Running"]:
                    break
                time.sleep(min(0.25, max(0, deadline - time.monotonic())))
            else:
                raise gate.AdmissionUncertain("synthetic gateway stop unconfirmed; no forced cleanup")
        state = _normal_cleanup_exit(actual, original_identity, purpose=purpose)
        gate._write(root / ("STOPPED_" + purpose + ".json"), state)
