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
    assert result.stderr == b"sender namespace client unconfirmed\n" and TOKEN.encode() not in result.stderr


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
