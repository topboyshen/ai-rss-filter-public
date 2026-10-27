"""Native harness guards, not substitutes for the hosted Go/Flask/PG run."""
import json
from types import SimpleNamespace

import pytest

from scripts import verify_sender_receiver_joint_native as joint
import verify_miniflux_pause_native as base


def test_plan_only_cannot_invoke_container_or_phase_execution(monkeypatch, capsys):
    monkeypatch.setattr(joint, "run", lambda *_a: pytest.fail("plan invoked native execution"))
    monkeypatch.setattr(joint.sys, "argv", ["joint", "--repo", "/absent", "--source", "/absent",
        "--binary", "/absent", "--output", "/absent", "--sender-image", "unresolved", "--receiver-image", "unresolved"])
    assert joint.main() == 0
    assert json.loads(capsys.readouterr().out) == {"status":"plan_only","production_access":False,"real_models":0}


def test_native_platform_guard_runs_before_storage_or_docker(monkeypatch, tmp_path):
    monkeypatch.setattr(base.platform, "system", lambda: "Darwin")
    path = tmp_path / "not-created"
    with pytest.raises(ValueError, match="native Linux AMD64"):
        joint.Joint(tmp_path, tmp_path, "sha256:" + "a" * 64, tmp_path / "binary", path)
    assert not path.exists()


@pytest.mark.parametrize("changed", [{"Memory":768*1024**2+1}, {"PidsLimit":257},
                                      {"PortBindings":{"5000/tcp":[{"HostPort":"5000"}]}}])
def test_actual_sender_resource_or_network_broadening_is_rejected(changed):
    row = {"Image":"sha256:" + "a"*64,"Config":{"Labels":{base.LABEL:"owned"}},"State":{"OOMKilled":False},
           "HostConfig":{"NetworkMode":"owned-internal","PortBindings":{},"Memory":768*1024**2,
                         "MemorySwap":768*1024**2,"PidsLimit":256,"NanoCpus":1_000_000_000,
                         "Privileged":False,"SecurityOpt":["no-new-privileges"]}}
    assert all(base.resource_checks(row, row["Image"], "owned", "owned-internal").values())
    row["HostConfig"].update(changed)
    with pytest.raises(ValueError, match="resource binding failed"):
        base.resource_checks(row, row["Image"], "owned", "owned-internal")


def test_missing_joint_stage_proof_cannot_report_green(monkeypatch, tmp_path):
    output = tmp_path / "evidence"
    def fake_harness(*_args):
        output.mkdir()
        return SimpleNamespace(prepare=lambda:None, prepare_receiver_files=lambda _image:None,
            scenario=lambda:None, redacted=lambda value:value, names={}, preserve=lambda:[],
            started=joint.time.monotonic(), network="synthetic-guard-only")
    monkeypatch.setattr(joint, "source_binding", lambda *_args:{"revision":"a"*40})
    monkeypatch.setattr(joint, "sha", lambda _path:"b"*64)
    monkeypatch.setattr(joint, "Joint", fake_harness)
    result = joint.run(tmp_path,tmp_path,"sender","receiver",tmp_path / "binary",output)
    assert result["status"] == "failed_or_evidence_insufficient"
    assert "stage evidence absent" in result["error"]["message"]
    assert (output / "FAILURE.json").exists()
    assert not (output / joint.CASE).exists()
