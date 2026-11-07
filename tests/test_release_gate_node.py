"""A reviewed Node is a gate-only executable, never an ambient PATH exemption.

Guard unit tests execute the actual embedded guard with synthetic stat/FD owner
metadata. Shell tests retain the existing fake-Python fixture and prove wiring,
not a real root-owned toolchain or production Node installation.
"""

import ast
import hashlib
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from test_release_gate_runner import (
    FIXED_PYTEST_ARGV,
    RUNNER_SOURCE,
    _assert_no_temporary_artifacts,
    _make_gate_repo,
    _recorded_pytest_call,
    _run_gate,
    _sha256,
)


SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
MARKER = "AI_FILTER_RELEASE_GATE_NODE_GUARD"


def _actual_guard():
    source = RUNNER_SOURCE.read_text().split("<<'" + MARKER + "'\n", 1)[1].split("\n" + MARKER, 1)[0]
    namespace = {"__name__": "actual_gate_guard_unit"}
    exec(compile(source, "actual_gate_node_guard.py", "exec"), namespace)
    return SimpleNamespace(**namespace), source


def _metadata(info, **changes):
    values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
    values.update(changes)
    return SimpleNamespace(**values)


class SyntheticRoot:
    """Only the guard's metadata/FD boundary is mocked; its actual I/O is used."""

    def __init__(self):
        self.overrides = {}
        self.fd_override = None
        self.euid = 0

    def __getattr__(self, name):
        return getattr(os, name)

    def lstat(self, path):
        info = os.lstat(path)
        mode = info.st_mode & ~0o022 if stat.S_ISDIR(info.st_mode) else info.st_mode
        changes = {"st_uid": 0, "st_gid": 0, "st_mode": mode}
        changes.update(self.overrides.get(str(path), {}))
        return _metadata(info, **changes)

    def fstat(self, fd):
        info = _metadata(os.fstat(fd), st_uid=0, st_gid=0)
        return self.fd_override(info) if self.fd_override else info

    def geteuid(self):
        return self.euid


@pytest.fixture
def guarded(tmp_path):
    guard, source = _actual_guard()
    virtual = SyntheticRoot()
    guard.stage.__globals__["os"] = virtual
    tool = tmp_path / "tools" / "bin" / "node"
    tool.parent.mkdir(parents=True, mode=0o755)
    tool.write_bytes(b"#!/bin/sh\nprintf 'synthetic-node-only\\n'\n")
    tool.chmod(0o755)
    work = tmp_path / "private"
    work.mkdir(mode=0o700)
    repo = tmp_path / "source"
    repo.mkdir()
    expected = _sha256(tool)
    args = (str(tool), expected, str(work), str(repo))
    return SimpleNamespace(guard=guard, source=source, os=virtual, node=tool, work=work, repo=repo, args=args)


def _rejects(case, action="stage", match=None):
    with pytest.raises(case.guard.NodeGuardError, match=match):
        getattr(case.guard, action)(*case.args)


def test_actual_guard_stages_one_readonly_node_and_verifies_unchanged_inputs(guarded):
    case = guarded
    proof = case.guard.stage(*case.args)
    assert case.guard.verify(*case.args) == proof
    assert set(proof) == {"node_sha256", "private_proof_sha256", "metadata_proof"}
    assert proof["metadata_proof"]["source"] == str(case.node)
    assert hashlib.sha256(case.guard.encoded(proof["metadata_proof"])).hexdigest() == proof["private_proof_sha256"]
    assert b"synthetic-node-only" not in case.guard.encoded(proof)
    assert len(case.guard.encoded(proof)) <= case.guard.MAX_PROOF
    assert proof["node_sha256"] == case.args[1]
    node = case.work / "node-bin" / "node"
    assert node.read_bytes() == case.node.read_bytes()
    assert not node.is_symlink()
    assert stat.S_IMODE(node.stat().st_mode) == 0o500
    assert stat.S_IMODE(node.parent.stat().st_mode) == 0o700
    assert node.stat().st_nlink == 1
    assert sorted(path.name for path in node.parent.iterdir()) == ["node"]
    assert stat.S_IMODE((case.work / "node-proof.json").stat().st_mode) == 0o600


