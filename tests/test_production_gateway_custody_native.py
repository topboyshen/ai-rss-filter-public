"""Local wiring/negative tests, never a native custody or .125 execution proof."""
from copy import deepcopy
from contextlib import contextmanager
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tarfile
import time

import pytest

from scripts import production_activation_fixture as fixture
from scripts import production_gateway_custody_native as native
from scripts import verify_production_activation_native as activation
from scripts import retained_gateway_configuration as configuration
from services import admission_fence as gate


def archive(name="default.conf", body=native.DEFAULT_CONFIG, kind=tarfile.REGTYPE):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as stream:
        header = tarfile.TarInfo(name)
        header.uid = header.gid = 0
        header.mode, header.type, header.mtime = 0o644, kind, 1
        header.size = len(body)
        stream.addfile(header, io.BytesIO(body))
    return buffer.getvalue()


def fake_transport(raw, observations=None):
    def read_archive(identifier, path, consumer):
        assert identifier == "a" * 64 and path == configuration.FILE_PATHS[2]
        reader = io.BytesIO(raw)
        def read(size):
            if observations is not None:
                observations.append((reader.tell(), size))
            return reader.read(size)
        return consumer(read)
    return SimpleNamespace(archive=read_archive)


def test_archive_retains_exact_parser_consumed_original_and_never_overwrites(tmp_path):
    raw, reads = archive(), []
    path = tmp_path / "original.tar"
    result = native.capture_archive(fake_transport(raw, reads), "a" * 64, configuration.FILE_PATHS[2], path)
    assert result["bytes"] == len(raw) and path.read_bytes() == raw
    assert path.stat().st_mode & 0o777 == 0o600
    assert reads[0] == (0, 512) and max(size for _, size in reads) <= configuration.MAX_FILE
    with pytest.raises(FileExistsError):
        native.capture_archive(fake_transport(raw), "a" * 64, configuration.FILE_PATHS[2], path)
    assert path.read_bytes() == raw


@pytest.mark.parametrize("kind,name", [(tarfile.SYMTYPE, "default.conf"), (tarfile.REGTYPE, "unknown.conf")])
def test_invalid_archive_header_stops_before_payload_and_retains_original(tmp_path, kind, name):
    raw, reads = archive(name=name, kind=kind), []
    path = tmp_path / "failed-original.tar"
    with pytest.raises(gate.AdmissionError):
        native.capture_archive(fake_transport(raw, reads), "a" * 64, configuration.FILE_PATHS[2], path)
    assert reads == [(0, 512)]
    assert path.read_bytes() == raw[:512]


def test_archive_overread_is_not_written(tmp_path):
    transport = SimpleNamespace(archive=lambda identifier, path, consumer: consumer(lambda size: b"x" * (size + 1)))
    path = tmp_path / "overread.tar"
    with pytest.raises(gate.AdmissionError, match="over-read"):
        native.capture_archive(transport, "a" * 64, configuration.FILE_PATHS[2], path)
    assert path.exists() and path.read_bytes() == b""


@pytest.mark.parametrize("context", [None, SimpleNamespace(check=lambda: None)])
def test_unattested_context_cannot_reach_docker_or_native_stage(context):
    harness = SimpleNamespace(private_host_context=context, command=lambda *_a, **_k: pytest.fail("no Docker"))
    with pytest.raises(gate.AdmissionError, match="attested real"):
        native.require_private_host(harness)


def test_attested_context_still_requires_root_and_inherited_deadline(monkeypatch):
    from scripts.production_activation_host_native import PrivateHostContext
    context = object.__new__(PrivateHostContext)
    calls = []
    context.check = lambda: calls.append("check")
    harness = SimpleNamespace(private_host_context=context)
    monkeypatch.setattr(native.os, "geteuid", lambda: 1000)
    with pytest.raises(gate.AdmissionError, match="attested real"):
        native.require_private_host(harness)
    monkeypatch.setattr(native.os, "geteuid", lambda: 0)
    token = native.native_deadline.STATE.set(None)
    try:
        with pytest.raises(gate.AdmissionError, match="inherit"):
            native.require_private_host(harness)
    finally:
        native.native_deadline.STATE.reset(token)
    assert calls == ["check"]


