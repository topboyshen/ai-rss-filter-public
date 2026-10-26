"""Explicit local, isolation-only application-fence operations; never a deployment.

Authorization/binding are operator intent, not Docker inspect proof. No SSH,
Docker, PostgreSQL, HTTP, model, cleanup, automatic retry or automatic reopen.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
ACTIONS = ("provision", "open-trial", "stop-new", "freeze", "query", "snapshot")
MAX_INPUT = 65536
MAX_OUTPUT = 1024 * 1024


class ControlRefused(RuntimeError):
    """Missing authority/binding; cannot claim any operation succeeded."""


def require(condition, text):
    if not condition:
        raise ControlRefused(text)


def _unique(pairs):
    result = {}
    for name, value in pairs:
        require(name not in result, "duplicate input key")
        result[name] = value
    return result


def private_input(path):
    path = Path(path)
    require(path.is_absolute(), "input path must be absolute")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and stat.S_IMODE(before.st_mode) == 0o600
                and before.st_uid == os.geteuid() and before.st_nlink == 1
                and 0 < before.st_size <= MAX_INPUT, "unsafe private input")
        raw = stream.read(before.st_size + 1)
        after = os.fstat(stream.fileno())
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    require(len(raw) == before.st_size and all(getattr(before, key) == getattr(after, key) for key in fields),
            "private input changed")
    value = json.loads(raw, object_pairs_hook=_unique,
                       parse_constant=lambda value: (_ for _ in ()).throw(ControlRefused("nonfinite JSON")))
    require(isinstance(value, dict), "private input must be an object")
    return value, hashlib.sha256(raw).hexdigest()


def core_module():
    name = "controlled_application_admission"
    spec = importlib.util.spec_from_file_location(name, PROJECT / "services/admission_fence.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def validate_request(args, now):
    require(args.action in ACTIONS, "unsupported action")
    root = Path(args.root)
    require(root.is_absolute() and str(root.resolve()) == str(root), "root must be an absolute canonical path")
    require(root.name not in {"", ".", ".."} and root.parent != root, "invalid root")
    require(type(args.expected_generation) is int and 0 <= args.expected_generation < 2**63, "invalid generation")
    require(type(args.wait_seconds) in (int, float) and math.isfinite(args.wait_seconds)
            and 0 <= args.wait_seconds <= 5, "wait limit must be within five seconds")
    require(isinstance(args.reason, str) and 0 < len(args.reason) <= 200, "invalid operation reason")
    operation_required = args.action not in {"provision", "snapshot"}
    require((isinstance(args.operation_id, str) and re.fullmatch(r"[0-9a-f]{64}", args.operation_id))
            if operation_required else args.operation_id is None, "operation identity required or forbidden")
    binding, binding_hash = private_input(args.binding)
    require((args.policy is not None) == (args.action == "provision"), "policy is only accepted for provision")
    policy, policy_hash = private_input(args.policy) if args.policy is not None else (None, None)
    auth, _ = private_input(args.authorization)
    expected = {"schema": 1, "scope": "isolation", "action": args.action, "root": str(root),
                "binding_sha256": binding_hash, "policy_sha256": policy_hash,
                "operation_id": args.operation_id, "expected_generation": args.expected_generation,
                "reason": args.reason}
    require(type(auth.get("schema")) is int and type(auth.get("expected_generation")) is int,
            "authorization integer types invalid")
    require(set(auth) == set(expected) | {"not_before", "expires_at"}
            and all(auth.get(key) == value for key, value in expected.items()), "authorization binding differs")
    start, end = auth["not_before"], auth["expires_at"]
    require(type(start) is int and type(end) is int and 0 < end - start <= 7200
            and type(now) in (int, float) and math.isfinite(now) and start <= now < end,
            "authorization not active")
    return root, binding, policy, auth


def execute(args, *, now=None):
    """One explicit action. Core ACK is returned unchanged, never upgraded to switch proof."""
    root, binding, policy, auth = validate_request(args, time.time() if now is None else now)
    module = core_module()
    if args.action == "provision":
        result = module.provision(root, binding, policy)
    else:
        fence = module.Fence(root, binding, lock_seconds=min(1.0, args.wait_seconds))
        if args.action == "open-trial":
            result = fence.open_trial(args.operation_id, args.expected_generation, args.reason)
        elif args.action == "stop-new":
            result = fence.stop_new(args.operation_id, args.expected_generation, args.reason)
        elif args.action == "freeze":
            result = fence.freeze(args.operation_id, args.expected_generation, args.reason,
                                  wait_seconds=args.wait_seconds)
        elif args.action == "query":
            result = fence.query_operation(args.operation_id, wait_seconds=args.wait_seconds)
        else:
            result = fence.snapshot()
    response = {"status": "local_action_returned", "scope": "isolation", "action": args.action,
                "operation_id": args.operation_id, "authorization": auth,
                "runtime_inspect_verified": False, "queue_checked": False,
                "switch_authorized": False, "result": result}
    require(len(json.dumps(response, ensure_ascii=True, allow_nan=False).encode()) <= MAX_OUTPUT,
            "result exceeds safe output bound")
    return response


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--authorization", required=True, type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--operation-id")
    parser.add_argument("--expected-generation", type=int, default=0)
    parser.add_argument("--reason", default="isolated_operator_request")
    parser.add_argument("--wait-seconds", type=float, default=1.0)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(execute(args), sort_keys=True, ensure_ascii=True, allow_nan=False))
        return 0
    except Exception as error:
        # Unknown means preserve and query exact original op; never retry here.
        print(json.dumps({"status": "not_confirmed", "action": args.action,
                          "operation_id": args.operation_id, "error_type": type(error).__name__,
                          "switch_authorized": False, "retry_performed": False}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
