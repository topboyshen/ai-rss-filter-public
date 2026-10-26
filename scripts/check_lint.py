"""Reject new Ruff findings without hiding or automatically fixing old debt."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / ".ci" / "ruff-baseline.json"
LINT_REQUIREMENTS = ROOT / "requirements-lint.txt"
_RUFF_PIN = re.compile(r"^ruff==(\d+\.\d+\.\d+)\s*(?:#.*)?$")


def pinned_ruff_version(requirements: Path = LINT_REQUIREMENTS) -> str:
    """Return the single exact Ruff pin; requirements-lint.txt is the source."""
    pins = []
    for raw in requirements.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"ruff\b", line, re.IGNORECASE):
            match = _RUFF_PIN.match(line)
            if match is None:
                raise ValueError(
                    f"Ruff must be pinned exactly as ruff==X.Y.Z: {line!r}"
                )
            pins.append(match.group(1))
    if len(pins) != 1:
        raise ValueError("requirements-lint.txt must pin Ruff exactly once")
    return pins[0]


def installed_ruff_version() -> str:
    output = subprocess.check_output(
        [sys.executable, "-m", "ruff", "--version"], text=True, timeout=30
    )
    match = re.fullmatch(r"ruff (\S+)\s*", output)
    if match is None:
        raise ValueError(f"unrecognized Ruff version output: {output!r}")
    return match.group(1)


def finding_counts(findings: list[dict], root: Path) -> Counter:
    """Ignore line movement, but preserve each path, rule, message and count."""
    return Counter(
        (
            Path(item["filename"]).resolve().relative_to(root.resolve()).as_posix(),
            str(item["code"] or "invalid-syntax"),
            re.sub(r" from line \d+\b", " from line <line>", item["message"]),
        )
        for item in findings
    )


def baseline_counts(payload: dict, ruff_version: str) -> Counter:
    """Load allowances; non-empty findings are only valid for the capturing Ruff."""
    if payload.get("schema") != 1 or not isinstance(payload.get("ruff_version"), str):
        raise ValueError("unsupported lint baseline schema/version")
    if not isinstance(payload.get("findings"), list):
        raise ValueError("invalid lint baseline findings")
    # Diagnostic text is Ruff-version specific, so recorded allowances must be
    # recaptured with the pinned Ruff. An empty list allows nothing under any
    # version, which lets a reviewed Ruff pin bump stay a one-file change.
    if payload["findings"] and payload["ruff_version"] != ruff_version:
        raise ValueError(
            "lint baseline findings were captured with Ruff "
            f"{payload['ruff_version']}, not the pinned {ruff_version}"
        )
    counts = Counter()
    for item in payload["findings"]:
        key = (item["path"], item["code"], item["message"])
        if key in counts or type(item["count"]) is not int or item["count"] <= 0:
            raise ValueError("invalid or duplicate lint baseline entry")
        counts[key] = item["count"]
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        action="store_true",
        help="print current findings as JSON; never write the baseline",
    )
    args = parser.parse_args(argv)
    ruff_version = pinned_ruff_version()
    running = installed_ruff_version()
    if running != ruff_version:
        print(
            f"Ruff {running} is installed, but requirements-lint.txt pins "
            f"{ruff_version}; run: python -m pip install -r requirements-lint.txt",
            file=sys.stderr,
        )
        return 2
    paths = (
        subprocess.check_output(
            [
                "git",
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
                "*.py",
            ],
            cwd=ROOT,
        )
        .decode()
        .split("\0")
    )
    paths = sorted({path for path in paths if path and (ROOT / path).is_file()})
    if not paths:
        raise ValueError("no Python source files found")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--output-format",
            "json",
            "--",
            *paths,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode not in (0, 1):
        print(result.stderr or "Ruff failed to run", file=sys.stderr)
        return 2
    current = finding_counts(json.loads(result.stdout), ROOT)
    if args.report:
        print(
            json.dumps(
                {
                    "schema": 1,
                    "ruff_version": ruff_version,
                    "findings": [
                        dict(path=path, code=code, message=message, count=count)
                        for (path, code, message), count in sorted(current.items())
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    allowed = baseline_counts(
        json.loads(BASELINE.read_text(encoding="utf-8")), ruff_version
    )
    added, removed = current - allowed, allowed - current
    for (path, code, message), count in sorted(added.items()):
        print(f"NEW {path}: {code} {message} (x{count})")
    print(
        f"Ruff: {sum(current.values())} existing findings; {sum(added.values())} new; {sum(removed.values())} baseline findings resolved"
    )
    if removed:
        print(
            "Remove resolved baseline entries in the same cleanup change; do not increase allowances to make CI green."
        )
    return 1 if added or removed else 0


if __name__ == "__main__":
    raise SystemExit(main())
