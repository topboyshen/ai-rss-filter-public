"""Offline boundary tests, not Linux namespace/Docker execution evidence."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import subprocess
import time

import pytest

from scripts import production_host_ingress_native as native


@pytest.fixture
def plan():
    return {"owner": "a" * 32, "cgroup": "/ai-filter-host-" + "a" * 32,
            "image": {"Id": "sha256:" + "b" * 64}}


@pytest.fixture
def image():
    return {"Id": "sha256:" + "b" * 64, "Architecture": "amd64", "Os": "linux", "Variant": "",
            "Config": {"Entrypoint": ["/docker-entrypoint.sh"], "Cmd": ["nginx", "-g", "daemon off;"]},
            "RootFS": {"Type": "layers", "Layers": ["sha256:" + "c" * 64]},
            "RepoDigests": ["nginx@" + native.gateway.NGINX_REF.split("@", 1)[1]]}


def test_attested_load_preserves_exact_config_diffids_not_repo_digest(image):
    binding = native.image_binding(image, outer=True)
    loaded = deepcopy(image)
    loaded["RepoDigests"] = []
    assert native.image_binding(loaded, outer=False) == binding
    with pytest.raises(native.ProofError, match="RepoDigest"):
        native.image_binding(loaded, outer=True)


@pytest.mark.parametrize("field,value", [("Architecture", "arm64"), ("Os", "windows"), ("Id", "mutable:tag"),
                                        ("Config", None), ("RootFS", {"Type": "layers", "Layers": []})])
def test_bad_actual_image_fails(image, field, value):
    image[field] = value
    with pytest.raises(native.ProofError):
        native.image_binding(image, outer=True)


@pytest.mark.parametrize("field,value", [("Config", {"Cmd": ["wrong"]}), ("Variant", "v7"),
                                        ("RootFS", {"Type": "layers", "Layers": ["sha256:" + "d" * 64]})])
def test_complete_image_binding_changes_when_loaded_content_changes(image, field, value):
    before = native.image_binding(image, outer=True)
    image[field] = value
    assert native.image_binding(image, outer=False) != before


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), 100, 99, 281, 700, "200", True])
def test_deadline_cannot_open_new_or_invalid_window(value):
    with pytest.raises(native.ProofError):
        native.bounded_deadline(value, now=100)


def test_deadline_only_tightens_original_package():
    assert native.bounded_deadline(280, now=100) == 280
    assert native.bounded_deadline(105, now=100) == 105


def test_archive_same_fd_identity_and_hash(tmp_path):
    path = tmp_path / "image.tar"
    path.write_bytes(b"actual immutable transfer")
    expected = native.file_proof(path)
    fd = os.open(path, os.O_RDONLY)
    try:
        assert native.file_proof(path, fd=fd) == expected
        assert os.read(fd, 6) == b"actual"
        path.rename(tmp_path / "retained.tar")
        path.write_bytes(b"actual immutable transfer")
        with pytest.raises(native.ProofError, match="identity"):
            native.file_proof(path, fd=fd)
    finally:
        os.close(fd)
    assert (tmp_path / "retained.tar").read_bytes() == b"actual immutable transfer"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "size"])
def test_archive_no_follow_no_unknown_hardlink_no_unbounded_input(tmp_path, kind):
    path = tmp_path / "image.tar"
    path.write_bytes(b"original")
    if kind == "symlink":
        path.rename(tmp_path / "original")
        path.symlink_to(tmp_path / "original")
    elif kind == "hardlink":
        os.link(path, tmp_path / "alias")
    with pytest.raises((OSError, native.ProofError)):
        native.file_proof(path, limit=1 if kind == "size" else 100)


def test_daemon_config_has_only_private_state_and_charged_driver(tmp_path, plan):
    config = native.daemon_config(tmp_path, plan)
    assert config["hosts"] == ["unix:///var/run/docker.sock"]
    assert config["exec-opts"] == ["native.cgroupdriver=cgroupfs"]
    assert config["cgroup-parent"] == plan["cgroup"] + "/containers"
    assert config["bridge"] == "none" and config["storage-driver"] == "vfs"
    assert config["dns"] == ["127.0.0.1"] and config["live-restore"] is True
    for key in ("data-root", "exec-root", "pidfile"):
        assert Path(config[key]).parent == tmp_path
    assert "containerd" not in config  # Dedicated managed containerd; never host socket.


def test_container_remains_nonprivileged_and_below_parent_limits(tmp_path, plan):
    args = native.container_args(plan, tmp_path, "owned", 35073)
    assert "--privileged" not in args and "--cap-add" not in args
    assert args[args.index("--memory") + 1] == args[args.index("--memory-swap") + 1] == str(128 * 1024**2)
    assert args[args.index("--pids-limit") + 1] == "64" and args[args.index("--cpus") + 1] == "1"
    assert args[args.index("--cgroup-parent") + 1] == plan["cgroup"] + "/containers"
    assert args[args.index("--dns") + 1] == "127.0.0.1"
    assert args[args.index("--publish") + 1] == "127.0.0.1:35073:5000/tcp"
    assert args[-3:] == [plan["image"]["Id"], "-g", "daemon off;"]


def fixture_container(plan):
    return {"Id": "d" * 64, "Image": plan["image"]["Id"], "Config": {"Labels": {native.LABEL: plan["owner"]}},
            "RestartCount": 0, "State": {"Running": True, "Pid": 123, "Status": "running"},
            "HostConfig": {"Memory": 128 * 1024**2, "MemorySwap": 128 * 1024**2, "PidsLimit": 64,
                           "NanoCpus": 1_000_000_000, "CgroupParent": plan["cgroup"] + "/containers",
                           "Privileged": False, "CapAdd": [], "RestartPolicy": {"Name": "no"},
                           "SecurityOpt": ["no-new-privileges:true"], "Dns": ["127.0.0.1"],
                           "PortBindings": {"5000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "35073"}]}},
            "NetworkSettings": {"Networks": {"owned": {}}, "Ports": {"80/tcp": None,
                                "5000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "35073"}]}}}


def test_actual_ports_and_pid_cgroup_are_both_required(plan, monkeypatch):
    row = fixture_container(plan)
    monkeypatch.setattr(native, "proc_identity", lambda pid: {"cgroup": plan["cgroup"] + "/containers/owned"})
    native.verify_container(row, plan, "owned", 35073, identifier=row["Id"])
    row["NetworkSettings"]["Ports"] = {}
    with pytest.raises(native.ProofError, match="actual loopback"):
        native.verify_container(row, plan, "owned", 35073)


@pytest.mark.parametrize("fault", ["resource", "parent", "process", "network", "identity", "privilege", "dns", "state", "port"])
def test_actual_execution_drift_refused(plan, monkeypatch, fault):
    row = fixture_container(plan)
    monkeypatch.setattr(native, "proc_identity", lambda pid: {
        "cgroup": "/outside" if fault == "process" else plan["cgroup"] + "/containers/owned"})
    if fault == "resource":
        row["HostConfig"]["Memory"] += 1
    elif fault == "parent":
        row["HostConfig"]["CgroupParent"] = "/outside"
    elif fault == "network":
        row["NetworkSettings"]["Networks"]["other"] = {}
    elif fault == "identity":
        row["Config"]["Labels"][native.LABEL] = "wrong"
    elif fault == "privilege":
        row["HostConfig"]["Privileged"] = True
    elif fault == "dns":
        row["HostConfig"]["Dns"] = []
    elif fault == "state":
        row["State"]["Paused"] = True
    elif fault == "port":
        row["HostConfig"]["PortBindings"] = {}
    with pytest.raises(native.ProofError):
        native.verify_container(row, plan, "owned", 35073)


@pytest.mark.parametrize("host,port", [("0.0.0.0", 35073), ("localhost", 35073), ("192.0.2.1", 35073),
                                      ("127.0.0.1", 35074), ("127.0.0.1", True)])
def test_illegal_http_target_rejected_before_real_transport_io(monkeypatch, host, port):
    monkeypatch.setattr(native.gateway.NativeTransport, "health", lambda *_: pytest.fail("must not reach HTTP"))
    with pytest.raises(native.ProofError):
        native.guarded_health(native.gateway.NativeTransport(), 35073, host, port, "e" * 64)


def test_exact_real_transport_selected_not_duck_type(monkeypatch):
    monkeypatch.setattr(native.gateway.NativeTransport, "health", lambda *_: {"actual": "called"})
    assert native.guarded_health(native.gateway.NativeTransport(), 35073, "127.0.0.1", 35073, "e" * 64) == {"actual": "called"}
    with pytest.raises(native.ProofError):
        native.guarded_health(SimpleNamespace(health=lambda *_: {}), 35073, "127.0.0.1", 35073, "e" * 64)


class NetworkCommands:
    def __init__(self, links, routes):
        self.links, self.routes = links, routes

    def run(self, args):
        return json.dumps(self.links if "link" in args else self.routes).encode()


@pytest.mark.parametrize("fault", ["external_link", "default", "gateway", "unknown_dev"])
def test_no_external_interfaces_or_routes(fault, namespace_stat):
    links = [{"ifname": "lo"}, {"ifname": "owned", "linkinfo": {"info_kind": "bridge"}},
             {"ifname": "veth1", "master": "owned", "linkinfo": {"info_kind": "veth"}}]
    routes = [{"dst": "172.20.0.0/16", "dev": "owned"}]
    if fault == "external_link":
        links.append({"ifname": "eth0"})
    elif fault == "default":
        routes[0]["dst"] = "default"
    elif fault == "gateway":
        routes[0]["gateway"] = "10.0.0.1"
    else:
        routes[0]["dev"] = "eth0"
    with pytest.raises(native.ProofError):
        native.namespace_snapshot(NetworkCommands(links, routes), bridge="owned")


def test_namespace_local_bridge_only(monkeypatch):
    monkeypatch.setattr(native.os, "stat", lambda path: SimpleNamespace(st_ino=123))
    result = native.namespace_snapshot(NetworkCommands([{"ifname": "lo"}], [{"dst": "127.0.0.0/8", "dev": "lo"}]))
    assert result["netns"] == 123


class FamilyNetworkCommands:
    def __init__(self, links, ipv4=(), ipv6=()):
        self.outputs = {"links": links, "-4": list(ipv4), "-6": list(ipv6)}
        self.calls = []

    def run(self, argv):
        self.calls.append(argv)
        key = "links" if "link" in argv else "-4" if "-4" in argv else "-6"
        value = self.outputs[key]
        return value if isinstance(value, bytes) else json.dumps(value).encode()


@pytest.fixture
def namespace_stat(monkeypatch):
    original = native.os.stat
    def actual(path, *args, **kwargs):
        return SimpleNamespace(st_ino=123) if str(path) == "/proc/self/ns/net" else original(path, *args, **kwargs)
    monkeypatch.setattr(native.os, "stat", actual)


def namespace_fixture_links():
    return [{"ifname": "lo", "ifindex": 1},
            {"ifname": "afh" + "a" * 10, "ifindex": 2, "linkinfo": {"info_kind": "bridge"}},
            {"ifname": "veth1234", "ifindex": 3, "master": "afh" + "a" * 10, "linkinfo": {"info_kind": "veth"}}]


@pytest.mark.parametrize("row", [
    {"dst": "default", "dev": "afh" + "a" * 10},
    {"dst": "::/0", "dev": "afh" + "a" * 10},
    {"dst": "fe80::/64", "dev": "veth1234"},
    {"dst": "fe80::/64", "dev": "afh" + "a" * 10, "gateway": "fe80::1"},
    {"dst": "10.20.0.0/16", "dev": "unknown-sensitive-interface"},
])
def test_rejected_route_keeps_exact_original_without_relaxing_guard(tmp_path, namespace_stat, row):
    path = tmp_path / "NAMESPACE_AFTER_OBSERVATION.json"
    commands = FamilyNetworkCommands(namespace_fixture_links(), ipv6=[row])
    with pytest.raises(native.ProofError, match="external namespace route"):
        native.namespace_snapshot(commands, bridge="afh" + "a" * 10, evidence_path=path)
    raw = json.loads(path.read_bytes())
    assert raw["status"] == "rejected" and raw["routes_by_family"]["ipv6"] == [row]
    assert raw["violation"] == {"reason": "external_route", "section": "ipv6", "index": 0}
    assert raw["components"]["ipv6"]["sha256"] == hashlib.sha256(json.dumps([row]).encode()).hexdigest()
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(commands.calls) == 3
    profile = native.profile_for(path)
    text = json.dumps(profile)
    assert "10.20." not in text and "fe80::" not in text and "sensitive" not in text
    assert profile["violation"] == raw["violation"]
    assert profile["routes_by_family"]["ipv6"][0]["gateway_present"] == bool(row.get("gateway"))
    if row.get("dev") == "veth1234":
        assert profile["routes_by_family"]["ipv6"][0]["device_role"] == "owned_veth"
        assert profile["routes_by_family"]["ipv6"][0]["destination_class"] == "link_local"


def test_valid_namespace_has_independent_original_and_success_snapshot(tmp_path, namespace_stat):
    path = tmp_path / "NAMESPACE_AFTER_OBSERVATION.json"
    bridge = "afh" + "a" * 10
    ipv4 = [{"dst": "127.0.0.0/8", "dev": "lo"}, {"dst": "172.20.0.0/16", "dev": bridge}]
    ipv6 = [{"dst": "::1/128", "dev": "lo"}]
    result = native.namespace_snapshot(FamilyNetworkCommands(namespace_fixture_links(), ipv4, ipv6),
                                       bridge=bridge, evidence_path=path)
    assert result["routes"] == ipv4 + ipv6 and result["netns"] == 123
    original = json.loads(path.read_bytes())
    assert original["status"] == "validated" and original["violation"] is None
    assert native.profile_for(path)["status"] == "validated"


def test_namespace_identity_drift_retains_evidence_and_cannot_validate(tmp_path, monkeypatch):
    original, seen = native.os.stat, []
    def changed(path, *args, **kwargs):
        if str(path) != "/proc/self/ns/net":
            return original(path, *args, **kwargs)
        seen.append(path)
        return SimpleNamespace(st_ino=123 if len(seen) == 1 else 124)
    monkeypatch.setattr(native.os, "stat", changed)
    path = tmp_path / "NAMESPACE_AFTER_OBSERVATION.json"
    with pytest.raises(native.ProofError, match="namespace identity changed"):
        native.namespace_snapshot(FamilyNetworkCommands([{"ifname": "lo"}]), evidence_path=path)
    profile = native.profile_for(path)
    assert profile["status"] == "rejected" and profile["netns"] == 123 and profile["netns_after"] == 124
    assert profile["violation"] == {"reason": "namespace_identity", "section": "namespace", "index": None}


def test_parse_failure_retains_partial_capture_without_exporting_error_text(tmp_path, namespace_stat):
    path = tmp_path / "NAMESPACE_AFTER_OBSERVATION.json"
    commands = FamilyNetworkCommands(namespace_fixture_links())
    commands.outputs["-4"] = b'{"secret-private-input":'
    with pytest.raises(json.JSONDecodeError):
        native.namespace_snapshot(commands, bridge="afh" + "a" * 10, evidence_path=path)
    original = json.loads(path.read_bytes())
    assert original["status"] == "capture_incomplete" and original["error_type"] == "JSONDecodeError"
    assert len(commands.calls) == 2  # Stop on the failed read/parse; never query the next component.
    assert original["components"]["ipv4"]["sha256"] == hashlib.sha256(commands.outputs["-4"]).hexdigest()
    profile = native.profile_for(path)
    assert profile["capture_error_type"] == "JSONDecodeError"
    assert "secret-private-input" not in json.dumps(profile)


def test_capture_timeout_preserves_diagnostic_and_does_not_query_remaining_family(tmp_path, namespace_stat):
    path = tmp_path / "NAMESPACE_AFTER_OBSERVATION.json"
    class Commands(FamilyNetworkCommands):
        def run(self, argv):
            if "-4" in argv:
                self.calls.append(argv)
                raise TimeoutError("private timeout detail must not escape")
            return super().run(argv)
    commands = Commands(namespace_fixture_links())
    with pytest.raises(TimeoutError):
        native.namespace_snapshot(commands, bridge="afh" + "a" * 10, evidence_path=path)
    assert len(commands.calls) == 2
    profile = native.profile_for(path)
    assert profile["status"] == "capture_incomplete" and profile["capture_error_type"] == "TimeoutError"
    assert "private timeout" not in json.dumps(profile)


@pytest.mark.parametrize("capture", ["timeout", "json"])
@pytest.mark.parametrize("evidence", ["fsync", "size"])
def test_capture_error_remains_primary_when_diagnostic_preservation_also_fails(
        tmp_path, namespace_stat, monkeypatch, capture, evidence):
    commands = FamilyNetworkCommands(namespace_fixture_links())
    if capture == "json":
        commands.outputs["-4"] = b'{"private-broken-json":'
        expected = json.JSONDecodeError
    else:
        original = commands.run
        def timeout(argv):
            if "-4" in argv:
                commands.calls.append(argv)
                raise TimeoutError("original private capture failure")
            return original(argv)
        monkeypatch.setattr(commands, "run", timeout)
        expected = TimeoutError
    diagnostic = OSError("synthetic fsync failed")
    if evidence == "fsync":
        monkeypatch.setattr(native, "durable", lambda *_: (_ for _ in ()).throw(diagnostic))
    else:
        monkeypatch.setattr(native, "encoded", lambda *_: b"x" * (native.OUTPUT_BYTES + 1))
    path = tmp_path / "NAMESPACE_AFTER_OBSERVATION.json"
    with pytest.raises(expected) as actual:
        native.namespace_snapshot(commands, bridge="afh" + "a" * 10, evidence_path=path)
    assert len(commands.calls) == 2 and not path.exists()
    assert any("observation preservation failed" in note for note in actual.value.__notes__)
    assert isinstance(actual.value.__cause__, OSError if evidence == "fsync" else native.ProofError)
    if evidence == "fsync":
        assert actual.value.__cause__ is diagnostic


def test_policy_rejection_remains_primary_if_its_diagnostic_fsync_fails(tmp_path, namespace_stat, monkeypatch):
    commands = FamilyNetworkCommands(namespace_fixture_links(), ipv6=[{"dst": "fe80::/64", "dev": "veth1234"}])
    diagnostic = OSError("synthetic fsync failure")
    monkeypatch.setattr(native, "durable", lambda *_: (_ for _ in ()).throw(diagnostic))
    with pytest.raises(native.ProofError, match="external namespace route") as actual:
        native.namespace_snapshot(commands, bridge="afh" + "a" * 10,
                                  evidence_path=tmp_path / "NAMESPACE_AFTER_OBSERVATION.json")
    assert actual.value.__cause__ is diagnostic and len(commands.calls) == 3


def test_rejected_diagnostic_does_not_make_success_packet_complete(tmp_path, namespace_stat):
    root = tmp_path / "inner"
    root.mkdir()
    path = root / "NAMESPACE_AFTER_OBSERVATION.json"
    with pytest.raises(native.ProofError):
        native.namespace_snapshot(FamilyNetworkCommands(namespace_fixture_links(), ipv6=[{"dst": "fe80::/64", "dev": "veth1234"}]),
                                  bridge="afh" + "a" * 10, evidence_path=path)
    packet = native.evidence_packet(tmp_path)
    assert packet["complete"] is False
    assert packet["profiles"]["inner/NAMESPACE_AFTER_OBSERVATION.json"]["violation"]["reason"] == "external_route"
    assert packet["originals"]["inner/NAMESPACE_AFTER_OBSERVATION.json"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_profile_export_cannot_mix_original_hash_with_replaced_profile(tmp_path, monkeypatch):
    (tmp_path / "inner").mkdir()
    path = tmp_path / "inner/NAMESPACE_AFTER_OBSERVATION.json"
    native.durable(path, {"status": "capture_incomplete"})
    original = native.profile_for
    def swapped(candidate):
        profile = original(candidate)
        candidate.rename(candidate.with_suffix(".retained"))
        native.durable(candidate, {"status": "validated"})
        return profile
    monkeypatch.setattr(native, "profile_for", swapped)
    with pytest.raises(native.ProofError, match="original changed during profile export"):
        native.evidence_packet(tmp_path)
    assert path.with_suffix(".retained").read_bytes() == native.encoded({"status": "capture_incomplete"})


@pytest.mark.parametrize("kind", ["existing", "symlink"])
def test_namespace_evidence_cannot_replace_or_follow_existing_original(tmp_path, namespace_stat, kind):
    original = tmp_path / "private-original"
    original.write_bytes(b"must remain unchanged")
    path = tmp_path / "NAMESPACE_AFTER_OBSERVATION.json"
    if kind == "symlink":
        path.symlink_to(original)
    else:
        path.write_bytes(b"previous evidence")
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        native.namespace_snapshot(FamilyNetworkCommands([{"ifname": "lo"}]), evidence_path=path)
    assert path.read_bytes() == before and original.read_bytes() == b"must remain unchanged"


@pytest.mark.parametrize("links,routes,reason", [
    ([{"ifname": "eth0"}], [], "external_interface"),
    ([{"ifname": "lo"}] * 9, [], "interface_bound"),
    ([{"ifname": "lo"}], [{"dst": "127.0.0.0/8", "dev": "lo"}] * 33, "route_bound"),
    ([{"ifname": "lo"}], ["unexpected-value"], "route_shape"),
    ([{"ifname": "lo"}], [{"dst": ["private-invalid"], "dev": "lo"}], "route_shape"),
])
def test_invalid_namespace_shape_is_preserved_as_rejected(tmp_path, namespace_stat, links, routes, reason):
    path = tmp_path / "NAMESPACE_AFTER_OBSERVATION.json"
    with pytest.raises(native.ProofError):
        native.namespace_snapshot(FamilyNetworkCommands(links, routes), evidence_path=path)
    original = json.loads(path.read_bytes())
    assert original["status"] == "rejected" and original["violation"]["reason"] == reason
    assert native.profile_for(path)["violation"]["reason"] == reason


@pytest.fixture
def stopping(tmp_path, plan, monkeypatch):
    row = fixture_container(plan)
    row["State"]["Status"] = "running"
    state = SimpleNamespace(calls=[], daemon_term=0, fault=None, row=row)
    class Commands:
        deadline = native.time.monotonic() + 10
        uncertain_mutation = False

        def docker(self, args):
            state.calls.append(args)
            if args[:2] == ["ps", "--all"]:
                return row["Id"]
            if args[0] == "logs":
                return "retained"
            if args[0] == "kill":
                if state.fault == "ack":
                    raise TimeoutError("TERM result unknown")
                row["State"].update(Running=False, Status="exited", ExitCode=0)
            if args[0] == "ps":
                return row["Id"] if state.fault == "live" else ""
            return ""

        def inspect(self, identifier):
            assert identifier == row["Id"]
            return deepcopy(row)

    class Daemon:
        pid, returncode = 300, 0

        def send_signal(self, value):
            assert value == native.signal.SIGTERM
            state.daemon_term += 1

        def poll(self):
            return 0

    monkeypatch.setattr(native, "check_peer", lambda *_: None)
    monkeypatch.setattr(native, "resource_snapshot", lambda *_: {"memory.peak": "42"})
    logs = SimpleNamespace(finish=lambda *_: None)
    return SimpleNamespace(commands=Commands(), daemon=Daemon(), state=state, logs=logs, root=tmp_path, plan=plan)


def test_cleanup_requires_all_stopped_before_daemon_term(stopping):
    s = stopping
    assert native.stop_owned(s.commands, s.root, s.plan, s.daemon, {}, s.logs)
    assert s.state.daemon_term == 1
    assert [x for x in s.state.calls if x[0] == "kill"] == [["kill", "--signal", "TERM", s.state.row["Id"]]]
    assert not any(x[0] in {"rm", "stop", "restart", "prune"} for x in s.state.calls)
    assert json.loads((s.root / "CLEANUP.json").read_bytes())["forced_kill"] is False


@pytest.mark.parametrize("fault", ["ack", "live", "foreign", "evidence"])
def test_unknown_cleanup_keeps_daemon_and_originals(stopping, monkeypatch, fault):
    s = stopping
    s.state.fault = fault
    if fault == "foreign":
        s.state.row["Config"]["Labels"][native.LABEL] = "foreign"
    if fault == "evidence":
        monkeypatch.setattr(native, "durable", lambda *_: (_ for _ in ()).throw(OSError("synthetic fsync")))
    with pytest.raises((native.ProofError, TimeoutError, OSError)):
        native.stop_owned(s.commands, s.root, s.plan, s.daemon, {}, s.logs)
    assert s.state.daemon_term == 0 and not (s.root / "CLEANUP.json").exists()


def test_capture_failure_remains_primary_if_record_write_fails(tmp_path, monkeypatch):
    commands = native.Commands(tmp_path, native.time.monotonic() + 2)
    failure = OSError("original process spawn failure; private not exported")
    monkeypatch.setattr(native.subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(failure))
    monkeypatch.setattr(native, "durable", lambda *_: (_ for _ in ()).throw(RuntimeError("fsync failed")))
    with pytest.raises(OSError) as actual:
        commands.run(["/never"])
    assert actual.value is failure and "evidence failure" in actual.value.__notes__[0]


def test_private_evidence_is_exclusive_and_preserved(tmp_path):
    path = tmp_path / "ORIGINAL.json"
    native.durable(path, {"one": 1})
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        native.durable(path, {"two": 2})
    assert path.read_bytes() == original and path.stat().st_mode & 0o777 == 0o600


def test_main_failure_safe_packet_has_no_message_env_or_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(native, "outer_proof", lambda *_: (_ for _ in ()).throw(RuntimeError("TOKEN=private /private/path")))
    monkeypatch.setattr(native.sys, "platform", "linux")
    monkeypatch.setattr(native.os, "geteuid", lambda: 0)
    original = Path.stat
    monkeypatch.setattr(Path, "stat", lambda p, **kw: SimpleNamespace(st_uid=0) if p in {native.ROOT, Path(native.__file__)}
                        else original(p, **kw))
    assert native.main(["--repo", str(native.ROOT), "--output", str(tmp_path / "new"),
                        "--deadline-monotonic", str(native.time.monotonic() + 60)]) == 1
    output = capsys.readouterr().out
    assert "TOKEN" not in output and "/private/path" not in output
    packet = json.loads(output)
    assert packet["status"] == "unconfirmed" and packet["cleanup_confirmed"] is False
    assert packet["error_sha256"] == hashlib.sha256(b"TOKEN=private /private/path").hexdigest()


def test_cgroup_unavailable_fails_without_global_write(tmp_path, monkeypatch):
    (tmp_path / "cgroup.controllers").write_text("cpu memory pids")
    (tmp_path / "cgroup.subtree_control").write_text("memory")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    monkeypatch.setattr(native, "CGROUP", tmp_path)
    with pytest.raises(native.ProofError, match="delegation"):
        native.cgroup_setup("a" * 32)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_cgroup_empty_parent_children_and_controller_order(tmp_path, monkeypatch):
    (tmp_path / "cgroup.controllers").write_text("cpu memory pids")
    (tmp_path / "cgroup.subtree_control").write_text("cpu memory pids")
    monkeypatch.setattr(native, "CGROUP", tmp_path)
    mkdir, writes = Path.mkdir, []
    write = Path.write_text

    def create(path, *args, **kwargs):
        result = mkdir(path, *args, **kwargs)
        (path / "cgroup.procs").touch()
        (path / "cgroup.subtree_control").touch()
        return result

    def record(path, data, *args, **kwargs):
        writes.append((str(path.relative_to(tmp_path)), data))
        return write(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", create)
    monkeypatch.setattr(Path, "write_text", record)
    monkeypatch.setattr(native, "proc_identity", lambda pid: {"cgroup": "/ai-filter-host-" + "a" * 32 + "/supervisor"})
    path = native.cgroup_setup("a" * 32)
    assert path == "/ai-filter-host-" + "a" * 32
    assert (tmp_path / "cgroup.subtree_control").read_text() == "cpu memory pids"
    assert [p.rsplit("/", 1)[-1] for p, _ in writes[:4]] == ["memory.max", "memory.swap.max", "pids.max", "cpu.max"]
    assert writes[-1] == (path[1:] + "/supervisor/cgroup.procs", str(os.getpid()))
    assert (tmp_path / path[1:] / "cgroup.procs").read_text() == ""
    assert not any(p == "cgroup.subtree_control" for p, _ in writes)


def test_pipe_retainer_only_reads_exact_read_ends(tmp_path):
    read_fd, write_fd = os.pipe()
    try:
        proof = native.pipe_identity(read_fd)
        assert proof["fd"] == read_fd
        with pytest.raises(native.ProofError):
            native.pipe_identity(write_fd)
        path = tmp_path / "not-a-pipe"
        path.write_bytes(b"retained")
        with path.open("rb") as stream, pytest.raises(native.ProofError):
            native.pipe_identity(stream.fileno())
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_actual_subprocess_capture_and_single_fd_load(tmp_path):
    path = tmp_path / "image.tar"
    path.write_bytes(b"synthetic image archive original bytes")
    proof = native.file_proof(path)
    commands = native.Commands(tmp_path, native.time.monotonic() + 3)
    output = commands.run([sys.executable, "-I", "-B", "-c",
                           "import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())"],
                          input_path=path, input_proof=proof)
    assert output.decode().strip() == proof["sha256"]
    record = json.loads((tmp_path / "command-0001.json").read_bytes())
    assert record["returncode"] == 0 and record["deadline_monotonic"] <= commands.deadline
    assert native.file_proof(path) == proof


def test_bad_transfer_identity_does_not_launch_process(tmp_path, monkeypatch):
    path = tmp_path / "image.tar"
    path.write_bytes(b"original")
    commands = native.Commands(tmp_path, native.time.monotonic() + 3)
    monkeypatch.setattr(native.subprocess, "Popen", lambda *_a, **_k: pytest.fail("no process before identity match"))
    with pytest.raises(native.ProofError, match="fd differs"):
        commands.run(["docker", "load"], input_path=path, input_proof={})
    assert json.loads((tmp_path / "command-0001.json").read_bytes())["returncode"] is None


def test_command_uses_isolated_session_and_no_injected_environment(tmp_path, monkeypatch):
    commands = native.Commands(tmp_path, native.time.monotonic() + 3)
    calls = []

    def launch(args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("stop before real process")

    monkeypatch.setenv("LD_PRELOAD", "secret-injected")
    monkeypatch.setenv("PYTHONPATH", "secret-path")
    monkeypatch.setattr(native.subprocess, "Popen", launch)
    with pytest.raises(RuntimeError):
        commands.run(["fixed"])
    assert calls[0][1]["start_new_session"] is True
    assert set(calls[0][1]["env"]) == {"PATH", "LC_ALL", "DOCKER_CONFIG"}


def test_safe_packet_includes_actual_ports_and_hashes_but_not_env_or_paths(tmp_path, plan):
    (tmp_path / "inner").mkdir()
    root = tmp_path / "inner"
    row = fixture_container(plan)
    row["Config"]["Env"] = ["TOKEN=private-secret"]
    row["Args"] = ["/secret/private/path"]
    native.durable(root / "HTTP_AFTER.json", row)
    native.durable(root / "CLEANUP.json", {"all_containers_stopped": True, "daemon_returncode": 0, "removed": False, "forced_kill": False})
    native.durable(root / "RESOURCES_FINAL.json", {"memory.max": str(native.MEMORY), "memory.events": "oom 0\noom_kill 0"})
    for name in ("NAMESPACE_BEFORE.json", "NAMESPACE_AFTER.json"):
        native.durable(root / name, {"netns": 123, "links": [{"ifname": "lo", "ifindex": 1}],
                                     "routes": [{"dst": "127.0.0.0/8", "dev": "lo"}]})
        native.durable(root / name.replace(".json", "_OBSERVATION.json"), {
            "netns": 123, "status": "validated", "bridge": None,
            "links": [{"ifname": "lo", "ifindex": 1}], "violation": None,
            "routes_by_family": {"ipv4": [{"dst": "127.0.0.0/8", "dev": "lo"}], "ipv6": []}})
    native.durable(root / "DAEMON.json", {"pid": 42, "netns": 123, "cgroup": "/secret-private-cgroup"})
    native.durable(root / "PROCESSES.json", [{"pid": 42, "netns": 123, "cgroup": "/secret-private-cgroup"}])
    native.durable(root / "command-0001.stderr", b"TOKEN=private-secret /secret/private/path")
    packet = native.evidence_packet(tmp_path)
    encoded = json.dumps(packet)
    assert packet["complete"] is True and len(encoded) < 512 * 1024
    assert "private-secret" not in encoded and "/secret" not in encoded and "Env" not in encoded
    assert packet["profiles"]["inner/HTTP_AFTER.json"]["actual_ports"]["bindings"] == row["NetworkSettings"]["Ports"]
    assert packet["profiles"]["inner/NAMESPACE_AFTER.json"]["routes"] == [
        {"destination": "127.0.0.0/8", "interface": "lo", "has_gateway": False}]
    assert packet["originals"]["inner/command-0001.stderr"]["sha256"] == hashlib.sha256(
        b"TOKEN=private-secret /secret/private/path").hexdigest()
    assert packet["full_raw_archive_verified"] is False


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_export_never_follows_links_or_unknown_hardlinks(tmp_path, kind):
    (tmp_path / "inner").mkdir()
    source = tmp_path / "sensitive"
    source.write_bytes(b"TOKEN=must-not-follow")
    target = tmp_path / "inner/DAEMON.json"
    if kind == "symlink":
        target.symlink_to(source)
    else:
        os.link(source, target)
    with pytest.raises((OSError, native.ProofError)):
        native.evidence_packet(tmp_path)
    assert source.read_bytes() == b"TOKEN=must-not-follow"


def test_partial_packet_is_not_complete(tmp_path):
    (tmp_path / "inner").mkdir()
    native.durable(tmp_path / "inner/STOP.json", {"status": "unconfirmed"})
    packet = native.evidence_packet(tmp_path)
    assert packet["complete"] is False and packet["full_raw_archive_verified"] is False


def test_unknown_create_ack_never_turns_empty_inventory_into_daemon_stop(stopping):
    s = stopping
    s.commands.uncertain_mutation = True
    with pytest.raises(native.ProofError, match="unresolved mutation"):
        native.stop_owned(s.commands, s.root, s.plan, s.daemon, {}, s.logs)
    assert s.state.daemon_term == 0 and not (s.root / "CLEANUP.json").exists()


@pytest.mark.parametrize("args,uncertain", [(["create", "image"], True), (["start", "id"], True),
                                          (["network", "create", "owned"], True), (["inspect", "id"], False)])
def test_mutation_uncertainty_is_retained_without_retry(tmp_path, monkeypatch, args, uncertain):
    commands, calls = native.Commands(tmp_path, native.time.monotonic() + 3), []
    error = TimeoutError("original response missing")

    def fail(argv, **kwargs):
        calls.append(argv)
        raise error

    monkeypatch.setattr(commands, "run", fail)
    with pytest.raises(TimeoutError) as actual:
        commands.docker(args)
    assert actual.value is error and commands.uncertain_mutation is uncertain
    assert calls == [[*native.DOCKER, *args]]


def test_real_retainer_holds_only_pipe_read_ends_until_writer_eof(tmp_path):
    pairs = [os.pipe(), os.pipe()]
    record = {"schema": 1, "pipes": [native.pipe_identity(pair[0]) for pair in pairs],
              "script": native.file_proof(Path(native.__file__)), "cgroup": "/owned", "daemon": {"netns": 123}}
    native.durable(tmp_path / "RETAIN.json", record)
    code = """import os,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from scripts import production_host_ingress_native as m
