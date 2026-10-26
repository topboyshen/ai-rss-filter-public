#!/usr/bin/env python3
"""Capture bounded, redacted Docker evidence for a release health timeout.

The helper is deliberately read-only with respect to Docker.  It accepts the
release registry chosen by ``release.sh``, but always writes into the fixed
``diagnostics`` child of that registry.  Each invocation creates a private,
unique directory and seals it with hashes plus either a ``SEALED`` or
``PARTIAL`` marker.  Evidence is never cleaned up by this program.

Only three Docker command shapes are allowed:

* ``docker compose ... ps --all -q SERVICE``
* ``docker inspect --type container --format FIXED_TEMPLATE CONTAINER``
* ``docker logs --timestamps --tail 200 CONTAINER``

The inspect template is fixed below and contains container state only.  Docker
configuration and, in particular, ``Config.Env`` are never requested.
"""

from __future__ import annotations

import argparse
from collections import Counter
import dataclasses
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import uuid
from typing import Any, Iterable, Mapping, Sequence


SCHEMA = "ai-filter-release-health-timeout-evidence-v1"
DIAGNOSTICS_DIRNAME = "diagnostics"
DOCKER = "docker"
LOG_TAIL_LINES = 200
MAX_SERVICES = 16
MAX_CONTAINERS_PER_SERVICE = 4
MAX_COMPOSE_BYTES = 4 * 1024 * 1024
MAX_COMMAND_STREAM_BYTES = 256 * 1024
MAX_TEXT_FIELD_CHARS = 4096
MAX_HEALTH_HISTORY = 10
MAX_TOTAL_FILE_BYTES = 4 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 4.0
TOTAL_TIMEOUT_SECONDS = 15.0
RETENTION_DAYS = 30
LOG_SERVICE_ALLOWLIST = frozenset({"redis"})
DOCKER_STATES = frozenset(
    {
        "created",
        "dead",
        "exited",
        "healthy",
        "missing",
        "paused",
        "removing",
        "restarting",
        "running",
        "starting",
        "unhealthy",
        "unknown",
    }
)

SERVICE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
PHASE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
CONTAINER_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
HASH_RE = re.compile(r"\A[0-9a-fA-F]{64}\Z")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# This is the only Docker inspect format the helper will execute.  It exposes
# .State and .RestartCount, not the container configuration or environment.
INSPECT_FORMAT = (
    '{"Id":{{json .Id}},"Name":{{json .Name}},'
    '"RestartCount":{{json .RestartCount}},"State":{{json .State}}}'
)

SENSITIVE_KEY_RE = re.compile(
    r"(?:authorization|proxy[-_]?authorization|bearer|cookie|set[-_]?cookie|"
    r"password|passwd|pwd|secret|client[-_]?secret|token|access[-_]?token|"
    r"refresh[-_]?token|api[-_]?key|apikey|credential|private[-_]?key)",
    re.IGNORECASE,
)
NAMED_SECRET_RE = re.compile(
    r"(?i)(\b(?:authorization|proxy-authorization|cookie|set-cookie|password|"
    r"passwd|pwd|secret|client[_-]?secret|token|access[_-]?token|"
    r"refresh[_-]?token|api[_-]?key|apikey|credential)\b\s*[:=]\s*)"
    r"(?:Bearer\s+)?(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
)
JSON_SECRET_RE = re.compile(
    r"(?i)((?:\"|')(?:authorization|proxy-authorization|cookie|set-cookie|"
    r"password|passwd|pwd|secret|client[_-]?secret|token|access[_-]?token|"
    r"refresh[_-]?token|api[_-]?key|apikey|credential)(?:\"|')\s*:\s*)"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^,}\]\r\n]+)"
)
QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:access_token|refresh_token|token|api_key|apikey|key|secret|"
    r"password)=)[^&#\s]*"
)
URL_USERINFO_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/@\s]+@")
TELEGRAM_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
ACTIVE_RUN_DIR: Path | None = None


class SafetyError(RuntimeError):
    """Raised when an input or output path fails a safety check."""


class SafeArgumentParser(argparse.ArgumentParser):
    """Reject invalid CLI input without echoing user-provided values."""

    def error(self, message: str) -> None:
        del message
        raise SafetyError("invalid diagnostic capture arguments")


