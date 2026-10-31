"""Local negative contracts; these mocks never stand in for a native proof."""
from copy import deepcopy
from contextlib import nullcontext, redirect_stderr
import hashlib
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import production_activation_host_native as native
from scripts import verify_production_activation_native as activation
from scripts import verify_production_startup_native as startup
from services import admission_fence as gate


@pytest.fixture(autouse=True)
def reset_local_phase_counter(monkeypatch):
    monkeypatch.setattr(native, "PHASE", "initial_validation")
    monkeypatch.setattr(native, "PHASE_SEQUENCE", 0)


def test_imported_cli_delegates_same_arguments_to_canonical_main(monkeypatch):
    arguments, observed = ["--inside", "--unchanged-argument"], []
    monkeypatch.setattr(native, "main", lambda argv: observed.append(argv) or 19)
    assert native.cli(arguments) == 19 and observed == [arguments]
    assert observed[0] is arguments


@pytest.fixture
def private_run_case(tmp_path, monkeypatch):
    import os
    import stat
    root = tmp_path / "inner"
    root.mkdir()
    parent = {"pid": os.getpid() + 1000, "netns": 1, "mntns": 2, "cgroup": "/outer"}
    current = {"netns": 3, "mntns": 4, "cgroup": "/owned/supervisor"}
    plan = {"parent": parent, "cgroup": "/owned", "gateway_custody": True,
            "wrapper_source": {"exact": "source"}, "active_deadline": 580, "deadline": 600}
    info = SimpleNamespace(st_dev=7, st_ino=8, st_mode=stat.S_IFDIR | 0o700, st_uid=0, st_gid=0)
    rows = {"/run": info, str(root / "run"): deepcopy(info)}
    real_lstat, real_resolve = os.lstat, Path.resolve
    monkeypatch.setattr(native.os, "geteuid", lambda: 0)
    monkeypatch.setattr(native.os, "lstat", lambda path: rows[str(path)] if str(path) in rows else real_lstat(path))
    monkeypatch.setattr(Path, "resolve", lambda path, *a, **kw:
                        Path("/run") if str(path) == "/var/run" else real_resolve(path, *a, **kw))
    monkeypatch.setattr(native, "original", lambda path: (deepcopy(plan), {}))
    monkeypatch.setattr(native.host, "proc_identity", lambda pid: current if pid == os.getpid() else parent)
    monkeypatch.setattr(native.host, "file_proof", lambda path: {"exact": "source"})
    deadlines = []
    monkeypatch.setattr(native, "remaining", lambda value: deadlines.append(value) or 10)
    return SimpleNamespace(root=root, plan=plan, current=current, rows=rows, deadlines=deadlines)


def test_fixed_pidfile_requires_real_private_run_and_keeps_both_original_deadlines(private_run_case):
    case = private_run_case
    identity = native._private_run_identity(case.root, case.plan)
    assert identity[:2] == (7, 8)
    assert native._private_run_identity(case.root, case.plan, final=True) == identity
    assert case.deadlines == [580, 600]


@pytest.mark.parametrize("fault", ["network", "mount", "cgroup", "parent", "source", "mode", "uid", "gid", "symlink", "different_bind"])
def test_fixed_pidfile_refuses_namespace_source_or_run_drift(private_run_case, monkeypatch, fault):
    import stat
    case = private_run_case
    if fault == "network":
        case.current["netns"] = case.plan["parent"]["netns"]
    elif fault == "mount":
        case.current["mntns"] = case.plan["parent"]["mntns"]
    elif fault == "cgroup":
        case.current["cgroup"] = "/unrelated/supervisor"
    elif fault == "parent":
        monkeypatch.setattr(native.host, "proc_identity", lambda pid: case.current)
    elif fault == "source":
        monkeypatch.setattr(native.host, "file_proof", lambda path: {"exact": "changed"})
    elif fault == "mode":
        case.rows["/run"].st_mode = stat.S_IFDIR | 0o755
    elif fault in {"uid", "gid"}:
        setattr(case.rows["/run"], "st_" + fault, 1)
    elif fault == "symlink":
        case.rows["/run"].st_mode = stat.S_IFLNK | 0o700
    else:
        case.rows["/run"].st_ino += 1
    with pytest.raises(gate.AdmissionError, match="fixed archive PID"):
        native._private_run_identity(case.root, case.plan)


@pytest.mark.parametrize("fault", ["root", "frozen", "custody", "deadline"])
def test_private_run_authority_cannot_be_enabled_by_flag_or_new_clock(private_run_case, monkeypatch, fault):
    case = private_run_case
    if fault == "root":
        monkeypatch.setattr(native.os, "geteuid", lambda: 1)
    elif fault == "frozen":
        monkeypatch.setattr(native, "original", lambda path: ({"another": "plan"}, {}))
    elif fault == "custody":
        case.plan["gateway_custody"] = False
    else:
        def expired(deadline):
            assert deadline == case.plan["active_deadline"]
            raise gate.AdmissionError("original full-host deadline expired")
        monkeypatch.setattr(native, "remaining", expired)
    with pytest.raises(gate.AdmissionError):
        native._private_run_identity(case.root, case.plan)


@pytest.mark.parametrize("raw", [b"123", b"123\n"])
def test_private_pidfile_uses_unchanged_production_reader_and_exact_actual_pid(private_run_case, monkeypatch, raw):
    from scripts import retained_gateway_configuration as configuration
    case, calls = private_run_case, []
    metadata = (7, 9, 0o100644, 0, 0, 1, len(raw), 10, 11)
    monkeypatch.setattr(configuration, "_small", lambda path, limit: calls.append((path, limit)) or (raw, metadata))
    run = native._private_run_identity(case.root, case.plan)
    proof = native._private_daemon_pidfile(case.root, case.plan, run, 123)
    assert proof == (raw, metadata) and calls == [("/run/docker.pid", 32)]
    assert native._private_daemon_pidfile(case.root, case.plan, run, 123, proof, final=True) == proof
    assert case.deadlines[-1] == 600


@pytest.mark.parametrize("fault", ["wrong_pid", "gid", "metadata", "run", "missing"])
def test_private_pidfile_missing_wrong_or_replaced_still_blocks(private_run_case, monkeypatch, fault):
    from scripts import retained_gateway_configuration as configuration
    case = private_run_case
    run = native._private_run_identity(case.root, case.plan)
    proof = (b"123", (7, 9, 0o100644, 0, 0, 1, 3, 10, 11))
    raw, metadata = proof
    if fault == "wrong_pid":
        raw = b"124"
    elif fault == "gid":
        metadata = (*metadata[:4], 1, *metadata[5:])
    elif fault == "metadata":
        metadata = (*metadata[:-1], 12)
    elif fault == "run":
        case.rows["/run"].st_ino += 1
    def read(path, limit):
        if fault == "missing":
            raise FileNotFoundError("synthetic missing PIDfile")
        return raw, metadata
    monkeypatch.setattr(configuration, "_small", read)
    with pytest.raises((gate.AdmissionError, FileNotFoundError)):
        native._private_daemon_pidfile(case.root, case.plan, run, 123, proof)


@pytest.mark.parametrize("custody", [False, True])
def test_daemon_pidfile_override_is_only_custody_and_rechecks_on_existing_peer(tmp_path, monkeypatch, custody):
    calls, configs = [], []
    plan = {"gateway_custody": custody, "active_deadline": 580, "deadline": 600}
    commands = SimpleNamespace(deadline=580, peer=None)
    process = SimpleNamespace(pid=123, poll=lambda: None)
    logs = SimpleNamespace(check=lambda: calls.append("logs"))
    run, metadata = (1, 2, 3, 0, 0), (b"123", ("fixed",))
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    monkeypatch.setattr(native.host, "daemon_config", lambda *_a: {"pidfile": str(tmp_path / "daemon.pid")})
    monkeypatch.setattr(native, "_private_run_identity", lambda *_a: calls.append("run") or run)
    monkeypatch.setattr(native.os.path, "lexists", lambda path: False)
    monkeypatch.setattr(native, "record", lambda path, data: configs.append((path, data)))
    monkeypatch.setattr(native.host, "launch_daemon", lambda *_a: (process, logs))
    monkeypatch.setattr(native.host, "retain_daemons", lambda **kw: calls.append(("retain", kw)))
    monkeypatch.setattr(native.host, "retainer_confirmed", lambda value: value is process)
    monkeypatch.setattr(native.host, "proc_identity", lambda pid: {"netns": 1, "mntns": 2, "cgroup": "/owned"})
    monkeypatch.setattr(native.host, "check_peer", lambda *_a: calls.append("peer"))
    monkeypatch.setattr(Path, "exists", lambda path: str(path) == "/var/run/docker.sock")
    monkeypatch.setattr(native.host, "wait_until", lambda check, deadline: calls.append(("wait", deadline)) or check())
    def verify(*args, **kwargs):
        calls.append(("pidfile", args[3:], kwargs))
        return metadata
    monkeypatch.setattr(native, "_private_daemon_pidfile", verify)
    def docker(args):
        commands.peer()
        return "{}"
    commands.docker = docker
    assert native.start_daemon(commands, tmp_path, plan, "http://127.0.0.1:3333")[0] is process
    assert configs[0][1]["pidfile"] == ("/run/docker.pid" if custody else str(tmp_path / "daemon.pid"))
    assert ("wait", 110) in calls and ("retain", {"deadline": 580}) in calls
    if custody:
        assert calls.count("run") == 1
        assert ("pidfile", (123,), {}) in calls
        assert ("pidfile", (123, metadata), {"final": False}) in calls
        commands.deadline = 600
        commands.peer()
        assert calls[-1] == ("pidfile", (123, metadata), {"final": True})
        commands.deadline = 601
        with pytest.raises(gate.AdmissionError, match="replace the shared deadline"):
            commands.peer()
    else:
        assert "run" not in calls and not any(isinstance(call, tuple) and call[0] == "pidfile" for call in calls)


