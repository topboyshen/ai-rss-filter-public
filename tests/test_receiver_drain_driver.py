"""Bounded control uses real loopback HTTP; Docker rows are synthetic."""
import copy
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import production_receiver_driver as host  # noqa: E402
from receiver_drain_driver import ReceiverDrainDriver  # noqa: E402
from services import admission_fence as gate  # noqa: E402
from services.receiver_drain_control import DrainServer, ReceiverDrainControl  # noqa: E402
from tests.test_production_receiver_driver import FixtureTransport  # noqa: E402
from tests.test_receiver_drain_transfer import receiver, bundle_and_plan, pending  # noqa: E402,F401
from tests.test_webhook_durable_delivery import delivery  # noqa: E402,F401
from tests.test_webhook_durable_spool import isolated_spool  # noqa: E402,F401


@pytest.fixture
def driver(receiver):  # noqa: F811
    control = ReceiverDrainControl(receiver.fence)
    server = DrainServer(control, "e" * 64)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    token = receiver.root / "control-token.json"
    host.durable_write(token, {"token": "e" * 64})
    transport = FixtureTransport(f"http://127.0.0.1:{server.server_port}", token)
    transport.rows, transport.calls, transport.lose_ack = [], [], False
    binding, components = receiver.fence.binding, {}
    spool = receiver.delivery.webhook._webhook_spool_dir
    for index, role in enumerate(("web", "worker"), 1):
        mounts = [{"Type": "bind", "Source": str(path), "Destination": str(path), "RW": True}
                  for path in (receiver.fence.root, spool)]
        component = {"id": str(index) * 64, "image": binding["image_id"], "revision": binding["revision"],
                     "started_at": "2026-10-27T00:00:00Z", "restart_count": 0, "role": role,
                     "user": str(os.getuid()), "memory": 768 * 1024**2, "pids": 256, "mounts": mounts}
        components[role] = component
        transport.rows.append({"Id": component["id"], "Name": "/" + role, "Image": component["image"],
            "RestartCount": 0, "Config": {"User": component["user"], "Labels": {
                "org.opencontainers.image.revision": component["revision"]}, "Env": ["AI_FILTER_ROLE=" + role,
                "AI_FILTER_ADMISSION_MODE=enforced", "AI_FILTER_ADMISSION_ROOT=" + str(receiver.fence.root),
                "WEBHOOK_SPOOL_DIR=" + str(spool), "AI_FILTER_ADMISSION_BINDING=" + gate._encoded(binding).decode()]},
            "State": {"Running": True, "OOMKilled": False, "Paused": False, "Restarting": False,
                      "StartedAt": component["started_at"]},
            "HostConfig": {"Memory": component["memory"], "PidsLimit": component["pids"]}, "Mounts": mounts})
    now = int(time.time())
    plan = {"schema": 1, "scope": "instrumented_receiver_only", "sources": host.sources(),
            "components": components, "registration": control.registration(),
            "host_fence_root": str(receiver.fence.root), "container_fence_root": str(receiver.fence.root),
            "spool": str(spool), "container_spool_root": str(spool), "spool_identity": host.spool_identity(spool),
            "not_before": now - 1, "expires_at": now + 60, "owner": "synthetic operator",
            "capacity": {"stop_records": 128, "stop_bytes": 200 * 1024**2,
                         "minimum_free_bytes": host.receipt_format.MAX_ALIAS_RESERVATION}}
    receiver.host = host.ReceiverDriver(plan, transport, receiver.root / "host-evidence")
    receiver.transport, receiver.server = transport, server
    request, _ = bundle_and_plan(receiver, inside_fence=True)
    value = ReceiverDrainDriver(receiver.host, request, receiver.root / "transfer-evidence")
    try:
        yield value
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_control_prepares_then_queries_without_repost(receiver, driver):  # noqa: F811
    assert len(host.encoded(driver.request)) <= 8192
    result = driver.prepare()
    assert result["state"] == "complete" and len(pending(receiver)) == 1
    assert driver.query() == result
    assert [row[1] for row in receiver.transport.calls if row[0] == "POST"] == ["/drain/transfer"]


def test_lost_control_ack_exact_query_survives_window_without_new_post(receiver, driver):  # noqa: F811
    receiver.transport.lose_ack = True
    with pytest.raises(TimeoutError):
        driver.prepare()
    restarted = ReceiverDrainDriver(receiver.host, driver.request, driver.root)
    receiver.host.deadline = 0
    assert restarted.query()["state"] == "complete"
    with pytest.raises(Exception, match="window expired"):
        restarted.prepare()
    assert len([row for row in receiver.transport.calls if row[0] == "POST"]) == 1
    assert len(pending(receiver)) == 1


def test_control_query_rebind_rejects_identity_changed_during_get(receiver, driver, monkeypatch):  # noqa: F811
    driver.prepare()
    original = receiver.transport.request
    def drift(method, path, value=None):
        response = original(method, path, value)
        if path.startswith("/drain/operations/"):
            receiver.transport.rows[0]["RestartCount"] = 1
        return response
    monkeypatch.setattr(receiver.transport, "request", drift)
    with pytest.raises(Exception, match="identity"):
        driver.query()
    assert not list(driver.root.glob("response-*.json"))


def test_wrong_control_token_never_creates_transfer_intent(receiver, driver):  # noqa: F811
    wrong = receiver.root / "wrong-token.json"
    host.durable_write(wrong, {"token": "f" * 64})
    transport = host.NativeTransport(f"http://127.0.0.1:{receiver.server.server_port}", wrong)
    with pytest.raises(Exception, match="unconfirmed"):
        transport.request("POST", "/drain/transfer", driver.request)
    assert not (receiver.control.root / "intent.json").exists()
    assert pending(receiver) == []


def test_unbound_bundle_or_source_cannot_send_transfer(receiver, driver):  # noqa: F811
    request = copy.deepcopy(driver.request)
    request["sender_bundle"] = str(receiver.root / "unbound-originals")
    with pytest.raises(Exception, match="bound receiver fence"):
        ReceiverDrainDriver(receiver.host, request, receiver.root / "wrong-evidence")
    driver.request["source_sha256"]["app.py"] = "0" * 64
    with pytest.raises(Exception, match="changed"):
        driver.prepare()
    assert not [row for row in receiver.transport.calls if row[0] == "POST"]


def test_persistent_query_budget_no_unbounded_polling(receiver, driver):  # noqa: F811
    driver.prepare()
    for _ in range(32):
        assert driver.query()["state"] == "complete"
    with pytest.raises(Exception, match="query budget exhausted"):
        ReceiverDrainDriver(receiver.host, driver.request, driver.root).query()
    assert len([row for row in receiver.transport.calls if row[1].startswith("/drain/operations/")]) == 32