@dataclasses.dataclass(frozen=True)
class CommandResult:
    kind: str
    argv: tuple[str, ...]
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: int

    @property
    def complete(self) -> bool:
        return (
            self.returncode == 0
            and not self.timed_out
            and not self.stdout_truncated
            and not self.stderr_truncated
        )


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_text(value: dt.datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def reject_unsafe_text(value: str, label: str, limit: int) -> str:
    if not value or len(value) > limit or "\n" in value or "\r" in value or CONTROL_RE.search(value):
        raise SafetyError(f"{label} is empty, too long, or contains control characters")
    return value


def redact_text(value: str) -> str:
    """Redact common credential forms without relying on service log syntax."""

    value = CONTROL_RE.sub("?", value)
    value = JSON_SECRET_RE.sub(lambda match: f'{match.group(1)}"[REDACTED]"', value)
    value = NAMED_SECRET_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", value)
    value = QUERY_SECRET_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", value)
    value = URL_USERINFO_RE.sub(lambda match: f"{match.group(1)}[REDACTED]@", value)
    value = TELEGRAM_TOKEN_RE.sub("[REDACTED_TELEGRAM_TOKEN]", value)
    value = JWT_RE.sub("[REDACTED_JWT]", value)
    value = AWS_KEY_RE.sub("[REDACTED_ACCESS_KEY]", value)
    if len(value) > MAX_TEXT_FIELD_CHARS:
        value = value[:MAX_TEXT_FIELD_CHARS] + "...[TRUNCATED]"
    return value


def classify_untrusted_text(value: str) -> str:
    lowered = value.casefold()
    categories = (
        ("permission_denied", ("permission denied", "operation not permitted")),
        ("address_in_use", ("address already in use", "bind: address")),
        ("out_of_memory", ("out of memory", "oom", "cannot allocate memory")),
        ("connection_refused", ("connection refused",)),
        ("fatal", ("fatal", "panic", "segmentation fault")),
        ("failed", ("failed", "failure", "can't", "cannot")),
        ("warning", ("warning", "warn")),
        ("ready", ("ready", "pong", "accept connections")),
        ("shutdown", ("shutdown", "exiting", "sigterm", "sigint")),
        ("persistence", ("rdb", "aof", "background saving", "loading db")),
        ("startup", ("starting", "server initialized", "configuration loaded")),
    )
    for category, needles in categories:
        if any(needle in lowered for needle in needles):
            return category
    return "other"


def untrusted_text_summary(value: str | bytes | None) -> dict[str, Any]:
    if value is None:
        data = b""
        decoded = ""
    elif isinstance(value, bytes):
        data = value
        decoded = value.decode("utf-8", errors="replace")
    else:
        data = value.encode("utf-8", errors="replace")
        decoded = value
    return {
        "present": bool(data),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "category": classify_untrusted_text(decoded),
    }


def log_stream_summary(data: bytes, *, truncated: bool) -> dict[str, Any]:
    text = data.decode("utf-8", errors="replace")
    categories = Counter(
        classify_untrusted_text(line) for line in text.splitlines() if line
    )
    return {
        "bytes_captured": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "line_count": len(text.splitlines()),
        "categories": dict(sorted(categories.items())),
        "truncated": truncated,
    }


def redact_object(value: Any, *, key: str = "") -> Any:
    if SENSITIVE_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {str(k): redact_object(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_object(item) for item in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return redact_text(str(value))


def redacted_stream(data: bytes, *, byte_limit: int) -> tuple[bytes, bool]:
    text = data.decode("utf-8", errors="replace")
    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        ending = "\n" if line.endswith(("\n", "\r")) else ""
        body = line.rstrip("\r\n")
        lines.append(redact_text(body) + ending)
    encoded = "".join(lines).encode("utf-8")
    if len(encoded) <= byte_limit:
        return encoded, False
    marker = b"\n...[REDACTED OUTPUT TRUNCATED BY BYTE LIMIT]\n"
    keep = max(0, byte_limit - len(marker))
    safe_prefix = encoded[:keep].decode("utf-8", errors="ignore").encode("utf-8")
    return safe_prefix + marker, True


def path_metadata(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SafetyError(f"cannot inspect {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise SafetyError(f"symlink paths are not allowed: {path}")
    return metadata


def require_owned_directory(path: Path, label: str, *, private: bool = False) -> Path:
    metadata = path_metadata(path)
    if not stat.S_ISDIR(metadata.st_mode):
        raise SafetyError(f"{label} is not a directory: {path}")
    if metadata.st_uid != os.geteuid():
        raise SafetyError(f"{label} is not owned by effective uid {os.geteuid()}: {path}")
    if metadata.st_mode & 0o022:
        raise SafetyError(f"{label} must not be group/world writable: {path}")
    if private and metadata.st_mode & 0o077:
        raise SafetyError(f"{label} must have no group/world permissions: {path}")
    return path.resolve(strict=True)


def require_owned_file(path: Path, label: str) -> Path:
    metadata = path_metadata(path)
    if not stat.S_ISREG(metadata.st_mode):
        raise SafetyError(f"{label} is not a regular file: {path}")
    if metadata.st_uid != os.geteuid():
        raise SafetyError(f"{label} is not owned by effective uid {os.geteuid()}: {path}")
    if metadata.st_mode & 0o022:
        raise SafetyError(f"{label} must not be group/world writable: {path}")
    return path.resolve(strict=True)


def ensure_private_child(parent: Path, name: str) -> Path:
    if name != DIAGNOSTICS_DIRNAME:
        raise SafetyError("diagnostics directory name is not the fixed allowlisted name")
    candidate = parent / name
    try:
        os.mkdir(candidate, 0o700)
    except FileExistsError:
        pass
    resolved = require_owned_directory(candidate, "diagnostics directory", private=True)
    if os.path.commonpath((str(parent), str(resolved))) != str(parent) or resolved == parent:
        raise SafetyError("diagnostics directory must be a true child of the release registry")
    return resolved


def make_run_directory(diagnostics: Path, phase: str) -> tuple[str, Path]:
    timestamp = utc_now().strftime("%Y%m%dT%H%M%S.%fZ")
    for _ in range(16):
        run_id = f"{timestamp}-{phase}-{uuid.uuid4().hex[:12]}"
        candidate = diagnostics / run_id
        try:
            os.mkdir(candidate, 0o700)
        except FileExistsError:
            continue
        resolved = require_owned_directory(candidate, "diagnostic run directory", private=True)
        if resolved.parent != diagnostics:
            raise SafetyError("diagnostic run directory escaped diagnostics root")
        return run_id, resolved
    raise SafetyError("could not allocate a unique diagnostic run directory")


def make_private_directory(parent: Path, name: str) -> Path:
    if not SERVICE_RE.fullmatch(name.split("-", 1)[-1]):
        # The caller supplies an ordinal prefix; validate the service suffix.
        raise SafetyError(f"unsafe evidence directory name: {name}")
    candidate = parent / name
    os.mkdir(candidate, 0o700)
    resolved = require_owned_directory(candidate, "service evidence directory", private=True)
    if resolved.parent != parent:
        raise SafetyError("service evidence directory escaped run directory")
    return resolved


def open_new_file(path: Path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    return os.fdopen(descriptor, "wb")


def write_bytes(path: Path, data: bytes, budget: list[int]) -> None:
    if len(data) > budget[0]:
        raise SafetyError("diagnostic evidence exceeded the fixed total byte limit")
    with open_new_file(path) as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    budget[0] -= len(data)


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_json(path: Path, value: Any, budget: list[int]) -> None:
    write_bytes(path, json_bytes(redact_object(value)), budget)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(128 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    # The CLI parent may exit while a descendant still owns the pipes. Kill
    # the whole private group regardless; ProcessLookupError means it is gone.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_bounded(
    kind: str,
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None,
    global_deadline: float,
) -> CommandResult:
    started = time.monotonic()
    remaining = global_deadline - started
    if remaining <= 0:
        return CommandResult(kind, tuple(argv), None, b"", b"", True, False, False, 0)
    timeout = min(COMMAND_TIMEOUT_SECONDS, remaining)
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env) if env is not None else None,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        message = redact_text(str(exc)).encode("utf-8", errors="replace")
        return CommandResult(kind, tuple(argv), None, b"", message, False, False, False, 0)

    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}
    deadline = started + timeout
    timed_out = False

    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                terminate_process_group(process)
                break
            events = selector.select(timeout=min(remaining, 0.25))
            if not events and process.poll() is not None:
                # The final select/read pass below drains data already in pipes.
                events = [(key, selectors.EVENT_READ) for key in selector.get_map().values()]
            for key, _ in events:
                try:
                    block = os.read(key.fileobj.fileno(), 8192)
                except BlockingIOError:
                    continue
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                name = key.data
                available = MAX_COMMAND_STREAM_BYTES - len(buffers[name])
                if available > 0:
                    buffers[name].extend(block[:available])
                if len(block) > max(available, 0):
                    truncated[name] = True
        # Do not attempt a direct drain after timeout. A descendant can retain
        # the pipe even after the Docker CLI exits, and a blocking read here
        # would delay rollback indefinitely. Evidence already read by the
        # selector is retained; the timeout flag makes the bundle PARTIAL.
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()

    try:
        returncode = process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        terminate_process_group(process)
        try:
            returncode = process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            returncode = None
            timed_out = True

    return CommandResult(
        kind=kind,
        argv=tuple(argv),
        returncode=returncode,
        stdout=bytes(buffers["stdout"]),
        stderr=bytes(buffers["stderr"]),
        timed_out=timed_out,
        stdout_truncated=truncated["stdout"],
        stderr_truncated=truncated["stderr"],
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def command_record(result: CommandResult, *, service: str, container_id: str | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "kind": result.kind,
        "service": service,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
        "duration_ms": result.duration_ms,
        "stderr_bytes": len(result.stderr),
        "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
    }
    if container_id is not None:
        record["container_id"] = container_id
    return record


def fixed_state_and_health(payload: Any, expected_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("inspect response is not an object")
    state = payload.get("State")
    if not isinstance(state, dict):
        raise ValueError("inspect response has no State object")
    reported_id = payload.get("Id")
    if not isinstance(reported_id, str) or not reported_id:
        raise ValueError("inspect response has no container Id")
    if not (reported_id == expected_id or reported_id.startswith(expected_id) or expected_id.startswith(reported_id)):
        raise ValueError("inspect response container Id does not match compose ps")

    fixed = {
        "container_id": expected_id,
        "reported_id": reported_id,
        "name": payload.get("Name"),
        "restart_count": payload.get("RestartCount"),
        "status": state.get("Status"),
        "running": state.get("Running"),
        "paused": state.get("Paused"),
        "restarting": state.get("Restarting"),
        "oom_killed": state.get("OOMKilled"),
        "dead": state.get("Dead"),
        "pid": state.get("Pid"),
        "exit_code": state.get("ExitCode"),
        "error": untrusted_text_summary(state.get("Error")),
        "started_at": state.get("StartedAt"),
        "finished_at": state.get("FinishedAt"),
    }

    health = state.get("Health")
    if isinstance(health, dict):
        fixed["health_status"] = health.get("Status")
        fixed["health_failing_streak"] = health.get("FailingStreak")
        raw_history = health.get("Log")
    else:
        fixed["health_status"] = None
        fixed["health_failing_streak"] = None
        raw_history = []

    entries: list[dict[str, Any]] = []
    if isinstance(raw_history, list):
        for raw_entry in raw_history[-MAX_HEALTH_HISTORY:]:
            if not isinstance(raw_entry, dict):
                continue
            output = raw_entry.get("Output")
            entries.append(
                {
                    "start": raw_entry.get("Start"),
                    "end": raw_entry.get("End"),
                    "exit_code": raw_entry.get("ExitCode"),
                    "output": untrusted_text_summary(
                        output if isinstance(output, str) else None
                    ),
                }
            )
    history = {
        "container_id": expected_id,
        "available_entries": len(raw_history) if isinstance(raw_history, list) else 0,
        "retained_entries": len(entries),
        "entries": entries,
    }
    return redact_object(fixed), redact_object(history)


def parse_container_ids(result: CommandResult) -> list[str]:
    text = result.stdout.decode("utf-8", errors="replace")
    ids = [line.strip() for line in text.splitlines() if line.strip()]
    if len(ids) > MAX_CONTAINERS_PER_SERVICE:
        raise ValueError(f"compose ps returned more than {MAX_CONTAINERS_PER_SERVICE} containers")
    if len(set(ids)) != len(ids):
        raise ValueError("compose ps returned duplicate container ids")
    for container_id in ids:
        if not CONTAINER_ID_RE.fullmatch(container_id):
            raise ValueError("compose ps returned an unsafe container id")
    return ids


def iter_evidence_files(run_dir: Path) -> Iterable[Path]:
    for path in sorted(run_dir.rglob("*"), key=lambda item: item.relative_to(run_dir).as_posix()):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise SafetyError(f"symlink appeared in diagnostic run: {path}")
        if stat.S_ISREG(metadata.st_mode):
            yield path


def seal_run(
    run_dir: Path,
    *,
    marker_name: str,
    marker_payload: dict[str, Any],
    budget: list[int],
) -> None:
    files = [path for path in iter_evidence_files(run_dir) if path.name not in {"SHA256SUMS", "SEALED", "PARTIAL"}]
    lines: list[str] = []
    for path in files:
        relative = path.relative_to(run_dir).as_posix()
        lines.append(f"{sha256_file(path)}  {relative}\n")
    sums_data = "".join(lines).encode("utf-8")
    sums_path = run_dir / "SHA256SUMS"
    write_bytes(sums_path, sums_data, budget)

    marker_payload = dict(marker_payload)
    marker_payload.update(
        {
            "schema": SCHEMA,
            "status": marker_name,
            "sealed_at": utc_text(),
            "index_sha256": sha256_file(run_dir / "index.json"),
            "sha256sums_sha256": sha256_file(sums_path),
        }
    )
    write_json(run_dir / marker_name, marker_payload, budget)

    # A unique run is append-free after sealing.  Hashes remain the integrity
    # authority, while private read-only modes prevent accidental modification.
    for path in iter_evidence_files(run_dir):
        os.chmod(path, 0o400, follow_symlinks=False)
    service_root = run_dir / "services"
    if service_root.exists():
        for child in service_root.iterdir():
            os.chmod(child, 0o500, follow_symlinks=False)
        os.chmod(service_root, 0o500, follow_symlinks=False)
    os.chmod(run_dir, 0o500, follow_symlinks=False)


def best_effort_mark_partial(reason_code: str) -> None:
    """Seal an already-created run after an unexpected collector failure."""

    run_dir = ACTIVE_RUN_DIR
    if run_dir is None:
        return
    try:
        metadata = run_dir.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
        ):
            return
        if any((run_dir / marker).exists() for marker in ("SEALED", "PARTIAL")):
            return
        payload = json_bytes(
            {
                "schema": SCHEMA,
                "status": "PARTIAL",
                "reason_code": reason_code,
                "sealed_at": utc_text(),
                "integrity_complete": False,
            }
        )
        with open_new_file(run_dir / "PARTIAL") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        for root, directories, files in os.walk(run_dir, topdown=False, followlinks=False):
            root_path = Path(root)
            for name in files:
                path = root_path / name
                if not path.is_symlink():
                    os.chmod(path, 0o400, follow_symlinks=False)
            for name in directories:
                path = root_path / name
                if not path.is_symlink():
                    os.chmod(path, 0o500, follow_symlinks=False)
        os.chmod(run_dir, 0o500, follow_symlinks=False)
    except Exception:
        # ENOSPC and filesystem failures can also prevent the marker itself.
        # The caller still exits non-zero without exposing an unredacted trace.
        return


def verify_manifest(path: Path, expected_hash: str) -> str:
    metadata = path.stat()
    if metadata.st_size > MAX_COMPOSE_BYTES:
        raise SafetyError(f"compose manifest exceeds {MAX_COMPOSE_BYTES} bytes")
    actual = sha256_file(path)
    if actual != expected_hash.lower():
        raise SafetyError("compose manifest sha256 does not match --manifest-hash")
    return actual


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--registry-dir", required=True)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--compose-file", required=True)
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--manifest-hash", required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--failed-service", required=True)
    parser.add_argument("--failed-state", required=True)
    parser.add_argument("--service", action="append", dest="services", required=True)
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> tuple[Path, Path, Path, list[str]]:
    for label in ("registry_dir", "project_dir", "compose_file"):
        value = getattr(args, label)
        reject_unsafe_text(value, f"--{label.replace('_', '-')}", 4096)
        if not Path(value).is_absolute():
            raise SafetyError(f"--{label.replace('_', '-')} must be absolute")

    registry = require_owned_directory(Path(args.registry_dir), "release registry")
    project = require_owned_directory(Path(args.project_dir), "project directory")
    compose = require_owned_file(Path(args.compose_file), "compose manifest")

    reject_unsafe_text(args.image_ref, "--image-ref", 512)
    reject_unsafe_text(args.failed_state, "--failed-state", 128)
    if args.failed_state not in DOCKER_STATES:
        raise SafetyError("--failed-state is not an allowlisted Docker state")
    if not HASH_RE.fullmatch(args.manifest_hash):
        raise SafetyError("--manifest-hash must be exactly 64 hexadecimal characters")
    if not PHASE_RE.fullmatch(args.phase):
        raise SafetyError("--phase is not an allowlisted identifier")
    if not SERVICE_RE.fullmatch(args.failed_service):
        raise SafetyError("--failed-service is not an allowlisted Compose service name")
    if not args.services or len(args.services) > MAX_SERVICES:
        raise SafetyError(f"one to {MAX_SERVICES} --service values are required")
    if len(set(args.services)) != len(args.services):
        raise SafetyError("duplicate --service values are not allowed")
    for service in args.services:
        if not SERVICE_RE.fullmatch(service):
            raise SafetyError(f"unsafe Compose service name: {service!r}")
    if args.failed_service not in args.services:
        raise SafetyError("--failed-service must also be present in --service")

    verify_manifest(compose, args.manifest_hash)
    return registry, project, compose, list(args.services)


def collect(args: argparse.Namespace) -> tuple[int, Path, str, int]:
    global ACTIVE_RUN_DIR
    registry, project, compose, services = validate_args(args)
    diagnostics = ensure_private_child(registry, DIAGNOSTICS_DIRNAME)
    run_id, run_dir = make_run_directory(diagnostics, args.phase)
    ACTIVE_RUN_DIR = run_dir
    started_at = utc_now()
    global_deadline = time.monotonic() + TOTAL_TIMEOUT_SECONDS
    budget = [MAX_TOTAL_FILE_BYTES]
    errors: list[str] = []
    commands: list[dict[str, Any]] = []

    request = {
        "schema": SCHEMA,
        "run_id": run_id,
        "started_at": utc_text(started_at),
        "retain_until": utc_text(started_at + dt.timedelta(days=RETENTION_DAYS)),
        "registry_dir": str(registry),
        "diagnostics_dir": str(diagnostics),
        "project_dir": str(project),
        "compose_file": str(compose),
        "image_ref": args.image_ref,
        "manifest_hash": args.manifest_hash.lower(),
        "phase": args.phase,
        "failed_service": args.failed_service,
        "failed_state": args.failed_state,
        "services": services,
        "limits": {
            "total_timeout_seconds": TOTAL_TIMEOUT_SECONDS,
            "command_timeout_seconds": COMMAND_TIMEOUT_SECONDS,
            "max_command_stream_bytes": MAX_COMMAND_STREAM_BYTES,
            "log_tail_lines": LOG_TAIL_LINES,
            "max_log_stream_bytes": MAX_COMMAND_STREAM_BYTES,
            "max_health_history": MAX_HEALTH_HISTORY,
            "max_total_file_bytes": MAX_TOTAL_FILE_BYTES,
            "retention_days": RETENTION_DAYS,
        },
    }
    write_json(run_dir / "request.json", request, budget)
    services_root = run_dir / "services"
    os.mkdir(services_root, 0o700)
    require_owned_directory(services_root, "services evidence directory", private=True)

    docker_env = dict(os.environ)
    docker_env["IMAGE_REF"] = args.image_ref
    # Preserve the caller's Docker and Compose context.  Isolated rehearsals
    # may intentionally use a private daemon or project name; stripping those
    # variables would query a different runtime than release.sh just checked.

    for service_index, service in enumerate(services, start=1):
        service_dir = make_private_directory(services_root, f"{service_index:02d}-{service}")
        ps_argv = (
            DOCKER,
            "compose",
            "--project-directory",
            str(project),
            "-f",
            str(compose),
            "ps",
            "--all",
            "-q",
            service,
        )
        ps_result = run_bounded("compose_ps", ps_argv, env=docker_env, global_deadline=global_deadline)
        commands.append(command_record(ps_result, service=service))
        ps_output, ps_redaction_truncated = redacted_stream(ps_result.stdout, byte_limit=16 * 1024)
        write_bytes(service_dir / "compose-ps.txt", ps_output, budget)
        if ps_result.stderr:
            write_json(
                service_dir / "compose-ps.stderr.json",
                untrusted_text_summary(ps_result.stderr),
                budget,
            )
        if not ps_result.complete or ps_redaction_truncated:
            errors.append(f"{service}: compose ps was incomplete")
            continue
        try:
            container_ids = parse_container_ids(ps_result)
        except ValueError as exc:
            errors.append(f"{service}: {redact_text(str(exc))}")
            continue
        if not container_ids:
            errors.append(f"{service}: compose ps returned no container")
            continue

        for container_index, container_id in enumerate(container_ids, start=1):
            inspect_argv = (
                DOCKER,
                "inspect",
                "--type",
                "container",
                "--format",
                INSPECT_FORMAT,
                container_id,
            )
            inspect_result = run_bounded(
                "inspect_state", inspect_argv, env=docker_env, global_deadline=global_deadline
            )
            commands.append(command_record(inspect_result, service=service, container_id=container_id))
            prefix = f"container-{container_index:02d}"
            if inspect_result.stderr:
                write_json(
                    service_dir / f"{prefix}-inspect.stderr.json",
                    untrusted_text_summary(inspect_result.stderr),
                    budget,
                )
            if not inspect_result.complete:
                errors.append(f"{service}/{container_id}: docker inspect was incomplete")
            else:
                try:
                    payload = json.loads(inspect_result.stdout.decode("utf-8"))
                    fixed_state, health_history = fixed_state_and_health(payload, container_id)
                    write_json(service_dir / f"{prefix}-state.json", fixed_state, budget)
                    write_json(
                        service_dir / f"{prefix}-health-history.json", health_history, budget
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    errors.append(f"{service}/{container_id}: invalid inspect state: {redact_text(str(exc))}")
                    write_json(
                        service_dir / f"{prefix}-inspect-parse.json",
                        untrusted_text_summary(inspect_result.stdout),
                        budget,
                    )

            if service not in LOG_SERVICE_ALLOWLIST:
                write_json(
                    service_dir / f"{prefix}-logs.json",
                    {
                        "policy": "omitted_non_allowlisted_service",
                        "service": service,
                    },
                    budget,
                )
                continue

            logs_argv = (
                DOCKER,
                "logs",
                "--timestamps",
                "--tail",
                str(LOG_TAIL_LINES),
                container_id,
            )
            logs_result = run_bounded(
                "container_logs", logs_argv, env=docker_env, global_deadline=global_deadline
            )
            commands.append(command_record(logs_result, service=service, container_id=container_id))
            write_json(
                service_dir / f"{prefix}-logs.json",
                {
                    "policy": "redis_metadata_only",
                    "stdout": log_stream_summary(
                        logs_result.stdout,
                        truncated=logs_result.stdout_truncated,
                    ),
                    "stderr": log_stream_summary(
                        logs_result.stderr,
                        truncated=logs_result.stderr_truncated,
                    ),
                },
                budget,
            )
            if not logs_result.complete:
                errors.append(f"{service}/{container_id}: docker logs was incomplete")

    completed_at = utc_now()
    status = "SEALED" if not errors else "PARTIAL"
    file_records: list[dict[str, Any]] = []
    for path in iter_evidence_files(run_dir):
        relative = path.relative_to(run_dir).as_posix()
        file_records.append(
            {"path": relative, "bytes": path.stat().st_size, "sha256": sha256_file(path)}
        )
    index = {
        "schema": SCHEMA,
        "run_id": run_id,
        "status": status,
        "started_at": utc_text(started_at),
        "completed_at": utc_text(completed_at),
        "duration_ms": int((completed_at - started_at).total_seconds() * 1000),
        "failed_service": args.failed_service,
        "failed_state": args.failed_state,
        "services_requested": len(services),
        "commands": commands,
        "errors": errors,
        "files": file_records,
        "remaining_byte_budget": budget[0],
    }
    write_json(run_dir / "index.json", index, budget)
    seal_run(
        run_dir,
        marker_name=status,
        marker_payload={"run_id": run_id, "error_count": len(errors)},
        budget=budget,
    )
    ACTIVE_RUN_DIR = None
    return (0 if status == "SEALED" else 1), run_dir, status, len(errors)


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    try:
        args = parse_args(argv if argv is not None else sys.argv[1:])
        code, run_dir, status, error_count = collect(args)
    except SafetyError as exc:
        best_effort_mark_partial("safety_error")
        print(f"diagnostic_capture_rejected: {redact_text(str(exc))}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        best_effort_mark_partial("interrupted")
        print("diagnostic_capture_interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # Defensive: never expose an unredacted traceback.
        best_effort_mark_partial("collector_exception")
        print(f"diagnostic_capture_failed: {redact_text(type(exc).__name__)}: {redact_text(str(exc))}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "diagnostics_dir": str(run_dir),
                "status": status,
                "errors": error_count,
            },
            sort_keys=True,
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
