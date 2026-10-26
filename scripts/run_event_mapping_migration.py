#!/usr/bin/env python3
"""Host coordinator for the forward-only v5 event mapping migration.

This program never builds, deploys, or rolls an image back.  It binds every
batch to the immutable release already recorded in ``current.state`` and holds
all SQLite-writer task locks for the complete bounded run.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


EXIT_BUSY = 75
EXIT_INCOMPLETE = 10
EXIT_FAILURE = 20
MAX_RESULT_BYTES = 1024 * 1024
MIGRATION_GENERATION = "event-mapping-store-v5-provider-attribution-v1"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MANIFEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_RE = re.compile(
    r"^(?:sha256:[0-9a-f]{64}|[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}|"
    r"[A-Za-z0-9._:/-]+:v?[0-9][A-Za-z0-9._-]*)$"
)
_MAPPING_GATES = (
    "old_current_mappings",
    "pending_evidence",
    "dirty_groups",
    "exhausted_groups",
)
_MAPPING_FAILURE_COUNTS = (
    "failed_evidence",
    "unreplayable_groups",
    "exhausted_groups",
)
_AUDIT_COUNTS = (
    "active_equities",
    "input_evidence",
    "old_current_mappings",
    "old_current_groups",
    "pending_evidence",
    "pending_groups",
    "dirty_groups",
    "mapping_dirty_groups",
    "exhausted_groups",
    "similarity_dirty_groups",
    "terminal_evidence",
    "current_rule_mappings",
    "merge_history_count",
    "orphan_current_groups",
    "foreign_key_violations",
)
_AUDIT_SHA256_FIELDS = (
    "security_master_fingerprint",
    "input_snapshot_sha256",
    "similarity_dirty_snapshot_sha256",
    "terminal_snapshot_sha256",
    "current_mapping_snapshot_sha256",
    "merge_history_snapshot_sha256",
)
_GRAPH_GATES = (
    "deferred_merges",
    "suppressed_merges",
    "pending_similarity_writes",
    "pending_similarity_deletes",
    "pending_similarity_migrations",
    "pending_similarity_updates",
    "deferred_similarities",
    "deferred_similarity_deletes",
    "deferred_similarity_migrations",
    "deferred_similarity_updates",
)
_MERGE_DRAIN_COUNTS = (
    "qualified_merges",
    "merged",
    "deferred_merges",
    "suppressed_merges",
)
_ZERO_WORK_COUNTS = (
    "candidate_pairs",
    "merged",
    "similarities",
    "deleted_similarities",
    "migrated_similarities",
    "reconciled_similarities",
)
_ACTIVE_CHILD: subprocess.Popen[str] | None = None
_ACTIVE_CONTAINER: tuple[str, str, int] | None = None


class CoordinatorError(RuntimeError):
    """A sanitized coordinator failure safe for the host state file."""

    def __init__(
        self,
        failure_code: str,
        exit_code: int = EXIT_FAILURE,
        *,
        evidence: dict[str, Any] | None = None,
    ):
        super().__init__(failure_code)
        self.failure_code = failure_code
        self.exit_code = exit_code
        self.evidence = evidence


class CoordinatorInterrupted(CoordinatorError):
    pass


def _log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S%z")
    print(f"[{timestamp}] {message}", flush=True)


def _safe_parent(path: Path, *, create: bool) -> Path:
    if not path.is_absolute():
        raise CoordinatorError("path_not_absolute", 78)
    parent = Path(os.path.realpath(path.parent))
    if create:
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise CoordinatorError("path_parent_create_failed", 78) from exc
    try:
        parent_stat = parent.stat()
    except OSError as exc:
        raise CoordinatorError("path_parent_unavailable", 78) from exc
    if not stat.S_ISDIR(parent_stat.st_mode):
        raise CoordinatorError("path_parent_not_directory", 78)
    if parent_stat.st_uid != os.geteuid() or stat.S_IMODE(parent_stat.st_mode) & 0o022:
        raise CoordinatorError("path_parent_unsafe", 78)
    return parent


def _validate_regular_file(path: Path, *, owner_only: bool) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as exc:
        raise CoordinatorError("file_unavailable", 78) from exc
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
        raise CoordinatorError("file_not_regular", 78)
    unsafe_mask = 0o077 if owner_only else 0o022
    if value.st_uid != os.geteuid() or stat.S_IMODE(value.st_mode) & unsafe_mask:
        raise CoordinatorError("file_permissions_unsafe", 78)
    return value


def _create_generation_file(
    path: Path,
    *,
    token: str,
    target_ref: str,
    target_digest: str,
    kind: str,
) -> dict[str, Any]:
    parent = _safe_parent(path, create=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    payload = {
        "version": 1,
        "kind": kind,
        "status": "prepared",
        "token": token,
        "generation": MIGRATION_GENERATION,
        "target_image_ref": target_ref,
        "target_image_digest": target_digest,
        "created_at": int(time.time()),
    }
    encoded = (
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise CoordinatorError(f"{kind}_already_exists", 2) from exc
    except OSError as exc:
        raise CoordinatorError(f"{kind}_create_failed", 78) from exc
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise CoordinatorError(f"{kind}_write_failed", 78)
            offset += written
        os.fsync(descriptor)
        value = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return {
        "path": str(path),
        "device": value.st_dev,
        "inode": value.st_ino,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _read_generation_file(
    path: Path,
    *,
    target_ref: str,
    target_digest: str,
    kind: str,
) -> tuple[str, dict[str, Any]]:
    value = _validate_regular_file(path, owner_only=True)
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, TypeError, ValueError) as exc:
        raise CoordinatorError(f"{kind}_invalid", 78) from exc
    token = payload.get("token") if isinstance(payload, dict) else None
    if (
        len(raw) > 4096
        or not isinstance(payload, dict)
        or payload.get("version") != 1
        or payload.get("kind") != kind
        or payload.get("status") != "prepared"
        or payload.get("generation") != MIGRATION_GENERATION
        or payload.get("target_image_ref") != target_ref
        or payload.get("target_image_digest") != target_digest
        or not isinstance(token, str)
        or re.fullmatch(r"[0-9a-f]{64}", token) is None
    ):
        raise CoordinatorError(f"{kind}_invalid", 78)
    return token, {
        "path": str(path),
        "device": value.st_dev,
        "inode": value.st_ino,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _remove_generation_file(
    path: Path,
    *,
    token: str,
    target_ref: str,
    target_digest: str,
    kind: str,
) -> None:
    observed_token, evidence = _read_generation_file(
        path,
        target_ref=target_ref,
        target_digest=target_digest,
        kind=kind,
    )
    if not secrets.compare_digest(observed_token, token):
        raise CoordinatorError(f"{kind}_changed", 78)
    before = path.lstat()
    if (before.st_dev, before.st_ino) != (evidence["device"], evidence["inode"]):
        raise CoordinatorError(f"{kind}_changed", 78)
    path.unlink()
    parent = _safe_parent(path, create=False)
    directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _read_complete_mapping_gate(
    path: Path,
    *,
    target_ref: str,
    target_digest: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    value = _validate_regular_file(path, owner_only=True)
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, TypeError, ValueError) as exc:
        raise CoordinatorError("event_mapping_gate_invalid", 78) from exc
    if (
        len(raw) > 4096
        or not isinstance(payload, dict)
        or payload.get("version") != 1
        or payload.get("kind") != "event_mapping_gate"
        or payload.get("status") != "complete"
        or payload.get("generation") != MIGRATION_GENERATION
        or payload.get("target_image_ref") != target_ref
        or payload.get("target_image_digest") != target_digest
        or not _MANIFEST_RE.fullmatch(str(payload.get("manifest_hash") or ""))
        or not str(payload.get("mapping_contract") or "").startswith("asset-map:")
        or re.fullmatch(
            r"[0-9a-f]{64}", str(payload.get("input_snapshot_sha256") or "")
        )
        is None
        or re.fullmatch(
            r"[0-9a-f]{64}", str(payload.get("verification_sha256") or "")
        )
        is None
        or isinstance(payload.get("completed_at"), bool)
        or not isinstance(payload.get("completed_at"), int)
        or payload.get("completed_at", 0) <= 0
        or "token" in payload
    ):
        raise CoordinatorError("event_mapping_gate_invalid", 78)
    return payload, {
        "path": str(path),
        "device": value.st_dev,
        "inode": value.st_ino,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "generation": MIGRATION_GENERATION,
    }


def _seal_mapping_gate(
    path: Path,
    *,
    token: str,
    target_ref: str,
    release: dict[str, str],
    mapping_contract: str,
    input_snapshot: str,
    verification_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    observed_token, prepared = _read_generation_file(
        path,
        target_ref=target_ref,
        target_digest=release["digest"],
        kind="event_mapping_gate",
    )
    if not secrets.compare_digest(observed_token, token):
        raise CoordinatorError("event_mapping_gate_changed", 78)
    payload = {
        "version": 1,
        "kind": "event_mapping_gate",
        "status": "complete",
        "generation": MIGRATION_GENERATION,
        "target_image_ref": target_ref,
        "target_image_digest": release["digest"],
        "manifest_hash": release["manifest_hash"],
        "mapping_contract": mapping_contract,
        "input_snapshot_sha256": input_snapshot,
        "verification_sha256": verification_sha256,
        "completed_at": int(time.time()),
    }
    encoded = (
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    parent = _safe_parent(path, create=False)
    descriptor, temporary = tempfile.mkstemp(prefix=".event-mapping-complete-", dir=parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (
            prepared["device"],
            prepared["inode"],
        ):
            raise CoordinatorError("event_mapping_gate_changed", 78)
        os.replace(temporary, path)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return _read_complete_mapping_gate(
        path,
        target_ref=target_ref,
        target_digest=release["digest"],
    )


def _open_lock(path: Path, *, owner_only: bool = True) -> int:
    _safe_parent(path, create=False)
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None:
        _validate_regular_file(path, owner_only=owner_only)
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise CoordinatorError("lock_open_failed", 78) from exc
    try:
        descriptor = os.fstat(fd)
        current = _validate_regular_file(path, owner_only=owner_only)
        if (descriptor.st_dev, descriptor.st_ino) != (current.st_dev, current.st_ino):
            raise CoordinatorError("lock_inode_changed", 78)
        unsafe_mask = 0o077 if owner_only else 0o022
        if descriptor.st_uid != os.geteuid() or stat.S_IMODE(descriptor.st_mode) & unsafe_mask:
            raise CoordinatorError("lock_permissions_unsafe", 78)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _take_lock(
    stack: contextlib.ExitStack,
    path: Path,
    *,
    shared: bool = False,
    timeout: float = 0,
    owner_only: bool = True,
) -> tuple[int, int]:
    fd = _open_lock(path, owner_only=owner_only)
    stack.callback(os.close, fd)
    operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
            descriptor = os.fstat(fd)
            return descriptor.st_ino, descriptor.st_dev
        except BlockingIOError as exc:
            if time.monotonic() >= deadline:
                raise CoordinatorError("migration_lock_busy", EXIT_BUSY) from exc
            time.sleep(0.1)


def _read_release(
    *,
    root: Path,
    registry: Path,
    expected_ref: str,
    docker_timeout: int,
    require_migration_gate: bool = True,
) -> dict[str, str]:
    state_path = registry / "current.state"
    _validate_regular_file(state_path, owner_only=False)
    try:
        values = state_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CoordinatorError("release_state_read_failed", 3) from exc
    if len(values) != 4 or any(not value or "\r" in value for value in values):
        raise CoordinatorError("release_state_invalid", 3)
    image_ref, digest, manifest_hash, health_services = values
    if image_ref != expected_ref:
        raise CoordinatorError("release_ref_mismatch", 3)
    if image_ref.endswith(":latest") or not _IMAGE_RE.fullmatch(image_ref):
        raise CoordinatorError("release_ref_invalid", 3)
    if not _DIGEST_RE.fullmatch(digest) or not _MANIFEST_RE.fullmatch(manifest_hash):
        raise CoordinatorError("release_identity_invalid", 3)
    manifest = registry / "manifests" / f"{manifest_hash}.compose.yml"
    _validate_regular_file(manifest, owner_only=False)
    try:
        actual_manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    except OSError as exc:
        raise CoordinatorError("release_manifest_read_failed", 3) from exc
    if actual_manifest_hash != manifest_hash:
        raise CoordinatorError("release_manifest_hash_mismatch", 3)

    try:
        inspect = subprocess.run(
            ["docker", "image", "inspect", image_ref, "--format", "{{.Id}}"],
            text=True,
            capture_output=True,
            timeout=docker_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CoordinatorError("release_image_inspect_timeout", 124) from exc
    if inspect.returncode or inspect.stdout.strip() != digest:
        raise CoordinatorError("release_image_digest_mismatch", 3)
    environment = os.environ.copy()
    environment["IMAGE_REF"] = digest
    try:
        config = subprocess.run(
            [
                "docker",
                "compose",
                "--project-directory",
                str(root),
                "-f",
                str(manifest),
                "--profile",
                "cron",
                "config",
                "--format",
                "json",
            ],
            env=environment,
            text=True,
            capture_output=True,
            timeout=docker_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CoordinatorError("release_manifest_inspect_timeout", 124) from exc
    try:
        configured_services = json.loads(config.stdout)["services"]
        configured_images = {
            name: configured_services[name]["image"]
            for name in ("ai-filter-api", "ai-filter-worker", "event-enrichment")
        }
    except (KeyError, TypeError, ValueError):
        configured_images = {}
    if config.returncode or set(configured_images.values()) != {digest}:
        raise CoordinatorError("release_manifest_service_mismatch", 3)
    expected_data = Path(os.path.realpath(root / "data"))
    try:
        data_stat = expected_data.stat()
    except OSError as exc:
        raise CoordinatorError("release_data_source_unavailable", 3) from exc
    if not stat.S_ISDIR(data_stat.st_mode):
        raise CoordinatorError("release_data_source_invalid", 3)
    for service_name in ("ai-filter-api", "ai-filter-worker", "event-enrichment"):
        configured_environment = configured_services[service_name].get("environment")
        if require_migration_gate and (
            not isinstance(configured_environment, dict)
            or str(
                configured_environment.get(
                    "AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE", ""
                )
            )
            != "1"
            or configured_environment.get(
                "AI_FILTER_MAPPING_MIGRATION_REQUIRED_GENERATION"
            )
            != MIGRATION_GENERATION
        ):
            raise CoordinatorError("release_migration_gate_mismatch", 3)
        if (
            not isinstance(configured_environment, dict)
            or configured_environment.get("AI_FILTER_DB_PATH")
            != "/app/data/cache.db"
        ):
            raise CoordinatorError("release_database_path_mismatch", 3)
        volumes = configured_services[service_name].get("volumes")
        shadow_targets = {
            str(item.get("target") or "")
            for item in volumes or []
            if isinstance(item, dict)
            and str(item.get("target") or "").startswith("/app/data/")
            and str(item.get("target") or "") != "/app/data/releases"
        }
        matches = [
            item
            for item in volumes or []
            if isinstance(item, dict) and item.get("target") == "/app/data"
        ]
        if (
            len(matches) != 1
            or matches[0].get("type") != "bind"
            or Path(os.path.realpath(str(matches[0].get("source") or "")))
            != expected_data
            or shadow_targets
        ):
            raise CoordinatorError("release_data_mount_mismatch", 3)
    return {
        "image_ref": image_ref,
        "digest": digest,
        "manifest_hash": manifest_hash,
        "manifest": str(manifest),
        "health_services": health_services,
        "data_source": str(expected_data),
        "data_device": str(data_stat.st_dev),
        "data_inode": str(data_stat.st_ino),
    }


def _current_release_ref(registry: Path) -> str:
    state_path = registry / "current.state"
    _validate_regular_file(state_path, owner_only=False)
    try:
        values = state_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CoordinatorError("release_state_read_failed", 3) from exc
    if len(values) != 4 or any(not value or "\r" in value for value in values):
        raise CoordinatorError("release_state_invalid", 3)
    return values[0]


def _inspect_image_digest(image_ref: str, *, timeout: int) -> str:
    if image_ref.endswith(":latest") or not _IMAGE_RE.fullmatch(image_ref):
        raise CoordinatorError("release_ref_invalid", 2)
    try:
        inspected = subprocess.run(
            ["docker", "image", "inspect", image_ref, "--format", "{{.Id}}"],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CoordinatorError("release_image_inspect_timeout", 124) from exc
    digest = inspected.stdout.strip()
    if inspected.returncode or not _DIGEST_RE.fullmatch(digest):
        raise CoordinatorError("release_image_unavailable", 3)
    return digest


def _compose_base(root: Path, release: dict[str, str]) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-directory",
        str(root),
        "-f",
        release["manifest"],
    ]


def _migration_container_name(root: Path) -> str:
    identity = hashlib.sha256(
        f"{Path(os.path.realpath(root))}\0{MIGRATION_GENERATION}".encode("utf-8")
    ).hexdigest()[:20]
    return f"ai-filter-event-mapping-{identity}"


def _reconcile_migration_container(
    *,
    container_name: str,
    expected_digest: str,
    timeout: int,
) -> bool:
    """Remove an exact-generation batch container and prove it is absent."""
    if re.fullmatch(r"ai-filter-event-mapping-[0-9a-f]{20}", container_name) is None:
        raise CoordinatorError("migration_container_name_invalid", 78)
    if _DIGEST_RE.fullmatch(expected_digest) is None:
        raise CoordinatorError("migration_container_digest_invalid", 78)
    command_timeout = max(1, min(timeout, 30))

    def listed_ids() -> list[str]:
        try:
            listed = subprocess.run(
                [
                    "docker",
                    "ps",
                    "--all",
                    "--no-trunc",
                    "--quiet",
                    "--filter",
                    f"name=^/{container_name}$",
                ],
                text=True,
                capture_output=True,
                timeout=command_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CoordinatorError("migration_container_inspect_timeout", 124) from exc
        identifiers = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
        if listed.returncode or len(identifiers) > 1 or any(
            re.fullmatch(r"[0-9a-f]{12,64}", identifier) is None
            for identifier in identifiers
        ):
            raise CoordinatorError("migration_container_inspect_failed")
        return identifiers

    identifiers = listed_ids()
    if not identifiers:
        return False
    identifier = identifiers[0]
    try:
        inspected = subprocess.run(
            [
                "docker",
                "inspect",
                identifier,
                "--format",
                "{{.Id}}|{{.Image}}|{{.State.Status}}",
            ],
            text=True,
            capture_output=True,
            timeout=command_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CoordinatorError("migration_container_inspect_timeout", 124) from exc
    parts = inspected.stdout.strip().split("|")
    if (
        inspected.returncode
        or len(parts) != 3
        or parts[0] != identifier
        or parts[1] != expected_digest
        or not parts[2]
    ):
        raise CoordinatorError("migration_container_identity_mismatch")
    if parts[2] not in {"created", "exited", "dead"}:
        try:
            stopped = subprocess.run(
                ["docker", "stop", "--time", "10", identifier],
                text=True,
                capture_output=True,
                timeout=command_timeout + 10,
                check=False,
            )
        except subprocess.TimeoutExpired:
            stopped = None
        if stopped is None or stopped.returncode:
            try:
                killed = subprocess.run(
                    ["docker", "kill", identifier],
                    text=True,
                    capture_output=True,
                    timeout=command_timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise CoordinatorError(
                    "migration_container_cleanup_timeout", 124
                ) from exc
            if killed.returncode:
                raise CoordinatorError("migration_container_cleanup_failed")
    try:
        removed = subprocess.run(
            ["docker", "rm", "--force", identifier],
            text=True,
            capture_output=True,
            timeout=command_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CoordinatorError("migration_container_cleanup_timeout", 124) from exc
    if removed.returncode or listed_ids():
        raise CoordinatorError("migration_container_cleanup_failed")
    return True


def _stop_application_writers(
    *,
    root: Path,
    release: dict[str, str],
    timeout: int,
) -> None:
    environment = os.environ.copy()
    environment["IMAGE_REF"] = release["digest"]
    try:
        stopped = subprocess.run(
            [
                *_compose_base(root, release),
                "stop",
                "--timeout",
                str(min(timeout, 120)),
                "ai-filter-api",
                "ai-filter-worker",
            ],
            env=environment,
            text=True,
            capture_output=True,
            timeout=timeout + 10,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CoordinatorError("application_writer_stop_timeout", 124) from exc
    if stopped.returncode:
        raise CoordinatorError("application_writer_stop_failed")
    for service in ("ai-filter-api", "ai-filter-worker"):
        try:
            container = subprocess.run(
                [*_compose_base(root, release), "ps", "--all", "-q", service],
                env=environment,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CoordinatorError("application_writer_stop_verify_timeout", 124) from exc
        container_id = container.stdout.strip()
        if container.returncode or not container_id:
            raise CoordinatorError("application_writer_stop_unverified")
        try:
            inspected = subprocess.run(
                [
                    "docker",
                    "inspect",
                    container_id,
                    "--format",
                    "{{.State.Status}}|{{.Image}}",
                ],
                env=environment,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CoordinatorError("application_writer_stop_verify_timeout", 124) from exc
        if inspected.returncode or inspected.stdout.strip() != f"exited|{release['digest']}":
            raise CoordinatorError("application_writer_stop_unverified")


def _start_application_writers(
    *,
    root: Path,
    release: dict[str, str],
    timeout: int,
) -> None:
    environment = os.environ.copy()
    environment["IMAGE_REF"] = release["digest"]
    try:
        started = subprocess.run(
            [
                *_compose_base(root, release),
                "up",
                "-d",
                "--no-deps",
                "ai-filter-api",
                "ai-filter-worker",
            ],
            env=environment,
            text=True,
            capture_output=True,
            timeout=min(timeout, 120),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CoordinatorError("application_writer_start_timeout", 124) from exc
    if started.returncode:
        raise CoordinatorError("application_writer_start_failed")

    deadline = time.monotonic() + timeout
    pending = {"ai-filter-api", "ai-filter-worker"}
    while pending and time.monotonic() < deadline:
        for service in tuple(pending):
            try:
                container = subprocess.run(
                    [*_compose_base(root, release), "ps", "-q", service],
                    env=environment,
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise CoordinatorError("application_writer_health_timeout", 124) from exc
            container_id = container.stdout.strip()
            if container.returncode or not container_id:
                continue
            try:
                health = subprocess.run(
                    [
                        "docker",
                        "inspect",
                        container_id,
                        "--format",
                        "{{.State.Health.Status}}|{{.Image}}",
                    ],
                    env=environment,
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise CoordinatorError("application_writer_health_timeout", 124) from exc
            if (
                health.returncode == 0
                and health.stdout.strip() == f"healthy|{release['digest']}"
            ):
                pending.remove(service)
        if pending:
            time.sleep(2)
    if pending:
        raise CoordinatorError("application_writer_health_failed")


def _load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    _validate_regular_file(path, owner_only=True)
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, TypeError, ValueError) as exc:
        raise CoordinatorError("migration_state_invalid", 78) from exc
    if len(raw) > MAX_RESULT_BYTES or not isinstance(value, dict):
        raise CoordinatorError("migration_state_invalid", 78)
    return value


def _aborted_state_is_reusable(state: dict[str, Any], *, root: Path) -> bool:
    if state.get("status") != "aborted":
        return False
    try:
        _validate_abort_state(state, expected_root=root)
    except CoordinatorError:
        return False
    return True


def _archive_aborted_state(path: Path, state: dict[str, Any]) -> Path:
    """Preserve an aborted attempt before starting a new generation attempt."""
    original = _validate_regular_file(path, owner_only=True)
    try:
        raw = path.read_bytes()
        if json.loads(raw) != state:
            raise CoordinatorError("migration_state_changed", 78)
    except (OSError, TypeError, ValueError) as exc:
        raise CoordinatorError("migration_state_invalid", 78) from exc
    parent = _safe_parent(path, create=False)
    archive = parent / (
        f"{path.stem}.aborted.{int(time.time())}."
        f"{hashlib.sha256(raw).hexdigest()[:12]}.{secrets.token_hex(4)}{path.suffix}"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(archive, flags, 0o600)
    except OSError as exc:
        raise CoordinatorError("migration_state_archive_failed", 78) from exc
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise CoordinatorError("migration_state_archive_failed", 78)
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    current = path.lstat()
    if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
        raise CoordinatorError("migration_state_changed", 78)
    path.unlink()
    directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return archive


def _validate_resume_state(state: dict[str, Any], *, expected_root: Path) -> bool:
    """Validate resumable state and return whether activation must be finished."""
    release = state.get("release")
    history = state.get("history")
    contract = state.get("mapping_contract")
    snapshot = state.get("input_snapshot_sha256")
    batch_count = state.get("batch_count")
    attempt_count = state.get("attempt_count", 0)
    graph_write_observed = state.get(
        "rollback_compatible_graph_write_observed", False
    )
    merge_drain_merged = state.get("merge_drain_merged", 0)
    status = state.get("status")
    phase = state.get("phase")
    point_of_no_return = state.get("point_of_no_return")
    write_attempted = state.get("write_attempted")
    last_history = (
        history[-1]
        if isinstance(history, list)
        and history
        and isinstance(history[-1], dict)
        else None
    )
    last_history_result = (
        {
            key: value
            for key, value in last_history.items()
            if key not in {"batch", "completed_at"}
        }
        if isinstance(last_history, dict)
        else None
    )
    last_result = state.get("last_result")
    verification_graph = (
        last_result.get("graph") if isinstance(last_result, dict) else None
    )
    verification_count_fields = (*_GRAPH_GATES, *_ZERO_WORK_COUNTS)
    verification_counts_valid = isinstance(verification_graph, dict) and all(
        not isinstance(verification_graph.get(key), bool)
        and isinstance(verification_graph.get(key), int)
        and verification_graph.get(key) >= 0
        for key in verification_count_fields
    )
    valid_batch_count = (
        not isinstance(batch_count, bool)
        and isinstance(batch_count, int)
        and batch_count >= 0
    )
    history_sequence_valid = (
        valid_batch_count
        and isinstance(history, list)
        and (
            (batch_count == 0 and not history and state.get("last_result") is None)
            or (
                batch_count > 0
                and bool(history)
                and len(history) <= batch_count
                and all(isinstance(item, dict) for item in history)
                and all(
                    not isinstance(item.get("batch"), bool)
                    and isinstance(item.get("batch"), int)
                    for item in history
                )
                and [item.get("batch") for item in history]
                == list(range(batch_count - len(history) + 1, batch_count + 1))
                and state.get("last_result") == last_history_result
            )
        )
    )
    last_transition = (
        (
            last_history.get("coordinator_phase"),
            last_history.get("outcome"),
            last_history.get("child_exit_code"),
        )
        if isinstance(last_history, dict)
        else None
    )
    failed_nonzero_verification = (
        phase == "verify"
        and status == "failed"
        and state.get("failure_code") == "migration_zero_work_verification_failed"
        and last_transition == ("verify", "complete", 0)
        and isinstance(last_result, dict)
        and last_result.get("phase") == "similarity"
        and last_result.get("point_of_no_return") is True
        and state.get("last_attempt") == last_result
        and verification_counts_valid
        and not any(verification_graph[key] for key in _GRAPH_GATES)
        and any(verification_graph[key] for key in _ZERO_WORK_COUNTS)
    )
    allowed_history_phases = {
        "merge_drain": {"merge_drain"},
        "audit": {"merge_drain"},
        "mapping": {"merge_drain", "audit", "mapping"},
        "similarity": {"merge_drain", "audit", "mapping", "similarity"},
        "verify": {"merge_drain", "audit", "mapping", "similarity", "verify"},
        "complete": {"merge_drain", "audit", "mapping", "similarity", "verify"},
    }.get(phase, set())
    history_phase_ranks = (
        [
            {
                "merge_drain": 0,
                "audit": 1,
                "mapping": 2,
                "similarity": 3,
                "verify": 4,
            }.get(item.get("coordinator_phase"), -1)
            for item in history
        ]
        if isinstance(history, list) and all(isinstance(item, dict) for item in history)
        else []
    )
    history_phase_order_valid = (
        isinstance(history, list)
        and len(history_phase_ranks) == len(history)
        and history_phase_ranks == sorted(history_phase_ranks)
    )
    phase_history_valid = (
        isinstance(history, list)
        and all(
            isinstance(item, dict)
            and item.get("coordinator_phase") in allowed_history_phases
            for item in history
        )
        and (
            (
                phase == "merge_drain"
                and last_transition in {None, ("merge_drain", "progress", 1)}
            )
            or (
                phase == "audit"
                and last_transition == ("merge_drain", "complete", 0)
            )
            or (
                phase == "mapping"
                and last_transition
                in {("audit", "ready", 0), ("mapping", "progress", 1)}
            )
            or (
                phase == "similarity"
                and last_transition
                in {
                    ("mapping", "mapping_complete", 0),
                    ("similarity", "progress", 1),
                }
            )
            or (
                phase == "verify"
                and (
                    last_transition
                    in {("similarity", "complete", 0), ("verify", "progress", 1)}
                    or failed_nonzero_verification
                )
            )
            or (
                phase == "complete"
                and last_transition == ("verify", "complete", 0)
            )
        )
    )
    rollback_history_safe = isinstance(history, list) and all(
        isinstance(item, dict)
        and item.get("coordinator_phase") not in {"mapping", "similarity", "verify"}
        and item.get("point_of_no_return") is False
        for item in history
    )
    mapping_prewrite = (
        phase == "mapping"
        and point_of_no_return is False
        and write_attempted is False
        and isinstance(last_history, dict)
        and last_history.get("coordinator_phase") == "audit"
        and last_history.get("outcome") == "ready"
        and last_history.get("child_exit_code") == 0
        and last_history.get("point_of_no_return") is False
        and last_history.get("batch") == batch_count
        and state.get("last_result") == last_history_result
        and state.get("last_attempt") == state.get("last_result")
        and rollback_history_safe
    )
    complete_verification_valid = (
        phase != "complete"
        or (
            not isinstance(state.get("verified_at"), bool)
            and isinstance(state.get("verified_at"), int)
            and state.get("verified_at") > 0
            and isinstance(last_result, dict)
            and last_result.get("coordinator_phase") == "verify"
            and last_result.get("phase") == "similarity"
            and last_result.get("outcome") == "complete"
            and last_result.get("child_exit_code") == 0
            and last_result.get("point_of_no_return") is True
            and verification_counts_valid
            and not any(verification_graph[key] for key in verification_count_fields)
        )
    )
    activation_recovery = (
        phase == "complete"
        and status
        in {
            "verified",
            "activation_pending",
            "restoring_writers",
            "failed",
        }
    )
    batch_recovery = (
        phase in {"merge_drain", "audit", "mapping", "similarity", "verify"}
        and status in {"running", "progress", "failed"}
    )
    if (
        state.get("version") != 1
        or state.get("generation") != MIGRATION_GENERATION
        or state.get("root") != str(Path(os.path.realpath(expected_root)))
        or state.get("migration_container_name")
        != _migration_container_name(expected_root)
        or not isinstance(state.get("source_release"), dict)
        or not (batch_recovery or activation_recovery)
        or not isinstance(release, dict)
        or release.get("image_ref") != state.get("target_image_ref")
        or not _DIGEST_RE.fullmatch(str(release.get("digest") or ""))
        or state.get("target_digest") != release.get("digest")
        or not _MANIFEST_RE.fullmatch(str(release.get("manifest_hash") or ""))
        or not isinstance(release.get("manifest"), str)
        or not str(release.get("manifest") or "").startswith("/")
        or not isinstance(release.get("health_services"), str)
        or not str(release.get("health_services") or "").strip()
        or not isinstance(release.get("data_source"), str)
        or not str(release.get("data_source") or "").startswith("/")
        or not str(release.get("data_device") or "").isdigit()
        or not str(release.get("data_inode") or "").isdigit()
        or not isinstance(history, list)
        or len(history) > 64
        or any(not isinstance(item, dict) for item in history)
        or not history_sequence_valid
        or not history_phase_order_valid
        or not phase_history_valid
        or not complete_verification_valid
        or isinstance(batch_count, bool)
        or not isinstance(batch_count, int)
        or batch_count < 0
        or isinstance(attempt_count, bool)
        or not isinstance(attempt_count, int)
        or attempt_count < batch_count
        or not isinstance(graph_write_observed, bool)
        or isinstance(merge_drain_merged, bool)
        or not isinstance(merge_drain_merged, int)
        or merge_drain_merged < 0
        or not isinstance(contract, str)
        or (contract and not contract.startswith("asset-map:"))
        or not isinstance(snapshot, str)
        or (snapshot and re.fullmatch(r"[0-9a-f]{64}", snapshot) is None)
        or not isinstance(point_of_no_return, bool)
        or not isinstance(write_attempted, bool)
        or write_attempted is not point_of_no_return
        or (point_of_no_return is False and not rollback_history_safe)
        or not isinstance(state.get("automatic_rollback_allowed"), bool)
        or state.get("automatic_rollback_allowed")
        is not (not point_of_no_return)
        or (
            phase in {"similarity", "verify", "complete"}
            and point_of_no_return is not True
        )
        or (
            phase in {"merge_drain", "audit"}
            and point_of_no_return is not False
        )
        or (
            phase == "mapping"
            and point_of_no_return is False
            and not mapping_prewrite
        )
        or (
            phase not in {"merge_drain", "audit"}
            and (not contract or not snapshot)
        )
        or (
            activation_recovery
            and (
                state.get("migration_complete") is not True
                or not isinstance(state.get("last_result"), dict)
                or re.fullmatch(
                    r"[0-9a-f]{64}",
                    str((state.get("last_result") or {}).get("result_sha256") or ""),
                )
                is None
            )
        )
        or (phase != "complete" and state.get("migration_complete") is True)
    ):
        raise CoordinatorError("migration_resume_state_invalid", 2)
    return activation_recovery


def _release_identity_valid(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("image_ref"), str)
        and _IMAGE_RE.fullmatch(value["image_ref"]) is not None
        and isinstance(value.get("digest"), str)
        and _DIGEST_RE.fullmatch(value["digest"]) is not None
        and isinstance(value.get("manifest_hash"), str)
        and _MANIFEST_RE.fullmatch(value["manifest_hash"]) is not None
        and isinstance(value.get("manifest"), str)
        and value["manifest"].startswith("/")
        and isinstance(value.get("health_services"), str)
        and bool(value["health_services"].strip())
        and isinstance(value.get("data_source"), str)
        and value["data_source"].startswith("/")
        and str(value.get("data_device") or "").isdigit()
        and str(value.get("data_inode") or "").isdigit()
    )


def _generation_evidence_valid(value: Any) -> bool:
    if value is None:
        return True
    return (
        isinstance(value, dict)
        and set(value) == {"path", "device", "inode", "sha256"}
        and isinstance(value.get("path"), str)
        and value["path"].startswith("/")
        and not isinstance(value.get("device"), bool)
        and isinstance(value.get("device"), int)
        and value["device"] >= 0
        and not isinstance(value.get("inode"), bool)
        and isinstance(value.get("inode"), int)
        and value["inode"] > 0
        and isinstance(value.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is not None
    )


def _validate_preparation_state(
    state: dict[str, Any],
    *,
    expected_root: Path,
    allowed_statuses: set[str],
) -> None:
    source = state.get("source_release")
    gate = state.get("gate")
    maintenance = state.get("maintenance")
    status = state.get("status")
    started_at = state.get("started_at")
    updated_at = state.get("updated_at")
    batch_count = state.get("batch_count")
    expected_fields = {
        "version",
        "status",
        "started_at",
        "updated_at",
        "target_image_ref",
        "target_digest",
        "generation",
        "root",
        "source_release",
        "migration_container_name",
        "gate",
        "maintenance",
        "point_of_no_return",
        "automatic_rollback_allowed",
        "write_attempted",
        "batch_count",
        "history",
    }
    if (
        set(state) != expected_fields
        or isinstance(state.get("version"), bool)
        or state.get("version") != 1
        or not isinstance(status, str)
        or status not in allowed_statuses
        or state.get("generation") != MIGRATION_GENERATION
        or state.get("root") != str(Path(os.path.realpath(expected_root)))
        or state.get("migration_container_name")
        != _migration_container_name(expected_root)
        or not isinstance(state.get("target_image_ref"), str)
        or _IMAGE_RE.fullmatch(state["target_image_ref"]) is None
        or not isinstance(state.get("target_digest"), str)
        or _DIGEST_RE.fullmatch(state["target_digest"]) is None
        or not _release_identity_valid(source)
        or not isinstance(started_at, int)
        or isinstance(started_at, bool)
        or started_at <= 0
        or not isinstance(updated_at, int)
        or isinstance(updated_at, bool)
        or updated_at < started_at
        or state.get("point_of_no_return") is not False
        or state.get("write_attempted") is not False
        or state.get("automatic_rollback_allowed") is not True
        or isinstance(batch_count, bool)
        or batch_count != 0
        or state.get("history") != []
        or not _generation_evidence_valid(gate)
        or not _generation_evidence_valid(maintenance)
        or (maintenance is not None and gate is None)
        or (status == "prepared" and (gate is None or maintenance is None))
    ):
        raise CoordinatorError("migration_preparation_state_invalid", 2)


def _validate_abort_state(state: dict[str, Any], *, expected_root: Path) -> None:
    """Fail closed unless state proves that no write-capable batch was started."""
    status = state.get("status")
    if not isinstance(status, str) or status not in {
        "preparing",
        "prepared",
        "running",
        "progress",
        "failed",
        "aborting",
        "aborted",
    }:
        raise CoordinatorError("migration_abort_state_invalid", 2)
    if (
        state.get("point_of_no_return") is not False
        or state.get("write_attempted") is not False
        or state.get("automatic_rollback_allowed") is not True
    ):
        raise CoordinatorError("migration_abort_state_invalid", 2)

    active_shape = "release" in state or "phase" in state
    if not active_shape:
        try:
            _validate_preparation_state(
                state,
                expected_root=expected_root,
                allowed_statuses={"preparing", "prepared", "aborting", "aborted"},
            )
        except CoordinatorError as exc:
            raise CoordinatorError("migration_abort_state_invalid", 2) from exc
        return

    resumable = dict(state)
    if status in {"aborting", "aborted"}:
        resumable["status"] = "failed"
    try:
        activation_recovery = _validate_resume_state(
            resumable,
            expected_root=expected_root,
        )
    except CoordinatorError as exc:
        raise CoordinatorError("migration_abort_state_invalid", 2) from exc
    if activation_recovery:
        raise CoordinatorError("migration_abort_state_invalid", 2)


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    parent = _safe_parent(path, create=True)
    if path.exists() or path.is_symlink():
        _validate_regular_file(path, owner_only=True)
    encoded = (
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_RESULT_BYTES:
        raise CoordinatorError("migration_state_too_large", 78)
    fd, temporary = tempfile.mkstemp(prefix=".event-mapping-migration-", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _finish_activation(
    *,
    state_path: Path,
    state: dict[str, Any],
    root: Path,
    release: dict[str, str],
    target_ref: str,
    gate_path: Path,
    gate_status: str,
    gate_token: str,
    complete_gate_payload: dict[str, Any] | None,
    complete_gate_evidence: dict[str, Any] | None,
    maintenance: Path,
    maintenance_token: str,
    maintenance_present: bool,
    writer_health_timeout: int,
) -> dict[str, Any]:
    """Idempotently release the transient fence and restore normal writers."""
    mapping_contract = str(state.get("mapping_contract") or "")
    input_snapshot = str(state.get("input_snapshot_sha256") or "")
    last_result = state.get("last_result")
    verification_sha256 = str(
        last_result.get("result_sha256") if isinstance(last_result, dict) else ""
    )
    if (
        not mapping_contract.startswith("asset-map:")
        or re.fullmatch(r"[0-9a-f]{64}", input_snapshot) is None
        or re.fullmatch(r"[0-9a-f]{64}", verification_sha256) is None
    ):
        raise CoordinatorError("migration_activation_state_invalid", 2)

    if gate_status == "prepared":
        if not gate_token or not maintenance_present:
            raise CoordinatorError("migration_activation_generation_invalid", 78)
        complete_gate_payload, complete_gate_evidence = _seal_mapping_gate(
            gate_path,
            token=gate_token,
            target_ref=target_ref,
            release=release,
            mapping_contract=mapping_contract,
            input_snapshot=input_snapshot,
            verification_sha256=verification_sha256,
        )
    if not isinstance(complete_gate_payload, dict) or not isinstance(
        complete_gate_evidence, dict
    ):
        raise CoordinatorError("event_mapping_gate_invalid", 78)
    expected_gate = {
        "target_image_digest": release["digest"],
        "manifest_hash": release["manifest_hash"],
        "mapping_contract": mapping_contract,
        "input_snapshot_sha256": input_snapshot,
        "verification_sha256": verification_sha256,
    }
    if any(complete_gate_payload.get(key) != value for key, value in expected_gate.items()):
        raise CoordinatorError("event_mapping_gate_release_mismatch", 78)
    recorded_gate = state.get("completion_gate")
    if recorded_gate is not None and recorded_gate != complete_gate_evidence:
        raise CoordinatorError("event_mapping_gate_evidence_changed", 78)
    state["completion_gate"] = complete_gate_evidence
    state["status"] = "restoring_writers"
    state["updated_at"] = int(time.time())
    _write_state(state_path, state)

    _start_application_writers(
        root=root,
        release=release,
        timeout=writer_health_timeout,
    )
    state["writers_stopped"] = False
    state["writers_restored_at"] = int(time.time())
    state["updated_at"] = int(time.time())
    _write_state(state_path, state)

    if maintenance_present:
        if not maintenance_token:
            raise CoordinatorError("event_mapping_maintenance_invalid", 78)
        _remove_generation_file(
            maintenance,
            token=maintenance_token,
            target_ref=target_ref,
            target_digest=release["digest"],
            kind="event_mapping_maintenance",
        )
    state["status"] = "complete"
    state["completed_at"] = int(time.time())
    state["updated_at"] = state["completed_at"]
    state.pop("failure_code", None)
    state.pop("error_type", None)
    state.pop("writer_restore_failure_code", None)
    _write_state(state_path, state)
    return state


def _integer(mapping: dict[str, Any], field: str) -> int:
    value = mapping.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CoordinatorError("migration_result_invalid")
    return value


def _parse_result(stdout: str) -> dict[str, Any]:
    if len(stdout.encode("utf-8", "replace")) > MAX_RESULT_BYTES:
        raise CoordinatorError("migration_result_too_large")
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise CoordinatorError("migration_result_invalid")
    try:
        payload = json.loads(lines[0])
    except (TypeError, ValueError) as exc:
        raise CoordinatorError("migration_result_invalid") from exc
    if not isinstance(payload, dict):
        raise CoordinatorError("migration_result_invalid")
    return payload


def _classify_result(
    *,
    phase: str,
    return_code: int,
    payload: dict[str, Any],
    expected_contract: str,
    expected_snapshot: str,
) -> str:
    if payload.get("status") != "ok" or payload.get("mapping_scope") != "all_history":
        raise CoordinatorError(str(payload.get("failure_code") or "migration_child_failed"))
    if not isinstance(payload.get("point_of_no_return"), bool):
        raise CoordinatorError("migration_result_invalid")
    if payload.get("migration_phase") != phase:
        raise CoordinatorError("migration_result_phase_mismatch")
    outcome = payload.get("migration_outcome")
    if phase == "merge_drain":
        graph = payload.get("graph")
        if (
            not isinstance(graph, dict)
            or payload.get("migration_complete") is not (outcome == "complete")
            or payload.get("point_of_no_return") is not False
            or graph.get("merges_allowed") is not True
        ):
            raise CoordinatorError("migration_result_invalid")
        counts = {key: _integer(graph, key) for key in _MERGE_DRAIN_COUNTS}
        if (
            counts["suppressed_merges"]
            or counts["merged"] > counts["qualified_merges"]
            or counts["deferred_merges"] > counts["qualified_merges"]
            or counts["merged"] + counts["deferred_merges"]
            > counts["qualified_merges"]
        ):
            raise CoordinatorError("merge_drain_result_invalid")
        if return_code == 1 and outcome == "progress":
            if not counts["qualified_merges"] or not counts["merged"]:
                raise CoordinatorError("merge_drain_result_invalid")
            return "progress"
        if return_code == 0 and outcome == "complete":
            if any(counts.values()):
                raise CoordinatorError("merge_drain_result_invalid")
            return "complete"
        raise CoordinatorError("migration_child_exit_contract_invalid")

    contract = payload.get("mapping_contract")
    snapshot = payload.get("input_snapshot_sha256")
    if not isinstance(contract, str) or not contract.startswith("asset-map:"):
        raise CoordinatorError("migration_result_invalid")
    if not isinstance(snapshot, str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot):
        raise CoordinatorError("migration_result_invalid")
    if expected_contract and contract != expected_contract:
        raise CoordinatorError("mapping_contract_changed")
    if expected_snapshot and snapshot != expected_snapshot:
        raise CoordinatorError("mapping_input_changed")
    migration = payload.get("mapping_migration")
    if not isinstance(migration, dict) or migration.get("mapping_contract") != contract:
        raise CoordinatorError("migration_result_invalid")
    for field in _AUDIT_COUNTS:
        _integer(migration, field)
    for field in _AUDIT_SHA256_FIELDS:
        value = migration.get(field)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise CoordinatorError("migration_result_invalid")
    if not isinstance(migration.get("migration_complete"), bool):
        raise CoordinatorError("migration_result_invalid")
    if _integer(migration, "active_equities") == 0:
        raise CoordinatorError("security_master_empty")
    if _integer(migration, "orphan_current_groups"):
        raise CoordinatorError("mapping_orphan_groups_present")
    if _integer(migration, "foreign_key_violations"):
        raise CoordinatorError("mapping_foreign_key_violation")
    if _integer(migration, "exhausted_groups"):
        raise CoordinatorError("mapping_exhausted_groups_present")

    if phase == "audit":
        if (
            return_code != 0
            or outcome != "ready"
            or payload.get("migration_complete") is not False
            or payload.get("point_of_no_return") is not False
        ):
            raise CoordinatorError("migration_audit_exit_contract_invalid")
        return "ready"
    if phase == "mapping":
        mappings = payload.get("mappings")
        if (
            not isinstance(mappings, dict)
            or payload.get("migration_complete") is not False
            or not isinstance(migration.get("migration_complete"), bool)
        ):
            raise CoordinatorError("migration_result_invalid")
        if any(_integer(mappings, key) for key in _MAPPING_FAILURE_COUNTS):
            raise CoordinatorError("mapping_batch_degraded")
        if return_code == 1 and outcome == "progress":
            if migration.get("migration_complete") is not False or not any(
                _integer(migration, key) for key in _MAPPING_GATES
            ):
                raise CoordinatorError("migration_result_invalid")
            return "progress"
        if return_code == 0 and outcome == "mapping_complete":
            if migration.get("migration_complete") is not True or any(
                _integer(migration, key) for key in _MAPPING_GATES
            ):
                raise CoordinatorError("migration_result_invalid")
            return "mapping_complete"
    else:
        graph = payload.get("graph")
        similarity = payload.get("similarity_replay")
        if (
            not isinstance(graph, dict)
            or not isinstance(similarity, dict)
            or not isinstance(payload.get("migration_complete"), bool)
            or migration.get("migration_complete") is not True
        ):
            raise CoordinatorError("migration_result_invalid")
        if graph.get("merges_allowed") is not False or _integer(graph, "merged"):
            raise CoordinatorError("similarity_reconciliation_merged_event")
        if migration.get("migration_complete") is not True or any(
            _integer(migration, key) for key in _MAPPING_GATES
        ):
            raise CoordinatorError("mapping_phase_incomplete")
        graph_clear = not any(_integer(graph, key) for key in _GRAPH_GATES)
        dirty_clear = _integer(similarity, "dirty_groups") == 0
        if return_code == 1 and outcome == "progress":
            if payload.get("migration_complete") is not False or (
                graph_clear and dirty_clear
            ):
                raise CoordinatorError("migration_result_invalid")
            return "progress"
        if return_code == 0 and outcome == "complete":
            if payload.get("migration_complete") is not True or not graph_clear or not dirty_clear:
                raise CoordinatorError("migration_result_invalid")
            return "complete"
    raise CoordinatorError("migration_child_exit_contract_invalid")


def _result_evidence(
    payload: dict[str, Any],
    return_code: int,
    *,
    coordinator_phase: str,
) -> dict[str, Any]:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    evidence: dict[str, Any] = {
        "child_exit_code": return_code,
        "result_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "coordinator_phase": coordinator_phase,
        "phase": payload.get("migration_phase"),
        "outcome": payload.get("migration_outcome"),
        "mapping_contract": payload.get("mapping_contract"),
        "input_snapshot_sha256": payload.get("input_snapshot_sha256"),
        "point_of_no_return": payload.get("point_of_no_return") is True,
    }
    for section_name in ("mappings", "mapping_migration", "similarity_replay", "graph"):
        section = payload.get(section_name)
        if not isinstance(section, dict):
            continue
        allowed = {
            key: value
            for key, value in section.items()
            if isinstance(value, (bool, int, float))
            or key.endswith("sha256")
            or key == "mapping_contract"
        }
        evidence[section_name] = allowed
    return evidence


def _terminate_child(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _signal_handler(signum: int, _frame: object) -> None:
    if _ACTIVE_CHILD is not None:
        _terminate_child(_ACTIVE_CHILD)
    if _ACTIVE_CONTAINER is not None:
        container_name, expected_digest, timeout = _ACTIVE_CONTAINER
        try:
            _reconcile_migration_container(
                container_name=container_name,
                expected_digest=expected_digest,
                timeout=timeout,
            )
        except CoordinatorError as exc:
            raise CoordinatorInterrupted(
                "migration_container_signal_cleanup_failed",
                128 + signum,
                evidence={"failure_code": exc.failure_code},
            ) from exc
    raise CoordinatorInterrupted(f"coordinator_signal_{signum}", 128 + signum)


def _run_batch(
    *,
    root: Path,
    release: dict[str, str],
    phase: str,
    limit: int,
    max_pairs: int,
    timeout_seconds: int,
    expected_contract: str,
    expected_snapshot: str,
    bypass_token: str,
    container_name: str,
) -> tuple[int, dict[str, Any]]:
    command = [
        "docker",
        "compose",
        "--project-directory",
        str(root),
        "-f",
        release["manifest"],
        "--profile",
        "cron",
        "run",
        "--pull",
        "never",
        "--rm",
        "--no-deps",
        "--name",
        container_name,
    ]
    if phase != "merge_drain":
        command.extend(["-e", "AI_FILTER_MAPPING_MIGRATION_BYPASS_TOKEN"])
    command.extend(
        [
            "event-enrichment",
            "python",
            "scripts/run_event_enrichment.py",
            (
                "--migration-merge-drain-batch"
                if phase == "merge_drain"
                else "--migration-audit"
                if phase == "audit"
                else "--migration-mapping-batch"
                if phase == "mapping"
                else "--migration-similarity-batch"
            ),
        ]
    )
    if phase == "mapping":
        command.extend(["--limit", str(limit)])
    elif phase in {"merge_drain", "similarity"}:
        command.extend(["--max-pairs", str(max_pairs)])
    if phase != "merge_drain" and expected_contract:
        command.extend(["--expected-mapping-contract", expected_contract])
    if phase != "merge_drain" and expected_snapshot:
        command.extend(["--expected-input-snapshot", expected_snapshot])
    environment = os.environ.copy()
    environment["IMAGE_REF"] = release["digest"]
    if phase == "merge_drain":
        environment.pop("AI_FILTER_MAPPING_MIGRATION_BYPASS_TOKEN", None)
    else:
        environment["AI_FILTER_MAPPING_MIGRATION_BYPASS_TOKEN"] = bypass_token
    _reconcile_migration_container(
        container_name=container_name,
        expected_digest=release["digest"],
        timeout=min(timeout_seconds, 30),
    )
    global _ACTIVE_CHILD, _ACTIVE_CONTAINER
    process = subprocess.Popen(
        command,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    _ACTIVE_CHILD = process
    _ACTIVE_CONTAINER = (
        container_name,
        release["digest"],
        min(timeout_seconds, 30),
    )
    try:
        stdout, _stderr = process.communicate(timeout=timeout_seconds)
        if _reconcile_migration_container(
            container_name=container_name,
            expected_digest=release["digest"],
            timeout=min(timeout_seconds, 30),
        ):
            raise CoordinatorError(
                "migration_container_survived_client",
                evidence={
                    "child_exit_code": process.returncode,
                    "phase": phase,
                    "failure_code": "migration_container_survived_client",
                },
            )
    except subprocess.TimeoutExpired as exc:
        _terminate_child(process)
        _reconcile_migration_container(
            container_name=container_name,
            expected_digest=release["digest"],
            timeout=min(timeout_seconds, 30),
        )
        raise CoordinatorError(
            "migration_child_timeout",
            124,
            evidence={
                "child_exit_code": 124,
                "phase": phase,
                "failure_code": "migration_child_timeout",
            },
        ) from exc
    finally:
        _ACTIVE_CHILD = None
        _ACTIVE_CONTAINER = None
    raw_hash = hashlib.sha256(stdout.encode("utf-8", "replace")).hexdigest()
    try:
        payload = _parse_result(stdout)
    except CoordinatorError as exc:
        raise CoordinatorError(
            exc.failure_code,
            exc.exit_code,
            evidence={
                "child_exit_code": process.returncode,
                "result_sha256": raw_hash,
                "phase": phase,
            },
        ) from exc
    return process.returncode, payload


def _is_zero_work(payload: dict[str, Any]) -> bool:
    graph = payload.get("graph")
    return isinstance(graph, dict) and not any(
        _integer(graph, key) for key in (*_GRAPH_GATES, *_ZERO_WORK_COUNTS)
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the bounded forward-only event mapping migration",
    )
    parser.add_argument("mode", choices=("prepare", "execute", "resume", "abort"))
    parser.add_argument("image_ref", help="exact immutable current production image ref")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--max-pairs", type=int, default=10000)
    parser.add_argument("--max-batches", type=int, default=16)
    parser.add_argument("--batch-timeout", type=int, default=900)
    parser.add_argument("--docker-timeout", type=int, default=30)
    parser.add_argument("--writer-health-timeout", type=int, default=180)
    args = parser.parse_args()
    if not 1 <= args.limit <= 100_000:
        parser.error("--limit must be between 1 and 100000")
    if not 1 <= args.max_pairs <= 250_000:
        parser.error("--max-pairs must be between 1 and 250000")
    if not 1 <= args.max_batches <= 64:
        parser.error("--max-batches must be between 1 and 64")
    if not 1 <= args.batch_timeout <= 3600:
        parser.error("--batch-timeout must be between 1 and 3600")
    if not 1 <= args.docker_timeout <= 300:
        parser.error("--docker-timeout must be between 1 and 300")
    if not 10 <= args.writer_health_timeout <= 600:
        parser.error("--writer-health-timeout must be between 10 and 600")
    return args


def main() -> int:
    args = _arguments()
    configured_root = Path(
        os.environ.get("AI_FILTER_ROOT", "/root/ai-rss-filter")
    )
    root = Path(os.path.realpath(configured_root))
    if configured_root != root:
        raise CoordinatorError("root_path_must_be_canonical", 78)
    registry = Path(os.environ.get("RELEASE_REGISTRY_DIR", str(root / "data/releases")))
    state_path = Path(
        os.environ.get(
            "AI_FILTER_MAPPING_MIGRATION_STATE_FILE",
            "/var/lib/ai-filter-ops-check/event-mapping-migration.json",
        )
    )
    maintenance = Path(
        os.environ.get(
            "AI_FILTER_MAINTENANCE_FILE",
            os.environ.get("HEALTH_MAINTENANCE_FILE", "/var/lib/ai-filter-health/maintenance"),
        )
    )
    gate_path = Path(
        os.environ.get(
            "AI_FILTER_MAPPING_MIGRATION_GATE_FILE",
            str(root / "data/.event-mapping-migration-required"),
        )
    )
    container_name = _migration_container_name(root)
    lock_timeout = int(
        os.environ.get("MAPPING_MIGRATION_RELEASE_LOCK_TIMEOUT_SECONDS", "300")
    )
    if lock_timeout < 0:
        raise CoordinatorError("release_lock_timeout_invalid", 2)
    if not all(
        path.is_absolute()
        for path in (root, registry, state_path, maintenance, gate_path)
    ):
        raise CoordinatorError("path_not_absolute", 78)
    operational_paths = (state_path, maintenance, gate_path)
    if any(path != Path(os.path.realpath(path)) for path in operational_paths):
        raise CoordinatorError("operational_path_must_be_canonical", 78)
    if len({str(path) for path in operational_paths}) != len(operational_paths):
        raise CoordinatorError("operational_paths_must_be_distinct", 78)

    release: dict[str, str] | None = None
    active_state: dict[str, Any] | None = None
    lock_evidence: dict[str, dict[str, int]] = {}
    writer_stop_attempted = False
    writers_restored = False
    started_at = int(time.time())
    try:
        with contextlib.ExitStack() as stack:
            for name, path, shared, timeout, owner_only in (
                ("event_enrichment", registry / "event-enrichment.lock", False, 0, True),
                ("security_master", registry / "security-master-sync.lock", False, 0, True),
                ("release", registry / "release.lock", False, lock_timeout, False),
            ):
                inode, device = _take_lock(
                    stack,
                    path,
                    shared=shared,
                    timeout=float(timeout),
                    owner_only=owner_only,
                )
                lock_evidence[name] = {"inode": inode, "device": device}

            loaded_state = _load_state(state_path)
            if args.mode == "prepare":
                if loaded_state is not None:
                    if not _aborted_state_is_reusable(loaded_state, root=root):
                        raise CoordinatorError("migration_generation_already_exists", 2)
                    if (
                        maintenance.exists()
                        or maintenance.is_symlink()
                        or gate_path.exists()
                        or gate_path.is_symlink()
                    ):
                        raise CoordinatorError("migration_generation_file_exists", 2)
                    archived = _archive_aborted_state(state_path, loaded_state)
                    _log(f"status=archived_aborted_state path={archived}")
                    loaded_state = None
                if (
                    maintenance.exists()
                    or maintenance.is_symlink()
                    or gate_path.exists()
                    or gate_path.is_symlink()
                ):
                    raise CoordinatorError("migration_generation_file_exists", 2)
                source_ref = _current_release_ref(registry)
                if source_ref == args.image_ref:
                    raise CoordinatorError("migration_prepare_requires_predeploy", 2)
                source_release = _read_release(
                    root=root,
                    registry=registry,
                    expected_ref=source_ref,
                    docker_timeout=args.docker_timeout,
                    require_migration_gate=False,
                )
                target_digest = _inspect_image_digest(
                    args.image_ref,
                    timeout=args.docker_timeout,
                )
                token = secrets.token_hex(32)
                preparing = {
                    "version": 1,
                    "status": "preparing",
                    "started_at": started_at,
                    "updated_at": started_at,
                    "target_image_ref": args.image_ref,
                    "target_digest": target_digest,
                    "generation": MIGRATION_GENERATION,
                    "root": str(root),
                    "source_release": source_release,
                    "migration_container_name": container_name,
                    "gate": None,
                    "maintenance": None,
                    "point_of_no_return": False,
                    "automatic_rollback_allowed": True,
                    "write_attempted": False,
                    "batch_count": 0,
                    "history": [],
                }
                _write_state(state_path, preparing)
                gate_evidence = _create_generation_file(
                    gate_path,
                    token=token,
                    target_ref=args.image_ref,
                    target_digest=target_digest,
                    kind="event_mapping_gate",
                )
                preparing["gate"] = gate_evidence
                preparing["updated_at"] = int(time.time())
                _write_state(state_path, preparing)
                maintenance_evidence = _create_generation_file(
                    maintenance,
                    token=token,
                    target_ref=args.image_ref,
                    target_digest=target_digest,
                    kind="event_mapping_maintenance",
                )
                preparing["maintenance"] = maintenance_evidence
                preparing["status"] = "prepared"
                preparing["updated_at"] = int(time.time())
                _write_state(state_path, preparing)
                _log(
                    f"status=prepared target={args.image_ref} digest={target_digest}"
                )
                return 0

            if args.mode == "abort":
                if (
                    not loaded_state
                    or loaded_state.get("target_image_ref") != args.image_ref
                ):
                    raise CoordinatorError("migration_abort_state_invalid", 2)
                _validate_abort_state(loaded_state, expected_root=root)
                source_release = loaded_state.get("source_release")
                if not isinstance(source_release, dict):
                    raise CoordinatorError("migration_abort_state_invalid", 2)
                current_source = _read_release(
                    root=root,
                    registry=registry,
                    expected_ref=str(source_release.get("image_ref") or ""),
                    docker_timeout=args.docker_timeout,
                    require_migration_gate=False,
                )
                if current_source != source_release:
                    raise CoordinatorError("migration_abort_source_release_changed", 2)
                target_digest = str(loaded_state.get("target_digest") or "")
                _reconcile_migration_container(
                    container_name=container_name,
                    expected_digest=target_digest,
                    timeout=args.docker_timeout,
                )
                gate_present = gate_path.exists() or gate_path.is_symlink()
                maintenance_present = maintenance.exists() or maintenance.is_symlink()
                gate_token = maintenance_token = ""
                if gate_present:
                    gate_token, gate_evidence = _read_generation_file(
                        gate_path,
                        target_ref=args.image_ref,
                        target_digest=target_digest,
                        kind="event_mapping_gate",
                    )
                    if loaded_state.get("gate") not in (None, gate_evidence) or (
                        loaded_state.get("gate") is None
                        and loaded_state.get("status") not in {"preparing", "aborting"}
                    ):
                        raise CoordinatorError("migration_abort_generation_changed", 2)
                if maintenance_present:
                    maintenance_token, maintenance_evidence = _read_generation_file(
                        maintenance,
                        target_ref=args.image_ref,
                        target_digest=target_digest,
                        kind="event_mapping_maintenance",
                    )
                    if loaded_state.get("maintenance") not in (
                        None,
                        maintenance_evidence,
                    ) or (
                        loaded_state.get("maintenance") is None
                        and loaded_state.get("status") not in {"preparing", "aborting"}
                    ):
                        raise CoordinatorError("migration_abort_generation_changed", 2)
                if loaded_state.get("status") in {
                    "prepared",
                    "running",
                    "progress",
                    "failed",
                } and not (gate_present and maintenance_present):
                    raise CoordinatorError("migration_abort_generation_missing", 2)
                if gate_token and maintenance_token and not secrets.compare_digest(
                    gate_token, maintenance_token
                ):
                    raise CoordinatorError("migration_generation_token_mismatch", 78)
                aborted = dict(loaded_state)
                aborted["status"] = "aborting"
                aborted["updated_at"] = int(time.time())
                _write_state(state_path, aborted)
                token = gate_token or maintenance_token
                if maintenance_present:
                    _remove_generation_file(
                        maintenance,
                        token=token,
                        target_ref=args.image_ref,
                        target_digest=target_digest,
                        kind="event_mapping_maintenance",
                    )
                if gate_present:
                    _remove_generation_file(
                        gate_path,
                        token=token,
                        target_ref=args.image_ref,
                        target_digest=target_digest,
                        kind="event_mapping_gate",
                    )
                aborted["status"] = "aborted"
                aborted["updated_at"] = int(time.time())
                aborted["automatic_rollback_allowed"] = True
                _write_state(state_path, aborted)
                _log("status=aborted")
                return 0

            if args.mode == "execute":
                if not loaded_state:
                    raise CoordinatorError("migration_execute_state_invalid", 2)
                try:
                    _validate_preparation_state(
                        loaded_state,
                        expected_root=root,
                        allowed_statuses={"prepared"},
                    )
                except CoordinatorError as exc:
                    raise CoordinatorError(
                        "migration_execute_state_invalid", 2
                    ) from exc

            release = _read_release(
                root=root,
                registry=registry,
                expected_ref=args.image_ref,
                docker_timeout=args.docker_timeout,
            )
            activation_recovery = False
            if args.mode == "resume":
                if not loaded_state:
                    raise CoordinatorError("migration_resume_state_invalid", 2)
                activation_recovery = _validate_resume_state(
                    loaded_state,
                    expected_root=root,
                )

            gate_status = "prepared"
            complete_gate_payload: dict[str, Any] | None = None
            complete_gate_evidence: dict[str, Any] | None = None
            try:
                gate_token, gate_evidence = _read_generation_file(
                    gate_path,
                    target_ref=args.image_ref,
                    target_digest=release["digest"],
                    kind="event_mapping_gate",
                )
            except CoordinatorError as prepared_gate_error:
                if not activation_recovery:
                    raise
                try:
                    complete_gate_payload, complete_gate_evidence = (
                        _read_complete_mapping_gate(
                            gate_path,
                            target_ref=args.image_ref,
                            target_digest=release["digest"],
                        )
                    )
                except CoordinatorError:
                    raise prepared_gate_error
                gate_status = "complete"
                gate_token = ""
                gate_evidence = complete_gate_evidence

            maintenance_present = maintenance.exists() or maintenance.is_symlink()
            if maintenance_present:
                maintenance_token, maintenance_evidence = _read_generation_file(
                    maintenance,
                    target_ref=args.image_ref,
                    target_digest=release["digest"],
                    kind="event_mapping_maintenance",
                )
            else:
                maintenance_token = ""
                maintenance_evidence = None

            if args.mode == "execute":
                if (
                    not loaded_state
                    or loaded_state.get("status") != "prepared"
                    or loaded_state.get("target_image_ref") != args.image_ref
                    or loaded_state.get("target_digest") != release["digest"]
                    or loaded_state.get("generation") != MIGRATION_GENERATION
                    or loaded_state.get("root")
                    != str(Path(os.path.realpath(root)))
                    or loaded_state.get("migration_container_name") != container_name
                    or loaded_state.get("gate") != gate_evidence
                    or loaded_state.get("maintenance") != maintenance_evidence
                    or gate_status != "prepared"
                    or not maintenance_present
                    or not secrets.compare_digest(gate_token, maintenance_token)
                ):
                    raise CoordinatorError("migration_execute_state_invalid", 2)
                active_state = {
                    **loaded_state,
                    "status": "running",
                    "updated_at": started_at,
                    "release": release,
                    "locks": lock_evidence,
                    "phase": "merge_drain",
                    "batch_count": 0,
                    "rollback_compatible_graph_write_observed": False,
                    "merge_drain_merged": 0,
                    "mapping_contract": "",
                    "input_snapshot_sha256": "",
                    "point_of_no_return": False,
                    "automatic_rollback_allowed": True,
                    "history": [],
                }
            else:
                assert loaded_state is not None
                prior_release = loaded_state.get("release")
                release_keys = (
                    "image_ref",
                    "digest",
                    "manifest_hash",
                    "manifest",
                    "health_services",
                    "data_source",
                    "data_device",
                    "data_inode",
                )
                if (
                    not isinstance(prior_release, dict)
                    or any(
                        prior_release.get(key) != release[key]
                        for key in release_keys
                    )
                ):
                    raise CoordinatorError("migration_resume_release_changed", 2)
                if gate_status == "prepared":
                    if (
                        loaded_state.get("gate") != gate_evidence
                        or loaded_state.get("maintenance") != maintenance_evidence
                        or not maintenance_present
                        or not secrets.compare_digest(gate_token, maintenance_token)
                    ):
                        raise CoordinatorError("migration_resume_generation_changed", 2)
                if maintenance_present and (
                    loaded_state.get("maintenance") != maintenance_evidence
                ):
                    raise CoordinatorError("migration_resume_generation_changed", 2)
                if gate_status == "complete" and not activation_recovery:
                    raise CoordinatorError("migration_resume_generation_changed", 2)
                if gate_status == "complete" and loaded_state.get(
                    "completion_gate"
                ) not in (None, complete_gate_evidence):
                    raise CoordinatorError("migration_resume_generation_changed", 2)
                if not activation_recovery and not maintenance_present:
                    raise CoordinatorError("migration_resume_generation_changed", 2)
                active_state = dict(loaded_state)
                active_state["updated_at"] = started_at
                active_state["locks"] = lock_evidence
                active_state.pop("failure_code", None)
                active_state.pop("error_type", None)
                active_state.pop("writer_restore_failure_code", None)

                if activation_recovery:
                    _reconcile_migration_container(
                        container_name=container_name,
                        expected_digest=release["digest"],
                        timeout=args.docker_timeout,
                    )
                    writer_stop_attempted = bool(
                        active_state.get("writers_stopped")
                    )
                    active_state = _finish_activation(
                        state_path=state_path,
                        state=active_state,
                        root=root,
                        release=release,
                        target_ref=args.image_ref,
                        gate_path=gate_path,
                        gate_status=gate_status,
                        gate_token=gate_token,
                        complete_gate_payload=complete_gate_payload,
                        complete_gate_evidence=complete_gate_evidence,
                        maintenance=maintenance,
                        maintenance_token=maintenance_token,
                        maintenance_present=maintenance_present,
                        writer_health_timeout=args.writer_health_timeout,
                    )
                    writers_restored = True
                    _log(
                        f"status=complete batches={active_state['batch_count']} "
                        f"digest={release['digest']} "
                        f"contract={active_state['mapping_contract']}"
                    )
                    return 0
                active_state["status"] = "running"
            _write_state(state_path, active_state)

            _reconcile_migration_container(
                container_name=container_name,
                expected_digest=release["digest"],
                timeout=args.docker_timeout,
            )
            writer_stop_attempted = True
            _stop_application_writers(
                root=root,
                release=release,
                timeout=args.writer_health_timeout,
            )
            active_state["writers_stopped"] = True
            active_state["updated_at"] = int(time.time())
            _write_state(state_path, active_state)
            inode, device = _take_lock(
                stack,
                root / "data/.refresh-execution.lock",
                shared=False,
                timeout=0,
                owner_only=False,
            )
            lock_evidence["refresh_execution"] = {
                "inode": inode,
                "device": device,
            }
            active_state["locks"] = lock_evidence
            _write_state(state_path, active_state)

            phase = str(active_state.get("phase") or "merge_drain")
            expected_contract = str(active_state.get("mapping_contract") or "")
            expected_snapshot = str(
                active_state.get("input_snapshot_sha256") or ""
            )
            for _run_index in range(args.max_batches):
                if phase == "mapping":
                    # A killed container can commit a canonical group before
                    # the host receives JSON. Conservatively cross the rollback
                    # boundary before the first write-capable child starts.
                    active_state["write_attempted"] = True
                    active_state["point_of_no_return"] = True
                    active_state["automatic_rollback_allowed"] = False
                    active_state["updated_at"] = int(time.time())
                    _write_state(state_path, active_state)
                child_exit, payload = _run_batch(
                    root=root,
                    release=release,
                    phase="similarity" if phase == "verify" else phase,
                    limit=args.limit,
                    max_pairs=args.max_pairs,
                    timeout_seconds=args.batch_timeout,
                    expected_contract=expected_contract,
                    expected_snapshot=expected_snapshot,
                    bypass_token=gate_token,
                    container_name=container_name,
                )
                evidence = _result_evidence(
                    payload,
                    child_exit,
                    coordinator_phase=phase,
                )
                active_state["last_attempt"] = evidence
                active_state["attempt_count"] = int(
                    active_state.get("attempt_count") or 0
                ) + 1
                _write_state(state_path, active_state)
                classified = _classify_result(
                    phase="similarity" if phase == "verify" else phase,
                    return_code=child_exit,
                    payload=payload,
                    expected_contract=expected_contract,
                    expected_snapshot=expected_snapshot,
                )
                if phase != "merge_drain":
                    expected_contract = str(evidence["mapping_contract"])
                    expected_snapshot = str(evidence["input_snapshot_sha256"])
                    active_state["mapping_contract"] = expected_contract
                    active_state["input_snapshot_sha256"] = expected_snapshot
                else:
                    graph = payload["graph"]
                    merged_count = _integer(graph, "merged")
                    active_state["rollback_compatible_graph_write_observed"] = bool(
                        active_state.get(
                            "rollback_compatible_graph_write_observed"
                        )
                        or merged_count
                        or _integer(graph, "candidate_pairs")
                    )
                    active_state["merge_drain_merged"] = int(
                        active_state.get("merge_drain_merged") or 0
                    ) + merged_count
                active_state["point_of_no_return"] = bool(
                    active_state.get("point_of_no_return")
                    or evidence["point_of_no_return"]
                )
                active_state["automatic_rollback_allowed"] = not active_state[
                    "point_of_no_return"
                ]
                active_state["batch_count"] = int(
                    active_state.get("batch_count") or 0
                ) + 1
                active_state["updated_at"] = int(time.time())
                history = list(active_state.get("history") or [])
                history.append(
                    {
                        "batch": active_state["batch_count"],
                        "completed_at": active_state["updated_at"],
                        **evidence,
                    }
                )
                active_state["history"] = history[-64:]
                active_state["last_result"] = evidence

                if phase == "merge_drain" and classified == "complete":
                    phase = "audit"
                elif phase == "audit" and classified == "ready":
                    phase = "mapping"
                elif phase == "mapping" and classified == "mapping_complete":
                    phase = "similarity"
                elif phase == "similarity" and classified == "complete":
                    phase = "verify"
                elif phase == "verify" and classified == "complete":
                    if not _is_zero_work(payload):
                        raise CoordinatorError("migration_zero_work_verification_failed")
                    active_state["status"] = "verified"
                    active_state["phase"] = "complete"
                    active_state["verified_at"] = int(time.time())
                    active_state["migration_complete"] = True
                    _write_state(state_path, active_state)
                    active_state = _finish_activation(
                        state_path=state_path,
                        state=active_state,
                        root=root,
                        release=release,
                        target_ref=args.image_ref,
                        gate_path=gate_path,
                        gate_status="prepared",
                        gate_token=gate_token,
                        complete_gate_payload=None,
                        complete_gate_evidence=None,
                        maintenance=maintenance,
                        maintenance_token=maintenance_token,
                        maintenance_present=True,
                        writer_health_timeout=args.writer_health_timeout,
                    )
                    writers_restored = True
                    _log(
                        f"status=complete batches={active_state['batch_count']} "
                        f"digest={release['digest']} contract={expected_contract}"
                    )
                    return 0
                active_state["phase"] = phase
                active_state["status"] = "running"
                _write_state(state_path, active_state)

            active_state["status"] = "progress"
            active_state["phase"] = phase
            active_state["updated_at"] = int(time.time())
            active_state["failure_code"] = "migration_batch_budget_exhausted"
            _write_state(state_path, active_state)
            _log(
                "status=progress reason=batch_budget "
                f"batches={active_state['batch_count']} "
                f"phase={phase}"
            )
            return EXIT_INCOMPLETE
    except CoordinatorError as exc:
        if active_state is not None and release is not None:
            if exc.evidence:
                active_state["last_attempt"] = exc.evidence
            if (
                writer_stop_attempted
                and not writers_restored
                and active_state.get("phase") == "complete"
                and active_state.get("migration_complete") is True
            ):
                try:
                    _start_application_writers(
                        root=root,
                        release=release,
                        timeout=args.writer_health_timeout,
                    )
                    writers_restored = True
                    active_state["writers_stopped"] = False
                except CoordinatorError as restore_exc:
                    active_state["writer_restore_failure_code"] = (
                        restore_exc.failure_code
                    )
            active_state["status"] = (
                "activation_pending"
                if active_state.get("phase") == "complete"
                and active_state.get("migration_complete") is True
                else "failed"
            )
            active_state["updated_at"] = int(time.time())
            active_state["failure_code"] = exc.failure_code
            active_state["automatic_rollback_allowed"] = not bool(
                active_state.get("point_of_no_return")
            )
            try:
                _write_state(state_path, active_state)
            except CoordinatorError:
                pass
        _log(f"status=failed failure_code={exc.failure_code}")
        return exc.exit_code
    except Exception as exc:
        if active_state is not None and release is not None:
            if (
                writer_stop_attempted
                and not writers_restored
                and active_state.get("phase") == "complete"
                and active_state.get("migration_complete") is True
            ):
                try:
                    _start_application_writers(
                        root=root,
                        release=release,
                        timeout=args.writer_health_timeout,
                    )
                    active_state["writers_stopped"] = False
                except CoordinatorError as restore_exc:
                    active_state["writer_restore_failure_code"] = (
                        restore_exc.failure_code
                    )
            active_state["status"] = (
                "activation_pending"
                if active_state.get("phase") == "complete"
                and active_state.get("migration_complete") is True
                else "failed"
            )
            active_state["updated_at"] = int(time.time())
            active_state["failure_code"] = "migration_coordinator_failed"
            active_state["error_type"] = type(exc).__name__
            active_state["automatic_rollback_allowed"] = not bool(
                active_state.get("point_of_no_return")
            )
            try:
                _write_state(state_path, active_state)
            except CoordinatorError:
                pass
        _log(
            "status=failed failure_code=migration_coordinator_failed "
            f"error_type={type(exc).__name__}"
        )
        return EXIT_FAILURE


if __name__ == "__main__":
    for handled_signal in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(handled_signal, _signal_handler)
    try:
        entrypoint_exit = main()
    except CoordinatorError as exc:
        _log(f"status=failed failure_code={exc.failure_code}")
        entrypoint_exit = exc.exit_code
    except ValueError:
        _log("status=failed failure_code=configuration_invalid")
        entrypoint_exit = 2
    except Exception as exc:
        _log(
            "status=failed failure_code=migration_coordinator_failed "
            f"error_type={type(exc).__name__}"
        )
        entrypoint_exit = EXIT_FAILURE
    raise SystemExit(entrypoint_exit)