def test_existing_private_pidfile_never_reaches_config_or_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(native.host, "daemon_config", lambda *_a: {})
    monkeypatch.setattr(native, "_private_run_identity", lambda *_a: (1, 2))
    monkeypatch.setattr(native.os.path, "lexists", lambda path: True)
    monkeypatch.setattr(native, "record", lambda *_a: pytest.fail("existing PIDfile reached config write"))
    monkeypatch.setattr(native.host, "launch_daemon", lambda *_a: pytest.fail("existing PIDfile launched daemon"))
    with pytest.raises(gate.AdmissionError, match="already exists"):
        native.start_daemon(SimpleNamespace(), tmp_path, {"gateway_custody": True}, "http://127.0.0.1:3333")


@pytest.mark.parametrize("fault", ["name", "spec_absent", "spec_name", "spec_origin", "file"])
def test_cli_refuses_alternate_canonical_module_source_before_delegate(monkeypatch, fault):
    import scripts
    calls = []
    alternate = SimpleNamespace(__name__=native.__name__, __file__=native.__file__,
        __spec__=SimpleNamespace(name=native.__spec__.name, origin=native.__spec__.origin),
        main=lambda argv: calls.append(argv))
    if fault == "name":
        alternate.__name__ = "malicious_alternate"
    elif fault == "spec_absent":
        alternate.__spec__ = None
    elif fault == "spec_name":
        alternate.__spec__.name = "malicious_alternate"
    elif fault == "spec_origin":
        alternate.__spec__.origin = "/untrusted/substitute.py"
    else:
        alternate.__file__ = "/untrusted/substitute.py"
    monkeypatch.setattr(scripts, "production_activation_host_native", alternate)
    with pytest.raises(gate.AdmissionError, match="canonical full-host CLI source required"):
        native.cli([])
    assert calls == []


@pytest.mark.parametrize("inside", [False, True])
def test_real_isolated_script_cli_keeps_original_argument_validation(tmp_path, inside):
    # This executes the actual file with Python's isolated CLI, not a copied
    # dispatcher. Invalid arguments must stop before any native side effect.
    argv = [sys.executable, "-I", "-B", str(Path(native.__file__).resolve())]
    argv += (["--inside"] if inside else []) + ["--invalid-native-argument"]
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True, timeout=15)
    value = json.loads(result.stdout)
    assert result.returncode == 1 and value["status"] == "failed_or_evidence_insufficient"
    assert value["error_sha256"] == hashlib.sha256(b"2").hexdigest()
    assert value["full_activation_verified"] is False and value["production_access"] is False
    assert "error:" in result.stderr and "--repo" in result.stderr
    progress = [json.loads(line) for line in result.stderr.splitlines() if line.startswith('{"event":')]
    assert [row["phase"] for row in progress] == ["script_start", "canonical_import_before", "canonical_import_verified",
                                                "canonical_main_enter", "arguments_before"]
    assert [row["sequence"] for row in progress] == [1, 2, 3, 4, 5]
    assert all(len(json.dumps(row, separators=(",", ":")).encode()) + 1 <= 192 for row in progress)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("inside", [False, True])
def test_real_runpy_cli_uses_canonical_class_for_outer_and_inside(tmp_path, inside):
    # A fresh isolated subprocess exercises the real file's __main__ branch.
    # Only its canonical main body is replaced by a local contract probe; no
    # native command, network, resource observation or production proof runs.
    source = r'''
import json, pathlib, runpy, sys
sys.path.insert(0, sys.argv[1])
from scripts import production_activation_host_native as canonical
from scripts import verify_production_activation_native as activation
from services import admission_fence as gate
expected = sys.argv[3:]
def probe(argv=None, *, _cli_started=None):
    assert argv is None and sys.argv[1:] == expected
    assert isinstance(_cli_started, float) and _cli_started <= canonical.time.monotonic()
    try:
        canonical._arguments(["--invalid-native-argument"])
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("original argument validator was bypassed")
    activation.Harness.__init__ = lambda self, *args, **kwargs: None
    context = object.__new__(canonical.PrivateHostContext)
    context.plan = {"started": 123}
    context.check = lambda: None
    observed = []
    context.check_network = lambda row, owner: observed.append((row, owner))
    harness = activation.ActivationHarness(private_host_context=context)
    assert harness.private_host_context is context and harness.started == 123
    harness.owner = "canonical-owner"
    canonical.require_harness_network(harness, {"network": "unchanged"})
    assert observed == [({"network": "unchanged"}, "canonical-owner")]
    class ForeignContext(canonical.PrivateHostContext):
        pass
    duplicate = object.__new__(sys.modules["__main__"].PrivateHostContext)
    assert type(duplicate).__module__ == "__main__" and type(duplicate) is not canonical.PrivateHostContext
    for fake in (duplicate, object.__new__(ForeignContext), object()):
        try:
            activation.ActivationHarness(private_host_context=fake)
        except gate.AdmissionError as error:
            assert str(error) == "exact private host context required"
        else:
            raise AssertionError("foreign context was accepted")
        harness.private_host_context = fake
        try:
            canonical.require_harness_network(harness, {})
        except gate.AdmissionError as error:
            assert str(error) == "ordinary bridge requires the exact full private-host context"
        else:
            raise AssertionError("foreign network context was accepted")
    print(json.dumps({"canonical_module": type(context).__module__, "inside": "--inside" in expected,
                      "argument_guard": True, "exact_type_guard": True, "native_executed": False}))
    return 23
canonical.main = probe
path = sys.argv[2]
sys.argv = [path, *expected]
try:
    runpy.run_path(path, run_name="__main__")
except SystemExit as error:
    assert error.code == 23
else:
    raise AssertionError("real script entry did not delegate")
'''
    argv = [sys.executable, "-I", "-B", "-c", source, str(native.ROOT), str(Path(native.__file__).resolve())]
    argv += ["--inside"] if inside else []
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value == {"canonical_module": "scripts.production_activation_host_native", "inside": inside,
                     "argument_guard": True, "exact_type_guard": True, "native_executed": False}