@pytest.mark.parametrize("which", ["ancestor", "node", "private"])
def test_actual_guard_rejects_nonroot_owner_without_touching_the_node(guarded, which):
    case = guarded
    path = {"ancestor": case.node.parent, "node": case.node, "private": case.work}[which]
    case.os.overrides[str(path)] = {"st_uid": 1001}
    _rejects(case, match="unsafe_path_owner_or_link")
    assert not (case.work / "node-bin").exists()


def test_actual_guard_requires_root_for_explicit_mode(guarded):
    guarded.os.euid = 1001
    _rejects(guarded, match="root_required")


@pytest.mark.parametrize("which,mode", [("ancestor", 0o775), ("ancestor", 0o757), ("node", 0o775), ("node", 0o757)])
def test_actual_guard_rejects_group_or_other_writable_source_chain(guarded, which, mode):
    case = guarded
    path = case.node.parent if which == "ancestor" else case.node
    kind = stat.S_IFDIR if which == "ancestor" else stat.S_IFREG
    case.os.overrides[str(path)] = {"st_mode": kind | mode}
    _rejects(case, match="writable_path_component")


@pytest.mark.parametrize("which", ["node", "parent"])
def test_actual_guard_rejects_symlink_in_any_source_component(guarded, which):
    case = guarded
    if which == "node":
        target = case.node.with_name("actual-node")
        case.node.rename(target)
        case.node.symlink_to(target)
    else:
        target = case.node.parent.with_name("actual-bin")
        case.node.parent.rename(target)
        case.node.parent.symlink_to(target, target_is_directory=True)
    _rejects(case, match="unsafe_path_owner_or_link")


@pytest.mark.parametrize("change", ["directory", "hardlink", "nonexec", "empty", "oversize"])
def test_actual_guard_rejects_unsafe_final_file(guarded, change):
    case = guarded
    if change == "directory":
        case.node.unlink()
        case.node.mkdir()
    elif change == "hardlink":
        os.link(case.node, case.node.with_name("duplicate"))
    elif change == "nonexec":
        case.node.chmod(0o644)
    elif change == "empty":
        case.node.write_bytes(b"")
    else:
        case.os.overrides[str(case.node)] = {"st_size": case.guard.MAX_NODE + 1}
    _rejects(case, match="invalid_node_file")


@pytest.mark.parametrize("change", ["wronghash", "upperhash", "relative", "colon", "newline", "wrongname", "inside_source"])
def test_actual_guard_rejects_unreviewed_path_or_hash(guarded, change):
    case = guarded
    args = list(case.args)
    if change == "wronghash":
        args[1] = "0" * 64
    elif change == "upperhash":
        args[1] = args[1].upper()
    elif change == "relative":
        args[0] = "tools/bin/node"
    elif change in {"colon", "newline"}:
        args[0] += ":bad" if change == "colon" else "\n"
    elif change == "wrongname":
        renamed = case.node.with_name("bash")
        case.node.rename(renamed)
        args[0] = str(renamed)
    else:
        args[3] = str(case.node.parent)
    case.args = tuple(args)
    _rejects(case)
    assert not (case.work / "node-proof.json").exists()


def test_actual_guard_stops_if_open_fd_differs_from_checked_path(guarded):
    case = guarded
    case.os.fd_override = lambda info: _metadata(info, st_ino=info.st_ino + 1)
    _rejects(case, match="opened_identity_changed")
    assert not (case.work / "node-bin").exists()


def test_actual_guard_uses_nofollow_cloexec_and_no_command_execution(guarded):
    case = guarded
    flags = []
    original = case.os.open

    def record(path, options, *args):
        flags.append(options)
        return original(path, options, *args)

    case.os.open = record
    case.guard.stage(*case.args)
    assert flags and all(value & os.O_NOFOLLOW and value & os.O_CLOEXEC for value in flags)
    tree = ast.parse(case.source)
    forbidden = {"subprocess", "socket", "urllib", "shutil"}
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) and
                   any(alias.name.split(".")[0] in forbidden for alias in node.names) for node in ast.walk(tree))
    assert not any(isinstance(node, ast.Attribute) and node.attr in {"execv", "execve", "system", "popen", "chown"}
                   for node in ast.walk(tree))


