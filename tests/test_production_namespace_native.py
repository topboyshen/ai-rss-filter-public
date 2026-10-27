"""Offline tests of native helper wiring; these never claim nsenter execution."""
from copy import deepcopy
import json
import hashlib
import os
from types import SimpleNamespace

import pytest

from scripts import production_namespace_native as native
from services import admission_fence as gate


@pytest.fixture
def owned(tmp_path, monkeypatch):
    owner, token = "a" * 32, "b" * 64
    options = {"container_id": "c" * 64, "owner": owner, "image_id": "sha256:" + "d" * 64,
        "manifest": "e" * 64, "network": "miniflux-pause-" + owner[:12] + "-network",
        "stop_operation": "f" * 64, "evidence_root": str(tmp_path / "root-originals")}
    row = {"Id": options["container_id"], "Image": options["image_id"], "RestartCount": 0,
        "Config": {"Labels": {native.LABEL: owner}, "Env": ["HANDOFF_ENABLED=1", "HANDOFF_MANIFEST=" + options["manifest"],
            "HANDOFF_CONTROL_ADDR=127.0.0.1:8787", "HANDOFF_CONTROL_TOKEN=" + token, "ADMIN_USERNAME=synthetic",
            "DATABASE_URL=postgres://synthetic:only-synthetic@pg:5432/synthetic_pause?sslmode=disable"]},
        "HostConfig": {"NetworkMode": options["network"], "PortBindings": {}, "Memory": native.MEMORY,
            "MemorySwap": native.MEMORY, "PidsLimit": 256, "NanoCpus": 1_000_000_000, "Privileged": False,
            "ReadonlyRootfs": True, "CapAdd": [], "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"]},
        "State": {"Running": True, "OOMKilled": False, "Paused": False, "Restarting": False, "Dead": False,
                  "Pid": 321, "StartedAt": "synthetic-start"},
        "NetworkSettings": {"Networks": {options["network"]: {}}}}
    network = {"Name": options["network"], "Internal": True, "Labels": {native.LABEL: owner}}
    p = SimpleNamespace(options=options, token=token, row=row, network=network, calls=[], docker=[], lost=False)
    def docker(args, deadline):
        p.docker.append(args)
        return [deepcopy(network if args[0] == "network" else row)]
    def receipt(operation, action, state, generation):
        return {"confirmed": True, "result": {"schema": 1, "manifest": options["manifest"], "operation_id": operation,
            "action": action, "state": state, "durable": True, "confirmed": True, "generation": generation,
            "previous_generation": generation - 1}}
    class Transport:
        def __init__(self, url, token_file, *, container_id):
            assert url == "http://127.0.0.1:8787" and container_id == options["container_id"]
            assert gate._read(token_file) == {"token": token} and token_file.stat().st_mode & 0o777 == 0o600
        def request(self, method, path, payload=None):
            p.calls.append((method, path, payload))
            if method == "POST":
                if p.lost:
                    raise TimeoutError("synthetic ACK lost")
                return 200, receipt(payload["operation_id"], "pause", "closed", 3)
            operation = path.removeprefix("/operations/")
            return 200, receipt(operation, "stop_new" if operation == options["stop_operation"] else "pause",
                                "draining" if operation == options["stop_operation"] else "closed",
                                2 if operation == options["stop_operation"] else 3)
    monkeypatch.setattr(native, "_host", lambda: None)  # Explicit synthetic unit boundary only.
    monkeypatch.setattr(native, "_docker", docker)
    monkeypatch.setattr(native, "NativeSenderTransport", Transport)
    return p


def test_owned_fixture_single_post_original_query_and_private_evidence(owned):
    p = owned
    result = native.execute_owned(p.options, p.token)
    assert result["status"] == "passed" and result["get_count"] == 2 and result["post_count"] == 1
    assert result["queue_drained"] is result["production_access"] is False
    assert [method for method, _, _ in p.calls] == ["GET", "POST", "GET"]
    assert p.calls[-1][1] == "/operations/" + p.calls[1][2]["operation_id"]
    assert p.token not in json.dumps(result)
    root = native.Path(p.options["evidence_root"])
    assert root.stat().st_mode & 0o777 == 0o700 and all(path.stat().st_mode & 0o777 == 0o600 for path in root.iterdir())
    assert gate._read(root / "pause.intent.json")["post_count_limit"] == 1
    assert all(args[0] in {"inspect", "network"} for args in p.docker)
    with pytest.raises(FileExistsError):
        native.execute_owned(p.options, p.token)
    assert len(p.calls) == 3


