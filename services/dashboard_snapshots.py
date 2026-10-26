"""Durable, last-success snapshots for the public dashboard cards.

The public web process reads these files only.  The worker is responsible for
rendering the live cards and atomically publishing a complete replacement.  A
failed render or validation therefore leaves the previous snapshot untouched.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import stat
import tempfile
import threading
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote


logger = logging.getLogger(__name__)

SNAPSHOT_SCHEMA = 1
SNAPSHOT_CONTENT_TYPE = "text/html; charset=utf-8"
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_HTML_BYTES = 1_900_000

# Dict insertion order is intentional: cheap cards are built before the model
# quality calculation so a slow/failing backtest cannot delay their freshness.
CARD_PATHS: dict[str, str] = {
    "briefing": "/briefing-card?min_score=7&limit=12",
    "realtime": "/glance-sse",
    "event_radar": "/event-radar-card?limit=8",
    "model_quality": "/score-backtest?days=90&view=compact",
}
CARD_NAMES = frozenset(CARD_PATHS)

_REQUIRED_FIELDS = frozenset(
    {
        "schema",
        "card",
        "generated_at",
        "published_at",
        "release_version",
        "content_type",
        "html_sha256",
        "html",
    }
)
_OPTIONAL_FIELDS = frozenset(
    {"strategy_version", "mapping_version", "data_version"}
)
_MODEL_RENDER_LOCK = threading.Lock()
_MODEL_COORDINATION_THREAD_LOCK = threading.RLock()
_MODEL_COORDINATION_LOCK_FILE = ".model_quality.lock"
_MODEL_INVALIDATION_FILE = ".model_quality.invalid"
_MAX_CONTROL_BYTES = 4096

try:  # Linux production uses flock; Windows tests use the process-local lock.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised on native Windows
    _fcntl = None


class DashboardSnapshotError(RuntimeError):
    """Base class for snapshot publication errors."""


class DashboardSnapshotSecurityError(DashboardSnapshotError):
    """The snapshot directory or file failed its ownership/type/mode check."""


class DashboardSnapshotValidationError(DashboardSnapshotError):
    """A snapshot envelope or rendered card is incomplete or malformed."""


class DashboardSnapshotBuildError(DashboardSnapshotError):
    """A live card could not be safely rendered for publication."""


class DashboardSnapshotSupersededError(DashboardSnapshotBuildError):
    """A render was superseded by live inputs changing mid-build."""


class DashboardSnapshotBusyError(DashboardSnapshotError):
    """The snapshot coordination lock is busy; Web must fail closed fast."""


@dataclass(frozen=True)
class _RenderedCard:
    html: str
    generated_at: str
    strategy_version: str | None = None
    mapping_version: str | None = None


@dataclass
class ModelQualityFeedbackInvalidation:
    """An invalidation receipt held across one feedback database mutation."""

    token: str
    _restore_previous: bool = True

    def keep_invalidated(self) -> None:
        """Keep the new generation invalid after the database commit."""

        self._restore_previous = False

    def restore_previous_state(self) -> None:
        """Restore the exact state that preceded a no-op mutation."""

        self._restore_previous = True


def snapshots_enabled() -> bool:
    """Return whether web routes should serve dashboard snapshots.

    The switch is explicit so a rollback image never starts a new publisher by
    accident.  Malformed values fail closed instead of changing the serving
    plane.
    """

    value = os.environ.get("AI_FILTER_DASHBOARD_SNAPSHOTS", "0")
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _current_release_version() -> str:
    from services.release_info import release_version

    return _validated_optional_version("release_version", release_version())


def _parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise DashboardSnapshotValidationError(f"invalid {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DashboardSnapshotValidationError(f"invalid {field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DashboardSnapshotValidationError(f"invalid {field}")
    return parsed


def _database_path(db_path: str | os.PathLike[str] | None = None) -> Path:
    if db_path is not None:
        return Path(db_path)
    # Import lazily so pytest/maintenance callers that replace database.DB_PATH
    # after collection still select the isolated database and its sibling.
    from db import database

    return Path(database.DB_PATH)


def _snapshot_directory(
    snapshot_dir: str | os.PathLike[str] | None = None,
    *,
    db_path: str | os.PathLike[str] | None = None,
) -> Path:
    if snapshot_dir is not None:
        return Path(snapshot_dir)
    configured = str(
        os.environ.get("AI_FILTER_DASHBOARD_SNAPSHOT_DIR", "") or ""
    ).strip()
    if configured:
        configured_path = Path(configured)
        if not configured_path.is_absolute():
            raise DashboardSnapshotSecurityError(
                "AI_FILTER_DASHBOARD_SNAPSHOT_DIR must be absolute"
            )
        return configured_path
    return _database_path(db_path).parent / "dashboard_snapshots"


def _expected_uid() -> int | None:
    getter = getattr(os, "geteuid", None)
    return int(getter()) if getter is not None else None


def _validate_stat(
    st: os.stat_result,
    *,
    kind: str,
    expected_mode: int,
) -> None:
    is_expected_type = (
        stat.S_ISDIR(st.st_mode) if kind == "directory" else stat.S_ISREG(st.st_mode)
    )
    if not is_expected_type:
        raise DashboardSnapshotSecurityError(
            f"snapshot {kind} is not the required filesystem type"
        )
    # Windows does not expose enforceable Unix ownership/mode semantics.  The
    # production container is POSIX, where both checks are mandatory.
    if os.name != "nt":
        if stat.S_IMODE(st.st_mode) != expected_mode:
            raise DashboardSnapshotSecurityError(
                f"snapshot {kind} has an unsafe mode"
            )
        uid = _expected_uid()
        if uid is not None and st.st_uid != uid:
            raise DashboardSnapshotSecurityError(
                f"snapshot {kind} has an unexpected owner"
            )


def _validate_directory(directory: Path) -> None:
    try:
        st = directory.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise DashboardSnapshotSecurityError(
            "snapshot directory is unavailable"
        ) from exc
    if stat.S_ISLNK(st.st_mode):
        raise DashboardSnapshotSecurityError("snapshot directory cannot be a symlink")
    _validate_stat(st, kind="directory", expected_mode=0o700)


def _ensure_directory(directory: Path) -> None:
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise DashboardSnapshotSecurityError(
            "snapshot directory could not be created"
        ) from exc
    _validate_directory(directory)


def _snapshot_path(card: str, directory: Path) -> Path:
    if card not in CARD_NAMES:
        raise DashboardSnapshotValidationError("unknown dashboard card")
    return directory / f"{card}.json"


def _validate_file_descriptor(fd: int) -> os.stat_result:
    st = os.fstat(fd)
    _validate_stat(st, kind="file", expected_mode=0o600)
    return st


def _open_snapshot_file(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    else:
        try:
            if stat.S_ISLNK(path.lstat().st_mode):
                raise DashboardSnapshotSecurityError(
                    "snapshot file cannot be a symlink"
                )
        except FileNotFoundError:
            raise
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise DashboardSnapshotSecurityError(
            "snapshot file could not be safely opened"
        ) from exc
    try:
        _validate_file_descriptor(fd)
    except Exception:
        os.close(fd)
        raise
    return fd


def _read_bounded_file(path: Path) -> bytes:
    fd = _open_snapshot_file(path)
    try:
        st = _validate_file_descriptor(fd)
        if st.st_size <= 0 or st.st_size > MAX_SNAPSHOT_BYTES:
            raise DashboardSnapshotValidationError("snapshot file size is invalid")
        chunks: list[bytes] = []
        remaining = MAX_SNAPSHOT_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(131072, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > MAX_SNAPSHOT_BYTES:
            raise DashboardSnapshotValidationError("snapshot file is too large")
        return payload
    finally:
        os.close(fd)


def _open_coordination_lock(directory: Path) -> int:
    path = directory / _MODEL_COORDINATION_LOCK_FILE
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if stat.S_ISLNK(existing.st_mode):
            raise DashboardSnapshotSecurityError(
                "snapshot coordination lock cannot be a symlink"
            )
        _validate_stat(existing, kind="file", expected_mode=0o600)

    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise DashboardSnapshotSecurityError(
            "snapshot coordination lock is unavailable"
        ) from exc
    try:
        if existing is None and os.name != "nt":
            os.fchmod(fd, 0o600)
        _validate_file_descriptor(fd)
    except Exception:
        os.close(fd)
        raise
    return fd


@contextmanager
def _model_quality_coordination_lock(
    directory: Path, *, exclusive: bool, blocking: bool = True
):
    """Coordinate Web readers, feedback writers, and the worker publisher."""

    acquired = _MODEL_COORDINATION_THREAD_LOCK.acquire(blocking=blocking)
    if not acquired:
        raise DashboardSnapshotBusyError(
            "model-quality snapshot coordination is busy"
        )
    fd: int | None = None
    file_locked = False
    try:
        fd = _open_coordination_lock(directory)
        try:
            if _fcntl is not None:
                operation = _fcntl.LOCK_EX if exclusive else _fcntl.LOCK_SH
                if not blocking:
                    operation |= _fcntl.LOCK_NB
                try:
                    _fcntl.flock(fd, operation)
                except BlockingIOError as exc:
                    raise DashboardSnapshotBusyError(
                        "model-quality snapshot coordination is busy"
                    ) from exc
                file_locked = True
            yield
        finally:
            if _fcntl is not None and file_locked:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            os.close(fd)
            fd = None
    finally:
        if fd is not None:
            os.close(fd)
        _MODEL_COORDINATION_THREAD_LOCK.release()


def _invalidation_path(directory: Path) -> Path:
    return directory / _MODEL_INVALIDATION_FILE


def _read_invalidation_unlocked(directory: Path) -> bytes | None:
    path = _invalidation_path(directory)
    try:
        payload = _read_bounded_file(path)
    except FileNotFoundError:
        return None
    if len(payload) > _MAX_CONTROL_BYTES:
        raise DashboardSnapshotValidationError(
            "model-quality invalidation marker is too large"
        )
    return payload


def _atomic_write_control_file(directory: Path, payload: bytes) -> None:
    if not payload or len(payload) > _MAX_CONTROL_BYTES:
        raise DashboardSnapshotValidationError(
            "model-quality invalidation marker is invalid"
        )
    target = _invalidation_path(directory)
    try:
        existing = target.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if stat.S_ISLNK(existing.st_mode):
            raise DashboardSnapshotSecurityError(
                "model-quality invalidation marker cannot be a symlink"
            )
        _validate_stat(existing, kind="file", expected_mode=0o600)

    fd: int | None = None
    temp_path: Path | None = None
    try:
        fd, raw_temp_path = tempfile.mkstemp(
            prefix=".model_quality.invalid.", suffix=".tmp", dir=str(directory)
        )
        temp_path = Path(raw_temp_path)
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        _validate_file_descriptor(fd)
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError("short invalidation marker write")
            offset += written
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temp_path, target)
        temp_path = None
        _fsync_directory(directory)
    finally:
        if fd is not None:
            os.close(fd)
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def _remove_invalidation_unlocked(directory: Path) -> None:
    try:
        _invalidation_path(directory).unlink()
    except FileNotFoundError:
        return
    _fsync_directory(directory)


def _new_invalidation_marker(token: str) -> bytes:
    return json.dumps(
        {
            "schema": 1,
            "kind": "model_quality_feedback",
            "token": token,
            "created_at": _utc_now(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


@contextmanager
def model_quality_feedback_invalidation(
    *,
    snapshot_dir: str | os.PathLike[str] | None = None,
    db_path: str | os.PathLike[str] | None = None,
):
    """Invalidate before a feedback write and restore on a failed/no-op write.

    The exclusive filesystem lock is deliberately held for the caller's short
    database mutation.  A worker can therefore neither capture a pre-mutation
    contract under the new marker nor clear the marker before the commit.
    """

    directory = _snapshot_directory(snapshot_dir, db_path=db_path)
    _ensure_directory(directory)
    with _model_quality_coordination_lock(directory, exclusive=True):
        previous = _read_invalidation_unlocked(directory)
        token = secrets.token_hex(32)
        marker = _new_invalidation_marker(token)
        _atomic_write_control_file(directory, marker)
        receipt = ModelQualityFeedbackInvalidation(token=token)
        try:
            yield receipt
        finally:
            if receipt._restore_previous:
                current = _read_invalidation_unlocked(directory)
                if current != marker:
                    logger.error(
                        "Model-quality invalidation changed inside feedback guard; "
                        "retaining fail-closed state"
                    )
                elif previous is None:
                    _remove_invalidation_unlocked(directory)
                else:
                    _atomic_write_control_file(directory, previous)


def _capture_model_quality_invalidation(
    directory: Path,
) -> bytes | None:
    _ensure_directory(directory)
    with _model_quality_coordination_lock(directory, exclusive=False):
        return _read_invalidation_unlocked(directory)


def _validated_html(html: object) -> str:
    if not isinstance(html, str):
        raise DashboardSnapshotValidationError("snapshot html must be text")
    encoded = html.encode("utf-8")
    stripped = html.strip()
    if (
        not stripped
        or len(encoded) > MAX_HTML_BYTES
        or "\x00" in html
        or not stripped.startswith("<")
        or not stripped.endswith(">")
    ):
        raise DashboardSnapshotValidationError("snapshot html is incomplete")
    return html


def _validated_optional_version(field: str, value: object) -> str:
    if not isinstance(value, str):
        raise DashboardSnapshotValidationError(f"invalid {field}")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 255
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in normalized
        )
        or (field == "data_version" and not normalized.isdecimal())
    ):
        raise DashboardSnapshotValidationError(f"invalid {field}")
    return normalized


def _validate_envelope(payload: object, expected_card: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise DashboardSnapshotValidationError("snapshot envelope must be an object")
    keys = frozenset(payload)
    if not _REQUIRED_FIELDS.issubset(keys) or not keys.issubset(
        _REQUIRED_FIELDS | _OPTIONAL_FIELDS
    ):
        raise DashboardSnapshotValidationError("snapshot envelope fields are invalid")
    if (
        type(payload.get("schema")) is not int
        or payload.get("schema") != SNAPSHOT_SCHEMA
        or payload.get("card") != expected_card
    ):
        raise DashboardSnapshotValidationError("snapshot envelope identity mismatch")
    generated = _parse_timestamp(payload.get("generated_at"), "generated_at")
    published = _parse_timestamp(payload.get("published_at"), "published_at")
    if generated > published:
        raise DashboardSnapshotValidationError("snapshot timestamps are inconsistent")
    if payload.get("content_type") != SNAPSHOT_CONTENT_TYPE:
        raise DashboardSnapshotValidationError("snapshot content type is invalid")
    _validated_optional_version("release_version", payload.get("release_version"))
    html = _validated_html(payload.get("html"))
    digest = payload.get("html_sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or digest != hashlib.sha256(html.encode("utf-8")).hexdigest()
    ):
        raise DashboardSnapshotValidationError("snapshot html digest mismatch")
    for field in _OPTIONAL_FIELDS:
        if field in payload:
            _validated_optional_version(field, payload[field])
    return dict(payload)


def _normalized_expected_contract(
    expected_contract: Mapping[str, object] | tuple[object, object] | None,
) -> dict[str, str]:
    if expected_contract is None:
        return {}
    if isinstance(expected_contract, tuple) and len(expected_contract) == 2:
        raw: Mapping[str, object] = {
            "strategy_version": expected_contract[0],
            "mapping_version": expected_contract[1],
        }
    elif isinstance(expected_contract, Mapping):
        raw = expected_contract
    else:
        raise DashboardSnapshotValidationError("invalid expected contract")
    if not raw or any(field not in _OPTIONAL_FIELDS for field in raw):
        raise DashboardSnapshotValidationError("invalid expected contract")
    return {
        field: _validated_optional_version(field, value)
        for field, value in raw.items()
    }


def read_dashboard_snapshot(
    card: str,
    expected_contract: Mapping[str, object] | tuple[object, object] | None = None,
    *,
    expected_release_version: str | None = None,
    snapshot_dir: str | os.PathLike[str] | None = None,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any] | None:
    """Read and strictly validate one snapshot, returning ``None`` on failure."""

    try:
        directory = _snapshot_directory(snapshot_dir, db_path=db_path)
        _validate_directory(directory)

        def read_envelope() -> dict[str, Any] | None:
            if card == "model_quality" and _read_invalidation_unlocked(directory) is not None:
                return None
            path = _snapshot_path(card, directory)
            raw = _read_bounded_file(path)
            try:
                decoded = raw.decode("utf-8", errors="strict")
                parsed = json.loads(decoded, object_pairs_hook=_json_object)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DashboardSnapshotValidationError(
                    "snapshot JSON is invalid"
                ) from exc
            envelope = _validate_envelope(parsed, card)
            expected_release = _validated_optional_version(
                "release_version",
                expected_release_version
                if expected_release_version is not None
                else _current_release_version(),
            )
            if envelope.get("release_version") != expected_release:
                return None
            expected = _normalized_expected_contract(expected_contract)
            if any(
                envelope.get(field) != value for field, value in expected.items()
            ):
                return None
            return envelope

        if card == "model_quality":
            with _model_quality_coordination_lock(
                directory, exclusive=False, blocking=False
            ):
                return read_envelope()
        return read_envelope()
    except FileNotFoundError:
        return None
    except (DashboardSnapshotError, OSError, ValueError):
        logger.warning("Dashboard snapshot rejected for card=%s", card, exc_info=True)
        return None


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DashboardSnapshotValidationError(
                "snapshot JSON contains duplicate fields"
            )
        value[key] = item
    return value


def _fsync_directory(directory: Path) -> None:
    # Windows cannot fsync a directory handle.  Production is Linux and takes
    # this durability barrier after each atomic rename.
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(directory, flags)
    try:
        _validate_stat(os.fstat(fd), kind="directory", expected_mode=0o700)
        os.fsync(fd)
    finally:
        os.close(fd)


def write_dashboard_snapshot(
    card: str,
    html: str,
    *,
    generated_at: str | None = None,
    strategy_version: str | None = None,
    mapping_version: str | None = None,
    data_version: str | None = None,
    snapshot_dir: str | os.PathLike[str] | None = None,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Atomically publish a complete snapshot from the worker process."""

    if str(os.environ.get("AI_FILTER_ROLE", "")).strip().casefold() != "worker":
        raise DashboardSnapshotSecurityError(
            "dashboard snapshots may only be published by the worker role"
        )
    if card not in CARD_NAMES:
        raise DashboardSnapshotValidationError("unknown dashboard card")
    valid_html = _validated_html(html)
    generated_value = generated_at or _utc_now()
    generated = _parse_timestamp(generated_value, "generated_at")
    published_value = _utc_now()
    published = _parse_timestamp(published_value, "published_at")
    if generated > published:
        raise DashboardSnapshotValidationError("generated_at cannot be in the future")

    envelope: dict[str, Any] = {
        "schema": SNAPSHOT_SCHEMA,
        "card": card,
        "generated_at": generated_value,
        "published_at": published_value,
        "release_version": _current_release_version(),
        "content_type": SNAPSHOT_CONTENT_TYPE,
        "html_sha256": hashlib.sha256(valid_html.encode("utf-8")).hexdigest(),
        "html": valid_html,
    }
    optional = {
        "strategy_version": strategy_version,
        "mapping_version": mapping_version,
        "data_version": data_version,
    }
    for field, value in optional.items():
        if value is not None:
            envelope[field] = _validated_optional_version(field, value)
    _validate_envelope(envelope, card)
    encoded = json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if not encoded or len(encoded) > MAX_SNAPSHOT_BYTES:
        raise DashboardSnapshotValidationError("serialized snapshot is too large")

    directory = _snapshot_directory(snapshot_dir, db_path=db_path)
    _ensure_directory(directory)
    target = _snapshot_path(card, directory)
    try:
        existing = target.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if stat.S_ISLNK(existing.st_mode):
            raise DashboardSnapshotSecurityError("snapshot target cannot be a symlink")
        _validate_stat(existing, kind="file", expected_mode=0o600)

    temp_path: Path | None = None
    fd: int | None = None
    try:
        fd, raw_temp_path = tempfile.mkstemp(
            prefix=f".{card}.", suffix=".tmp", dir=str(directory)
        )
        temp_path = Path(raw_temp_path)
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        _validate_file_descriptor(fd)
        offset = 0
        while offset < len(encoded):
            written = os.write(fd, encoded[offset:])
            if written <= 0:
                raise OSError("short snapshot write")
            offset += written
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temp_path, target)
        temp_path = None
        _fsync_directory(directory)
    except Exception:
        if fd is not None:
            os.close(fd)
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
        raise
    return envelope


