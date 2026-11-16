"""Plan binding and repeated live guard invocation; no Docker or PostgreSQL I/O."""
from copy import deepcopy

import pytest

from test_sender_host_loopback_postgres_identity import loopback, setup  # noqa: F401
from durable_handoff_cutover import CutoverError
from scripts.durable_handoff_cutover import CutoverError as CapabilityError
import production_sender_driver as sender


def capability(dsn_sha256):
    return {
        "schema": "host-loopback-postgres-v1", "dsn_sha256": dsn_sha256,
        "host": {"boot_id": "00000000-0000-0000-0000-000000000001", "network_device": 4, "network_inode": 2},
        "postgres": {
            "id": "a" * 64, "image_id": "sha256:" + "b" * 64, "started_at": "2026-10-31T00:00:00Z",
            "restart_count": 0, "pid": 123, "start_ticks": 456, "uid": 999, "config_sha256": "c" * 64,
            "mounts_sha256": "d" * 64, "network_name": "sender_pg", "network_id": "e" * 64, "address": "172.19.0.2",
        },
    }


@pytest.fixture
def plan(request):
    binding, _row, _transport = request.getfixturevalue("loopback")
    existing, _unused, _reader = request.getfixturevalue("setup")
    value = deepcopy(existing.plan)
    value["binding"] = binding
    value["postgres_transport"] = capability(binding["component"]["host_loopback_postgres"]["dsn_sha256"])
    return value


def test_loopback_plan_binds_independent_transport_and_its_source(plan):
    assert sender.validate_plan(plan) == plan
    assert "sender_host_loopback_pg.py" in sender.sources()


@pytest.mark.parametrize("change", ["missing_transport", "missing_policy", "dsn", "host_flag", "old_sources", "scope", "role", "control"])
def test_transport_cannot_be_inferred_or_used_outside_narrow_sender_scope(plan, change):
    if change == "missing_transport":
        del plan["postgres_transport"]
    elif change == "missing_policy":
        del plan["binding"]["component"]["host_loopback_postgres"]
    elif change == "dsn":
        plan["postgres_transport"]["dsn_sha256"] = "f" * 64
    elif change == "host_flag":
        plan["postgres_transport"]["host"] = True
    elif change == "old_sources":
        del plan["sources"]["sender_host_loopback_pg.py"]
    elif change == "scope":
        plan["scope"] = "isolation"
    elif change == "role":
        plan["binding"]["component"]["role"] = "worker"
    else:
        plan["binding"]["control_id"] = "other"
    with pytest.raises((CutoverError, CapabilityError)):
        sender.validate_plan(plan)


def test_non_loopback_plan_cannot_smuggle_transport(request):
    existing, _transport, _reader = request.getfixturevalue("setup")
    value = deepcopy(existing.plan)
    value["postgres_transport"] = capability("f" * 64)
    with pytest.raises(CutoverError):
        sender.validate_plan(value)


@pytest.mark.parametrize("reader_change", ["missing_binding", "different_binding", "missing_guard", "callback", "different_guard"])
def test_loopback_reader_requires_actual_guard_and_exact_binding(plan, request, tmp_path, reader_change):
    _existing, transport, reader = request.getfixturevalue("setup")
    reader.transport_binding = deepcopy(plan["postgres_transport"])
    reader.transport_guard = sender.sender_host_loopback_pg.HostLoopbackPostgres(reader.transport_binding)
    if reader_change == "missing_binding":
        del reader.transport_binding
    elif reader_change == "different_binding":
        reader.transport_binding["postgres"]["restart_count"] = 1
    elif reader_change == "missing_guard":
        del reader.transport_guard
    elif reader_change == "callback":
        reader.transport_guard = lambda: True
    else:
        reader.transport_guard.binding["postgres"]["restart_count"] = 1
    with pytest.raises(CutoverError):
        sender.SenderDriver(plan, transport, reader, tmp_path / reader_change)


def test_drain_and_activation_recheck_actual_sender_every_time(plan, request, tmp_path, monkeypatch):
    _binding, row, transport = request.getfixturevalue("loopback")
    _existing, _old_transport, reader = request.getfixturevalue("setup")
    transport.binding, transport.rows = plan["binding"], [row]
    reader.transport = transport
    reader.transport_binding = deepcopy(plan["postgres_transport"])
    reader.transport_guard = sender.sender_host_loopback_pg.HostLoopbackPostgres(reader.transport_binding)
    observed = []

    def check_actual(_self, actual):
        observed.append(deepcopy(actual))
        assert actual["Id"] == row["Id"]

    monkeypatch.setattr(sender.sender_host_loopback_pg.HostLoopbackPostgres, "check_sender", check_actual)
    driver = sender.SenderDriver(plan, transport, reader, tmp_path / "drain")
    driver.bind()
    driver.bind()
    assert len(observed) == 6  # bind + registration before/after, each time.
    activation = object.__new__(sender.SenderActivationDriver)
    activation.__dict__.update(driver.__dict__)
    reader.initial_snapshot = reader.snapshot
    activation.bind()
    assert len(observed) == 9
    assert all(value["HostConfig"]["NetworkMode"] == "host" for value in observed)


def test_failed_live_guard_blocks_private_control_and_inventory(plan, request, tmp_path, monkeypatch):
    _binding, row, transport = request.getfixturevalue("loopback")
    _existing, _old_transport, reader = request.getfixturevalue("setup")
    transport.binding, transport.rows = plan["binding"], [row]
    reader.transport_binding = deepcopy(plan["postgres_transport"])
    reader.transport_guard = sender.sender_host_loopback_pg.HostLoopbackPostgres(reader.transport_binding)

    def reject_actual(_self, _actual):
        raise CapabilityError("synthetic namespace changed")

    def no_snapshot():
        raise AssertionError("inventory must not execute after failed live identity")

    monkeypatch.setattr(sender.sender_host_loopback_pg.HostLoopbackPostgres, "check_sender", reject_actual)
    reader.snapshot = no_snapshot
    driver = sender.SenderDriver(plan, transport, reader, tmp_path / "failed")
    with pytest.raises(CapabilityError, match="namespace changed"):
        driver.bind()
    assert transport.calls == []
