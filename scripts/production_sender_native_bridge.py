"""CI-only loopback bridge to the owned real Go fixture, never production.

The existing Harness performs actual container-network HTTP. This bridge lets
the unchanged native driver class and its child supervisor reach that fixture.
It is separate evidence from the production nsenter transport; no control port
is published and no sender result is synthesized.
"""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from production_sender_driver import NativeSenderActivationTransport  # noqa: E402
import sender_namespace_http as namespace  # noqa: E402
from scripts.verify_miniflux_pause_native import Harness  # noqa: E402
from services import admission_fence as gate  # noqa: E402

REQUEST_SECONDS = 3
MAX_REQUESTS = 200
MAX_BODY = 8192


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


class _DeadlineReader(io.RawIOBase):
    def __init__(self, connection, deadline):
        self.connection, self.deadline = connection, deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("native bridge read deadline")
        self.connection.settimeout(remaining)
        return self.connection.recv_into(buffer)


class _Handler(BaseHTTPRequestHandler):
    server_version = "SyntheticNativeBridge"
    sys_version = ""

    def log_message(self, *_args):
        pass  # Never log request headers, paths, bodies or credentials.

    def setup(self):
        self.deadline = time.monotonic() + REQUEST_SECONDS
        self.request.settimeout(REQUEST_SECONDS)
        super().setup()
        self.rfile.close()
        self.rfile = io.BufferedReader(_DeadlineReader(self.connection, self.deadline))

    def handle(self):
        self.sequence = self.server.bridge._begin()
        self.server.bridge.request_deadline = self.deadline
        self.handled = False
        try:
            super().handle()
        finally:
            if not self.handled:
                self.server.bridge._failed(self.sequence, "request_headers", TimeoutError("native request was incomplete"))

    def send_error(self, code, message=None, explain=None):
        self.handled = True
        self.server.bridge._failed(self.sequence, "http_parser", ValueError("invalid native bridge request"))
        self._reply(code, {"error": "native_bridge_unconfirmed"})

    def _reply(self, status, value):
        raw = namespace._encoded(value)
        gate._require(len(raw) <= namespace.MAX_HTTP, "native bridge response bound")
        self.connection.settimeout(max(0.001, self.deadline - time.monotonic()))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def _input(self):
        auth = self.headers.get_all("Authorization", [])
        gate._require(len(auth) == 1 and secrets.compare_digest(auth[0], "Bearer " + self.server.bridge.token),
                      "native bridge authentication refused")
        gate._require(not self.headers.get_all("Transfer-Encoding", []), "chunked native bridge request refused")
        lengths = self.headers.get_all("Content-Length", [])
        gate._require(len(lengths) <= 1 and (not lengths or re.fullmatch(r"[0-9]{1,5}", lengths[0])),
                      "exact bounded native bridge content length required")
        length = int(lengths[0]) if lengths else 0
        gate._require(length <= MAX_BODY and (self.command != "GET" or length == 0), "native bridge body bound")
        self.connection.settimeout(max(0.001, self.deadline - time.monotonic()))
        raw = self.rfile.read(length) if length else b""
        gate._require(len(raw) == length and time.monotonic() < self.deadline, "native bridge request deadline")
        value = namespace._parsed(raw) if raw else None
        namespace._validate_request(self.server.server_port, self.command, self.path, self.server.bridge.token, value)
        return value, raw

    def _request(self):
        self.handled = True
        try:
            value, raw = self._input()
            status, body = self.server.bridge._forward(self.sequence, self.command, self.path, value, raw, self.deadline)
            self._reply(status, body)
        except Exception as error:
            self.server.bridge._failed(self.sequence, "request", error)
            try:
                self._reply(503, {"error": "native_bridge_unconfirmed"})
            except OSError:
                pass

    do_GET = _request
    do_POST = _request


class _Server(HTTPServer):
    def handle_error(self, request, client_address):
        # socketserver's default traceback could print private exception text.
        error = sys.exc_info()[1]
        self.bridge.failed = True
        try:
            self.bridge._failed(self.bridge.requests, "http_handler", error)
        except Exception:
            pass  # The preserved request intent remains unknown; no POST is retried.