def test_old_manifest_is_exact_same_project_three_roles_zero_gateway_mounts_and_fixed_ports(tmp_path, monkeypatch):
    original = {"name": "same-project", "services": {name: {"image": "old-python", "command": ["old-command"],
        "environment": {"AI_FILTER_ROLE": role}, "volumes": [{"type": "bind", "source": "/synthetic", "target": "/app/data"}],
        "mem_limit": "768m", "pids_limit": 256} for name, role in (("old-api", "web"), ("old-worker", "worker"))}}
    monkeypatch.setattr(fixture, "previous_manifest", lambda p, image: (deepcopy(original),
        {"ai-filter-api": "old-api", "ai-filter-worker": "old-worker"}))
    p = {"custody_owner": "a" * 32, "custody_old_name": "owned-gateway"}
    result, names = native.old_manifest(p, "sha256:" + "b" * 64, configuration.IMAGE_ID)
    assert set(result["services"]) == {*names.values(), native.OLD_SERVICE}
    assert result["name"] == "same-project" and original["services"]["old-api"]["command"] == ["old-command"]
    for name in names.values():
        old = result["services"][name]
        assert old["entrypoint"][-1] == native.bootstrap_native.OLD_PROCESS and old["command"] == []
        assert old["volumes"] == original["services"][name]["volumes"]
    old = result["services"][native.OLD_SERVICE]
    assert old["image"] == configuration.IMAGE_ID and "volumes" not in old and "tmpfs" not in old
    assert old["ports"] == [{"target": 5000, "published": "5000", "host_ip": host, "protocol": "tcp"}
                            for host in native.HOSTS]
    assert old["mem_limit"] == old["memswap_limit"] == "128m" and old["pids_limit"] == 64
    assert old["cap_drop"] == ["ALL"] and old["networks"] == ["isolated"]
    assert old["entrypoint"] == ["/bin/sh", "-ec"] and old["command"][0].endswith("exec nginx -g 'daemon off;'")
    assert "ports" not in result["services"]["old-api"]


def old_rows():
    p = {"project": "same-project", "paths": {"project": Path("/synthetic/project")}, "custody_owner": "a" * 32,
         "custody_old_services": ("old-api", "old-worker", native.OLD_SERVICE), "custody_old_name": "owned-gateway",
         "custody_image_id": configuration.IMAGE_ID}
    rows = []
    for number, name in enumerate(p["custody_old_services"], start=1):
        row = {"Id": str(number) * 64, "Name": "/owned-gateway" if name == native.OLD_SERVICE else "/" + name,
            "Image": configuration.IMAGE_ID if name == native.OLD_SERVICE else "sha256:" + "b" * 64,
            "Config": {"Labels": {"com.docker.compose.service": name, "com.docker.compose.project": p["project"],
                "com.docker.compose.project.working_dir": str(p["paths"]["project"]),
                "io.ai-filter.synthetic-custody": p["custody_owner"]}}, "RestartCount": 0,
            "State": {"Running": True, "Status": "running"}, "Mounts": [],
            "HostConfig": {"PortBindings": {"5000/tcp": [{"HostIp": host, "HostPort": "5000"} for host in native.HOSTS]}},
            "NetworkSettings": {"Ports": {"5000/tcp": [{"HostIp": host, "HostPort": "5000"} for host in native.HOSTS],
                                           "80/tcp": None}}}
        rows.append(row)
    return p, rows


def test_exact_running_old_shape_accepts_only_declared_zero_mount_gateway():
    p, rows = old_rows()
    assert native._running_old(p, rows)[native.OLD_SERVICE] is rows[-1]


@pytest.mark.parametrize("fault", ["mount", "missing_port", "wrong_port", "wildcard", "extra_port", "wrong_image",
                                   "project", "owner", "working_dir", "extra_role", "duplicate", "not_running", "oom", "restart"])
def test_changed_old_topology_is_blocked(fault):
    p, rows = old_rows()
    old = rows[-1]
    if fault == "mount":
        old["Mounts"] = [{"Destination": "/etc/nginx/nginx.conf"}]
    elif fault == "missing_port":
        old["NetworkSettings"]["Ports"]["5000/tcp"].pop()
    elif fault == "wrong_port":
        old["HostConfig"]["PortBindings"]["5000/tcp"][0]["HostPort"] = "5001"
    elif fault == "wildcard":
        old["NetworkSettings"]["Ports"]["5000/tcp"][0]["HostIp"] = "0.0.0.0"
    elif fault == "extra_port":
        old["NetworkSettings"]["Ports"]["80/tcp"] = [{"HostIp": "127.0.0.1", "HostPort": "80"}]
    elif fault == "wrong_image":
        old["Image"] = "sha256:" + "c" * 64
    elif fault in ("project", "owner", "working_dir"):
        key = {"project": "com.docker.compose.project", "owner": "io.ai-filter.synthetic-custody",
               "working_dir": "com.docker.compose.project.working_dir"}[fault]
        old["Config"]["Labels"][key] = "foreign"
    elif fault == "extra_role":
        rows.append(deepcopy(old))
    elif fault == "duplicate":
        rows[0]["Config"]["Labels"]["com.docker.compose.service"] = native.OLD_SERVICE
    elif fault == "not_running":
        old["State"].update(Running=False, Status="exited")
    elif fault == "oom":
        old["State"]["OOMKilled"] = True
    elif fault == "restart":
        old["RestartCount"] = 1
    with pytest.raises(gate.AdmissionError):
        native._running_old(p, rows)


