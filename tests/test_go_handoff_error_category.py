"""CI-only refusal diagnostics: Go enum header -> harness -> bridge result.json -> watchdog record."""
import http.server
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from scripts import production_sender_native_bridge as bridge_module
from scripts import activation_window_diagnostics as window
from scripts import verify_miniflux_pause_native as pause_native
from services import admission_fence as gate
from tests.test_production_sender_native_bridge import request

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "ops/miniflux/0002-unified-pause-and-held-originals.patch"


@pytest.mark.parametrize("value,expected", [(None, None), ("uncertain", "uncertain"), ("lock", "lock"),
                                            ("password=x", "invalid"), ("", "invalid")])
def test_error_category_is_fixed_enum(value, expected):
    assert pause_native.error_category(value) == expected


def test_go_patch_sets_header_only_and_keeps_status_and_body():
    text = PATCH.read_text()
    handler = text[text.index("+func handoffControlHandler"):text.index("+func startHandoffControl")]
    assert handler.count("w.Header().Set(handoffCategoryHeader, handoffErrorCategory(err))") == 2
    assert '+		json.NewEncoder(w).Encode(map[string]any{"result": result, "confirmed": confirmed})' in handler
    assert "error_category" not in handler and handler.count("http.StatusConflict") == 2
    enum = text[text.index("+func handoffErrorCategory"):text.index("+func handoffFields")]
    returned = {line.split('"')[1] for line in enum.splitlines() if 'return "' in line}
    assert returned == set(pause_native.ERROR_CATEGORIES)
    assert "err.Error()" not in enum and "Message" not in enum


class _Conflict(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b'{"confirmed":false,"result":{"schema":0}}'
        self.send_response(409)
        self.send_header("X-Handoff-Error-Category", "uncertain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def test_fixed_client_captures_header_without_changing_body():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Conflict)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = {"url": f"http://127.0.0.1:{server.server_port}/operations/x", "method": "GET", "value": None, "auth": None}
        out = subprocess.run([sys.executable, "-B", "-c", pause_native.CLIENT], input=json.dumps(payload),
                             capture_output=True, text=True, timeout=10, check=True).stdout
    finally:
        server.shutdown()
        server.server_close()
    value = json.loads(out)
    assert value == {"status": 409, "body": '{"confirmed":false,"result":{"schema":0}}', "category": "uncertain"}


@pytest.fixture
def harness(tmp_path):
    class SyntheticHarness(bridge_module.Harness):
        def __init__(self):
            self.owner, self.token = "a" * 32, "b" * 64
            self.network = "miniflux-pause-" + self.owner[:12] + "-network"
            self.output = tmp_path
            self.sequence, self.commands = 0, []
            self.status, self.category = 409, "uncertain"
        def command(self, args, **kwargs):
            self.sequence += 1
            return "backend"
        def private_exchange(self, path, method="GET", value=None):
            self.command(["synthetic-forward"], timeout=15)
            time.sleep(0.05)
            self.last_error_category = self.category
            return self.status, {"result": {}, "confirmed": self.status == 200}
    return SyntheticHarness()


def test_bridge_result_records_elapsed_and_category_and_record_summarizes(harness):
    with bridge_module.NativeControlBridge(harness) as bridge:
        assert request(bridge, "GET", "/operations/" + "c" * 64)[0] == 409
        harness.status, harness.category = 200, "lock"
        assert request(bridge, "GET", "/operations/" + "c" * 64)[0] == 200
        first = gate._read(bridge.root / "request-001.result.json")
        second = gate._read(bridge.root / "request-002.result.json")
        summary = window._bridge(bridge.root)
    assert first["error_category"] == "uncertain" and 0.05 <= first["elapsed_seconds"] < bridge_module.REQUEST_SECONDS
    assert first["elapsed_within_seconds"] == bridge_module.REQUEST_SECONDS
    assert second["error_category"] is None  # only refusals carry a category
    assert summary["last_result"]["status"] == "200" and summary["last_result"]["error_category"] == "None"