def test_ordinary_isolated_import_has_no_cli_clock_or_output(tmp_path):
    source = "import sys,json;sys.path.insert(0,sys.argv[1]);from scripts import production_activation_host_native as n;"
    source += "print(json.dumps({'started':n.CLI_STARTED,'offset':n.CLI_PHASE_OFFSET,'sequence':n.PHASE_SEQUENCE}))"
    result = subprocess.run([sys.executable, "-I", "-B", "-c", source, str(native.ROOT)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout) == {"started": None, "offset": 0, "sequence": 0}


@pytest.mark.parametrize("fault", ["exception", "short_write"])
def test_real_preimport_cli_write_failure_stops_before_canonical_import(tmp_path, fault):
    source = r'''
import os, runpy, sys
def failed_write(fd, data):
    if sys.argv[2] == "short_write":
        return 0
    raise OSError("synthetic fixed progress failure")
os.write = failed_write
try:
    runpy.run_path(sys.argv[1], run_name="__main__")
except (OSError, RuntimeError):
    assert "scripts.production_activation_host_native" not in sys.modules
else:
    raise AssertionError("progress write failure was ignored")
raise SystemExit(17)
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", source, str(Path(native.__file__).resolve()), fault],
                            cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 17 and result.stdout == "" and result.stderr == ""


@pytest.mark.parametrize("at_write", [1, 2])
def test_cli_progress_failure_never_calls_canonical_main(monkeypatch, at_write):
    calls, writes = [], []
    def write(fd, data):
        writes.append((fd, data))
        return 0 if len(writes) == at_write else len(data)
    monkeypatch.setattr(native.os, "write", write)
    monkeypatch.setattr(native, "main", lambda argv: calls.append(argv))
    with pytest.raises(gate.AdmissionError, match="CLI progress write incomplete"):
        native.cli([])
    assert calls == [] and len(writes) == at_write


def test_canonical_main_entry_progress_failure_stops_original_main(monkeypatch):
    calls = []
    monkeypatch.setattr(native.os, "write", lambda *args: 0)
    monkeypatch.setattr(native, "_main", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(native.host, "LIVE_DAEMONS", [])
    with pytest.raises(gate.AdmissionError, match="CLI progress write incomplete"):
        native.main([], _cli_started=100)
    assert calls == []


@pytest.mark.parametrize("at_write", [1, 2])
def test_argument_progress_failure_never_enters_native_operation(tmp_path, monkeypatch, capsys, at_write):
    writes, calls, native_calls = [], [], []
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    def write(fd, data):
        writes.append((fd, data))
        return 0 if len(writes) == at_write else len(data)
    monkeypatch.setattr(native.os, "write", write)
    monkeypatch.setattr(native, "_arguments", lambda argv: calls.append(argv) or SimpleNamespace(output=tmp_path, inside=False))
    monkeypatch.setattr(native, "outer_execute", lambda *args: native_calls.append(args))
    assert native._main([], _cli_started=90) == 1
    value = json.loads(capsys.readouterr().out)
    assert value["full_activation_verified"] is False and native_calls == []
    assert len(calls) == at_write - 1 and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("value", [True, False, "100", float("nan"), float("inf"), 101, -500, -501])
def test_cli_start_rejects_future_invalid_or_expired_origin_before_arguments(monkeypatch, value):
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    monkeypatch.setattr(native, "_arguments", lambda argv: pytest.fail("invalid start reached arguments"))
    with pytest.raises(gate.AdmissionError, match="original native CLI start required"):
        native._main([], _cli_started=value)


def test_original_cli_start_survives_import_and_argument_delay(tmp_path, monkeypatch, capsys):
    root, observed = tmp_path / "originals", []
    monkeypatch.setattr(native.time, "monotonic", lambda: 130)
    monkeypatch.setattr(native, "_arguments", lambda argv: SimpleNamespace(output=root, inside=False))
    monkeypatch.setattr(native.host, "private_directory", lambda path, **kwargs: path.mkdir(mode=0o700))
    monkeypatch.setattr(native, "outer_execute", lambda args, output, started: observed.append(started) or failed_child_result())
    monkeypatch.setattr(native, "safe_inventory", lambda *args: {"complete": False})
    assert native._main([], _cli_started=100) == 1
    value = json.loads(capsys.readouterr().out)
    assert observed == [100] and value["elapsed_seconds"] == 30


def test_cli_and_runtime_phase_share_original_total_line_budget(tmp_path, monkeypatch):
    rows = []
    monkeypatch.setattr(native, "record", lambda *args, **kwargs: {})
    monkeypatch.setattr(native.os, "write", lambda fd, data: rows.append((fd, data)) or len(data))
    for index in range(64):
        if index % 2:
            native.phase(tmp_path, "child_observation")
        else:
            native.cli_progress("arguments_before")
    with pytest.raises(gate.AdmissionError, match="phase observation bound"):
        native.cli_progress("arguments_verified")
    assert len(rows) == 64 and all(fd == 2 and len(data) <= 192 for fd, data in rows)


@pytest.mark.parametrize("value", ["PATH-TOKEN-MUST-NOT-ESCAPE", None, []])
def test_cli_progress_refuses_unknown_or_malicious_phase_without_write(monkeypatch, value):
    monkeypatch.setattr(native.os, "write", lambda *args: pytest.fail("untrusted CLI progress escaped"))
    with pytest.raises(gate.AdmissionError, match="unknown native CLI phase"):
        native.cli_progress(value)
    assert native.PHASE_SEQUENCE == 0


@pytest.mark.parametrize("deadline", [True, False, None, "600", float("nan"), float("inf"), -1, 0, 701])
def test_original_deadline_rejects_invalid_or_replaced_values(monkeypatch, deadline):
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    with pytest.raises(gate.AdmissionError):
        native.remaining(deadline)


def test_deadline_is_not_reset_between_phases(monkeypatch):
    clock = [100]
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    assert native.remaining(600) == 500
    clock[0] = 599
    assert native.remaining(600) == 1
    clock[0] = 601
    with pytest.raises(gate.AdmissionError):
        native.remaining(600)


def test_fixed_phase_progress_reaches_process_stderr_despite_python_redirect(tmp_path, capfd):
    hidden = StringIO()
    with redirect_stderr(hidden):
        native.phase(tmp_path, "dependency_inspect")
    progress = json.loads(capfd.readouterr().err)
    assert progress == {"event": "full_activation_phase", "phase": "dependency_inspect", "sequence": 1}
    assert hidden.getvalue() == ""
    assert native.host.read_original_json(tmp_path / "PHASE_001.json")["phase"] == "dependency_inspect"


@pytest.mark.parametrize("name", ["secret-path-or-token-MUST-NOT-LEAK", [], None])
def test_unknown_phase_is_rejected_before_any_progress_or_original(tmp_path, capfd, name):
    with pytest.raises(gate.AdmissionError, match="unknown native phase"):
        native.phase(tmp_path, name)
    assert capfd.readouterr().err == "" and list(tmp_path.iterdir()) == []


def test_phase_progress_remains_count_and_byte_bounded(tmp_path, monkeypatch):
    rows = []
    monkeypatch.setattr(native, "record", lambda *a, **k: {})
    monkeypatch.setattr(native.os, "write", lambda fd, data: rows.append((fd, data)) or len(data))
    for _ in range(64):
        native.phase(tmp_path, "child_observation")
    with pytest.raises(gate.AdmissionError, match="phase observation bound"):
        native.phase(tmp_path, "cleanup")
    assert len(rows) == 64 and all(fd == 2 and len(data) <= 192 for fd, data in rows)


def test_short_progress_write_is_failure_with_original_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(native.os, "write", lambda fd, data: len(data) - 1)
    with pytest.raises(gate.AdmissionError, match="progress write incomplete"):
        native.phase(tmp_path, "namespace_launch")
    assert (tmp_path / "PHASE_001.json").is_file()


def test_outer_supervisor_caps_precede_download_export_and_other_heavy_work(tmp_path, monkeypatch):
    seen = []
    class Stop(Exception):
        pass
    monkeypatch.setattr(native, "cgroup_setup", lambda owner: seen.append("caps_installed") or "/owned")
    monkeypatch.setattr(native, "FullCommands", lambda *a: seen.append("commands_created") or object())
    monkeypatch.setattr(native, "phase", lambda *a: None)
    def first_heavy(*args):
        seen.append("first_heavy_operation")
        raise Stop
    monkeypatch.setattr(native.host, "host_tools", first_heavy)
    with pytest.raises(Stop):
        native.outer_plan(SimpleNamespace(gateway_custody=False), tmp_path, 100)
    assert seen == ["caps_installed", "commands_created", "first_heavy_operation"]


def network_proof():
    holder = {"netns": 17}
    proof = {"holder": holder, "bridge": "afa" + "a" * 10, "network_id": "f" * 64,
             "network_name": "synthetic", "owner": "a" * 32}
    row = {"Id": proof["network_id"], "Name": "synthetic", "Internal": False, "Driver": "bridge", "Scope": "local",
           "Labels": {native.LABEL: proof["owner"]}, "Options": {"com.docker.network.bridge.name": proof["bridge"]},
           "Containers": {"c" * 64: {}}}
    return proof, row


@pytest.mark.parametrize("field,value", [("Id", "b" * 64), ("Name", "other"), ("Internal", True), ("Internal", None),
    ("Driver", "host"), ("Scope", "swarm"), ("Labels", {}), ("Options", {})])
def test_private_network_still_requires_every_actual_boundary(tmp_path, monkeypatch, field, value):
    proof, row = network_proof()
    row[field] = value
    monkeypatch.setattr(native.host, "namespace_snapshot", lambda *a, **k: pytest.fail("must reject before observation"))
    with pytest.raises(gate.AdmissionError):
        native.validate_network(proof, row, proof["owner"], SimpleNamespace(root=tmp_path, sequence=1))


def test_private_network_records_unique_route_observation(tmp_path, monkeypatch):
    proof, row = network_proof()
    seen = []
    def snapshot(commands, **kwargs):
        seen.append(kwargs["evidence_path"])
        commands.sequence += 3
        return {"netns": 17}
    monkeypatch.setattr(native.host, "namespace_snapshot", snapshot)
    commands = SimpleNamespace(root=tmp_path, sequence=1)
    native.validate_network(proof, row, proof["owner"], commands)
    native.validate_network(proof, row, proof["owner"], commands)
    assert seen == [tmp_path / "NAMESPACE_CHECK_0001.json", tmp_path / "NAMESPACE_CHECK_0004.json"]


def test_private_network_passes_registered_endpoint_count(tmp_path, monkeypatch):
    proof, row = network_proof()
    row["Containers"] = {format(i, "064x"): {} for i in range(7)}
    seen = []
    monkeypatch.setattr(native.host, "namespace_snapshot", lambda commands, **kw: seen.append(kw["registered"]) or {"netns": 17})
    native.validate_network(proof, row, proof["owner"], SimpleNamespace(root=tmp_path, sequence=1))
    assert seen == [7]


@pytest.mark.parametrize("endpoints", [None, [], {"not-an-id": {}}])
def test_private_network_requires_exact_endpoint_ids(tmp_path, monkeypatch, endpoints):
    proof, row = network_proof()
    row["Containers"] = endpoints
    monkeypatch.setattr(native.host, "namespace_snapshot", lambda *a, **k: pytest.fail("must not snapshot"))
    with pytest.raises(gate.AdmissionError, match="endpoints"):
        native.validate_network(proof, row, proof["owner"], SimpleNamespace(root=tmp_path, sequence=1))


@pytest.mark.parametrize("candidate", [None, object(), SimpleNamespace(check_network=lambda *_: None)])
def test_ordinary_bridge_never_accepts_duck_typed_context(candidate):
    with pytest.raises(gate.AdmissionError):
        native.require_harness_network(SimpleNamespace(private_host_context=candidate), {})


def identity_setup(monkeypatch):
    parent = {"pid": 1, "ppid": 0, "starttime": 1, "netns": 10, "mntns": 11, "cgroup": "/owned/supervisor"}
    holder = {"pid": 2, "ppid": 1, "starttime": 2, "netns": 12, "mntns": 13, "cgroup": "/owned/supervisor"}
    daemon = {**holder, "pid": 3, "ppid": 2, "starttime": 3}
    plan = {"deadline": 700, "active_deadline": 680, "parent": parent, "cgroup": "/owned", "wrapper_source": {"sha256": "a" * 64}}
    actual = {1: deepcopy(parent), 2: deepcopy(holder), 3: deepcopy(daemon)}
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    monkeypatch.setattr(native.os, "getpid", lambda: 2)
    monkeypatch.setattr(native.host, "proc_identity", lambda pid: actual[pid])
    monkeypatch.setattr(native.host, "file_proof", lambda *a, **k: {"sha256": "a" * 64})
    monkeypatch.setattr(native.host, "check_peer", lambda pid, expected: gate._require(actual[pid] == expected, "peer drift"))
    monkeypatch.setattr(native, "resource_check", lambda p: None)
    return plan, holder, daemon, actual


def test_private_host_binds_live_parent_holder_daemon_and_source(monkeypatch):
    plan, holder, daemon, _actual = identity_setup(monkeypatch)
    native.process_check(plan, holder, daemon)


@pytest.mark.parametrize("pid,key,value", [(1, "starttime", 7), (2, "netns", 10), (2, "mntns", 11),
    (2, "cgroup", "/other"), (3, "netns", 10), (3, "pid", 30)])
def test_private_host_identity_drift_never_authorizes_mutation(monkeypatch, pid, key, value):
    plan, holder, daemon, actual = identity_setup(monkeypatch)
    actual[pid][key] = value
    with pytest.raises(gate.AdmissionError):
        native.process_check(plan, holder, daemon)


def test_context_final_uses_same_total_deadline_not_new_budget(monkeypatch):
    plan, holder, daemon, _actual = identity_setup(monkeypatch)
    monkeypatch.setattr(native.time, "monotonic", lambda: 690)
    with pytest.raises(gate.AdmissionError):
        native.process_check(plan, holder, daemon)
    native.process_check(plan, holder, daemon, final=True)
    monkeypatch.setattr(native.time, "monotonic", lambda: 701)
    with pytest.raises(gate.AdmissionError):
        native.process_check(plan, holder, daemon, final=True)


@pytest.mark.parametrize("fault", [None, "memory", "pids", "owner", "image", "name", "cgroup", "host_netns", "host_mntns"])
def test_running_inventory_binds_exact_pg_caps_owner_image_name_and_placement(tmp_path, monkeypatch, fault):
    image = "sha256:" + "a" * 64
    row = {"Id": "b" * 64, "Name": "/owned-pg", "Image": image,
           "State": {"Pid": 123, "OOMKilled": False}, "Config": {"Labels": {native.LABEL: "c" * 32}},
           "HostConfig": {"Memory": 384 * 1024**2, "MemorySwap": 384 * 1024**2, "PidsLimit": 64, "NanoCpus": 10**9}}
    process = {"cgroup": "/owned/containers/postgres", "netns": 20, "mntns": 30}
    if fault == "memory":
        row["HostConfig"].update(Memory=768 * 1024**2, MemorySwap=768 * 1024**2)
    elif fault == "pids":
        row["HostConfig"]["PidsLimit"] = 256
    elif fault == "owner":
        row["Config"]["Labels"][native.LABEL] = "d" * 32
    elif fault == "image":
        row["Image"] = "sha256:" + "d" * 64
    elif fault == "name":
        row["Name"] = "/unowned-pg"
    elif fault == "cgroup":
        process["cgroup"] = "/owned/supervisor"
    elif fault == "host_netns":
        process["netns"] = 10
    elif fault == "host_mntns":
        process["mntns"] = 11
    context = native.PrivateHostContext(tmp_path, {"cgroup": "/owned", "images": {}, "references": [], "sender": {"Id": image}},
        SimpleNamespace(docker=lambda *a: "b" * 64, inspect=lambda *a: row), {"netns": 10, "mntns": 11}, {})
    monkeypatch.setattr(context, "check", lambda: None)
    monkeypatch.setattr(native.host, "proc_identity", lambda pid: process)
    harness = SimpleNamespace(owner="c" * 32, names={"pg": "owned-pg"}, images={"pg": image})
    if fault:
        with pytest.raises(gate.AdmissionError):
            context.check_running_inventory(harness)
    else:
        context.check_running_inventory(harness)


def test_frozen_proof_is_exclusive_and_read_only(tmp_path):
    path = tmp_path / "proof.json"
    proof = native.record(path, {"schema": 1}, sealed=True)
    assert native.original(path, proof["sha256"])[0] == {"schema": 1}
    assert path.stat().st_mode & 0o777 == 0o400
    with pytest.raises(FileExistsError):
        native.record(path, {"schema": 2}, sealed=True)
    with pytest.raises(gate.AdmissionError):
        native.original(path, "b" * 64)


def test_sealed_publication_is_invisible_until_complete_mode_and_single_link(tmp_path, monkeypatch):
    path = tmp_path / "BUILD_REQUEST.json"
    real = native.rename_noreplace
    seen = []
    def publish(source, target):
        assert not target.exists() and target == path
        info = source.stat()
        assert info.st_mode & 0o777 == 0o400 and info.st_nlink == 1
        assert json.loads(source.read_bytes()) == {"status": "complete"}
        seen.append("complete_sealed_inode_before_atomic_publish")
        real(source, target)
    monkeypatch.setattr(native, "rename_noreplace", publish)
    native.record(path, {"status": "complete"}, sealed=True)
    assert seen == ["complete_sealed_inode_before_atomic_publish"]
    assert path.stat().st_nlink == 1


def test_sealed_file_fsync_failure_leaves_private_original_not_visible_final(tmp_path, monkeypatch):
    path = tmp_path / "BUILD_RESULT.json"
    real, calls = native.os.fsync, []
    def sync(fd):
        calls.append(fd)
        if len(calls) == 3:
            raise OSError("sealed original fsync failed")
        return real(fd)
    monkeypatch.setattr(native.os, "fsync", sync)
    with pytest.raises(OSError):
        native.record(path, {"status": "complete"}, sealed=True)
    assert not path.exists()
    assert len(list(tmp_path.glob("BUILD_RESULT.json.sealed-prepared-*"))) == 1


@pytest.mark.parametrize("mutation", ["extra", "source", "tag", "sha"])
def test_build_ipc_rejects_unreviewed_request_before_build(tmp_path, monkeypatch, mutation):
    request = {"schema": 1, "contract_sha256": "a" * 64, "source": {"source_sha": "b" * 40},
               "image": "ai-filter-activation-native:fixed-project"}
    plan = {"repo": str(tmp_path / "repo"), "source_identity": deepcopy(request["source"])}
    contract = {"review": {"project": "fixed-project"}}
    if mutation == "extra":
        request["argv"] = ["arbitrary"]
    if mutation == "source":
        request["source"] = {"source_sha": "c" * 40}
    if mutation == "tag":
        request["image"] = "my-ai-filter:.126"
    if mutation == "sha":
        request["contract_sha256"] = "d" * 64
    monkeypatch.setattr(native, "original", lambda path: (request, {"sha256": "e" * 64}))
    monkeypatch.setattr(native.private, "load", lambda path, root, **kwargs: (contract, {}, "a" * 64))
    with pytest.raises(gate.AdmissionError):
        native.checked_build_request(tmp_path, plan)


def test_exact_build_ipc_reads_only_fixed_contract(tmp_path, monkeypatch):
    request = {"schema": 1, "contract_sha256": "a" * 64, "source": {"source_sha": "b" * 40},
               "image": "ai-filter-activation-native:fixed-project"}
    monkeypatch.setattr(native, "original", lambda path: (request, {"sha256": "e" * 64}))
    seen = []
    def load(path, repo, **kwargs):
        seen.append((path, repo, kwargs))
        return {"review": {"project": "fixed-project"}}, {}, "a" * 64
    monkeypatch.setattr(native.private, "load", load)
    native.checked_build_request(tmp_path, {"repo": str(tmp_path), "source_identity": request["source"]})
    assert seen == [(tmp_path / "inner/activation/receiver/sealed/contract.json", tmp_path, {"require_fresh": True})]


def child_observation_fixture(tmp_path, monkeypatch, result=None):
    inner = tmp_path / "inner"
    inner.mkdir(mode=0o700)
    for name in ("inner.stdout", "inner.stderr"):
        (tmp_path / name).touch(mode=0o600)
    if result is not None:
        native.record(inner / "INNER_RESULT.json", result, sealed=True)
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    monkeypatch.setattr(native, "peer_identity", lambda: {"pid": 1})
    return {"deadline": 600, "active_deadline": 580, "outer_peer": {"pid": 1}}


def failed_child_result():
    return {"status": "failed_or_evidence_insufficient", "phase": "dependency_pull",
            "error_type": "ProofError", "error_sha256": "a" * 64,
            "retained_live_daemon": True, "activation_result": None, "daemon_stop": None}


def test_sealed_failed_terminal_returns_while_original_daemon_holder_is_live(tmp_path, monkeypatch):
    result = failed_child_result()
    plan = child_observation_fixture(tmp_path, monkeypatch, result)
    (tmp_path / "BUILD_REQUEST.json").touch(mode=0o400)
    process = SimpleNamespace(poll=lambda: None, send_signal=lambda *a: pytest.fail("never signal holder"))
    monkeypatch.setattr(native.time, "sleep", lambda *a: pytest.fail("must not wait after sealed failure"))
    monkeypatch.setattr(native, "build_requested_candidate", lambda *a: pytest.fail("no build after terminal failure"))
    assert native.observe_child(process, tmp_path, plan, None) == result


@pytest.mark.parametrize("state", ["complete_unsealed", "partial_unsealed", "partial_sealed", "broken_link"])
def test_partial_or_nonsealed_terminal_never_starts_another_action(tmp_path, monkeypatch, state):
    plan = child_observation_fixture(tmp_path, monkeypatch)
    path = tmp_path / "inner/INNER_RESULT.json"
    if state == "broken_link":
        path.symlink_to(tmp_path / "absent")
    else:
        path.write_text(json.dumps(failed_child_result()) if state == "complete_unsealed" else '{"status":')
        path.chmod(0o400 if state == "partial_sealed" else 0o600)
    monkeypatch.setattr(native.time, "sleep", lambda *a: pytest.fail("no terminal-read retry"))
    monkeypatch.setattr(native, "build_requested_candidate", lambda *a: pytest.fail("cannot build on invalid terminal"))
    with pytest.raises((ValueError, RuntimeError)):
        native.observe_child(SimpleNamespace(poll=lambda: None), tmp_path, plan, None)


def test_passed_terminal_cannot_replace_real_process_exit(tmp_path, monkeypatch):
    plan = child_observation_fixture(tmp_path, monkeypatch, {"status": "passed", "activation_result": {"status": "passed"}})
    clock, waits = [100], []
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    def wait(seconds):
        waits.append(seconds)
        clock[0] = 601
    monkeypatch.setattr(native.time, "sleep", wait)
    with pytest.raises(gate.AdmissionError, match="deadline expired"):
        native.observe_child(SimpleNamespace(poll=lambda: None), tmp_path, plan, None)
    assert waits == [0.05]


@pytest.mark.parametrize("returncode", [0, 1])
def test_passed_terminal_still_requires_zero_exit_and_actual_sealed_candidate_build(tmp_path, monkeypatch, returncode):
    plan = child_observation_fixture(tmp_path, monkeypatch, {"status": "passed", "activation_result": {"status": "passed"}})
    with pytest.raises(gate.AdmissionError, match="sealed candidate build" if returncode == 0 else "child failed"):
        native.observe_child(SimpleNamespace(poll=lambda: returncode, returncode=returncode), tmp_path, plan, None)


def test_zero_exit_with_success_and_original_candidate_path_still_passes(tmp_path, monkeypatch):
    plan = child_observation_fixture(tmp_path, monkeypatch)
    (tmp_path / "BUILD_REQUEST.json").touch(mode=0o400)
    state = [None, 0]
    expected = {"status": "passed", "activation_result": {"status": "passed"}, "daemon_stop": {"status": "stopped"}}
    def build(*args):
        native.record(tmp_path / "BUILD_RESULT.json", {"status": "built"}, sealed=True)
        native.record(tmp_path / "inner/INNER_RESULT.json", expected, sealed=True)
    monkeypatch.setattr(native, "build_requested_candidate", build)
    monkeypatch.setattr(native.time, "sleep", lambda *a: None)
    process = SimpleNamespace(poll=lambda: state.pop(0), returncode=0)
    assert native.observe_child(process, tmp_path, plan, None) == expected


@pytest.mark.parametrize("custody", [False, True])
def test_outer_failure_preserves_live_holder_without_any_signal(tmp_path, monkeypatch, custody):
    result = failed_child_result()
    plan = {"deadline": 600, "active_deadline": 580, "outer_peer": {"pid": 1}, "source_identity": {},
            "wrapper_source": native.host.file_proof(Path(native.__file__)), "gateway_custody": custody}
    args = SimpleNamespace(repo=tmp_path, source=tmp_path, binary=tmp_path, image_id="sha256:" + "a" * 64,
                           gateway_custody=custody)
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    monkeypatch.setattr(native, "outer_plan", lambda *a: (plan, SimpleNamespace(env={})))
    monkeypatch.setattr(native, "peer_identity", lambda: {"pid": 1})
    monkeypatch.setattr(native.private, "source_identity", lambda *a: {})
    def launch(argv, **kwargs):
        assert argv.count("--gateway-custody") == int(custody)
        native.record(tmp_path / "inner/INNER_RESULT.json", result, sealed=True)
        return SimpleNamespace(poll=lambda: None, pid=123, send_signal=lambda *a: pytest.fail("never signal holder"))
    monkeypatch.setattr(native.subprocess, "Popen", launch)
    assert native.outer_execute(args, tmp_path, 100) == result
    receipt = native.host.read_original_json(tmp_path / "OUTER_CHILD_UNCONFIRMED.json")
    assert receipt["status"] == "retained_unconfirmed" and receipt["forced_kill"] is False


@pytest.mark.parametrize("state", ["uncertain", "running"])
def test_unknown_or_running_resources_never_authorize_daemon_signal(tmp_path, monkeypatch, state):
    context = SimpleNamespace(root=tmp_path, check=lambda **kw: None, plan={"deadline": 600},
        commands=SimpleNamespace(deadline=580, docker=lambda args: "a" * 64 if state == "running" else "", uncertain_mutation=state == "uncertain"))
    process = SimpleNamespace(send_signal=lambda *_: pytest.fail("must retain unknown resources"))
    with pytest.raises(gate.AdmissionError):
        native.stop_daemon(context, process, None)


def test_transfer_write_failure_preserves_original_and_sticky_unknown(tmp_path, monkeypatch):
    commands = native.FullCommands(tmp_path, 600)
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    class Failure(Exception):
        pass
    original = Failure("original capture failure")
    process = SimpleNamespace(poll=lambda: None, terminate=lambda: None)
    monkeypatch.setattr(native.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(commands, "_capture", lambda *a, **k: (_ for _ in ()).throw(original))
    monkeypatch.setattr(native, "record", lambda *a, **k: (_ for _ in ()).throw(OSError("evidence full")))
    with pytest.raises(Failure) as caught:
        commands.docker(["image", "save", "sha256:" + "a" * 64], transfer=True, output_path=tmp_path / "image.tar")
    assert caught.value is original and commands.uncertain_mutation
    assert any("evidence failed" in note for note in caught.value.__notes__)


def test_transfer_client_term_race_preserves_capture_failure_and_receipt(tmp_path, monkeypatch):
    commands = native.FullCommands(tmp_path, 600)
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    original = RuntimeError("original capture failure")
    process = SimpleNamespace(poll=lambda: None, terminate=lambda: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(native.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(commands, "_capture", lambda *a, **k: (_ for _ in ()).throw(original))
    with pytest.raises(RuntimeError) as caught:
        commands.docker(["image", "save", "sha256:" + "a" * 64], transfer=True, output_path=tmp_path / "image.tar")
    assert caught.value is original and commands.uncertain_mutation
    assert native.host.read_original_json(tmp_path / "command-0001.json")["client_term_error_type"] == "ProcessLookupError"


@pytest.mark.parametrize("failure_point", ["launch_registered_before_return", "stdout", "fsync"])
def test_outermost_guard_holds_original_daemon_fds_on_every_failure(monkeypatch, failure_point):
    class Held(Exception):
        pass
    calls = []
    monkeypatch.setattr(native.host, "LIVE_DAEMONS", [(SimpleNamespace(poll=lambda: None), None, None, None)])
    monkeypatch.setattr(native, "_main", lambda argv: (_ for _ in ()).throw(OSError(failure_point)))
    def paused():
        calls.append("original_fd_holder_preserved")
        raise Held
    monkeypatch.setattr(native.signal, "pause", paused)
    with pytest.raises(Held):
        native.main([])
    assert calls == ["original_fd_holder_preserved"]


def test_safe_profile_never_forwards_unrecognized_text():
    evil = "secret-password-DO-NOT-FORWARD"
    value = {"status": evil, "source_sha": evil, "last_stage": evil, "Env": [evil], "failure": {"type": evil, "message_sha256": evil},
             "evidence_errors": [evil], "elapsed_seconds": evil}
    serialized = json.dumps(native.safe_activation(value))
    assert evil not in serialized and "Env" not in serialized
    assert native.safe_activation(value)["status"] == "unrecognized"


def test_actual_stage_allowlist_matches_frozen_source_not_arbitrary_text():
    import ast
    from scripts import production_gateway_custody_native as custody
    trees = [ast.parse(Path(module.__file__).read_text()) for module in (activation, custody)]
    actual = {node.args[0].value for tree in trees for node in ast.walk(tree) if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute) and node.func.attr == "stage"
              and node.args and isinstance(node.args[0], ast.Constant)}
    assert actual == native.ACTIVATION_STAGES
    stage = "actual_fixed_gateway_switch_while_closed"
    assert native.safe_activation({"last_stage": stage})["last_stage"] == stage


def test_unknown_error_class_is_not_forwarded():
    evil = type("SecretPasswordMustNotAppear", (Exception,), {})
    assert native.error_type(evil()) == "unrecognized_exception"


@pytest.mark.parametrize("mode", [False, True])
def test_inner_frozen_mode_reaches_actual_activation_without_new_budget(tmp_path, monkeypatch, mode):
    from scripts import production_offline_registry as registry
    observed, contexts = [], []
    plan = {"active_deadline": 580, "deadline": 600, "bundle": {}, "parent": {}, "repo": str(tmp_path),
            "source": str(tmp_path), "binary": str(tmp_path), "sender": {"Id": "sha256:" + "a" * 64},
            "gateway_custody": mode}
    monkeypatch.setattr(native.os, "setsid", lambda: None)
    monkeypatch.setattr(native.host, "proc_identity", lambda *_a: {"netns": 10})
    monkeypatch.setattr(native.host, "restrict_private_ipv6", lambda *_a: None)
    monkeypatch.setattr(native, "prepare_mounts", lambda *_a: None)
    monkeypatch.setattr(native, "phase", lambda *_a: None)
    monkeypatch.setattr(native, "record", lambda *_a, **_k: None)
    monkeypatch.setattr(native, "resource_check", lambda *_a: {})
    monkeypatch.setattr(native, "load_dependencies", lambda *_a: None)
    monkeypatch.setattr(native, "FullCommands", lambda *_a: SimpleNamespace())
    monkeypatch.setattr(native.PrivateHostContext, "check", lambda *_a, **_k: None)
    monkeypatch.setattr(native, "start_daemon", lambda *_a: (object(), {}, None))
    monkeypatch.setattr(native, "stop_daemon", lambda *_a: {"status": "stopped"})
    monkeypatch.setattr(registry, "serve_bundle", lambda *_a, **_k: nullcontext(SimpleNamespace(url="http://127.0.0.1:3333")))
    monkeypatch.setattr(native.native_deadline, "scope", lambda *args: contexts.append(args) or nullcontext())
    def execute(*args, **kwargs):
        observed.append((args, kwargs))
        return {"status": "passed", "gateway_custody": kwargs["gateway_custody"]}
    monkeypatch.setattr(activation, "execute", execute)
    result = native.inner_execute(tmp_path, plan)
    assert result["status"] == "passed" and result["activation_result"]["gateway_custody"] is mode
    assert contexts == [(580, 600)] and len(observed) == 1
    assert observed[0][0] == (tmp_path, tmp_path, plan["sender"]["Id"], tmp_path, tmp_path / "activation")
    assert type(observed[0][1]["private_host_context"]) is native.PrivateHostContext
    assert observed[0][1]["gateway_custody"] is mode


@pytest.mark.parametrize("mode", [None, 0, 1, "true", [], {}])
def test_inner_missing_or_non_boolean_frozen_mode_stops_before_native_operations(monkeypatch, mode):
    monkeypatch.setattr(native.os, "setsid", lambda: pytest.fail("invalid mode reached namespace"))
    with pytest.raises(gate.AdmissionError, match="frozen native gateway custody"):
        native.inner_execute(Path("/unused"), {"gateway_custody": mode})


@pytest.mark.parametrize("mode", [None, 0, 1, "true", [], {}])
def test_outer_non_boolean_mode_stops_before_host_or_cgroup_operations(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(native, "cgroup_setup", lambda *_a: pytest.fail("invalid mode reached cgroups"))
    with pytest.raises(gate.AdmissionError, match="explicit native gateway custody"):
        native.outer_plan(SimpleNamespace(gateway_custody=mode), tmp_path, 100)


@pytest.mark.parametrize("cli_mode,plan_mode", [(False, True), (True, False), (False, 0), (True, 1)])
def test_inner_cli_mode_must_exactly_match_sealed_plan(tmp_path, monkeypatch, capsys, cli_mode, plan_mode):
    root = tmp_path / "inner"
    root.mkdir(mode=0o700)
    image_id = "sha256:" + "a" * 64
    args = SimpleNamespace(output=root, inside=True, repo=tmp_path, source=tmp_path,
                           binary=tmp_path, image_id=image_id, gateway_custody=cli_mode)
    plan = {"repo": str(tmp_path), "source": str(tmp_path), "binary": str(tmp_path),
            "sender": {"Id": image_id}, "gateway_custody": plan_mode,
            "wrapper_source": native.host.file_proof(Path(native.__file__))}
    monkeypatch.setattr(native, "_arguments", lambda *_a: args)
    monkeypatch.setattr(native.private, "private_directory", lambda *_a: None)
    monkeypatch.setattr(native, "original", lambda *_a: (plan, {}))
    monkeypatch.setattr(native, "inner_execute", lambda *_a: pytest.fail("mismatch reached native activation"))
    monkeypatch.setattr(native, "safe_inventory", lambda *_a: {"complete": False})
    assert native._main([]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "failed_or_evidence_insufficient"
    assert result["error_sha256"] == hashlib.sha256(b"inner frozen invocation differs").hexdigest()


@pytest.mark.parametrize("value,expected", [(False, False), (True, True), (1, None), ("true", None), (None, None)])
def test_safe_activation_reports_only_boolean_custody_mode(value, expected):
    assert native.safe_activation({"gateway_custody": value})["gateway_custody"] is expected


def test_public_failure_locations_retain_source_line_without_runtime_text():
    failure = {"type": "RuntimeError", "locations": [{
        "file": "production_gateway_custody_native.py", "function": "TOKEN=never-export", "line": 159}]}
    result = native.safe_activation({"failure": failure})
    assert result["failure_locations"] == [{"file": "production_gateway_custody_native.py", "line": 159}]
    assert "never-export" not in json.dumps(result)


@pytest.mark.parametrize("change", [
    {"file": "../secrets.py"}, {"file": "/private/secrets.py"}, {"file": "TOKEN_never_export.py"},
    {"line": True}, {"line": 0}, {"line": 20001}, {"unexpected": "secret"},
])
def test_public_failure_locations_reject_unreviewed_or_malformed_fields(change):
    row = {"file": "production_gateway_custody_native.py", "function": "prepare_and_seal", "line": 159}
    row.update(change)
    assert native.safe_failure_locations([row]) is None


def test_public_failure_locations_are_bounded_and_not_execution_proof():
    row = {"file": "production_gateway_custody_native.py", "function": "prepare_and_seal", "line": 159}
    assert native.safe_failure_locations([row] * 32) == [{"file": row["file"], "line": 159}] * 32
    assert native.safe_failure_locations([row] * 33) is None
    assert native.safe_failure_locations(None) is None
    assert native.safe_failure_locations([]) == []


def test_failed_terminal_summary_reports_safe_child_phase_without_waiting_for_holder(tmp_path, monkeypatch, capsys):
    root = tmp_path / "private-originals"
    monkeypatch.setattr(native, "_arguments", lambda argv: SimpleNamespace(output=root, inside=False))
    monkeypatch.setattr(native.host, "private_directory", lambda path, **kwargs: path.mkdir(mode=0o700))
    def failed(*args):
        assert native.host.read_original_json(root / "PHASE_001.json")["phase"] == "initial_validation"
        return failed_child_result()
    monkeypatch.setattr(native, "outer_execute", failed)
    monkeypatch.setattr(native, "safe_inventory", lambda *args: {"complete": False})
    assert native._main([]) == 1
    value = json.loads(capsys.readouterr().out)
    assert value["phase"] == "dependency_pull" and value["error_type"] == "ProofError"
    assert value["error_sha256"] == "a" * 64 and value["inner_result"]["retained_live_daemon"] is True
    assert value["full_activation_verified"] is False


def test_unrecognized_terminal_phase_and_error_cannot_escape_safe_summary(tmp_path, monkeypatch, capsys):
    root = tmp_path / "private-originals"
    secret = "PRIVATE-PATH-TOKEN-MUST-NOT-ESCAPE"
    result = {**failed_child_result(), "phase": secret, "error_type": secret, "error_sha256": secret, "unknown": secret}
    monkeypatch.setattr(native, "_arguments", lambda argv: SimpleNamespace(output=root, inside=False))
    monkeypatch.setattr(native.host, "private_directory", lambda path, **kwargs: path.mkdir(mode=0o700))
    monkeypatch.setattr(native, "outer_execute", lambda *args: result)
    monkeypatch.setattr(native, "safe_inventory", lambda *args: {"complete": False})
    assert native._main([]) == 1
    text = capsys.readouterr().out
    value = json.loads(text)
    assert secret not in text and value["inner_result"]["phase"] is None
    assert value["inner_result"]["error_type"] is None and value["error_sha256"] is None


def test_failed_inventory_keeps_minimal_phase_without_exception_text(tmp_path, monkeypatch, capsys):
    root = tmp_path / "private-originals"
    monkeypatch.setattr(native, "_arguments", lambda argv: SimpleNamespace(output=root, inside=False))
    monkeypatch.setattr(native.host, "private_directory", lambda path, **kwargs: path.mkdir(mode=0o700))
    secret = "DO-NOT-EXPORT-PRIVATE-PATH-OR-TOKEN"
    def fail(*args):
        native.PHASE = "candidate_build"
        raise RuntimeError(secret)
    monkeypatch.setattr(native, "outer_execute", fail)
    monkeypatch.setattr(native, "safe_inventory", lambda *args: (_ for _ in ()).throw(OSError(secret)))
    assert native._main([]) == 1
    text = capsys.readouterr().out
    value = json.loads(text)
    assert secret not in text and value["phase"] == "candidate_build"
    assert value["safe_evidence"] is None and value["full_activation_verified"] is False


def test_inventory_never_follows_link_or_claims_full_archive(tmp_path, monkeypatch):
    target = tmp_path / "private-target"
    target.mkdir()
    (target / "never-read").write_text("secret")
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    (root / "external").symlink_to(target, target_is_directory=True)
    native.record(root / "ORIGINAL.json", {"status": "failed"})
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    result = native.safe_inventory(root, 600)
    assert result["full_raw_archive_verified"] is False and result["complete"] is False
    assert len(result["originals"]) == 1 and len(result["omissions"]) == 1
    assert hashlib.sha256(b"external").hexdigest() == result["omissions"][0]["path_sha256"]


def test_inventory_budget_exhaustion_stops_before_hashing(tmp_path, monkeypatch):
    (tmp_path / "one").write_text("one")
    monkeypatch.setattr(native.time, "monotonic", lambda: 601)
    monkeypatch.setattr(native.host, "file_proof", lambda *a, **k: pytest.fail("cannot hash after original deadline"))
    with pytest.raises(gate.AdmissionError):
        native.safe_inventory(tmp_path, 600)


@pytest.mark.parametrize("mutation", [None, "missing", "failed", "schema", "roles", "mounts", "legacy", "actual125"])
def test_custody_inventory_requires_actual_safe_completion_profile_only_in_new_mode(tmp_path, monkeypatch, mutation):
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    paths = ["inner/INNER_RESULT.json", "inner/activation/RESULT.json", "inner/DAEMON_STOP.json", "inner/RESOURCES_FINAL.json"]
    for name in paths:
        path = tmp_path / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        native.record(path, {}, sealed=name.endswith("INNER_RESULT.json"))
    path = tmp_path / "inner/activation/receiver/ACTUAL_CUSTODY_PROOF.json"
    value = {"schema": 1, "status": "passed", "old_project_roles": 3, "old_gateway_mounts": 0,
             "actual_gateway_and_live_legacy": True, "actual_125_compatibility_proven": False,
             "token": "raw-secret-must-not-export"}
    for key, changed in {"failed": ("status", "failed"), "schema": ("schema", True), "roles": ("old_project_roles", 3.0),
                         "mounts": ("old_gateway_mounts", False), "legacy": ("actual_gateway_and_live_legacy", False),
                         "actual125": ("actual_125_compatibility_proven", True)}.items():
        if mutation == key:
            value[changed[0]] = changed[1]
    if mutation != "missing":
        path.parent.mkdir(mode=0o700)
        native.record(path, value)
    before = path.read_bytes() if path.exists() else None
    old = native.safe_inventory(tmp_path, 600)
    assert old["complete"] is True and "ACTUAL_CUSTODY_PROOF.json" not in old["profiles"]
    new = native.safe_inventory(tmp_path, 600, gateway_custody=True)
    assert new["complete"] is (mutation is None)
    assert "raw-secret-must-not-export" not in json.dumps(new)
    assert (path.read_bytes() if path.exists() else None) == before
    if mutation is None:
        assert new["profiles"]["ACTUAL_CUSTODY_PROOF.json"] == {
            "status": "passed", "sha256": hashlib.sha256(before).hexdigest(), "actual_125_compatibility_proven": False}


def test_custody_completion_cannot_use_different_bytes_after_inventory(tmp_path, monkeypatch):
    name = "inner/activation/receiver/ACTUAL_CUSTODY_PROOF.json"
    path = tmp_path / name
    path.parent.mkdir(mode=0o700, parents=True)
    native.record(path, {"status": "passed"})
    records = [{"path_sha256": hashlib.sha256(name.encode()).hexdigest(), "sha256": "a" * 64}]
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    with pytest.raises(gate.AdmissionError, match="completion original changed"):
        native.custody_completion_profile(tmp_path, records, 600)


def test_inventory_one_directory_cannot_exceed_entry_count(tmp_path, monkeypatch):
    for number in range(4097):
        (tmp_path / str(number)).touch(mode=0o600)
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    with pytest.raises(gate.AdmissionError, match="inventory count bound"):
        native.safe_inventory(tmp_path, 600)


def test_full_activation_final_command_is_clamped_and_timeout_is_sticky(tmp_path, monkeypatch):
    context = SimpleNamespace(check=lambda **kw: None, plan={"deadline": 600, "active_deadline": 580},
                              commands=SimpleNamespace(uncertain_mutation=False))
    harness = object.__new__(activation.ActivationHarness)
    harness.private_host_context, harness.output, harness.sequence = context, tmp_path, 0
    seen = []
    monkeypatch.setattr(native.time, "monotonic", lambda: 599)
    def command(self, args, **kwargs):
        seen.append(kwargs["timeout"])
        self.sequence += 1
        from scripts.verify_miniflux_pause_native import command_original_proof, retain_command_result
        path = self.output / f"command-{self.sequence:04d}.json"
        gate._write(path, {"rc": None})
        assert kwargs["command_result"] == {}
        kwargs["command_result"].update(retain_command_result(path, self.sequence, None, command_original_proof(path, sync=True)))
        return ""
    monkeypatch.setattr(activation.Harness, "command", command)
    assert harness.command(["stop", "owned"], final=True, timeout=30) == ""
    assert seen == [1] and context.commands.uncertain_mutation


def test_harness_missing_durable_command_evidence_is_sticky_unknown(tmp_path, monkeypatch):
    context = SimpleNamespace(check=lambda **kw: None, plan={"deadline": 600, "active_deadline": 580},
                              commands=SimpleNamespace(uncertain_mutation=False))
    harness = object.__new__(activation.ActivationHarness)
    harness.private_host_context, harness.output, harness.sequence = context, tmp_path, 0
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    monkeypatch.setattr(activation.Harness, "command", lambda *a, **k: "created-without-retained-ACK")
    with pytest.raises(Exception):
        harness.command(["create", "owned"])
    assert context.commands.uncertain_mutation


def test_failed_build_receipt_write_never_hides_original_failure(tmp_path, monkeypatch):
    request = {"source": {}, "image": "fixed", "contract_sha256": "a" * 64}
    plan = {"outer_peer": {}, "repo": str(tmp_path), "source_identity": {}, "active_deadline": 600}
    monkeypatch.setattr(native, "checked_build_request", lambda *a: (request, {"sha256": "b" * 64}, {}))
    monkeypatch.setattr(native, "peer_identity", lambda: {})
    monkeypatch.setattr(native.private, "source_identity", lambda *a: {})
    original = RuntimeError("original actual build failure")
    monkeypatch.setattr(native.startup, "build_candidate", lambda *a, **k: (_ for _ in ()).throw(original))
    def record(path, *args, **kwargs):
        if path.name == "BUILD_RESULT.json":
            raise OSError("receipt fsync failed")
    monkeypatch.setattr(native, "record", record)
    with pytest.raises(RuntimeError) as caught:
        native.build_requested_candidate(tmp_path, plan, None)
    assert caught.value is original
    assert any("receipt could not be persisted" in note for note in original.__notes__)


@pytest.mark.parametrize("point", ["receipt", "fsync"])
def test_outer_preservation_failure_does_not_hide_primary(tmp_path, monkeypatch, point):
    primary, attempted = RuntimeError("actual original guard failure"), []
    def record(*args, **kwargs):
        if point == "receipt":
            raise OSError("private receipt full")
    monkeypatch.setattr(native, "record", record)
    def fsync(fd):
        attempted.append(fd)
        if point == "fsync":
            raise OSError("private stream sync full")
    monkeypatch.setattr(native.os, "fsync", fsync)
    streams = [SimpleNamespace(flush=lambda: None, fileno=lambda: 10, close=lambda: None),
               SimpleNamespace(flush=lambda: None, fileno=lambda: 11, close=lambda: None)]
    native.preserve_outer_outputs(SimpleNamespace(poll=lambda: None, pid=123), tmp_path, {"deadline": 600}, streams, primary)
    assert attempted == [10, 11]
    assert any("outer child evidence failed" in note for note in primary.__notes__)


def test_outer_evidence_failure_is_not_silently_ignored_when_no_primary(tmp_path, monkeypatch):
    monkeypatch.setattr(native.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError):
        native.preserve_outer_outputs(None, tmp_path, {}, [SimpleNamespace(flush=lambda: None, fileno=lambda: 10, close=lambda: None)], None)


@pytest.mark.parametrize("point", ["direct_mutator_exception", "direct_mutator_failed_report", "launch_before_tuple_return"])
def test_failed_inner_paths_preserve_live_daemon_and_never_term_even_if_ps_empty(tmp_path, monkeypatch, point):
    from scripts import production_offline_registry as registry
    monkeypatch.setattr(native.os, "setsid", lambda: None)
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    monkeypatch.setattr(native.host, "proc_identity", lambda pid: {"netns": 10})
    monkeypatch.setattr(native.host, "restrict_private_ipv6", lambda *a: None)
    monkeypatch.setattr(native, "prepare_mounts", lambda *a: None)
    monkeypatch.setattr(native, "resource_check", lambda *a: {})
    records = []
    monkeypatch.setattr(native, "record", lambda path, value, **kwargs: records.append((path.name, value, kwargs)) or {})
    monkeypatch.setattr(native, "load_dependencies", lambda *a: None)
    monkeypatch.setattr(native.PrivateHostContext, "check", lambda *a, **k: None)
    commands = SimpleNamespace(deadline=580, uncertain_mutation=False, docker=lambda *a: "")
    monkeypatch.setattr(native, "FullCommands", lambda *a: commands)
    monkeypatch.setattr(registry, "serve_bundle", lambda *a, **k: nullcontext(SimpleNamespace(url="http://127.0.0.1:3333")))
    process = SimpleNamespace(poll=lambda: None, send_signal=lambda *a: pytest.fail("unknown direct mutation forbids daemon TERM"))
    monkeypatch.setattr(native.host, "LIVE_DAEMONS", [])
    def launched(*args):
        native.host.LIVE_DAEMONS.append((process, None, tmp_path, {}))
        if point == "launch_before_tuple_return":
            raise RuntimeError("retainer setup failed after original FD registration")
        return process, {}, None
    monkeypatch.setattr(native, "start_daemon", launched)
    def execute(*args, **kwargs):
        if point == "direct_mutator_exception":
            raise OSError("direct private.run mutation outcome unknown")
        return {"status": "failed_or_evidence_insufficient"}
    monkeypatch.setattr(activation, "execute", execute)
    plan = {"active_deadline": 580, "deadline": 600, "bundle": {}, "parent": {}, "repo": str(tmp_path),
            "source": str(tmp_path), "binary": str(tmp_path), "sender": {"Id": "sha256:" + "a" * 64},
            "gateway_custody": False}
    result = native.inner_execute(tmp_path, plan)
    assert result["status"] == "failed_or_evidence_insufficient" and result["retained_live_daemon"] is True
    assert result["daemon_stop"] is None
    assert [kwargs for name, _value, kwargs in records if name == "INNER_RESULT.json"] == [{"sealed": True}]
    if point != "launch_before_tuple_return":
        assert commands.uncertain_mutation is True


@pytest.mark.parametrize("deadline", [True, "600", float("inf"), float("nan"), 100])
def test_native_source_build_rejects_bad_deadline_before_any_archive(monkeypatch, deadline):
    monkeypatch.setattr(startup.time, "monotonic", lambda: 100)
    monkeypatch.setattr(startup, "_oneoff_public_source", lambda *a, **k: pytest.fail("must not start source read"))
    with pytest.raises(ValueError):
        startup.build_candidate(None, None, None, None, None, None, deadline=deadline)


def test_probe_each_command_uses_fresh_remaining_original_deadline(tmp_path, monkeypatch):
    clock, seen = [100], []
    monkeypatch.setattr(startup.time, "monotonic", lambda: clock[0])
    def capture(args, path, **kwargs):
        seen.append(kwargs["seconds"])
        Path(str(path) + ".stdout").write_bytes(b"{}")
        clock[0] += 50
    def probe(contract, directory, digest, runner):
        runner(["first"])
        runner(["second"])
    monkeypatch.setattr(startup, "archive_command", capture)
    monkeypatch.setattr(startup.private, "image_probe", probe)
    startup.probe_candidate({}, tmp_path, "image", tmp_path, deadline=200)
    assert seen == [100, 50]


def test_source_preparation_cannot_replay_elapsed_build_budget(tmp_path, monkeypatch):
    clock, seen = [100], []
    monkeypatch.setattr(startup.time, "monotonic", lambda: clock[0])
    def prepared(*args, **kwargs):
        assert kwargs["deadline"] == 200
        clock[0] = 201
        return tmp_path / "context"
    monkeypatch.setattr(startup, "_oneoff_public_source", prepared)
    monkeypatch.setattr(startup, "archive_command", lambda *a, **k: seen.append(k))
    source = {"source_sha": "a" * 40, "example_config_sha256": "b" * 64}
    contract = {"files": {"compose.json": "c" * 64, "runtime.yaml": "d" * 64}}
    with pytest.raises(ValueError, match="deadline expired"):
        startup.build_candidate(tmp_path, tmp_path, source, "fixed", contract, "e" * 64, deadline=200)
    assert seen == []


def _gateway_switch_dir(root):
    switch = root / "inner/activation/receiver/gateway-native/switch"
    switch.mkdir(parents=True, mode=0o700)
    return switch


def test_inventory_exports_gateway_command_stderr_redacted_with_size_and_hash(tmp_path, monkeypatch):
    root = tmp_path / "evidence"
    switch = _gateway_switch_dir(root)
    stderr = (b'nginx: [emerg] could not build map_hash, you should increase map_hash_bucket_size: 64\n'
              b'password=hunter2-secret Authorization: Bearer abc.def.ghi\n')
    (switch / "config-test-start.stderr").write_bytes(stderr)
    receipt = b'{"argv_sha256":"' + b"a" * 64 + b'","returncode":1}'
    (switch / "config-test-start.command.json").write_bytes(receipt)
    (switch / "config-test-start.stdout").write_bytes(b"stdout must stay hash-only token=zzz\n")
    (switch / "new-nginx.conf").write_bytes(b"config stays hash-only\n")
    (root / "inner/other.stderr").write_bytes(b"non-gateway stderr stays hash-only\n")
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    result = native.safe_inventory(root, 600)
    rows = {row["path"]: row for row in result["gateway_command_stderr"]}
    prefix = "inner/activation/receiver/gateway-native/switch/"
    assert set(rows) == {prefix + "config-test-start.stderr", prefix + "config-test-start.command.json"}
    row = rows[prefix + "config-test-start.stderr"]
    assert row["tag"] == "gateway_command_stderr" and row["truncated"] is False
    assert row["bytes"] == len(stderr) and row["sha256"] == hashlib.sha256(stderr).hexdigest()
    assert "map_hash_bucket_size: 64" in row["text"]
    assert "hunter2" not in row["text"] and "abc.def.ghi" not in row["text"] and "[REDACTED]" in row["text"]
    assert rows[prefix + "config-test-start.command.json"]["text"] == receipt.decode()
    encoded = json.dumps(result)
    for hidden in ("stdout must stay", "config stays", "non-gateway stderr", "token=zzz"):
        assert hidden not in encoded
    hashes = {r["path_sha256"]: r for r in result["originals"]}
    for name in ("config-test-start.stdout", "new-nginx.conf"):
        assert hashlib.sha256((prefix + name).encode()).hexdigest() in hashes


def test_gateway_stderr_export_is_capped_with_marker_and_budget(tmp_path, monkeypatch):
    root = tmp_path / "evidence"
    switch = _gateway_switch_dir(root)
    big = (b"x" * 1023 + b"\n") * 65
    for name in ("a-start", "b-start", "c-start"):
        (switch / (name + ".stderr")).write_bytes(big)
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    rows = native.safe_inventory(root, 600)["gateway_command_stderr"]
    assert [row["path"].rsplit("/", 1)[1] for row in rows] == ["a-start.stderr", "b-start.stderr", "c-start.stderr"]
    first = rows[0]
    assert first["truncated"] is True and first["bytes"] == len(big) and first["sha256"] == hashlib.sha256(big).hexdigest()
    assert first["text"].startswith("x" * 100) and first["text"].count("x") == 64 * 1023
    assert first["text"].endswith(f"[TRUNCATED: first {native.GATEWAY_TEXT_CAP} of {len(big)} bytes]")
    assert rows[1]["text"] is not None
    assert rows[2]["text"] is None and rows[2]["reason"] == "export_budget_exhausted"
    assert rows[2]["sha256"] == hashlib.sha256(big).hexdigest()


def test_gateway_export_ignores_lookalike_paths_and_links(tmp_path, monkeypatch):
    root = tmp_path / "evidence"
    switch = _gateway_switch_dir(root)
    (root / "inner/gateway-native").mkdir(parents=True)
    (root / "inner/gateway-native/switch.stderr").write_bytes(b"wrong place")
    outside = tmp_path / "outside.stderr"
    outside.write_bytes(b"outside secret")
    (switch / "linked.stderr").symlink_to(outside)
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    result = native.safe_inventory(root, 600)
    assert result["gateway_command_stderr"] == []
    assert "outside secret" not in json.dumps(result) and "wrong place" not in json.dumps(result)


def test_route_probe_record_is_exported_redacted(tmp_path, monkeypatch):
    root = tmp_path / "evidence"
    switch_dir = _gateway_switch_dir(root)
    record = {"schema": 1, "probes": [{"status": 503, "body_text": "token=leakme123", "upstream": None,
                                       "failed_checks": ["status", "upstream"]}]}
    (switch_dir / "route-probe.stderr").write_text(json.dumps(record) + "\n")
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    rows = native.safe_inventory(root, 600)["gateway_command_stderr"]
    assert [r["path"].rsplit("/", 1)[1] for r in rows] == ["route-probe.stderr"]
    assert '"status": 503' in rows[0]["text"] and "failed_checks" in rows[0]["text"]
    assert "leakme123" not in rows[0]["text"]
