"""Bounded, redacted failure record for the step-9 watchdog prepare/start/ready window.

Written atomically into the gateway switch evidence directory so the existing
``gateway_command_stderr`` export (capped + redacted) carries it. Diagnostic only:
no timeout, retry or control-flow change.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import time
from contextlib import contextmanager
from pathlib import Path

from services import admission_fence as gate

RECORD_NAME = "watchdog-window.stderr"
RECORD_LIMIT = 8192
TEXT_LIMIT = 256
RESULT_FIELDS = ("action", "state", "phase", "generation", "previous_generation", "durable", "confirmed",
                 "active_leases", "uncertain_leases")


def configured_seconds():
    """Read (never set) the live constants so the record cannot drift from the code."""
    try:
        return _configured_seconds()
    except Exception:  # noqa: BLE001 - diagnostics only
        return None


def _configured_seconds():
    from scripts import production_sender_native_bridge as bridge
    from production_sender_driver import NativeSenderTransport
    return {"native_transport_client_default": NativeSenderTransport.DEFAULT_TIMEOUT_SECONDS,
            "native_bridge_client": bridge.CLIENT_SECONDS, "native_bridge_request": bridge.REQUEST_SECONDS,
            "native_bridge_command_cap": bridge.COMMAND_SECONDS}
ENDPOINTS = {"prepare": ["GET /activation-registration", "GET /operations/<operation>"],
             "start": [], "ready": []}
_SECRET = re.compile(r"(?i)(bearer\s+)\S+|([0-9a-f]{32,})|((?:token|secret|password|auth)[^=:\s]*[=:]\s*)\S+")


def _redact(text):
    return _SECRET.sub(lambda m: (m.group(1) or m.group(3) or "") + "<redacted>", str(text))[:TEXT_LIMIT]


def _port_listening(port):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.2):
            return True
    except (OSError, ValueError, TypeError):
        return False


def _bridge(root):
    """Hash-free summary of the synthetic bridge's own bounded records (no bodies, no tokens)."""
    out = {"present": root.is_dir()}
    if not out["present"]:
        return out
    intents = sorted(root.glob("request-*.intent.json"))
    out["requests"] = len(intents)
    out["results"] = len(list(root.glob("request-*.result.json")))
    for name, keys in (("STOP.json", ("request", "stage", "error_type")), ("START.json", ("port", "request_seconds"))):
        try:
            value = json.loads((root / name).read_text())
            out[name.split(".")[0].lower()] = {k: _redact(value.get(k)) for k in keys}
        except (OSError, ValueError, AttributeError):
            pass
    if intents:
        try:
            last = json.loads(intents[-1].read_text())
            out["last_intent"] = _redact(f"{last.get('method')} {last.get('route')}")
        except (OSError, ValueError, AttributeError):
            pass
    results = sorted(root.glob("request-*.result.json"))
    if results:
        try:
            last = json.loads(results[-1].read_text())
            out["last_result"] = {k: _redact(last.get(k)) for k in ("status", "elapsed_seconds", "error_category")}
        except (OSError, ValueError, AttributeError):
            pass
    port = out.get("start", {}).get("port")
    out["port_listening"] = _port_listening(port) if port not in (None, "None") else None
    return out


def _last_command(output):
    try:
        path = max(Path(output).glob("command-*.json"))
        value = json.loads(path.read_text())
        argv = value.get("argv") or []
        return {"name": path.name, "subcommand": _redact(argv[3] if len(argv) > 3 else ""),
                "rc": value.get("rc"), "elapsed_seconds": round(float(value.get("elapsed_seconds", 0)), 3)}
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _scalar(value):
    return value if type(value) in (bool, int, float) or value is None else _redact(value)[:64]


