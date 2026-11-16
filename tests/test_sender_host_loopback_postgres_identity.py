"""Static host-loopback policy is narrow; live namespace/PG proof stays separate."""
from copy import deepcopy
import hashlib

import pytest

from test_production_sender_driver import setup  # noqa: F401
from services import sender_drain_contract as contract
import handoff_runtime_adapter as runtime
import miniflux_handoff_adapter as bridge
from durable_handoff_cutover import CutoverError


URL = "postgres://synthetic:synthetic@127.0.0.1:5432/synthetic?sslmode=disable"


@pytest.fixture
def loopback(request):
    driver, transport, _reader = request.getfixturevalue("setup")
    binding = deepcopy(driver.plan["binding"])
    row = deepcopy(transport.rows[0])
    row["Mounts"] = []
    row["HostConfig"].update(ReadonlyRootfs=True, NetworkMode="host", PortBindings={})
    row["NetworkSettings"] = {"Networks": {"host": {"NetworkID": "a" * 64}}}
    row["Config"]["Env"].append("DATABASE_URL=" + URL)
    binding["component"]["mounts"] = []
    binding["component"]["host_loopback_postgres"] = contract.host_loopback_postgres_identity(URL, True, "host")
    return binding, row, transport


def inspect(binding, row, **kwargs):
    return runtime.inspect_binding([row], {binding["component_name"]: binding["component"]}, **kwargs)


def test_explicit_readonly_host_loopback_policy_only_in_production_sender_scope(loopback):
    binding, row, _transport = loopback
    assert contract.validate_binding(binding) == binding
    assert bridge.checked_binding(binding, scope="instrumented_sender_only") == binding
    assert inspect(binding, row, allow_host_loopback_postgres=True)
    with pytest.raises(CutoverError):
        inspect(binding, row)
    with pytest.raises(CutoverError):
        inspect(binding, row, allow_external_postgres=True)
    isolation = {**binding, "scope": "isolation"}
    with pytest.raises(CutoverError):
        bridge.checked_binding(isolation)
    with pytest.raises(contract.DrainContractError):
        contract.validate_binding(isolation)


def test_every_control_exchange_reconstructs_actual_host_loopback_identity(loopback, tmp_path):
    binding, row, transport = loopback
    transport.binding = binding
    transport.rows = [row]
    calls = []

    def fresh():
        calls.append("actual inspect")
        return transport.inspect(None)

    control = bridge.MinifluxDrainControl(binding, transport, fresh, tmp_path / "control")
    assert control.registration()["binding_sha256"] == bridge.sha(binding)
    assert len(calls) == 2
    transport.rows[0]["HostConfig"]["NetworkMode"] = "bridge"
    with pytest.raises(contract.DrainContractError):
        control.registration()
    assert len(calls) == 3
    assert len(transport.calls) == 1  # Failed identity does not send another HTTP request.