def _response_html(response: Any, card: str) -> str:
    if int(getattr(response, "status_code", 0)) != 200:
        raise DashboardSnapshotBuildError(
            f"{card} returned HTTP {getattr(response, 'status_code', 0)}"
        )
    if str(getattr(response, "mimetype", "")).casefold() != "text/html":
        raise DashboardSnapshotBuildError(f"{card} returned a non-HTML response")
    try:
        raw = bytes(response.get_data())
        html = raw.decode("utf-8", errors="strict")
    except (AttributeError, UnicodeDecodeError) as exc:
        raise DashboardSnapshotBuildError(
            f"{card} returned invalid UTF-8"
        ) from exc
    return _validated_html(html)


def _current_model_quality_contract() -> tuple[str, str]:
    from routes.dashboard_routes import _backtest_cache_contract

    contract = _backtest_cache_contract()
    if (
        not isinstance(contract, tuple)
        or len(contract) != 2
        or not all(isinstance(value, str) and value.strip() for value in contract)
    ):
        raise DashboardSnapshotBuildError("model-quality contract is unavailable")
    return contract[0].strip(), contract[1].strip()


def _render_model_quality_card(app: Any) -> _RenderedCard:
    # Capture the exact computed contract passed into the renderer.  Looking at
    # the pre-request config alone is insufficient because dynamic feedback can
    # change its strategy fingerprint while a long calculation is running.
    from routes import dashboard_routes
    from services.model_quality_overview import collect_processing_overview

    captured: dict[str, str] = {}
    with _MODEL_RENDER_LOCK:
        # A snapshot publication is a coherence boundary, not a normal public
        # cache read.  The route's five-minute in-process cache is keyed by the
        # scoring/mapping contract but not SQLite data_version; retaining it
        # here could label pre-refresh HTML with a post-refresh data_version.
        with dashboard_routes._BACKTEST_CACHE_LOCK:
            dashboard_routes._BACKTEST_CACHE.clear()
        original_renderer = dashboard_routes.render_backtest_compact_html

        def capturing_renderer(
            result: dict[str, Any], mapping: dict[str, Any]
        ) -> str:
            computed = dashboard_routes._computed_backtest_contract(result)
            if computed is None:
                raise DashboardSnapshotBuildError(
                    "computed model-quality contract is unavailable"
                )
            if not isinstance(mapping, dict):
                raise DashboardSnapshotBuildError(
                    "mapping-validation contract is unavailable"
                )
            if mapping.get("data_complete") is not True:
                raise DashboardSnapshotBuildError(
                    "mapping-validation data is incomplete"
                )
            mapping_contract = (
                str(mapping.get("strategy_version") or "").strip(),
                str(mapping.get("mapping_version") or "").strip(),
            )
            if computed != mapping_contract:
                raise DashboardSnapshotBuildError(
                    "score and mapping contracts do not match"
                )
            captured["strategy_version"], captured["mapping_version"] = computed
            # Publish anonymous counts only; do not mutate the cached research
            # result or read private article/neutral-record details here.
            view_result = dict(result)
            view_result["_processing_overview"] = collect_processing_overview(computed[0])
            view_result["_include_full_research"] = True
            return original_renderer(view_result, mapping)

        dashboard_routes.render_backtest_compact_html = capturing_renderer
        try:
            with app.test_client() as client:
                response = client.get(CARD_PATHS["model_quality"])
        finally:
            dashboard_routes.render_backtest_compact_html = original_renderer
    html = _response_html(response, "model_quality")
    if not captured:
        raise DashboardSnapshotBuildError(
            "model-quality response did not expose its computed contract"
        )
    return _RenderedCard(
        html=html,
        generated_at=_utc_now(),
        strategy_version=captured["strategy_version"],
        mapping_version=captured["mapping_version"],
    )