class NativeControlBridge:
    """One finite, private CI bridge; context exit restores the fixture method."""
    def __init__(self, harness):
        gate._require(isinstance(harness, Harness) and re.fullmatch(r"[0-9a-f]{32}", harness.owner)
                      and namespace.HEX64.fullmatch(harness.token)
                      and harness.network == "miniflux-pause-" + harness.owner[:12] + "-network",
                      "owned synthetic harness required")
        self.harness, self.token = harness, harness.token
        self.root = Path(harness.output) / "native-control-bridge"
        self.root.mkdir(mode=0o700, exist_ok=False)
        self.command_lock = threading.RLock()
        self.original_command, self.wrapper = harness.command, self._command
        self.requests, self.failed, self.live = 0, False, False
        self.post_operations = set()
        self.stop_event = threading.Event()
        self.thread, self.server, self.request_deadline = None, None, None
        self.owner_pid = os.getpid()

    def __enter__(self):
        gate._require(not self.live and self.server is None, "bridge cannot restart")
        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.server.timeout = 0.1
        self.server.bridge = self
        self.thread = threading.Thread(target=self._serve, name="synthetic-native-control-bridge", daemon=True)
        try:
            gate._write(self.root / "START.json", {"schema": 1, "native_only": True, "production_access": False,
                "port": self.server.server_port, "max_requests": MAX_REQUESTS, "body_bytes": MAX_BODY,
                "request_seconds": REQUEST_SECONDS, "temporary_bridge_not_nsenter_proof": True})
            self.live = True
            self.harness.command = self.wrapper
            self.thread.start()
        except BaseException:
            self.live = False
            self.harness.command = self.original_command
            self.server.server_close()
            raise
        return self

    def __exit__(self, kind, error, traceback):
        self.stop_event.set()
        self.thread.join(REQUEST_SECONDS + 1)
        self.live = False
        self.server.server_close()
        failure = None
        try:
            gate._require(not self.thread.is_alive() and self.harness.command is self.wrapper, "native bridge shutdown unconfirmed")
            self.harness.command = self.original_command
            gate._write(self.root / "CLOSED.json", {"requests": self.requests, "failed": self.failed,
                        "native_only": True, "production_access": False})
        except Exception as close_error:
            failure = close_error
        if failure is not None:
            if error is not None:
                error.add_note("native bridge shutdown failure: " + type(failure).__name__)
            else:
                raise failure
        return False

    def _serve(self):
        try:
            while not self.stop_event.is_set() and self.requests < MAX_REQUESTS:
                self.server.handle_request()
            if self.requests >= MAX_REQUESTS:
                self._failed(self.requests, "request_budget", ValueError("native request budget exhausted"))
        except Exception as error:
            self._failed(self.requests, "server", error)
        finally:
            self.server.server_close()

    def _begin(self):
        gate._require(os.getpid() == self.owner_pid and self.requests < MAX_REQUESTS, "native bridge request budget exhausted")
        self.requests += 1
        return self.requests

    def _command(self, *args, **kwargs):
        if threading.current_thread() is not self.thread:
            with self.command_lock:
                return self.original_command(*args, **kwargs)
        remaining = self.request_deadline - time.monotonic()
        gate._require(remaining > 0 and self.command_lock.acquire(timeout=remaining), "native bridge command deadline")
        try:
            remaining = self.request_deadline - time.monotonic()
            gate._require(remaining > 0, "native bridge command deadline")
            kwargs["timeout"] = min(2, kwargs.get("timeout", 30), remaining)
            return self.original_command(*args, **kwargs)
        finally:
            self.command_lock.release()

    def _failed(self, sequence, stage, error):
        self.failed = True
        path = self.root / "STOP.json"
        if not gate._present(path) and not gate._present(path.with_suffix(".json.pending")):
            gate._write(path, {"request": sequence, "stage": stage, "error_type": type(error).__name__,
                "error_sha256": _hash(str(error).encode()), "post_retry": False, "next_action": "query_original_only"})

    def _forward(self, sequence, method, path, value, raw, deadline):
        gate._require(self.live and os.getpid() == self.owner_pid, "native bridge owner no longer live")
        if method == "POST":
            operation = value.get("operation_id")
            gate._require(not self.failed and isinstance(operation, str) and namespace.HEX64.fullmatch(operation)
                          and operation not in self.post_operations, "native bridge mutation cannot repeat")
            self.post_operations.add(operation)
        route = "/operations/<operation_id>" if path.startswith("/operations/") else path
        gate._write(self.root / f"request-{sequence:03d}.intent.json", {"method": method, "route": route, "path_sha256": _hash(path.encode()),
                    "body_sha256": _hash(raw), "forwarded_result_not_synthesized": True})
        status, body = self.harness.private_exchange(path, method, value)
        gate._require(type(status) is int and status in {200, 409} and isinstance(body, dict)
                      and len(namespace._encoded(body)) <= namespace.MAX_HTTP and time.monotonic() < deadline,
                      "native Go response unconfirmed")
        gate._write(self.root / f"request-{sequence:03d}.result.json", {"status": status,
                    "body_sha256": _hash(namespace._encoded(body)), "elapsed_within_seconds": REQUEST_SECONDS})
        return status, body

    def transport(self, token_file):
        gate._require(self.live and os.getpid() == self.owner_pid and self.thread.is_alive(), "live native bridge required")
        token_file = Path(token_file)
        gate._require(token_file.is_absolute() and not gate._present(token_file)
                      and not gate._present(token_file.with_suffix(".json.pending")), "new private synthetic token file required")
        gate._write(token_file, {"token": self.token})
        return NativeSenderActivationTransport(f"http://127.0.0.1:{self.server.server_port}", token_file, container_id=None)