@pytest.mark.parametrize("with_legacy", [False, True])
def test_original_lost_ack_path_passes_exact_live_capability_without_changing_default(tmp_path, monkeypatch, with_legacy):
    from services import production_container_lifecycle as lifecycle
    p = {"operation": "a" * 64, "paths": {name: tmp_path / name for name in ("grants", "slots", "lifecycle")}}
    cap, calls, starts = object(), [], []
    created = {"containers": {"web": "b" * 64, "worker": "c" * 64}}
    def check(kind, kwargs):
        calls.append(kind)
        assert (kwargs.get("legacy") is cap) if with_legacy else "legacy" not in kwargs
    def create(*args, **kwargs):
        check("create", kwargs)
        return created
    def start(*args, **kwargs):
        check("start", kwargs)
        if starts:
            raise gate.AdmissionError("already attempted")
        starts.append(True)
        activation.private.run(["docker", "start", "b" * 64, "c" * 64])
    def query(*args, **kwargs):
        check("query", kwargs)
        return {"status": "closed_initialized"}
    monkeypatch.setattr(lifecycle, "create", create)
    monkeypatch.setattr(lifecycle, "start", start)
    monkeypatch.setattr(lifecycle, "query", query)
    monkeypatch.setattr(activation.private, "run", lambda command: b"started")
    result, _closed = activation.create_and_start(object(), p, **({"legacy": cap} if with_legacy else {}))
    assert result == created and starts == [True] and calls == ["create", "start", "query", "start"]
    assert p.get("custody_new_containers") == (created["containers"] if with_legacy else None)


def test_custody_execute_without_private_context_fails_before_source_or_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(activation, "source_binding", lambda *_a: pytest.fail("must reject before source/Docker"))
    with pytest.raises(gate.AdmissionError, match="actual private host"):
        activation.execute(tmp_path, tmp_path, "sha256:" + "a" * 64, tmp_path, tmp_path, gateway_custody=True)


@pytest.mark.parametrize("lost_start", [False, True])
def test_old_sender_exact_id_is_preserved_before_start_ack_and_never_enters_generic_cleanup(tmp_path, monkeypatch, lost_start):
    from scripts.verify_miniflux_pause_native import LABEL
    calls = []
    h = SimpleNamespace(prefix="owned", python_id="sha256:" + "b" * 64, owner="c" * 32,
                        network="owned-net", names={}, images={}, created=[])
    row = {"Id": "a" * 64, "Image": h.python_id, "Config": {"Labels": {LABEL: h.owner}}, "State": {"Running": False}}
    p = {"custody_roles": {}}
    def command(argv, **kwargs):
        calls.append(deepcopy(argv))
        if argv[0] == "create":
            assert "--memory" in argv and argv[argv.index("--memory") + 1] == str(768 * 1024**2)
            assert "--pids-limit" in argv and argv[argv.index("--pids-limit") + 1] == "256"
            return row["Id"]
        if argv[0] == "inspect":
            return json.dumps([row])
        if argv[0] == "start":
            assert p["custody_roles"]["sender"]["Id"] == row["Id"]
            if lost_start:
                raise TimeoutError("synthetic original start ACK lost")
            row["State"]["Running"] = True
            return "started"
        if argv[0] == "exec":
            return "1"
        raise AssertionError(argv)
    h.command, h.wait = command, lambda function, **kwargs: function()
    monkeypatch.setattr("scripts.verify_miniflux_pause_native.resource_checks", lambda *a: None)
    if lost_start:
        with pytest.raises(TimeoutError, match="ACK lost"):
            native._create_old_sender(h, p, tmp_path)
    else:
        assert native._create_old_sender(h, p, tmp_path)["State"]["Running"]
    assert h.created == [] and p["custody_roles"]["sender"]["Id"] == row["Id"]
    assert sum(command[0] == "start" for command in calls) == 1
    assert not any(command[0] in ("kill", "stop", "rm") for command in calls)
    assert json.loads((tmp_path / "SENDER_CREATED.json").read_bytes())["id"] == row["Id"]


