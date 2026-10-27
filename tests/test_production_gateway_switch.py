"""Synthetic native-transport boundaries only; no Docker daemon or HTTP server."""

from copy import deepcopy
import fcntl
import hashlib
import os
from pathlib import Path
import signal
import time
from types import SimpleNamespace

import pytest

from scripts import production_gateway_switch as switch
from services import admission_fence as gate


def save(path, raw):
    path.write_bytes(raw)
    path.chmod(0o600)
    return switch.file_identity(path)[0]


def base_row(identifier, image, config_path, network, name):
    return {
        "Id": identifier,
        "Image": image,
        "Name": "/" + name,
        "RestartCount": 0,
        "Config": {
            "Labels": {"com.docker.compose.project": "retained-app"},
            "User": "",
            "Entrypoint": ["/docker-entrypoint.sh"],
            "Cmd": ["nginx", "-g", "daemon off;"],
            "Env": [],
        },
        "State": {
            "Running": False,
            "Status": "exited",
            "ExitCode": 0,
            "OOMKilled": False,
            "Paused": False,
            "Restarting": False,
            "Dead": False,
            "StartedAt": "original-start",
            "FinishedAt": "original-stop",
        },
        "Mounts": [{"Type": "bind", "Source": str(config_path), "Destination": switch.CONFIG_TARGET, "RW": False}],
        "HostConfig": {
            "Memory": 128 * 1024**2,
            "MemorySwap": 256 * 1024**2,
            "PidsLimit": 64,
            "NanoCpus": 0,
            "ReadonlyRootfs": False,
            "SecurityOpt": ["no-new-privileges:true"],
            "IpcMode": "private",
            "NetworkMode": network,
            "RestartPolicy": {"Name": "unless-stopped"},
            "PortBindings": {
                "5000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5000"}, {"HostIp": "172.17.0.1", "HostPort": "5000"}]
            },
        },
        "NetworkSettings": {
            "Networks": {
                "retained-net": {
                    "NetworkID": network,
                    "EndpointID": "original-endpoint",
                    "IPAddress": "172.19.0.2",
                    "Aliases": [name],
                    "DNSNames": [name],
                }
            }
        },
    }


def _mock_transport(monkeypatch, state, network, old_id, image, new_config):
    def inspect(_self, identifier):
        return deepcopy(state.rows[identifier])

    def members(_self, identifier):
        assert identifier == network
        rows = [r for r in state.rows.values() if r["State"]["Running"]]
        return {"Id": network, "Name": "retained-net"}, deepcopy(rows)

    def find(_self, operation, purpose):
        rows = [
            r
            for r in state.rows.values()
            if r["Config"]["Labels"].get("io.ai-filter.gateway.operation") == operation
            and r["Config"]["Labels"].get("io.ai-filter.gateway.purpose") == purpose
        ]
        assert len(rows) <= 1
        return deepcopy(rows[0]) if rows else None

    def run(self, args, **_kw):
        state.calls.append(list(args))
        stage = args[0]
        stdout, stderr = b"", b""
        if stage == "create":
            labels = dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "--label")
            purpose = labels["io.ai-filter.gateway.purpose"]
            stage = purpose + "-create"
            identifier = ("5" if purpose == "config-test" else "6") * 64
            row = base_row(identifier, image, new_config["path"], network, args[args.index("--name") + 1])
            row["Config"].update(
                Labels=labels,
                Entrypoint=["nginx"],
                Cmd=["-t", "-c", switch.CONFIG_TARGET] if purpose == "config-test" else ["-g", "daemon off;"],
            )
            row["State"].update(Status="created", StartedAt="", FinishedAt="")
            row["HostConfig"]["RestartPolicy"] = {"Name": "no"}
            if purpose == "config-test":
                row["HostConfig"].update(NetworkMode="none", PortBindings={})
                row["NetworkSettings"]["Networks"] = {"none": {}}
            else:
                row["NetworkSettings"]["Networks"]["retained-net"].update(
                    EndpointID="gateway-endpoint", IPAddress="172.19.0.8"
                )
            state.rows[identifier] = row
            stdout = (identifier + "\n").encode()
        elif stage == "start":
            row = state.rows[args[-1]]
            purpose = row["Config"]["Labels"]["io.ai-filter.gateway.purpose"]
            stage = purpose + "-start"
            row["State"].update(
                Running=purpose != "config-test",
                Status="exited" if purpose == "config-test" else "running",
                StartedAt="new-start",
                ExitCode=state.tested_exit if purpose == "config-test" else 0,
            )
            stderr = b"synthetic config-test diagnostics\n" if purpose == "config-test" else b""
        elif stage == "kill":
            assert args == ["kill", "--signal", "TERM", old_id]
            state.rows[old_id]["State"].update(Running=False, Status="exited", ExitCode=143, FinishedAt="term-stop")
            stage = "old-term"
        else:
            raise AssertionError(args)
        self.last_result = {"stdout": stdout, "stderr": stderr, "returncode": 0}
        if state.fail == stage:
            raise TimeoutError("synthetic lost ACK after actual mutation")
        return stdout

    def health(_self, host, port, operation):
        state.http.append((host, port, operation))
        return {
            "status": 200,
            "body_sha256": hashlib.sha256(b"OK").hexdigest(),
            "operation": operation,
            "upstream": "wrong:5000" if state.wrong_http else "172.19.0.7:5000",
        }

    monkeypatch.setattr(switch.NativeTransport, "inspect", inspect)
    monkeypatch.setattr(switch.NativeTransport, "network_members", members)
    monkeypatch.setattr(switch.NativeTransport, "image", lambda _self: {"Id": image})
    monkeypatch.setattr(switch.NativeTransport, "find", find)
    monkeypatch.setattr(switch.NativeTransport, "run", run)
    monkeypatch.setattr(switch.NativeTransport, "health", health)


