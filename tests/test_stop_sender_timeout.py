"""Restrictive stop rebuilds the sender transport but must keep its bounded timeout."""
from pathlib import Path
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import production_sender_driver as driver_module  # noqa: E402
from production_sender_driver import NativeSenderActivationTransport, NativeSenderTransport  # noqa: E402
from services import production_admission_lifecycle as life  # noqa: E402

CONTAINER = "c" * 64


@pytest.fixture
def token(tmp_path):
    path = tmp_path / "t.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"token": "a" * 64}, stream)
    return path


def run_stop(tmp_path, monkeypatch, transport):
    captured = {}

    class FakeDriver:
        def __init__(self, plan, transport, postgres, evidence_root):
            captured["transport"] = transport
            self.control = SimpleNamespace(root=tmp_path / "control")

        def stop_new(self, operation, reason):
            return {"operation": operation}

    monkeypatch.setattr(driver_module, "SenderDriver", FakeDriver)
    monkeypatch.setattr(life, "_stop_plan", lambda root, authority, sender: {})
    authority = {"identity": {"sender_plan_sha256": "p"}}
    sender = SimpleNamespace(plan_sha256="p", transport=transport, postgres=None)
    life._stop_sender(tmp_path, authority, sender)
    return captured["transport"]


@pytest.mark.parametrize("timeout", [20, 0.5, 30])
def test_stop_transport_keeps_activation_timeout(tmp_path, monkeypatch, token, timeout):
    active = NativeSenderActivationTransport("http://127.0.0.1:8787", token,
                                             container_id=CONTAINER, timeout_seconds=timeout)
    stop = run_stop(tmp_path, monkeypatch, active)
    assert type(stop) is NativeSenderTransport
    assert stop.timeout_seconds == timeout
    assert (stop.port, stop.token_file, stop.container_id) == (8787, token, CONTAINER)


def test_stop_transport_default_stays_two_seconds(tmp_path, monkeypatch, token):
    active = NativeSenderActivationTransport("http://127.0.0.1:8787", token)
    stop = run_stop(tmp_path, monkeypatch, active)
    assert stop.timeout_seconds == NativeSenderTransport.DEFAULT_TIMEOUT_SECONDS == 2


def test_stop_transport_whitelist_is_ordinary_not_activation(tmp_path, monkeypatch, token):
    active = NativeSenderActivationTransport("http://127.0.0.1:8787", token, timeout_seconds=20)
    stop = run_stop(tmp_path, monkeypatch, active)
    assert stop._allowed.__func__ is NativeSenderTransport._allowed
    for method, path in (("POST", "/initialize-closed"), ("POST", "/production-resume"),
                         ("GET", "/activation-registration")):
        assert active._allowed(method, path)
        assert not stop._allowed(method, path)


def test_bounds_still_enforced(token):
    for bad in (31, 0, -1, float("inf")):
        with pytest.raises(Exception, match="bounded sender control timeout"):
            NativeSenderTransport("http://127.0.0.1:8787", token, timeout_seconds=bad)
