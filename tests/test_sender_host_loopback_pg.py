"""Exact local-transport guards; doubles do not count as native proof."""

from copy import deepcopy
import hashlib
from types import SimpleNamespace
import time

import pytest

from scripts import sender_host_loopback_pg as local
from scripts.durable_handoff_cutover import CutoverError, encoded
from services.sender_drain_contract import DrainContractError

DSN = "postgres://miniflux:synthetic@127.0.0.1:5432/miniflux?sslmode=disable"
HOST = {
    "boot_id": "12345678-1234-1234-1234-123456789abc",
    "network_device": 4,
    "network_inode": 8,
}
IDENTITY = {
    "database": "miniflux",
    "schema": "public",
    "system_identifier": "123456",
    "postmaster_started_at": "2026-10-31 00:00:00+00",
    "current_user": "miniflux",
    "session_user": "miniflux",
}


def row():
    ports = {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5432"}]}
    return {
        "Id": "a" * 64,
        "Image": "sha256:" + "b" * 64,
        "RestartCount": 0,
        "Config": {"Env": ["PRIVATE_SYNTHETIC_VALUE=must-not-appear"]},
        "Mounts": [{"Type": "volume", "Name": "test"}],
        "State": {
            "Running": True,
            "Pid": 321,
            "StartedAt": "2026-10-31T00:00:00Z",
            "Health": {"Status": "healthy"},
        },
        "HostConfig": {"PortBindings": deepcopy(ports), "NetworkMode": "test_bridge"},
        "NetworkSettings": {
            "Ports": deepcopy(ports),
            "Networks": {
                "test_bridge": {"NetworkID": "c" * 64, "IPAddress": "172.19.0.2"}
            },
        },
    }


@pytest.fixture
def case(monkeypatch):
    actual = row()
    process = {"pid": 321, "start_ticks": 22, "uid": 999}
    monkeypatch.setattr(local, "_host", lambda: deepcopy(HOST))
    monkeypatch.setattr(local, "_process", lambda pid: deepcopy(process))
    calls = []

    def inspect(identifier, deadline):
        calls.append((identifier, deadline))
        return deepcopy(actual)

    monkeypatch.setattr(local, "_inspect", inspect)
    binding = local.observe(DSN, actual["Id"])
    return SimpleNamespace(actual=actual, binding=binding, calls=calls, process=process)


def test_exact_metadata_observation_has_no_original_or_password(case):
    assert len(case.calls) == 2
    assert local.validate(case.binding) == case.binding
    assert case.binding["dsn_sha256"] == hashlib.sha256(DSN.encode()).hexdigest()
    raw = encoded(case.binding)
    assert b"synthetic" not in raw and b"PRIVATE_SYNTHETIC_VALUE" not in raw
    assert case.binding["postgres"]["uid"] == 999  # Real PG need not be a root process.


def test_check_uses_actual_postgres_lifecycle_and_same_deadline(case):
    deadline = time.monotonic() + 1
    local.HostLoopbackPostgres(case.binding).check(DSN, deadline=deadline)
    assert case.calls[-1] == (case.actual["Id"], deadline)


@pytest.mark.parametrize(
    "kind",
    [
        "id",
        "image",
        "restart",
        "start",
        "pid",
        "config",
        "mounts",
        "network",
        "port",
        "unhealthy",
        "paused",
    ],
)
def test_lifecycle_or_topology_drift_blocks(case, kind):
    a = case.actual
    if kind == "id":
        a["Id"] = "e" * 64
    elif kind == "image":
        a["Image"] = "sha256:" + "e" * 64
    elif kind == "restart":
        a["RestartCount"] += 1
    elif kind == "start":
        a["State"]["StartedAt"] = "changed"
    elif kind == "pid":
        a["State"]["Pid"] = 322
        case.process["pid"] = 322
    elif kind == "config":
        a["Config"]["Env"].append("EXTRA=1")
    elif kind == "mounts":
        a["Mounts"].append({"Name": "new"})
    elif kind == "network":
        a["NetworkSettings"]["Networks"]["test_bridge"]["NetworkID"] = "e" * 64
    elif kind == "port":
        a["HostConfig"]["PortBindings"]["5432/tcp"][0]["HostIp"] = "0.0.0.0"
    elif kind == "unhealthy":
        a["State"]["Health"]["Status"] = "unhealthy"
    elif kind == "paused":
        a["State"]["Paused"] = True
    with pytest.raises(CutoverError):
        local.HostLoopbackPostgres(case.binding).check(
            DSN, deadline=time.monotonic() + 1
        )


@pytest.mark.parametrize("change", ["boot", "namespace", "dsn", "mutation", "deadline"])
def test_host_credentials_or_binding_drift_blocks(case, monkeypatch, change):
    guard = local.HostLoopbackPostgres(case.binding)
    dsn, deadline = DSN, time.monotonic() + 1
    if change == "boot":
        monkeypatch.setattr(local, "_host", lambda: {**HOST, "boot_id": "changed"})
    elif change == "namespace":
        monkeypatch.setattr(local, "_host", lambda: {**HOST, "network_inode": 9})
    elif change == "dsn":
        dsn = DSN.replace("synthetic", "different")
    elif change == "mutation":
        guard.binding["postgres"]["restart_count"] += 1
    elif change == "deadline":
        deadline = time.monotonic() - 1
    with pytest.raises(CutoverError):
        guard.check(dsn, deadline=deadline)


@pytest.mark.parametrize(
    "host", ["localhost", "127.0.0.2", "0.0.0.0", "172.19.0.2", "127.1"]
)
def test_no_alias_or_bridge_address_can_be_adopted(case, host):
    with pytest.raises(DrainContractError):
        local.HostLoopbackPostgres(case.binding).check(
            DSN.replace("127.0.0.1", host), deadline=time.monotonic() + 1
        )


def test_actual_connection_checks_same_cursor_without_connecting_again(case):
    calls = []
    expected = tuple(IDENTITY.values())
    cursor = SimpleNamespace(
        execute=lambda sql: calls.append(sql),
        fetchone=lambda: ("172.19.0.2", 5432, *expected),
    )
    local.HostLoopbackPostgres(case.binding).check_connection(cursor, IDENTITY)
    assert calls == [local.SQL]


@pytest.mark.parametrize("field", range(8))
def test_actual_connection_wrong_server_or_identity_stops(case, field):
    result = ["172.19.0.2", 5432, *IDENTITY.values()]
    result[field] = "changed"
    cursor = SimpleNamespace(execute=lambda sql: None, fetchone=lambda: tuple(result))
    with pytest.raises(CutoverError):
        local.HostLoopbackPostgres(case.binding).check_connection(cursor, IDENTITY)


@pytest.mark.parametrize(
    "field,value",
    [
        ("pid", True),
        ("restart_count", True),
        ("uid", -1),
        ("network_id", "unknown"),
        ("address", "127.0.0.1"),
        ("address", "0.0.0.0"),
    ],
)
def test_capability_rejects_bad_types_and_addresses(case, field, value):
    case.binding["postgres"][field] = value
    with pytest.raises((CutoverError, ValueError)):
        local.validate(case.binding)


def test_unknown_capability_fields_rejected(case):
    case.binding["assume_host"] = True
    with pytest.raises(CutoverError):
        local.validate(case.binding)


def test_observation_drift_has_no_second_attempt(case, monkeypatch):
    count = 0

    def inspect(identifier, deadline):
        nonlocal count
        count += 1
        value = deepcopy(case.actual)
        value["RestartCount"] = count
        return value

    monkeypatch.setattr(local, "_inspect", inspect)
    with pytest.raises(CutoverError):
        local.observe(DSN, case.actual["Id"])
    assert count == 2


@pytest.mark.parametrize(
    "key",
    [
        "PGHOST",
        "PGHOSTADDR",
        "PGPORT",
        "PGSERVICE",
        "PGSERVICEFILE",
        "PGOPTIONS",
        "PGPASSWORD",
    ],
)
def test_process_environment_cannot_override_sealed_literal_endpoint(
    case, monkeypatch, key
):
    monkeypatch.setenv(key, "synthetic-no-secret")
    calls = []
    monkeypatch.setattr(local, "_inspect", lambda *args: calls.append(args))
    with pytest.raises(CutoverError, match="environment overrides"):
        local.observe(DSN, case.actual["Id"])
    with pytest.raises(CutoverError, match="environment overrides"):
        local.HostLoopbackPostgres(case.binding).check(
            DSN, deadline=time.monotonic() + 1
        )
    assert calls == []
