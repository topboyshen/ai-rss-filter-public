"""Synthetic private files and inspect records; no Docker or production access."""

from copy import deepcopy
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

import pytest

from scripts import private_release_contract as private


HELPER = Path(private.__file__)
DIGEST = "sha256:" + "a" * 64
SECRET = "synthetic-secret-that-must-never-appear-in-public-output"


def save(path, value):
    path.write_bytes(value if isinstance(value, bytes) else private.encoded(value))
    path.chmod(0o600)
    return path


@pytest.fixture
def fixture(tmp_path):
    tmp_path = tmp_path.resolve()
    repo = tmp_path / "public"
    repo.mkdir()
    (repo / "config.yaml").write_text("public_base_url: https://example.invalid\n")
    (repo / "docker-compose.yml").write_text("services: {}\n")
    for command in (["init", "-q"], ["add", "."], ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.org", "commit", "-qm", "fixture"]):
        subprocess.run(["git", "-C", str(repo), *command], check=True, capture_output=True)
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    project_dir = directory / "existing-project"
    project_dir.mkdir(mode=0o700)
    data = project_dir / "data"
    data.mkdir(mode=0o700)
    secret = save(directory / "secret", b"synthetic-file-secret-value-0123456789")
    config = save(directory / "runtime.yaml", b"threshold:\n  pass: 7\nscoring_strategy_version: fixture\n")
    environment = save(directory / "env", f"LLM_API_KEY={SECRET}\n".encode())
    manifest = {
        "name": "existing-project",
        "services": {
            "ai-filter-api": {
                "image": private.IMAGE, "mem_limit": "768m", "pids_limit": 256,
                "user": f"{os.geteuid()}:{os.getegid()}",
                "command": ["python", "app.py"],
                "environment": {"AI_FILTER_CONFIG_PATH": "/app/config.yaml", "LLM_API_KEY": "${LLM_API_KEY:?}"},
                "volumes": [
                    {"type": "bind", "source": str(data), "target": "/app/data", "bind": {"create_host_path": False}},
                    {"type": "bind", "source": private.RUNTIME, "target": "/app/config.yaml", "read_only": True},
                ],
                "networks": ["ai-net"],
                "ports": [{"target": 5000, "published": "5000", "host_ip": "127.0.0.1", "protocol": "tcp"}],
            },
            "redis": {"image": "redis@" + DIGEST, "mem_limit": "384m", "pids_limit": 128, "volumes": [{"type": "volume", "source": "redis-data", "target": "/data"}],
                      "networks": ["ai-net"]},
        },
        "secrets": {"session": {"file": str(secret)}},
        "networks": {"ai-net": {"external": True, "name": "existing-project_ai-net"}},
        "volumes": {"redis-data": {"external": True, "name": "existing-project_redis-data"}},
        "configs": {"gateway": {"content": "proxy_set_header Host $$host;"}},
    }
    manifest_path = save(directory / "manifest.json", manifest)
    review = {
        **private.source_identity(repo), "approved": True, "approved_by": "synthetic-reviewer",
        "approved_at": int(time.time()) - 1, "expires_at": int(time.time()) + 3600,
        "runtime_config_sha256": private.sha(config.read_bytes()),
        "input_manifest_sha256": private.sha(manifest_path.read_bytes()),
        "environment_sha256": private.sha(environment.read_bytes()),
        "strategy": "fixture:abcdef012345", "project": "existing-project", "project_directory": str(project_dir),
        "health_services": ["ai-filter-api", "redis"],
        "service_mapping": {"ai-filter-api": ["ai-filter-api"], "redis": ["redis"]},
        "topology": {name: {"mounts": service.get("volumes", []), "networks": service.get("networks", []),
                            "ports": service.get("ports", [])} for name, service in manifest["services"].items()},
    }
    review["existing_topology"] = private.runtime_topology(inspect_records(
        {"manifest_data": manifest, "review_data": review}, directory / "sealed"))
    review_path = save(directory / "review.json", review)
    result = {"root": repo, "config": config, "manifest": manifest_path, "environment": environment,
              "review": review_path, "output": directory / "sealed", "secret": secret,
              "manifest_data": manifest, "review_data": review}
    try:
        yield result
    finally:
        # Assertions exercise the real sealed permissions. Only after the test,
        # restore directory writes in this disposable fixture so the ordinary
        # unprivileged release-test runner can unlink its synthetic files.
        sealed = result["output"]
        if sealed.is_dir() and not sealed.is_symlink():
            sealed.chmod(stat.S_IMODE(sealed.stat().st_mode) | stat.S_IWUSR)


def seal(fixture):
    return private.seal(**{key: fixture[key] for key in ("root", "config", "manifest", "environment", "review", "output")})


def reseal_inputs(fixture):
    save(fixture["manifest"], fixture["manifest_data"])
    fixture["review_data"]["input_manifest_sha256"] = private.sha(fixture["manifest"].read_bytes())
    save(fixture["review"], fixture["review_data"])


def inspect_records(fixture, directory, *, image=DIGEST):
    records = []
    for index, (name, service) in enumerate(fixture["manifest_data"]["services"].items()):
        mounts = []
        for mount in service.get("volumes", []):
            source = mount["source"]
            if source == private.RUNTIME:
                source = str(directory / "runtime.yaml")
            mounts.append({"Type": mount["type"], "Source": source,
                           "Name": "existing-project_redis-data" if mount["type"] == "volume" else "",
                           "Destination": mount["target"], "RW": not mount.get("read_only", False)})
        records.append({"Id": str(index) * 64, "Image": image, "State": {"Running": True},
                        "Config": {"Labels": {"com.docker.compose.project": "existing-project",
                                              "com.docker.compose.project.working_dir": fixture["review_data"]["project_directory"],
                                              "com.docker.compose.service": name},
                                   "Env": [key + "=" + (SECRET if value == "${LLM_API_KEY:?}" else str(value))
                                           for key, value in service.get("environment", {}).items()],
                                   "Cmd": service.get("command", []), "Entrypoint": None, "User": service.get("user", ""),
                                   "WorkingDir": service.get("working_dir", "/app")},
                        "Mounts": mounts, "NetworkSettings": {"Networks": {"existing-project_ai-net": {}}},
                        "HostConfig": {"Memory": private.memory_bytes(service["mem_limit"]),
                                       "MemorySwap": 2 * private.memory_bytes(service["mem_limit"]),
                                       "PidsLimit": service["pids_limit"], "PortBindings": {f"{port['target']}/{port['protocol']}": [
                            {"HostIp": port["host_ip"], "HostPort": port["published"]}] for port in service.get("ports", [])}}})
    return records


def fake_docker(monkeypatch, contract, records, *, probe=None, image_defaults=None):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ["docker", "ps"]:
            return ("\n".join(record["Id"] for record in records) + "\n").encode()
        if args[:2] == ["docker", "inspect"]:
            return private.encoded(records)
        if args[:3] == ["docker", "image", "inspect"]:
            defaults = {"Cmd": [], "Entrypoint": None, "Env": [], "WorkingDir": "/app"}
            return private.encoded([{"Id": DIGEST, "Config": {**defaults, **(image_defaults or {})}}])
        if args[:2] in (["docker", "volume"], ["docker", "network"]):
            return private.encoded([{"Name": args[-1]}])
        if args[:2] in (["docker", "exec"], ["docker", "run"]):
            return private.encoded(probe or {"config_sha256": contract["files"]["runtime.yaml"],
                                           "strategy": contract["review"]["strategy"]})
        raise AssertionError(args)

    monkeypatch.setattr(private, "run", run)
    return calls


def test_sealed_contract_keeps_public_source_and_private_runtime_distinct(fixture):
    path = seal(fixture)
    contract, manifest, digest = private.load(path, fixture["root"], require_fresh=True)
    assert len(digest) == 64
    assert contract["example_config_sha256"] != contract["files"]["runtime.yaml"]
    assert contract["source_manifest_sha256"] != contract["files"]["compose.json"]
    assert private.sha(fixture["config"].read_bytes()) == contract["files"]["runtime.yaml"]
    assert manifest["services"]["ai-filter-api"]["volumes"][1]["source"] == str(path.parent / "runtime.yaml")
    assert SECRET not in path.read_text()
    assert "synthetic-file-secret-value" not in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o500
    assert not list(fixture["root"].glob("**/runtime.yaml"))


@pytest.mark.parametrize("name", ["config", "manifest", "environment", "review", "secret"])
def test_original_input_drift_after_seal_is_rejected(fixture, name):
    path = seal(fixture)
    fixture[name].write_bytes(fixture[name].read_bytes() + b"\n")
    with pytest.raises(private.ContractError, match="changed after sealing|metadata drift"):
        private.load(path, fixture["root"])


@pytest.mark.parametrize("mutation", ["mode", "symlink", "owner", "hardlink", "parent_symlink"])
def test_input_permissions_ownership_links_rejected(fixture, monkeypatch, mutation):
    path = fixture["config"]
    if mutation == "mode":
        path.chmod(0o644)
    elif mutation == "symlink":
        actual = path.with_suffix(".actual")
        path.rename(actual)
        path.symlink_to(actual)
    elif mutation == "owner":
        monkeypatch.setattr(private.os, "geteuid", lambda: path.stat().st_uid + 1)
    elif mutation == "hardlink":
        os.link(path, path.with_suffix(".link"))
    else:
        alias = path.parent.parent / "alias"
        alias.symlink_to(path.parent, target_is_directory=True)
        fixture["config"] = alias / path.name
    with pytest.raises(private.ContractError, match="metadata|symlink"):
        seal(fixture)


@pytest.mark.parametrize("file", ["runtime.yaml", "compose.json", "environment", "review.json", "input-compose.json"])
def test_frozen_contents_and_permissions_cannot_drift(fixture, file):
    path = seal(fixture)
    frozen = path.parent / file
    frozen.chmod(0o600)
    with pytest.raises(private.ContractError, match="metadata"):
        private.load(path, fixture["root"])
    frozen.write_bytes(frozen.read_bytes() + b"\n")
    frozen.chmod(0o400)
    with pytest.raises(private.ContractError, match="frozen private input drift"):
        private.load(path, fixture["root"])


@pytest.mark.parametrize("change", ["config", "project", "path", "data", "network", "ports", "stale", "source"])
def test_review_must_bind_every_identity(fixture, change):
    review = fixture["review_data"]
    manifest = fixture["manifest_data"]
    if change == "config":
        review["runtime_config_sha256"] = "0" * 64
    elif change == "project":
        manifest["name"] = "wrong-project"
    elif change == "path":
        review["project_directory"] += "/missing"
    elif change == "data":
        review["topology"] = deepcopy(review["topology"])
        review["topology"]["ai-filter-api"]["mounts"][0]["source"] += "-wrong"
    elif change == "network":
        manifest["networks"]["ai-net"]["external"] = False
    elif change == "ports":
        review["topology"] = deepcopy(review["topology"])
        review["topology"]["ai-filter-api"]["ports"][0]["published"] = "9000"
    elif change == "stale":
        review["expires_at"] = int(time.time()) - 5
    else:
        review["source_tree"] = "0" * 40
    reseal_inputs(fixture)
    with pytest.raises(private.ContractError):
        seal(fixture)


def test_stale_review_blocks_new_action_but_keeps_historical_verification(fixture, monkeypatch):
    path = seal(fixture)
    monkeypatch.setattr(private.time, "time", lambda: fixture["review_data"]["expires_at"] + 1)
    private.load(path, fixture["root"])
    with pytest.raises(private.ContractError, match="stale"):
        private.load(path, fixture["root"], require_fresh=True)


@pytest.mark.parametrize("body", [b"public_base_url: https://example.invalid", b"security:\n  api_key: private-secret",
                                  b"url: https://user:password@service.test", b"[1,2]", b"llm:\n  api_key: secret"])
def test_private_yaml_rejects_example_or_inline_secrets(fixture, body):
    save(fixture["config"], body)
    fixture["review_data"]["runtime_config_sha256"] = private.sha(body)
    reseal_inputs(fixture)
    with pytest.raises(private.ContractError):
        seal(fixture)


@pytest.mark.parametrize("change", ["literal_secret", "env_file", "build", "config_rw", "config_missing", "interpolation", "source_mount", "floating"])
def test_unsafe_manifest_is_rejected(fixture, change):
    service = fixture["manifest_data"]["services"]["ai-filter-api"]
    if change == "literal_secret":
        service["environment"]["LLM_API_KEY"] = SECRET
    elif change in {"env_file", "build"}:
        service[change] = "."
    elif change == "config_rw":
        service["volumes"][1]["read_only"] = False
    elif change == "config_missing":
        service["volumes"].pop()
    elif change == "interpolation":
        service["command"] = "${UNREVIEWED}"
    elif change == "source_mount":
        service["volumes"][0]["target"] = "/app"
    else:
        service["image"] = "image:latest"
    reseal_inputs(fixture)
    with pytest.raises(private.ContractError):
        seal(fixture)


def test_reseal_never_overwrites_evidence(fixture):
    path = seal(fixture)
    before = path.read_bytes()
    with pytest.raises(private.ContractError, match="must be new"):
        seal(fixture)
    assert path.read_bytes() == before


def test_dirty_or_new_source_rejected_after_gate(fixture):
    path = seal(fixture)
    (fixture["root"] / "untracked.txt").write_text("drift")
    with pytest.raises(private.ContractError, match="clean source"):
        private.load(path, fixture["root"])
    subprocess.run(["git", "-C", str(fixture["root"]), "add", "."], check=True)
    subprocess.run(["git", "-C", str(fixture["root"]), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.org", "commit", "-qm", "drift"], check=True)
    with pytest.raises(private.ContractError, match="source identity drift"):
        private.load(path, fixture["root"])


def test_image_and_runtime_probe_exact_agreement(fixture, monkeypatch):
    path = seal(fixture)
    contract, manifest, _ = private.load(path, fixture["root"])
    calls = fake_docker(monkeypatch, contract, inspect_records(fixture, path.parent))
    private.image_probe(contract, path.parent, DIGEST)
    private.preflight(contract, manifest)
    private.verify_runtime(contract, manifest, path.parent, DIGEST)
    assert any(args[:2] == ["docker", "exec"] for args, _ in calls)
    command = next(args for args, _ in calls if args[:2] == ["docker", "run"])
    assert command[command.index("--user") + 1] == f"{os.geteuid()}:{os.getegid()}"
    assert command[command.index("--network") + 1] == "none"
    assert "--read-only" in command and SECRET not in " ".join(command)


@pytest.mark.parametrize("change", ["image", "mount", "rw", "project", "directory", "network", "port", "data", "missing", "strategy", "hash", "stopped"])
def test_actual_runtime_drift_is_rejected(fixture, monkeypatch, change):
    path = seal(fixture)
    contract, manifest, _ = private.load(path, fixture["root"])
    records = inspect_records(fixture, path.parent)
    probe = None
    if change == "image":
        records[0]["Image"] = "sha256:" + "b" * 64
    elif change in {"mount", "data"}:
        records[0]["Mounts"][1 if change == "mount" else 0]["Source"] += "-wrong"
    elif change == "rw":
        records[0]["Mounts"][1]["RW"] = True
    elif change in {"project", "directory"}:
        key = "com.docker.compose.project" + (".working_dir" if change == "directory" else "")
        records[0]["Config"]["Labels"][key] = "wrong"
    elif change == "network":
        records[0]["NetworkSettings"]["Networks"] = {"wrong": {}}
    elif change == "port":
        records[0]["HostConfig"]["PortBindings"]["5000/tcp"][0]["HostPort"] = "9000"
    elif change == "missing":
        records[0]["Config"]["Labels"]["com.docker.compose.service"] = "wrong"
    elif change == "stopped":
        records[0]["State"]["Running"] = False
    else:
        probe = {"config_sha256": contract["files"]["runtime.yaml"], "strategy": contract["review"]["strategy"]}
        probe["strategy" if change == "strategy" else "config_sha256"] = "wrong"
    fake_docker(monkeypatch, contract, records, probe=probe)
    with pytest.raises(private.ContractError):
        private.verify_runtime(contract, manifest, path.parent, DIGEST)


def test_compose_uses_existing_project_and_ignores_ambient_secrets(fixture, monkeypatch):
    path = seal(fixture)
    contract, _, _ = private.load(path, fixture["root"])
    calls = []
    monkeypatch.setenv("LLM_API_KEY", "unreviewed-ambient-secret")
    monkeypatch.setenv("COMPOSE_FILE", "unreviewed.yml")
    monkeypatch.setattr(private, "run", lambda args, **kwargs: calls.append((args, kwargs)) or b"ok")
    assert private.compose(contract, path.parent, DIGEST, ["-f", str(path.parent / "compose.json"), "config", "--quiet"]) == b"ok"
    args, kwargs = calls[0]
    assert args[args.index("--project-name") + 1] == "existing-project"
    assert args[args.index("--project-directory") + 1] == fixture["review_data"]["project_directory"]
    assert "LLM_API_KEY" not in kwargs["env"]
    assert args[args.index("--env-file") + 1] == str(path.parent / "environment")
    assert kwargs["env"]["IMAGE_REF"] == DIGEST
    assert "COMPOSE_FILE" not in kwargs["env"]


@pytest.mark.parametrize("text", ["FOO=a\nFOO=b", "COMPOSE_FILE=x", "IMAGE_REF=x", "DOCKER_HOST=x", "TOKEN='$VALUE'", "NOT-A-NAME=x"])
def test_interpolation_input_is_strict(text):
    with pytest.raises(private.ContractError):
        private.environment_values(text.encode())


def test_cli_does_not_leak_malformed_private_content_or_path(fixture):
    save(fixture["manifest"], ("{" + SECRET).encode())
    fixture["review_data"]["input_manifest_sha256"] = private.sha(fixture["manifest"].read_bytes())
    save(fixture["review"], fixture["review_data"])
    args = [sys.executable, "-I", str(HELPER), "seal"]
    for key in ("root", "config", "manifest", "environment", "review", "output"):
        args += ["--" + key, str(fixture[key])]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    assert result.returncode == 3
    assert SECRET not in result.stdout + result.stderr
    assert str(fixture["manifest"]) not in result.stderr
    assert "Traceback" not in result.stderr


def test_failed_docker_diagnostic_is_not_echoed():
    with pytest.raises(private.ContractError, match="subprocess failed") as error:
        private.run([sys.executable, "-c", f"import sys; print({SECRET!r}); sys.exit(1)"])
    assert SECRET not in str(error.value)


def test_subprocess_output_is_bounded_before_exit(monkeypatch):
    monkeypatch.setattr(private, "MAX_BYTES", 65536)
    with pytest.raises(private.ContractError, match="too large"):
        private.run([sys.executable, "-c", "import os; [os.write(2,b'x'*65536) for _ in range(100)]"])


def test_duplicate_runtime_environment_rejected(fixture, monkeypatch):
    path = seal(fixture)
    contract, manifest, _ = private.load(path, fixture["root"])
    records = inspect_records(fixture, path.parent)
    records[0]["Config"]["Env"].append("AI_FILTER_CONFIG_PATH=/app/config.yaml")
    fake_docker(monkeypatch, contract, records)
    with pytest.raises(private.ContractError, match="duplicate runtime environment"):
        private.verify_runtime(contract, manifest, path.parent, DIGEST)


def test_service_mount_swap_cannot_pass_project_wide_sets(fixture, monkeypatch):
    path = seal(fixture)
    contract, manifest, _ = private.load(path, fixture["root"])
    records = inspect_records(fixture, path.parent)
    records[0]["Mounts"][0], records[1]["Mounts"][0] = records[1]["Mounts"][0], records[0]["Mounts"][0]
    fake_docker(monkeypatch, contract, records)
    with pytest.raises(private.ContractError, match="existing service identity"):
        private.preflight(contract, manifest)
    with pytest.raises(private.ContractError, match="runtime service mount"):
        private.verify_runtime(contract, manifest, path.parent, DIGEST)


def test_named_volume_cannot_hide_source(fixture):
    service = fixture["manifest_data"]["services"]["redis"]
    service["volumes"][0]["target"] = "/app/core"
    reseal_inputs(fixture)
    with pytest.raises(private.ContractError, match="source mount"):
        seal(fixture)


@pytest.mark.parametrize("payload", [b"", b"short", b"x" * 32 + b"\n\n", b"x" * 32 + b"\x00", b"x" * 4097])
def test_private_mode_keeps_existing_secret_content_rules(fixture, payload):
    save(fixture["secret"], payload)
    with pytest.raises(private.ContractError, match="ASCII graphic"):
        seal(fixture)


def test_existing_yaml_secret_references_and_boolean_controls_supported(fixture):
    payload = (b"threshold: {pass: 7}\nsecurity: {allow_query_api_key: false, api_key: ''}\n"
               b"providers: [{api_key: '${LLM_API_KEY}'}]\n")
    save(fixture["config"], payload)
    fixture["review_data"]["runtime_config_sha256"] = private.sha(payload)
    reseal_inputs(fixture)
    path = seal(fixture)
    private.load(path, fixture["root"])


@pytest.mark.parametrize("target", ["project", "data"])
def test_deployment_directory_inode_replacement_rejected(fixture, target):
    path = seal(fixture)
    directory = Path(fixture["review_data"]["project_directory"])
    if target == "data":
        directory = directory / "data"
    directory.rename(directory.with_name(directory.name + "-original"))
    directory.mkdir(mode=0o700)
    if target == "project":
        (directory / "data").mkdir(mode=0o700)
    with pytest.raises(private.ContractError, match="directory identity drift"):
        private.load(path, fixture["root"])


@pytest.mark.parametrize("key", ["PATH", "HOME", "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "BASH_ENV", "DYLD_INSERT_LIBRARIES"])
def test_private_environment_cannot_control_host_loader_or_executable(key):
    with pytest.raises(private.ContractError):
        private.environment_values((key + "=/synthetic/attacker\n").encode())


def test_non_app_python_runtime_mount_cannot_override_source(fixture):
    service = fixture["manifest_data"]["services"]["redis"]
    service["volumes"] = [{"type": "bind", "source": fixture["review_data"]["project_directory"],
                           "target": "/usr/local/lib/python3.11/site-packages", "bind": {"create_host_path": False}}]
    reseal_inputs(fixture)
    with pytest.raises(private.ContractError, match="source mount"):
        seal(fixture)


def test_candidate_cross_role_data_swap_rejected_with_same_project_sets(fixture, monkeypatch):
    services = fixture["manifest_data"]["services"]
    second = deepcopy(services["ai-filter-api"])
    data_b = Path(fixture["review_data"]["project_directory"]) / "other-data"
    data_b.mkdir(mode=0o700)
    second["volumes"][0]["source"] = str(data_b)
    second["ports"] = []
    services["ai-filter-worker"] = second
    review = fixture["review_data"]
    review["service_mapping"]["ai-filter-worker"] = ["ai-filter-worker"]
    review["existing_topology"] = private.runtime_topology(inspect_records(fixture, fixture["output"]))
    records = inspect_records(fixture, fixture["output"])
    mounts_a, mounts_b = services["ai-filter-api"]["volumes"], second["volumes"]
    mounts_a[0]["source"], mounts_b[0]["source"] = mounts_b[0]["source"], mounts_a[0]["source"]
    review["topology"] = {name: {"mounts": service.get("volumes", []), "networks": service.get("networks", []),
                               "ports": service.get("ports", [])} for name, service in services.items()}
    reseal_inputs(fixture)
    path = seal(fixture)
    contract, manifest, _ = private.load(path, fixture["root"])
    fake_docker(monkeypatch, contract, records)
    with pytest.raises(private.ContractError, match="reviewed role mapping"):
        private.preflight(contract, manifest)


@pytest.mark.parametrize("mutation", ["command", "entrypoint", "memory", "pids", "cpu", "role", "infra_image"])
def test_actual_execution_and_resource_drift_rejected(fixture, monkeypatch, mutation):
    path = seal(fixture)
    contract, manifest, _ = private.load(path, fixture["root"])
    records = inspect_records(fixture, path.parent)
    if mutation == "command":
        records[0]["Config"]["Cmd"] = ["sleep", "3600"]
    elif mutation == "entrypoint":
        records[0]["Config"]["Entrypoint"] = ["/not-reviewed"]
    elif mutation == "memory":
        records[0]["HostConfig"]["Memory"] = 16 * 1024**3
    elif mutation == "pids":
        records[0]["HostConfig"]["PidsLimit"] = 4096
    elif mutation == "cpu":
        records[0]["HostConfig"]["NanoCpus"] = 8 * 10**9
    elif mutation == "role":
        records[0]["Config"]["Env"].append("AI_FILTER_ROLE=unreviewed")
    else:
        records[1]["Image"] = "sha256:" + "b" * 64
    fake_docker(monkeypatch, contract, records)
    with pytest.raises(private.ContractError):
        private.verify_runtime(contract, manifest, path.parent, DIGEST)


def test_implicit_network_creation_rejected(fixture):
    fixture["manifest_data"]["services"]["ai-filter-api"]["networks"] = []
    reseal_inputs(fixture)
    with pytest.raises(private.ContractError, match="implicit new network"):
        seal(fixture)


@pytest.mark.parametrize("target", ["/usr/local/lib/python3.11/site-packages/override.py", "/app/core/config.py"])
def test_inline_config_cannot_override_runtime_code(fixture, target):
    service = fixture["manifest_data"]["services"]["redis"]
    service["image"] = "nginx:stable-alpine@" + DIGEST
    service["configs"] = [{"source": "gateway", "target": target}]
    reseal_inputs(fixture)
    with pytest.raises(private.ContractError, match="non-code allowlist"):
        seal(fixture)


def test_application_cannot_mount_inline_configs(fixture):
    service = fixture["manifest_data"]["services"]["ai-filter-api"]
    service["configs"] = [{"source": "gateway", "target": "/etc/nginx/nginx.conf"}]
    reseal_inputs(fixture)
    with pytest.raises(private.ContractError, match="application inline config"):
        seal(fixture)


@pytest.mark.parametrize("name", ["../../escape", "/absolute", "with space", "../file"])
def test_secret_name_cannot_escape_container_secret_directory(fixture, name):
    secret = fixture["manifest_data"]["secrets"].pop("session")
    fixture["manifest_data"]["secrets"][name] = secret
    reseal_inputs(fixture)
    with pytest.raises(private.ContractError, match="invalid secret reference name"):
        seal(fixture)


@pytest.mark.parametrize("kind", ["config", "secret"])
def test_all_service_attachment_targets_are_unique(fixture, kind):
    service = fixture["manifest_data"]["services"]["redis"]
    if kind == "config":
        service["image"] = "nginx:stable-alpine@" + DIGEST
        service["configs"] = [{"source": "gateway", "target": "/etc/nginx/nginx.conf"}] * 2
    else:
        service["secrets"] = ["session", "session"]
    reseal_inputs(fixture)
    with pytest.raises(private.ContractError, match="duplicate service mount target"):
        seal(fixture)


def test_multiple_secret_references_have_stable_frozen_order(fixture):
    first = save(fixture["secret"].parent / "another-secret", b"other-synthetic-secret-value-123456789")
    fixture["manifest_data"]["secrets"] = {"z-last": {"file": str(first)}, "a-first": {"file": str(fixture["secret"])}}
    reseal_inputs(fixture)
    path = seal(fixture)
    private.load(path, fixture["root"])



def test_fixture_teardown_only_restores_disposable_directory_writes(tmp_path):
    generator = fixture.__wrapped__(tmp_path)
    inputs = next(generator)
    path = seal(inputs)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o500
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    generator.close()
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    path.unlink()



def test_image_probe_uses_role_identity_env_aliases_and_scoped_secrets(fixture, monkeypatch):
    manifest = fixture["manifest_data"]
    service = manifest["services"]["ai-filter-api"]
    service["environment"].pop("LLM_API_KEY")
    service["environment"]["AI_FILTER_API_KEY"] = "${SECRET_REF:?}"
    service["secrets"] = ["session"]
    service["group_add"] = ["44"]
    worker = deepcopy(service)
    worker["ports"] = []
    worker["secrets"] = ["worker-only"]
    manifest["services"]["ai-filter-worker"] = worker
    second_secret = save(fixture["secret"].parent / "worker-secret", b"worker-only-synthetic-secret-123456789")
    manifest["secrets"]["worker-only"] = {"file": str(second_secret)}
    save(fixture["environment"], ("SECRET_REF=" + SECRET + "\n").encode())
    review = fixture["review_data"]
    review["environment_sha256"] = private.sha(fixture["environment"].read_bytes())
    review["service_mapping"]["ai-filter-worker"] = ["ai-filter-worker"]
    review["existing_topology"] = private.runtime_topology(inspect_records(fixture, fixture["output"]))
    review["topology"]["ai-filter-worker"] = {"mounts": worker["volumes"], "networks": worker["networks"], "ports": []}
    reseal_inputs(fixture)
    path = seal(fixture)
    contract, _, _ = private.load(path, fixture["root"])
    seen = []

    def run(args, **kwargs):
        if args[:3] == ["docker", "image", "inspect"]:
            return private.encoded([{"Id": DIGEST, "Config": {"Env": ["IMAGE_DEFAULT=kept"], "User": "", "WorkingDir": "/app"}}])
        assert args[:2] == ["docker", "run"]
        env_file = Path(args[args.index("--env-file") + 1])
        content = env_file.read_text()
        assert "AI_FILTER_API_KEY=" + SECRET in content
        assert "IMAGE_DEFAULT=kept" in content
        assert "SECRET_REF=" not in content
        assert SECRET not in " ".join(args)
        assert env_file.stat().st_mode & 0o777 == 0o600
        assert args[args.index("--user") + 1] == service["user"]
        assert args[args.index("--workdir") + 1] == "/app"
        assert args[args.index("--group-add") + 1] == "44"
        seen.append((env_file, [args[index + 1] for index, value in enumerate(args) if value == "--mount"]))
        return private.encoded({"config_sha256": contract["files"]["runtime.yaml"], "strategy": review["strategy"]})

    monkeypatch.setattr(private, "run", run)
    private.image_probe(contract, path.parent, DIGEST)
    assert len(seen) == 2 and all(not file.exists() for file, _ in seen)
    assert any("/run/secrets/session" in mount for mount in seen[0][1])
    assert not any("worker-only" in mount for mount in seen[0][1])
    assert any("/run/secrets/worker-only" in mount for mount in seen[1][1])
    assert not any("/run/secrets/session" in mount for mount in seen[1][1])


@pytest.mark.parametrize("user", ["node", "1001", "root", "1:-1", "4294967295:1"])
def test_ambiguous_or_invalid_user_identity_is_rejected(fixture, user):
    fixture["manifest_data"]["services"]["ai-filter-api"]["user"] = user
    reseal_inputs(fixture)
    with pytest.raises(private.ContractError, match="identity"):
        seal(fixture)


def test_probe_refuses_different_owner_without_widening_permissions(fixture, monkeypatch):
    service = fixture["manifest_data"]["services"]["ai-filter-api"]
    service["user"] = f"{os.geteuid() + 1}:{os.getegid()}"
    reseal_inputs(fixture)
    path = seal(fixture)
    contract, _, _ = private.load(path, fixture["root"])
    calls = fake_docker(monkeypatch, contract, inspect_records(fixture, path.parent))
    with pytest.raises(private.ContractError, match="runtime UID does not own"):
        private.image_probe(contract, path.parent, DIGEST)
    assert not any(args[:2] == ["docker", "run"] for args, _ in calls)
    assert stat.S_IMODE((path.parent / "runtime.yaml").stat().st_mode) == 0o400


@pytest.mark.parametrize("change", ["user", "groups", "privileged", "cap_add", "cap_drop", "security_opt"])
def test_runtime_launch_identity_and_privilege_drift_rejected(fixture, monkeypatch, change):
    path = seal(fixture)
    contract, manifest, _ = private.load(path, fixture["root"])
    records = inspect_records(fixture, path.parent)
    if change == "user":
        records[0]["Config"]["User"] = f"{os.geteuid() + 1}:0"
    else:
        key, value = {"groups": ("GroupAdd", ["77"]), "privileged": ("Privileged", True),
                      "cap_add": ("CapAdd", ["SYS_ADMIN"]), "cap_drop": ("CapDrop", ["ALL"]),
                      "security_opt": ("SecurityOpt", ["seccomp=unconfined"])}[change]
        records[0]["HostConfig"][key] = value
    fake_docker(monkeypatch, contract, records)
    with pytest.raises(private.ContractError):
        private.verify_runtime(contract, manifest, path.parent, DIGEST)


def test_manifest_cannot_request_added_capabilities(fixture):
    fixture["manifest_data"]["services"]["ai-filter-api"]["cap_add"] = ["DAC_OVERRIDE"]
    reseal_inputs(fixture)
    with pytest.raises(private.ContractError, match="capabilities forbidden"):
        seal(fixture)


@pytest.mark.parametrize("field", ["WorkingDir", "Test", "disable", "Interval", "Timeout",
                                    "StartPeriod", "StartInterval", "Retries", "missing"])
def test_actual_execution_context_drift_rejected_before_loader(fixture, monkeypatch, field):
    health = {"Test": ["CMD", "curl", "--fail", "http://127.0.0.1:5000/health"],
              "Interval": 30_000_000_000, "Timeout": 3_000_000_000, "StartPeriod": 20_000_000_000,
              "StartInterval": 1_000_000_000, "Retries": 3}
    path = seal(fixture)
    contract, manifest, _ = private.load(path, fixture["root"])
    records = inspect_records(fixture, path.parent)
    for record in records:
        record["Config"]["Healthcheck"] = deepcopy(health)
    calls = fake_docker(monkeypatch, contract, records, image_defaults={"Healthcheck": health})
    private.verify_runtime(contract, manifest, path.parent, DIGEST)
    config = records[0]["Config"]
    if field == "WorkingDir":
        config[field] = "/app/data"
    elif field == "missing":
        config.pop("Healthcheck")
    elif field in {"Test", "disable"}:
        config["Healthcheck"]["Test"] = ["CMD", "true"] if field == "Test" else ["NONE"]
    else:
        config["Healthcheck"][field] += 1
    calls.clear()
    with pytest.raises(private.ContractError, match="working directory|healthcheck configuration"):
        private.verify_runtime(contract, manifest, path.parent, DIGEST)
    assert not any(args[:2] == ["docker", "exec"] for args, _ in calls)


@pytest.mark.parametrize("disabled", [False, True])
def test_explicit_healthcheck_merges_image_defaults_and_verifies_exactly(fixture, monkeypatch, disabled):
    service = fixture["manifest_data"]["services"]["ai-filter-api"]
    service["working_dir"] = "/app"
    service["healthcheck"] = {"interval": "0s", "timeout": "1.5s", "start_period": "20s",
                              "start_interval": "100ms", "retries": 4, "disable": disabled}
    if not disabled:
        service["healthcheck"]["test"] = ["CMD-SHELL", "curl --fail http://127.0.0.1:5000/health"]
    reseal_inputs(fixture)
    path = seal(fixture)
    contract, manifest, _ = private.load(path, fixture["root"])
    health = {"Test": ["CMD", "image-health"], "Interval": 30_000_000_000, "Timeout": 3_000_000_000, "Retries": 3}
    records = inspect_records(fixture, path.parent)
    for record in records:
        record["Config"]["Healthcheck"] = deepcopy(health)
    records[0]["Config"]["Healthcheck"] = {
        "Test": ["NONE"] if disabled else service["healthcheck"]["test"],
        "Interval": 30_000_000_000, "Timeout": 1_500_000_000, "StartPeriod": 20_000_000_000,
        "StartInterval": 100_000_000, "Retries": 4}
    fake_docker(monkeypatch, contract, records, image_defaults={"Healthcheck": health})
    private.verify_runtime(contract, manifest, path.parent, DIGEST)
    records[0]["Config"]["Healthcheck"]["Retries"] = 3
    with pytest.raises(private.ContractError, match="healthcheck configuration"):
        private.verify_runtime(contract, manifest, path.parent, DIGEST)


@pytest.mark.parametrize("field,value", [
    ("working_dir", "/app/data"), ("working_dir", "app"), ("working_dir", "/app/../app"), ("working_dir", ""),
    ("healthcheck", {"test": "true"}), ("healthcheck", {"test": ["UNREVIEWED", "true"]}),
    ("healthcheck", {"disable": True, "test": ["CMD", "true"]}),
    ("healthcheck", {"interval": "1s500ms"}), ("healthcheck", {"interval": -1}),
    ("healthcheck", {"start_interval": "1s"}), ("healthcheck", {"retries": True}),
    ("healthcheck", {"unknown": "ignored"}),
])
def test_unsupported_execution_context_refused_at_seal(fixture, field, value):
    fixture["manifest_data"]["services"]["ai-filter-api"][field] = value
    reseal_inputs(fixture)
    with pytest.raises(private.ContractError):
        seal(fixture)


def test_actual_code_mount_drift_rejected_before_loader(fixture, monkeypatch):
    path = seal(fixture)
    contract, manifest, _ = private.load(path, fixture["root"])
    records = inspect_records(fixture, path.parent)
    records[0]["Mounts"].append({"Type": "bind", "Source": "/synthetic/unreviewed", "Destination": "/app/core", "RW": True})
    calls = fake_docker(monkeypatch, contract, records)
    with pytest.raises(private.ContractError, match="mount, network, or port"):
        private.verify_runtime(contract, manifest, path.parent, DIGEST)
    assert not any(args[:2] == ["docker", "exec"] for args, _ in calls)


@pytest.mark.parametrize("change", ["none", "exited", "started", "restart", "dead", "restarting"])
def test_created_execution_contract_is_explicit_and_never_accepts_previous_start(fixture, change):
    contract_path = seal(fixture)
    contract, manifest, _ = private.load(contract_path, fixture["root"])
    records = inspect_records(fixture, contract_path.parent)
    container = records[0]
    container["State"].update(Running=False, Status="created", StartedAt="0001-01-01T00:00:00Z", Restarting=False, Dead=False)
    container["RestartCount"] = 0
    if change == "exited":
        container["State"]["Status"] = "exited"
    elif change == "started":
        container["State"]["StartedAt"] = "2026-01-01T00:00:00Z"
    elif change == "restart":
        container["RestartCount"] = 1
    elif change in {"dead", "restarting"}:
        container["State"][change.title()] = True
    service = manifest["services"]["ai-filter-api"]
    image = {"Id": DIGEST, "Config": {"Env": [], "Cmd": [], "Entrypoint": None, "WorkingDir": "/app"}}
    values = private.environment_values(fixture["environment"].read_bytes())
    if change == "none":
        private.verify_execution(service, container, image, values, expected_running=False)
        with pytest.raises(private.ContractError):
            private.verify_execution(service, container, image, values)
    else:
        with pytest.raises(private.ContractError):
            private.verify_execution(service, container, image, values, expected_running=False)