@pytest.fixture
def case(tmp_path, monkeypatch):
    tmp_path = tmp_path.resolve()
    operation, network, old_id, api_id, image = "a" * 64, "b" * 64, "c" * 64, "d" * 64, "sha256:" + "e" * 64
    evidence, runtime = tmp_path / "evidence", tmp_path / "runtime"
    evidence.mkdir(mode=0o700)
    runtime.mkdir(mode=0o700)
    old_config = save(tmp_path / "old.conf", b"retained original nginx configuration\n")
    new_config = save(tmp_path / "new.conf", switch.render_config(operation, "candidate-api"))
    old = base_row(old_id, image, old_config["path"], network, "old-gateway")
    api = base_row(api_id, "sha256:" + "f" * 64, old_config["path"], network, "candidate-api")
    api["State"].update(Running=True, Status="running", StartedAt="candidate-start")
    api["Config"]["Env"] = [
        "AI_FILTER_TRUSTED_PROXY_HOST=" + switch.gateway_name(operation),
        "AI_FILTER_WEB_BASE_URL=http://" + switch.gateway_name(operation) + ":5000",
        "PRIVATE_TEST=do-not-persist",
    ]
    api["NetworkSettings"]["Networks"]["retained-net"].update(EndpointID="actual-api-endpoint", IPAddress="172.19.0.7")
    now = int(switch.time.time())
    session = object.__new__(switch.ReceiverActivationSession)
    session.root, session.opened, session.operation_id = runtime, False, "1" * 64
    session.permit = {
        "source": {"revision": "2" * 40, "image_id": api["Image"]},
        "not_before": now - 10,
        "expires_at": now + 600,
    }
    session.descriptor = {"slots": {"api-1": "web", "worker-1": "worker"}}
    ready = {"api-1": "3" * 64, "worker-1": "4" * 64}
    gate._write(runtime / "activation.confirm.json", {"slots": ready, "slots_sha256": gate._hash(ready)})
    monkeypatch.setattr(switch.ReceiverActivationSession, "_identity", lambda _self, **_kw: None)
    monkeypatch.setattr(switch.ReceiverActivationSession, "_owners", lambda _self: {"api-1": {"container_id": api_id}})
    monkeypatch.setattr(switch.ReceiverActivationSession, "_ready", lambda _self: ready)
    monkeypatch.setattr(switch.ReceiverActivationSession, "check_receiver_resume", lambda *_args, **_kw: {})
    plan = {
        "schema": 1,
        "operation_id": operation,
        "source_sha256": switch.source_sha256(),
        "source_sha": session.permit["source"]["revision"],
        "window": {"not_before": now - 1, "expires_at": now + 500},
        "old_gateway_id": old_id,
        "old_gateway_identity": switch.gateway_identity(old),
        "old_config": old_config,
        "new_config": new_config,
        "api_alias": "candidate-api",
        "network_id": network,
        "wait_seconds": 1,
        "max_polls": 1,
    }
    state = SimpleNamespace(
        rows={old_id: old, api_id: api}, calls=[], http=[], fail=None, wrong_http=False, tested_exit=0
    )
    transport = switch.NativeTransport()

    _mock_transport(monkeypatch, state, network, old_id, image, new_config)
    return SimpleNamespace(
        plan=plan,
        evidence=evidence,
        session=session,
        transport=transport,
        state=state,
        old=old,
        api=api,
        now=now,
        ready=ready,
    )


