"""Opt-in, bounded command observations for the isolated native activation proof.

No command arguments, input/output text, environment, identifiers or paths are
retained. These observations do not enforce, extend or replace any deadline.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import json
import math
from pathlib import Path
import subprocess
import time

from scripts import production_native_deadline as native_deadline

SCHEMA = "private-release-commands-v1"
MAX_BYTES = 8192
RECENT_LIMIT = 8
STATE = ContextVar("private_release_native_diagnostics", default=None)
ROOT = Path(__file__).resolve().parents[1]

# Keys are reviewed source locations, never values supplied by a command.
CALLSITES = {
    ("scripts/private_release_contract.py", "source_identity"): "source_identity",
    ("scripts/private_release_contract.py", "compose"): "compose_contract",
    ("scripts/private_release_contract.py", "inspect_project"): "project_inspection",
    ("scripts/private_release_contract.py", "preflight"): "private_preflight",
    ("scripts/private_release_contract.py", "verify_service_execution"): "service_execution",
    ("scripts/private_release_contract.py", "verify_app_loaders"): "application_loader",
    ("scripts/private_release_contract.py", "_verify_runtime_records"): "runtime_records",
    ("services/production_migration.py", "_initial_candidates"): "migration_candidates",
    ("services/production_container_lifecycle.py", "start"): "candidate_start",
    ("services/production_startup_control.py", "_container"): "startup_container",
    ("services/production_startup_control.py", "verify_container"): "startup_image",
    ("services/production_startup_control.py", "_actual_process"): "startup_process",
    ("scripts/production_activation_fixture.py", "seal_inputs"): "activation_seal",
    ("scripts/production_startup_fixture.py", "seal_inputs"): "startup_seal",
    ("scripts/verify_production_startup_native.py", "kill_owner"): "negative_owner_stop",
    ("scripts/verify_production_startup_native.py", "negative_duplicate"): "negative_duplicate",
    ("scripts/verify_production_startup_native.py", "prove_closed"): "closed_state_proof",
    ("scripts/verify_production_startup_native.py", "replace_worker"): "negative_worker_replace",
    ("scripts/verify_production_startup_native.py", "scenario"): "startup_scenario",
    ("scripts/verify_production_startup_native.py", "build_candidate"): "startup_build_inspect",
    ("scripts/verify_production_startup_native.py", "preserve_scenario"): "startup_preserve",
    ("scripts/verify_production_startup_native.py", "execute"): "startup_architecture",
    ("scripts/verify_private_release_native.py", "verify_config_reader_identity"): "config_identity",
    ("scripts/verify_private_release_native.py", "build_candidate"): "private_build_inspect",
    ("scripts/verify_private_release_native.py", "execute"): "private_native_scenario",
}
OPERATIONS = {
    "git_status", "git_revision", "git_tree", "docker_ps", "docker_inspect",
    "docker_image_inspect", "docker_network_inspect", "docker_volume_inspect",
    "docker_exec_python", "docker_exec_sha256sum", "docker_start", "docker_kill",
    "docker_run", "docker_create", "docker_info", "docker_pull",
    "docker_network_create", "docker_volume_create", "docker_compose_config",
    "docker_compose_create", "docker_compose_up", "docker_compose_stop",
    "docker_compose_down", "unknown",
}
CONTEXTS = {"receiver_prepare", "receiver_query", "receiver_confirm", "other"}
PHASES = {"budget", "spawn", "stream", "wait", "close", "complete"}
ERRORS = {None, "contract_error", "stream_timeout", "wait_timeout", "process_timeout",
          "output_limit", "nonzero_rc", "timeout", "os_error", "interrupted", "other"}
RECORD_KEYS = {
    "sequence", "operation", "callsite", "line", "context", "state", "phase", "error",
    "elapsed_seconds", "requested_seconds", "clamped_seconds", "active_remaining_start",
    "total_remaining_start", "active_remaining_end", "total_remaining_end", "returncode",
    "stdout_eof", "stderr_eof", "stdout_bytes", "stderr_bytes",
}


def _operation(args):
    # Inspect fixed positions only. Unknown commands do not become new labels.
    if type(args) not in (list, tuple) or not args or len(args) > 4096:
        return "unknown"
    if not all(type(value) is str for value in args):
        return "unknown"
    if args[:2] == ["git", "-C"] or tuple(args[:2]) == ("git", "-C"):
        if len(args) == 6 and list(args[3:]) == ["status", "--porcelain=v1", "--untracked-files=all"]:
            return "git_status"
        if len(args) == 5 and args[3] == "rev-parse":
            return {"HEAD": "git_revision", "HEAD^{tree}": "git_tree"}.get(args[4], "unknown")
    if args[0] != "docker" or len(args) < 2:
        return "unknown"
    if args[1] in {"ps", "inspect", "start", "kill", "run", "create", "info", "pull"}:
        return "docker_" + args[1]
    if len(args) >= 3 and args[1] in {"image", "network", "volume"}:
        candidate = "docker_" + args[1] + "_" + args[2]
        return candidate if candidate in OPERATIONS else "unknown"
    if len(args) >= 4 and args[1] == "exec":
        return {"python": "docker_exec_python", "sha256sum": "docker_exec_sha256sum"}.get(args[3], "unknown")
    if args[1] == "compose":
        index = 2
        while index < len(args) and args[index] in {"--project-name", "--project-directory", "--env-file", "-f", "--file"}:
            index += 2
        if index < len(args):
            candidate = "docker_compose_" + args[index]
            return candidate if candidate in OPERATIONS else "unknown"
    return "unknown"


def _location(frame):
    try:
        relative = str(Path(frame.f_code.co_filename).relative_to(ROOT))
    except ValueError:
        return "unknown", 0, "other"
    label = CALLSITES.get((relative, frame.f_code.co_name), "unknown")
    line = frame.f_lineno if label != "unknown" else 0
    contexts = set()
    for _ in range(32):
        if frame is None:
            break
        if frame.f_code.co_filename == str(ROOT / "services/production_activation_control.py"):
            candidate = "receiver_" + frame.f_code.co_name
            if candidate in CONTEXTS:
                contexts.add(candidate)
        frame = frame.f_back
    context = next((name for name in ("receiver_confirm", "receiver_prepare", "receiver_query") if name in contexts), "other")
    return label, line, context


def _remaining(now):
    state = native_deadline.STATE.get()
    return (None, None) if state is None else tuple(round(value - now, 6) for value in state)


def _number(value, low, high, *, optional=False):
    return (optional and value is None) or (type(value) in (int, float) and math.isfinite(value) and low <= value <= high)


def _record_valid(row):
    if type(row) is not dict or set(row) != RECORD_KEYS:
        return False
    return (
        type(row["sequence"]) is int and 1 <= row["sequence"] <= 1000000
        and row["operation"] in OPERATIONS
        and row["callsite"] in {*CALLSITES.values(), "unknown"}
        and type(row["line"]) is int and 0 <= row["line"] <= 100000
        and row["context"] in CONTEXTS and row["state"] in {"running", "success", "failed"}
        and row["phase"] in PHASES and row["error"] in ERRORS
        and _number(row["elapsed_seconds"], 0, 600)
        and type(row["requested_seconds"]) is int and row["requested_seconds"] == 120
        and (row["clamped_seconds"] is None or (_number(row["clamped_seconds"], 0, 120)
                                               and row["clamped_seconds"] > 0))
        and all(_number(row[key], -600, 600, optional=True) for key in (
            "active_remaining_start", "total_remaining_start", "active_remaining_end", "total_remaining_end"))
        and (row["returncode"] is None or (type(row["returncode"]) is int and -255 <= row["returncode"] <= 255))
        and all(type(row[key]) is bool for key in ("stdout_eof", "stderr_eof"))
        and all(type(row[key]) is int and 0 <= row[key] <= 4 * 1024 * 1024 + 65536
                for key in ("stdout_bytes", "stderr_bytes"))
        and (row["state"] != "success" or (row["phase"] == "complete" and row["error"] is None
                                         and row["returncode"] == 0 and row["stdout_eof"] and row["stderr_eof"]))
        and (row["state"] != "failed" or row["error"] is not None)
    )


def safe_summary(value):
    """Fail closed on unknown fields, values or size; return a detached projection."""
    try:
        if type(value) is not dict or set(value) != {"schema", "status", "calls", "omitted", "first_failure", "recent"}:
            return None
        if value["schema"] != SCHEMA or value["status"] not in {"complete", "evidence_insufficient"}:
            return None
        if any(type(value[key]) is not int or not 0 <= value[key] <= 1000000 for key in ("calls", "omitted")):
            return None
        rows = value["recent"]
        if type(rows) is not list or len(rows) > RECENT_LIMIT or value["omitted"] != max(0, value["calls"] - len(rows)):
            return None
        first = value["first_failure"]
        if (first is not None and (not _record_valid(first) or first["state"] != "failed")) or not all(_record_valid(row) for row in rows):
            return None
        numbers = [row["sequence"] for row in rows]
        if numbers != sorted(set(numbers)) or any(number > value["calls"] for number in numbers):
            return None
        if first is not None and first["sequence"] > value["calls"]:
            return None
        if first is not None and any(row["sequence"] == first["sequence"] and row != first for row in rows):
            return None
        if value["status"] == "complete" and any(row["operation"] == "unknown" or row["callsite"] == "unknown"
                                                  or row["state"] == "running" for row in [*rows, *([first] if first else [])]):
            return None
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        return json.loads(raw) if len(raw) <= MAX_BYTES else None
    except BaseException:  # Diagnostic data must not replace the actual failure.
        return None


class Observation:
    def __init__(self):
        self.calls = 0
        self.recent = deque(maxlen=RECENT_LIMIT)
        self.first_failure = None
        self.incomplete = False

    def summary(self):
        try:
            rows = [dict(token["record"]) for token in self.recent]
            insufficient = self.incomplete or any(row["state"] == "running" for row in rows)
            return safe_summary({"schema": SCHEMA, "status": "evidence_insufficient" if insufficient else "complete",
                                 "calls": self.calls, "omitted": max(0, self.calls - len(rows)),
                                 "first_failure": self.first_failure, "recent": rows})
        except BaseException:
            return None


@contextmanager
def scope():
    """Explicit native-only opt-in; nested scopes share the original bounded ring."""
    inherited = STATE.get()
    if inherited is not None:
        yield inherited
        return
    observer = Observation()
    token = STATE.set(observer)
    try:
        yield observer
    finally:
        STATE.reset(token)


def begin(args, frame):
    observer = STATE.get()
    if observer is None:
        return None
    try:
        now = time.monotonic()
        operation = _operation(args)
        callsite, line, context = _location(frame)
        active, total = _remaining(now)
        observer.calls += 1
        observer.incomplete |= operation == "unknown" or callsite == "unknown"
        row = {"sequence": observer.calls, "operation": operation, "callsite": callsite, "line": line,
               "context": context, "state": "running", "phase": "budget", "error": None,
               "elapsed_seconds": 0.0, "requested_seconds": 120, "clamped_seconds": None,
               "active_remaining_start": active, "total_remaining_start": total,
               "active_remaining_end": None, "total_remaining_end": None, "returncode": None,
               "stdout_eof": False, "stderr_eof": False, "stdout_bytes": 0, "stderr_bytes": 0}
        token = {"observer": observer, "started": now, "record": row}
        observer.recent.append(token)
        return token
    except BaseException:
        observer.incomplete = True
        return None


def applied(token, seconds):
    try:
        if token is not None:
            token["record"]["clamped_seconds"] = seconds
    except BaseException:
        if STATE.get() is not None:
            STATE.get().incomplete = True


def finish(token, *, error, phase, process, stdout_eof, stderr_eof, stdout_bytes, stderr_bytes):
    """Freeze the failure before client cleanup; never poll/wait or touch a pipe."""
    try:
        if token is None or token["record"]["state"] != "running":
            return
        now = time.monotonic()
        active, total = _remaining(now)
        category = None
        if error is not None:
            if isinstance(error, subprocess.TimeoutExpired):
                category = "wait_timeout" if phase == "wait" else "process_timeout"
            elif isinstance(error, TimeoutError):
                category = "timeout"
            elif isinstance(error, OSError):
                category = "os_error"
            elif isinstance(error, (KeyboardInterrupt, SystemExit)):
                category = "interrupted"
            elif type(error).__name__ == "ContractError" and type(error).__module__ in {
                    "scripts.private_release_contract", "private_release_contract", "__main__"}:
                category = {
                    ("private release subprocess timed out",): "stream_timeout",
                    ("private release output too large",): "output_limit",
                    ("private release subprocess failed",): "nonzero_rc",
                }.get(error.args, "contract_error")
            else:
                category = "other"
        row = token["record"]
        row.update(state="failed" if error is not None else "success", phase=phase, error=category,
                   elapsed_seconds=round(now - token["started"], 6), active_remaining_end=active,
                   total_remaining_end=total, returncode=getattr(process, "returncode", None),
                   stdout_eof=stdout_eof, stderr_eof=stderr_eof, stdout_bytes=stdout_bytes, stderr_bytes=stderr_bytes)
        observer = token["observer"]
        if error is not None and observer.first_failure is None:
            observer.first_failure = dict(row)
    except BaseException:
        if STATE.get() is not None:
            STATE.get().incomplete = True
