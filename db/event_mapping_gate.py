"""Persistent authorization fence for event-mapping mutations."""

from __future__ import annotations

import hmac
import json
import os
import re
import stat


class EventMappingMigrationGateError(RuntimeError):
    """An event-mapping write is not authorized for the active generation."""


def event_mapping_migration_gate_path(db_path: str | None = None) -> str:
    configured = os.environ.get("AI_FILTER_MAPPING_MIGRATION_GATE_FILE", "").strip()
    if configured:
        return configured
    resolved_db_path = str(db_path or os.environ.get("AI_FILTER_DB_PATH") or "")
    if not resolved_db_path:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        resolved_db_path = os.path.join(project_root, "data", "cache.db")
    return os.path.join(
        os.path.dirname(os.path.abspath(resolved_db_path)),
        ".event-mapping-migration-required",
    )


def require_event_mapping_write_authorized(
    mapping_version: str,
    *,
    db_path: str | None = None,
    gate_path: str | None = None,
) -> None:
    """Fail closed at the shared mutation boundary during a migration."""
    del mapping_version  # The complete generation deliberately permits later contracts.
    path = gate_path or event_mapping_migration_gate_path(db_path)
    enforce = os.environ.get("AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE", "0")
    required_generation = os.environ.get(
        "AI_FILTER_MAPPING_MIGRATION_REQUIRED_GENERATION", ""
    ).strip()
    if enforce not in {"0", "1"}:
        raise EventMappingMigrationGateError(
            "event mapping migration enforcement configuration is invalid"
        )
    try:
        value = os.lstat(path)
    except FileNotFoundError:
        if enforce == "0":
            return
        raise EventMappingMigrationGateError(
            "event mapping migration has not been activated"
        )
    except OSError as exc:
        raise EventMappingMigrationGateError(
            "event mapping migration gate is unreadable"
        ) from exc
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
        raise EventMappingMigrationGateError(
            "event mapping migration gate is unsafe"
        )
    if value.st_uid != os.geteuid() or stat.S_IMODE(value.st_mode) & 0o077:
        raise EventMappingMigrationGateError(
            "event mapping migration gate permissions are unsafe"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (value.st_dev, value.st_ino):
                raise EventMappingMigrationGateError(
                    "event mapping migration gate changed while opening"
                )
            raw = os.read(descriptor, 4097)
        finally:
            os.close(descriptor)
    except EventMappingMigrationGateError:
        raise
    except OSError as exc:
        raise EventMappingMigrationGateError(
            "event mapping migration gate cannot be opened"
        ) from exc
    if len(raw) > 4096:
        raise EventMappingMigrationGateError(
            "event mapping migration gate is malformed"
        )
    try:
        payload = json.loads(raw)
        gate_status = payload["status"]
    except (KeyError, TypeError, ValueError) as exc:
        raise EventMappingMigrationGateError(
            "event mapping migration gate is malformed"
        ) from exc
    if (
        isinstance(payload.get("version"), bool)
        or payload.get("version") != 1
        or payload.get("kind") != "event_mapping_gate"
        or not isinstance(payload.get("generation"), str)
        or (
            required_generation
            and not hmac.compare_digest(
                str(payload.get("generation")), required_generation
            )
        )
        or not isinstance(payload.get("target_image_ref"), str)
        or not re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            str(payload.get("target_image_digest") or ""),
        )
    ):
        raise EventMappingMigrationGateError(
            "event mapping migration gate is malformed"
        )
    if gate_status == "complete":
        if (
            not re.fullmatch(
                r"[0-9a-f]{64}", str(payload.get("manifest_hash") or "")
            )
            or not str(payload.get("mapping_contract") or "").startswith(
                "asset-map:"
            )
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                str(payload.get("input_snapshot_sha256") or ""),
            )
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                str(payload.get("verification_sha256") or ""),
            )
            or isinstance(payload.get("completed_at"), bool)
            or not isinstance(payload.get("completed_at"), int)
            or payload.get("completed_at", 0) <= 0
            or "token" in payload
        ):
            raise EventMappingMigrationGateError(
                "event mapping migration gate is malformed"
            )
        return
    token = payload.get("token")
    bypass = os.environ.get("AI_FILTER_MAPPING_MIGRATION_BYPASS_TOKEN", "")
    if (
        gate_status != "prepared"
        or isinstance(payload.get("created_at"), bool)
        or not isinstance(payload.get("created_at"), int)
        or payload.get("created_at", 0) <= 0
        or not isinstance(token, str)
        or re.fullmatch(r"[0-9a-f]{64}", token) is None
        or not hmac.compare_digest(token, bypass)
    ):
        raise EventMappingMigrationGateError(
            "event mapping writes are paused for migration"
        )


def require_event_mapping_connection_write_authorized(
    conn: object,
    mapping_version: str,
) -> None:
    """Bind authorization to the actual SQLite main database being mutated."""
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
        main_rows = [row for row in rows if str(row[1]) == "main"]
    except Exception as exc:
        raise EventMappingMigrationGateError(
            "event mapping database identity is unavailable"
        ) from exc
    if len(main_rows) != 1:
        raise EventMappingMigrationGateError(
            "event mapping database identity is unavailable"
        )
    database_path = str(main_rows[0][2] or "")
    if not database_path:
        if (
            os.environ.get("AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE", "0") == "1"
            or os.environ.get("AI_FILTER_MAPPING_MIGRATION_GATE_FILE", "").strip()
        ):
            raise EventMappingMigrationGateError(
                "an in-memory mapping database cannot be bound to a persistent gate"
            )
        return
    connection_gate_path = os.path.join(
        os.path.dirname(os.path.abspath(database_path)),
        ".event-mapping-migration-required",
    )
    require_event_mapping_write_authorized(
        mapping_version,
        db_path=database_path,
        gate_path=connection_gate_path,
    )


__all__ = [
    "EventMappingMigrationGateError",
    "event_mapping_migration_gate_path",
    "require_event_mapping_connection_write_authorized",
    "require_event_mapping_write_authorized",
]
