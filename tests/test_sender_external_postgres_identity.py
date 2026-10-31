"""Only explicitly bound read-only Miniflux may have an empty mount inventory."""
from copy import deepcopy

import pytest

from test_production_sender_driver import setup  # noqa: F401
from services import sender_drain_contract as contract
import handoff_runtime_adapter as runtime
import miniflux_handoff_adapter as bridge
from durable_handoff_cutover import CutoverError


@pytest.fixture
def empty(request):
    driver, transport, _reader = request.getfixturevalue("setup")
    binding = deepcopy(driver.plan["binding"])
    row = deepcopy(transport.rows[0])
    url = "postgres://synthetic:synthetic@192.0.2.44:5432/synthetic?sslmode=disable"
    row["Mounts"] = []
    row["HostConfig"]["ReadonlyRootfs"] = True
    row["Config"]["Env"].append("DATABASE_URL=" + url)
    binding["component"]["mounts"] = []
    binding["component"]["external_postgres"] = contract.external_postgres_identity(url, True)
    return binding, row


def test_zero_mounts_need_explicit_external_pg_readonly_policy_and_production_scope(empty):
    binding, row = empty
    assert contract.validate_binding(binding) == binding
    assert bridge.checked_binding(binding, scope="instrumented_sender_only") == binding
    assert runtime.inspect_binding([row], {binding["component_name"]: binding["component"]}, allow_external_postgres=True)
    with pytest.raises(CutoverError):
        runtime.inspect_binding([row], {binding["component_name"]: binding["component"]})
    isolation = {**binding, "scope":"isolation"}
    with pytest.raises(CutoverError):
        bridge.checked_binding(isolation)


@pytest.mark.parametrize("change",["missing_mounts","missing_policy","app_role","wrong_control","rw_rootfs","dsn_drift","added_mount","duplicate_env","dsn_file"])
def test_actual_inspect_cannot_hide_missing_or_changed_external_pg_identity(empty,change):
    binding,row=empty
    if change=="missing_mounts":
        del row["Mounts"]
    elif change=="missing_policy":
        del binding["component"]["external_postgres"]
    elif change=="app_role":
        binding["component"]["role"]="web"
        row["Config"]["Labels"]["io.ai-filter.handoff.role"]="web"
    elif change=="wrong_control":
        binding["control_id"]="unrelated"
    elif change=="rw_rootfs":
        row["HostConfig"]["ReadonlyRootfs"]=False
    elif change=="dsn_drift":
        row["Config"]["Env"][-1]="DATABASE_URL=postgres://synthetic@192.0.2.45/other"
    elif change=="added_mount":
        row["Mounts"]=[{"Type":"bind","Source":"/private/real","Destination":"/data","RW":False}]
    elif change=="dsn_file":
        row["Config"]["Env"].append("DATABASE_URL_FILE=/private/override")
    else:
        row["Config"]["Env"].append(row["Config"]["Env"][-1])
    with pytest.raises((CutoverError,contract.DrainContractError,KeyError)):
        bridge.checked_binding(binding,scope="instrumented_sender_only")
        runtime.inspect_binding([row],{binding["component_name"]:binding["component"]},allow_external_postgres=True)


@pytest.mark.parametrize("dsn",["postgres://localhost/db","postgres://127.0.0.1/db","postgres://[::1]/db",
                                "postgres://[::ffff:127.0.0.1]/db","postgres://2130706433/db","postgres://0177.0.0.1/db",
                                "postgres://pg/db?host=127.0.0.1","postgres://pg/db?sslmode=disable&sslmode=require",
                                "postgres://pg/db?options=-csearch_path=other","host=postgres dbname=example"])
def test_external_mount_exception_never_accepts_loopback_or_libpq_host_override(dsn):
    with pytest.raises((contract.DrainContractError,ValueError)):
        contract.external_postgres_identity(dsn,True)
