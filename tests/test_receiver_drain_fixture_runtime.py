"""Actual standalone fixture child and HTTP, with an explicitly fake scorer."""
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from services import admission_fence as gate
from services import receiver_drain_storage as disk
from tests.test_webhook_durable_delivery import raw_body


def free_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


def test_standalone_real_http_receipt_and_bounded_fake_consumer(tmp_path):
    root = tmp_path / "native-fixture"
    root.mkdir(mode=0o700)
    now = int(time.time())
    binding = {"revision": "a" * 40, "image_id": "sha256:" + "b" * 64, "strategy": "native-joint-synthetic-v1",
               "source_sha256": gate.source_sha256(), "deployment_nonce": "c" * 32}
    gate.provision(root / "fence", binding, {"not_before": now - 1, "expires_at": now + 120,
                                          "max_admissions": 16, "max_bytes": 1024**2})
    gate.Fence(root / "fence", binding).open_trial("1" * 64, 0, "synthetic fixture")
    disk.once(root / "synthetic-owner.json", {"schema": 1, "synthetic": True, "nonce": "d" * 64})
    disk.once(root / "token.json", {"token": "e" * 64})
    disk.once(root / "config.json", {"scoring_strategy_version": binding["strategy"],
                                   "feeds": [{"url": "http://fixture:8080/feed", "name": "Synthetic joint feed"}]})
    port, control_port = free_port(), free_port()
    env = {**os.environ, "AI_FILTER_ROLE": "web", "WEBHOOK_SPOOL_DIR": str(root / "spool"),
           "AI_FILTER_ADMISSION_ROOT": str(root / "fence"), "AI_FILTER_ADMISSION_MODE": "enforced",
           "AI_FILTER_ADMISSION_BINDING": json.dumps(binding), "AI_FILTER_REVISION": binding["revision"],
           "MINIFLUX_WEBHOOK_SECRET": "synthetic-native-only", "AI_FILTER_DB_PATH": str(root / "unused.sqlite")}
    output = (root / "runtime.log").open("w")
    (root / "runtime.log").chmod(0o600)
    child = subprocess.Popen([sys.executable, "-m", "scripts.receiver_drain_fixture_runtime", "--root", str(root),
        "--token-file", str(root / "token.json"), "--port", str(port), "--control-port", str(control_port),
        "--seconds", "30", "--max-consume", "1"], cwd=Path(__file__).resolve().parents[1], env=env,
        stdout=output, stderr=subprocess.STDOUT)
    def post(path, raw, headers):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("POST", path, raw, headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()
    try:
        deadline = time.monotonic() + 10
        while not (root / "ready.json").exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (root / "ready.json").exists(), (root / "runtime.log").read_text()
        ready = disk.confirm(root / "ready.json")
        assert ready["synthetic"] is True and ready["model"] == "fake"
        body = raw_body(feed="http://fixture:8080/feed")
        headers = {"Content-Type": "application/json", "X-Miniflux-Signature": hmac.new(b"synthetic-native-only", body, hashlib.sha256).hexdigest()}
        assert post("/webhook", body, headers)[0] == 202
        gate.Fence(root / "fence", binding).stop_new("2" * 64, 1, "synthetic draining")
        status, value = post("/__synthetic__/consume", b"", {"Authorization": "Bearer " + "e" * 64})
        assert status == 200 and json.loads(value) == {"schema": 1, "consumed": True, "calls": 1, "real_model_calls": 0}, value
        status, value = post("/webhook/receipt", body, headers)
        assert status == 200 and json.loads(value)["state"] == "completed"
        assert post("/__synthetic__/consume", b"", {"Authorization": "Bearer " + "e" * 64})[0] == 503
        assert len(disk.confirm(root / "synthetic-scores.json")["calls"]) == 1
        assert not (root / "unused.sqlite").exists()
    finally:
        child.terminate()
        child.wait(timeout=5)
        output.close()