def test_lost_response_preserves_intent_and_never_reposts(owned):
    p = owned
    p.lost = True
    with pytest.raises(TimeoutError):
        native.execute_owned(p.options, p.token)
    root = native.Path(p.options["evidence_root"])
    assert (root / "pause.intent.json").exists() and not (root / "RESULT.json").exists()
    assert gate._read(root / "STOP.json")["next_action"] == "preserve_originals_query_only_no_post_retry"
    assert [method for method, _, _ in p.calls] == ["GET", "POST"]


@pytest.mark.parametrize("key,value", [("Memory", 2**30), ("MemorySwap", 2**30), ("PidsLimit", 512),
    ("NanoCpus", 2_000_000_000), ("Privileged", True), ("ReadonlyRootfs", False), ("CapAdd", ["SYS_ADMIN"]),
    ("CapDrop", []), ("SecurityOpt", []), ("PortBindings", {"8787/tcp": [{"HostPort": "8787"}]})])
def test_native_resource_or_published_port_drift_refuses_before_http(owned, key, value):
    p = owned
    p.row["HostConfig"][key] = value
    with pytest.raises(gate.AdmissionError):
        native.execute_owned(p.options, p.token)
    assert p.calls == []


@pytest.mark.parametrize("change", ["owner", "image", "manifest", "token", "duplicate", "database", "network"])
def test_other_or_non_synthetic_sender_never_receives_control(owned, change):
    p = owned
    if change == "owner":
        p.row["Config"]["Labels"][native.LABEL] = "0" * 32
    elif change == "image":
        p.row["Image"] = "sha256:" + "0" * 64
    elif change == "network":
        p.network["Internal"] = False
    else:
        prefix = {"manifest": "HANDOFF_MANIFEST=", "token": "HANDOFF_CONTROL_TOKEN=", "database": "DATABASE_URL="}.get(change)
        if change == "duplicate":
            p.row["Config"]["Env"].append("HANDOFF_ENABLED=1")
        else:
            p.row["Config"]["Env"] = [prefix + "wrong" if entry.startswith(prefix) else entry for entry in p.row["Config"]["Env"]]
    with pytest.raises(gate.AdmissionError):
        native.execute_owned(p.options, p.token)
    assert p.calls == []


def test_nonroot_boundary_does_not_write_or_call_docker(monkeypatch, tmp_path):
    monkeypatch.setattr(native.os, "geteuid", lambda: 1001)
    with pytest.raises(gate.AdmissionError, match="Linux root"):
        native.execute_owned({}, "b" * 64)
    assert not list(tmp_path.iterdir())


def test_cli_parser_requires_only_fixed_synthetic_identity_fields():
    assert native.NativeSenderTransport.__module__ == "production_sender_driver"
    assert os.path.isabs(native.__file__)


def test_safe_export_redacts_nested_token_and_every_inspect_environment(owned):
    p = owned
    native.execute_owned(p.options, p.token)
    root = native.Path(p.options["evidence_root"])
    before_inspect = gate._read(root / "inspect.before.json")
    before_inspect["nested"] = {"url": "synthetic://" + p.token, "list": [p.token, {p.token: p.token}]}
    gate._write(root / "inspect.before.json", before_inspect)
    before = {path.name: (path.read_bytes(), native._metadata(path.stat())) for path in root.iterdir()}
    packet = native.safe_export(p.options, p.token)
    raw = json.dumps(packet)
    assert packet["complete"] is True and packet["originals_modified"] is False
    assert p.token not in raw and "only-synthetic" not in raw and "postgres://" not in raw
    assert packet["files"]["inspect.before.json"]["content"]["Config"]["Env"] == "<redacted-env>"
    assert packet["files"]["inspect.before.json"]["content"]["nested"]["url"] == "synthetic://<redacted-token>"
    assert "content" not in packet["files"]["token.json"]
    for path in root.iterdir():
        assert (path.read_bytes(), native._metadata(path.stat())) == before[path.name]
        assert packet["files"][path.name]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert packet["files"][path.name]["metadata"] == before[path.name][1]


