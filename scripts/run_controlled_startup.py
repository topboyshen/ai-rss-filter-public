"""Fixed isolation-only initialization runner: no Docker, SSH, network or model.

Production paths/scopes are rejected. A permit binding is not runtime image
inspection. Never automatically retries an initializer after a durable intent.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))


def execute(args):
    from services import admission_fence as gate
    from services import controlled_startup as startup

    gate._require(args.scope == "isolation", "production startup execution is not implemented/authorized")
    binding, permit = gate._read(args.binding), gate._read(args.permit)
    fence = gate.Fence(args.root, binding)
    startup._validate(fence, permit)
    os.environ["AI_FILTER_DB_PATH"] = permit["db_path"]
    if args.action == "authorize":
        result = startup.authorize(fence, permit)
    elif args.action == "migrate":
        result = startup.migrate(fence, permit["operation_id"])
    else:
        result = startup.query_migration(fence, permit["operation_id"])
    return {"status": "isolation_action_returned", "action": args.action,
            "operation_id": permit["operation_id"], "result": result,
            "runtime_inspect_verified": False, "business_open": False, "switch_authorized": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("authorize", "migrate", "query"))
    parser.add_argument("--scope", required=True)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--permit", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(execute(args), sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps({"status": "not_confirmed", "error_type": type(error).__name__,
                          "initializer_retried": False, "business_open": False, "switch_authorized": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
