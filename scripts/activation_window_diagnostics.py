"""Bounded, redacted failure record for the step-9 watchdog prepare/start/ready window.

Written atomically into the gateway switch evidence directory so the existing
``gateway_command_stderr`` export (capped + redacted) carries it. Diagnostic only:
no timeout, retry or control-flow change.
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
from pathlib import Path

from services import admission_fence as gate

RECORD_NAME = "watchdog-window.stderr"
RECORD_LIMIT = 8192
TEXT_LIMIT = 256
# Configured values observed in code (documentation only; nothing here changes them).
CONFIGURED_SECONDS = {"native_transport_client": 2, "native_bridge_request": 3, "native_bridge_command_cap": 2}
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


def record(evidence_root, sub_stage, started, error, *, harness_output=None, child=None):
    """Atomically write <=8 KiB; never raises (the original error must surface)."""
    try:
        root = Path(evidence_root)
        output = Path(harness_output) if harness_output is not None else None
        value = {"schema": 1, "sub_stage": sub_stage, "endpoints": ENDPOINTS.get(sub_stage, []),
                 "configured_seconds": CONFIGURED_SECONDS, "elapsed_seconds": round(time.monotonic() - started, 3),
                 "error_type": type(error).__name__, "error_message": _redact(error),
                 "watchdog_child_alive": None if child is None else child.poll() is None,
                 "bridge": _bridge(output / "native-control-bridge") if output else None,
                 "last_harness_command": _last_command(output) if output else None}
        raw = (json.dumps(value, sort_keys=True) + "\n").encode()[:RECORD_LIMIT]
        temporary, target = root / ("." + RECORD_NAME + ".tmp"), root / RECORD_NAME
        temporary.unlink(missing_ok=True)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        gate._sync_directory(root)
        return target
    except Exception:  # noqa: BLE001 - diagnostics must never mask the real failure
        return None