def test_failed_execution_still_exports_original_stop_hash_without_secrets(owned):
    p = owned
    p.lost = True
    result = native.run_report(p.options, p.token)
    assert result["status"] == "unconfirmed" and result["error_type"] == "TimeoutError"
    packet = result["safe_evidence"]
    stop = native.Path(p.options["evidence_root"]) / "STOP.json"
    assert packet["complete"] is True and packet["files"]["STOP.json"]["sha256"] == hashlib.sha256(stop.read_bytes()).hexdigest()
    assert "RESULT.json" not in packet["files"] and "pause.intent.json" in packet["files"]
    assert p.token not in json.dumps(result) and "only-synthetic" not in json.dumps(result)
    assert [method for method, _, _ in p.calls] == ["GET", "POST"]


def test_export_output_bound_omits_content_but_preserves_exact_original_hash(owned):
    p = owned
    native.execute_owned(p.options, p.token)
    root = native.Path(p.options["evidence_root"])
    large = {"nested": "x" * native.EXPORT_BYTES}
    (root / "inspect.before.json").write_text(json.dumps(large))
    original = (root / "inspect.before.json").read_bytes()
    packet = native.safe_export(p.options, p.token)
    assert packet["complete"] is False and len(native.namespace._encoded(packet)) <= native.EXPORT_BYTES
    record = packet["files"]["inspect.before.json"]
    assert record["content_omitted"] == "output_budget" and "content" not in record
    assert record["sha256"] == hashlib.sha256(original).hexdigest()
    assert (root / "inspect.before.json").read_bytes() == original


def test_export_rejects_wrong_token_and_symlink_without_reading_target(owned, tmp_path):
    p = owned
    native.execute_owned(p.options, p.token)
    with pytest.raises(gate.AdmissionError, match="identity differs"):
        native.safe_export(p.options, "1" * 64)
    root = native.Path(p.options["evidence_root"])
    (root / "inspect.before.json").rename(tmp_path / "retained-before.json")
    (root / "inspect.before.json").symlink_to(tmp_path / "retained-before.json")
    with pytest.raises(OSError):
        native.safe_export(p.options, p.token)


@pytest.mark.parametrize("lost", [False, True])
def test_main_stdout_is_the_bounded_safe_package_on_success_or_failure(owned, monkeypatch, lost):
    import io
    p = owned
    p.lost = lost
    argv = [native.__file__]
    for name, value in p.options.items():
        argv.extend(["--" + name.replace("_", "-"), value])
    stdout = io.BytesIO()
    monkeypatch.setattr(native.sys, "argv", argv)
    monkeypatch.setattr(native.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps({"token": p.token}).encode())))
    monkeypatch.setattr(native.sys, "stdout", SimpleNamespace(buffer=stdout))
    assert native.main() == (3 if lost else 0)
    raw = stdout.getvalue()
    packet = json.loads(raw)
    assert packet["status"] == ("unconfirmed" if lost else "passed") and packet["safe_evidence"]["complete"] is True
    assert ("STOP.json" if lost else "RESULT.json") in packet["safe_evidence"]["files"]
    assert len(raw) <= native.EXPORT_BYTES and p.token.encode() not in raw and b"only-synthetic" not in raw


def test_main_invalid_private_input_still_emits_safe_incomplete_packet(owned, monkeypatch):
    import io
    p = owned
    argv = [native.__file__]
    for name, value in p.options.items():
        argv.extend(["--" + name.replace("_", "-"), value])
    stdout = io.TextIOWrapper(io.BytesIO())
    monkeypatch.setattr(native.sys, "argv", argv)
    monkeypatch.setattr(native.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps({"other": p.token}).encode())))
    monkeypatch.setattr(native.sys, "stdout", stdout)
    assert native.main() == 3
    stdout.flush()
    raw = stdout.buffer.getvalue()
    assert json.loads(raw)["safe_evidence"]["complete"] is False
    assert p.token.encode() not in raw and not native.Path(p.options["evidence_root"]).exists()
