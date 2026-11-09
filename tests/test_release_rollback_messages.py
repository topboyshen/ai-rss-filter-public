"""A failed rollback must not announce itself as a rollback of the bad release.

``restore_previous`` is shared by deploy and rollback.  During a rollback the
"previous" state is the release being rolled *away from*, so the old wording
("Restoring previous release ...") read as if the rollback had succeeded while
the command was actually putting the rejected release back.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "release.sh"


def _function(name: str) -> str:
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(name + "() {"))
    end = next(index for index in range(start + 1, len(lines)) if lines[index] == "}")
    return "\n".join(lines[start:end + 1])


def _restore(action: str) -> subprocess.CompletedProcess:
    stubs = "\n".join((
        "validate_recorded_state() { :; }",
        "validate_event_mapping_release_target() { return 0; }",
        "compose_up() { :; }",
        "wait_healthy() { return 1; }",
        "resolved_image_id() { printf 'sha256:'; printf 'b%.0s' $(seq 64); printf '\\n'; }",
        "record_history() { :; }",
    ))
    harness = "\n".join((
        "set -uo pipefail",
        _function("restore_previous"),
        stubs,
        f'restore_previous {action} my-ai-filter:2026.11.09.1 sha256:{"a" * 64} manifest.yml ai-filter-api',
    ))
    return subprocess.run(
        ["bash", "-c", harness], text=True, capture_output=True, timeout=30
    )


def test_failed_rollback_names_the_release_it_restores():
    result = _restore("rollback")

    assert "recorded current release my-ai-filter:2026.11.09.1" in result.stderr
    assert "Restoring previous release" not in result.stderr


def test_failed_deploy_still_names_the_previous_release():
    result = _restore("deploy")

    assert "Restoring previous release my-ai-filter:2026.11.09.1" in result.stderr