def _render_card(app: Any, card: str) -> _RenderedCard:
    if card == "model_quality":
        return _render_model_quality_card(app)
    with app.test_client() as client:
        response = client.get(CARD_PATHS[card])
    return _RenderedCard(
        html=_response_html(response, card),
        generated_at=_utc_now(),
    )


def _open_data_version_connection(db_path: Path) -> sqlite3.Connection:
    absolute = db_path.resolve()
    uri_path = quote(str(absolute).replace("\\", "/"), safe="/:")
    return sqlite3.connect(
        f"file:{uri_path}?mode=ro",
        uri=True,
        timeout=5.0,
        check_same_thread=False,
    )


def _pragma_data_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA data_version").fetchone()
    if row is None:
        raise DashboardSnapshotBuildError("SQLite data_version is unavailable")
    return int(row[0])


def _build_one_dashboard_snapshot(
    card: str,
    app: Any,
    *,
    snapshot_dir: str | os.PathLike[str] | None = None,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    if card not in CARD_NAMES:
        raise DashboardSnapshotValidationError("unknown dashboard card")
    if card != "model_quality":
        rendered = _render_card(app, card)
        html = rendered.html
        if card == "realtime":
            from routes.dashboard_routes import _prepend_realtime_ingestion_coverage

            html, _ = _prepend_realtime_ingestion_coverage(html)
        return write_dashboard_snapshot(
            card,
            html,
            generated_at=rendered.generated_at,
            snapshot_dir=snapshot_dir,
            db_path=db_path,
        )

    snapshot_directory = _snapshot_directory(snapshot_dir, db_path=db_path)
    invalidation_before = _capture_model_quality_invalidation(snapshot_directory)
    live_db_path = _database_path(db_path)
    try:
        connection = _open_data_version_connection(live_db_path)
    except (OSError, sqlite3.Error) as exc:
        raise DashboardSnapshotBuildError(
            "model-quality data version guard is unavailable"
        ) from exc
    with closing(connection):
        version_before = _pragma_data_version(connection)
        contract_before = _current_model_quality_contract()
        rendered = _render_card(app, card)
        contract_after = _current_model_quality_contract()
        version_after = _pragma_data_version(connection)
    computed_contract = (
        str(rendered.strategy_version or "").strip(),
        str(rendered.mapping_version or "").strip(),
    )
    if version_before != version_after or contract_before != contract_after:
        raise DashboardSnapshotSupersededError(
            "model-quality inputs changed while the snapshot was computed"
        )
    if computed_contract != contract_before:
        # A stable mismatch is not transient input churn.  Retrying it forever
        # would hide a broken cache/contract boundary instead of surfacing the
        # hard publication failure through the bounded DLQ path.
        raise DashboardSnapshotBuildError(
            "computed model-quality contract does not match the captured contract"
        )
    with _model_quality_coordination_lock(
        snapshot_directory, exclusive=True
    ):
        invalidation_after = _read_invalidation_unlocked(snapshot_directory)
        if invalidation_after != invalidation_before:
            raise DashboardSnapshotSupersededError(
                "model-quality invalidation changed while the snapshot was computed"
            )
        published = write_dashboard_snapshot(
            card,
            rendered.html,
            generated_at=rendered.generated_at,
            strategy_version=computed_contract[0],
            mapping_version=computed_contract[1],
            data_version=str(version_after),
            snapshot_dir=snapshot_dir,
            db_path=db_path,
        )
        if invalidation_before is not None:
            _remove_invalidation_unlocked(snapshot_directory)
        return published


def build_all_dashboard_snapshots(
    app: Any | None = None,
    *,
    snapshot_dir: str | os.PathLike[str] | None = None,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Build all four cards independently, retaining old files on failures."""

    return _build_dashboard_cards(
        CARD_PATHS, app, snapshot_dir=snapshot_dir, db_path=db_path
    )


def build_realtime_dashboard_snapshots(
    app: Any | None = None,
    *,
    snapshot_dir: str | os.PathLike[str] | None = None,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Publish cheap homepage cards without recalculating model quality."""
    return _build_dashboard_cards(
        (card for card in CARD_PATHS if card != "model_quality"), app,
        snapshot_dir=snapshot_dir, db_path=db_path,
    )


def _build_dashboard_cards(
    cards, app: Any | None,
    *,
    snapshot_dir: str | os.PathLike[str] | None,
    db_path: str | os.PathLike[str] | None,
) -> dict[str, Any]:

    if str(os.environ.get("AI_FILTER_ROLE", "")).strip().casefold() != "worker":
        raise DashboardSnapshotSecurityError(
            "dashboard snapshots may only be built by the worker role"
        )
    if app is None:
        from app import app as flask_app

        app = flask_app

    result: dict[str, Any] = {
        "ok": [],
        "failed": [],
        "deferred": [],
        "errors": {},
    }
    for card in cards:
        try:
            _build_one_dashboard_snapshot(
                card,
                app,
                snapshot_dir=snapshot_dir,
                db_path=db_path,
            )
        except DashboardSnapshotSupersededError as exc:
            # This is normal optimistic-concurrency control: retain last-good
            # bytes and let the durable task retry after the writers quiesce.
            logger.info("Dashboard snapshot build deferred for card=%s: %s", card, exc)
            result["deferred"].append(card)
            result["errors"][card] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            logger.exception("Dashboard snapshot build failed for card=%s", card)
            result["failed"].append(card)
            result["errors"][card] = f"{type(exc).__name__}: {exc}"
        else:
            result["ok"].append(card)
    return result