@pytest.mark.parametrize("change", ["same_length_rewrite", "source_replacement", "source_mode", "source_owner", "parent_owner", "copy_rewrite", "copy_mode", "private_owner", "proof_mode", "extra_executable"])
def test_actual_guard_rejects_drift_before_attestation(guarded, change):
    case = guarded
    case.guard.stage(*case.args)
    private = case.work / "node-bin" / "node"
    if change == "same_length_rewrite":
        content = case.node.read_bytes()
        case.node.write_bytes(b"x" + content[1:])
    elif change == "source_replacement":
        original = case.node.read_bytes()
        case.node.unlink()
        case.node.write_bytes(original)
        case.node.chmod(0o755)
    elif change == "source_mode":
        case.node.chmod(0o555)
    elif change == "source_owner":
        case.os.overrides[str(case.node)] = {"st_uid": 1001}
    elif change == "parent_owner":
        case.os.overrides[str(case.node.parent)] = {"st_uid": 1001}
    elif change == "copy_rewrite":
        private.chmod(0o700)
        private.write_bytes(b"changed")
        private.chmod(0o500)
    elif change == "copy_mode":
        private.chmod(0o700)
    elif change == "private_owner":
        case.os.overrides[str(case.work)] = {"st_uid": 1001}
    elif change == "proof_mode":
        (case.work / "node-proof.json").chmod(0o644)
    else:
        (private.parent / "other-command").write_bytes(b"not-allowed")
    _rejects(case, action="verify")


def test_actual_guard_manual_timestamp_rewrite_remains_a_blocking_change(guarded):
    case = guarded
    proof = case.guard.stage(*case.args)
    before = case.node.stat()
    os.utime(case.node, ns=(before.st_atime_ns + 1000000, before.st_mtime_ns))
    # utime changes ctime, which must remain a blocking identity change.
    _rejects(case, action="verify", match="source_identity_changed")
    assert proof["node_sha256"] == case.args[1]


def test_actual_guard_shared_parent_without_sticky_bit_is_rejected(guarded):
    case = guarded
    case.os.overrides[str(case.work.parent)] = {"st_mode": stat.S_IFDIR | 0o777}
    _rejects(case, match="writable_path_component")


def test_actual_guard_unknown_operation_has_no_stage_fallback(guarded, capsys):
    assert guarded.guard.main(["other", *guarded.args]) == 2
    assert "fixed_guard_operation_required" in capsys.readouterr().err
    assert not (guarded.work / "node-bin").exists()


def _wire_node_fixture(tmp_path, *, verify_fail=False):
    gate = _make_gate_repo(tmp_path)
    node = tmp_path / "synthetic-tools" / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_text("#!/bin/sh\nprintf 'local-synthetic-node\\n'\n")
    node.chmod(0o755)
    record = tmp_path / "guard-wiring.jsonl"
    prefix = f'''if [ "${{1:-}}" = "-I" ] && [ "${{2:-}}" = "-" ]; then
  printf '%s\\n' "$3" >> '{record}'
  if [ "$3" = stage ]; then
    /bin/mkdir -m 700 "$6/node-bin"
    /bin/cp "$4" "$6/node-bin/node"
    /bin/chmod 500 "$6/node-bin/node"
  fi
  if [ "$3" = verify ] && [ '{int(verify_fail)}' = 1 ]; then exit 2; fi
  printf '%s\\n' '{{"node_sha256":"synthetic","private_proof_sha256":"synthetic"}}'
  exit 0
fi
'''
    text = gate.fake_python.read_text()
    gate.fake_python.write_text(text.replace("set -eu\n", "set -eu\n" + prefix, 1))
    gate.fake_python.chmod(0o755)
    return gate, node, record


@pytest.mark.parametrize("missing", ["path", "sha", "empty_path", "empty_sha"])
def test_gate_requires_exact_pair_before_pytest(tmp_path, missing):
    gate = _make_gate_repo(tmp_path)
    env = gate.env | {"AI_FILTER_RELEASE_GATE_NODE": "/opt/fixed/node", "AI_FILTER_RELEASE_GATE_NODE_SHA256": "0" * 64}
    key = "AI_FILTER_RELEASE_GATE_NODE" if "path" in missing else "AI_FILTER_RELEASE_GATE_NODE_SHA256"
    if missing.startswith("empty"):
        env[key] = ""
    else:
        env.pop(key)
    result = _run_gate(gate, env=env)
    assert result.returncode == 2
    assert "requires both" in result.stderr
    assert not gate.python_record.exists()
    assert not gate.state_file.exists()


