"""CI and the release gate must parse every tracked shell script."""

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "check-shell-syntax.sh"
SCRIPTS = (
    "deploy/health_check.sh",
    "ops/backup/backup_system.sh",
    "ops/install.sh",
    "ops/verify.sh",
    "scripts/check-shell-syntax.sh",
    "scripts/with spaces.sh",
    "start.sh",
)


def _fixture_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for name in SCRIPTS:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/usr/bin/env bash\nprintf 'ok\\n'\n", encoding="utf-8")
    shutil.copy2(CHECKER, repo / "scripts" / CHECKER.name)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    return repo


def _check(repo):
    return subprocess.run(
        ["bash", str(repo / "scripts" / CHECKER.name)],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def test_checks_all_tracked_scripts_including_root_ops_deploy_and_spaces(tmp_path):
    repo = _fixture_repo(tmp_path)
    (repo / "untracked.sh").write_text("if then\n", encoding="utf-8")
    result = _check(repo)
    assert result.returncode == 0, result.stderr
    for name in SCRIPTS:
        assert f"Checking shell syntax: {name}\n" in result.stdout
    assert "untracked.sh" not in result.stdout
    assert f"Shell syntax OK: {len(SCRIPTS)} tracked file(s)" in result.stdout


@pytest.mark.parametrize("broken", [SCRIPTS[1], SCRIPTS[-1]])
def test_invalid_second_or_last_script_fails(tmp_path, broken):
    repo = _fixture_repo(tmp_path)
    (repo / broken).write_text("#!/usr/bin/env bash\nif then\n", encoding="utf-8")
    # This is the old command's defect: only the first script is parsed.
    old = subprocess.run(
        ["bash", "-n", SCRIPTS[0], broken], cwd=repo, capture_output=True, check=False
    )
    assert old.returncode == 0
    result = _check(repo)
    assert result.returncode != 0
    assert broken in result.stderr
    assert "Shell syntax OK" not in result.stdout


def test_git_enumeration_failure_is_not_a_success(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(CHECKER, scripts)
    result = _check(tmp_path)
    assert result.returncode != 0
    assert "Shell syntax OK" not in result.stdout


def test_ci_and_release_gate_use_same_checker():
    command = "bash scripts/check-shell-syntax.sh"
    for path in (ROOT / ".github/workflows/ci.yml", ROOT / "scripts/run-release-gate.sh"):
        source = path.read_text(encoding="utf-8")
        assert command in source
        assert "bash -n scripts/*.sh" not in source
