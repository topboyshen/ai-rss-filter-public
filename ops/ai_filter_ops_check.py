#!/usr/bin/env python3
"""Read-only production checks with bounded, secret-safe alerting.

The checker never restarts, reloads, deploys, prunes, or repairs anything. It
only performs loopback HTTP requests, reads backup/release/market-close result
state, and asks systemd/Docker for status. Normal runs persist alert state and
a bounded, allowlisted dashboard snapshot; dry runs do not write either.
"""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo


_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SAFE_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_RELEASE_APP_SERVICES = frozenset(
    {"ai-filter-api", "ai-filter-worker", "ai-filter-sse"}
)
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
_BACKUP_DB_MEMBER_RE = re.compile(
    r"^[^/]+/ai-rss-filter/sqlite/data/cache\.db$"
)
_RESTORE_REQUIRED_TABLES = {
    "event_mapping_replay_ledger",
    "event_mapping_replay_dirty_roots",
    "event_similarity_replay_dirty_roots",
    "feedback_example_snapshots",
    "scoring_contract_snapshots",
}
# JD operator decision (2026-09-10): retire scheduled backup and restore drill.
# This is a versioned policy, not an environment toggle or evidence of recovery.
# Re-enabling backups requires a separately reviewed policy/schedule change.
_BACKUP_POLICY = "NO_BACKUP"
_BACKUP_POLICY_EPOCH = "no_backup_20260910_v1"
_POLICY_DISABLED_CHECKS = {
    "check_backup": "backup",
    "check_restore_drill": "restore_drill",
    "check_offsite": "offsite_upload",
}
_MARKET_TZ = ZoneInfo("Asia/Shanghai")
_MARKET_REQUIRED_SYMBOLS = {"sh000001", "sh000300", "sz399001", "sz399006"}
_CHINA_HOLIDAYS_BY_YEAR = {
    2026: {
        "2026-01-01", "2026-01-02", "2026-01-03",
        "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
        "2026-02-23", "2026-04-04", "2026-04-05", "2026-04-06",
        "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
        "2026-06-19", "2026-06-20", "2026-06-21",
        "2026-09-25", "2026-09-26", "2026-09-27",
        "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05",
        "2026-10-06", "2026-10-07",
    }
}


@dataclasses.dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    warning: bool = False
    skipped: bool = False

    def public(self) -> dict[str, object]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "warning": self.warning,
            "disposition": "SKIPPED_BY_POLICY" if self.skipped else ("WARN" if self.warning and self.ok else "PASS" if self.ok else "FAIL"),
        }


