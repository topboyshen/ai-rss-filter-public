"""Conservatively identify documentation-only PRs and check their changed text.

The classifier is an optimization, not a reason to skip an uncertain change:
missing metadata, unavailable Git history and unknown file types all select the
full gate. Push and scheduled runs always select that full gate.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys


_SHA = re.compile(r"[0-9a-f]{40}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_CONFLICT = re.compile(r"^(?:<{7,}|>{7,}|\|{7,})(?:\s|$)")
_ROOT_DOCUMENTS = frozenset({"README.md", "CONTRIBUTING.md"})
_TEST_INPUT_DOCUMENTS = frozenset({"docs/lint-baseline.md"})
_MAX_GIT_OUTPUT = 8 * 1024 * 1024


@dataclass(frozen=True)
class ScopeResult:
    docs_only: bool
    reason: str
    base_sha: str = ""
    head_sha: str = ""
    merge_base: str = ""
    paths: tuple[str, ...] = ()


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        [
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.quotePath=false",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        timeout=15,
        check=True,
    )
    if len(result.stdout) > _MAX_GIT_OUTPUT:
        raise ValueError("Git output exceeds the bounded inspection size")
    return result.stdout


def _pr_identity(event: dict) -> tuple[str, str]:
    if not isinstance(event, dict):
        raise ValueError("event is not an object")
    pull_request = event.get("pull_request")
    if not isinstance(pull_request, dict):
        raise ValueError("missing pull request")
    if type(pull_request.get("number")) is not int or pull_request["number"] <= 0:
        raise ValueError("invalid pull request number")
    if type(event.get("number")) is not int or event["number"] != pull_request["number"]:
        raise ValueError("pull request number does not match event")
    if pull_request.get("state") != "open":
        raise ValueError("pull request is not open")
    repository = event.get("repository")
    base, head = pull_request.get("base"), pull_request.get("head")
    if not all(isinstance(item, dict) for item in (repository, base, head)):
        raise ValueError("missing repository or commit metadata")
    full_name = repository.get("full_name")
    base_repository = base.get("repo")
    if (
        not isinstance(full_name, str)
        or _REPOSITORY.fullmatch(full_name) is None
        or not isinstance(base_repository, dict)
        or base_repository.get("full_name") != full_name
    ):
        raise ValueError("base repository is not the event repository")
    base_sha, head_sha = base.get("sha"), head.get("sha")
    if any(not isinstance(sha, str) or _SHA.fullmatch(sha) is None for sha in (base_sha, head_sha)):
        raise ValueError("commit identity must be a complete SHA")
    return base_sha, head_sha


def _is_document(path: str) -> bool:
    if (
        not path
        or path in _TEST_INPUT_DOCUMENTS
        or "\\" in path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
        or any(not part or part.startswith(".") for part in path.split("/"))
    ):
        return False
    return path in _ROOT_DOCUMENTS or (path.startswith("docs/") and path.endswith(".md"))


def _checkout_matches(repo: Path, base_sha: str, head_sha: str) -> bool:
    checkout_sha = _git(repo, "rev-parse", "--verify", "HEAD").decode().strip()
    if checkout_sha == head_sha:
        return True
    # GitHub's ordinary PR checkout is a synthetic merge commit. Keep full CI
    # testing that merge instead of replacing its checkout with the head branch.
    parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").decode().split()
    if parents != [checkout_sha, base_sha, head_sha]:
        return False
    # Only the PR head's blobs are checked below. If the tested merge introduces
    # *any* other content, even another document, retain the full merged gate.
    return not _raw_diff(repo, head_sha, checkout_sha)


def _raw_diff(repo: Path, start: str, end: str) -> bytes:
    return _git(
        repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
        "--raw", "-z", "--abbrev=40", start, end, "--",
    )


def _changed_document_paths(raw: bytes) -> tuple[str, ...]:
    fields = raw.split(b"\0")
    if not fields or fields[-1] != b"" or (len(fields) - 1) % 2:
        raise ValueError("invalid NUL-delimited Git diff")
    paths = []
    for position in range(0, len(fields) - 1, 2):
        header = fields[position].decode("ascii").split()
        path = fields[position + 1].decode("utf-8")
        if len(header) != 5 or not header[0].startswith(":"):
            raise ValueError("invalid raw Git diff header")
        old_mode, new_mode, old_sha, new_sha, status = header
        if any(_SHA.fullmatch(sha) is None for sha in (old_sha, new_sha)):
            raise ValueError("invalid diff object identity")
        # Deletes/renames and mode changes stay full, even between .md paths.
        valid_modes = (status == "A" and old_mode == ":000000") or (
            status == "M" and old_mode == ":100644"
        )
        if not valid_modes or new_mode != "100644" or not _is_document(path):
            raise ValueError("change is outside regular added/modified documentation")
        paths.append(path)
    if not paths or len(paths) != len(set(paths)):
        raise ValueError("empty or duplicate file changes")
    return tuple(paths)


def classify_scope(repo: Path, event_name: str, event: dict) -> ScopeResult:
    """Choose lightweight checks only for a fully identified PR's entire diff."""
    if event_name != "pull_request":
        return ScopeResult(False, "non_pull_request")
    try:
        base_sha, head_sha = _pr_identity(event)
        for sha in (base_sha, head_sha):
            resolved = _git(repo, "rev-parse", "--verify", f"{sha}^{{commit}}").decode().strip()
            if resolved != sha:
                raise ValueError("commit did not resolve exactly")
        if not _checkout_matches(repo, base_sha, head_sha):
            raise ValueError("checkout is not the event's head or exact test merge")
        merge_base = _git(repo, "merge-base", "--all", base_sha, head_sha).decode().strip()
        if _SHA.fullmatch(merge_base) is None:
            raise ValueError("missing or ambiguous merge base")
        paths = _changed_document_paths(_raw_diff(repo, merge_base, head_sha))
    except (OSError, ValueError, TypeError, UnicodeError, subprocess.SubprocessError):
        return ScopeResult(False, "unverified_or_non_document_change")
    return ScopeResult(True, "documentation_only", base_sha, head_sha, merge_base, paths)