m.private_directory=lambda path: None
m.proc_identity=lambda pid: {'pid':pid,'starttime':1,'netns':123,'cgroup':'/owned/supervisor'}
raise SystemExit(m.retain_pipes(Path(sys.argv[2])))
"""
    process = subprocess.Popen([sys.executable, "-I", "-B", "-c", code, str(native.ROOT), str(tmp_path)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               pass_fds=tuple(pair[0] for pair in pairs), start_new_session=True)
    try:
        end = time.monotonic() + 3
        while not (tmp_path / "RETAINER_READY.json").exists() and time.monotonic() < end:
            assert process.poll() is None
            time.sleep(0.01)
        assert (tmp_path / "RETAINER_READY.json").exists()
        for read_fd, write_fd in pairs:
            os.close(read_fd)
            assert os.write(write_fd, b"bounded retained data") == len(b"bounded retained data")
        assert process.poll() is None
        for _read_fd, write_fd in pairs:
            os.close(write_fd)
        assert process.wait(timeout=2) == 0
        assert process.stdout.read() == process.stderr.read() == b""
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)
        for pair in pairs:
            for fd in pair:
                try:
                    os.close(fd)
                except OSError:
                    pass
        process.stdout.close()
        process.stderr.close()


def test_fixed_tool_version_argv_respects_iproute_case(tmp_path, monkeypatch):
    paths = {name: tmp_path / name for name in native.TOOLS}
    for path in paths.values():
        path.write_bytes(b"synthetic public executable")
    monkeypatch.setattr(native, "TOOLS", paths)
    original = Path.stat

    def info(path, **kwargs):
        value = original(path, **kwargs)
        return SimpleNamespace(st_uid=0, st_mode=value.st_mode) if path in paths.values() else value

    monkeypatch.setattr(Path, "stat", info)
    monkeypatch.setattr(native, "file_proof", lambda path: {"sha256": "a" * 64})
    calls = []
    native.host_tools(tmp_path, SimpleNamespace(run=lambda argv: calls.append(argv)))
    assert [str(paths["ip"]), "-Version"] in calls
    assert all(argv[1] == "--version" for argv in calls if argv[0] != str(paths["ip"]))


@pytest.mark.parametrize("fault", [None, "third_netns", "other_container", "daemon_escape", "cgroup_escape"])
def test_actual_container_master_and_workers_need_exact_subtree_and_netns(plan, monkeypatch, fault):
    parent = plan["cgroup"]
    rows = {
        1: {"pid": 1, "ppid": 0, "starttime": 10, "netns": 100, "cgroup": parent + "/supervisor"},
        2: {"pid": 2, "ppid": 1, "starttime": 11, "netns": 200, "cgroup": parent + "/supervisor"},
        3: {"pid": 3, "ppid": 2, "starttime": 12, "netns": 300, "cgroup": parent + "/containers/id"},
        4: {"pid": 4, "ppid": 3, "starttime": 13, "netns": 300, "cgroup": parent + "/containers/id"},
    }
    plan["parent"] = rows[1]
    if fault == "third_netns":
        rows[4]["netns"] = 400
    elif fault == "other_container":
        rows[4]["cgroup"] = parent + "/containers/other"
    elif fault == "daemon_escape":
        rows[2]["netns"] = 400
    elif fault == "cgroup_escape":
        rows[4]["cgroup"] = "/outside"

    class Entries:
        def __enter__(self):
            return iter(SimpleNamespace(name=str(pid)) for pid in rows)

        def __exit__(self, *_):
            pass

    monkeypatch.setattr(native.os, "scandir", lambda path: Entries())
    monkeypatch.setattr(native.os, "getpid", lambda: 2)
    monkeypatch.setattr(native, "proc_identity", lambda pid: deepcopy(rows[pid]))
    if fault:
        with pytest.raises(native.ProofError):
            native.process_snapshot(plan, 200, container_pid=3)
    else:
        actual = native.process_snapshot(plan, 200, container_pid=3)
        assert {row["pid"] for row in actual} == {1, 2, 3, 4}


@pytest.mark.parametrize("fault", [None, "exited_after_ready", "identity", "read_end_closed"])
def test_retainer_ready_file_is_not_live_fd_proof(monkeypatch, fault):
    pairs = [os.pipe(), os.pipe()]
    streams = [os.fdopen(pair[0], "rb") for pair in pairs]
    process = SimpleNamespace(pid=123, stdout=streams[0], stderr=streams[1])
    actual = {"pid": 456, "starttime": 7, "netns": 8, "mntns": 9, "cgroup": "/owned/supervisor"}
    expected = {key: actual[key] for key in ("pid", "starttime", "netns", "mntns")}
    expected["cgroup_sha256"] = hashlib.sha256(actual["cgroup"].encode()).hexdigest()
    child = SimpleNamespace(pid=456, poll=lambda: 1 if fault == "exited_after_ready" else None)
    monkeypatch.setattr(native, "RETAINERS", {123: (child, expected)})
    monkeypatch.setattr(native, "proc_identity", lambda pid: {**actual, "starttime": 99} if fault == "identity" else actual)

    def lookup(path):
        if fault == "read_end_closed":
            raise FileNotFoundError("retainer original descriptor no longer exists")
        return os.fstat(int(path.name))

    monkeypatch.setattr(native.os, "stat", lookup)
    try:
        assert native.retainer_confirmed(process) is (fault is None)
    finally:
        for stream in streams:
            stream.close()
        for _read_fd, write_fd in pairs:
            os.close(write_fd)


@pytest.mark.parametrize("broken_stdout", [False, True])
def test_live_daemon_failure_always_keeps_original_holder_even_if_print_fails(monkeypatch, broken_stdout):
    process = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(native, "LIVE_DAEMONS", [(process, None, None, None)])
    calls = []

    def hold():
        calls.append("held")
        raise InterruptedError("test ends passive retention without killing any process")

    monkeypatch.setattr(native.signal, "pause", hold)
    if broken_stdout:
        monkeypatch.setattr("builtins.print", lambda *_a, **_kw: (_ for _ in ()).throw(BrokenPipeError("collector gone")))
    with pytest.raises(InterruptedError):
        native.emit_failure({"status": "unconfirmed"})
    assert calls == ["held"]


def test_outermost_holder_survives_failure_in_error_reporting_itself(monkeypatch):
    monkeypatch.setattr(native, "LIVE_DAEMONS", [(SimpleNamespace(poll=lambda: None), None, None, None)])
    monkeypatch.setattr(native, "_main", lambda *_: (_ for _ in ()).throw(OSError("lstat failed in error reporter")))
    calls = []

    def hold():
        calls.append("held")
        raise InterruptedError("end local passive-holder test")

    monkeypatch.setattr(native.signal, "pause", hold)
    with pytest.raises(InterruptedError):
        native.main([])
    assert calls == ["held"]


def test_log_thread_creation_failure_registers_live_daemon_before_outermost_hold(tmp_path, monkeypatch, plan):
    pairs = [os.pipe(), os.pipe()]
    streams = [os.fdopen(pair[0], "rb") for pair in pairs]
    process = SimpleNamespace(pid=123, poll=lambda: None, stdout=streams[0], stderr=streams[1])
    calls, registered = [], []
    monkeypatch.setattr(native, "LIVE_DAEMONS", registered)
    monkeypatch.setattr(native.subprocess, "Popen", lambda *_a, **_k: process)

    def fail_logs(actual, root):
        assert registered == [(process, None, tmp_path, plan)]
        assert actual is process and root == tmp_path
        raise RuntimeError("cannot start new thread at synthetic PID limit")

    def hold():
        calls.append("held")
        raise InterruptedError("end passive-holder test; no daemon or Docker launched")

    monkeypatch.setattr(native, "DaemonLogs", fail_logs)
    monkeypatch.setattr(native.signal, "pause", hold)
    monkeypatch.setattr(native, "_main", lambda *_: native.launch_daemon(SimpleNamespace(env={}), tmp_path, plan))
    try:
        with pytest.raises(InterruptedError):
            native.main([])
        assert calls == ["held"] and all(not stream.closed for stream in streams)
        assert registered == [(process, None, tmp_path, plan)]
    finally:
        for stream in streams:
            stream.close()
        for _read_fd, write_fd in pairs:
            os.close(write_fd)
