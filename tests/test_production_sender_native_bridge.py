"""Real local HTTP/threads with a synthetic Harness backend; no Docker access."""
import http.client
import json
import socket
import threading
from types import SimpleNamespace

import pytest

from scripts import production_sender_native_bridge as bridge_module
from services import admission_fence as gate
from production_sender_driver import NativeSenderActivationTransport


@pytest.fixture
def case(tmp_path):
    class SyntheticHarness(bridge_module.Harness):
        def __init__(self):
            self.owner, self.token = "a" * 32, "b" * 64
            self.network = "miniflux-pause-" + self.owner[:12] + "-network"
            self.output = tmp_path
            self.sequence, self.calls, self.commands = 0, [], []
            self.error = None
        def command(self, args, **kwargs):
            self.sequence += 1
            self.commands.append((self.sequence, args, kwargs))
            return "actual-synthetic-backend"
        def private_exchange(self, path, method="GET", value=None):
            self.calls.append((method, path, value))
            result = self.command(["synthetic-forward"], timeout=15)
            if self.error:
                raise self.error
            return 200, {"source": result, "confirmed": True, "result": {"original": 17}}
    harness = SyntheticHarness()
    return SimpleNamespace(harness=harness, root=tmp_path, token=harness.token)


def request(bridge, method, path, body=None, *, token=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", bridge.server.server_port, timeout=4)
    auth = {"Authorization": "Bearer " + (token or bridge.token), "Content-Type": "application/json"}
    try:
        connection.request(method, path, body=body, headers=headers if headers is not None else auth)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_actual_driver_class_reaches_loopback_and_context_restores_commands(case):
    h = case.harness
    original = h.command
    with bridge_module.NativeControlBridge(h) as bridge:
        assert bridge.server.server_address[0] == "127.0.0.1"
        transport = bridge.transport(case.root / "token.json")
        assert type(transport) is NativeSenderActivationTransport and transport.container_id is None
        status, body = transport.request("GET", "/activation-registration")
        assert (status, body) == (200, {"source": "actual-synthetic-backend", "confirmed": True, "result": {"original": 17}})
        transport.request("POST", "/initialize-closed", {"operation_id": "c" * 64})
        h.command(["main-thread-inspect"], timeout=17)
        assert all(2 < row[2]["timeout"] <= bridge_module.COMMAND_SECONDS for row in h.commands[:2])
        assert transport.timeout_seconds == bridge_module.CLIENT_SECONDS
        assert h.commands[-1][2]["timeout"] == 17
    assert h.command == original and not bridge.thread.is_alive()
    records = b"".join(path.read_bytes() for path in bridge.root.iterdir())
    assert h.token.encode() not in records and b'"original":17' not in records
    assert not (bridge.root / "STOP.json").exists()
    with pytest.raises(gate.AdmissionError, match="live native"):
        bridge.transport(case.root / "another.json")


@pytest.mark.parametrize("method,path,body,token,headers", [
    ("GET", "/registration", None, "wrong", None),
    ("POST", "/resume", b'{"operation_id":"' + b"c" * 64 + b'"}', None, None),
    ("POST", "/pause", b"x" * 8193, None, None),
    ("GET", "/registration", b"x", None, None),
    ("POST", "/pause", b"", None, {"Authorization": "Bearer " + "b" * 64, "Transfer-Encoding": "chunked"}),
], ids=["authentication", "route", "body_limit", "get_body", "chunked"])
def test_auth_route_body_and_transfer_rejections_never_forward(case, method, path, body, token, headers):
    with bridge_module.NativeControlBridge(case.harness) as bridge:
        status, result = request(bridge, method, path, body, token=token, headers=headers)
        assert status == 503 and result == {"error": "native_bridge_unconfirmed"}
        assert not case.harness.calls and (bridge.root / "STOP.json").exists()


def test_duplicate_post_is_never_forwarded_and_original_query_remains_available(case):
    body = json.dumps({"operation_id": "c" * 64}).encode()
    with bridge_module.NativeControlBridge(case.harness) as bridge:
        assert request(bridge, "POST", "/pause", body)[0] == 200
        assert request(bridge, "POST", "/pause", body)[0] == 503
        assert request(bridge, "GET", "/operations/" + "c" * 64)[0] == 200
        assert [row[0] for row in case.harness.calls] == ["POST", "GET"]
        assert gate._read(bridge.root / "STOP.json")["post_retry"] is False


def test_backend_timeout_is_unknown_and_never_repeats_post(case):
    case.harness.error = TimeoutError("synthetic private failure " + case.token)
    body = json.dumps({"operation_id": "c" * 64}).encode()
    with bridge_module.NativeControlBridge(case.harness) as bridge:
        assert request(bridge, "POST", "/pause", body)[0] == 503
        assert request(bridge, "POST", "/pause", body)[0] == 503
        assert len(case.harness.calls) == 1
        records = b"".join(path.read_bytes() for path in bridge.root.iterdir())
        assert case.token.encode() not in records
        assert (bridge.root / "request-001.intent.json").exists()
        assert not (bridge.root / "request-001.result.json").exists()


def test_private_values_in_operation_path_or_body_are_only_hashed(case):
    with bridge_module.NativeControlBridge(case.harness) as bridge:
        assert request(bridge, "GET", "/operations/" + case.token)[0] == 200
        body = json.dumps({"operation_id": "c" * 64, "private": case.token})
        assert request(bridge, "POST", "/pause", body)[0] == 200
        records = b"".join(path.read_bytes() for path in bridge.root.iterdir())
        assert case.token.encode() not in records
        value = gate._read(bridge.root / "request-001.intent.json")
        assert value["route"] == "/operations/<operation_id>" and "path" not in value


def test_all_harness_commands_share_real_lock_without_changing_main_timeout(case):
    h = case.harness
    entered, release, main_attempt, main_entered = [threading.Event() for _ in range(4)]
    rows, errors = [], []
    def command(args, **kwargs):
        if args == ["synthetic-forward"]:
            entered.set()
            assert release.wait(2)
        else:
            main_entered.set()
        h.sequence += 1
        rows.append((h.sequence, args, kwargs))
        return "backend"
    h.command = command
    with bridge_module.NativeControlBridge(h) as bridge:
        def http_worker():
            try:
                assert request(bridge, "GET", "/registration")[0] == 200
            except BaseException as error:
                errors.append(error)
        client = threading.Thread(target=http_worker)
        client.start()
        assert entered.wait(1)
        def main_worker():
            main_attempt.set()
            h.command(["main"], timeout=19)
        main = threading.Thread(target=main_worker)
        main.start()
        assert main_attempt.wait(1) and not main_entered.is_set()
        release.set()
        client.join(2)
        main.join(2)
        assert not client.is_alive() and not main.is_alive() and not errors
    assert [row[0] for row in rows] == [1, 2]
    assert 2 < rows[0][2]["timeout"] <= bridge_module.COMMAND_SECONDS and rows[1][2]["timeout"] == 19


def test_finite_request_budget_closes_listener(case, monkeypatch):
    monkeypatch.setattr(bridge_module, "MAX_REQUESTS", 2)
    with bridge_module.NativeControlBridge(case.harness) as bridge:
        assert request(bridge, "GET", "/registration")[0] == 200
        assert request(bridge, "GET", "/registration")[0] == 200
        bridge.thread.join(1)
        assert not bridge.thread.is_alive() and bridge.requests == 2
        with pytest.raises(OSError):
            request(bridge, "GET", "/registration")
        assert len(case.harness.calls) == 2 and gate._read(bridge.root / "STOP.json")["stage"] == "request_budget"


def test_incomplete_http_headers_expire_before_any_backend_call(case, monkeypatch):
    monkeypatch.setattr(bridge_module, "REQUEST_SECONDS", 0.1)
    with bridge_module.NativeControlBridge(case.harness) as bridge:
        with socket.create_connection(("127.0.0.1", bridge.server.server_port), timeout=1) as connection:
            connection.sendall(b"GET /registration HTTP/1.1\r\nAuthorization: Bearer partial")
            assert connection.recv(100) == b""
        assert not case.harness.calls
        assert gate._read(bridge.root / "STOP.json")["stage"] == "request_headers"


def test_expired_forward_deadline_does_not_authorize_result(case, monkeypatch):
    original = case.harness.private_exchange
    def late(*args):
        result = original(*args)
        monkeypatch.setattr(bridge_module.time, "monotonic", lambda: 10**12)
        return result
    case.harness.private_exchange = late
    with bridge_module.NativeControlBridge(case.harness) as bridge:
        assert request(bridge, "POST", "/pause", json.dumps({"operation_id": "c" * 64}))[0] == 503
        assert (bridge.root / "STOP.json").exists() and len(case.harness.calls) == 1


def test_start_record_failure_restores_original_method_and_closes_socket(case, monkeypatch):
    original = case.harness.command
    def failure(*args):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(gate, "_write", failure)
    bridge = bridge_module.NativeControlBridge(case.harness)
    with pytest.raises(OSError):
        bridge.__enter__()
    assert not bridge.live and case.harness.command == original and bridge.server.fileno() == -1


def test_bridge_budget_constants_are_pinned():
    assert (bridge_module.REQUEST_SECONDS, bridge_module.COMMAND_SECONDS, bridge_module.CLIENT_SECONDS) == (18, 15, 20)
    assert bridge_module.COMMAND_SECONDS < bridge_module.REQUEST_SECONDS < bridge_module.CLIENT_SECONDS
    assert bridge_module.REQUEST_SECONDS not in (3, 17, 19) and bridge_module.CLIENT_SECONDS not in (2, 19, 21)
