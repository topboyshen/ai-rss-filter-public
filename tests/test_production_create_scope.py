"""Pure pre-create checks; forbidden inputs never reach the Docker callbacks."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from services import admission_fence as gate, production_container_lifecycle as lifecycle


def manifest():
    common = {"image": "${IMAGE_REF:?}", "mem_limit": "768m", "memswap_limit": "768m", "pids_limit": 256,
        "cpus": 1, "user": "1001:1001", "group_add": [], "cap_add": [], "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"], "networks": ["existing"], "restart": "no",
        "working_dir": "/app", "environment": {"AI_FILTER_CONFIG_PATH": "/app/config.yaml"},
        "volumes": [], "ports": [], "secrets": [], "configs": [], "healthcheck": {"disable": True}}
    services = {name: {**deepcopy(common), "command": command} for name, command in (
        ("candidate-api", ["/app/start.sh"]), ("candidate-worker", ["python", "worker_main.py"]))}
    for name, role in (("candidate-api", "web"), ("candidate-worker", "worker")):
        services[name]["environment"]["AI_FILTER_ROLE"] = role
    return {"name": "synthetic", "services": services,
            "networks": {"existing": {"external": True, "name": "existing-network"}}}


def test_existing_explicit_app_fields_are_preserved_without_dependency_inference():
    value = manifest()
    for service in value["services"].values():
        service.update({"scale": 1, "deploy": {"replicas": 1}, "profiles": [], "entrypoint": None,
            "read_only": True, "tmpfs": ["/tmp:size=16777216"], "platform": "linux/amd64",
            "stop_signal": "SIGTERM", "stop_grace_period": "10s", "logging": {"driver": "json-file"},
            "labels": {"synthetic.note": "service:literal-is-not-a-namespace"}})
        service["environment"]["SYNTHETIC_NOTE"] = "service:literal-is-not-a-dependency"
    original = deepcopy(value)
    lifecycle._create_scope(value)
    assert value == original
    assert lifecycle._roles({"app_services": list(value["services"])}, value) == {
        "web": "candidate-api", "worker": "candidate-worker"}


def test_existing_no_network_singleton_services_remain_supported():
    value = manifest()
    for service in value["services"].values():
        del service["networks"]
        service["network_mode"] = "none"
    lifecycle._create_scope(value)


BAD_FIELDS = [
    ("depends_on", ["extra"]), ("depends_on", {"extra": {"condition": "service_started", "required": False}}),
    ("depends_on", []), ("links", ["extra:alias"]), ("external_links", ["outside:alias"]),
    ("volumes_from", ["extra"]), ("extends", {"service": "extra"}), ("build", {"context": "."}),
    ("network_mode", "service:extra"), ("network_mode", "container:unreviewed"),
    ("ipc", "service:extra"), ("pid", "service:extra"), ("uts", "service:extra"), ("cgroup", "service:extra"),
    ("provider", {"type": "external-program"}), ("pre_start", [{"image": "temporary-image", "command": "true"}]),
    ("post_start", [{"command": "true"}]), ("pre_stop", [{"command": "true"}]),
    ("develop", {"watch": []}), ("models", ["unreviewed"]), ("label_file", ["unreviewed"]),
    ("future_service_field", {}), ("scale", 2), ("scale", True), ("scale", 1.0), ("scale", "1"),
    ("deploy", {"replicas": 2}), ("deploy", {"replicas": True}), ("deploy", {"replicas": 1.0}),
    ("deploy", {"replicas": "1"}), ("deploy", {"mode": "global"}), ("deploy", None),
    ("profiles", ["extra"]), ("profiles", ""),
]


@pytest.mark.parametrize("field,value", BAD_FIELDS)
def test_forbidden_creation_scope_precedes_callbacks_and_all_intents(tmp_path, monkeypatch, field, value):
    roots = tuple(tmp_path / name for name in ("runtime", "state", "evidence"))
    for path in roots:
        path.mkdir(mode=0o700)
    deployment = manifest()
    deployment["services"]["candidate-api"][field] = value
    contract = {"app_services": list(deployment["services"])}
    callbacks, commands = [], []
    monkeypatch.setattr(lifecycle, "_inputs", lambda *a, **kw: (roots, tmp_path, contract, deployment, "a" * 64))
    def before(*args):
        callbacks.append(args)
        raise AssertionError("forbidden input reached pre-create callback")
    def compose(*args):
        commands.append(args)
        raise AssertionError("forbidden input reached Compose")
    monkeypatch.setattr(lifecycle, "_before_create", before)
    monkeypatch.setattr(lifecycle.private, "compose", compose)
    paths = {name: str(tmp_path / name) for name in ("database", "config", "freeze", "state")}
    with pytest.raises(gate.AdmissionError, match="controlled create"):
        lifecycle.create(SimpleNamespace(), *roots, operation_id="b" * 64, paths=paths, web_workers=2, threads=4)
    assert callbacks == commands == []
    assert all(list(path.iterdir()) == [] for path in roots)


def test_unselected_service_indirection_cannot_hide_in_full_reviewed_manifest():
    value = manifest()
    value["services"]["unselected-existing-infra"] = {
        "image": "redis@sha256:" + "c" * 64, "cgroup": "service:extra"}
    with pytest.raises(gate.AdmissionError, match="controlled create"):
        lifecycle._create_scope(value)