@pytest.mark.parametrize("change", [
    "missing_mounts", "missing_policy", "app_role", "wrong_control", "rw_rootfs", "dsn_drift", "added_mount",
    "duplicate_env", "dsn_file", "bridge", "container_network", "missing_networks", "extra_network",
    "bridge_inventory", "port_binding", "created", "paused", "oom", "restart", "memory", "pids",
])
def test_runtime_never_adopts_expected_identity_or_changed_actual_state(loopback, change):
    binding, row, _transport = loopback
    if change == "missing_mounts":
        del row["Mounts"]
    elif change == "missing_policy":
        del binding["component"]["host_loopback_postgres"]
    elif change == "app_role":
        binding["component"]["role"] = "worker"
        row["Config"]["Labels"]["io.ai-filter.handoff.role"] = "worker"
    elif change == "wrong_control":
        binding["control_id"] = "other"
    elif change == "rw_rootfs":
        row["HostConfig"]["ReadonlyRootfs"] = False
    elif change == "dsn_drift":
        row["Config"]["Env"][-1] = "DATABASE_URL=" + URL.replace("/synthetic?", "/different?")
    elif change == "added_mount":
        row["Mounts"] = [{"Type": "bind", "Source": "/private/real", "Destination": "/data", "RW": False}]
    elif change == "duplicate_env":
        row["Config"]["Env"].append(row["Config"]["Env"][-1])
    elif change == "dsn_file":
        row["Config"]["Env"].append("DATABASE_URL_FILE=/private/override")
    elif change == "bridge":
        row["HostConfig"]["NetworkMode"] = "bridge"
    elif change == "container_network":
        row["HostConfig"]["NetworkMode"] = "container:" + "a" * 64
    elif change == "missing_networks":
        del row["NetworkSettings"]
    elif change == "extra_network":
        row["NetworkSettings"]["Networks"]["bridge"] = {}
    elif change == "bridge_inventory":
        row["NetworkSettings"]["Networks"] = {"bridge": {}}
    elif change == "port_binding":
        row["HostConfig"]["PortBindings"] = {"8888/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8888"}]}
    elif change == "created":
        row["State"]["Running"] = False
    elif change == "paused":
        row["State"]["Paused"] = True
    elif change == "oom":
        row["State"]["OOMKilled"] = True
    elif change == "restart":
        row["RestartCount"] += 1
    elif change == "memory":
        row["HostConfig"]["Memory"] = 768 * 1024 * 1024 + 1
    elif change == "pids":
        row["HostConfig"]["PidsLimit"] = 257
    with pytest.raises((CutoverError, contract.DrainContractError, KeyError)):
        bridge.checked_binding(binding, scope="instrumented_sender_only")
        inspect(binding, row, allow_host_loopback_postgres=True)


def test_policies_are_mutually_exclusive_and_flags_do_not_authorize_each_other(loopback):
    binding, row, _transport = loopback
    external = contract.external_postgres_identity("postgres://synthetic@192.0.2.44/synthetic", True)
    binding["component"]["external_postgres"] = external
    for check in (lambda: contract.validate_binding(binding),
                  lambda: bridge.checked_binding(binding, scope="instrumented_sender_only"),
                  lambda: inspect(binding, row, allow_external_postgres=True, allow_host_loopback_postgres=True)):
        with pytest.raises((CutoverError, contract.DrainContractError)):
            check()
    del binding["component"]["host_loopback_postgres"]
    with pytest.raises(CutoverError):
        runtime._component(binding["component_name"], binding["component"], allow_host_loopback_postgres=True)


@pytest.mark.parametrize("dsn", [
    URL, URL.replace("postgres://", "postgresql://"), URL.split("?")[0],
    URL.replace(":5432", ""), URL.replace("postgres://", "postgresql://").replace(":5432", ""),
    *[URL.replace("sslmode=disable", "sslmode=" + mode)
      for mode in ("allow", "prefer", "require", "verify-ca", "verify-full")],
])
def test_canonical_loopback_dsn_preserves_exact_bytes_and_effective_port(dsn):
    actual = contract.host_loopback_postgres_identity(dsn, True, "host")
    assert set(actual) == {"dsn_sha256", "host", "port", "read_only_rootfs", "network_mode"}
    assert actual["host"] == "127.0.0.1" and actual["port"] == 5432
    assert actual["dsn_sha256"] == hashlib.sha256(dsn.encode()).hexdigest()


def test_default_port_is_not_a_dsn_hash_alias(loopback):
    binding, row, _transport = loopback
    omitted = URL.replace(":5432", "")
    actual = contract.host_loopback_postgres_identity(omitted, True, "host")
    assert actual["dsn_sha256"] != binding["component"]["host_loopback_postgres"]["dsn_sha256"]
    row["Config"]["Env"][-1] = "DATABASE_URL=" + omitted
    with pytest.raises(CutoverError, match="identity changed"):
        inspect(binding, row, allow_host_loopback_postgres=True)
    binding["component"]["host_loopback_postgres"] = actual
    assert inspect(binding, row, allow_host_loopback_postgres=True)