def test_gate_no_variables_keeps_fixed_environment_and_ignores_test_node(tmp_path):
    gate = _make_gate_repo(tmp_path)
    child_env = tmp_path / "child-env"
    text = gate.fake_python.read_text()
    last = text.rfind("exit 0\n")
    text = text[:last] + f"env > '{child_env}'\n" + text[last:]
    gate.fake_python.write_text(text)
    env = gate.env | {"AI_FILTER_TEST_NODE": "/does/not/exist", "NODE_OPTIONS": "--must-not-run", "NODE_PATH": "/bad"}
    result = _run_gate(gate, env=env)
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in child_env.read_text().splitlines())
    assert values["PATH"] == SAFE_PATH
    assert all(key not in values for key in ("AI_FILTER_TEST_NODE", "NODE_OPTIONS", "NODE_PATH", "AI_FILTER_RELEASE_GATE_NODE"))
    assert _recorded_pytest_call(gate) == FIXED_PYTEST_ARGV
    assert len(gate.state_file.read_text().splitlines()) == 11
    _assert_no_temporary_artifacts(gate)


def test_gate_explicit_wiring_exposes_one_private_node_and_preserves_eleven_fields(tmp_path):
    gate, node, record = _wire_node_fixture(tmp_path)
    child_env = tmp_path / "child-env"
    observed = tmp_path / "node-observation"
    text = gate.fake_python.read_text()
    last = text.rfind("exit 0\n")
    text = text[:last] + f'''env > '{child_env}'
node --version > '{observed}'
test "$(ls -A "$(dirname "$(command -v node)")")" = node
''' + text[last:]
    gate.fake_python.write_text(text)
    env = gate.env | {"AI_FILTER_RELEASE_GATE_NODE": str(node), "AI_FILTER_RELEASE_GATE_NODE_SHA256": _sha256(node),
                      "NODE_OPTIONS": "--must-not-run", "NODE_PATH": "/bad"}
    result = _run_gate(gate, env=env)
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in child_env.read_text().splitlines())
    private, original = values["PATH"].split(":", 1)
    assert Path(private).name == "node-bin" and original == SAFE_PATH
    assert not Path(private).exists()
    assert str(node.parent) not in values["PATH"].split(":")
    assert observed.read_text() == "local-synthetic-node\n"
    assert all(key not in values for key in ("NODE_OPTIONS", "NODE_PATH", "AI_FILTER_RELEASE_GATE_NODE",
                                            "AI_FILTER_RELEASE_GATE_NODE_SHA256", "AI_FILTER_TEST_NODE"))
    assert record.read_text().splitlines() == ["stage", "verify"]
    assert _recorded_pytest_call(gate) == FIXED_PYTEST_ARGV
    lines = gate.state_file.read_text().splitlines()
    assert len(lines) == 11 and lines[9] == _sha256(gate.log_file)
    assert "Release gate reviewed Node:" in gate.log_file.read_text()
    _assert_no_temporary_artifacts(gate)


def test_gate_verify_failure_cannot_publish_passing_state(tmp_path):
    gate, node, record = _wire_node_fixture(tmp_path, verify_fail=True)
    env = gate.env | {"AI_FILTER_RELEASE_GATE_NODE": str(node), "AI_FILTER_RELEASE_GATE_NODE_SHA256": _sha256(node)}
    result = _run_gate(gate, env=env)
    assert result.returncode == 2
    assert record.read_text().splitlines() == ["stage", "verify"]
    assert not gate.state_file.exists() and not gate.log_file.exists()
    _assert_no_temporary_artifacts(gate)


def test_gate_cleanup_failure_still_revokes_explicit_node_pass(tmp_path):
    gate, node, _record = _wire_node_fixture(tmp_path)
    fake_bin = tmp_path / "cleanup-failure-bin"
    fake_bin.mkdir()
    fake_rm = fake_bin / "rm"
    fake_rm.write_text("#!/bin/sh\ncase \"$*\" in *ai-filter-release-gate.*) exit 70 ;; esac\nexec /bin/rm \"$@\"\n")
    fake_rm.chmod(0o755)
    env = gate.env | {"PATH": f"{fake_bin}:{gate.env['PATH']}", "AI_FILTER_RELEASE_GATE_NODE": str(node),
                      "AI_FILTER_RELEASE_GATE_NODE_SHA256": _sha256(node)}
    result = _run_gate(gate, env=env)
    assert result.returncode == 70
    assert not gate.state_file.exists() and not gate.log_file.exists()


