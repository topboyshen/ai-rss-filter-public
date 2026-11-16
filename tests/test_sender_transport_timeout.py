import ast
import math
from pathlib import Path

import pytest

from scripts import production_sender_native_bridge  # noqa: F401  (adds scripts/ to sys.path)
from production_sender_driver import NativeSenderActivationTransport, NativeSenderTransport

ROOT = Path(__file__).resolve().parents[1]


def _make(tmp_path, **kwargs):
    token = tmp_path / "token.json"
    token.write_text('{"token": "' + "a" * 64 + '"}')
    token.chmod(0o600)
    return NativeSenderActivationTransport("http://127.0.0.1:9", token, **kwargs)


def test_default_timeout_stays_two_seconds(tmp_path):
    assert NativeSenderTransport.DEFAULT_TIMEOUT_SECONDS == 2
    assert _make(tmp_path).timeout_seconds == 2
    assert _make(tmp_path, timeout_seconds=20).timeout_seconds == 20


@pytest.mark.parametrize("bad", [0, -1, 30.001, 31, math.inf, math.nan, "20", True])
def test_timeout_is_validated(tmp_path, bad):
    with pytest.raises(Exception, match="timeout"):
        _make(tmp_path, timeout_seconds=bad)


def _constructions():
    for path in (ROOT / "scripts").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) in (
                    "NativeSenderActivationTransport", "NativeSenderTransport"):
                yield path.name, {k.arg for k in node.keywords}


def test_only_ci_bridge_passes_longer_timeout():
    rows = list(_constructions())
    assert len(rows) >= 4
    with_timeout = {name for name, keys in rows if "timeout_seconds" in keys}
    # supervisor only forwards the validated plan field (absent => default 2 s); see test_supervisor_bridge_timeout
    assert with_timeout == {"production_sender_native_bridge.py", "production_admission_supervisor.py"}
