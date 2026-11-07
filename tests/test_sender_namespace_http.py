"""Offline namespace boundaries; actual Linux nsenter needs separate native proof."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import production_sender_driver as sender  # noqa: E402
import sender_namespace_http as namespace  # noqa: E402
from durable_handoff_cutover import CutoverError  # noqa: E402

CID, TOKEN = "a" * 64, "b" * 64


@pytest.fixture
def process_case(tmp_path, monkeypatch):
    target = tmp_path / "synthetic-netns"
    target.write_bytes(b"synthetic namespace descriptor")
    stat = target.stat()
    owner = {"pid": 321, "start_ticks": 45, "network_device": stat.st_dev, "network_inode": stat.st_ino}
    before = {"container_id": CID, "pid": 321, "started_at": "synthetic", "restart_count": 0, "image_id": "sha256:" + CID}
    calls, fds, closed = [], [], []
    actual_close = os.close
    def close(fd):
        actual_close(fd)
        closed.append(fd)
    def opened(pid):
        assert pid == 321
        fd = os.open(target, os.O_RDONLY)
        fds.append(fd)
        return fd
    def capture(argv, **kwargs):
        calls.append((argv, kwargs))
        return b'{"status":200,"body":{"confirmed":true}}'
    monkeypatch.setattr(namespace, "_inspect", lambda cid, deadline: deepcopy(before))
    monkeypatch.setattr(namespace, "_process_identity", lambda pid: deepcopy(owner))
    monkeypatch.setattr(namespace, "_open_namespace", opened)
    monkeypatch.setattr(namespace, "_capture", capture)
    monkeypatch.setattr(namespace.os, "close", close)
    return SimpleNamespace(owner=owner, before=before, calls=calls, fds=fds, closed=closed)


def test_pins_namespace_fd_and_keeps_credentials_off_argv(process_case):
    p = process_case
    body = {"operation_id": "c" * 64, "private": "synthetic-body-secret"}
    assert namespace.request(CID, 8910, "POST", "/stop-new", TOKEN, body) == (200, {"confirmed": True})
    assert len(p.calls) == 1
    argv, options = p.calls[0]
    assert argv == ["/usr/bin/nsenter", "--target", "321", f"--net=/proc/self/fd/{p.fds[0]}", "--",
                    str(Path(sys.executable).resolve()), "-I", "-B", str(Path(namespace.__file__).resolve()), "--client"]
    assert options["pass_fds"] == (p.fds[0],)
    value = json.loads(options["input_bytes"])
    assert value["token"] == TOKEN and value["payload"] == body and value["owner"] == p.owner
    assert TOKEN not in str(argv) and body["private"] not in str(argv)
    assert p.fds[0] in p.closed  # Actual close observed; later background FD reuse cannot make this flaky.


@pytest.mark.parametrize("field,replacement", [("pid", 322), ("restart_count", 1), ("started_at", "changed"),
                                               ("image_id", "sha256:" + "f" * 64), ("container_id", "f" * 64)])
def test_post_request_docker_identity_change_is_unknown_without_retry(process_case, monkeypatch, field, replacement):
    p = process_case
    rows = [p.before, {**p.before, field: replacement}]
    monkeypatch.setattr(namespace, "_inspect", lambda *args: rows.pop(0))
    with pytest.raises(namespace.NamespaceUncertain, match="query original"):
        namespace.request(CID, 8910, "POST", "/stop-new", TOKEN, {"operation_id": "c" * 64})
    assert len(p.calls) == 1 and rows == []


@pytest.mark.parametrize("field", ["start_ticks", "network_inode", "network_device"])
def test_post_request_process_reuse_or_namespace_change_is_unknown(process_case, monkeypatch, field):
    p = process_case
    owners = [p.owner, p.owner, {**p.owner, field: p.owner[field] + 1}]
    monkeypatch.setattr(namespace, "_process_identity", lambda pid: owners.pop(0))
    with pytest.raises(namespace.NamespaceUncertain):
        namespace.request(CID, 8910, "GET", "/registration", TOKEN)
    assert len(p.calls) == 1


def test_pre_request_namespace_fd_mismatch_does_not_call_client(process_case):
    p = process_case
    p.owner["network_inode"] += 1
    with pytest.raises(namespace.NamespaceUncertain):
        namespace.request(CID, 8910, "GET", "/registration", TOKEN)
    assert p.calls == []


@pytest.mark.parametrize("method,path,payload", [("POST", "/resume", {}), ("GET", "/operations/short", None),
    ("GET", "http://outside/registration", None), ("GET", "/registration", {}),
    ("POST", "/pause", None), ("POST", "/pause", {"data": "x" * 8192}), ("GET", None, None)])
def test_unsupported_requests_stop_before_inspect(process_case, monkeypatch, method, path, payload):
    def forbidden(*args):
        pytest.fail("invalid request reached Docker")
    monkeypatch.setattr(namespace, "_inspect", forbidden)
    with pytest.raises(namespace.NamespaceUncertain):
        namespace.request(CID, 8910, method, path, TOKEN, payload)
    assert process_case.calls == []


@pytest.mark.parametrize("raw", [b'{"status":200,"body":{},"status":409}', b'{"status":200,"body":NaN}',
                               b'{"status":true,"body":{}}', b'{"status":500,"body":{}}', b'{"status":200,"body":[]}'])
def test_ambiguous_child_response_is_unknown(process_case, monkeypatch, raw):
    monkeypatch.setattr(namespace, "_capture", lambda *args, **kwargs: raw)
    with pytest.raises(namespace.NamespaceUncertain):
        namespace.request(CID, 8910, "GET", "/registration", TOKEN)


def test_capture_bounds_real_subprocess_environment_and_input(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_PARENT_SECRET", TOKEN)
    script = "import json,os,sys;print(json.dumps({'env':dict(os.environ),'body':sys.stdin.read()}))"
    raw = namespace._capture([sys.executable, "-I", "-B", "-c", script],
                             input_bytes=b"synthetic stdin", deadline=time.monotonic() + 3)
    value = json.loads(raw)
    assert value["body"] == "synthetic stdin"
    assert value["env"]["PATH"] == "/usr/bin:/bin" and "SYNTHETIC_PARENT_SECRET" not in value["env"]


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_capture_combined_output_budget_kills_only_own_client(stream):
    with pytest.raises(namespace.NamespaceUncertain):
        namespace._capture([sys.executable, "-I", "-B", "-c", f"import sys;sys.{stream}.write('x'*2048)"],
                           max_bytes=1024, deadline=time.monotonic() + 3)


def test_capture_real_subprocess_deadline_has_no_retry():
    started = time.monotonic()
    with pytest.raises(namespace.NamespaceUncertain):
        namespace._capture([sys.executable, "-I", "-B", "-c", "import time;time.sleep(30)"], deadline=started + 0.1)
    assert time.monotonic() - started < 2


def test_child_malformed_input_has_fixed_diagnostic_without_private_values():
    result = subprocess.run([sys.executable, "-I", "-B", namespace.__file__, "--client"],
                            input=b'{"secret":"' + TOKEN.encode() + b'"}', capture_output=True, timeout=3)
    assert result.returncode == 3 and result.stdout == b""
    diagnostic = json.loads(result.stderr)
    assert namespace._safe_child_diagnostic(diagnostic) == diagnostic
    assert diagnostic["stage"] == "main" and diagnostic["kind"] == "NamespaceUncertain"
    assert TOKEN.encode() not in result.stderr


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "token.json"
    path.write_text(json.dumps({"token": TOKEN}))
    path.chmod(0o600)
    return path


def test_native_optional_mode_keeps_canonical_class_and_exact_id(token_file, monkeypatch):
    calls = []
    monkeypatch.setattr(namespace, "request", lambda *args: calls.append(args) or (200, {"confirmed": True}))
    plain = sender.NativeSenderTransport("http://127.0.0.1:8910", token_file)
    assert plain.container_id is None
    transport = sender.NativeSenderActivationTransport("http://127.0.0.1:8910", token_file, container_id=CID)
    assert isinstance(transport, sender.NativeSenderTransport) and transport.container_id == CID
    assert transport.request("POST", "/initialize-closed", {"operation_id": "c" * 64}) == (200, {"confirmed": True})
    assert calls == [(CID, 8910, "POST", "/initialize-closed", TOKEN, {"operation_id": "c" * 64})]
    with pytest.raises(CutoverError, match="namespace binding differs"):
        transport.inspect("d" * 64)


@pytest.mark.parametrize("klass,path", [(sender.NativeSenderTransport, "/production-resume"),
                                         (sender.NativeSenderActivationTransport, "/pause")])
def test_parent_route_capability_still_rejects_before_namespace(token_file, monkeypatch, klass, path):
    def forbidden(*args):
        pytest.fail("parent route denial reached namespace client")
    monkeypatch.setattr(namespace, "request", forbidden)
    transport = klass("http://127.0.0.1:8910", token_file, container_id=CID)
    with pytest.raises(CutoverError, match="not allowed"):
        transport.request("POST", path, {})


def test_token_change_is_rejected_before_namespace(token_file, monkeypatch):
    def forbidden(*args):
        pytest.fail("changed credentials reached namespace client")
    monkeypatch.setattr(namespace, "request", forbidden)
    transport = sender.NativeSenderTransport("http://127.0.0.1:8910", token_file, container_id=CID)
    token_file.write_text(json.dumps({"token": "f" * 64}))
    with pytest.raises(CutoverError, match="credential changed"):
        transport.request("GET", "/registration")


def test_sender_source_contract_binds_namespace_implementation():
    import hashlib
    assert sender.sources()["sender_namespace_http.py"] == hashlib.sha256(Path(namespace.__file__).read_bytes()).hexdigest()


@pytest.mark.parametrize("field,value", [("Running", False), ("Paused", True), ("Restarting", True),
    ("Dead", True), ("Pid", True), ("Pid", 1), ("StartedAt", "0001-01-01T00:00:00Z")])
def test_inspect_requires_live_exact_container_state(monkeypatch, field, value):
    row = {"Id": CID, "State": {"Running": True, "Paused": False, "Restarting": False, "Dead": False,
                              "Pid": 321, "StartedAt": "2026-10-27T00:00:00Z"}, "Image": "sha256:" + CID, "RestartCount": 0}
    calls = []
    def capture(argv, **options):
        calls.append(argv)
        return json.dumps([row]).encode()
    monkeypatch.setattr(namespace, "_capture", capture)
    assert namespace._inspect(CID, time.monotonic() + 3)["pid"] == 321
    row["State"][field] = value
    with pytest.raises(namespace.NamespaceUncertain):
        namespace._inspect(CID, time.monotonic() + 3)
    assert all(argv == ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", "inspect", CID] for argv in calls)


@pytest.fixture
def http_case(monkeypatch):
    owner = {"pid": 321, "start_ticks": 45, "network_device": 4, "network_inode": 12}
    p = SimpleNamespace(owner=owner, calls=[], response=b'{"confirmed":true}', status=200, closed=False,
                        listener_live=False, listener_released=False, listener_confirmed=False)
    class Connection:
        sock = None
        def __init__(self, *args, **kwargs):
            p.calls.append((args, kwargs))
        def request(self, method, path, **kwargs):
            assert p.listener_live
            p.calls.append((method, path, kwargs))
        def getresponse(self):
            return SimpleNamespace(status=p.status, read1=self.read)
        def read(self, size):
            part, p.response = p.response[:size], p.response[size:]
            return part
        def close(self):
            assert p.listener_live
            p.closed = True
    monkeypatch.setattr(namespace, "Path", lambda path: SimpleNamespace(stat=lambda: SimpleNamespace(st_dev=4, st_ino=12)))
    monkeypatch.setattr(namespace, "_process_identity", lambda pid: deepcopy(owner))
    @contextmanager
    def owned_listener(_owner, _port, _deadline):
        # This fixture isolates HTTP framing. Real FD/port ownership has its own
        # strict positive and negative tests; it is never inferred from this fake.
        assert _owner == owner and _port == 8910 and _deadline <= time.monotonic() + 2
        p.listener_live = True
        def confirm():
            assert p.listener_live
            p.listener_confirmed = True
        try:
            yield SimpleNamespace(confirm=confirm)
        finally:
            p.listener_live, p.listener_released = False, True
    monkeypatch.setattr(namespace, "_listener_owner", owned_listener)
    monkeypatch.setattr(namespace.http.client, "HTTPConnection", Connection)
    p.value = {"port": 8910, "method": "POST", "path": "/initialize-closed", "token": TOKEN,
               "payload": {"operation_id": "c" * 64}, "owner": owner}
    return p


def test_child_only_requests_loopback_after_own_namespace_check(http_case):
    p = http_case
    assert namespace._http(p.value) == {"status": 200, "body": {"confirmed": True}}
    assert p.calls[0][0] == ("127.0.0.1", 8910)
    assert 0 < p.calls[0][1]["timeout"] <= 2
    assert p.calls[1][0:2] == ("POST", "/initialize-closed")
    assert p.calls[1][2]["headers"]["Authorization"] == "Bearer " + TOKEN
    assert json.loads(p.calls[1][2]["body"]) == p.value["payload"] and p.closed
    assert p.listener_confirmed and p.listener_released and not p.listener_live


def test_child_wrong_namespace_never_opens_http(http_case):
    p = http_case
    p.value["owner"]["network_inode"] += 1
    with pytest.raises(namespace.NamespaceUncertain):
        namespace._http(p.value)
    assert p.calls == []


@pytest.mark.parametrize("raw,status", [(b"x" * 65537, 200), (b'{}', 500), (b'[]', 200),
                                      (b'{"confirmed":true,"confirmed":false}', 200)])
def test_child_response_budget_status_and_duplicates_fail_closed(http_case, raw, status):
    p = http_case
    p.response, p.status = raw, status
    with pytest.raises(namespace.NamespaceUncertain):
        namespace._http(p.value)
    assert p.closed and p.listener_released and not p.listener_live and not p.listener_confirmed


def _capture_failure(script, *, operation="namespace_http", seconds=3, max_bytes=namespace.MAX_OUTPUT):
    with pytest.raises(namespace.NamespaceUncertain) as caught:
        namespace._capture([sys.executable, "-I", "-B", "-c", script], operation=operation,
                           deadline=time.monotonic() + seconds, max_bytes=max_bytes)
    diagnostic = caught.value.namespace_diagnostic
    assert namespace.safe_namespace_diagnostic(diagnostic) == diagnostic
    return diagnostic


def test_child_real_failure_preserves_fixed_kind_and_source_position_only():
    with pytest.raises(namespace.NamespaceUncertain) as caught:
        namespace._capture([sys.executable, "-I", "-B", namespace.__file__, "--client"],
                           input_bytes=json.dumps({"secret": TOKEN}).encode(), operation="namespace_http",
                           deadline=time.monotonic() + 3)
    diagnostic = caught.value.namespace_diagnostic
    assert namespace.safe_namespace_diagnostic(diagnostic) == diagnostic
    assert diagnostic["returncode"] == 3 and diagnostic["stage"] == "exit_and_deadline"
    assert diagnostic["stdout_eof"] and diagnostic["stderr_eof"] and not diagnostic["deadline_expired"]
    assert diagnostic["child_status"] == "valid" and diagnostic["child"]["stage"] == "main"
    assert TOKEN not in json.dumps(diagnostic) and "secret" not in json.dumps(diagnostic)


def test_capture_unknown_stderr_never_exports_private_text():
    value = _capture_failure("import sys;sys.stderr.write('PRIVATE_TOKEN_URL_BODY');sys.exit(7)")
    assert value["returncode"] == 7 and value["child_status"] == "invalid" and value["child"] is None
    assert value["stdout_bytes"] == 0 and value["stderr_bytes"] == 22
    assert "PRIVATE_TOKEN_URL_BODY" not in json.dumps(value)


def test_capture_stderr_prefix_is_inside_original_output_bound():
    value = _capture_failure("import sys;sys.stderr.write('PRIVATE'*20000)", max_bytes=1024)
    assert value["stage"] == "output_bound" and value["stderr_bytes"] == 1025
    assert value["child_status"] == "invalid" and value["child"] is None
    assert "PRIVATE" not in json.dumps(value)


def test_capture_truncated_diagnostic_is_unknown_not_a_child_result():
    value = _capture_failure("import sys;sys.stderr.write('x'*4097);sys.exit(3)")
    assert value["child_status"] == "truncated" and value["child"] is None


def test_capture_timeout_keeps_partial_pipe_counts_and_unknown_exit():
    value = _capture_failure("import sys,time;sys.stdout.write('partial');sys.stdout.flush();time.sleep(30)", seconds=0.2)
    assert value["deadline_expired"] and value["returncode"] is None and value["stage"] == "pipe_select"
    assert value["stdout_bytes"] == 7 and value["stderr_bytes"] == 0
    assert value["child_status"] == "absent" and not value["stdout_eof"]


def test_parseable_child_json_without_stderr_eof_is_not_a_complete_report():
    child = {"schema": 1, "stage": "unclassified", "kind": "other", "errno": None, "location": None}
    script = "import sys,time;sys.stderr.write(" + repr(json.dumps(child)) + ");sys.stderr.flush();time.sleep(30)"
    value = _capture_failure(script, seconds=0.2)
    assert value["deadline_expired"] and value["returncode"] is None and not value["stderr_eof"]
    assert value["child_status"] == "invalid" and value["child"] is None


def test_zero_exit_after_deadline_is_still_refused(monkeypatch):
    clock = [0.0]
    class Process:
        stdin = None
        def __init__(self):
            streams = []
            for _ in range(2):
                read, write = os.pipe()
                os.close(write)
                streams.append(os.fdopen(read, "rb"))
            self.stdout, self.stderr = streams
        def wait(self, timeout):
            clock[0] = 3.1
            return 0
        def poll(self):
            return 0
    monkeypatch.setattr(namespace.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(namespace.subprocess, "Popen", lambda *args, **kwargs: Process())
    with pytest.raises(namespace.NamespaceUncertain) as caught:
        namespace._capture(["unused"], deadline=3)
    value = caught.value.namespace_diagnostic
    assert value["returncode"] == 0 and value["deadline_expired"] and value["remaining_seconds"] == 0
    assert value["stage"] == "exit_and_deadline" and value["stdout_eof"] and value["stderr_eof"]


def test_capture_budget_counts_process_creation(monkeypatch):
    original = namespace.subprocess.Popen
    stamp = []
    def spawned(*args, **kwargs):
        stamp.append(time.monotonic())
        return original(*args, **kwargs)
    monkeypatch.setattr(namespace.subprocess, "Popen", spawned)
    start = time.monotonic()
    value = _capture_failure("import sys;sys.exit(2)")
    assert value["capture_elapsed_seconds"] >= time.monotonic() - stamp[0] - 0.05
    assert value["capture_budget_seconds"] > 2.9 and time.monotonic() - start < 3


def test_unrelated_disappearing_fd_is_diagnosed_but_not_ignored(monkeypatch):
    import errno
    listener = namespace._PinnedListener({"pid": 321}, 8910, time.monotonic() + 2)
    monkeypatch.setattr(listener, "_window", lambda: None)
    @contextmanager
    def entries(path):
        yield iter([SimpleNamespace(name="5", path="/private/path/canary")])
    def disappeared(path):
        raise FileNotFoundError(errno.ENOENT, "PRIVATE_FILENAME_CANARY")
    monkeypatch.setattr(namespace.os, "scandir", entries)
    monkeypatch.setattr(namespace.os, "readlink", disappeared)
    with pytest.raises(FileNotFoundError) as caught:
        listener._owned_fds(999)
    value = namespace._child_diagnostic(caught.value)
    assert value["stage"] == "_PinnedListener._owned_fds" and value["kind"] == "OSError" and value["errno"] == errno.ENOENT
    assert namespace._safe_child_diagnostic(value) == value
    assert "PRIVATE" not in json.dumps(value) and "canary" not in json.dumps(value)


@pytest.mark.parametrize("change", [
    {"kind": []}, {"kind": "PRIVATE"}, {"stage": []}, {"stage": "PRIVATE"}, {"errno": True}, {"errno": 4096},
    {"location": {"function": [], "line": 1}}, {"location": {"function": "main", "line": 999999}},
    {"location": {"function": "main", "line": True}}, {"token": TOKEN}, {"schema": True},
])
def test_child_diagnostic_rejects_non_source_or_private_fields(change):
    value = {"schema": 1, "stage": "unclassified", "kind": "other", "errno": None, "location": None}
    assert namespace._safe_child_diagnostic({**value, **change}) is None


@pytest.mark.parametrize("field,value", [
    ("operation", []), ("operation", "PRIVATE"), ("stage", {}), ("stage", "PRIVATE"),
    ("child_status", []), ("child_status", "PRIVATE"), ("capture_elapsed_seconds", float("nan")),
    ("remaining_seconds", float("inf")), ("stdout_bytes", True), ("returncode", True),
    ("returncode", 256), ("stderr_eof", 1), ("schema", True), ("token", TOKEN),
    ("child_status", "valid"), ("deadline_expired", True), ("remaining_seconds", 4), ("capture_budget_seconds", 3.001),
])
def test_parent_projection_rejects_malformed_diagnostic(field, value):
    fixed = {"schema": 1, "operation": "namespace_http", "stage": "exit_and_deadline", "capture_elapsed_seconds": 0.1,
             "capture_budget_seconds": 3, "remaining_seconds": 2.9, "deadline_expired": False,
             "returncode": 3, "stdout_bytes": 0, "stderr_bytes": 0, "stdout_eof": True, "stderr_eof": True,
             "stdin_complete": True, "child_status": "absent", "child": None}
    assert namespace.safe_namespace_diagnostic({**fixed, field: value}) is None


def test_diagnostic_failure_does_not_replace_primary_refusal(monkeypatch):
    from scripts import production_sender_bootstrap_native as native
    def fail(value):
        raise RuntimeError("PRIVATE_DIAGNOSTIC_FAILURE")
    monkeypatch.setattr(namespace, "safe_namespace_diagnostic", fail)
    with pytest.raises(namespace.NamespaceUncertain, match="query original") as caught:
        namespace._capture([sys.executable, "-I", "-B", "-c", "import sys;sys.exit(7)"], deadline=time.monotonic() + 3)
    assert caught.value.namespace_diagnostic is None and "PRIVATE" not in str(caught.value)
    assert native._namespace_failure(caught.value) == {"namespace_diagnostic_status": "evidence_insufficient"}
    assert native._namespace_failure(RuntimeError("not a namespace observation")) == {}


@pytest.mark.parametrize("failure", ["classify", "encode", "write"])
def test_child_diagnostic_fault_still_returns_three_without_traceback(monkeypatch, failure):
    from io import BytesIO, StringIO
    stderr = StringIO()
    monkeypatch.setattr(namespace.sys, "argv", ["client", "--client"])
    monkeypatch.setattr(namespace.sys, "stdin", SimpleNamespace(buffer=BytesIO(b'{"private":"SECRET"}')))
    monkeypatch.setattr(namespace.sys, "stderr", stderr)
    def bad(*args, **kwargs):
        raise RuntimeError("PRIVATE_DIAGNOSTIC_ERROR")
    if failure == "classify":
        monkeypatch.setattr(namespace, "_child_diagnostic", bad)
    elif failure == "encode":
        monkeypatch.setattr(namespace, "_encoded", bad)
    else:
        monkeypatch.setattr(stderr, "write", bad)
    assert namespace.main() == 3
    assert "SECRET" not in stderr.getvalue() and "PRIVATE" not in stderr.getvalue()
    assert "Traceback" not in stderr.getvalue()
