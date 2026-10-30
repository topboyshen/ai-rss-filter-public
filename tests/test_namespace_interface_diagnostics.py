"""Rejected namespace proofs report interface names/count only; the pinned guard itself is unchanged."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import production_host_ingress_native as ingress
from scripts import production_namespace_native as namespace_native
from scripts import verify_production_activation_native as native
from tests.test_production_host_ingress_native import FamilyNetworkCommands
from tests.test_production_host_ingress_native import namespace_stat as _namespace_stat

namespace_stat = _namespace_stat

REPO = Path(__file__).resolve().parents[1]
BRIDGE = "afa0123456789"


def _links(veths):
    return ([{"ifname": "lo"}, {"ifname": BRIDGE, "linkinfo": {"info_kind": "bridge"}}]
            + [{"ifname": "veth%04d" % i, "master": BRIDGE, "linkinfo": {"info_kind": "veth"}} for i in range(veths)])


def test_interface_bound_rejection_carries_names_only(tmp_path, namespace_stat):
    with pytest.raises(ingress.ProofError, match="^namespace interface bound$") as caught:
        ingress.namespace_snapshot(FamilyNetworkCommands(_links(9)), bridge=BRIDGE,
                                   evidence_path=tmp_path / "NAMESPACE_CHECK_0001.json")
    value = caught.value.interface_names
    assert value["count"] == 11 and value["names"][:2] == ["lo", BRIDGE] and value["names"][-1] == "veth0008"


def test_within_bound_namespace_still_validates(tmp_path, namespace_stat):
    result = ingress.namespace_snapshot(FamilyNetworkCommands(_links(6)), bridge=BRIDGE)
    assert len(result["links"]) == 8


def test_interface_names_reject_unsafe_text():
    value = ingress.interface_names([{"ifname": "lo"}, {"ifname": "x" * 40}, {"ifname": "a b;rm"}, "bad"])
    assert value == {"count": 4, "names": ["lo", "<invalid>", "<invalid>", "<invalid>"]}


def test_helper_report_and_summary_include_interface_names(tmp_path, monkeypatch):
    error = ingress.ProofError("namespace interface bound")
    error.interface_names = {"count": 11, "names": ["lo", BRIDGE, "veth0000", "$(boom)"]}
    monkeypatch.setattr(namespace_native, "safe_export", lambda options, token: {"complete": True})
    monkeypatch.setattr(namespace_native, "execute_owned", lambda options, token: (_ for _ in ()).throw(error))
    report = namespace_native.run_report({}, "9" * 64)
    assert report["error_type"] == "ProofError" and report["error_message_literal"] == "namespace interface bound"
    assert report["error_interfaces"] == {"count": 11, "names": ["lo", BRIDGE, "veth0000", "<invalid>"]}
    path = tmp_path / "NAMESPACE_PROCESS"
    Path(str(path) + ".stdout").write_text(json.dumps(report))
    summary = native.namespace_helper_summary(path, REPO)
    assert summary == ("status=unconfirmed,error_type=ProofError,error_message=namespace interface bound,"
                       "interfaces=11:lo/" + BRIDGE + "/veth0000/<invalid>")