@dataclasses.dataclass(frozen=True)
class Settings:
    root: Path
    backup_dir: Path
    backup_max_age_seconds: int
    state_file: Path
    cooldown_seconds: int
    metrics_url: str
    api_key_file: Path | None
    timer_unit: str
    release_registry: Path
    compose_project: str
    required_services: tuple[str, ...]
    app_services: tuple[str, ...]
    offsite_hook: Path | None
    offsite_state_file: Path
    require_offsite: bool
    telegram_token: str
    telegram_chat_id: str
    command_timeout_seconds: int
    backup_verification_state_file: Path | None = None
    backup_reverify_seconds: int = 86400
    run_budget_seconds: int = 55
    run_deadline_monotonic: float | None = None
    persist_verification_state: bool = False
    restore_drill_state_file: Path | None = None
    restore_drill_max_age_seconds: int = 691200
    restore_drill_running_grace_seconds: int = 3000
    enrichment_state_file: Path | None = None
    enrichment_max_age_seconds: int = 4500
    enrichment_running_grace_seconds: int = 900
    database_path: Path | None = None
    market_service_unit: str = ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _parse_bool(value: str, name: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be 0 or 1")


def _positive_int(value: str, name: str, *, allow_zero: bool = False) -> int:
    if not re.fullmatch(r"[0-9]+", str(value or "")):
        raise ValueError(f"{name} must be an integer")
    parsed = int(value)
    if parsed < (0 if allow_zero else 1):
        raise ValueError(f"{name} is out of range")
    return parsed


def _bounded_int(value: str, name: str, *, minimum: int, maximum: int) -> int:
    parsed = _positive_int(value, name)
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{name} is out of range")
    return parsed


def _csv_names(value: str, name: str) -> tuple[str, ...]:
    items = tuple(part.strip() for part in value.split(",") if part.strip())
    if not items or any(not _SAFE_NAME_RE.fullmatch(item) for item in items):
        raise ValueError(f"{name} contains an invalid service name")
    return items


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse simple KEY=VALUE files without executing shell syntax."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    except OSError as exc:
        raise ValueError(f"cannot read configuration file: {path}") from exc
    for line_number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid configuration line {line_number}: {path}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"invalid configuration key at line {line_number}: {path}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def _is_private_file(path: Path) -> bool:
    try:
        stat_result = path.stat()
        return stat_result.st_uid == os.geteuid() and not (stat_result.st_mode & 0o077)
    except OSError:
        return False


def _load_configuration_files(paths: Iterable[Path]) -> None:
    for path in paths:
        if path.exists() and not _is_private_file(path):
            raise ValueError(f"configuration file ownership or mode is unsafe: {path}")
        for key, value in _parse_env_file(path).items():
            os.environ.setdefault(key, value)


def _optional_path(value: str) -> Path | None:
    stripped = str(value or "").strip()
    return Path(stripped) if stripped else None


def _load_telegram_credentials(root: Path) -> tuple[str, str]:
    ops_token = os.environ.get("OPS_TELEGRAM_BOT_TOKEN", "").strip()
    ops_chat = os.environ.get("OPS_TELEGRAM_CHAT_ID", "").strip()
    # If either dedicated value is present, keep the pair isolated and let the
    # notifier report incomplete configuration rather than mixing destinations.
    if ops_token or ops_chat:
        return ops_token, ops_chat
    app_env = root / ".env"
    if not _is_private_file(app_env):
        return "", ""
    try:
        values = _parse_env_file(app_env)
    except ValueError:
        return "", ""
    return (
        values.get("TELEGRAM_BOT_TOKEN", "").strip(),
        values.get("TELEGRAM_CHAT_ID", "").strip(),
    )


def settings_from_env(args: argparse.Namespace) -> Settings:
    root = Path(args.root or os.environ.get("AI_FILTER_ROOT", "/root/ai-rss-filter"))
    backup_dir = Path(args.backup_dir or os.environ.get("OPS_BACKUP_DIR", "/root/backups/system"))
    state_file = Path(
        args.state_file
        or os.environ.get("OPS_STATE_FILE", "/var/lib/ai-filter-ops-check/state.json")
    )
    backup_max_age = _positive_int(
        str(
            args.backup_max_age
            if args.backup_max_age is not None
            else os.environ.get("OPS_BACKUP_MAX_AGE_SECONDS", "108000")
        ),
        "OPS_BACKUP_MAX_AGE_SECONDS",
    )
    cooldown = _positive_int(
        str(
            args.cooldown
            if args.cooldown is not None
            else os.environ.get("OPS_ALERT_COOLDOWN_SECONDS", "1800")
        ),
        "OPS_ALERT_COOLDOWN_SECONDS",
        allow_zero=True,
    )
    timeout = _positive_int(
        os.environ.get("OPS_COMMAND_TIMEOUT_SECONDS", "10"),
        "OPS_COMMAND_TIMEOUT_SECONDS",
    )
    run_budget = _bounded_int(
        os.environ.get("OPS_RUN_BUDGET_SECONDS", "55"),
        "OPS_RUN_BUDGET_SECONDS",
        minimum=10,
        maximum=60,
    )
    backup_reverify = _positive_int(
        os.environ.get("OPS_BACKUP_REVERIFY_SECONDS", "86400"),
        "OPS_BACKUP_REVERIFY_SECONDS",
    )
    restore_drill_max_age = _positive_int(
        os.environ.get("OPS_RESTORE_DRILL_MAX_AGE_SECONDS", "691200"),
        "OPS_RESTORE_DRILL_MAX_AGE_SECONDS",
    )
    restore_drill_running_grace = _bounded_int(
        os.environ.get("OPS_RESTORE_DRILL_RUNNING_GRACE_SECONDS", "3000"),
        "OPS_RESTORE_DRILL_RUNNING_GRACE_SECONDS",
        minimum=60,
        maximum=3600,
    )
    enrichment_max_age = _positive_int(
        os.environ.get("OPS_ENRICHMENT_MAX_AGE_SECONDS", "4500"),
        "OPS_ENRICHMENT_MAX_AGE_SECONDS",
    )
    enrichment_running_grace = _bounded_int(
        os.environ.get("OPS_ENRICHMENT_RUNNING_GRACE_SECONDS", "900"),
        "OPS_ENRICHMENT_RUNNING_GRACE_SECONDS",
        minimum=60,
        maximum=3600,
    )
    required_services = _csv_names(
        os.environ.get(
            "OPS_REQUIRED_SERVICES",
            "redis,rsshub,ai-filter-web,ai-filter-api,ai-filter-worker",
        ),
        "OPS_REQUIRED_SERVICES",
    )
    app_services = _csv_names(
        os.environ.get(
            "OPS_APP_SERVICES", "ai-filter-api,ai-filter-worker"
        ),
        "OPS_APP_SERVICES",
    )
    hook = _optional_path(os.environ.get("SYSTEM_BACKUP_UPLOAD_HOOK", ""))
    telegram_token, telegram_chat_id = _load_telegram_credentials(root)
    return Settings(
        root=root,
        backup_dir=backup_dir,
        backup_max_age_seconds=backup_max_age,
        state_file=state_file,
        cooldown_seconds=cooldown,
        metrics_url=os.environ.get(
            "OPS_METRICS_URL", "http://127.0.0.1:5000/metrics"
        ).strip(),
        api_key_file=_optional_path(os.environ.get("OPS_API_KEY_FILE", "")),
        timer_unit=os.environ.get(
            "OPS_MARKET_TIMER_UNIT", "ai-filter-market-close.timer"
        ).strip(),
        release_registry=Path(
            os.environ.get(
                "OPS_RELEASE_REGISTRY", str(root / "data" / "releases")
            )
        ),
        compose_project=os.environ.get("OPS_COMPOSE_PROJECT", "ai-rss-filter").strip(),
        required_services=required_services,
        app_services=app_services,
        offsite_hook=hook,
        offsite_state_file=Path(
            os.environ.get(
                "SYSTEM_BACKUP_OFFSITE_STATE",
                str(backup_dir / ".offsite-upload.state"),
            )
        ),
        require_offsite=_parse_bool(
            os.environ.get(
                "OPS_REQUIRE_OFFSITE_UPLOAD",
                os.environ.get("SYSTEM_BACKUP_UPLOAD_REQUIRED", "0"),
            ),
            "OPS_REQUIRE_OFFSITE_UPLOAD",
        ),
        telegram_token=telegram_token,
        telegram_chat_id=telegram_chat_id,
        command_timeout_seconds=timeout,
        backup_verification_state_file=Path(
            os.environ.get(
                "OPS_BACKUP_VERIFICATION_STATE",
                str(state_file.parent / "backup-verification.json"),
            )
        ),
        backup_reverify_seconds=backup_reverify,
        run_budget_seconds=run_budget,
        restore_drill_state_file=Path(
            os.environ.get(
                "OPS_RESTORE_DRILL_STATE",
                str(state_file.parent / "restore-drill.json"),
            )
        ),
        restore_drill_max_age_seconds=restore_drill_max_age,
        restore_drill_running_grace_seconds=restore_drill_running_grace,
        enrichment_state_file=Path(
            os.environ.get(
                "OPS_ENRICHMENT_STATE",
                str(state_file.parent / "event-enrichment.json"),
            )
        ),
        enrichment_max_age_seconds=enrichment_max_age,
        enrichment_running_grace_seconds=enrichment_running_grace,
        database_path=Path(
            os.environ.get("OPS_DATABASE_PATH", str(root / "data" / "cache.db"))
        ),
        market_service_unit=os.environ.get(
            "OPS_MARKET_SERVICE_UNIT", "ai-filter-market-close.service"
        ).strip(),
    )


def _safe_detail(value: object, *, limit: int = 240) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    return " ".join(text.split())[:limit]


def _is_loopback_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
            return False
        host = parsed.hostname.rstrip(".").lower()
        if host == "localhost":
            return True
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return False


def _read_first_line(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return handle.readline(8193).strip()
    except OSError:
        return ""


def _load_api_key(settings: Settings) -> str:
    direct = os.environ.get("AI_FILTER_API_KEY", "").strip()
    if direct:
        return direct
    if settings.api_key_file:
        return (
            _read_first_line(settings.api_key_file)
            if _is_private_file(settings.api_key_file)
            else ""
        )
    try:
        env_file = settings.root / ".env"
        if not _is_private_file(env_file):
            return ""
        return _parse_env_file(env_file).get("AI_FILTER_API_KEY", "").strip()
    except ValueError:
        return ""


def _http_get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 5,
    max_bytes: int = 1024 * 1024,
) -> tuple[int, bytes]:
    if not _is_loopback_url(url):
        raise ValueError("endpoint must use loopback HTTP")
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    # Never let host proxy variables route an API key away from loopback.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    with opener.open(request, timeout=timeout) as response:
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError("response exceeds size limit")
        return int(response.status), body


def _remaining_timeout(settings: Settings, maximum: float | None = None) -> float:
    timeout = float(settings.command_timeout_seconds)
    if maximum is not None:
        timeout = min(timeout, float(maximum))
    if settings.run_deadline_monotonic is not None:
        remaining = settings.run_deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("run budget exhausted")
        timeout = min(timeout, remaining)
    return max(0.1, timeout)


_PROMETHEUS_SCALAR_RE = re.compile(
    r"^([A-Za-z_:][A-Za-z0-9_:]*)[ \t]+"
    r"([-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?)"
    r"(?:[ \t]+[0-9]+)?$"
)


def _parse_prometheus_scalars(text: str) -> dict[str, float]:
    """Parse the unlabelled gauges used by the operational contract.

    Labelled series and comments are intentionally ignored. Duplicate scalar
    names are rejected so a malformed payload cannot make a red gauge look
    green by ordering two values strategically.
    """
    values: dict[str, float] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "{" in line:
            continue
        match = _PROMETHEUS_SCALAR_RE.fullmatch(line)
        if not match:
            continue
        name, raw_value = match.groups()
        if name in values:
            raise ValueError(f"duplicate metric: {name}")
        value = float(raw_value)
        if not math.isfinite(value):
            raise ValueError(f"non-finite metric: {name}")
        values[name] = value
    return values


def check_metrics(settings: Settings, _now: int) -> CheckResult:
    key = _load_api_key(settings)
    if not key:
        return CheckResult("metrics", False, "API credential unavailable")
    try:
        status, body = _http_get(
            settings.metrics_url,
            headers={"X-API-Key": key, "Accept": "text/plain"},
            timeout=_remaining_timeout(settings, 10),
        )
        if status != 200:
            return CheckResult("metrics", False, f"HTTP {status}")
        text = body.decode("utf-8", errors="replace")
        values = _parse_prometheus_scalars(text)
        required = {
            "ai_filter_refresh_stale",
            "ai_filter_refresh_degraded",
            "ai_filter_feed_empty_degraded_sources",
            "ai_filter_worker_queue_alert",
            "ai_filter_score_fallback_alert",
            "ai_filter_cold_filter_limit",
            "ai_filter_cold_filter_inflight",
            "ai_filter_cold_filter_rejections_total",
        }
        missing = sorted(required.difference(values))
        if missing:
            return CheckResult(
                "metrics", False, "required metrics missing=" + ",".join(missing)
            )

        failures: list[str] = []
        if values["ai_filter_refresh_stale"] != 0:
            failures.append("refresh_stale")
        if values["ai_filter_refresh_degraded"] != 0:
            failures.append("refresh_degraded")
        if values["ai_filter_feed_empty_degraded_sources"] > 0:
            failures.append("feed_empty_degraded")
        if values["ai_filter_worker_queue_alert"] != 0:
            failures.append("worker_queue_alert")
        if values["ai_filter_score_fallback_alert"] != 0:
            failures.append("score_fallback_alert")
        cold_inflight = values["ai_filter_cold_filter_inflight"]
        cold_limit = values["ai_filter_cold_filter_limit"]
        if cold_limit <= 0:
            failures.append("cold_filter_capacity_invalid")
        elif cold_inflight > cold_limit:
            failures.append("cold_filter_capacity_inconsistent")
        if failures:
            return CheckResult(
                "metrics",
                False,
                "red gauges=" + ",".join(failures),
            )

        warnings: list[str] = []
        if cold_limit > 0 and cold_inflight >= cold_limit:
            warnings.append("cold_filter_at_capacity")
        for metric, label in (
            ("ai_filter_cold_filter_rejections_total", "cold_filter_rejections_seen"),
        ):
            if values[metric] > 0:
                warnings.append(label)
        detail = "authenticated metrics healthy"
        safe_neutral_metrics = {
            "ai_filter_score_safe_neutral_count_2h",
            "ai_filter_score_safe_neutral_denominator_2h",
            "ai_filter_score_safe_neutral_ratio_2h",
            "ai_filter_score_safe_neutral_count_24h",
            "ai_filter_score_safe_neutral_denominator_24h",
            "ai_filter_score_safe_neutral_ratio_24h",
        }
        if safe_neutral_metrics.issubset(values):
            detail += (
                "; info=safe_neutral_2h="
                f"{int(values['ai_filter_score_safe_neutral_count_2h'])}/"
                f"{int(values['ai_filter_score_safe_neutral_denominator_2h'])} "
                f"ratio={values['ai_filter_score_safe_neutral_ratio_2h']:.6f},"
                "safe_neutral_24h="
                f"{int(values['ai_filter_score_safe_neutral_count_24h'])}/"
                f"{int(values['ai_filter_score_safe_neutral_denominator_24h'])} "
                f"ratio={values['ai_filter_score_safe_neutral_ratio_24h']:.6f}"
            )
        if warnings:
            detail += "; warnings=" + ",".join(warnings)
        return CheckResult(
            "metrics",
            True,
            detail,
            warning=bool(warnings),
        )
    except urllib.error.HTTPError as exc:
        return CheckResult("metrics", False, f"HTTP {exc.code}")
    except Exception as exc:  # network/parse failures are deliberately summarized
        return CheckResult("metrics", False, type(exc).__name__)


def check_notification_transport(settings: Settings, _now: int) -> CheckResult:
    if settings.telegram_token and settings.telegram_chat_id:
        return CheckResult("notification_transport", True, "Telegram configured")
    if settings.telegram_token or settings.telegram_chat_id:
        return CheckResult(
            "notification_transport", False, "Telegram configuration is incomplete"
        )
    return CheckResult(
        "notification_transport",
        True,
        "unconfigured; failures are local-only",
        warning=True,
    )


def _run(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
        check=False,
        env=os.environ.copy(),
    )


def _service_container_id(settings: Settings, service: str) -> str:
    result = _run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={settings.compose_project}",
            "--filter",
            f"label=com.docker.compose.service={service}",
        ],
        _remaining_timeout(settings),
    )
    if result.returncode != 0:
        return ""
    ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return ids[0] if len(ids) == 1 else ""


