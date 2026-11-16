import ast
from pathlib import Path

from scripts import verify_production_activation_native as native

ROOT = Path(__file__).resolve().parents[1]


def test_ci_gets_90_and_default_stays_25():
    assert native.guardian_exit_seconds({"GITHUB_ACTIONS": "true", "CI": "true"}) == 90
    assert native.guardian_exit_seconds({}) == 25
    assert native.guardian_exit_seconds({"CI": "true"}) == 25
    assert native.guardian_exit_seconds({"GITHUB_ACTIONS": "1", "CI": "true"}) == 25
    assert native.guardian_exit_seconds({"GITHUB_ACTIONS": "true"}) == 25


def test_reads_process_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CI", "true")
    assert native.guardian_exit_seconds() == 90
    monkeypatch.delenv("GITHUB_ACTIONS")
    assert native.guardian_exit_seconds() == 25


def test_guardian_wait_is_wired_and_no_literal_wait_remains():
    tree = ast.parse((ROOT / "scripts/verify_production_activation_native.py").read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "complete_native_supervision")
    waits = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "wait"]
    assert len(waits) == 1 and waits[0].keywords[0].arg == "timeout"
    value = waits[0].keywords[0].value
    assert isinstance(value, ast.Call) and value.func.id == "guardian_exit_seconds" and not value.args

