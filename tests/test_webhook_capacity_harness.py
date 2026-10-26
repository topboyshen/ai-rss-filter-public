"""Offline guards for the native-image gate; never start Docker from pytest."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_webhook_durable_delivery import delivery, isolated_spool  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def driver():
    return load_script("verify_webhook_capacity_native")


def test_native_gate_accepts_matching_linux_and_daemon(driver):
    value = driver.native_identity({"OSType": "linux", "Architecture": "x86_64"},
                                   system="Linux", machine="x86_64", binfmt=[])
    assert value["native_amd64"] is True


@pytest.mark.parametrize("system,machine,daemon,handlers", [
    ("Darwin", "arm64", "x86_64", []),
    ("Linux", "aarch64", "x86_64", []),
    ("Linux", "x86_64", "aarch64", []),
    ("Linux", "x86_64", "x86_64", ["qemu-x86_64"]),
])
def test_native_gate_rejects_emulation_or_architecture_mismatch(driver, system, machine, daemon, handlers):
    with pytest.raises(ValueError):
        driver.native_identity({"OSType": "linux", "Architecture": daemon},
                               system=system, machine=machine, binfmt=handlers)


def test_non_native_rejected_before_any_docker_command(driver, monkeypatch, tmp_path):
    monkeypatch.setattr(driver.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(driver.platform, "machine", lambda: "arm64")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Docker must not be contacted on emulated host")

    monkeypatch.setattr(driver, "command", forbidden)
    with pytest.raises(ValueError, match="before Docker"):
        driver.verify_environment(["docker"], tmp_path)


def state():
    return {"Image": "sha256:synthetic", "HostConfig": {"Memory": 768 * 1024 * 1024, "MemorySwap": 768 * 1024 * 1024,
                           "PidsLimit": 256, "NetworkMode": "none"},
            "State": {"OOMKilled": False, "Running": False, "ExitCode": 0}}


def test_candidate_success_requires_final_and_fixed_limits(driver):
    assert driver.summarize_case(0, {"status": "passed"}, state(), expected_image="sha256:synthetic")["status"] == "passed"


@pytest.mark.parametrize("kind", ["missing", "failed", "empty", "nonzero", "exitcode", "image", "oom", "memory", "pids", "network", "running", "timeout"])
def test_candidate_unsafe_or_missing_result_cannot_pass(driver, kind):
    record = state()
    final, rc = {"status": "passed"}, 0
    if kind == "missing":
        final = None
    elif kind == "failed":
        final = {"status": "failed"}
    elif kind == "empty":
        final = {}
    elif kind == "nonzero":
        rc = 137
    elif kind == "exitcode":
        record["State"]["ExitCode"] = 1
    elif kind == "image":
        record["Image"] = "sha256:other"
    elif kind == "oom":
        record["State"]["OOMKilled"] = True
    elif kind == "memory":
        record["HostConfig"]["Memory"] *= 2
    elif kind == "pids":
        record["HostConfig"]["PidsLimit"] = 512
    elif kind == "network":
        record["HostConfig"]["NetworkMode"] = "bridge"
    elif kind == "running":
        record["State"]["Running"] = True
    assert driver.summarize_case(rc, final, record, expected_image="sha256:synthetic",
                                 timed_out=kind == "timeout")["status"] == "failed"


def test_evidence_is_exclusive_not_overwritten(driver, tmp_path):
    target = tmp_path / "result.json"
    driver.save(target, {"status": "failed"})
    with pytest.raises(FileExistsError):
        driver.save(target, {"status": "passed"})
    assert json.loads(target.read_text())["status"] == "failed"


def test_probe_cases_and_identity_guards_are_structural():
    probe = load_script("webhook_capacity_probe")
    source = (ROOT / "scripts/webhook_capacity_probe.py").read_text()
    assert set(probe.CASES) == {"allocation", "gradient", "bytes", "maxbody", "partial", "restart", "enospc",
                              "alias", "alias-restart", "alias-sync", "alias-capacity", "alias-intent", "alias-intent-restart",
                              "alias-inventory"}
    assert "socket.socket.connect = forbidden" in source
    assert "webhook.process_entry = forbidden" in source
    assert "image source hash mismatch" in source
    assert "tracemalloc.get_traced_memory()" in source
    assert "module.MAX_BYTES * .99" in source
    assert "partial globally blocked unrelated admission" in source


def test_workflow_has_exact_pr_checkout_native_runner_and_always_artifacts():
    source = (ROOT / ".github/workflows/webhook-capacity.yml").read_text()
    assert "runs-on: ubuntu-24.04" in source
    assert "github.event.pull_request.head.sha || github.sha" in source
    assert "if: always()" in source
    assert "if-no-files-found: error" in source
    assert "services/webhook*.py" in source and "tests/test_webhook*" in source
    assert "continue-on-error" not in source
    assert "setup-qemu" not in source
    assert "webhook-native-capacity/*/alias*/**" in source
    driver_source = (ROOT / "scripts/verify_webhook_capacity_native.py").read_text()
    assert '["docker", "--host", "unix:///var/run/docker.sock"]' in driver_source


def test_comparison_locks_previous_alias_bug_revision(driver):
    assert driver.BASELINE == "0f3f56120f95c2ddcec81f5d38b1287cfce2275c"
    assert "alias" in driver.CASES and "alias-intent-restart" in driver.CASES


def test_synthetic_requests_are_distinct_and_offline_only():
    probe = load_script("webhook_capacity_probe")
    first, second = probe.raw_body(1), probe.raw_body(2)
    assert first != second
    assert json.loads(first)["feed"]["feed_url"].startswith("https://example.invalid/")
    assert len(probe.raw_body(1, 65536)) > 65536


@pytest.fixture
def actual_receiver(request, monkeypatch):
    receiver = request.getfixturevalue("delivery")
    probe = load_script("webhook_capacity_probe")
    monkeypatch.setenv("MINIFLUX_WEBHOOK_SECRET", probe.SECRET)
    monkeypatch.setattr(probe, "counters", lambda: {"scope": "local functional check, not native resource evidence"})
    import services.webhook_receipts as receipts
    return receiver, probe, receipts


def test_actual_store_tiny_read_allocation_control(actual_receiver):
    receiver, probe, receipts = actual_receiver
    result = probe.allocation(receiver.webhook._webhook_spool_dir, receiver.webhook, receipts)
    assert result["returned_bytes"] == 1177
    assert result["tracemalloc_peak_bytes"] < 1024 * 1024
    assert receiver.calls == []


def test_actual_routes_one_MiB_and_actual_store_oversized_read(actual_receiver):
    receiver, probe, receipts = actual_receiver
    result = probe.maxbody(receiver.webhook._webhook_spool_dir, receiver.webhook, receiver.app, receipts)
    assert result["oversized_record_rejected"] is True
    assert [row["result"]["status"] for row in result["measurements"]] == [202, 202, 200]
    assert receiver.calls == []


def test_partial_fixture_has_real_queue_context_and_preserves_bytes(actual_receiver, tmp_path):
    receiver, probe, receipts = actual_receiver
    state_path = tmp_path / "partial"
    state_path.mkdir()
    result = probe.partial(SimpleNamespace(case="partial", revision="a" * 40), state_path,
                           receiver.webhook._webhook_spool_dir, receiver.webhook, receiver.app, receipts)
    assert result["partial_sha256"]
    assert result["restart_process"] is False
    assert receiver.calls == []


@pytest.mark.parametrize("case", ["alias", "alias-intent"])
def test_alias_fault_fixture_isolates_concurrent_requests_and_restart(actual_receiver, tmp_path, case):
    receiver, probe, receipts = actual_receiver
    state_path = tmp_path / case
    state_path.mkdir()
    result = probe.alias_partial(SimpleNamespace(case=case, revision="a" * 40), state_path,
                                 receiver.webhook._webhook_spool_dir, receiver.webhook, receiver.app, receipts)
    assert result["completed_primary_unchanged"] is True
    assert result["partial_intent"] is (case == "alias-intent")
    restarted = tmp_path / (case + "-restart")
    restarted.mkdir()
    again = probe.alias_partial(SimpleNamespace(case=case + "-restart", revision="a" * 40), restarted,
                                receiver.webhook._webhook_spool_dir, receiver.webhook, receiver.app, receipts)
    assert again["partial_sha256"] == result["partial_sha256"]
    assert again["restart_process"] is True
    assert receiver.calls == []


def test_alias_intent_sync_fault_has_no_ack_until_exact_reconfirmation(actual_receiver, tmp_path):
    receiver, probe, receipts = actual_receiver
    state_path = tmp_path / "alias-sync"
    state_path.mkdir()
    result = probe.alias_sync(SimpleNamespace(case="alias-sync", revision="a" * 40), state_path,
                              receiver.webhook._webhook_spool_dir, receiver.webhook, receiver.app, receipts)
    assert result["completed_primary_unchanged"] is True
    assert result["injected_sync_failures"] > 0
    assert receiver.calls == []


def test_alias_inventory_prepopulation_and_concurrent_paths(actual_receiver, tmp_path):
    receiver, probe, receipts = actual_receiver
    state_path = tmp_path / "alias-inventory"
    state_path.mkdir()
    result = probe.alias_inventory(SimpleNamespace(case="alias-inventory", revision="a" * 40), state_path,
                                   receiver.webhook._webhook_spool_dir, receiver.webhook,
                                   receiver.app, receipts, count=16)
    assert result["fixture"]["alias_count"] == 16
    assert result["fixture"]["with_intents"] is True
    assert result["final"]["records"] == 18
    assert result["intent_count"] == 16
    assert result["completed_primary_unchanged"] is True
    assert receiver.calls == []