@pytest.mark.parametrize("override", [
    "PGHOSTADDR=192.0.2.44", "PGPORT=5433", "PGSERVICE=synthetic", "PGSERVICEFILE=/private/override",
    "PGHOST=localhost", "PGDATABASE=other", "PGUSER=other", "PGPASSWORD=synthetic", "PGOPTIONS=",
    "PGAPPNAME=synthetic", "PG=", "PG_UNKNOWN=synthetic",
])
@pytest.mark.parametrize("omitted_port", [False, True])
def test_actual_sender_environment_cannot_override_loopback_identity(loopback, override, omitted_port):
    binding, row, _transport = loopback
    dsn = URL.replace(":5432", "") if omitted_port else URL
    row["Config"]["Env"][-1] = "DATABASE_URL=" + dsn
    binding["component"]["host_loopback_postgres"] = contract.host_loopback_postgres_identity(dsn, True, "host")
    row["Config"]["Env"].append(override)
    with pytest.raises(CutoverError, match="policy differs"):
        inspect(binding, row, allow_host_loopback_postgres=True)


@pytest.mark.parametrize("dsn", [
    "postgres://localhost:5432/db", "postgres://127.0.0.2:5432/db", "postgres://[::1]:5432/db",
    "postgres://[::ffff:127.0.0.1]:5432/db", "postgres://2130706433:5432/db", "postgres://0177.0.0.1:5432/db",
    "postgres://127.0.0.1:/db", "postgres://127.0.0.1:5433/db", "postgres://127.0.0.1:05432/db",
    "postgres://127.0.0.1/db?port=5433", "postgres://127.0.0.1/db?host=192.0.2.44",
    "postgres://127.0.0.1/db?hostaddr=192.0.2.44", "postgres://127.0.0.1/db?service=override",
    "postgres://%31%32%37.0.0.1:5432/db", "postgres://pg:5432/db", "postgres://127.0.0.1:5432/",
    "postgres://127.0.0.1:5432/db#fragment", "postgres://127.0.0.1:5432/db?host=192.0.2.44",
    "postgres://127.0.0.1:5432/db?hostaddr=192.0.2.44", "postgres://127.0.0.1:5432/db?service=override",
    "postgres://127.0.0.1:5432/db?options=-csearch_path=other", "postgres://127.0.0.1:5432/db?sslmode=disable&sslmode=require",
    "postgres://127.0.0.1:5432/db?sslmode=", "postgres://127.0.0.1:5432/db?sslmode", "postgres://127.0.0.1:65536/db",
    "host=127.0.0.1 port=5432 dbname=synthetic", "postgres://127.0.0.1:5432/" + "x" * 8192,
    " postgres://127.0.0.1:5432/db", "postgres://127.0.0.1:5432/db\n", "postgres://127.0.0.\t1:5432/db",
    "postgres://user:raw password@127.0.0.1:5432/db", "postgres://user:\x7f@127.0.0.1:5432/db",
])
def test_local_variant_rejects_aliases_options_and_noncanonical_identity(dsn):
    with pytest.raises((contract.DrainContractError, ValueError)):
        contract.host_loopback_postgres_identity(dsn, True, "host")


@pytest.mark.parametrize("read_only,network", [(False, "host"), (1, "host"), (True, "bridge"), (True, None), (True, "container:a")])
def test_actual_rootfs_and_network_are_not_caller_host_flags(read_only, network):
    with pytest.raises(contract.DrainContractError):
        contract.host_loopback_postgres_identity(URL, read_only, network)


@pytest.mark.parametrize("change", ["host_true", "unknown", "bool_port", "port", "readonly", "network", "missing", "hash"])
def test_static_variant_is_exact_and_does_not_substitute_for_live_proof(change):
    value = contract.host_loopback_postgres_identity(URL, True, "host")
    if change == "host_true":
        value["host"] = True
    elif change == "unknown":
        value["host_proven"] = True
    elif change == "bool_port":
        value["port"] = True
    elif change == "port":
        value["port"] = 5433
    elif change == "readonly":
        value["read_only_rootfs"] = False
    elif change == "network":
        value["network_mode"] = "bridge"
    elif change == "missing":
        del value["network_mode"]
    else:
        value["dsn_sha256"] = "unknown"
    with pytest.raises(contract.DrainContractError):
        contract.validate_host_loopback_postgres(value)