def control_exchange(control_root):
    """Summary of the adapter's own last retained private-control exchange (status + response shape)."""
    try:
        paths = [p for p in Path(control_root).iterdir() if re.fullmatch(r"(?:query|attempt)-\d{2}\.json", p.name)]
        if not paths:
            return None
        path = max(paths, key=lambda item: (item.stat().st_mtime_ns, item.name))
        value = json.loads(path.read_text())
        response = value.get("response")
        raw = json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
        out = {"name": path.name, "status": value.get("status"), "body_bytes": len(raw),
               "body_sha256": hashlib.sha256(raw).hexdigest(), "keys": None, "confirmed": None, "result": None}
        if isinstance(response, dict):
            out["keys"] = sorted(_redact(k)[:40] for k in response)[:16]
            if "confirmed" in response:
                out["confirmed"] = {"type": type(response["confirmed"]).__name__, "value": _scalar(response["confirmed"])}
            result = response.get("result")
            if isinstance(result, dict):
                out["result"] = {k: _scalar(result[k]) for k in RESULT_FIELDS if k in result}
            else:
                out["result"] = {"type": type(result).__name__}
        text = _redact(raw.decode(errors="replace"))
        out["body_text"] = text if len(raw) <= TEXT_LIMIT else None
        return out
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def record(evidence_root, sub_stage, started, error, *, harness_output=None, child=None, control_root=None,
           postgres=None):
    """Atomically write <=8 KiB; never raises (the original error must surface)."""
    try:
        root = Path(evidence_root)
        output = Path(harness_output) if harness_output is not None else None
        value = {"schema": 1, "sub_stage": sub_stage, "endpoints": ENDPOINTS.get(sub_stage, []),
                 "configured_seconds": configured_seconds(),
                 "control_exchange": control_exchange(control_root) if control_root is not None else None,
                 "postgres": postgres, "elapsed_seconds": round(time.monotonic() - started, 3),
                 "error_type": type(error).__name__, "error_message": _redact(error),
                 "watchdog_child_alive": None if child is None else child.poll() is None,
                 "bridge": _bridge(output / "native-control-bridge") if output else None,
                 "last_harness_command": _last_command(output) if output else None}
        return _write_record(root, RECORD_NAME, value)
    except Exception:  # noqa: BLE001 - diagnostics must never mask the real failure
        return None


def _write_record(root, name, value):
    """Atomic capped write into the exported gateway evidence directory."""
    # indent keeps every line far below the 4096-char per-line export redaction cap
    raw = (json.dumps(value, sort_keys=True, indent=1) + "\n").encode()[:RECORD_LIMIT]
    temporary, target = root / ("." + name + ".tmp"), root / name
    temporary.unlink(missing_ok=True)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    gate._sync_directory(root)
    return target


STOP_RECORD_NAME = "restrictive-stop.stderr"
STOP_CALLS = ("_stop_sender", "_stop_receiver")


@contextmanager
def observe_stop(lifetime, outcomes):
    """Time each restrictive sub-call and keep its redacted error text.

    The original callables run unchanged and every exception is re-raised, so
    _stop's own error capture and status stay exactly as before."""
    originals = {name: getattr(lifetime, name) for name in STOP_CALLS}

    def wrap(name, call):
        def observed(*args, **kwargs):
            started = time.monotonic()
            try:
                value = call(*args, **kwargs)
            except BaseException as error:
                outcomes[name] = {"ok": False, "elapsed_seconds": round(time.monotonic() - started, 3),
                                  "error_type": type(error).__name__, "error_message": _redact(error)}
                raise
            outcomes[name] = {"ok": True, "elapsed_seconds": round(time.monotonic() - started, 3)}
            return value
        return observed
    for name, call in originals.items():
        setattr(lifetime, name, wrap(name, call))
    try:
        yield outcomes
    finally:
        for name, call in originals.items():
            setattr(lifetime, name, call)


def _stop_result(value):
    if not isinstance(value, dict):
        return None
    errors = value.get("errors") if isinstance(value.get("errors"), dict) else {}
    return {"status": _scalar(value.get("status")), "drained": _scalar(value.get("drained")),
            "queue_checked": _scalar(value.get("queue_checked")),
            "parts": sorted(_redact(k)[:24] for k in value if k in ("sender", "receiver")),
            "errors": {_redact(k)[:24]: {"type": _scalar((e or {}).get("type")) if isinstance(e, dict) else None}
                       for k, e in list(errors.items())[:8]}}


def record_stop(evidence_root, sub_stage, started, error, *, outcomes, returned=None, lifecycle_root=None,
                child=None, closed_evidence_source=None, postgres=None):
    """Step-9 restrictive stop failure record; never raises."""
    try:
        written = None
        if lifecycle_root is not None and (Path(lifecycle_root) / "shutdown-result.json").is_file():
            written = json.loads((Path(lifecycle_root) / "shutdown-result.json").read_bytes()[:65536])
        value = {"schema": 1, "sub_stage": sub_stage, "elapsed_seconds": round(time.monotonic() - started, 3),
                 "error_type": type(error).__name__, "error_message": _redact(error),
                 "sub_calls": {k: {f: _scalar(v) for f, v in o.items()} for k, o in outcomes.items()},
                 "returned": _stop_result(returned), "shutdown_result_file": _stop_result(written),
                 "watchdog_child_alive": None if child is None else child.poll() is None,
                 "watchdog_child_returncode": None if child is None else _scalar(child.poll()),
                 "closed_evidence_source": _scalar(closed_evidence_source), "postgres": postgres}
        return _write_record(Path(evidence_root), STOP_RECORD_NAME, value)
    except Exception:  # noqa: BLE001 - diagnostics must never mask the real failure
        return None


