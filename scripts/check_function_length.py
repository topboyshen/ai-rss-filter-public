#!/usr/bin/env python3
"""Fail CI when Python functions grow beyond the agreed length budget.

Legacy long functions are pinned by a repository-relative allowlist so they
cannot grow further. Shortening one requires lowering its allowance in the
same change; fitting the budget requires removing the stale allowance.
New long functions fail immediately.
"""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FunctionLength:
    path: str
    name: str
    lineno: int
    length: int

    @property
    def key(self) -> str:
        return f"{self.path}:{self.name}:{self.lineno}"


def tracked_python_files(root: Path) -> list[str]:
    output = subprocess.check_output(
        ["git", "ls-files", "*.py"], cwd=root, text=True
    )
    return [line for line in output.splitlines() if line.strip()]


def iter_function_lengths(
    path: Path, *, root: Path | None = None
) -> list[FunctionLength]:
    text = path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(text, filename=str(path))
    found: list[FunctionLength] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            found.append(
                FunctionLength(
                    path=(path.relative_to(root) if root is not None else path).as_posix(),
                    name=node.name,
                    lineno=node.lineno,
                    length=end - node.lineno + 1,
                )
            )
    return found


def load_allowlist(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"allowlist must be a JSON object: {path}")
    return {str(k): int(v) for k, v in payload.items()}


def evaluate(
    lengths: list[FunctionLength], *, max_lines: int, allowlist: dict[str, int]
) -> list[str]:
    errors: list[str] = []
    for item in sorted(lengths, key=lambda x: (x.path, x.lineno)):
        baseline = allowlist.get(f"{item.path}:{item.name}")
        if item.length <= max_lines:
            if baseline is not None:
                errors.append(
                    f"STALE_ALLOWLIST {item.path}:{item.name}:{item.lineno} "
                    f"now {item.length} <= {max_lines}; remove the allowlist entry"
                )
            continue
        if baseline is None:
            errors.append(
                f"NEW_LONG_FUNCTION {item.path}:{item.name}:{item.lineno} "
                f"{item.length} > {max_lines}"
            )
        elif item.length > baseline:
            errors.append(
                f"LONG_FUNCTION_GREW {item.path}:{item.name}:{item.lineno} "
                f"{item.length} > allowlisted {baseline}"
            )
        elif item.length < baseline:
            errors.append(
                f"ALLOWLIST_NOT_TIGHTENED {item.path}:{item.name}:{item.lineno} "
                f"now {item.length} < allowlisted {baseline}; "
                f"lower the allowance to {item.length}"
            )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--max", type=int, default=120, dest="max_lines")
    parser.add_argument("--allowlist", default="ops/function_length_allowlist.json")
    parser.add_argument("--write-baseline", default="")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    lengths: list[FunctionLength] = []
    for rel in tracked_python_files(root):
        path = root / rel
        if path.is_file():
            lengths.extend(iter_function_lengths(path, root=root))

    if args.write_baseline:
        baseline = {
            f"{item.path}:{item.name}": item.length
            for item in lengths
            if item.length > args.max_lines
        }
        Path(args.write_baseline).write_text(
            json.dumps(baseline, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {len(baseline)} baseline entries to {args.write_baseline}")
        return 0

    allowlist = load_allowlist((root / args.allowlist).resolve())
    errors = evaluate(lengths, max_lines=args.max_lines, allowlist=allowlist)
    if errors:
        for error in errors:
            print(error)
        return 1
    print(f"function length gate OK: max={args.max_lines} functions={len(lengths)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