@pytest.mark.parametrize("with_legacy", [False, True])
def test_activation_forwards_same_live_legacy_and_actual_gateway_instead_of_receipt(tmp_path, monkeypatch, with_legacy):
    from services import production_activation_control as control, production_container_lifecycle as lifecycle
    from scripts import production_gateway_switch as gateway
    p = {"directory": tmp_path, "operation": "a" * 64, "activation_operation": "b" * 64,
         "closed_operation": "c" * 64, "paths": {name: tmp_path / name for name in ("grants", "slots", "fence", "lifecycle")},
         "fence": SimpleNamespace(_operation_inventory=lambda: ([], [])), "gateway_plan": {"operation_id": "d" * 64},
         "gateway_evidence": tmp_path / "gateway", "sender_transport": SimpleNamespace(posts=["/initialize-closed"], drop=set())}
    events, resumes, cap = [], [], object()
    def resume():
        resumes.append(True)
        if len(resumes) == 1:
            raise gate.AdmissionError("not yet prepared")
        return {"status": "opened"}
    receiver = SimpleNamespace(prepare=lambda: {}, query=lambda: {"status": "business_prepared"}, confirm=lambda: {}, resume=resume)
    @contextmanager
    def locked(*args, **kwargs):
        yield receiver
    monkeypatch.setattr(control, "locked_activation", locked)
    monkeypatch.setattr(activation.gate, "_read", lambda path: {"phase": "frozen", "generation": 0}
                        if Path(path).name == "state.json" else {"operation_id": "e" * 64})
    switch = SimpleNamespace(switch=lambda: {"status": "switched"}, query=lambda: {"status": "switched"})
    monkeypatch.setattr(gateway, "GatewaySwitch", lambda actual, plan, evidence: switch)
    monkeypatch.setattr(activation, "_watchdog_window", lambda *args: ({}, {}))
    def verify(actual, evidence, *, operation_id, **kwargs):
        assert actual is receiver and operation_id == p["operation"]
        if with_legacy:
            assert kwargs["legacy"] is cap and kwargs["gateway"] is switch
            assert kwargs["gateway"] != {"status": "switched"}
        else:
            assert kwargs == {}
        events.append("verified")
        return {"status": "actual_runtime_verified"}
    monkeypatch.setattr(lifecycle, "verify_active", verify)
    def sender_resume(operation, generation, actual):
        if actual is not receiver:
            raise gate.AdmissionError("real receiver required")
        assert events == ["verified"]
        p["sender_transport"].posts.append("/production-resume")
        raise activation.LostAck("synthetic original ACK discarded")
    driver = SimpleNamespace(production_resume=sender_resume, control=SimpleNamespace(root=tmp_path),
        query_production_resume=lambda operation, actual: {"state": "open"})
    h = SimpleNamespace(stage=lambda *args: None)
    monkeypatch.setattr(activation, "finite_native_consumption", lambda *args: {"bounded": True})
    result = activation.activate(h, p, driver, tmp_path / "migration", object(),
                                 **({"legacy": cap} if with_legacy else {}))
    assert result["supervisor"] == {"bounded": True} and events == ["verified"]
    assert p["sender_transport"].posts.count("/production-resume") == 1


