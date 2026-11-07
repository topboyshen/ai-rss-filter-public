"""Inspect enumeration order is not mount identity; all actual drift is refused."""
from copy import deepcopy

import pytest

from scripts import production_gateway_native as native
from services import admission_fence as gate


@pytest.fixture
def row():
    return {"Id": "a" * 64, "Name": "/owned", "Image": "sha256:" + "b" * 64,
            "Config": {"Labels": {"owner": "synthetic"}, "Cmd": ["sleep", "60"]},
            "HostConfig": {"Memory": 67108864, "PidsLimit": 32}, "RestartCount": 0,
            "Mounts": [{"Type": "bind", "Source": "/synthetic/one", "Destination": "/one", "RW": False,
                        "Mode": "", "Propagation": "rprivate"},
                       {"Type": "bind", "Source": "/synthetic/two", "Destination": "/two", "RW": False,
                        "Mode": "", "Propagation": "rprivate"}],
            "State": {"StartedAt": "2026-11-06T01:00:00Z", "FinishedAt": "2026-11-06T01:00:10Z",
                      "Running": False, "Status": "exited", "ExitCode": 0,
                      "Paused": False, "Dead": False, "Restarting": False, "OOMKilled": False}}


def test_reordered_mounts_remain_identical_and_normal_term_exit_is_verified(row):
    original = deepcopy(row)
    identity = native._cleanup_identity(row)
    row["Mounts"].reverse()
    assert native._cleanup_identity(row) == identity
    assert native._normal_cleanup_exit(row, identity, purpose="owned") == row["State"]
    assert identity[0]["Mounts"] == original["Mounts"]
    assert row["Mounts"] == list(reversed(original["Mounts"]))  # No inspect mutation.


@pytest.mark.parametrize("field", ["Type", "Source", "Destination", "RW", "Mode", "Propagation", "new_field"])
def test_every_mount_field_including_new_fields_remains_identity_bound(row, field):
    identity = native._cleanup_identity(row)
    row["Mounts"][0][field] = True if field == "RW" else "changed"
    with pytest.raises(gate.AdmissionError, match="identity changed"):
        native._normal_cleanup_exit(row, identity, purpose="owned")


@pytest.mark.parametrize("change", ["duplicate", "remove", "replace"])
def test_mount_multiset_preserves_cardinality_and_duplicate_records(row, change):
    identity = native._cleanup_identity(row)
    if change == "duplicate":
        row["Mounts"].append(deepcopy(row["Mounts"][0]))
    elif change == "remove":
        row["Mounts"].pop()
    else:
        row["Mounts"][1] = deepcopy(row["Mounts"][0])
    with pytest.raises(gate.AdmissionError, match="identity changed"):
        native._normal_cleanup_exit(row, identity, purpose="owned")


@pytest.mark.parametrize("field", ["Id", "Name", "Image", "Config", "HostConfig", "RestartCount", "StartedAt"])
def test_non_mount_identity_guards_are_preserved(row, field):
    identity = native._cleanup_identity(row)
    if field == "StartedAt":
        row["State"][field] = "2026-11-06T01:00:01Z"
    elif field in {"Config", "HostConfig"}:
        row[field]["unexpected"] = True
    else:
        row[field] = 1 if field == "RestartCount" else "changed"
    with pytest.raises(gate.AdmissionError, match="identity changed"):
        native._normal_cleanup_exit(row, identity, purpose="owned")


@pytest.mark.parametrize("field,value", [("Running", True), ("ExitCode", 137), ("OOMKilled", True),
                                        ("Restarting", True), ("Dead", True), ("FinishedAt", "")])
def test_order_invariance_never_substitutes_for_normal_exit(row, field, value):
    identity = native._cleanup_identity(row)
    row["Mounts"].reverse()
    row["State"][field] = value
    with pytest.raises(gate.AdmissionError, match="normal owned process exit unconfirmed"):
        native._normal_cleanup_exit(row, identity, purpose="owned")


@pytest.mark.parametrize("mounts", [None, {}, "mount", [None]])
def test_malformed_mount_records_fail_closed(row, mounts):
    row["Mounts"] = mounts
    with pytest.raises(gate.AdmissionError, match="mount records malformed"):
        native._cleanup_identity(row)