def adapter(case):
    return switch.GatewaySwitch(case.session, case.plan, case.evidence, transport=case.transport)


@pytest.mark.parametrize("running", [False, True])
def test_actual_stage_chain_retains_old_originals_and_independent_project(case, running):
    p = case
    p.old["State"].update(Running=running, Status="running" if running else "exited")
    subject = adapter(p)
    result = subject.switch()
    assert result["status"] == "gateway_switched" and len(result["http"]) == 2
    assert result["receiver_opened_by_adapter"] is result["sender_resumed_by_adapter"] is False
    assert result["automatic_rollback"] is False
    assert (p.evidence / "old-nginx.conf").read_bytes() == Path(p.plan["old_config"]["path"]).read_bytes()
    assert (p.evidence / "config-test-start.stderr").read_bytes() == b"synthetic config-test diagnostics\n"
    assert all(b"do-not-persist" not in f.read_bytes() for f in p.evidence.iterdir())
    assert sum(call[0] == "kill" for call in p.state.calls) == int(running)
    assert all("--signal" not in c or c[c.index("--signal") + 1] == "TERM" for c in p.state.calls)
    creates = [call for call in p.state.calls if call[0] == "create"]
    assert len(creates) == 2 and "--publish" not in creates[0]
    assert creates[0][creates[0].index("--network") + 1] == "none"
    assert "com.docker.compose.project=" + subject.project in creates[1]
    assert subject.project != "retained-app"
    assert creates[1][creates[1].index("--network") + 1] == p.plan["network_id"]
    count = len(p.state.calls)
    assert subject.query()["query_only"] is True
    assert len(p.state.calls) == count
    with pytest.raises(gate.AdmissionError, match="already open|attempted"):
        subject.switch()


@pytest.mark.parametrize(
    "stage", ["config-test-create", "config-test-start", "old-term", "gateway-create", "gateway-start"]
)
def test_lost_ack_never_replays_or_advances_unattempted_stage(case, stage):
    p = case
    p.old["State"].update(Running=True, Status="running")
    p.state.fail = stage
    subject = adapter(p)
    with pytest.raises(TimeoutError, match="lost ACK"):
        subject.switch()
    calls = deepcopy(p.state.calls)
    result = subject.query()
    assert p.state.calls == calls
    assert result["status"] == ("gateway_switched" if stage == "gateway-start" else "gateway_stage_unconfirmed")
    with pytest.raises(gate.AdmissionError, match="attempted"):
        subject.switch()
    assert p.state.calls == calls