@pytest.mark.parametrize("fault", [None, "json_capability", "wrong_new_app", "extra_old_app", "missing_custody", "wrong_gateway", "not_durable"])
def test_completed_proof_requires_live_type_and_exact_persisted_runtime_association(tmp_path, fault):
    p = {"directory": tmp_path, "paths": {"lifecycle": tmp_path / "lifecycle"},
        "previous_containers": {str(n) * 64: {} for n in (1, 2, 3)},
        "custody_new_containers": {"web": "4" * 64, "worker": "5" * 64},
        "gateway_plan": {"old_gateway_id": "3" * 64, "old_config": {"closure_sha256": "f" * 64}},
        "custody_roles": {"gateway": {"HostConfig": {"PortBindings": {"5000/tcp":
            [{"HostIp": host, "HostPort": "5000"} for host in native.HOSTS]}}}}}
    p["paths"]["lifecycle"].mkdir(mode=0o700)
    verified = {"status": "actual_runtime_verified", "durable": True, "containers": deepcopy(p["custody_new_containers"]),
        "retained_previous_ids": ["1" * 64, "2" * 64], "retained_gateway_custody": {"old_gateway_id": "3" * 64}}
    cap = native.legacy.LegacyTakeover.__new__(native.legacy.LegacyTakeover)
    cap.check_stopped = lambda: {"writer_lock_held": True}
    if fault == "json_capability":
        cap = {"writer_lock_held": True}
    elif fault == "wrong_new_app":
        verified["containers"]["web"] = "6" * 64
    elif fault == "extra_old_app":
        verified["retained_previous_ids"].append("3" * 64)
    elif fault == "missing_custody":
        verified.pop("retained_gateway_custody")
    elif fault == "wrong_gateway":
        verified["retained_gateway_custody"]["old_gateway_id"] = "6" * 64
    elif fault == "not_durable":
        verified["durable"] = False
    gate._write(p["paths"]["lifecycle"] / "active-verified.json", verified)
    if fault is not None:
        with pytest.raises((gate.AdmissionError, KeyError)):
            native.completed_proof(p, cap)
        assert not (tmp_path / "ACTUAL_CUSTODY_PROOF.json").exists()
    else:
        result = native.completed_proof(p, cap)
        assert result["status"] == "passed" and result["actual_125_compatibility_proven"] is False
        assert result["old_project_roles"] == 3 and result["system_cron_access"] is False
        assert len(result["old_gateway_ports"]) == 2


def test_preserve_unknown_project_id_does_not_stop_anything(tmp_path, monkeypatch):
    p = {"directory": tmp_path, "project": "owned", "paths": {"project": tmp_path},
         "previous_containers": {"a" * 64: {}}, "custody_new_containers": {"web": "b" * 64}}
    monkeypatch.setattr(native.private, "inspect_project", lambda _contract: [{"Id": "c" * 64}])
    calls = []
    harness = SimpleNamespace(command=lambda *_a, **_k: calls.append(True))
    with pytest.raises(gate.AdmissionError, match="unknown project"):
        native.preserve(harness, p)
    assert calls == [] and list((tmp_path / "custody-final").iterdir()) == []


@pytest.mark.parametrize("exit_code", [0, 143, 137])
def test_preserve_uses_one_term_and_requires_normal_exit_without_delete(tmp_path, monkeypatch, exit_code):
    p = {"directory": tmp_path, "project": "owned", "paths": {"project": tmp_path},
         "previous_containers": {"a" * 64: {}}}
    original = {"Id": "a" * 64, "Name": "/owned", "Image": "sha256:" + "b" * 64, "Config": {}, "HostConfig": {}, "Mounts": [],
                "RestartCount": 0, "State": {"Running": True, "Status": "running", "StartedAt": "original-start"}}
    current = deepcopy(original)
    monkeypatch.setattr(native.private, "inspect_project", lambda _contract: [deepcopy(original)])
    calls = []
    def command(args, **kwargs):
        calls.append(args)
        if args[0] == "kill":
            assert args == ["kill", "--signal", "TERM", original["Id"]]
            current["State"].update(Running=False, Status="exited", ExitCode=exit_code,
                                    FinishedAt="2026-10-31T13:00:00.123456789Z")
        if args[0] == "inspect":
            return json.dumps([current])
        return ""
    harness = SimpleNamespace(command=command, safe_inspect=deepcopy, wait=lambda function, **kwargs: function())
    if exit_code == 137:
        with pytest.raises(gate.AdmissionError, match="normal owned"):
            native.preserve(harness, p)
        assert not list((tmp_path / "custody-final").glob("*.stopped.json"))
    else:
        native.preserve(harness, p)
        assert len(list((tmp_path / "custody-final").glob("*.stopped.json"))) == 1
    assert sum(command[0] == "kill" for command in calls) == 1
    assert not any(command[0] in ("stop", "rm", "prune") for command in calls)


