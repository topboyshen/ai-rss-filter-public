"""Exact command budgets without argv, streams, Docker or new timeout policy."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from scripts import verify_miniflux_pause_native as native  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    previous = os.umask(0o077)
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(native.time, "monotonic", lambda: clock.now)
    value = object.__new__(native.Harness)
    value.output, value.sequence, value.started = tmp_path, 0, clock.now
    value.phase_deadline, value.env = None, {"PRIVATE_ENV": "never-export"}
    value.password, value.token, value.auth = "secret-password", "secret-token", "secret-auth"
    try:
        yield value, clock
    finally:
        os.umask(previous)


def completed(monkeypatch, clock, *, rc=0, stdout="ordinary", stderr="secret-stderr", seconds=1.25, during=None):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if during:
            during(args, kwargs)
        clock.now += seconds
        return subprocess.CompletedProcess(args, rc, stdout, stderr)

    monkeypatch.setattr(native.subprocess, "run", run)
    return calls


def test_observation_is_filled_before_real_call_then_contains_only_actual_safe_fields(client, monkeypatch):
    h, clock = client
    observation = {}

    def inspect(_args, kwargs):
        assert observation == {"schema": 1, "sequence": 1, "operation": "docker_exec", "timeout_seconds": 30,
                               "harness_remaining_seconds": 600, "elapsed_seconds": None,
                               "returncode": None, "outcome": "unconfirmed"}
        assert kwargs["timeout"] == observation["timeout_seconds"]

    calls = completed(monkeypatch, clock, during=inspect)
    assert h.command(["exec", "secret-container", "private-program"], stdin="private-body", command_observation=observation) == "ordinary"
    assert observation["elapsed_seconds"] == 1.25 and observation["returncode"] == 0 and observation["outcome"] == "returned"
    assert len(calls) == 1
    raw = json.dumps(observation)
    assert len(raw) < 1024
    assert all(secret not in raw for secret in ("private-body", "private-program", "secret-container", "secret-stderr", "never-export", str(h.output)))


@pytest.mark.parametrize("started,phase,timeout,final,expected,remaining", [
    (100, None, 30, False, 30, 600),
    (-485, None, 30, False, 15, 15),
    (100, 107, 30, False, 7, 7),
    (-485, 107, 4, False, 4, 7),
    (-505, 90, 5, True, 5, -5),
])
def test_actual_budget_preserves_total_phase_requested_and_final_rules(client, monkeypatch, started, phase, timeout, final, expected, remaining):
    h, clock = client
    h.started, h.phase_deadline = started, phase
    calls = completed(monkeypatch, clock)
    observation = {}
    assert h.command(["inspect", "private"], timeout=timeout, final=final, command_observation=observation) == "ordinary"
    assert calls[0][1]["timeout"] == observation["timeout_seconds"] == expected
    assert observation["harness_remaining_seconds"] == remaining


def test_expired_precommand_budget_still_does_not_spawn(client, monkeypatch):
    h, _clock = client
    h.phase_deadline = 99
    monkeypatch.setattr(native.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("expired command spawned"))
    observation = {}
    with pytest.raises(TimeoutError, match="bounded isolated rehearsal elapsed"):
        h.command(["inspect", "private"], command_observation=observation)
    assert observation == {} and not list(h.output.iterdir())


@pytest.mark.parametrize("allowed", [False, True])
def test_timeout_retains_partial_original_rc_none_and_same_failure_decision(client, monkeypatch, allowed):
    h, clock = client
    calls = []

    def timeout(args, **kwargs):
        calls.append(kwargs["timeout"])
        clock.now += kwargs["timeout"]
        raise subprocess.TimeoutExpired(args, kwargs["timeout"], output=b"partial-original", stderr=b"private-stderr")

    monkeypatch.setattr(native.subprocess, "run", timeout)
    observation, retained_result = {}, {}
    if allowed:
        assert h.command(["run", "private"], timeout=3, allowed_failure=True, command_result=retained_result,
                         command_observation=observation) == "partial-original"
    else:
        with pytest.raises(RuntimeError, match="isolated command 1 failed rc=None"):
            h.command(["run", "private"], timeout=3, command_result=retained_result, command_observation=observation)
    assert calls == [3] and observation["outcome"] == "timeout"
    assert observation["returncode"] is None and observation["elapsed_seconds"] == 3
    original = h.output / "command-0001.json"
    raw = json.loads(original.read_bytes())
    assert raw["rc"] is None and raw["stdout"] == "partial-original" and raw["stderr"] == "private-stderr"
    assert native.read_command_result(original, 1, retained_result)["returncode"] is None
    assert "private" not in json.dumps(observation) and "partial-original" not in json.dumps(observation)


@pytest.mark.parametrize("allowed", [False, True])
def test_nonzero_is_observed_without_overriding_failure_policy(client, monkeypatch, allowed):
    h, clock = client
    completed(monkeypatch, clock, rc=7)
    observation = {}
    if allowed:
        assert h.command(["inspect", "private"], allowed_failure=True, command_observation=observation) == "ordinary"
    else:
        with pytest.raises(RuntimeError, match="failed rc=7"):
            h.command(["inspect", "private"], command_observation=observation)
    assert observation["returncode"] == 7 and observation["outcome"] == "returned"


def test_spawn_exception_is_same_object_without_synthetic_returncode(client, monkeypatch):
    h, clock = client
    error = OSError("private exec path")

    def fail(*_args, **_kwargs):
        clock.now += 0.5
        raise error

    monkeypatch.setattr(native.subprocess, "run", fail)
    observation = {}
    with pytest.raises(OSError) as raised:
        h.command(["run", "private"], command_observation=observation)
    assert raised.value is error
    assert observation["outcome"] == "unconfirmed" and observation["returncode"] is None and observation["elapsed_seconds"] == 0.5
    assert list(h.output.iterdir()) == []


@pytest.mark.parametrize("invalid", [{"preexisting": "preserve"}, [], 0, False, "secret"])
def test_invalid_observation_mapping_is_rejected_before_command_or_evidence(client, monkeypatch, invalid):
    h, _clock = client
    monkeypatch.setattr(native.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("bad observation spawned"))
    with pytest.raises(ValueError, match="command observation must be a fresh output mapping"):
        h.command(["inspect", "private"], command_observation=invalid)
    assert h.sequence == 0 and not list(h.output.iterdir())


def test_default_off_does_not_classify_or_change_logs_return(client, monkeypatch):
    h, clock = client
    completed(monkeypatch, clock)
    monkeypatch.setattr(native, "_command_observation", lambda *_: pytest.fail("default-off observation"))
    assert h.command(["logs", "private"]) == "ordinarysecret-stderr"
    original = json.loads((h.output / "command-0001.json").read_text())
    assert original["rc"] == 0 and original["stdout"] == "ordinary" and original["stderr"] == "secret-stderr"
    assert not (h.output / "command-0001.result.json").exists()


def test_unknown_operation_does_not_export_user_text(client, monkeypatch):
    h, clock = client
    completed(monkeypatch, clock)
    observation = {}
    h.command(["secret-subcommand", "secret-argument"], command_observation=observation)
    assert observation["operation"] == "unknown" and "secret" not in json.dumps(observation)


def test_reusing_filled_mapping_cannot_overwrite_evidence(client, monkeypatch):
    h, clock = client
    calls = completed(monkeypatch, clock)
    observation = {}
    h.command(["ps"], command_observation=observation)
    frozen = dict(observation)
    with pytest.raises(ValueError, match="fresh output mapping"):
        h.command(["ps"], command_observation=observation)
    assert len(calls) == 1 and observation == frozen and h.sequence == 1