@pytest.mark.parametrize(
    "field",
    [
        "source",
        "config_bytes",
        "config_inode",
        "old_id",
        "old_image",
        "old_mount",
        "old_restart",
        "api_alias",
        "duplicate_alias",
        "api_env",
        "api_image",
        "api_restart",
        "api_endpoint",
        "open",
        "expired",
    ],
)
def test_preflight_rejects_changes_before_any_mutation(case, field, monkeypatch):
    p = case
    if field == "source":
        p.plan["source_sha256"] = "0" * 64
    elif field == "config_bytes":
        Path(p.plan["new_config"]["path"]).write_bytes(b"changed")
    elif field == "config_inode":
        path = Path(p.plan["old_config"]["path"])
        raw = path.read_bytes()
        path.rename(path.with_suffix(".retained"))
        save(path, raw)
    elif field == "old_id":
        p.old["Id"] = "0" * 64
    elif field == "old_image":
        p.old["Image"] = "sha256:" + "0" * 64
    elif field == "old_mount":
        p.old["Mounts"][0]["RW"] = True
    elif field == "old_restart":
        p.old["RestartCount"] = 1
    elif field == "api_alias":
        p.api["NetworkSettings"]["Networks"]["retained-net"].update(Aliases=["other"], DNSNames=["other"])
    elif field == "duplicate_alias":
        row = deepcopy(p.api)
        row["Id"] = "9" * 64
        p.state.rows[row["Id"]] = row
    elif field == "api_env":
        p.api["Config"]["Env"][0] = "AI_FILTER_TRUSTED_PROXY_HOST=old-gateway"
    elif field == "api_image":
        p.api["Image"] = "sha256:" + "0" * 64
    elif field == "api_restart":
        p.api["RestartCount"] = 1
    elif field == "api_endpoint":
        p.api["NetworkSettings"]["Networks"]["retained-net"]["EndpointID"] = ""
    elif field == "open":
        p.session.opened = True
    else:
        monkeypatch.setattr(switch.time, "time", lambda: p.now + 501)
    with pytest.raises(gate.AdmissionError):
        adapter(p).switch()
    assert p.state.calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("Memory", 129 * 1024**2),
        ("MemorySwap", 257 * 1024**2),
        ("PidsLimit", 65),
        ("NanoCpus", 1000000001),
        ("Privileged", True),
        ("SecurityOpt", []),
        ("CapAdd", ["NET_ADMIN"]),
        ("Ulimits", [{"Name": "nofile", "Hard": 100, "Soft": 100}]),
        ("Sysctls", {"net.ipv4.ip_forward": "1"}),
        ("OomKillDisable", True),
    ],
)
def test_resources_and_security_are_not_relaxed(case, field, value):
    p = case
    p.old["HostConfig"][field] = value
    p.plan["old_gateway_identity"] = switch.gateway_identity(p.old)
    with pytest.raises(gate.AdmissionError, match="resource|security"):
        adapter(p).switch()
    assert p.state.calls == []


@pytest.mark.parametrize("host", ["0.0.0.0", "8.8.8.8", "::1", "224.0.0.1"])
def test_explicit_private_ipv4_host_bindings_only(case, host):
    p = case
    p.old["HostConfig"]["PortBindings"]["5000/tcp"][0]["HostIp"] = host
    p.plan["old_gateway_identity"] = switch.gateway_identity(p.old)
    with pytest.raises(gate.AdmissionError, match="binding"):
        adapter(p).switch()
    assert p.state.calls == []


def test_failed_configuration_does_not_stop_old_or_create_main(case):
    p = case
    p.old["State"].update(Running=True, Status="running")
    p.state.tested_exit = 1
    with pytest.raises(gate.AdmissionError, match="configuration test"):
        adapter(p).switch()
    assert [call[0] for call in p.state.calls] == ["create", "start"]
    assert p.old["State"]["Running"]
    assert not (p.evidence / "config-tested.json").exists()


def test_wrong_actual_http_stops_without_rollback_and_exact_query_can_recover(case):
    p = case
    p.state.wrong_http = True
    subject = adapter(p)
    with pytest.raises(gate.AdmissionError, match="actual gateway HTTP"):
        subject.switch()
    assert not (p.evidence / "result.json").exists()
    calls = deepcopy(p.state.calls)
    p.state.wrong_http = False
    assert subject.query()["status"] == "gateway_switched"
    assert p.state.calls == calls


def test_query_after_expiry_is_read_only_and_has_total32_persistent_slots(case, monkeypatch):
    p = case
    subject = adapter(p)
    subject.switch()
    calls = deepcopy(p.state.calls)
    monkeypatch.setattr(switch.time, "time", lambda: p.now + 501)
    for index in range(32):
        assert subject.query()["query_only"]
        assert (p.evidence / f"query-{index:02d}.json").exists()
    with pytest.raises(gate.AdmissionError, match="budget exhausted"):
        adapter(p).query()
    assert p.state.calls == calls


@pytest.mark.parametrize(
    "predecessor",
    [
        "config-tested.json",
        "config-test-start.intent.json",
        "old-stopped.json",
        "gateway-created.json",
        "gateway-create.intent.json",
    ],
)
def test_query_cannot_invent_a_missing_predecessor(case, predecessor):
    p = case
    subject = adapter(p)
    subject.switch()
    path = p.evidence / predecessor
    path.rename(path.with_suffix(".retained"))
    calls = deepcopy(p.state.calls)
    with pytest.raises(gate.AdmissionError, match="not confirmed"):
        subject.query()
    assert p.state.calls == calls


