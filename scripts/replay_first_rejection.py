#!/usr/bin/env python3
"""Offline replay of stored first-rejection rows. No model call, no network, no write.

For each row the recorded input (title + source text) and the recorded raw model
output are fed to the validator of a chosen code tree:

* ``--code-root DIR``   an explicit checkout (default: this repository);
* ``--git-repo DIR``    extract the commit recorded in the row's ``run_revision``
                        (only when it is a commit SHA) and replay under it.

The row records the SHA-256 of every file the validator depends on; the report
says whether the chosen tree has identical files (``validator_same``).  Only a
verdict difference under an identical validator is a mismatch; a difference under
a different validator is reported as drift, never as a misjudgment.

Exit status (highest applicable wins; the summary always lists every count):

  2  execution failure: unreadable database, or at least one row whose replay could
     not run (status ``error``) -- the result is incomplete, so it outranks the rest
  1  at least one MISMATCH_SAME_VALIDATOR (identical validator, different verdict)
  3  at least one ``not_replayable`` row (and no error or mismatch)
  0  no error, no same-validator mismatch and no not-replayable row.  This INCLUDES
     ``differs_other_validator`` (verdict drift under a different validator version)
     and an empty table, so 0 alone does NOT prove every historical verdict was
     reproduced: check each row's ``status`` (only ``match`` with
     ``validator_same`` is a reproduction under the recorded validator).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULT_MARK = "@@REPLAY@@"
_SNIPPET = r"""
import hashlib, json, sys
from pathlib import Path
payload = json.loads(sys.stdin.read())
root = Path(sys.argv[1])
files = {}
for rel in payload["files"]:
    path = root / rel
    files[rel] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
import core.scorer as scorer_module
scorer_module.record_score_validation = lambda **kwargs: None  # never write telemetry
scorer = scorer_module.Scorer()
backend = type("Backend", (), {"name": "replay", "model": "replay"})()


def judge(raw, attempt, trigger):
    parsed, reason, _detail = scorer._validate_scoring_attempt(
        raw, payload["title"], payload["source"], attempt, trigger,
        payload["stage"], backend, {"scoring_strategy_version": ""})
    kind = ""
    if parsed[0] >= 0:
        kind = scorer_module._score_grounding_verdict(
            parsed, payload["title"], payload["source"])[1]
    return list(parsed), reason, kind


parsed, reason, kind = judge(payload["raw"], "initial", "")
later = [
    dict(zip(("parsed", "reason", "kind"), judge(item["raw"], item["attempt"], item["trigger"])))
    for item in payload["later"]
]
print("@@REPLAY@@" + json.dumps(
    {"parsed": parsed, "reason": reason, "kind": kind, "files": files, "later": later},
    ensure_ascii=False))
