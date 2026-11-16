import ast
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import production_admission_supervisor as supervisor
from scripts import production_admission_watchdog as watchdog
from scripts import production_sender_native_bridge as bridge
from production_sender_driver import NativeSenderActivationTransport

ROOT = Path(__file__).resolve().parents[1]
BASE = {k: None for k in supervisor.KEYS}


def _transport(tmp_path, **kwargs):
    token = tmp_path / "token.json"
    if not token.exists():
        token.write_text('{"token": "' + "a" * 64 + '"}')
        token.chmod(0o600)
    return NativeSenderActivationTransport("http://127.0.0.1:9", token, **kwargs)


def test_production_transport_gets_no_field_and_two_seconds(tmp_path):
    prod = SimpleNamespace(transport=_transport(tmp_path, container_id="d" * 64))
    assert supervisor.bridge_timeout_field(prod) == {}
    assert supervisor.bridge_timeout_field(SimpleNamespace(transport=_transport(tmp_path))) == {}
    plan = dict(BASE, sender_namespace_container_id="d" * 64)
    assert supervisor.plan_keys_valid(plan, supervisor.KEYS)
    assert plan.get(supervisor.TIMEOUT_KEY) is None  # sender_from_plan passes None => default 2 s
    assert NativeSenderActivationTransport.DEFAULT_TIMEOUT_SECONDS == 2


def test_bridge_transport_gets_twenty(tmp_path):
    ci = SimpleNamespace(transport=_transport(tmp_path, timeout_seconds=bridge.CLIENT_SECONDS))
    assert supervisor.bridge_timeout_field(ci) == {supervisor.TIMEOUT_KEY: 20}
    plan = dict(BASE, sender_namespace_container_id=None, **{supervisor.TIMEOUT_KEY: 20})
    assert supervisor.plan_keys_valid(plan, supervisor.KEYS)
    guardian = dict({k: None for k in watchdog.KEYS}, **{supervisor.TIMEOUT_KEY: 20})
    assert supervisor.plan_keys_valid(guardian, watchdog.KEYS)


def test_longer_timeout_refused_with_namespace_container(tmp_path):
    with pytest.raises(Exception, match="CI bridge only"):
        supervisor.bridge_timeout_field(SimpleNamespace(transport=_transport(tmp_path, container_id="d" * 64,
                                                                             timeout_seconds=20)))
    plan = dict(BASE, sender_namespace_container_id="d" * 64, **{supervisor.TIMEOUT_KEY: 20})
    assert not supervisor.plan_keys_valid(plan, supervisor.KEYS)


@pytest.mark.parametrize("bad", [0, -1, 31, math.inf, "20", None, True])
def test_invalid_plan_values_rejected(bad):
    plan = dict(BASE, sender_namespace_container_id=None, **{supervisor.TIMEOUT_KEY: bad})
    assert not supervisor.plan_keys_valid(plan, supervisor.KEYS)
    assert not supervisor.plan_keys_valid(dict(BASE, other=1), supervisor.KEYS)
    assert not supervisor.plan_keys_valid({k: None for k in list(supervisor.KEYS)[1:]}, supervisor.KEYS)


def test_only_bridge_constructs_a_longer_timeout_transport():
    literal = set()
    for path in (ROOT / "scripts").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            name = getattr(getattr(node, "func", None), "id", getattr(getattr(node, "func", None), "attr", None))
            if isinstance(node, ast.Call) and name in ("NativeSenderActivationTransport", "NativeSenderTransport"):
                literal |= {(path.name, ast.unparse(k.value)) for k in node.keywords if k.arg == "timeout_seconds"}
    assert literal == {("production_sender_native_bridge.py", "CLIENT_SECONDS"),
                       ("production_admission_supervisor.py", "plan.get(TIMEOUT_KEY)")}
