import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER = PROJECT_ROOT / "scripts" / "run-release-tests.sh"


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None):
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _make_runner_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(RUNNER, scripts / RUNNER.name)
    shutil.copy2(PROJECT_ROOT / "pytest.ini", repo / "pytest.ini")
    (repo / ".gitignore").write_text(
        "secrets/\n.venv/\n.venv-*/\n", encoding="utf-8"
    )
    _run(["git", "init", "--quiet"], cwd=repo)
    _run(["git", "add", "."], cwd=repo)
    committed = _run(
        [
            "git",
            "-c",
            "user.name=Release Runner Test",
            "-c",
            "user.email=release-runner@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "test fixture",
        ],
        cwd=repo,
    )
    assert committed.returncode == 0, committed.stderr
    return repo


def _make_fake_python(tmp_path: Path, *, write_coverage: bool = True) -> dict[str, Path]:
    fake_python = tmp_path / "fake-python"
    arguments = tmp_path / "python-arguments"
    environment = tmp_path / "python-environment"
    secret_visibility = tmp_path / "secret-visibility"
    coverage_command = "printf '<coverage generated=\"true\"/>\\n' > coverage.xml"
    if not write_coverage:
        coverage_command = ":"
    fake_python.write_text(
        f"""#!/bin/sh
set -eu
test -f pytest.ini
grep -Fx 'addopts = -q' pytest.ini > /dev/null
printf '%s\\n' "$@" > {shlex.quote(str(arguments))}
env | LC_ALL=C sort > {shlex.quote(str(environment))}
if [ -e secrets/production-only-token ]; then
  printf 'visible\\n' > {shlex.quote(str(secret_visibility))}
else
  printf 'hidden\\n' > {shlex.quote(str(secret_visibility))}
fi
{coverage_command}
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    return {
        "executable": fake_python,
        "arguments": arguments,
        "environment": environment,
        "secret_visibility": secret_visibility,
    }


def _runner_env(
    fake_python: Path,
    tmp_base: Path,
    *,
    coverage_output: Path | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "AI_FILTER_TEST_PYTHON": str(fake_python.resolve()),
            "AI_FILTER_TEST_TMPDIR": str(tmp_base.resolve()),
            "RELEASE_RUNNER_MUST_NOT_LEAK": "production-like-secret",
        }
    )
    if coverage_output is not None:
        env["AI_FILTER_TEST_COVERAGE_OUTPUT"] = str(coverage_output)
    return env


@pytest.mark.parametrize(
    ("arguments", "expected_usage"),
    [
        ([], True),
        (["full", "-k", "release"], True),
        (["tests/test_release_script.py"], True),
    ],
)
def test_runner_rejects_missing_mode_and_arbitrary_pytest_arguments(
    arguments, expected_usage
):
    result = _run([str(RUNNER), *arguments], cwd=PROJECT_ROOT)

    assert result.returncode == 2
    assert ("Usage: scripts/run-release-tests.sh {focused|full}" in result.stderr) is (
        expected_usage
    )


@pytest.mark.parametrize(
    ("mode", "expected_arguments", "repeat_count"),
    [
        (
            "focused",
            ["-I", "-m", "pytest", "tests/test_release_script.py"],
            1,
        ),
        (
            "full",
            [
                "-I",
                "-m",
                "pytest",
                "--cov",
                "--cov-config=.coveragerc",
                "--cov-report=xml",
                "--cov-report=term",
                "--cov-fail-under=75",
            ],
            2,
        ),
    ],
)
def test_runner_uses_fixed_isolated_mode_and_cleans_worktree(
    tmp_path, mode, expected_arguments, repeat_count
):
    repo = _make_runner_repo(tmp_path)
    tmp_base = tmp_path / "runner-tmp"
    tmp_base.mkdir()
    fake = _make_fake_python(tmp_path)
    secret = repo / "secrets" / "production-only-token"
    secret.parent.mkdir()
    secret.write_text("must-stay-outside-detached-worktree\n", encoding="utf-8")
    env = _runner_env(fake["executable"], tmp_base)

    for _ in range(repeat_count):
        result = _run(
            [str(repo / "scripts" / RUNNER.name), mode],
            cwd=repo,
            env=env,
        )
        assert result.returncode == 0, result.stderr

    assert fake["arguments"].read_text(encoding="utf-8").splitlines() == (
        expected_arguments
    )
    isolated_env = fake["environment"].read_text(encoding="utf-8")
    assert "RELEASE_RUNNER_MUST_NOT_LEAK=" not in isolated_env
    assert "AI_FILTER_TEST_PYTHON=" not in isolated_env
    assert "PYTHONNOUSERSITE=1" in isolated_env
    assert fake["secret_visibility"].read_text(encoding="utf-8") == "hidden\n"
    assert not list(tmp_base.glob("ai-filter-release-tests.*"))
    worktrees = _run(["git", "worktree", "list", "--porcelain"], cwd=repo)
    assert worktrees.stdout.count("worktree ") == 1

    coverage_output = repo / "coverage.xml"
    assert not coverage_output.exists()


def test_full_mode_exports_coverage_outside_source_worktree(tmp_path):
    repo = _make_runner_repo(tmp_path)
    tmp_base = tmp_path / "runner-tmp"
    tmp_base.mkdir()
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    coverage_output = artifact_dir / "ai-filter-coverage.xml"
    fake = _make_fake_python(tmp_path)

    result = _run(
        [str(repo / "scripts" / RUNNER.name), "full"],
        cwd=repo,
        env=_runner_env(
            fake["executable"],
            tmp_base,
            coverage_output=coverage_output,
        ),
    )

    assert result.returncode == 0, result.stderr
    assert coverage_output.read_text(encoding="utf-8") == (
        '<coverage generated="true"/>\n'
    )
    assert "AI_FILTER_TEST_COVERAGE_OUTPUT=" not in fake[
        "environment"
    ].read_text(encoding="utf-8")
    assert not (repo / "coverage.xml").exists()
    assert not list(tmp_base.glob("ai-filter-release-tests.*"))


def test_runner_passes_only_validated_private_synthetic_evidence_and_preserves_it(tmp_path):
    repo = _make_runner_repo(tmp_path)
    tmp_base = tmp_path / "runner-tmp"
    tmp_base.mkdir()
    evidence = tmp_path / "synthetic-evidence"
    evidence.mkdir(mode=0o700)
    original = evidence / "previous-failure.json"
    original.write_bytes(b'{"status":"unconfirmed"}')
    fake = _make_fake_python(tmp_path)
    env = _runner_env(fake["executable"], tmp_base)
    env["AI_FILTER_CUTOVER_EVIDENCE_DIR"] = str(evidence)
    result = _run([str(repo / "scripts" / RUNNER.name), "full"], cwd=repo, env=env)
    assert result.returncode == 0, result.stderr
    actual = fake["environment"].read_text()
    assert "AI_FILTER_CUTOVER_EVIDENCE_DIR=" + str(evidence.resolve()) in actual
    assert "RELEASE_RUNNER_MUST_NOT_LEAK=" not in actual
    assert original.read_bytes() == b'{"status":"unconfirmed"}'
    assert not list(tmp_base.glob("ai-filter-release-tests.*"))


@pytest.mark.parametrize("invalid", ["relative", "missing", "file", "symlink", "public", "source"])
def test_runner_rejects_unsafe_synthetic_evidence_before_test_execution(tmp_path, invalid):
    repo = _make_runner_repo(tmp_path)
    tmp_base = tmp_path / "runner-tmp"
    tmp_base.mkdir()
    evidence = tmp_path / "synthetic-evidence"
    evidence.mkdir(mode=0o700)
    target = evidence
    if invalid == "relative":
        target = Path("relative-evidence")
    elif invalid == "missing":
        target = tmp_path / "missing"
    elif invalid == "file":
        target = tmp_path / "file"
        target.write_text("not a directory")
    elif invalid == "symlink":
        target = tmp_path / "alias"
        target.symlink_to(evidence, target_is_directory=True)
    elif invalid == "public":
        evidence.chmod(0o755)
    else:
        target = repo
        target.chmod(0o700)
    fake = _make_fake_python(tmp_path)
    env = _runner_env(fake["executable"], tmp_base)
    env["AI_FILTER_CUTOVER_EVIDENCE_DIR"] = str(target)
    result = _run([str(repo / "scripts" / RUNNER.name), "full"], cwd=repo, env=env)
    assert result.returncode == 2, result.stderr
    assert "evidence" in result.stderr.lower()
    assert not fake["arguments"].exists()
    assert not list(tmp_base.glob("ai-filter-release-tests.*"))


@pytest.mark.parametrize("explicit_override", [False, True])
def test_runner_selects_release_gate_environment_or_explicit_python(
    tmp_path, explicit_override
):
    repo = _make_runner_repo(tmp_path)
    tmp_base = tmp_path / "runner-tmp"
    tmp_base.mkdir()
    default_fake = _make_fake_python(tmp_path)
    default_python = repo / ".venv-release-gate" / "bin" / "python"
    default_python.parent.mkdir(parents=True)
    default_python.symlink_to(default_fake["executable"])
    legacy_python = repo / ".venv" / "bin" / "python"
    legacy_python.parent.mkdir(parents=True)
    legacy_python.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
    legacy_python.chmod(0o755)
    selected = default_fake
    env = _runner_env(default_python, tmp_base)
    env.pop("AI_FILTER_TEST_PYTHON")
    if explicit_override:
        override_dir = tmp_path / "explicit-python"
        override_dir.mkdir()
        selected = _make_fake_python(override_dir)
        env["AI_FILTER_TEST_PYTHON"] = str(selected["executable"])

    result = _run(
        [str(repo / "scripts" / RUNNER.name), "focused"], cwd=repo, env=env
    )

    assert result.returncode == 0, result.stderr
    assert selected["arguments"].read_text().splitlines() == [
        "-I", "-m", "pytest", "tests/test_release_script.py"
    ]
    if explicit_override:
        assert not default_fake["arguments"].exists()
    assert not list(tmp_base.glob("ai-filter-release-tests.*"))


def test_runner_refuses_dirty_source_before_creating_worktree(tmp_path):
    repo = _make_runner_repo(tmp_path)
    tmp_base = tmp_path / "runner-tmp"
    tmp_base.mkdir()
    fake = _make_fake_python(tmp_path)
    (repo / "uncommitted.py").write_text("dirty = True\n", encoding="utf-8")

    result = _run(
        [str(repo / "scripts" / RUNNER.name), "focused"],
        cwd=repo,
        env=_runner_env(fake["executable"], tmp_base),
    )

    assert result.returncode == 2
    assert "Refusing to test a dirty source worktree" in result.stderr
    assert not fake["arguments"].exists()
    assert not list(tmp_base.glob("ai-filter-release-tests.*"))


def test_full_mode_requires_coverage_artifact(tmp_path):
    repo = _make_runner_repo(tmp_path)
    tmp_base = tmp_path / "runner-tmp"
    tmp_base.mkdir()
    fake = _make_fake_python(tmp_path, write_coverage=False)

    result = _run(
        [str(repo / "scripts" / RUNNER.name), "full"],
        cwd=repo,
        env=_runner_env(fake["executable"], tmp_base),
    )

    assert result.returncode == 1
    assert "did not produce a regular coverage.xml" in result.stderr
    assert not (repo / "coverage.xml").exists()
    assert not list(tmp_base.glob("ai-filter-release-tests.*"))