def test_actual_guard_short_write_cannot_publish_private_proof(guarded):
    case = guarded
    case.os.write = lambda _fd, _data: 0
    _rejects(case, match="short_private_write")
    assert not (case.work / "node-proof.json").exists()


def test_actual_guard_actual_read_limit_cannot_be_bypassed_by_metadata(guarded):
    case = guarded
    case.guard.digest_fd.__globals__["MAX_NODE"] = 8
    fd = os.open(case.node, os.O_RDONLY)
    try:
        with pytest.raises(case.guard.NodeGuardError, match="node_size_limit"):
            case.guard.digest_fd(fd)
    finally:
        os.close(fd)


def test_actual_guard_read_access_does_not_bind_atime(guarded):
    case = guarded
    proof = case.guard.stage(*case.args)
    fd = os.open(case.node, os.O_RDONLY)
    try:
        assert os.read(fd, 1)
    finally:
        os.close(fd)
    assert case.guard.verify(*case.args) == proof


@pytest.mark.parametrize("where", ["source", "copy"])
def test_actual_guard_refuses_noncanonical_private_or_source_proof(guarded, where):
    case = guarded
    case.guard.stage(*case.args)
    proof = case.work / "node-proof.json"
    if where == "source":
        raw = proof.read_bytes()
        proof.write_bytes(raw.rstrip(b"\n") + b" \n")
    else:
        private = case.work / "node-bin" / "node"
        private.rename(private.with_name("old-node"))
        private.symlink_to(private.with_name("old-node"))
    _rejects(case, action="verify")


class GuardTimer:
    SIGALRM = 14
    ITIMER_REAL = 0

    def __init__(self):
        self.active = False
        self.calls = []
        self.handler = None

    def getitimer(self, _which):
        return (0.0, 0.0)

    def getsignal(self, _signum):
        return "old-handler"

    def signal(self, signum, handler):
        self.calls.append(("handler", signum, handler))
        self.handler = handler

    def setitimer(self, which, seconds):
        self.active = bool(seconds)
        self.calls.append(("timer", which, seconds))


def test_actual_guard_fixed_alarm_spans_output_and_restores_handler(guarded):
    case = guarded
    timer = GuardTimer()
    namespace = case.guard.main.__globals__
    namespace["signal"] = timer
    outputs = []

    def checked_print(value, **options):
        assert timer.active and options["flush"] is True
        outputs.append(value)

    namespace["print"] = checked_print
    assert case.guard.main(["stage", *case.args]) == 0
    assert outputs and json.loads(outputs[0])["metadata_proof"]["source"] == str(case.node)
    assert [(kind, which, value) for kind, which, value in timer.calls if kind == "timer"] == [
        ("timer", 0, 5), ("timer", 0, 0)]
    assert timer.handler == "old-handler" and not timer.active


def test_actual_guard_expired_budget_never_reports_success(guarded, capsys):
    case = guarded
    namespace = case.guard.main.__globals__
    namespace["signal"] = GuardTimer()
    clock = iter([0.0, 6.0])
    namespace["time"] = SimpleNamespace(monotonic=lambda: next(clock))
    assert case.guard.main(["stage", *case.args]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "node_guard_deadline" in captured.err


def test_actual_guard_alarm_hard_exit_cannot_fall_back_to_success(guarded):
    case = guarded
    namespace = case.guard.main.__globals__
    timer = GuardTimer()
    namespace["signal"] = timer

    def hard_exit(code):
        raise SystemExit(code)

    case.os._exit = hard_exit

    def expire(*_args):
        timer.handler(14, None)

    namespace["stage"] = expire
    with pytest.raises(SystemExit) as error:
        case.guard.main(["stage", *case.args])
    assert error.value.code == 124
    assert timer.handler == "old-handler" and not timer.active


def test_gate_final_node_check_follows_python_identity_and_precedes_log_binding():
    source = RUNNER_SOURCE.read_text()
    python_check = source.index('[[ "$(sha256_text "${PYTHON_IDENTITY_AFTER}")" == "${PYTHON_IDENTITY_SHA256}" ]]')
    node_check = source.index('GATE_NODE_FINAL_PROOF="$(gate_node_guard verify)"')
    log_hash = source.index('LOG_SHA256="$(sha256_file "${RUN_LOG}")"')
    assert python_check < node_check < log_hash