@pytest.mark.parametrize("conflict", [False, True])
def test_prepare_retains_real_four_archive_bytes_seals_complete_plan_once_and_never_retries_conflict(tmp_path, monkeypatch, conflict):
    """Transport is fake here; real archive/closure/private-custody validation is not."""
    from core import config
    from scripts import private_release_contract as private
    from scripts import production_activation_host_native as host_native
    from scripts.verify_miniflux_pause_native import LABEL
    monkeypatch.setattr(config, "CONFIG_PATH", config.CONFIG_PATH)
    monkeypatch.setattr(config.config, "_cfg", config.config._cfg)
    monkeypatch.setattr(config.config, "_blacklist_re", config.config._blacklist_re)
    monkeypatch.setenv("AI_FILTER_API_KEY", "synthetic-previous")
    monkeypatch.setattr(gate, "_boot_identity", lambda: "synthetic-unit-boot")
    p = fixture.prepare_disk(tmp_path / "disk", {"source_sha": "a" * 40, "source_tree": "b" * 40},
                             "owned-network", "sha256:" + "c" * 64)
    harness = SimpleNamespace(owner="d" * 32, prefix="owned-case", network="owned-network", python_id="sha256:" + "c" * 64,
                              names={}, images={})
    network = {"Name": "owned-network", "Id": "e" * 64}
    rows, commands, sealed = [], [], []
    monkeypatch.setattr(native, "require_private_host", lambda _harness: None)
    monkeypatch.setattr(host_native, "require_harness_network", lambda _harness, _network: None)
    monkeypatch.setattr(native.native_deadline, "clamp", lambda seconds: seconds)
    monkeypatch.setattr("scripts.verify_miniflux_pause_native.image_checks", lambda *_a: None)
    def fake_command(argv, **kwargs):
        commands.append(argv)
        if argv[:2] == ["network", "inspect"]:
            return json.dumps([network])
        if argv[:2] == ["image", "inspect"]:
            from tests.test_retained_gateway_configuration import fixed_image
            return json.dumps([fixed_image(configuration.IMAGE_CONFIG_ID)])
        if argv[0] == "exec":
            return "1"
        if argv[0] == "inspect":
            return json.dumps([next(row for row in rows if row["Id"] == argv[1])])
        raise AssertionError(argv)
    harness.command = fake_command
    harness.wait = lambda function, **kwargs: function()
    def fake_compose(argv):
        commands.append(argv)
        assert argv[-5:] == ["up", "-d", "--no-build", "--pull", "never"]
        if conflict:
            raise RuntimeError("synthetic fixed host port already occupied")
        old = json.loads((p["directory"] / "previous.json").read_bytes())
        for number, (name, service) in enumerate(old["services"].items(), start=1):
            portmap = {"5000/tcp": [{"HostIp": host, "HostPort": "5000"} for host in native.HOSTS]} if name == native.OLD_SERVICE else {}
            rows.append({"Id": str(number) * 64, "Name": "/" + service.get("container_name", "owned-" + name),
                "Image": service["image"], "RestartCount": 0,
                "State": {"StartedAt": "2026-10-31T01:00:00Z", "Running": True, "Status": "running"},
                "Config": {"User": "0:0", "Labels": {**service["labels"], "com.docker.compose.service": name,
                    "com.docker.compose.project": p["project"], "com.docker.compose.project.working_dir": str(p["paths"]["project"])}},
                "HostConfig": {"PortBindings": deepcopy(portmap)}, "Mounts": [],
                "NetworkSettings": {"Ports": deepcopy(portmap), "Networks": {harness.network: {"NetworkID": network["Id"]}}}})
    monkeypatch.setattr(private, "run", fake_compose)
    monkeypatch.setattr(private, "inspect_project", lambda _contract: deepcopy(rows))
    from tests.test_retained_gateway_configuration import archive_map
    data = archive_map(main=native.NGINX_CONFIG, default=native.DEFAULT_CONFIG)
    class Transport:
        def __init__(self):
            self.deadline = time.monotonic() + 120
        def health(self, host, port, operation):
            assert host in native.HOSTS and port == 5000
            return {"status": 200, "body_sha256": native.hashlib.sha256(b"OLD").hexdigest(), "operation": None, "upstream": None}
        def archive(self, identifier, path, consumer):
            assert identifier == rows[-1]["Id"]
            return consumer(io.BytesIO(data[path]).read)
    monkeypatch.setattr(native.gateway, "NativeTransport", Transport)
    def fake_seal(repo, config_path, manifest, env, review_path, out):
        review = json.loads(review_path.read_bytes())
        current = json.loads(manifest.read_bytes())
        result = private.retained_custody({"review": review, "app_services": list(current["services"])}, current)
        assert result is not None
        sealed.append((review, result[1]))
        return out / "contract.json"
    monkeypatch.setattr(private, "seal", fake_seal)
    harness.repo = tmp_path
    try:
        if conflict:
            with pytest.raises(RuntimeError, match="already occupied"):
                native.prepare_and_seal(harness, p)
            assert not sealed and len([c for c in commands if c[:2] == ["docker", "compose"]]) == 1
            assert (p["gateway_native_root"] / "PREPARE_INTENT.json").exists()
            assert not (p["gateway_native_root"] / "PLAN.json").exists()
        else:
            native.prepare_and_seal(harness, p)
            assert len(sealed) == 1 and len(p["previous_containers"]) == 3
            review, info = sealed[0]
            assert info["plan"] == p["gateway_plan"] and info["plan"]["window"] == p["custody_window"]
            assert len(info["configuration"]["files"]) == 3
            assert p["custody_image_id"] == configuration.IMAGE_CONFIG_ID != configuration.IMAGE_ID
            assert info["proof"]["container_identity"]["image_id"] == configuration.IMAGE_CONFIG_ID
            assert info["plan"]["old_gateway_identity"]["image_id"] == configuration.IMAGE_CONFIG_ID
            assert info["declaration"]["ports"] == private.runtime_topology(rows)[native.OLD_SERVICE][0]["ports"]
            assert all(path.read_bytes() == data[container_path] for container_path, path in
                ((container_path, Path(record["path"])) for container_path, record in info["proof"]["archives"].items()))
            assert set(review["existing_topology"]) == {"previous-ai-filter-api", "previous-ai-filter-worker", native.OLD_SERVICE}
            assert all(row["Config"]["Labels"][LABEL] == harness.owner for row in rows)
            assert (p["gateway_native_root"] / "SEALED_TOPOLOGY.json").exists()
    finally:
        p["anchor"].close()


