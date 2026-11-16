"""Synthetic-only RSS/webhook endpoint for the fixed-image pause rehearsal.

No proxying or external network calls. All original requests survive in the
private evidence mount; a hold is an explicit handshake, not a timing guess.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import threading
from xml.sax.saxutils import escape

MAX_BODY = 1024 * 1024


def canonical(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False).encode()


class Fixture:
    def __init__(self, root, token):
        self.root, self.token = Path(root), token
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.events, self.items, self.hold = [], ["seed"], False
        self.release_generation = 0

    def record(self, value):
        with self.lock:
            if len(self.events) >= 1000:
                raise ValueError("bounded synthetic evidence capacity exhausted")
            row = {"sequence": len(self.events) + 1, "utc": datetime.now(timezone.utc).isoformat(), **value}
            path = self.root / f"event-{row['sequence']:05d}.json"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(canonical(row))
                stream.flush()
                os.fsync(stream.fileno())
            directory = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self.events.append(row)
            self.condition.notify_all()
            return row

    def rss(self):
        with self.lock:
            items = list(self.items)
        self.record({"kind": "feed", "items": items})
        rows = [f"<item><title>Synthetic {escape(key)}</title><link>https://example.invalid/{escape(key)}</link>"
                f"<guid>{escape(key)}</guid><description>Synthetic isolated fixture {escape(key)}</description></item>" for key in items]
        return ('<?xml version="1.0"?><rss version="2.0"><channel><title>Synthetic</title>'
                '<link>https://example.invalid/</link><description>Isolated only</description>'
                + "".join(rows) + "</channel></rss>").encode()

    def webhook(self, body, headers, legacy=False):
        with self.condition:
            generation, hold = self.release_generation, self.hold
            row = self.record({"kind": "legacy_received" if legacy else "webhook_received", "body_b64": base64.b64encode(body).decode(),
                               "body_sha256": hashlib.sha256(body).hexdigest(), "held": hold,
                               "delivery_id": headers.get("X-AI-Filter-Delivery-ID"),
                               "signature": headers.get("X-Miniflux-Signature"), "event": headers.get("X-Miniflux-Event-Type")})
            released = not hold or self.condition.wait_for(lambda: self.release_generation > generation, timeout=15)
        if not released:
            self.record({"kind": "fixture_hold_timeout", "request_sequence": row["sequence"]})
            return 503, {"error": "bounded_fixture_hold_timeout"}
        digest = row["body_sha256"]
        self.record({"kind": "legacy_response" if legacy else "webhook_response", "request_sequence": row["sequence"], "status": 202})
        # This fixture only emulates the exact protocol ACK. It is not an
        # AI-Filter durable receiver proof and does not run a model.
        return 202, {"schema": 1, "durable": True, "delivery_id": digest, "body_sha256": digest}

    def control(self, value):
        if not isinstance(value, dict) or set(value) != {"items", "hold", "release"}:
            raise ValueError("exact synthetic control required")
        if (type(value["hold"]) is not bool or type(value["release"]) is not bool
                or not isinstance(value["items"], list) or not 1 <= len(value["items"]) <= 20
                or any(not isinstance(v, str) or not re.fullmatch(r"[a-z0-9_-]{1,40}", v) for v in value["items"])):
            raise ValueError("invalid synthetic control")
        with self.condition:
            self.items, self.hold = value["items"], value["hold"]
            if value["release"]:
                self.release_generation += 1
                self.condition.notify_all()
            return {"items": self.items, "hold": self.hold, "release_generation": self.release_generation}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, status, body, content_type="application/json"):
        raw = body if isinstance(body, bytes) else canonical(body)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self):
        values = self.headers.get_all("Authorization") or []
        return len(values) == 1 and values[0].isascii() and hmac.compare_digest(values[0], "Bearer " + self.server.fixture.token)

    def do_GET(self):
        if self.path == "/feed":
            self.reply(200, self.server.fixture.rss(), "application/rss+xml")
        elif self.path == "/health":
            self.reply(200, {"status": "synthetic_fixture_ready"})
        elif self.path == "/events" and self.authorized():
            with self.server.fixture.lock:
                self.reply(200, {"events": list(self.server.fixture.events)})
        else:
            self.reply(403, {"error": "not_authorized_or_unknown_path"})

    def do_POST(self):
        try:
            lengths = self.headers.get_all("Content-Length") or []
            if len(lengths) != 1 or self.headers.get_all("Transfer-Encoding"):
                raise ValueError("fixed body required")
            length = int(lengths[0])
            if not 0 <= length <= MAX_BODY:
                raise ValueError("body exceeds fixture bound")
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("truncated request")
            if self.path in {"/hook", "/api/bookmarks/"}:
                status, value = self.server.fixture.webhook(body, self.headers, legacy=self.path != "/hook")
                self.reply(status, value)
            elif self.path == "/control" and self.authorized():
                self.reply(200, self.server.fixture.control(json.loads(body)))
            else:
                self.reply(403, {"error": "not_authorized_or_unknown_path"})
        except (ValueError, OSError):
            self.reply(400, {"error": "invalid_or_failed_fixture_request"})


def serve_until_term(server):
    """Stop accepting on TERM; exit only after accepted request threads finish."""
    stopping, finished = False, threading.Event()
    # ThreadingMixIn.server_close waits for these threads. In particular, a
    # held request must not disappear merely because the fixture received TERM.
    server.daemon_threads = False
    server.block_on_close = True

    def request_stop(_number, _frame):
        # Signal handlers can reenter: no locks or Event.set calls here.
        nonlocal stopping
        stopping = True

    def stop():
        while not stopping and not finished.wait(0.05):
            pass
        if not finished.is_set():
            # shutdown must run outside the serve_forever thread.
            server.shutdown()

    previous = signal.signal(signal.SIGTERM, request_stop)
    # A failure before serve_forever initializes must propagate even when TERM
    # races with it. A blocked shutdown helper cannot hold that failure hostage.
    thread = threading.Thread(target=stop, name="synthetic-http-stop", daemon=True)
    try:
        thread.start()
        server.serve_forever(poll_interval=0.05)
        if not stopping:
            raise RuntimeError("synthetic HTTP server stopped without TERM")
    finally:
        finished.set()
        try:
            server.server_close()
        finally:
            signal.signal(signal.SIGTERM, previous)


def main():
    token = os.environ["MINIFLUX_PAUSE_FIXTURE_TOKEN"]
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError("synthetic private control token required")
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    server.fixture = Fixture("/evidence", token)
    serve_until_term(server)


if __name__ == "__main__":
    main()