def _hash_file(path: Path, *, deadline: float | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("run budget exhausted during archive verification")
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _read_backup_manifest(archive: Path) -> tuple[Path, str]:
    manifest = Path(str(archive) + ".sha256")
    if not manifest.is_file():
        raise ValueError("SHA256 manifest missing")
    manifest_text = manifest.read_text(encoding="ascii").strip()
    parts = manifest_text.split()
    if len(parts) != 2 or not _HASH_RE.fullmatch(parts[0]):
        raise ValueError("SHA256 manifest malformed")
    recorded_name = parts[1].lstrip("*")
    if recorded_name != archive.name:
        raise ValueError("SHA256 manifest filename mismatch")
    return manifest, parts[0]


def _verify_backup_contains_canonical_database(archive: Path) -> None:
    with tarfile.open(archive, mode="r:gz") as bundle:
        matches = [
            member
            for member in bundle.getmembers()
            if _BACKUP_DB_MEMBER_RE.fullmatch(member.name)
        ]
    if len(matches) != 1 or not matches[0].isfile() or matches[0].size <= 0:
        raise ValueError("canonical AI-Filter data/cache.db is missing from backup")


def _read_private_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    if not _is_private_file(path):
        raise ValueError("verification state ownership or mode is unsafe")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("verification state malformed")
    return payload


def check_backup(settings: Settings, now: int) -> CheckResult:
    try:
        archives = [
            path
            for path in settings.backup_dir.glob("system_backup_*.tar.gz")
            if path.is_file()
        ]
        if not archives:
            return CheckResult("backup", False, "no system backup archive")
        latest = max(archives, key=lambda path: path.stat().st_mtime)
        age = now - int(latest.stat().st_mtime)
        if age < -300:
            return CheckResult("backup", False, "latest archive timestamp is in the future")
        if age > settings.backup_max_age_seconds:
            return CheckResult("backup", False, f"stale age_seconds={age}")
        _manifest, expected_hash = _read_backup_manifest(latest)
        stat_result = latest.stat()
        verification_path = settings.backup_verification_state_file
        cached: dict[str, object] = {}
        if verification_path is not None:
            cached = _read_private_state(verification_path)
        cached_at = int(cached.get("verified_at") or 0)
        cache_matches = (
            cached.get("version") == 1
            and cached.get("archive") == latest.name
            and cached.get("sha256") == expected_hash
            and cached.get("size") == stat_result.st_size
            and cached.get("mtime_ns") == stat_result.st_mtime_ns
            and cached.get("canonical_database") is True
            and 0 <= now - cached_at < settings.backup_reverify_seconds
        )
        verification = "cached"
        if not cache_matches:
            actual_hash = _hash_file(
                latest, deadline=settings.run_deadline_monotonic
            )
            if not hmac.compare_digest(expected_hash, actual_hash):
                return CheckResult("backup", False, "SHA256 mismatch")
            _verify_backup_contains_canonical_database(latest)
            verification = "full"
            if settings.persist_verification_state and verification_path is not None:
                _write_state(
                    verification_path,
                    {
                        "version": 1,
                        "archive": latest.name,
                        "sha256": actual_hash,
                        "size": stat_result.st_size,
                        "mtime_ns": stat_result.st_mtime_ns,
                        "canonical_database": True,
                        "verified_at": now,
                    },
                )
        return CheckResult(
            "backup",
            True,
            f"fresh age_seconds={max(0, age)} archive={latest.name} verification={verification}",
        )
    except (OSError, UnicodeError, ValueError, TypeError, tarfile.TarError) as exc:
        detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        return CheckResult("backup", False, _safe_detail(detail))


def _parse_key_value_state(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw or "=" not in raw:
            raise ValueError("malformed state")
        key, value = raw.split("=", 1)
        if key in values:
            raise ValueError("duplicate state key")
        values[key] = value
    return values


def check_restore_drill(settings: Settings, now: int) -> CheckResult:
    state_path = settings.restore_drill_state_file
    if state_path is None:
        return CheckResult("restore_drill", False, "restore drill state is unconfigured")
    try:
        state = _read_private_state(state_path)
        if state.get("version") != 1:
            return CheckResult("restore_drill", False, "restore drill state missing or malformed")
        status = state.get("status")
        if status == "running":
            started_at = int(state.get("started_at") or 0)
            running_age = now - started_at
            if running_age < -300:
                return CheckResult(
                    "restore_drill", False, "restore drill start timestamp is in the future"
                )
            if running_age <= settings.restore_drill_running_grace_seconds:
                return CheckResult(
                    "restore_drill",
                    True,
                    f"isolated restore drill running age_seconds={max(0, running_age)}",
                    warning=True,
                )
            return CheckResult(
                "restore_drill", False, f"restore drill running timeout age_seconds={running_age}"
            )
        if status != "ok":
            error = str(state.get("error") or "unknown")
            return CheckResult(
                "restore_drill", False, f"latest restore drill failed error={_safe_detail(error, limit=64)}"
            )
        checked_at = int(state.get("checked_at") or 0)
        age = now - checked_at
        if age < -300:
            return CheckResult("restore_drill", False, "restore drill timestamp is in the future")
        if age > settings.restore_drill_max_age_seconds:
            return CheckResult("restore_drill", False, f"stale age_seconds={age}")
        archive = str(state.get("archive") or "")
        digest = str(state.get("sha256") or "")
        if not re.fullmatch(r"system_backup_[A-Za-z0-9T_.-]+\.tar\.gz", archive):
            return CheckResult("restore_drill", False, "restore drill archive is malformed")
        if not _HASH_RE.fullmatch(digest):
            return CheckResult("restore_drill", False, "restore drill hash is malformed")
        tables = state.get("required_tables")
        if not isinstance(tables, list) or not tables:
            return CheckResult("restore_drill", False, "restore drill table proof is missing")
        if not _RESTORE_REQUIRED_TABLES.issubset(
            {str(table) for table in tables}
        ):
            return CheckResult(
                "restore_drill", False, "restore drill table proof is outdated"
            )
        return CheckResult(
            "restore_drill",
            True,
            f"isolated SQLite restore verified age_seconds={max(0, age)} archive={archive}",
        )
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        return CheckResult("restore_drill", False, _safe_detail(detail))


def check_offsite(settings: Settings, now: int) -> CheckResult:
    hook = settings.offsite_hook
    if hook is None:
        return CheckResult(
            "offsite_upload",
            not settings.require_offsite,
            "unconfigured; backups are local-only",
            warning=not settings.require_offsite,
        )
    try:
        stat_result = hook.stat()
        if not hook.is_absolute() or not hook.is_file() or not os.access(hook, os.X_OK):
            return CheckResult("offsite_upload", False, "hook is not an absolute executable file")
        if stat_result.st_uid != 0 or stat_result.st_mode & 0o022:
            return CheckResult("offsite_upload", False, "hook ownership or mode is unsafe")
        state = _parse_key_value_state(settings.offsite_state_file)
        if state.get("version") != "1" or not state.get("uploaded_at", "").isdigit():
            return CheckResult("offsite_upload", False, "upload state malformed")
        uploaded_at = int(state["uploaded_at"])
        age = now - uploaded_at
        if age < -300 or age > settings.backup_max_age_seconds:
            return CheckResult("offsite_upload", False, f"upload state stale age_seconds={age}")
        if not _HASH_RE.fullmatch(state.get("sha256", "")):
            return CheckResult("offsite_upload", False, "upload state hash malformed")
        archive = state.get("archive", "")
        if not re.fullmatch(r"system_backup_[A-Za-z0-9T_.-]+\.tar\.gz", archive):
            return CheckResult("offsite_upload", False, "upload state archive malformed")
        local_archive = settings.backup_dir / archive
        if not local_archive.is_file():
            return CheckResult("offsite_upload", False, "uploaded archive state mismatch")
        _manifest, local_hash = _read_backup_manifest(local_archive)
        if not hmac.compare_digest(state["sha256"], local_hash):
            return CheckResult("offsite_upload", False, "uploaded archive state mismatch")
        local_archives = [
            path
            for path in settings.backup_dir.glob("system_backup_*.tar.gz")
            if path.is_file()
        ]
        if not local_archives or max(
            local_archives, key=lambda path: path.stat().st_mtime
        ).name != archive:
            return CheckResult("offsite_upload", False, "latest local backup is not uploaded")
        return CheckResult("offsite_upload", True, f"verified age_seconds={max(0, age)}")
    except (OSError, ValueError, UnicodeError) as exc:
        return CheckResult("offsite_upload", False, type(exc).__name__)


def check_event_enrichment(settings: Settings, now: int) -> CheckResult:
    state_path = settings.enrichment_state_file
    if state_path is None:
        return CheckResult("event_enrichment", False, "enrichment state is unconfigured")
    try:
        state = _read_private_state(state_path)
        if state.get("version") != 1:
            return CheckResult("event_enrichment", False, "enrichment state missing or malformed")
        status = str(state.get("status") or "")
        started_at = int(state.get("started_at") or 0)
        if started_at <= 0 or started_at > now + 300:
            return CheckResult("event_enrichment", False, "enrichment timestamp is invalid")
        if status == "running":
            age = now - started_at
            if age <= settings.enrichment_running_grace_seconds:
                return CheckResult(
                    "event_enrichment",
                    True,
                    f"running age_seconds={max(0, age)}",
                    warning=True,
                )
            return CheckResult(
                "event_enrichment", False, f"running timeout age_seconds={age}"
            )
        completed_at = int(state.get("completed_at") or 0)
        age = now - completed_at
        if completed_at < started_at or age < -300:
            return CheckResult("event_enrichment", False, "enrichment completion timestamp is invalid")
        if age > settings.enrichment_max_age_seconds:
            return CheckResult("event_enrichment", False, f"stale age_seconds={age}")
        exit_code = int(state.get("exit_code") or 0)
        if status != "ok" or exit_code != 0:
            return CheckResult(
                "event_enrichment",
                False,
                f"latest host run failed status={status or 'unknown'} "
                f"exit_code={exit_code}",
            )
        return CheckResult(
            "event_enrichment",
            True,
            f"completed age_seconds={max(0, age)}",
        )
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        return CheckResult("event_enrichment", False, _safe_detail(detail))


def _expected_market_trade_date(
    now: int, *, ready_at: datetime_time = datetime_time(16, 0)
) -> str:
    current = datetime.fromtimestamp(now, _MARKET_TZ)
    candidate = current.date()
    # Leave the one-shot collector its bounded retry/startup window. Before
    # 16:00 the previous trading close remains the freshness contract.
    if current.time() < ready_at:
        candidate -= timedelta(days=1)
    for _ in range(15):
        holidays = _CHINA_HOLIDAYS_BY_YEAR.get(candidate.year)
        if holidays is None:
            raise ValueError("A-share trading calendar is not configured")
        if candidate.weekday() < 5 and candidate.isoformat() not in holidays:
            return candidate.isoformat()
        candidate -= timedelta(days=1)
    raise ValueError("A-share trading date cannot be determined")


def _check_market_database(settings: Settings, now: int) -> tuple[str, int]:
    database_path = settings.database_path or (settings.root / "data" / "cache.db")
    if not database_path.is_file():
        raise ValueError("market database is missing")
    expected = _expected_market_trade_date(now)
    connection = sqlite3.connect(
        f"file:{database_path}?mode=ro", uri=True, timeout=min(10, settings.command_timeout_seconds)
    )
    try:
        placeholders = ",".join("?" for _ in _MARKET_REQUIRED_SYMBOLS)
        rows = connection.execute(
            "SELECT lower(symbol), ts FROM future_prices "
            f"WHERE trade_date = ? AND lower(symbol) IN ({placeholders})",
            (expected, *sorted(_MARKET_REQUIRED_SYMBOLS)),
        ).fetchall()
    finally:
        connection.close()
    symbols = {str(row[0]) for row in rows}
    if symbols != _MARKET_REQUIRED_SYMBOLS:
        raise ValueError("market database is missing required benchmark closes")
    for _symbol, timestamp in rows:
        source_time = datetime.fromtimestamp(int(timestamp), _MARKET_TZ)
        if source_time.date().isoformat() != expected or source_time.time() < datetime_time(15, 0):
            raise ValueError("market database contains stale or pre-close snapshots")
    return expected, len(rows)


def _load_market_result(settings: Settings, kind: str) -> dict[str, object]:
    path = settings.root / "data" / "market_close" / f"{kind}_last.json"
    if not path.is_file():
        raise ValueError(f"{kind} result missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{kind} result unreadable") from exc
    if (
        not isinstance(payload, dict)
        or type(payload.get("version")) is not int
        or payload.get("version") != 1
        or payload.get("kind") != kind
    ):
        raise ValueError(f"{kind} result malformed")
    status = str(payload.get("status") or "")
    if status not in {
        "success", "complete", "incomplete", "error",
        "skipped_maintenance", "skipped_nontrading",
    }:
        raise ValueError(f"{kind} result malformed")
    return payload


def _market_result_times(payload: dict[str, object]) -> tuple[int, int]:
    started, completed = payload.get("started_at"), payload.get("completed_at")
    if (
        type(started) is not int or type(completed) is not int
        or started <= 0 or completed < started
    ):
        raise ValueError("market result timestamps invalid")
    return started, completed


def _collector_is_coverage_replay(payload: dict[str, object]) -> bool:
    """Coverage replay may write status=success; that is not a natural 15:10."""
    detail = str(payload.get("detail") or "").strip().casefold()
    return detail.startswith("replay:")


def _market_timer_from_results(
    settings: Settings, expected_date: str, row_count: int
) -> CheckResult:
    collector = _load_market_result(settings, "collector")
    status = str(collector.get("status") or "")
    if status == "skipped_maintenance":
        return CheckResult(
            "market_timer",
            False,
            "collector skipped maintenance; coverage not confirmed; "
            f"database_trade_date={expected_date} benchmark_rows={row_count}",
        )
    trade_date = str(collector.get("trade_date") or "")
    if trade_date != expected_date:
        return CheckResult(
            "market_timer",
            False,
            "collector result stale or mismatched; "
            f"result_trade_date={trade_date or 'missing'} "
            f"database_trade_date={expected_date}",
        )
    if status == "success" and _collector_is_coverage_replay(collector):
        return CheckResult(
            "market_timer",
            False,
            "collector success is coverage replay, not natural close; "
            f"database_trade_date={expected_date} benchmark_rows={row_count}",
        )
    if status == "success":
        return CheckResult(
            "market_timer",
            True,
            "collector success; "
            f"database_trade_date={expected_date} benchmark_rows={row_count} "
            f"due_total={collector.get('due_total')} "
            f"unresolved={collector.get('unresolved')}",
        )
    if status == "incomplete":
        try:
            repair = _load_market_result(settings, "repair")
            _collector_started, collector_completed = _market_result_times(collector)
            repair_started, _repair_completed = _market_result_times(repair)
        except ValueError as exc:
            return CheckResult("market_timer", False, _safe_detail(exc))
        if (
            str(repair.get("trade_date") or "") == expected_date
            and str(repair.get("status") or "") == "complete"
            and repair_started > collector_completed
        ):
            return CheckResult(
                "market_timer",
                True,
                "collector incomplete; same-day repair complete; "
                f"database_trade_date={expected_date} benchmark_rows={row_count}",
                warning=True,
            )
        return CheckResult(
            "market_timer",
            False,
            "collector incomplete; same-day pending not repaired; "
            f"repair_status={repair.get('status')}",
        )
    return CheckResult(
        "market_timer",
        False,
        f"collector status={status or 'unknown'}; coverage not confirmed",
    )


def check_market_timer(settings: Settings, now: int) -> CheckResult:
    service_unit = settings.market_service_unit or settings.timer_unit.removesuffix(".timer") + ".service"
    if not _SAFE_NAME_RE.fullmatch(settings.timer_unit) or not _SAFE_NAME_RE.fullmatch(service_unit):
        return CheckResult("market_timer", False, "market unit name invalid")
    try:
        enabled = _run(
            ["systemctl", "is-enabled", "--quiet", settings.timer_unit],
            _remaining_timeout(settings),
        )
        active = _run(
            ["systemctl", "is-active", "--quiet", settings.timer_unit],
            _remaining_timeout(settings),
        )
        next_run = _run(
            [
                "systemctl",
                "show",
                settings.timer_unit,
                "--property=NextElapseUSecRealtime",
                "--value",
            ],
            _remaining_timeout(settings),
        )
        service = _run(
            [
                "systemctl",
                "show",
                service_unit,
                "--property=ActiveState",
                "--property=Result",
                "--property=ExecMainStatus",
                "--property=ExecMainExitTimestamp",
            ],
            _remaining_timeout(settings),
        )
        if enabled.returncode != 0:
            return CheckResult("market_timer", False, "timer is not enabled")
        if active.returncode != 0:
            return CheckResult("market_timer", False, "timer is not active")
        if next_run.returncode != 0 or not next_run.stdout.strip():
            return CheckResult("market_timer", False, "timer has no next elapse")
        if service.returncode != 0:
            return CheckResult("market_timer", False, "market service status unavailable")
        properties: dict[str, str] = {}
        for line in service.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                properties[key] = value.strip()
        if properties.get("ActiveState") == "activating":
            local = datetime.fromtimestamp(now, _MARKET_TZ)
            if local.time() >= datetime_time(16, 0):
                return CheckResult(
                    "market_timer", False, "market service is still running after grace"
                )
        last_exit = properties.get("ExecMainExitTimestamp", "")
        if not last_exit or last_exit == "n/a":
            return CheckResult("market_timer", False, "market service has no last completion")
        expected_date, row_count = _check_market_database(settings, now)
        return _market_timer_from_results(settings, expected_date, row_count)
    except (
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
        OverflowError,
        subprocess.TimeoutExpired,
    ) as exc:
        detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        return CheckResult("market_timer", False, _safe_detail(detail))


def check_market_history(settings: Settings, now: int) -> CheckResult:
    try:
        payload = _load_market_result(settings, "history")
    except ValueError as exc:
        if "missing" in str(exc):
            return CheckResult(
                "market_history",
                True,
                "history result missing; backlog not yet reported",
                warning=True,
            )
        return CheckResult("market_history", False, _safe_detail(exc))
    status = str(payload.get("status") or "")
    if status == "error":
        return CheckResult(
            "market_history",
            False,
            f"history repair error detail={payload.get('detail') or 'unknown'}",
        )
    try:
        started, completed = _market_result_times(payload)
        # 16:30 timer + 5-minute service timeout + 30-second scheduling accuracy.
        expected = _expected_market_trade_date(now, ready_at=datetime_time(16, 35, 30))
        scheduled = int(datetime.combine(
            datetime.strptime(expected, "%Y-%m-%d").date(),
            datetime_time(16, 30), tzinfo=_MARKET_TZ,
        ).timestamp())
        if started < scheduled or completed > now:
            raise ValueError(f"history result stale or future; expected_run_date={expected}")
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        return CheckResult("market_history", False, _safe_detail(exc))
    remaining = payload.get("remaining_total", payload.get("due_total"))
    if status == "complete":
        return CheckResult(
            "market_history",
            True,
            f"historical pending complete trade_date={payload.get('trade_date')}",
        )
    return CheckResult(
        "market_history",
        True,
        "historical pending remain "
        f"total={remaining} truncated={payload.get('truncated')} "
        f"unresolved={payload.get('unresolved')} "
        f"trade_date={payload.get('trade_date')}",
        warning=True,
    )


def check_root_disk(settings: Settings, _now: int) -> CheckResult:
    try:
        usage = shutil.disk_usage("/")
    except OSError as exc:
        return CheckResult("root_disk", False, type(exc).__name__)
    usable = usage.used + usage.free
    if usable <= 0:
        return CheckResult("root_disk", False, "root disk total is invalid")
    # Match df Use% (used / (used + avail)), not used/total which hides reserved blocks.
    percent = math.ceil(100.0 * usage.used / usable)
    cache_db = (settings.database_path or (settings.root / "data" / "cache.db"))
    cache_bytes = cache_db.stat().st_size if cache_db.is_file() else 0
    detail = (
        f"root_disk_used_percent={percent} avail_bytes={usage.free} "
        f"cache_db_bytes={cache_bytes}"
    )
    if percent >= 90:
        return CheckResult("root_disk", False, detail)
    if percent >= 80:
        return CheckResult("root_disk", True, detail, warning=True)
    return CheckResult("root_disk", True, detail)


def _read_release_state(registry: Path) -> tuple[str, str, str, tuple[str, ...]]:
    lines = (registry / "current.state").read_text(encoding="utf-8").splitlines()
    if len(lines) != 4 or any(not line or "\r" in line for line in lines):
        raise ValueError("current release state malformed")
    ref, digest, manifest_hash, services_value = lines
    if ref.endswith(":latest") or ref == "latest":
        raise ValueError("floating release ref")
    if not _DIGEST_RE.fullmatch(digest) or not _HASH_RE.fullmatch(manifest_hash):
        raise ValueError("release digest/hash malformed")
    services = tuple(part for part in services_value.split() if part)
    if (
        not services
        or len(set(services)) != len(services)
        or any(not _SAFE_SERVICE_NAME_RE.fullmatch(part) for part in services)
    ):
        raise ValueError("release services malformed")
    return ref, digest, manifest_hash, services


def _parse_manifest_runtime_services(payload: str) -> dict[str, bool]:
    try:
        model = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("archived manifest model is malformed") from exc
    if not isinstance(model, dict) or not isinstance(model.get("services"), dict):
        raise ValueError("archived manifest model is malformed")

    runtime_services: dict[str, bool] = {}
    for service, definition in model["services"].items():
        if (
            not isinstance(service, str)
            or not _SAFE_SERVICE_NAME_RE.fullmatch(service)
            or not isinstance(definition, dict)
        ):
            raise ValueError("archived manifest model is malformed")
        profiles = definition.get("profiles", [])
        if profiles is None:
            profiles = []
        if not isinstance(profiles, list) or any(
            not isinstance(profile, str) or not profile for profile in profiles
        ):
            raise ValueError("archived manifest model is malformed")
        if profiles:
            continue
        healthcheck = definition.get("healthcheck")
        if healthcheck is not None and not isinstance(healthcheck, dict):
            raise ValueError("archived manifest model is malformed")
        runtime_services[service] = healthcheck is not None

    if not runtime_services:
        raise ValueError("archived manifest has no default runtime services")
    return runtime_services


def _read_manifest_runtime_services(
    settings: Settings, manifest: Path
) -> dict[str, bool]:
    # Render only the Compose model. A sterile environment plus no interpolation
    # and no env-file resolution prevents operator/application secrets from
    # entering the model subprocess.
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(settings.root),
            "--project-name",
            settings.compose_project,
            "--env-file",
            os.devnull,
            "-f",
            str(manifest),
            "config",
            "--format",
            "json",
            "--no-interpolate",
            "--no-env-resolution",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=_remaining_timeout(settings),
        check=False,
        env={"PATH": os.environ.get("PATH", os.defpath)},
    )
    if result.returncode != 0:
        raise ValueError("archived manifest model could not be rendered")
    return _parse_manifest_runtime_services(result.stdout)


def check_release_and_containers(settings: Settings, _now: int) -> CheckResult:
    release_lock = settings.release_registry / "release.lock"
    try:
        with release_lock.open("r", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return CheckResult(
                    "release_containers",
                    True,
                    "release in progress; container check deferred",
                    warning=True,
                )

            _ref, digest, manifest_hash, recorded_services = _read_release_state(
                settings.release_registry
            )
            manifest = (
                settings.release_registry
                / "manifests"
                / f"{manifest_hash}.compose.yml"
            )
            if not manifest.is_file() or not hmac.compare_digest(
                _hash_file(manifest), manifest_hash
            ):
                return CheckResult(
                    "release_containers", False, "archived manifest missing or drifted"
                )
            manifest_services = _read_manifest_runtime_services(settings, manifest)
            if not hmac.compare_digest(_hash_file(manifest), manifest_hash):
                return CheckResult(
                    "release_containers", False, "archived manifest missing or drifted"
                )
            if set(recorded_services) != set(manifest_services):
                return CheckResult(
                    "release_containers",
                    False,
                    "release services do not match archived manifest",
                )

            warnings: list[str] = []
            for service in recorded_services:
                container_id = _service_container_id(settings, service)
                if not container_id:
                    return CheckResult(
                        "release_containers", False, f"service {service} not uniquely found"
                    )
                inspected = _run(
                    [
                        "docker",
                        "inspect",
                        "--format",
                        "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.Image}}|{{.RestartCount}}|{{.State.OOMKilled}}",
                        container_id,
                    ],
                    _remaining_timeout(settings),
                )
                if inspected.returncode != 0:
                    return CheckResult(
                        "release_containers", False, f"service {service} inspect failed"
                    )
                fields = inspected.stdout.strip().split("|")
                if len(fields) != 5:
                    return CheckResult(
                        "release_containers", False, f"service {service} inspect malformed"
                    )
                state, health, image_id, restart_count, oom_killed = fields
                if (
                    state != "running"
                    or health not in {"healthy", "none"}
                    or (manifest_services[service] and health != "healthy")
                ):
                    return CheckResult(
                        "release_containers", False, f"service {service} is not healthy"
                    )
                if oom_killed.lower() == "true":
                    return CheckResult(
                        "release_containers", False, f"service {service} was OOM-killed"
                    )
                if service in _RELEASE_APP_SERVICES and image_id != digest:
                    return CheckResult(
                        "release_containers", False, f"service {service} image drift"
                    )
                if service in _RELEASE_APP_SERVICES or service in settings.app_services:
                    runtime_diff = _run(
                        ["docker", "diff", container_id],
                        _remaining_timeout(settings),
                    )
                    if runtime_diff.returncode != 0:
                        return CheckResult(
                            "release_containers",
                            False,
                            f"service {service} runtime diff failed",
                        )
                    drifted_paths = [
                        line
                        for line in runtime_diff.stdout.splitlines()
                        if re.fullmatch(
                            r"[ACD] /app/(?:.*\.py|config\.yaml)", line
                        )
                    ]
                    if drifted_paths:
                        return CheckResult(
                            "release_containers", False, f"service {service} source/config drift"
                        )
                if restart_count.isdigit() and int(restart_count) > 0:
                    warnings.append(f"{service}:{restart_count}")
            detail = "release manifest and containers healthy"
            if warnings:
                detail += "; restart_counts=" + ",".join(warnings)
            return CheckResult(
                "release_containers", True, detail, warning=bool(warnings)
            )
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired) as exc:
        return CheckResult("release_containers", False, type(exc).__name__)


CHECKS: tuple[Callable[[Settings, int], CheckResult], ...] = (
    check_metrics,
    check_notification_transport,
    check_backup,
    check_restore_drill,
    check_offsite,
    check_event_enrichment,
    check_market_timer,
    check_market_history,
    check_root_disk,
    check_release_and_containers,
)


def evaluate(settings: Settings, now: int) -> list[CheckResult]:
    if settings.run_deadline_monotonic is None:
        settings = dataclasses.replace(
            settings,
            run_deadline_monotonic=time.monotonic() + settings.run_budget_seconds,
        )
    results: list[CheckResult] = []
    for check in CHECKS:
        if check.__name__ in _POLICY_DISABLED_CHECKS:
            valid_policy = _BACKUP_POLICY == "NO_BACKUP"
            results.append(CheckResult(
                _POLICY_DISABLED_CHECKS[check.__name__],
                valid_policy,
                "SKIPPED_BY_POLICY NO_BACKUP; not executed or verified; prior evidence retained"
                if valid_policy else "backup policy invalid; refusing to assume backup required or healthy",
                skipped=valid_policy,
            ))
            continue
        if (
            settings.run_deadline_monotonic is not None
            and time.monotonic() >= settings.run_deadline_monotonic
        ):
            results.append(
                CheckResult(check.__name__, False, "run budget exhausted")
            )
            continue
        try:
            result = check(settings, now)
        except Exception as exc:  # one broken check must not hide the others
            result = CheckResult(check.__name__, False, type(exc).__name__)
        results.append(
            CheckResult(
                _safe_detail(result.name, limit=64),
                bool(result.ok),
                _safe_detail(result.detail),
                bool(result.warning),
                bool(result.skipped),
            )
        )
    return results


def _fingerprint(results: Iterable[CheckResult]) -> str:
    # Details can contain a growing age counter. Fingerprinting them would turn
    # one stale backup into a new incident every run and defeat the cooldown.
    def normalized_detail(detail: str) -> str:
        value = detail.lower()
        value = re.sub(r"(age_seconds=)-?[0-9]+(?:\.[0-9]+)?", r"\1#", value)
        value = re.sub(
            r"(archive=system_backup_)[^ ;]+", r"\1#", value
        )
        return value

    failed = sorted(
        f"{item.name}:{normalized_detail(item.detail)}"
        for item in results
        if not item.ok
    )
    return hashlib.sha256(
        json.dumps(failed, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def notification_decision(
    previous: dict[str, object], results: list[CheckResult], now: int, cooldown: int
) -> str:
    failed = any(not item.ok for item in results)
    # Old aggregate fingerprints cannot reveal whether the prior failure was a
    # retired backup check. The first natural policy-epoch transition must not
    # turn intentional retirement into a recovery notification. A current real
    # business failure still follows the existing failure path below, and the
    # normal state write records the epoch for later genuine recoveries.
    if (
        not failed
        and any(item.skipped for item in results)
        and previous.get("backup_policy_epoch") != _BACKUP_POLICY_EPOCH
    ):
        return ""
    prior_status = str(previous.get("status") or "unknown")
    prior_fingerprint = str(previous.get("fingerprint") or "")
    try:
        prior_notice = int(previous.get("last_notice_at") or 0)
    except (TypeError, ValueError):
        prior_notice = 0
    current_fingerprint = _fingerprint(results)
    pending = str(previous.get("pending_notification") or "")
    if pending == "failure" and failed:
        return "failure"
    if pending == "recovery" and not failed:
        return "recovery"
    if failed:
        if (
            prior_status != "failing"
            or prior_fingerprint != current_fingerprint
            or now - prior_notice >= cooldown
        ):
            return "failure"
        return ""
    if prior_status == "failing":
        return "recovery"
    return ""


def _notification_text(kind: str, results: list[CheckResult], now: int) -> str:
    if kind == "recovery":
        return f"AI-Filter 运维检查已恢复 | ts={now}"
    failed_names = ",".join(item.name for item in results if not item.ok)
    text = f"AI-Filter 运维检查失败 | checks={failed_names or 'unknown'} | ts={now}"
    # Never forward arbitrary exception/detail text (URLs can contain secrets).
    # These are the exact fixed reason tokens emitted by check_metrics.
    allowed = {
        "refresh_stale", "refresh_degraded", "feed_empty_degraded",
        "worker_queue_alert", "score_fallback_alert",
        "cold_filter_capacity_invalid", "cold_filter_capacity_inconsistent",
    }
    reasons = []
    for result in results:
        if result.name != "metrics" or result.ok or not result.detail.startswith("red gauges="):
            continue
        for reason in result.detail.removeprefix("red gauges=").split(","):
            if reason in allowed and reason not in reasons:
                reasons.append(reason)
    if reasons:
        text += " | metrics_reasons=" + ",".join(reasons)
    return text


def send_telegram(settings: Settings, text: str) -> tuple[bool, str]:
    if not settings.telegram_token and not settings.telegram_chat_id:
        return False, "unconfigured"
    if not settings.telegram_token or not settings.telegram_chat_id:
        return False, "incomplete configuration"
    # The token is part of Telegram's URL. Never include the URL or exception
    # text in logs, results, or state.
    url = f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage"
    body = urllib.parse.urlencode(
        {"chat_id": settings.telegram_chat_id, "text": text}
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            body = response.read(64 * 1024 + 1)
            if len(body) > 64 * 1024:
                return False, "response too large"
            if int(response.status) != 200:
                return False, f"HTTP {response.status}"
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeError, ValueError, TypeError):
                return False, "invalid response"
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                return False, "API rejected message"
            return True, "sent"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception:
        return False, "transport failure"


def _read_state(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=".ops-state-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


_DASHBOARD_CHECK_NAMES = frozenset({
    "metrics", "notification_transport", "backup", "restore_drill",
    "offsite_upload", "event_enrichment", "market_timer", "market_history",
    "root_disk", "release_containers",
})
_DASHBOARD_CHECK_ALIASES = {
    "check_offsite": "offsite_upload",
    "check_release_and_containers": "release_containers",
    **{f"check_{name}": name for name in _DASHBOARD_CHECK_NAMES},
}
_DASHBOARD_RELEASE_REF = re.compile(
    r"(?:my-ai-filter:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}|sha256:[0-9a-f]{64})"
)


def _dashboard_release_identity(settings: Settings) -> tuple | None:
    try:
        identity = _read_release_state(settings.release_registry)
        return identity if _DASHBOARD_RELEASE_REF.fullmatch(identity[0]) else None
    except (OSError, ValueError, UnicodeError):
        return None


def _dashboard_check_rows(results: list[CheckResult]) -> list[dict[str, str]]:
    rows = []
    seen = set()
    for result in results:
        name = _DASHBOARD_CHECK_ALIASES.get(result.name, result.name)
        if name not in _DASHBOARD_CHECK_NAMES:
            continue
        if name in seen:
            return []  # Ambiguous evidence must not produce a healthy card.
        seen.add(name)
        status = "FAIL"
        if result.ok:
            status = "SKIPPED_BY_POLICY" if result.skipped else "WARN" if result.warning else "PASS"
        rows.append({"name": name, "status": status})
    return rows


def _write_dashboard_snapshot(path: Path, payload: dict[str, object]) -> None:
    """Atomic replacement inside the dedicated directory; never follow links."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".host-status-" + os.urandom(8).hex() + ".tmp"
    try:
        os.fchmod(directory, 0o700)
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=directory,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
        finally:
            os.close(directory)


def _export_dashboard_snapshot(
    settings: Settings, results: list[CheckResult], started_at: int,
    completed_at: int, release_identity: tuple | None,
) -> None:
    """Export only typed public fields, independently of alert delivery state."""
    current_identity = _dashboard_release_identity(settings)
    release_ref = (
        release_identity[0]
        if release_identity is not None and release_identity == current_identity
        else ""
    )
    payload = {
        "version": 1,
        "producer": "ai-filter-ops-check",
        "started_at": started_at,
        "completed_at": completed_at,
        "release_ref": release_ref,
        "checks": _dashboard_check_rows(results),
    }
    database = settings.database_path or settings.root / "data" / "cache.db"
    try:
        _write_dashboard_snapshot(database.parent / "ops" / "host_status.json", payload)
    except (OSError, ValueError, TypeError):
        # Do not change existing health/notification decisions or expose paths.
        print("WARN dashboard snapshot: export failed", file=sys.stderr)


def _render(results: list[CheckResult], *, as_json: bool, quiet: bool) -> None:
    failing = any(not item.ok for item in results)
    warning = any(item.warning for item in results)
    if quiet and not failing and not warning:
        return
    if as_json:
        print(
            json.dumps(
                {
                    "ok": not failing,
                    "checks": [item.public() for item in results],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return
    for item in results:
        state = "OK" if item.ok else "FAIL"
        if item.warning and item.ok:
            state = "WARN"
        if item.skipped:
            state = "SKIP"
        print(f"{state} {item.name}: {item.detail}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only AI-Filter host checks")
    parser.add_argument("--config", default=os.environ.get("OPS_CONFIG_FILE", "/etc/ai-filter/ops-monitor.env"))
    parser.add_argument("--backup-config", default=os.environ.get("SYSTEM_BACKUP_CONFIG_FILE", "/etc/ai-filter/backup.env"))
    parser.add_argument("--root")
    parser.add_argument("--backup-dir")
    parser.add_argument("--backup-max-age", type=int)
    parser.add_argument("--state-file")
    parser.add_argument("--cooldown", type=int)
    parser.add_argument("--now", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _load_configuration_files((Path(args.config), Path(args.backup_config)))
        settings = settings_from_env(args)
    except ValueError as exc:
        print(f"configuration error: {_safe_detail(exc)}", file=sys.stderr)
        return 2

    now = int(args.now if args.now is not None else time.time())
    if now <= 0:
        print("configuration error: invalid clock", file=sys.stderr)
        return 2

    if args.dry_run:
        results = evaluate(settings, now)
        _render(results, as_json=args.json, quiet=False)
        return 0 if all(item.ok for item in results) else 1

    settings.state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(settings.state_file.parent, 0o700)
    lock_path = Path(str(settings.state_file) + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0

        previous = _read_state(settings.state_file)
        settings = dataclasses.replace(
            settings,
            persist_verification_state=True,
        )
        dashboard_identity = _dashboard_release_identity(settings)
        results = evaluate(settings, now)
        _export_dashboard_snapshot(
            settings, results, now, int(time.time()), dashboard_identity
        )
        decision = notification_decision(
            previous, results, now, settings.cooldown_seconds
        )
        notice_attempted = False
        notice_sent = False
        notice_result = "not_due"
        if decision:
            notice_attempted = True
            notice_sent, notice_result = send_telegram(
                settings, _notification_text(decision, results, now)
            )
            if not notice_sent:
                print(f"WARN notification: {notice_result}", file=sys.stderr)

        failing = any(not item.ok for item in results)
        state = {
            "version": 2,
            "backup_policy_epoch": _BACKUP_POLICY_EPOCH,
            "status": "failing" if failing else "healthy",
            "fingerprint": _fingerprint(results),
            "last_run_at": now,
            "last_notice_at": now
            if notice_sent
            else int(previous.get("last_notice_at") or 0),
            "notification": notice_result,
            "pending_notification": decision
            if notice_attempted and not notice_sent
            else "",
            "warnings": sorted(item.name for item in results if item.warning),
        }
        _write_state(settings.state_file, state)
        _render(results, as_json=args.json, quiet=args.quiet)
        return 1 if failing else 0


if __name__ == "__main__":
    raise SystemExit(main())
