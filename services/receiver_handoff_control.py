"""Authenticated stop-only control for an existing enforced receiver fence.

This is a receiver capability, not a first-takeover or deployment permit. The
host must independently bind every live web/worker to this fence. No provision,
open, restart, migration, cleanup, sender pause or shell operation is exposed.
"""
from __future__ import annotations

import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading

from services import admission_fence as gate

PROTOCOL = "receiver-stop-drain-v1"
MAX_BODY = 8192
MAX_RESPONSE = 65536


def source_sha256():
    from pathlib import Path
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def filesystem_identity(path):
    gate._directory(path)
    info = path.lstat()
    return {"device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid}


class ReceiverControl:
    def __init__(self, fence):
        self.fence = fence
        self.root_identity = filesystem_identity(fence.root)
        self.manifest_sha256 = gate._hash(fence._manifest())

    def registration(self):
        gate._require(filesystem_identity(self.fence.root) == self.root_identity
                      and gate._hash(self.fence._manifest()) == self.manifest_sha256,
                      "receiver fence identity changed")
        return {"schema": 1, "protocol": PROTOCOL, "source_sha256": source_sha256(),
                "manifest_sha256": self.manifest_sha256, "binding": self.fence.binding,
                "fence_root": str(self.fence.root), "filesystem": self.root_identity,
                "actions": ["stop_new", "freeze", "query"], "switch_authorized": False,
                "sender_controlled": False, "legacy_originals_proven": False}

    def snapshot(self):
        registration = self.registration()
        return {"registration": registration, "fence": self.fence.snapshot()}

    def operate(self, action, value):
        self.registration()
        gate._require(action in {"stop_new", "freeze"} and isinstance(value, dict)
                      and set(value) == {"operation_id", "generation", "reason"}, "exact stop operation required")
        if action == "stop_new":
            receipt = self.fence.stop_new(value["operation_id"], value["generation"], value["reason"])
        else:
            # Never stop consumers while a producer-stop receipt is absent.
            # The fence CAS enforces open -> draining -> frozen. Durable queue
            # emptiness must still be checked independently before a switch.
            receipt = self.fence.freeze(value["operation_id"], value["generation"], value["reason"], wait_seconds=0)
        return {"registration": self.registration(), "receipt": receipt}

    def query(self, operation_id):
        self.registration()
        return {"registration": self.registration(),
                "receipt": self.fence.query_operation(operation_id, wait_seconds=0)}


class ControlServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, control, token, port=0):
        gate._require(isinstance(token, str) and gate.HEX64.fullmatch(token), "private control token required")
        gate._require(type(port) is int and 0 <= port <= 65535, "invalid loopback port")
        self.control, self.token = control, token
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(("127.0.0.1", port), ControlHandler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        def expire():
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        # Socket timeouts alone restart on each byte and allow slow-drip
        # clients to retain all workers. Bound the whole connection as well.
        timer = threading.Timer(3, expire)
        timer.daemon = True
        timer.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            timer.cancel()
            self.slots.release()


class ControlHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(2)

    def log_message(self, *_args):
        pass

    def _reply(self, status, value):
        raw = gate._encoded(value)
        gate._require(len(raw) <= MAX_RESPONSE, "response capacity exceeded")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def _authorized(self):
        values = self.headers.get_all("Authorization") or []
        valid = (len(values) == 1 and values[0].isascii()
                 and hmac.compare_digest(values[0], "Bearer " + self.server.token))
        if not valid:
            self._reply(401, {"error": "unauthorized", "durable": False})
        return valid

    def do_GET(self):
        if not self._authorized():
            return
        try:
            gate._require(not self.headers.get_all("Transfer-Encoding")
                          and self.headers.get_all("Content-Length") in (None, ["0"]), "unexpected body")
            if self.path == "/registration":
                result = self.server.control.registration()
            elif self.path == "/snapshot":
                result = self.server.control.snapshot()
            elif self.path.startswith("/operations/") and gate.HEX64.fullmatch(self.path[12:]):
                result = self.server.control.query(self.path[12:])
            else:
                self._reply(404, {"error": "unsupported_operation", "durable": False})
                return
            self._reply(200, result)
        except Exception:
            self._reply(409, {"error": "unconfirmed", "durable": False})

    def do_POST(self):
        if not self._authorized():
            return
        if self.path not in {"/stop-new", "/freeze"}:
            self._reply(404, {"error": "unsupported_operation", "durable": False})
            return
        try:
            lengths = self.headers.get_all("Content-Length") or []
            gate._require(not self.headers.get_all("Transfer-Encoding") and len(lengths) == 1
                          and lengths[0].isascii() and lengths[0].isdecimal()
                          and 0 < int(lengths[0]) <= MAX_BODY
                          and self.headers.get_all("Content-Type") == ["application/json"], "bounded JSON required")
            raw = self.rfile.read(int(lengths[0]))
            gate._require(len(raw) == int(lengths[0]), "incomplete body")
            result = self.server.control.operate(self.path[1:].replace("-", "_"), gate._parsed(raw))
            self._reply(200, result)
        except Exception:
            # Timeout/409 never means the operation was cancelled. Caller must
            # retain its intent and query precisely this operation identity.
            self._reply(409, {"error": "unconfirmed", "durable": False})


def main():
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    fence = gate._configured()
    gate._require(fence is not None, "an existing enforced receiver fence is required")
    token = gate._read(args.token_file)
    gate._require(isinstance(token, dict) and set(token) == {"token"}, "exact private token file required")
    with ControlServer(ReceiverControl(fence), token["token"], args.port) as server:
        server.serve_forever(poll_interval=0.1)


if __name__ == "__main__":
    main()
