"""Offline harness bookkeeping only; no Docker, PG, production or network."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("held_local", ROOT / "scripts/verify_miniflux_held_local.py")
held = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(held)


def test_timeout_preserves_partial_and_true_unknown_exit(monkeypatch):
    records = []

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 9, output=b"partial SECRET", stderr=b"failure SECRET")

    monkeypatch.setattr(held.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="partial output retained"):
        held.recorded_command(["synthetic", "SECRET"], timeout=9, env=None, cwd=None,
                              password="SECRET", save=records.append)
    assert len(records) == 1
    record = records[0]
    assert record["timed_out"] is True
    assert record["exit_code"] is None
    assert record["timeout_seconds"] == 9
    assert record["stdout"] == "partial <synthetic-secret>"
    assert record["stderr"] == "failure <synthetic-secret>"
    assert record["elapsed_seconds"] >= 0
    assert "SECRET" not in json.dumps(record)


@pytest.mark.parametrize("kind", ["wrong_owner", "stop_fails", "still_running", "changed_id", "bad_exit"])
def test_owned_stop_never_claims_unconfirmed_success(kind):
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "stop":
            if kind == "stop_fails":
                raise RuntimeError("failed exact stop")
            return "fixture"
        post = len(calls) > 1
        owner = "different" if kind == "wrong_owner" else "owner"
        return json.dumps([{"Id": "other" if post and kind == "changed_id" else "fixture",
                            "Config": {"Labels": {"ai-filter.held-isolation": owner}},
                            "State": {"Running": not post or kind == "still_running",
                                      "ExitCode": 1 if kind == "bad_exit" else 0}}])

    with pytest.raises(RuntimeError):
        held.stop_owned(docker, "fixture", "owner")
    if kind == "wrong_owner":
        assert not any(args[0] == "stop" for args in calls)


@pytest.mark.parametrize("stop_error", [False, True])
def test_result_created_only_after_confirmed_stop(tmp_path, monkeypatch, stop_error):
    source = tmp_path / "source"
    migration = source / "internal/database/webhook_outbox.go"
    migration.parent.mkdir(parents=True)
    migration.write_bytes(b"synthetic migration")
    monkeypatch.setattr(held, "MIGRATION_SHA", held.hashlib.sha256(migration.read_bytes()).hexdigest())
    monkeypatch.setattr(held.time, "sleep", lambda _: None)
    output = tmp_path / "evidence"
    repo = tmp_path / "repo"
    (repo / "ops/miniflux/heldtool").mkdir(parents=True)
    image = {"Id": "fixed", "RepoDigests": [held.PG], "Architecture": "arm64"}
    actual = {"Id": "owned", "Image": "fixed", "State": {"OOMKilled": False},
              "HostConfig": {"Memory": 402653184, "MemorySwap": 402653184, "PidsLimit": 64, "NanoCpus": 1000000000}}

    def command(argv, **kwargs):
        text = json.dumps([image]) if "image" in argv else json.dumps([actual]) if "inspect" in argv else "ok"
        return subprocess.CompletedProcess(argv, 0, text, "")

    def stop(*args):
        assert not (output / "RESULT.json").exists()
        if stop_error:
            raise RuntimeError("owned stop incomplete")
        return {"stop_confirmed": True}

    monkeypatch.setattr(held.subprocess, "run", command)
    monkeypatch.setattr(held, "stop_owned", stop)
    if stop_error:
        with pytest.raises(RuntimeError, match="owned stop incomplete"):
            held.run(source, repo, output)
        assert not (output / "RESULT.json").exists()
        assert json.loads((output / "FAILURE.json").read_text())["stop_confirmed"] is False
        assert (output / "STOP_FAILURE.json").exists()
    else:
        assert held.run(source, repo, output)["status"] == "passed"
        assert json.loads((output / "RESULT.json").read_text())["owned_stop_confirmed"] is True
        assert (output / "STOP_CONFIRMED.json").exists()


def test_dedicated_ci_includes_harness_and_keeps_resource_constraints():
    workflow = (ROOT / ".github/workflows/miniflux-outbox.yml").read_text()
    assert workflow.count("'scripts/verify_miniflux_held_local.py'") == 2
    assert workflow.count("'tests/test_miniflux_held_harness.py'") == 2
    assert "402653184" in workflow and "PidsLimit" in workflow
    assert "docker exec -e MINIFLUX_OUTBOX_TEST_DSN=" in workflow
    assert "-test.v -test.count=1" in workflow
    assert "if: always()" in workflow
    assert "SYNTHETIC_ONLY.sql" in workflow
