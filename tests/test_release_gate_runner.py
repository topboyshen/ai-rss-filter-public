import hashlib
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER_SOURCE = PROJECT_ROOT / "scripts" / "run-release-gate.sh"
PYTHON_IDENTITY = "CPython|3.11.9"
FIXED_PYTEST_ARGV = [
    "-I",
    "-m",
    "pytest",
    "--cov",
    "--cov-config=.coveragerc",
    "--cov-report=term",
    "--cov-fail-under=75",
]


@dataclass(frozen=True)
class GateRepo:
    root: Path
    runner: Path
    attestation_root: Path
    tmp_base: Path
    fake_python: Path
    python_record: Path
    env: dict[str, str]
    head: str

    @property
    def attestation_dir(self) -> Path:
        return self.attestation_root

    @property
    def state_file(self) -> Path:
        return self.attestation_dir / f"{self.head}.state"

    @property
    def log_file(self) -> Path:
        return self.attestation_dir / f"{self.head}.log"


def _run(*args, cwd=None, env=None, timeout=30, check=False):
    return subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=check,
    )


def _git(repo, *args):
    return _run("git", "-C", repo, *args, check=True).stdout.strip()


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_fake_python(path, record, pytest_exit):
    path.write_text(
        f"""#!/bin/sh
set -eu
if [ "${{1:-}}" = "-I" ] && [ "${{2:-}}" = "-c" ]; then
  printf '%s\\n' '{PYTHON_IDENTITY}'
  exit 0
fi
test -f pytest.ini
grep -Fx 'addopts = -q' pytest.ini > /dev/null
: > '{record}'
printf 'ACTIVE=%s\\n' "${{AI_FILTER_RELEASE_GATE_ACTIVE:-}}" >> '{record}'
for arg in "$@"; do
  printf 'ARG=%s\\n' "$arg" >> '{record}'
done
exit {pytest_exit}
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _make_gate_repo(tmp_path, *, pytest_exit=0):
    assert RUNNER_SOURCE.is_file(), "scripts/run-release-gate.sh must exist"

    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    backup_scripts = repo / "ops" / "backup"
    scripts.mkdir(parents=True)
    backup_scripts.mkdir(parents=True)

    runner = scripts / RUNNER_SOURCE.name
    shutil.copy2(RUNNER_SOURCE, runner)
    runner.chmod(0o755)
    shutil.copy2(PROJECT_ROOT / "pytest.ini", repo / "pytest.ini")
    shutil.copy2(RUNNER_SOURCE.with_name("check-shell-syntax.sh"), scripts)
    placeholder = backup_scripts / "syntax-placeholder.sh"
    placeholder.write_text("#!/bin/sh\nset -eu\n", encoding="utf-8")
    placeholder.chmod(0o755)
    (repo / ".coveragerc").write_text("[run]\nbranch = True\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")

    _run("git", "init", "-q", repo, check=True)
    _git(repo, "config", "user.name", "Release Gate Test")
    _git(repo, "config", "user.email", "release-gate@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "release gate fixture")
    head = _git(repo, "rev-parse", "HEAD")

    attestation_root = tmp_path / "attestations"
    tmp_base = tmp_path / "runner-tmp"
    tmp_base.mkdir(mode=0o700)
    fake_python = tmp_path / "fake-python"
    python_record = tmp_path / "python-record"
    _write_fake_python(fake_python, python_record, pytest_exit)
    fake_tools = tmp_path / "fake-tools"
    fake_tools.mkdir()
    fake_flock = fake_tools / "flock"
    fake_flock.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_flock.chmod(0o755)

    env = {
        "HOME": str(tmp_path / "home"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": f"{fake_tools}:{os.environ['PATH']}",
        "AI_FILTER_TEST_PYTHON": str(fake_python),
        "AI_FILTER_TEST_TMPDIR": str(tmp_base),
        "RELEASE_TEST_ATTESTATION_DIR": str(attestation_root),
    }
    Path(env["HOME"]).mkdir(mode=0o700)
    return GateRepo(
        root=repo,
        runner=runner,
        attestation_root=attestation_root,
        tmp_base=tmp_base,
        fake_python=fake_python,
        python_record=python_record,
        env=env,
        head=head,
    )


def _run_gate(gate, *args, env=None):
    return _run(
        gate.runner,
        *args,
        cwd=gate.root,
        env=gate.env if env is None else env,
    )


def _recorded_pytest_call(gate):
    lines = gate.python_record.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "ACTIVE=1"
    return [line.removeprefix("ARG=") for line in lines[1:]]


def _assert_no_temporary_artifacts(gate):
    if gate.attestation_dir.exists():
        names = [path.name for path in gate.attestation_dir.iterdir()]
        assert not [name for name in names if ".tmp" in name]
    assert list(gate.tmp_base.iterdir()) == []


def test_runner_rejects_arguments_without_starting_pytest(tmp_path):
    gate = _make_gate_repo(tmp_path)

    result = _run_gate(gate, "tests/test_release_gate_runner.py")

    assert result.returncode != 0
    assert not gate.python_record.exists()
    assert not gate.state_file.exists()


def test_runner_rejects_active_recursion_without_starting_pytest(tmp_path):
    gate = _make_gate_repo(tmp_path)
    env = gate.env | {"ACTIVE": "1"}

    result = _run_gate(gate, env=env)

    assert result.returncode != 0
    assert not gate.python_record.exists()
    assert not gate.state_file.exists()


def test_runner_rejects_dirty_source_without_starting_pytest(tmp_path):
    gate = _make_gate_repo(tmp_path)
    (gate.root / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    result = _run_gate(gate)

    assert result.returncode != 0
    assert not gate.python_record.exists()
    assert not gate.state_file.exists()


def test_failed_pytest_invalidates_old_attestation_and_leaves_no_temp(tmp_path):
    gate = _make_gate_repo(tmp_path, pytest_exit=7)
    gate.attestation_dir.mkdir(parents=True, mode=0o700)
    gate.state_file.write_text("stale-pass\n", encoding="utf-8")
    gate.state_file.chmod(0o600)

    result = _run_gate(gate)

    assert result.returncode != 0
    assert _recorded_pytest_call(gate) == FIXED_PYTEST_ARGV
    assert not gate.state_file.exists()
    _assert_no_temporary_artifacts(gate)


def test_success_writes_fixed_atomic_eleven_line_attestation(tmp_path):
    gate = _make_gate_repo(tmp_path)

    result = _run_gate(gate)

    assert result.returncode == 0, result.stderr
    assert _recorded_pytest_call(gate) == FIXED_PYTEST_ARGV
    assert gate.state_file.is_file()
    assert gate.log_file.is_file()
    assert stat.S_IMODE(gate.attestation_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(gate.state_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(gate.log_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(
        (gate.attestation_dir / "attestation.lock").stat().st_mode
    ) == 0o600
    assert gate.state_file.stat().st_nlink == 1
    assert gate.log_file.stat().st_nlink == 1

    lines = gate.state_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 11
    assert lines[:3] == [
        "ai-filter-release-test-attestation-v1",
        f"{gate.head}:clean",
        "pytest-full-cov75-v1",
    ]
    assert lines[3] == _sha256(gate.runner)
    assert lines[4] == str(gate.fake_python.resolve())
    assert lines[5] == _sha256(gate.fake_python)
    assert lines[6] == hashlib.sha256(PYTHON_IDENTITY.encode()).hexdigest()
    assert lines[7].isdigit()
    assert lines[8].isdigit()
    assert int(lines[7]) <= int(lines[8])
    assert lines[9] == _sha256(gate.log_file)
    assert lines[10] == "passed"
    _assert_no_temporary_artifacts(gate)


def test_shell_syntax_failure_blocks_pytest_and_attestation(tmp_path):
    gate = _make_gate_repo(tmp_path)
    (gate.root / "start.sh").write_text("#!/bin/bash\nif then\n", encoding="utf-8")
    _git(gate.root, "add", "start.sh")
    _git(gate.root, "commit", "-qm", "invalid last shell file")

    result = _run_gate(gate)

    assert result.returncode != 0
    assert "start.sh" in result.stdout + result.stderr
    assert not gate.python_record.exists()
    assert not list(gate.attestation_root.rglob("*.state"))
    _assert_no_temporary_artifacts(gate)


def test_runner_invokes_venv_symlink_without_discarding_its_context(tmp_path):
    gate = _make_gate_repo(tmp_path)
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(gate.fake_python)
    env = gate.env | {"AI_FILTER_TEST_PYTHON": str(venv_python)}

    result = _run_gate(gate, env=env)

    assert result.returncode == 0, result.stderr
    assert _recorded_pytest_call(gate) == FIXED_PYTEST_ARGV
    lines = gate.state_file.read_text(encoding="utf-8").splitlines()
    assert lines[4] == str(venv_python)
    assert lines[5] == _sha256(gate.fake_python)


def test_cleanup_failure_revokes_a_published_pass(tmp_path):
    gate = _make_gate_repo(tmp_path)
    fake_bin = tmp_path / "cleanup-failure-bin"
    fake_bin.mkdir()
    fake_rm = fake_bin / "rm"
    fake_rm.write_text(
        """#!/bin/sh
case "$*" in
  *ai-filter-release-gate.*) exit 70 ;;
esac
exec /bin/rm "$@"
""",
        encoding="utf-8",
    )
    fake_rm.chmod(0o755)
    env = gate.env | {"PATH": f"{fake_bin}:{gate.env['PATH']}"}

    result = _run_gate(gate, env=env)

    assert result.returncode == 70
    assert not gate.state_file.exists()
    assert not gate.log_file.exists()