def test_pending_query_slots_remain_consumed(case):
    p = case
    subject = adapter(p)
    subject.switch()
    for index in range(32):
        gate._write(
            p.evidence / f"query-{index:02d}.json.pending", {"operation_id": p.plan["operation_id"], "reserved": True}
        )
    with pytest.raises(gate.AdmissionError, match="budget exhausted"):
        subject.query()


def test_concurrent_controller_refused_before_mutation(case):
    p = case
    fd = os.open(p.evidence / "control.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            adapter(p).switch()
    finally:
        os.close(fd)
    assert p.state.calls == []


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_config_original_cannot_follow_links(tmp_path, kind):
    source = tmp_path / "source"
    save(source, b"retained\n")
    path = tmp_path / "linked"
    if kind == "symlink":
        path.symlink_to(source)
    else:
        os.link(source, path)
    with pytest.raises((gate.AdmissionError, switch.private.ContractError)):
        switch.file_identity(path)


def test_real_capability_and_fixed_transport_required(case):
    with pytest.raises(gate.AdmissionError, match="real receiver"):
        switch.GatewaySwitch(SimpleNamespace(), case.plan, case.evidence)
    with pytest.raises(gate.AdmissionError, match="fixed native"):
        switch.GatewaySwitch(case.session, case.plan, case.evidence, transport=SimpleNamespace())


def test_result_rename_fsync_failure_is_unknown_until_original_query_durably_confirms(case, monkeypatch):
    p = case
    original = gate._sync_directory

    def sync(path):
        if Path(path) == p.evidence and (p.evidence / "result.json").exists():
            raise OSError("synthetic durable directory failure")
        original(path)

    monkeypatch.setattr(gate, "_sync_directory", sync)
    subject = adapter(p)
    with pytest.raises(OSError, match="durable directory"):
        subject.switch()
    assert (p.evidence / "result.json").exists()
    calls = deepcopy(p.state.calls)
    with pytest.raises(OSError, match="durable directory"):
        subject.query()
    assert p.state.calls == calls
    monkeypatch.setattr(gate, "_sync_directory", original)
    assert subject.query()["query_only"]
    assert p.state.calls == calls


def test_intent_durability_failure_prevents_first_docker_mutation(case, monkeypatch):
    p = case
    original = gate._sync_directory

    def sync(path):
        if Path(path) == p.evidence and (p.evidence / "intent.json").exists():
            raise OSError("synthetic intent sync failed")
        original(path)

    monkeypatch.setattr(gate, "_sync_directory", sync)
    with pytest.raises(OSError, match="intent sync"):
        adapter(p).switch()
    assert p.state.calls == []


@pytest.mark.parametrize("change", ["restart", "network_id", "port", "alias", "config_test_exit", "http_restart"])
def test_query_rechecks_real_container_endpoint_not_just_saved_receipt(case, change, monkeypatch):
    p = case
    subject = adapter(p)
    subject.switch()
    gateway = p.state.rows["6" * 64]
    if change == "restart":
        gateway["RestartCount"] += 1
    elif change == "network_id":
        gateway["NetworkSettings"]["Networks"]["retained-net"]["NetworkID"] = "0" * 64
    elif change == "port":
        gateway["HostConfig"]["PortBindings"]["5000/tcp"][0]["HostPort"] = "5001"
    elif change == "alias":
        p.api["NetworkSettings"]["Networks"]["retained-net"]["Aliases"].append(subject.name)
    elif change == "config_test_exit":
        p.state.rows["5" * 64]["State"]["ExitCode"] = 1
    else:
        health = switch.NativeTransport.health

        def changed(*args):
            result = health(*args)
            gateway["State"]["StartedAt"] = "different-start"
            return result

        monkeypatch.setattr(switch.NativeTransport, "health", changed)
    calls = deepcopy(p.state.calls)
    with pytest.raises(gate.AdmissionError):
        subject.query()
    assert p.state.calls == calls


def test_hard_wall_bounds_blocking_calls_and_restores_handler():
    before = signal.getsignal(signal.SIGALRM)
    start = time.monotonic()
    with pytest.raises(gate.AdmissionUncertain, match="blocking operation deadline"):
        with switch._hard_deadline(0.02):
            time.sleep(1)
    assert time.monotonic() - start < 0.5
    assert signal.getsignal(signal.SIGALRM) == before
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
