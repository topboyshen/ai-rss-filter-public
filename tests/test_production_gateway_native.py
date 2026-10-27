"""Fixture wiring guards; mocked Docker/HTTP does not prove native execution."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import production_gateway_native as native
from scripts import production_gateway_switch as gateway
from services import admission_fence as gate


@pytest.fixture
def case(tmp_path, monkeypatch):
    directory = tmp_path.resolve() / "receiver"
    directory.mkdir(mode=0o700)
    manifest = {
        "name": "synthetic-app",
        "services": {
            name: {"environment": {"UNCHANGED": "synthetic"}} for name in ("ai-filter-api", "ai-filter-worker")
        },
    }
    gate._write(directory / "manifest.json", manifest)
    p = {
        "directory": directory,
        "manifest": manifest,
        "manifest_path": directory / "manifest.json",
        "project": "synthetic-app",
        "source": {"source_sha": "a" * 40},
    }
    owner, identifier, image_id, network_id = "b" * 32, "c" * 64, "sha256:" + "d" * 64, "e" * 64
    network = {
        "Name": "owned-internal",
        "Id": network_id,
        "Internal": True,
        "Labels": {native.LABEL: owner},
        "Driver": "bridge",
        "Scope": "local",
    }
    image = {
        "Id": image_id,
        "Architecture": "amd64",
        "Os": "linux",
        "RepoDigests": ["nginx@" + gateway.NGINX_REF.split("@", 1)[1]],
    }
    state = SimpleNamespace(rows={}, calls=[], fail=None, network=network, image=image, kills=0)
    harness = SimpleNamespace(
        owner=owner,
        prefix="synthetic-owned",
        network=network["Name"],
        output=tmp_path.resolve(),
        names={},
        images={},
        created=[],
    )

    def command(args, **kwargs):
        state.calls.append((deepcopy(args), kwargs))
        if args[:2] == ["network", "inspect"]:
            return json.dumps([network])
        if args[:2] == ["image", "inspect"]:
            assert args[2] == gateway.NGINX_REF
            return json.dumps([image])
        if args[0] == "create":
            labels = dict(args[i + 1].split("=", 1) for i, value in enumerate(args) if value == "--label")
            source = args[args.index("--mount") + 1].split(",src=", 1)[1].split(",dst=", 1)[0]
            state.rows[identifier] = {
                "Id": identifier,
                "Image": image_id,
                "Name": "/" + args[args.index("--name") + 1],
                "RestartCount": 0,
                "Config": {"Labels": labels},
                "State": {"Running": False, "Status": "created", "StartedAt": "", "ExitCode": 0, "OOMKilled": False},
                "HostConfig": {"Memory": 128 * 1024**2, "MemorySwap": 128 * 1024**2, "PidsLimit": 64},
                "Mounts": [{"Source": source, "Destination": gateway.CONFIG_TARGET, "RW": False}],
                "NetworkSettings": {"Networks": {network["Name"]: {"NetworkID": network_id}}},
            }
            if state.fail == "create":
                raise TimeoutError("synthetic create ACK lost")
            return identifier + "\n"
        if args[0] == "start":
            state.rows[args[-1]]["State"].update(Running=True, Status="running", StartedAt="actual-synthetic-start")
            return identifier
        if args[0] == "kill":
            assert args == ["kill", "--signal", "TERM", identifier]
            state.kills += 1
            state.rows[identifier]["State"].update(Running=False, Status="exited", ExitCode=143)
            if state.fail == "term":
                raise TimeoutError("synthetic TERM ACK lost")
            return identifier
        if args[0] == "inspect":
            row = next(r for r in state.rows.values() if args[1] in {r["Id"], r["Name"].lstrip("/")})
            return json.dumps([row])
        if args[0] == "logs":
            return "synthetic retained nginx logs\n"
        raise AssertionError(args)

    def wait(function, **_kwargs):
        result = function()
        if result is None:
            raise TimeoutError("synthetic bounded wait")
        return result

    harness.command, harness.wait = command, wait
    harness.safe_inspect = lambda row: {k: deepcopy(row[k]) for k in ("Id", "State", "HostConfig", "Mounts")}
    monkeypatch.setattr(native, "_port", lambda: 35678)
    monkeypatch.setattr(
        gateway.NativeTransport,
        "health",
        lambda *_args: {
            "status": 200,
            "body_sha256": hashlib.sha256(b"OLD").hexdigest(),
            "operation": None,
            "upstream": None,
        },
    )
    monkeypatch.setattr(gateway.NativeTransport, "find", lambda *_args: None)
    return SimpleNamespace(p=p, harness=harness, state=state, identifier=identifier)


def test_prepare_keeps_two_apps_fixed_resources_old_original_and_real_port_preconditions(case):
    c = case
    before = c.p["manifest_path"].read_bytes()
    template = native.prepare(c.harness, c.p)
    assert set(c.p["manifest"]["services"]) == {"ai-filter-api", "ai-filter-worker"}
    name = gateway.gateway_name(template["operation_id"])
    for service in c.p["manifest"]["services"].values():
        assert service["environment"]["AI_FILTER_TRUSTED_PROXY_HOST"] == name
        assert service["environment"]["AI_FILTER_WEB_BASE_URL"] == "http://" + name + ":5000"
        assert service["environment"]["UNCHANGED"] == "synthetic"
    root = c.p["gateway_native_root"]
    assert (root / "manifest.before.json").read_bytes() == before
    assert Path(template["old_config"]["path"]).read_bytes() == native.OLD_CONFIG
    assert c.state.kills == 1 and c.harness.created == []
    assert c.state.rows[c.identifier]["State"]["Status"] == "exited"
    create = next(args for args, _kwargs in c.state.calls if args[0] == "create")
    assert create[create.index("--memory") + 1] == create[create.index("--memory-swap") + 1] == str(128 * 1024**2)
    assert create[create.index("--pids-limit") + 1] == "64"
    assert create[create.index("--publish") + 1] == "127.0.0.1:35678:5000/tcp"
    assert create[create.index("--network") + 1] == template["network_id"]
    assert not any(args[0] in {"pull", "rm", "stop", "restart"} for args, _ in c.state.calls)
    permit = {"not_before": 100, "expires_at": 900, "source": {"revision": "a" * 40}}
    plan = native.bind_plan(c.p, permit)
    assert set(plan) == gateway.PLAN_KEYS and plan["window"] == {"not_before": 100, "expires_at": 900}
    with pytest.raises(gate.AdmissionError, match="one source-bound"):
        native.bind_plan(c.p, permit)
    native.preserve(c.harness, c.p)
    assert c.state.kills == 1
    assert (root / "FINAL_old.json").exists()


@pytest.mark.parametrize("change", ["network_external", "network_owner", "image_arch", "third_app", "sealed"])
def test_rejects_non_owned_or_already_sealed_fixture_without_creation(case, change):
    c = case
    if change == "network_external":
        c.state.network["Internal"] = False
    elif change == "network_owner":
        c.state.network["Labels"][native.LABEL] = "other-owner"
    elif change == "image_arch":
        c.state.image["Architecture"] = "arm64"
    elif change == "third_app":
        c.p["manifest"]["services"]["third"] = {}
    else:
        (c.p["directory"] / "sealed").mkdir(mode=0o700)
    with pytest.raises((gate.AdmissionError, ValueError)):
        native.prepare(c.harness, c.p)
    assert not any(args[0] == "create" for args, _ in c.state.calls)


@pytest.mark.parametrize("stage", ["create", "term"])
def test_unknown_fixture_mutation_retained_without_repeat_or_forced_cleanup(case, stage):
    c = case
    c.state.fail = stage
    with pytest.raises(TimeoutError, match="ACK lost"):
        native.prepare(c.harness, c.p)
    count = len([args for args, _ in c.state.calls if args[0] == stage or (stage == "term" and args[0] == "kill")])
    native.preserve(c.harness, c.p)
    assert (
        len([args for args, _ in c.state.calls if args[0] == stage or (stage == "term" and args[0] == "kill")]) == count
    )
    assert not any(args[0] in {"rm", "stop", "restart"} for args, _ in c.state.calls)
    assert (c.p["gateway_native_root"] / "PREPARE_INTENT.json").exists()
    assert "gateway_plan" not in c.p


def test_bind_rejects_changed_original_and_does_not_reprepare(case):
    c = case
    template = native.prepare(c.harness, c.p)
    Path(template["old_config"]["path"]).write_bytes(b"changed")
    calls = deepcopy(c.state.calls)
    with pytest.raises(gate.AdmissionError, match="original changed"):
        native.bind_plan(c.p, {"not_before": 1, "expires_at": 2, "source": {"revision": "a" * 40}})
    assert c.state.calls == calls


def test_preparation_requires_original_old_http_evidence(case, monkeypatch):
    c = case
    monkeypatch.setattr(
        gateway.NativeTransport,
        "health",
        lambda *_args: {
            "status": 200,
            "body_sha256": hashlib.sha256(b"other").hexdigest(),
            "operation": None,
            "upstream": None,
        },
    )
    with pytest.raises(gate.AdmissionError, match="old synthetic HTTP"):
        native.prepare(c.harness, c.p)
    assert "gateway_preparation" not in c.p
    native.preserve(c.harness, c.p)
    assert c.state.kills == 1
    assert not c.state.rows[c.identifier]["State"]["Running"]