"""
_SHA = re.compile(r"^[0-9a-f]{7,40}$")


def read_rows(db: str, ids: Optional[Iterable[int]] = None, limit: int = 0) -> list[dict]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sql, args = "SELECT * FROM first_rejection_evidence", []
        ids = list(ids or [])
        if ids:
            sql += f" WHERE id IN ({','.join('?' for _ in ids)})"
            args = ids
        sql += " ORDER BY id" + (f" LIMIT {int(limit)}" if limit else "")
        return [dict(row) for row in conn.execute(sql, args)]
    finally:
        conn.close()


def extract_revision(git_repo: str, revision: str, into: Path) -> Optional[Path]:
    """git archive <revision> into ``into``; None when it is not a usable commit SHA."""
    if not _SHA.match(revision or ""):
        return None
    target = into / revision
    if target.exists():
        return target
    target.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "-C", git_repo, "archive", revision], capture_output=True, check=False
    )
    if archive.returncode != 0:
        return None
    untar = subprocess.run(
        ["tar", "-x", "-C", str(target)], input=archive.stdout, capture_output=True,
        check=False,
    )
    return target if untar.returncode == 0 else None


def replayable_later(row: dict) -> list[dict]:
    """Later attempts whose raw output was stored whole can be re-judged too."""
    return [
        {"attempt": a["attempt"], "trigger": a["repair_trigger"], "raw": a["raw_response"]}
        for a in json.loads(row["later_attempts"])
        if not a["raw_truncated"]
    ]


def run_validator(root: Path, row: dict) -> dict:
    """Run the validator of ``root`` in a clean subprocess (no inherited secrets)."""
    scratch = tempfile.mkdtemp(prefix="replay-first-rejection-")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "AI_FILTER_DB_PATH": os.path.join(scratch, "unused.db"),
    }
    payload = json.dumps(
        {
            "raw": row["rejected_raw"],
            "title": row["input_title"],
            "source": row["input_source"],
            "files": sorted(json.loads(row["validator_files"])),
            "stage": row["stage"],
            "later": replayable_later(row),
        }
    )
    done = subprocess.run(
        [sys.executable, "-c", _SNIPPET, str(root)], input=payload, text=True,
        capture_output=True, cwd=str(root), env=env, timeout=120, check=False,
    )
    for line in done.stdout.splitlines():
        if line.startswith(RESULT_MARK):
            return json.loads(line[len(RESULT_MARK):])
    raise RuntimeError("validator replay produced no result")


def classify(row: dict, replay: dict) -> dict:
    recorded_files = json.loads(row["validator_files"])
    same_validator = bool(recorded_files) and replay["files"] == recorded_files
    same_verdict = (
        replay["reason"] == row["failure_reason"]
        and replay["kind"] == (row["fact_failure_kind"] or "")
    )
    recorded_parsed = json.loads(row["rejected_parsed"])
    same_parsed = replay["parsed"] == recorded_parsed
    # Later attempts (repair / second backend) judged by the same code, where stored whole.
    recorded_later = [a for a in json.loads(row["later_attempts"]) if not a["raw_truncated"]]
    later_equal = len(recorded_later) == len(replay["later"]) and all(
        (got["reason"] == (want["failure_reason"] or "")) and got["parsed"] == want["parsed"]
        for want, got in zip(recorded_later, replay["later"])
    )
    same_parsed = same_parsed and later_equal
    if same_validator:
        status = "match" if same_verdict and same_parsed else "MISMATCH_SAME_VALIDATOR"
    else:
        status = "match_other_validator" if same_verdict and same_parsed else "differs_other_validator"
    return {
        "status": status,
        "validator_same": same_validator,
        "recorded": {"reason": row["failure_reason"], "kind": row["fact_failure_kind"]},
        "replayed": {"reason": replay["reason"], "kind": replay["kind"]},
        "parsed_equal": same_parsed,
        "later_attempts_rejudged": len(recorded_later),
    }


def replay_rows(rows: list[dict], *, code_root: Optional[Path], git_repo: Optional[str]) -> list[dict]:
    reports = []
    with tempfile.TemporaryDirectory(prefix="replay-revisions-") as tmp:
        for row in rows:
            base = {
                "id": row["id"],
                "stage": row["stage"],
                "run_revision": row["run_revision"],
                "validator_version": row["validator_version"],
            }
            if not row["replayable"]:
                reports.append({**base, "status": "not_replayable",
                                "replay_limit": row["replay_limit"]})
                continue
            root, root_note = code_root or REPO_ROOT, "code-root" if code_root else "this-repo"
            if code_root is None and git_repo:
                extracted = extract_revision(git_repo, row["run_revision"], Path(tmp))
                if extracted is not None:
                    root, root_note = extracted, "git-revision"
                else:
                    root_note = "revision-not-a-commit-using-this-repo"
            try:
                result = classify(row, run_validator(root, row))
            except (RuntimeError, OSError, subprocess.SubprocessError, ValueError,
                    KeyError, TypeError) as exc:
                result = {"status": "error", "error": type(exc).__name__}
            reports.append({**base, "code": root_note, **result})
    return reports


EXIT_OK, EXIT_MISMATCH, EXIT_ERROR, EXIT_NOT_REPLAYABLE = 0, 1, 2, 3

_HELP_EPILOG = """exit status (highest applicable wins):
  2  execution failure (unreadable database, or a row whose replay could not run)
  1  at least one MISMATCH_SAME_VALIDATOR
  3  at least one not_replayable row (no error, no mismatch)
  0  no error, no same-validator mismatch, no not-replayable row

Exit 0 alone does NOT prove all historical verdicts reproduced: it also covers
differs_other_validator (verdict drift under a different validator version) and an
empty table.  Check each row's status (and validator_same); only 'match' is a
reproduction under the recorded validator.
"""


def exit_code_for(reports: list[dict]) -> int:
    """Precedence: execution failure > same-validator mismatch > not replayable > ok."""
    statuses = {r["status"] for r in reports}
    if "error" in statuses:
        return EXIT_ERROR
    if "MISMATCH_SAME_VALIDATOR" in statuses:
        return EXIT_MISMATCH
    if "not_replayable" in statuses:
        return EXIT_NOT_REPLAYABLE
    return EXIT_OK


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", required=True, help="SQLite file (opened read-only)")
    parser.add_argument("--id", type=int, action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--code-root", type=Path)
    parser.add_argument("--git-repo")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        rows = read_rows(args.db, args.id, args.limit)
        reports = replay_rows(rows, code_root=args.code_root, git_repo=args.git_repo)
    except (sqlite3.Error, OSError) as exc:
        print(f"replay failed: {type(exc).__name__}", file=sys.stderr)
        return EXIT_ERROR
    code = exit_code_for(reports)
    counts: dict[str, int] = {}
    for item in reports:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    if args.json:
        print(json.dumps({"exit_code": code, "summary": counts, "rows": reports},
                         ensure_ascii=False, indent=1))
    else:
        for item in reports:
            print(" ".join(f"{k}={v}" for k, v in item.items()))
        print("summary", json.dumps(counts, sort_keys=True), "exit_code", code)
    return code


if __name__ == "__main__":
    sys.exit(main())
