"""Atomic market-close run artifacts for host ops-check.

These files are the business-result contract. systemd success and four
index rows are not sufficient to mark a trading-day close healthy.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

RESULT_VERSION = 1
VALID_KINDS = frozenset({"collector", "repair", "history"})
VALID_STATUSES = frozenset(
    {
        "success",
        "complete",
        "incomplete",
        "error",
        "skipped_maintenance",
        "skipped_nontrading",
    }
)
_ALLOWED_FIELDS = (
    "version",
    "kind",
    "run_id",
    "trade_date",
    "status",
    "started_at",
    "completed_at",
    "due_total",
    "resolved",
    "unresolved",
    "truncated",
    "written",
    "remaining_total",
    "exit_code",
    "detail",
    "scope",
)


def result_dir_from_env(*, db_path: Path | None = None) -> Path:
    override = os.environ.get("MARKET_CLOSE_RESULT_DIR", "").strip()
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise ValueError("MARKET_CLOSE_RESULT_DIR must be absolute")
        return path
    if db_path is None:
        raise ValueError("database path is required without MARKET_CLOSE_RESULT_DIR")
    return Path(db_path).resolve().parent / "market_close"


def result_path(directory: Path, kind: str) -> Path:
    if kind not in VALID_KINDS:
        raise ValueError("invalid market-close result kind")
    return Path(directory) / f"{kind}_last.json"


def new_run_id() -> str:
    return uuid.uuid4().hex


def write_result(directory: Path, **fields: Any) -> Path:
    kind = str(fields.get("kind") or "")
    status = str(fields.get("status") or "")
    if kind not in VALID_KINDS:
        raise ValueError("invalid market-close result kind")
    if status not in VALID_STATUSES:
        raise ValueError("invalid market-close result status")
    inherited_run = os.environ.get("MARKET_CLOSE_RUN_ID", "") if kind == "collector" else ""
    payload = {
        "version": RESULT_VERSION,
        "run_id": str(fields.get("run_id") or inherited_run or new_run_id()),
    }
    for key in _ALLOWED_FIELDS:
        if key in {"version", "run_id"}:
            continue
        if key in fields:
            payload[key] = fields[key]
    payload["kind"] = kind
    payload["status"] = status
    path = result_path(directory, kind)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(encoded + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    return path


def persist_host_fallback(
    directory: Path,
    *,
    exit_code: int,
    output: str,
    started_at: int,
    run_id: str,
    previous_result: dict[str, Any] | None = None,
) -> Path | None:
    """Accept only this invocation's receipt, never a recent file's mtime."""
    if not run_id:
        raise ValueError("collector run_id is required")
    path = result_path(directory, "collector")
    try:
        payload = read_result(path)
        if (
            payload.get("kind") == "collector"
            and payload.get("run_id") == run_id
            and payload.get("detail") != "collector attempt has not completed"
            and payload.get("status") in {"success", "incomplete", "error"}
            and not (int(exit_code) != 0 and payload.get("status") == "success")
        ):
            return None
    except (OSError, ValueError, TypeError):
        pass
    if int(exit_code) == 0 and "非交易日，跳过采集" in output:
        if previous_result is not None:
            write_result(directory, **previous_result)
        else:
            write_result(directory, kind="collector", status="skipped_nontrading",
                         run_id=run_id, started_at=started_at, completed_at=int(time.time()))
        return None
    if int(exit_code) == 0:
        status = "success"
    elif "待结算覆盖未完成" in output:
        status = "incomplete"
    else:
        status = "error"
    trade_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    return write_result(
        directory,
        kind="collector",
        run_id=run_id,
        status=status,
        trade_date=trade_date,
        started_at=int(started_at),
        completed_at=int(time.time()),
        exit_code=int(exit_code),
        detail="host-fallback",
    )


def read_result(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or int(payload.get("version") or 0) != RESULT_VERSION:
        raise ValueError("market-close result malformed")
    kind = str(payload.get("kind") or "")
    status = str(payload.get("status") or "")
    if kind not in VALID_KINDS or status not in VALID_STATUSES:
        raise ValueError("market-close result malformed")
    return payload