@pytest.mark.parametrize("fault", [None, "exit", "oom", "created", "paused", "dead", "restarting", "unfinished", "invalid_time", "identity"])
@pytest.mark.parametrize("exit_code", [0, 143])
def test_preserve_already_exited_rechecks_identity_and_normal_completion(tmp_path, monkeypatch, fault, exit_code):
    row = {"Id": "a" * 64, "Name": "/owned", "Image": "sha256:" + "b" * 64,
           "Config": {}, "HostConfig": {}, "Mounts": [], "RestartCount": 0,
           "State": {"Running": False, "Status": "exited", "ExitCode": exit_code,
                     "StartedAt": "original-start", "FinishedAt": "2026-10-31T13:00:00.123456789Z"}}
    if fault == "exit":
        row["State"]["ExitCode"] = 137
    elif fault == "created":
        row["State"]["Status"] = "created"
    elif fault == "unfinished":
        row["State"]["FinishedAt"] = "0001-01-01T00:00:00Z"
    elif fault == "invalid_time":
        row["State"]["FinishedAt"] = "not-a-time"
    elif fault not in {None, "identity"}:
        row["State"][{"oom": "OOMKilled", "paused": "Paused", "dead": "Dead", "restarting": "Restarting"}[fault]] = True
    actual = deepcopy(row)
    if fault == "identity":
        actual["Image"] = "sha256:" + "c" * 64
    p = {"directory": tmp_path, "project": "owned", "paths": {"project": tmp_path},
         "previous_containers": {row["Id"]: {}}}
    monkeypatch.setattr(native.private, "inspect_project", lambda _contract: [deepcopy(row)])
    calls = []
    def command(args, **kwargs):
        calls.append(args)
        assert args[0] in {"inspect", "logs"}
        return json.dumps([actual]) if args[0] == "inspect" else ""
    harness = SimpleNamespace(command=command, safe_inspect=deepcopy, wait=lambda *_a, **_k: pytest.fail("already exited; no polling"))
    if fault is None:
        native.preserve(harness, p)
        assert len(list((tmp_path / "custody-final").glob("*.stopped.json"))) == 1
    else:
        with pytest.raises(gate.AdmissionError, match="identity changed" if fault == "identity" else "normal owned"):
            native.preserve(harness, p)
        assert not list((tmp_path / "custody-final").glob("*.stopped.json"))
    assert sum(args[0] == "inspect" for args in calls) == 1
    assert not any(args[0] in {"kill", "rm", "stop", "prune"} for args in calls)


