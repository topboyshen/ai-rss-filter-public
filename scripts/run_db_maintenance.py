#!/usr/bin/env python3
"""Log the existing weekly SQLite maintenance, including transport failures.

The host streams this file to the existing container, so no image update is
needed. GNU timeout still bounds the Docker client, not the container process.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


PHASES = ("integrity_check", "vacuum", "close")


def log(run_id: str, scope: str, event: str, **fields) -> None:
    print(
        json.dumps(
            {
                "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "task": "db_maintenance",
                "run_id": run_id,
                "scope": scope,
                "event": event,
                **fields,
            },
            ensure_ascii=True,
        ),
        flush=True,
    )


def failure_code(exc: BaseException) -> int:
    if isinstance(exc, KeyboardInterrupt):
        return 130
    if isinstance(exc, SystemExit) and isinstance(exc.code, int) and exc.code:
        return exc.code if 0 < exc.code < 256 else 1
    return 1


def database_maintenance(path: str, run_id: str) -> int:
    # Only the in-container database mode imports the application. The host
    # transport stays standalone; an old installed host copy is NOT upgraded
    # by a new image and remains an explicit cutover installation precondition.
    # Preserve this runner's standalone/explicit-test-database contract when
    # disabled. Enforced/unknown mode must not silently bypass missing app code.
    if os.environ.get("AI_FILTER_ADMISSION_MODE", "disabled") == "disabled":
        return _database_maintenance_admitted(path, run_id)
    from services.admission_fence import admission

    with admission("database_maintenance", kind="intake"):
        return _database_maintenance_admitted(path, run_id)


def _database_maintenance_admitted(path: str, run_id: str) -> int:
    connection = None
    statuses = dict.fromkeys(PHASES, "skipped")
    phase = "connect"
    rc = 1
    log(run_id, "database", "start")
    try:
        log(run_id, "database", "phase_start", phase=phase)
        # Preserve sqlite3.connect's existing default lock timeout and settings.
        connection = sqlite3.connect(path)
        log(run_id, "database", "phase_end", phase=phase, status="success")
        phase = "integrity_check"
        log(run_id, "database", "phase_start", phase=phase)
        rows = connection.execute("PRAGMA integrity_check").fetchall()
        if rows != [("ok",)]:
            statuses[phase] = "failed"
            log(
                run_id,
                "database",
                "phase_end",
                phase=phase,
                status="failed",
                reason="non_ok_result",
                result_count=len(rows),
                results=rows[:10],
            )
        else:
            statuses[phase] = "success"
            log(run_id, "database", "phase_end", phase=phase, status="success")
            phase = "vacuum"
            log(run_id, "database", "phase_start", phase=phase)
            connection.execute("VACUUM")
            statuses[phase] = "success"
            log(run_id, "database", "phase_end", phase=phase, status="success")
            rc = 0
    except BaseException as exc:
        rc = failure_code(exc)
        if phase in statuses:
            statuses[phase] = "failed"
        log(
            run_id,
            "database",
            "phase_end",
            phase=phase,
            status="failed",
            error_type=type(exc).__name__,
            error=str(exc)[:400],
        )
    finally:
        for skipped in ("integrity_check", "vacuum"):
            if statuses[skipped] == "skipped":
                log(
                    run_id,
                    "database",
                    "phase_end",
                    phase=skipped,
                    status="skipped",
                    reason=f"{phase}_failed",
                )
        if connection is not None:
            log(run_id, "database", "phase_start", phase="close")
            try:
                connection.close()
                statuses["close"] = "success"
                log(run_id, "database", "phase_end", phase="close", status="success")
            except BaseException as exc:
                rc = rc or failure_code(exc)
                statuses["close"] = "failed"
                log(
                    run_id,
                    "database",
                    "phase_end",
                    phase="close",
                    status="failed",
                    error_type=type(exc).__name__,
                    error=str(exc)[:400],
                )
        else:
            log(
                run_id,
                "database",
                "phase_end",
                phase="close",
                status="skipped",
                reason="connection_not_opened",
            )
        log(run_id, "database", "finish", rc=rc, **statuses)
    return rc


def observe_phase(line: str, run_id: str, statuses: dict) -> int | None:
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    if record.get("run_id") != run_id or record.get("scope") != "database":
        return None
    phase = record.get("phase")
    status = record.get("status")
    if record.get("event") == "phase_end" and phase in statuses:
        if status in {"success", "failed", "skipped"}:
            statuses[phase] = status
    if record.get("event") == "finish" and type(record.get("rc")) is int:
        return record["rc"]
    return None


def host_maintenance(run_id: str) -> int:
    statuses = dict.fromkeys(PHASES, "unknown")
    database_rc = None
    rc = 69
    transport_rc = None
    launched = False
    outcome = "launch_failed"
    log(run_id, "host", "start", timeout_seconds=3600, kill_after_seconds=60)
    command = [
        "timeout",
        "--signal=TERM",
        "--kill-after=60s",
        "60m",
        "docker",
        "exec",
        "-i",
        "ai-rss-filter-ai-filter-api-1",
        "python",
        "-u",
        "-",
        "--run-id",
        run_id,
    ]
    try:
        with Path(__file__).open("rb") as payload:
            with subprocess.Popen(
                command,
                stdin=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
            ) as process:
                launched = True
                for line in process.stdout:
                    print(line, end="", flush=True)
                    observed_rc = observe_phase(line, run_id, statuses)
                    if observed_rc is not None:
                        database_rc = observed_rc
                rc = process.wait()
        rc = 128 - rc if rc < 0 else rc
        transport_rc = rc
        outcome = {0: "success", 124: "timeout", 137: "killed"}.get(rc, "failed")
        if rc == 0 and (database_rc != 0 or set(statuses.values()) != {"success"}):
            rc, outcome = 1, "missing_or_invalid_database_result"
    except OSError as exc:
        rc = 69
        log(
            run_id, "host", "error", error_type=type(exc).__name__, error=str(exc)[:400]
        )
        if not launched:
            statuses = dict.fromkeys(PHASES, "skipped")
        else:
            outcome = "transport_error"
    log(
        run_id,
        "host",
        "finish",
        rc=rc,
        outcome=outcome,
        transport_rc=transport_rc,
        database_result_rc=database_rc,
        container_exit="confirmed" if rc == 0 else "unknown",
        **statuses,
    )
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--host", action="store_true")
    mode.add_argument("--db", default="/app/data/cache.db")
    parser.add_argument("--run-id", default=uuid.uuid4().hex)
    args = parser.parse_args(argv)
    if args.host:
        return host_maintenance(args.run_id)
    return database_maintenance(args.db, args.run_id)


if __name__ == "__main__":
    sys.exit(main())
