"""Selected read-only host131 startup diagnostics, never execution proof.

Only fixed private files are read. Missing or changing live logs stay unknown;
neither stream counts nor startup markers prove daemon readiness or cleanup.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import time

from services import admission_fence as gate
from scripts import production_activation_host_native as isolated
from scripts import production_host_ingress_native as host
from scripts import production_namespace_native as reader

MAX_INPUT = 4 * 1024**2  # Existing bootstrap export has a separate 4 MiB cap.
MAX_FILE, MAX_JSON, MAX_PACKET, TAIL_BYTES = 2 * 1024**2, 256 * 1024, 16 * 1024, 8192
FIXED_FILES = {"PLAN.json", "INNER_RESULT.json", "HOLDER.json", "DAEMON.json", "DAEMON_INFO.json",
               "DAEMON_STOP.json", "RESOURCES_FINAL.json", "daemon.stdout", "daemon.stderr"}
MARKERS = {"initialization_completed": b"daemon has completed initialization",
           "api_listen": b"api listen on", "containerd_started": b"containerd successfully booted",
           "deadline_literal": b"context deadline exceeded", "permission_literal": b"permission denied",
           "memory_literal": b"out of memory", "error_level": b"level=error"}
REASONS = {"original_deadline_exhausted", "duplicate_json_key", "object_original_required",
           "fixed_original_required", "unsafe_or_oversized_original", "original_input_budget_exhausted",
           "original_short_read", "original_changed_during_read", "fixed_info_observation_missing",
           "command_observation_mismatch", "capture_receipt_mismatch", "original_plan_binding_mismatch",
           "private_inner_directory_required", "sealed_result_differs_from_observation",
           "inner_directory_changed", "original_root_changed", "diagnostic_packet_budget_exhausted"}


class EvidenceError(ValueError):
    pass


def require(value, code):
    if not value:
        raise EvidenceError(code)


def remaining(end):
    require(type(end) in (int, float) and math.isfinite(end) and time.monotonic() < end,
            "original_deadline_exhausted")


def failure(error):
    return {"status": "evidence_insufficient",
            "reason_code": str(error) if type(error) is EvidenceError and str(error) in REASONS
            else "read_or_projection_failure",
            "error_type": type(error).__name__ if type(error).__name__ in isolated.ERROR_TYPES | {"EvidenceError"}
            else "unrecognized", "error_sha256": hashlib.sha256(str(error).encode()).hexdigest()}


def _json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs)
    require(type(value) is dict, "object_original_required")
    return value


def _read(parent, name, budget, end, *, sealed=False, limit=MAX_FILE):
    remaining(end)
    require(name in FIXED_FILES or re.fullmatch(r"command-[0-9]{4,7}\.(json|stdout|stderr)", name),
            "fixed_original_required")
    source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        before = reader._metadata(os.fstat(source))
        require(stat.S_ISREG(before["mode"]) and before["nlink"] == 1 and before["uid"] == os.geteuid()
                and stat.S_IMODE(before["mode"]) in ({0o400} if sealed else {0o400, 0o600})
                and 0 <= before["size"] <= limit, "unsafe_or_oversized_original")
        require(budget["reserved"] + before["size"] <= MAX_INPUT, "original_input_budget_exhausted")
        budget["reserved"] += before["size"]
        raw = bytearray()
        while len(raw) < before["size"]:
            remaining(end)
            chunk = os.read(source, min(65536, before["size"] - len(raw)))
            require(bool(chunk), "original_short_read")
            raw.extend(chunk)
            budget["read"] += len(chunk)
        remaining(end)
        require(reader._metadata(os.fstat(source)) == before
                == reader._metadata(os.stat(name, dir_fd=parent, follow_symlinks=False)),
                "original_changed_during_read")
        return bytes(raw), {"sha256": hashlib.sha256(raw).hexdigest(), "metadata": before}
    finally:
        os.close(source)


def _markers(raw):
    partial = len(raw) > TAIL_BYTES
    window = raw[-TAIL_BYTES:]
    if partial:
        offset = window.find(b"\n")
        window = window[offset + 1:] if offset >= 0 else b""
    # An unfinished last line can still be in the live writer's buffer.
    lines = window.split(b"\n")[:-1]
    return {"scope": "complete_lines_in_bounded_live_log_snapshot_not_readiness",
            "counts": {key: sum(literal in line.lower() for line in lines) for key, literal in MARKERS.items()},
            "complete_lines": len(lines), "window_partial": partial or bool(window and not window.endswith(b"\n")),
            "absence_is_unknown": True, "writer_flush_and_eof_unconfirmed": True}


def _optional(parent, name, budget, end, *, markers=False, resources=False):
    try:
        raw, proof = _read(parent, name, budget, end, limit=MAX_JSON if resources else MAX_FILE)
        value = {"status": "stable_bounded_snapshot", "original": proof}
        if markers:
            value["markers"] = _markers(raw)
        if resources:
            value["profile"] = isolated._failure_resources(_json(raw))
        return value
    except FileNotFoundError:
        return {"status": "not_present_unknown"}
    except Exception as error:
        if type(error) is EvidenceError and str(error) == "original_deadline_exhausted":
            raise
        return failure(error)


def _command(parent, observation, capture, budget, end):
    require(observation is not None and observation["operation"] == "docker_info", "fixed_info_observation_missing")
    name = "command-%04d" % observation["sequence"]
    raw, proof = _read(parent, name + ".json", budget, end, limit=MAX_JSON)
    receipt = _json(raw)
    require(receipt.get("argv") == [*host.DOCKER, "info", "--format", "{{json .}}"]
            and receipt.get("transfer") is False
            and type(receipt.get("returncode")) is type(observation["returncode"])
            and receipt.get("returncode") == observation["returncode"]
            and type(receipt.get("elapsed")) in (int, float) and math.isfinite(receipt["elapsed"])
            and round(receipt["elapsed"], 6) == observation["elapsed_seconds"], "command_observation_mismatch")
    retained = host.safe_capture_diagnostic(receipt.get("capture_diagnostic"))
    require(capture is not None and retained == capture and retained["sequence"] == observation["sequence"],
            "capture_receipt_mismatch")
    times = [receipt.get("deadline_monotonic"), receipt.get("started_monotonic")]
    require(all(type(value) in (int, float) and math.isfinite(value) for value in times)
            and round(times[0] - times[1], 6) == observation["budget_seconds"] == capture["budget_seconds"]
            and observation["elapsed_seconds"] == capture["elapsed_seconds"]
            and observation["returncode"] == capture["client"]["returncode"], "command_observation_mismatch")
    return {"status": "available", "original": proof, "observation": observation, "capture": capture,
            "capture_scope": "pipe_read_observations_not_durable_bytes",
            "stdout": _optional(parent, name + ".stdout", budget, end),
            "stderr": _optional(parent, name + ".stderr", budget, end, markers=True)}


def _collect(root, result, end, packet, budget):
    remaining(end)
    outer = reader._evidence_root_fd(root)
    inner = None
    try:
        root_identity = reader._metadata(os.fstat(outer))
        plan_raw, plan_proof = _read(outer, "PLAN.json", budget, end, sealed=True, limit=MAX_JSON)
        plan = _json(plan_raw)
        require(plan.get("bootstrap_mode") == "private-host-loopback131-v1"
                and type(plan.get("started")) in (int, float) and math.isfinite(plan["started"])
                and plan.get("deadline") == end == plan["started"] + 180, "original_plan_binding_mismatch")
        packet["plan_original"] = plan_proof
        inner = os.open("inner", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=outer)
        inner_identity = reader._metadata(os.fstat(inner))
        require(inner_identity["uid"] == os.geteuid() and stat.S_IMODE(inner_identity["mode"]) == 0o700,
                "private_inner_directory_required")
        raw, proof = _read(inner, "INNER_RESULT.json", budget, end, sealed=True, limit=MAX_JSON)
        original = _json(raw)
        require(original == result, "sealed_result_differs_from_observation")
        packet["inner_original"] = proof
        packet["result_profile"] = isolated.safe_inner_result(original)
        observation = host.safe_command_failure(original.get("command_failure"))
        capture = host.safe_capture_diagnostic(original.get("command_capture"))
        try:
            packet["command"] = _command(inner, observation, capture, budget, end)
        except Exception as error:
            packet["command"] = failure(error)
        for name in ("HOLDER.json", "DAEMON.json", "DAEMON_INFO.json", "DAEMON_STOP.json",
                     "RESOURCES_FINAL.json", "daemon.stdout", "daemon.stderr"):
            packet["files"][name] = _optional(inner, name, budget, end, markers=name == "daemon.stderr",
                                               resources=name == "RESOURCES_FINAL.json")
        remaining(end)
        require(reader._metadata(os.fstat(inner)) == inner_identity
                == reader._metadata(os.stat("inner", dir_fd=outer, follow_symlinks=False)), "inner_directory_changed")
        reopened = reader._evidence_root_fd(root)
        try:
            require(reader._metadata(os.fstat(outer)) == root_identity == reader._metadata(os.fstat(reopened)),
                    "original_root_changed")
        finally:
            os.close(reopened)
        packet["status"] = ("diagnostic_only" if packet["command"]["status"] == "available"
                            and all(packet["command"][stream]["status"] != "evidence_insufficient"
                                    for stream in ("stdout", "stderr"))
                            and all(row["status"] != "evidence_insufficient" for row in packet["files"].values())
                            else "evidence_insufficient")
    finally:
        if inner is not None:
            os.close(inner)
        os.close(outer)


def export(root, result, end):
    packet = {"schema": 1, "scope": "selected_early_host131_originals_not_execution_proof", "complete": False,
              "execution_proof": False, "full_raw_archive_verified": False, "originals_modified": False,
              "status": "evidence_insufficient", "files": {}}
    if type(result) is dict and result.get("bootstrap_result") is not None:
        return {**packet, "status": "not_applicable_bootstrap_started"}
    budget = {"reserved": 0, "read": 0}
    try:
        _collect(root, result, end, packet, budget)
    except Exception as error:
        packet["error"] = failure(error)
    packet["original_bytes_read"] = budget["read"]
    packet["original_input_limit"] = MAX_INPUT
    require(len(gate._encoded(packet)) + 1 <= MAX_PACKET, "diagnostic_packet_budget_exhausted")
    return packet
