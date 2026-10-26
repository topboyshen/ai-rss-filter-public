from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_function_length.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("check_function_length", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_evaluate_rejects_new_long_function():
    gate = _load_gate()
    item = gate.FunctionLength("core/x.py", "new_big", 10, 121)
    errors = gate.evaluate([item], max_lines=120, allowlist={})
    assert any("NEW_LONG_FUNCTION" in error for error in errors)


def test_evaluate_rejects_allowlisted_growth():
    gate = _load_gate()
    item = gate.FunctionLength("core/x.py", "legacy_big", 10, 130)
    errors = gate.evaluate(
        [item], max_lines=120, allowlist={"core/x.py:legacy_big": 129}
    )
    assert any("LONG_FUNCTION_GREW" in error for error in errors)


def test_evaluate_requires_stale_allowlist_cleanup():
    gate = _load_gate()
    item = gate.FunctionLength("core/x.py", "legacy_big", 10, 120)
    errors = gate.evaluate(
        [item], max_lines=120, allowlist={"core/x.py:legacy_big": 130}
    )
    assert any("STALE_ALLOWLIST" in error for error in errors)


def test_shrinking_legacy_requires_tightening_before_future_growth():
    gate = _load_gate()
    item = gate.FunctionLength("core/x.py", "legacy_big", 10, 150)
    errors = gate.evaluate(
        [item], max_lines=120, allowlist={"core/x.py:legacy_big": 200}
    )
    assert len(errors) == 1
    assert errors[0].startswith("ALLOWLIST_NOT_TIGHTENED ")

    tightened = {"core/x.py:legacy_big": 150}
    assert gate.evaluate([item], max_lines=120, allowlist=tightened) == []

    regrown = gate.FunctionLength("core/x.py", "legacy_big", 10, 199)
    errors = gate.evaluate([regrown], max_lines=120, allowlist=tightened)
    assert len(errors) == 1
    assert errors[0].startswith("LONG_FUNCTION_GREW ")


def test_iter_function_lengths_uses_end_lineno(tmp_path):
    gate = _load_gate()
    path = tmp_path / "sample.py"
    path.write_text(
        "def ok():\n"
        "    return 1\n"
        "\n"
        "def big():\n"
        + "".join(f"    value_{i} = {i}\n" for i in range(121))
        + "    return value_0\n",
        encoding="utf-8",
    )
    lengths = {item.name: item.length for item in gate.iter_function_lengths(path)}
    assert lengths["ok"] == 2
    assert lengths["big"] == 123

    relative = gate.iter_function_lengths(path, root=tmp_path)
    assert {item.path for item in relative} == {"sample.py"}


def _checkout(path, *, length=123, allowlist=None):
    """Only initialize and stage this test's temporary repository."""
    path.mkdir(parents=True)
    source = path / "core" / "example.py"
    source.parent.mkdir()
    source.write_text(
        "def legacy_big():\n" + "    pass\n" * (length - 1), encoding="utf-8"
    )
    baseline = path / "ops" / "function_length_allowlist.json"
    baseline.parent.mkdir()
    baseline.write_text(json.dumps(allowlist or {}), encoding="utf-8")
    subprocess.run(
        ["git", "init", "--quiet", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "add", "core/example.py"],
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def _run_gate(root, *args):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "--root", str(root), *args],
        cwd=root.parent,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_main_accepts_same_relative_allowlist_in_different_checkouts(tmp_path):
    allowlist = {"core/example.py:legacy_big": 123}
    roots = [tmp_path / "checkout-one", tmp_path / "elsewhere" / "checkout-two"]
    for root in roots:
        _checkout(root, allowlist=allowlist)
        result = _run_gate(root)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "function length gate OK: max=120" in result.stdout


@pytest.mark.parametrize("length, expected_exit", [(119, 0), (120, 0), (121, 1)])
def test_main_default_limit_has_exact_120_line_boundary(
    tmp_path, length, expected_exit
):
    root = _checkout(tmp_path / "checkout", length=length)
    result = _run_gate(root)
    assert result.returncode == expected_exit, result.stdout + result.stderr
    if length > 120:
        assert (
            "NEW_LONG_FUNCTION core/example.py:legacy_big:1 121 > 120" in result.stdout
        )
    else:
        assert "function length gate OK: max=120" in result.stdout


def test_write_baseline_uses_relative_keys_and_is_accepted_by_main(tmp_path):
    root = _checkout(tmp_path / "checkout")
    baseline = root / "ops" / "function_length_allowlist.json"
    result = _run_gate(root, "--write-baseline", str(baseline))
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(baseline.read_text(encoding="utf-8")) == {
        "core/example.py:legacy_big": 123,
    }
    accepted = _run_gate(root)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