def test_scenario_precompose_failure_is_not_replaced_by_empty_project_cleanup(tmp_path, monkeypatch):
    """The first native failure remains observable; empty project never passes."""
    calls = []
    primary = gate.AdmissionError("original fixture preparation failed")
    p = {"directory": tmp_path, "strategy": "synthetic-strategy", "project": "synthetic-project",
         "paths": {"project": tmp_path}, "anchor": SimpleNamespace(close=lambda: calls.append("anchor"))}
    harness = SimpleNamespace(repo=tmp_path, network="synthetic-network", python_id="synthetic-python",
                              stage=lambda name: calls.append(name), launch_sender=lambda: calls.append("sender"))
    monkeypatch.setattr(native, "require_private_host", lambda _harness: None)
    monkeypatch.setattr(native.private, "source_identity", lambda _repo: {})
    monkeypatch.setattr(native.fixture, "prepare_disk", lambda *_a: p)
    monkeypatch.setattr(activation, "closed_initialization", lambda *_a: object())
    def fail_prepare(*_a):
        raise primary
    def missing_project(_contract):
        calls.append("inspect_missing_project")
        raise native.private.ContractError("existing Compose project is missing")
    monkeypatch.setattr(native, "prepare_and_seal", fail_prepare)
    monkeypatch.setattr(native.private, "inspect_project", missing_project)
    with pytest.raises(gate.AdmissionError) as caught:
        native.scenario(harness, tmp_path)
    assert caught.value is primary
    assert calls[-2:] == ["inspect_missing_project", "anchor"]
    assert len(primary.__notes__) == 1 and "ContractError" in primary.__notes__[0]
    assert "existing Compose project is missing" not in primary.__notes__[0]
    assert list((tmp_path / "custody-final").iterdir()) == []


@pytest.mark.parametrize("first_failure", ["gateway", "project", "anchor", "daemon", None])
@pytest.mark.parametrize("has_primary", [False, True])
def test_each_finalizer_runs_once_and_cannot_mask_first_failure(tmp_path, monkeypatch, first_failure, has_primary):
    calls = []
    failures = {label: RuntimeError("secret-token raw-argv /private/raw-root " + label)
                for label in ("gateway", "project", "anchor", "daemon")}
    def invoke(label):
        calls.append(label)
        if label == first_failure:
            raise failures[label]
    p = {"gateway_native_root": tmp_path, "anchor": SimpleNamespace(close=lambda: invoke("anchor"))}
    monkeypatch.setattr(native.ordinary_gateway, "preserve", lambda *_a: invoke("gateway"))
    monkeypatch.setattr(native, "preserve", lambda *_a: invoke("project"))
    daemon = SimpleNamespace(terminate=lambda: invoke("daemon"), wait=lambda **_k: calls.append("wait"))
    primary = ValueError("original operation") if has_primary else None
    if not has_primary and first_failure is not None:
        with pytest.raises(RuntimeError) as caught:
            native._preserve_scenario(object(), p, daemon, primary)
        assert caught.value is failures[first_failure]
    else:
        native._preserve_scenario(object(), p, daemon, primary)
    assert calls[:4] == ["gateway", "project", "anchor", "daemon"]
    assert calls.count("wait") == (0 if first_failure == "daemon" else 1)
    if has_primary and first_failure:
        notes = primary.__notes__
        assert len(notes) == 1 and first_failure in notes[0]
        assert native.hashlib.sha256(str(failures[first_failure]).encode()).hexdigest() in notes[0]
        assert all(value not in notes[0] for value in ("secret-token", "raw-argv", "/private/raw-root"))


def test_multiple_cleanup_failures_still_block_and_preserve_all_first_failure_notes(tmp_path, monkeypatch):
    errors = [gate.AdmissionError("gateway unknown"), native.private.ContractError("project unknown")]
    calls = []
    def reject(index):
        calls.append(index)
        raise errors[index]
    monkeypatch.setattr(native.ordinary_gateway, "preserve", lambda *_a: reject(0))
    monkeypatch.setattr(native, "preserve", lambda *_a: reject(1))
    p = {"gateway_native_root": tmp_path, "anchor": SimpleNamespace(close=lambda: calls.append("anchor"))}
    with pytest.raises(gate.AdmissionError) as caught:
        native._preserve_scenario(object(), p, None, None)
    assert caught.value is errors[0] and calls == [0, 1, "anchor"]
    assert len(errors[0].__notes__) == 1 and "ContractError" in errors[0].__notes__[0]


def test_finalizers_keep_original_total_deadline_without_a_new_window(monkeypatch):
    states, waits = [], []
    monkeypatch.setattr(native, "preserve", lambda *_a: states.append(native.native_deadline.STATE.get()))
    p = {"anchor": SimpleNamespace(close=lambda: states.append(native.native_deadline.STATE.get()))}
    daemon = SimpleNamespace(terminate=lambda: None, wait=lambda **kwargs: waits.append(kwargs["timeout"]))
    now = time.monotonic()
    with native.native_deadline.scope(now + 0.2, now + 1.5):
        before = native.native_deadline.STATE.get()
        native._preserve_scenario(object(), p, daemon, None)
        assert native.native_deadline.STATE.get() == before
    assert states == [(now + 1.5, now + 1.5)] * 2
    assert len(waits) == 1 and 0 < waits[0] <= 1.5
