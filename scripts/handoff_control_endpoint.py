"""Authenticated loopback control for a real isolation HostCronGate only.

This does not install a Miniflux scheduler/async pause hook. GET operation queries
may strictly synchronize original state; they are not read-only diagnostics.
"""
from __future__ import annotations

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading

from durable_handoff_cutover import encoded, parsed, read_private, require
from handoff_host_cron import HostCronGate, HEX64

MAX_BODY = 8192


class ControlServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, root, token, port=0):
        require(isinstance(token, str) and HEX64.fullmatch(token), "private isolation token required")
        require(type(port) is int and 0 <= port <= 65535, "invalid loopback port")
        self.gate = HostCronGate(root)
        self.token = token
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
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class ControlHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(2)

    def log_message(self, *_args):
        pass

    def _reply(self, status, value):
        raw = encoded(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self):
        values = self.headers.get_all("Authorization") or []
        valid = len(values) == 1 and values[0].isascii() and hmac.compare_digest(values[0], "Bearer " + self.server.token)
        if not valid:
            self._reply(401, {"error": "unauthorized", "durable": False})
        return valid

    def do_GET(self):
        if not self._authorized():
            return
        try:
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
                self._reply(400, {"error": "unexpected_body", "durable": False})
            elif self.path == "/registration":
                self._reply(200, self.server.gate.registration())
            elif re.fullmatch(r"/operations/[0-9a-f]{64}", self.path):
                result = self.server.gate.query(self.path.rsplit("/", 1)[-1], wait_seconds=0)
                self._reply(404 if result is None else 200, result)
            else:
                self._reply(404, {"error": "unknown_path", "durable": False})
        except Exception:
            self._reply(409, {"error": "unconfirmed", "durable": False})

    def _body(self):
        lengths = self.headers.get_all("Content-Length") or []
        require(not self.headers.get_all("Transfer-Encoding") and len(lengths) == 1
                and re.fullmatch(r"[0-9]+", lengths[0]), "exact bounded body required")
        length = int(lengths[0])
        require(0 < length <= MAX_BODY and self.headers.get("Content-Type") == "application/json", "body size/type invalid")
        raw = self.rfile.read(length)
        require(len(raw) == length, "incomplete body")
        value = parsed(raw)
        require(isinstance(value, dict) and set(value) == {"operation_id", "generation", "reason"}, "exact stop fields required")
        return value

    def do_POST(self):
        if not self._authorized():
            return
        if self.path != "/stop":
            self._reply(404, {"error": "unknown_path", "durable": False})
            return
        try:
            value = self._body()
        except Exception:
            self._reply(400, {"error": "invalid_body", "durable": False})
            return
        try:
            receipt = self.server.gate.stop(value["operation_id"], value["generation"], value["reason"], wait_seconds=0)
        except Exception:
            self._reply(409, {"error": "unconfirmed", "durable": False})
            return
        self._reply(200, receipt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--isolation", action="store_true", required=True)
    args = parser.parse_args()
    value = read_private(args.token_file)
    require(isinstance(value, dict) and set(value) == {"token"}, "exact private token record required")
    with ControlServer(args.root, value["token"]) as server:
        print(json.dumps({"port": server.server_port, "scope": "isolation"}), flush=True)
        server.serve_forever(poll_interval=0.05)


if __name__ == "__main__":
    main()