def _has_conflict_marker(text: str) -> bool:
    fence_character, fence_length = "", 0
    for line in text.splitlines():
        match = _FENCE.match(line)
        if match:
            marker, suffix = match.groups()
            if not fence_character:
                fence_character, fence_length = marker[0], len(marker)
            elif (
                marker[0] == fence_character
                and len(marker) >= fence_length
                and not suffix.strip()
            ):
                fence_character, fence_length = "", 0
            continue
        if not fence_character and _CONFLICT.match(line):
            return True
    return False


def _read_document(repo: Path, sha: str, path: str) -> str:
    reference = f"{sha}:{path}"
    size = int(_git(repo, "cat-file", "-s", reference).decode().strip())
    if not 0 <= size <= _MAX_GIT_OUTPUT:
        raise ValueError("document exceeds bounded inspection size")
    return _git(repo, "show", reference).decode("utf-8")


def _bad_added_whitespace(repo: Path, scope: ScopeResult) -> bool:
    # Git's --check also rejects *documented* conflict-marker examples. Inspect
    # only added lines here and leave actual conflict detection to the Markdown
    # fence-aware check. A Markdown two-space hard line break remains valid.
    diff = _git(
        repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
        "--no-color", "--unified=0", scope.merge_base, scope.head_sha, "--", *scope.paths,
    )
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith(b"diff --git "):
            in_hunk = False
        elif line.startswith(b"@@ "):
            in_hunk = True
        elif in_hunk and line.startswith(b"+"):
            content = line[1:]
            trailing_spaces = len(content) - len(content.rstrip(b" "))
            if content.endswith(b"\t") or trailing_spaces not in (0, 2):
                return True
            if trailing_spaces and not content.strip(b" "):
                return True
    return False


def check_documents(repo: Path, scope: ScopeResult) -> list[str]:
    """Check committed document blobs, never executable hooks or working links."""
    if not scope.docs_only or not scope.paths:
        return ["document check requires a verified documentation-only PR"]
    errors = []
    for path in scope.paths:
        try:
            text = _read_document(repo, scope.head_sha, path)
            if "\0" in text:
                errors.append(f"{path!r}: contains NUL bytes")
            if _has_conflict_marker(text):
                errors.append(f"{path!r}: unresolved merge conflict marker")
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
            errors.append(f"{path!r}: cannot read bounded UTF-8 document")
    try:
        if _bad_added_whitespace(repo, scope):
            errors.append("changed documentation contains invalid trailing whitespace")
    except (OSError, ValueError, subprocess.SubprocessError):
        errors.append("changed documentation could not complete whitespace checks")
    return errors


def _load_event(path: Path) -> dict:
    try:
        payload = path.read_bytes()
        if len(payload) > _MAX_GIT_OUTPUT:
            return {}
        event = json.loads(payload)
        return event if isinstance(event, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("classify", "check"))
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--event-path", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    scope = classify_scope(args.repo, args.event_name, _load_event(args.event_path))
    if args.command == "classify":
        line = f"docs_only={'true' if scope.docs_only else 'false'}\n"
        if args.output is not None:
            with args.output.open("a", encoding="utf-8") as output:
                output.write(line)
        print(line, end="")
        print(json.dumps({"reason": scope.reason, "changed_document_count": len(scope.paths)}))
        return 0
    errors = check_documents(args.repo, scope)
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print(f"Documentation check passed for {len(scope.paths)} changed files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