SYNTHETIC_USER, SYNTHETIC_DB = "synthetic", "synthetic_pause"
PG_QUERIES = (
    ("state", "SELECT manifest, generation, phase, current_operation FROM handoff_state WHERE id=1 LIMIT 1"),
    ("leases", "SELECT status, source, count(*) FROM handoff_leases WHERE status<>'finished' "
               "GROUP BY 1, 2 ORDER BY 1, 2 LIMIT 16"),
    ("operation_counts", "SELECT status, count(*) FROM handoff_operations GROUP BY 1 ORDER BY 1 LIMIT 8"),
    ("operations", "SELECT action, status, previous_generation, generation, operation_id FROM handoff_operations "
                   "ORDER BY generation DESC, operation_id LIMIT 6"),
    # Catalog views only (no lock functions): who holds/awaits the Go handoff advisory keys.
    ("advisory_locks", "SELECT classid::bigint, objsubid, mode, granted, count(*), count(DISTINCT pid) FROM pg_locks "
                       "WHERE locktype='advisory' AND classid IN (20261023::oid, 20261024::oid) "
                       "GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4 LIMIT 16"),
    ("activity", "SELECT state, wait_event_type, wait_event, count(*) FROM pg_stat_activity "
                 "WHERE datname=current_database() AND pid<>pg_backend_pid() GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 16"))


def _short(value):
    return None if value is None else hashlib.sha256(str(value).encode()).hexdigest()[:12]


def synthetic_dsn(harness, p):
    """Only the CI harness's own synthetic PG: owned harness + fixed synthetic user/db on a private address."""
    import ipaddress
    from urllib.parse import urlsplit
    gate._require(type(harness).__name__ == "ActivationHarness"
                  and re.fullmatch(r"[0-9a-f]{32}", str(getattr(harness, "owner", "")))
                  and getattr(harness, "network", None) == "miniflux-pause-" + harness.owner[:12] + "-network",
                  "owned synthetic CI harness required")
    dsn = json.loads((Path(p["directory"]) / "postgres.json").read_text())["dsn"]
    value = urlsplit(dsn)
    gate._require(value.scheme == "postgres" and value.username == SYNTHETIC_USER and value.path == "/" + SYNTHETIC_DB
                  and value.port == 5432 and ipaddress.ip_address(value.hostname).is_private
                  and not ipaddress.ip_address(value.hostname).is_loopback, "synthetic CI PostgreSQL target required")
    return dsn


def _pg_rows(cursor):
    out = {}
    for name, sql in PG_QUERIES:
        cursor.execute(sql)
        out[name] = cursor.fetchall()
    state = out["state"][0] if out["state"] else None
    return {"state": None if state is None else {"manifest": _short(state[0]), "generation": state[1],
                                                 "phase": _redact(state[2])[:32], "current_operation": _short(state[3])},
            "leases_not_finished": [{"status": _redact(a)[:24], "source": _redact(b)[:32], "count": c}
                                    for a, b, c in out["leases"]],
            "operation_counts": {_redact(a)[:24]: c for a, c in out["operation_counts"]},
            "recent_operations": [{"action": _redact(a)[:24], "status": _redact(b)[:24], "previous_generation": c,
                                   "generation": d, "operation": _short(e)} for a, b, c, d, e in out["operations"]],
            **_pg_contention(out)}


def _pg_contention(out):
    return {"advisory_locks": [{"classid": a, "objsubid": b, "mode": _redact(c)[:24], "granted": d, "locks": e, "pids": f}
                               for a, b, c, d, e, f in out["advisory_locks"]],
            "activity": [{"state": _redact(a)[:24], "wait": _redact(f"{b}:{c}")[:48], "count": d}
                         for a, b, c, d in out["activity"]]}


def pg_snapshot(harness, p, *, connect=None):
    """READ ONLY, <=3 s, small LIMITs; never raises (diagnostics must not mask the original failure)."""
    started = time.monotonic()
    try:
        dsn = synthetic_dsn(harness, p)
        if connect is None:
            import psycopg
            connect = psycopg.connect
        with connect(dsn, connect_timeout=3, autocommit=False) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                cursor.execute("SET LOCAL statement_timeout='3000ms'")
                cursor.execute("SET LOCAL lock_timeout='1000ms'")
                value = _pg_rows(cursor)
            connection.rollback()
        value["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return value
    except Exception as error:  # noqa: BLE001
        return {"error_type": type(error).__name__, "error_message": _redact(error)[:128],
                "elapsed_seconds": round(time.monotonic() - started, 3)}
