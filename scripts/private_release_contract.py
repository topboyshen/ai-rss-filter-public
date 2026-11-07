"""Sealed private deployment inputs for clean, public-source image releases.

No command prints private input, Docker diagnostics, environment, or paths on
failure. This module never starts an application except the networkless config
loader probe, and never creates production volumes or networks.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import shlex
from decimal import Decimal
import stat
import subprocess
import sys
import tempfile
import time

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import production_native_deadline as native_deadline  # noqa: E402
from scripts import private_release_diagnostics as command_diagnostics  # noqa: E402


SCHEMA = "ai-filter-private-release-v1"
IMAGE = "${IMAGE_REF:?}"
RUNTIME = "@RUNTIME_CONFIG@"
MAX_BYTES = 4 * 1024 * 1024
MAX_AGE = 21600
SENSITIVE = re.compile(r"(?:^|_)(?:password|passwd|token|secret|api_key|bot_token)(?:$|_)", re.I)
HOST_RESERVED = {"PATH", "HOME", "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
                 "DYLD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME", "BASH_ENV", "ENV", "IFS",
                 "SHELLOPTS", "TMPDIR", "TMP", "TEMP", "XDG_CONFIG_HOME"}
REFERENCE = re.compile(r"\$\{([A-Z][A-Z0-9_]*):\?\}")
PROBE = (
    "import hashlib,json,os; from core.config import load_config; "
    "p=os.environ.get('AI_FILTER_CONFIG_PATH'); "
    "assert p=='/app/config.yaml'; "
    "print(json.dumps({'config_sha256':hashlib.sha256(open(p,'rb').read()).hexdigest(),"
    "'strategy':load_config()['scoring_strategy_version']}))"
)


class ContractError(ValueError):
    """The message is always a fixed public-safe diagnostic."""


def require(condition, message):
    if not condition:
        raise ContractError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def safe_path(path):
    path = Path(path)
    require(path.is_absolute() and str(path) == os.path.normpath(path)
            and not any(c in str(path) for c in "\r\n\x00,$"), "unsafe private path")
    # Check every component, including parents: final-component O_NOFOLLOW alone
    # does not protect a directory redirected by a symlink.
    for part in (path, *path.parents):
        require(not part.is_symlink(), "symlink in private path")
    return path


def private_file(path, *, sealed=False):
    path = safe_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        modes = {0o400} if sealed else {0o400, 0o600}
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) in modes
                and info.st_size <= MAX_BYTES, "unsafe private file metadata")
        data = os.read(fd, MAX_BYTES + 1)
        after = os.fstat(fd)
        require((info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                == (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                and len(data) == info.st_size, "private input changed while reading")
        record = {"path": str(path), "sha256": sha(data), "device": info.st_dev,
                  "inode": info.st_ino, "uid": info.st_uid,
                  "mode": stat.S_IMODE(info.st_mode), "mtime_ns": info.st_mtime_ns,
                  "ctime_ns": info.st_ctime_ns, "size": info.st_size}
        return data, record
    finally:
        os.close(fd)


def private_directory(path, *, sealed=False):
    path = safe_path(path)
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == (0o500 if sealed else 0o700),
            "unsafe private directory metadata")
    return path


def directory_identity(path):
    path = safe_path(path)
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and not stat.S_IMODE(info.st_mode) & 0o022, "unsafe deployment directory metadata")
    return {"path": str(path), "device": info.st_dev, "inode": info.st_ino,
            "uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode)}


def validate_control_subpaths(manifest, review, apps):
    if "control_subpaths" not in review:
        return
    # This helper also runs under `python -I path/to/private_release_contract.py`.
    # Load only the sibling from this reviewed source, never ambient sys.path.
    spec = importlib.util.spec_from_file_location("private_control_subpaths", Path(__file__).with_name("private_control_subpaths.py"))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    try:
        helper.validate(manifest, review, apps)
    except (OSError, ValueError, KeyError, TypeError):
        raise ContractError("reviewed control subpath identity or mapping differs") from None


def retained_custody(contract, manifest):
    """The optional exact variant never grants stop/create/switch authority."""
    review = contract["review"]
    if "retained_gateway_custody" not in review:
        return None
    spec = importlib.util.spec_from_file_location("private_retained_gateway_custody",
        Path(__file__).with_name("retained_gateway_custody.py"))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    try:
        return helper, helper.load(review, manifest, contract["app_services"], read_private=private_file)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ContractError("reviewed retained gateway custody differs") from None


def run(args, *, env=None):
    # Stream a bounded total from both pipes. Checking capture_output only after
    # completion would permit unbounded memory before reaching the size check.
    output = bytearray()
    total = 0
    observation = command_diagnostics.begin(args, sys._getframe(1))
    process, phase = None, "budget"
    stdout_eof = stderr_eof = False
    stdout_bytes = stderr_bytes = 0

    def observed(error):
        command_diagnostics.finish(observation, error=error, phase=phase, process=process,
                                   stdout_eof=stdout_eof, stderr_eof=stderr_eof,
                                   stdout_bytes=stdout_bytes, stderr_bytes=stderr_bytes)

    try:
        # Keep the original evaluation order and use the one actual clamp result.
        started = time.monotonic()
        seconds = native_deadline.clamp(120)
        deadline = started + seconds
        command_diagnostics.applied(observation, seconds)
        phase = "spawn"
        with native_deadline.process(subprocess.Popen, args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     start_new_session=True) as process:
            try:
                phase = "stream"
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ, True)
                    selector.register(process.stderr, selectors.EVENT_READ, False)
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        require(remaining > 0, "private release subprocess timed out")
                        for key, _events in selector.select(min(remaining, 1)):
                            chunk = os.read(key.fileobj.fileno(), 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                if key.data:
                                    stdout_eof = True
                                else:
                                    stderr_eof = True
                                continue
                            total += len(chunk)
                            if key.data:
                                stdout_bytes += len(chunk)
                            else:
                                stderr_bytes += len(chunk)
                            require(total <= MAX_BYTES, "private release output too large")
                            if key.data:
                                output.extend(chunk)
                    phase = "wait"
                    require(process.wait(timeout=max(0.001, deadline - time.monotonic())) == 0,
                            "private release subprocess failed")
            except BaseException as error:
                observed(error)  # Freeze the first failure before the existing client cleanup.
                native_deadline.finish_client(process, error)
                raise
            phase = "close"
        phase = "complete"
    except BaseException as error:
        observed(error)
        raise
    observed(None)
    return bytes(output)


def source_identity(root):
    root = Path(root)
    require(not run(["git", "-C", str(root), "status", "--porcelain=v1",
                     "--untracked-files=all"]).strip(), "private release requires clean source")
    return {"source_sha": run(["git", "-C", str(root), "rev-parse", "HEAD"]).decode().strip(),
            "source_tree": run(["git", "-C", str(root), "rev-parse", "HEAD^{tree}"]).decode().strip(),
            "example_config_sha256": sha((root / "config.yaml").read_bytes()),
            "source_manifest_sha256": sha((root / "docker-compose.yml").read_bytes())}


def environment_values(data):
    values = {}
    for line in data.decode().splitlines():
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        require(sep and re.fullmatch(r"[A-Z][A-Z0-9_]*", key) and key not in values
                and key != "IMAGE_REF" and key not in HOST_RESERVED
                and not key.startswith(("COMPOSE_", "DOCKER_", "LD_", "DYLD_", "PYTHON"))
                and not any(c in value for c in "\r\x00$'\"\\"),
                "environment must use unique plain NAME=value records")
        values[key] = value
    return values


def reject_inline_credentials(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if SENSITIVE.search(str(key)) and not str(key).lower().endswith("_file"):
                reference = isinstance(child, str) and re.fullmatch(r"\$\{[A-Z][A-Z0-9_]*\}", child)
                require(child in (None, "") or type(child) is bool or reference,
                        "runtime YAML must use secret references")
            reject_inline_credentials(child)
    elif isinstance(value, list):
        for child in value:
            reject_inline_credentials(child)
    elif isinstance(value, str):
        require("example.invalid" not in value and not re.search(r"://[^/@\s]+:[^/@\s]+@", value),
                "placeholder or embedded URL credential in private input")


def validate_mounts(service, manifest, runtime_path, app):
    mounts = service.get("volumes", [])
    require(isinstance(mounts, list), "mounts must use long syntax")
    targets = set()
    canonical_mounts = []
    config_count = 0
    for mount in mounts:
        require(isinstance(mount, dict) and set(mount) <= {"type", "source", "target", "read_only", "bind"}
                and mount.get("type") in {"bind", "volume"}
                and isinstance(mount.get("target"), str) and mount["target"].startswith("/")
                and mount["target"] not in targets
                and isinstance(mount.get("read_only", False), bool), "invalid or duplicate mount")
        target = mount["target"]
        require(os.path.normpath(target) == target and not target.endswith(".py")
                and (target in {"/app/data", "/data", "/app/config.yaml"}
                     or target.startswith(("/app/data/", "/var/lib/"))),
                "runtime source mount forbidden")
        targets.add(target)
        source = mount.get("source")
        if mount["target"] == "/app/config.yaml":
            require(app and source == runtime_path and mount.get("read_only") is True
                    and mount["type"] == "bind", "runtime config mount mismatch")
            config_count += 1
        elif mount["type"] == "bind":
            safe_path(source)
            require(Path(source).is_dir() and mount.get("bind") == {"create_host_path": False},
                    "bind must reuse an existing directory without auto-creation")
            require(not mount["target"].startswith(("/app/core", "/app/services", "/app/scripts"))
                    and mount["target"] != "/app" and not mount["target"].endswith(".py"),
                    "runtime source mount forbidden")
        else:
            require(source in manifest.get("volumes", {}), "unknown named volume")
        normalized = dict(mount)
        if config_count and mount["target"] == "/app/config.yaml":
            normalized["source"] = RUNTIME
        canonical_mounts.append(normalized)
    return canonical_mounts, config_count


def validate_attachments(service, manifest, app):
    secrets = service.get("secrets", [])
    require(isinstance(secrets, list) and all(isinstance(key, str) and key in manifest.get("secrets", {}) for key in secrets),
            "service secrets must use named file references")
    configs = service.get("configs", [])
    require(isinstance(configs, list) and not (app and configs), "application inline config mounts forbidden")
    if configs:
        require(re.fullmatch(r"(?:docker\.io/library/)?nginx(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}", service["image"]),
                "inline configs are limited to the pinned nginx gateway")
    targets = [mount["target"] for mount in service.get("volumes", [])]
    targets.extend("/run/secrets/" + name for name in secrets)
    for config in configs:
        require(isinstance(config, dict) and set(config) <= {"source", "target", "mode"}
                and config.get("source") in manifest.get("configs", {})
                and config.get("target") in {"/etc/nginx/nginx.conf", "/etc/nginx/conf.d/default.conf"},
                "inline config target outside reviewed non-code allowlist")
        targets.append(config["target"])
    require(len(targets) == len(set(targets)), "duplicate service mount target")


def numeric_user(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9]+:[0-9]+", value),
            "application launch identity requires numeric UID:GID")
    uid, gid = (int(part) for part in value.split(":"))
    require(max(uid, gid) < 2**32 - 1, "invalid numeric launch identity")
    return uid, gid


def supplementary_groups(service):
    groups = service.get("group_add", [])
    require(isinstance(groups, list) and all(type(group) in (str, int) and re.fullmatch(r"[0-9]+", str(group))
                                          and int(group) < 2**32 - 1 for group in groups),
            "supplementary groups must be explicit numeric GIDs")
    normalized = sorted(str(int(group)) for group in groups)
    require(len(normalized) == len(set(normalized)), "duplicate supplementary group")
    return normalized


def security_options(values):
    require(isinstance(values, list) and all(value in {"no-new-privileges", "no-new-privileges:true"} for value in values)
            and len(values) <= 1, "unsupported runtime security option")
    return ["no-new-privileges:true"] if values else []


def validate_permissions(service):
    if "user" in service:
        numeric_user(service["user"])
    supplementary_groups(service)
    require(service.get("cap_add", []) == [] and service.get("cap_drop", []) in ([], ["ALL"]),
            "added or unsupported runtime capabilities forbidden")
    security_options(service.get("security_opt", []))


def working_directory(service, defaults):
    value = service.get("working_dir", defaults.get("WorkingDir") or "")
    require(isinstance(value, str) and (not value or (value.startswith("/")
            and not value.startswith("//") and os.path.normpath(value) == value
            and not any(char in value for char in "\r\n\x00"))), "invalid runtime working directory")
    require(service.get("image") != IMAGE or value == "/app", "application working directory must be /app")
    return value


def health_duration(value):
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ns|us|ms|s|m|h)", str(value))
    require(match is not None, "health duration requires a single explicit time unit")
    amount = Decimal(match[1]) * {"ns": 1, "us": 1000, "ms": 10**6, "s": 10**9,
                                  "m": 60 * 10**9, "h": 3600 * 10**9}[match[2]]
    require(amount == int(amount) and 0 <= amount < 2**63, "invalid health duration")
    return int(amount)


def configured_healthcheck(service):
    check = service.get("healthcheck", {})
    require(isinstance(check, dict) and set(check) <= {
        "test", "interval", "timeout", "start_period", "start_interval", "retries", "disable"},
        "unsupported healthcheck configuration")
    require(type(check.get("disable", False)) is bool and not (check.get("disable") and "test" in check),
            "ambiguous healthcheck disable configuration")
    test = ["NONE"] if check.get("disable") else check.get("test", [])
    require(isinstance(test, list) and all(isinstance(item, str) and "\x00" not in item for item in test)
            and (not test or (test[0] == "CMD" and len(test) > 1)
                 or (test[0] == "CMD-SHELL" and len(test) == 2) or test == ["NONE"]),
            "healthcheck test requires explicit command syntax")
    result = {"Test": test}
    for field, key in (("interval", "Interval"), ("timeout", "Timeout"),
                       ("start_period", "StartPeriod"), ("start_interval", "StartInterval")):
        result[key] = health_duration(check[field]) if field in check else 0
    require("start_interval" not in check or "start_period" in check,
            "health start interval requires explicit start period")
    retries = check.get("retries", 0)
    require(type(retries) is int and 0 <= retries < 2**31, "invalid health retry count")
    result["Retries"] = retries
    return result


def normalized_healthcheck(value):
    value = value or {}
    require(isinstance(value, dict) and set(value) <= {
        "Test", "Interval", "Timeout", "StartPeriod", "StartInterval", "Retries"},
        "unsupported image or runtime healthcheck")
    result = {"Test": value.get("Test") or []}
    require(isinstance(result["Test"], list) and all(isinstance(item, str) for item in result["Test"]),
            "invalid image or runtime healthcheck command")
    for key in ("Interval", "Timeout", "StartPeriod", "StartInterval", "Retries"):
        result[key] = value.get(key, 0)
        require(type(result[key]) is int and result[key] >= 0, "invalid image or runtime healthcheck duration")
    return result


def expected_healthcheck(service, defaults):
    # Moby merge inherits each empty/zero field independently, including when
    # Compose disables the test. Compare stored configuration, not only status.
    base = normalized_healthcheck(defaults.get("Healthcheck"))
    supplied = configured_healthcheck(service)
    return {key: supplied[key] or value for key, value in base.items()}


def validate_manifest(manifest, review, env_values, runtime_path):
    require(isinstance(manifest, dict) and set(manifest) <= {
        "name", "services", "volumes", "networks", "secrets", "configs"}, "unsupported manifest field")
    require(manifest.get("name") == review["project"]
            and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", review["project"]), "project mismatch")
    services = manifest.get("services")
    require(isinstance(services, dict) and services, "missing services")
    volumes = manifest.get("volumes", {})
    networks = manifest.get("networks", {})
    for resources in (volumes, networks):
        require(isinstance(resources, dict), "invalid external resources")
        for definition in resources.values():
            require(isinstance(definition, dict) and set(definition) == {"external", "name"}
                    and definition["external"] is True
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", definition["name"]),
                    "private release requires exact existing external resources")
    secret_refs = []
    for name, definition in manifest.get("secrets", {}).items():
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name), "invalid secret reference name")
        require(isinstance(definition, dict) and set(definition) == {"file"},
                "secret must be an absolute file reference")
        secret_data, record = private_file(definition["file"])
        payload = secret_data[:-1] if secret_data.endswith(b"\n") else secret_data
        require(32 <= len(payload) <= 4096 and all(0x21 <= value <= 0x7E for value in payload),
                "runtime secret must contain 32-4096 ASCII graphic bytes")
        # Secret bytes never become part of the descriptor. Pin inode and metadata
        # to reject replacement or rotation until a fresh review is sealed.
        record.pop("sha256")
        secret_refs.append(record)
    for definition in manifest.get("configs", {}).values():
        require(isinstance(definition, dict) and set(definition) == {"content"},
                "manifest config must be embedded and frozen")
    topology = {}
    apps = []
    used_env = set()
    for name, service in services.items():
        require(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", name)
                and isinstance(service, dict), "invalid service")
        require(not set(service) & {"build", "env_file", "extends", "volumes_from", "devices",
                                    "privileged", "pid", "ipc", "uts", "container_name"},
                "unsupported private service indirection")
        validate_permissions(service)
        require(set(service.get("deploy", {})) <= {"replicas"}
                and not set(service) & {"cpu_count", "cpu_percent", "cpu_period", "cpu_quota", "cpu_rt_runtime", "cpu_rt_period"},
                "unsupported deployment resource indirection")
        require(0 < memory_bytes(service.get("mem_limit", 0)) <= 768 * 1024**2
                and type(service.get("pids_limit")) is int and 0 < service["pids_limit"] <= 256,
                "each service requires bounded memory and PID limits")
        for field in ("command", "entrypoint"):
            require(service.get(field) is None or isinstance(service[field], (str, list)), "invalid runtime command")
        image = service.get("image", "")
        app = image == IMAGE
        require(app or re.fullmatch(r"[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}", image),
                "all service images must be immutable")
        if "working_dir" in service:
            require(service["working_dir"], "explicit working directory cannot be empty")
            working_directory(service, {})
        configured_healthcheck(service)
        environment = service.get("environment", {})
        require(isinstance(environment, dict), "environment must be a mapping")
        for key, value in environment.items():
            require(key not in HOST_RESERVED and not key.startswith(("LD_", "DYLD_", "PYTHON")),
                    "runtime loader environment override forbidden")
            require(isinstance(value, (str, int)) and not isinstance(value, bool), "invalid environment")
            value = str(value)
            match = REFERENCE.fullmatch(value)
            if match:
                used_env.add(match[1])
                require(match[1] in env_values, "missing private environment reference")
            else:
                require("$" not in value, "unsupported environment interpolation")
                if SENSITIVE.search(key) and not key.endswith("_FILE"):
                    require(value == "", "literal secret in deployment manifest")
            require("example.invalid" not in value, "example endpoint in private manifest")
        validate_attachments(service, manifest, app)
        canonical_mounts, config_count = validate_mounts(service, manifest, runtime_path, app)
        if app:
            apps.append(name)
            require(config_count == 1 and environment.get("AI_FILTER_CONFIG_PATH") == "/app/config.yaml",
                    "every app role must mount the frozen runtime config")
        service_networks = service.get("networks", [])
        require(isinstance(service_networks, list) and set(service_networks) <= set(networks)
                and service.get("network_mode", "none") == "none"
                and (service_networks or service.get("network_mode") == "none"),
                "unsupported or implicit new network")
        ports = service.get("ports", [])
        require(isinstance(ports, list), "ports must use long syntax")
        for port in ports:
            require(isinstance(port, dict) and set(port) == {"target", "published", "host_ip", "protocol"}
                    and port["protocol"] in {"tcp", "udp"} and str(port["published"]).isdigit()
                    and isinstance(port["target"], int) and isinstance(port["host_ip"], str), "invalid port")
        topology[name] = {"mounts": canonical_mounts, "networks": service_networks, "ports": ports}
    require(apps and set(review["health_services"]) <= set(services)
            and review["health_services"], "invalid health services")
    require(topology == review["topology"], "reviewed mount, network, or port drift")
    require(set(review.get("service_mapping", {})) == set(services), "review requires explicit service mapping")
    for names in review["service_mapping"].values():
        require(isinstance(names, list) and names and len(set(names)) == len(names)
                and all(name in review["existing_topology"] for name in names), "invalid reviewed service mapping")
    validate_control_subpaths(manifest, review, apps)
    retained_custody({"review": review, "app_services": apps}, manifest)
    require(used_env == set(env_values), "environment references must exactly match private environment")
    # No interpolation in command, mount, label, secret, config, etc. Embedded
    # Nginx dollar variables are allowed only when escaped for Compose.
    for name, service in services.items():
        other = {k: v for k, v in service.items() if k not in {"image", "environment"}}
        require("$" not in json.dumps(other), "interpolation outside approved environment")
    for definition in manifest.get("configs", {}).values():
        require("$" not in definition["content"].replace("$$", ""), "unescaped embedded config interpolation")
    return sorted(apps), sorted(secret_refs, key=lambda item: item["path"])


def fresh(review):
    now = int(time.time())
    require(review.get("approved") is True and isinstance(review.get("approved_by"), str)
            and 0 < len(review["approved_by"]) <= 128
            and type(review.get("approved_at")) is int and type(review.get("expires_at")) is int
            and review["approved_at"] <= now <= review["expires_at"]
            and 0 < review["expires_at"] - review["approved_at"] <= MAX_AGE, "private review is absent or stale")


def write_sealed(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # Leave incomplete evidence; never overwrite or silently retry a seal.
        raise


def seal(root, config, manifest, environment, review, output):
    import yaml

    source = source_identity(root)
    inputs = {}
    data = {}
    for name, path in {"runtime.yaml": config, "input-compose.json": manifest,
                       "environment": environment, "review.json": review}.items():
        data[name], inputs[name] = private_file(path)
    policy = json.loads(data["review.json"])
    fresh(policy)
    require(all(policy.get(k) == v for k, v in source.items()), "reviewed source identity mismatch")
    for key, name in {"runtime_config_sha256": "runtime.yaml", "input_manifest_sha256": "input-compose.json",
                      "environment_sha256": "environment"}.items():
        require(policy.get(key) == sha(data[name]), "reviewed private input hash mismatch")
    require(isinstance(policy.get("strategy"), str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", policy["strategy"]),
            "invalid expected runtime strategy")
    require(isinstance(policy.get("existing_topology"), dict) and policy["existing_topology"],
            "review requires independently captured existing service identities")
    project_dir = safe_path(policy["project_directory"])
    require(project_dir.is_dir(), "project directory does not exist")
    cfg = yaml.safe_load(data["runtime.yaml"])
    require(isinstance(cfg, dict), "runtime config must be a mapping")
    reject_inline_credentials(cfg)
    require(cfg != yaml.safe_load((Path(root) / "config.yaml").read_bytes()),
            "runtime config must not reuse the public example")
    output = safe_path(output)
    private_directory(output.parent)
    require(not output.exists() and not output.is_relative_to(Path(root).resolve()),
            "private seal must be new and outside source checkout")
    deployment = json.loads(data["input-compose.json"])
    apps, refs = validate_manifest(deployment, policy, environment_values(data["environment"]), RUNTIME)
    custody = retained_custody({"review": policy, "app_services": apps}, deployment)
    if custody is not None:
        _helper, held = custody
        data.update(held["files"])
        inputs.update(held["inputs"])
    # Freeze the complete manifest, with one controlled substitution only.
    for service in deployment["services"].values():
        for mount in service.get("volumes", []):
            if mount.get("source") == RUNTIME:
                mount["source"] = str(output / "runtime.yaml")
    data["compose.json"] = encoded(deployment)
    directory_paths = {str(project_dir)}
    directory_paths.update(mount["source"] for service in deployment["services"].values()
                           for mount in service.get("volumes", [])
                           if mount["type"] == "bind" and mount["target"] != "/app/config.yaml")
    directory_refs = [directory_identity(path) for path in sorted(directory_paths)]
    contract = {"schema": SCHEMA, **source, "review": policy, "inputs": inputs,
                "app_services": apps, "secret_refs": refs, "directory_refs": directory_refs,
                "files": {name: sha(value) for name, value in data.items()}}
    output.mkdir(mode=0o700)
    for name, value in data.items():
        write_sealed(output / name, value)
    write_sealed(output / "contract.json", encoded(contract))
    output.chmod(0o500)
    for directory in (output, output.parent):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return output / "contract.json"


def load(path, root, *, require_fresh=False):
    path = safe_path(path)
    directory = private_directory(path.parent, sealed=True)
    data, _record = private_file(path, sealed=True)
    contract = json.loads(data)
    require(contract.get("schema") == SCHEMA, "unsupported private release contract")
    custody_files = {"custody-gateway-plan.json", "custody-configuration-proof.json"} if "retained_gateway_custody" in contract["review"] else set()
    require(set(contract["files"]) == {"runtime.yaml", "input-compose.json", "environment", "review.json", "compose.json"} | custody_files,
            "invalid frozen file inventory")
    files = {}
    for name, digest in contract["files"].items():
        files[name], _metadata = private_file(directory / name, sealed=True)
        require(sha(files[name]) == digest, "frozen private input drift")
    require(json.loads(files["review.json"]) == contract["review"], "private review drift")
    for name, expected in contract["inputs"].items():
        require(name in {"runtime.yaml", "input-compose.json", "environment", "review.json"} | custody_files, "invalid input inventory")
        _data, actual = private_file(expected["path"])
        require(expected == actual, "private source input changed after sealing")
    require(set(contract["inputs"]) == {"runtime.yaml", "input-compose.json", "environment", "review.json"} | custody_files,
            "incomplete private input inventory")
    for expected in contract["directory_refs"]:
        require(directory_identity(expected["path"]) == expected, "deployment directory identity drift")
    for expected in contract["secret_refs"]:
        _secret, actual = private_file(expected["path"])
        actual.pop("sha256")
        require(expected == actual, "secret reference metadata drift")
    source = source_identity(root)
    require(all(contract.get(key) == value for key, value in source.items()), "private source identity drift")
    policy = contract["review"]
    if require_fresh:
        fresh(policy)
    require(safe_path(policy["project_directory"]).is_dir(), "project path drift")
    manifest = json.loads(files["compose.json"])
    apps, refs = validate_manifest(manifest, policy, environment_values(files["environment"]), str(directory / "runtime.yaml"))
    expected_directories = {policy["project_directory"]}
    expected_directories.update(mount["source"] for service in manifest["services"].values()
                                for mount in service.get("volumes", [])
                                if mount["type"] == "bind" and mount["target"] != "/app/config.yaml")
    require({item["path"] for item in contract["directory_refs"]} == expected_directories,
            "deployment directory inventory drift")
    require(apps == contract["app_services"] and refs == contract["secret_refs"], "private runtime contract drift")
    custody = retained_custody(contract, manifest)
    if custody is not None:
        _helper, held = custody
        require(all(held["files"][name] == files[name] and held["inputs"][name] == contract["inputs"][name]
                    for name in custody_files), "frozen retained custody input drift")
    return contract, manifest, sha(data)


def docker_environment(directory, image):
    env = {name: os.environ[name] for name in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                                             "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH") if name in os.environ}
    # Private interpolation values belong only to Compose --env-file and to
    # container --env-file. They must never influence the host process loader,
    # executable lookup, Docker credentials, or shell startup.
    environment_values(private_file(directory / "environment", sealed=True)[0])
    env["IMAGE_REF"] = image
    return env


def compose(contract, directory, image, args):
    policy = contract["review"]
    return run(["docker", "compose", "--project-name", policy["project"], "--project-directory",
                policy["project_directory"], "--env-file", str(directory / "environment"), *args],
               env=docker_environment(directory, image))


def role_environment(service, defaults, values):
    result = runtime_environment(defaults.get("Env"))
    for key, expected in service.get("environment", {}).items():
        match = REFERENCE.fullmatch(str(expected))
        result[key] = values[match[1]] if match else str(expected)
    require(all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in result)
            and all(not any(char in value for char in "\r\n\x00") for value in result.values()),
            "role environment cannot be represented without record injection")
    return result


@contextmanager
def probe_environment_file(directory, environment):
    # Docker's env-file contains the resolved role names, not interpolation
    # aliases. Values never enter host environment or process arguments.
    descriptor, name = tempfile.mkstemp(prefix=".private-role-probe-", dir=directory.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write("".join(f"{key}={value}\n" for key, value in sorted(environment.items())).encode())
            handle.flush()
            os.fsync(handle.fileno())
        yield name
    finally:
        Path(name).unlink(missing_ok=True)


def image_probe(contract, directory, digest, *, runner=None):
    runner = run if runner is None else runner
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", digest), "invalid image digest")
    manifest = json.loads(private_file(directory / "compose.json", sealed=True)[0])
    info = json.loads(runner(["docker", "image", "inspect", digest]))
    require(len(info) == 1 and info[0]["Id"] == digest, "image probe identity mismatch")
    defaults = info[0]["Config"]
    values = environment_values(private_file(directory / "environment", sealed=True)[0])
    for service_name in contract["app_services"]:
        service = manifest["services"][service_name]
        user = service.get("user", defaults.get("User") or "0:0")
        uid, _gid = numeric_user(user)
        _config, metadata = private_file(directory / "runtime.yaml", sealed=True)
        require(uid == metadata["uid"], "runtime UID does not own the private config")
        mounts = []
        for name in service.get("secrets", []):
            reference = manifest["secrets"][name]
            _secret, metadata = private_file(reference["file"])
            require(uid == metadata["uid"], "runtime UID does not own its private secret")
            mounts += ["--mount", f"type=bind,src={reference['file']},dst=/run/secrets/{name},readonly"]
        groups = [item for group in supplementary_groups(service) for item in ("--group-add", group)]
        workdir = working_directory(service, defaults)
        with probe_environment_file(directory, role_environment(service, defaults, values)) as environment:
            probe = json.loads(runner(["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
                                   "--user", user, *groups, "--workdir", workdir, "--security-opt", "no-new-privileges",
                                   "--memory", "768m", "--pids-limit", "256", "--env-file", environment,
                                   "--mount", f"type=bind,src={directory / 'runtime.yaml'},dst=/app/config.yaml,readonly", *mounts,
                                   "--entrypoint", "python", digest, "-c", PROBE]))
        require(probe == {"config_sha256": contract["files"]["runtime.yaml"], "strategy": contract["review"]["strategy"]},
                "image runtime config or strategy mismatch")


def expected_topology(manifest):
    writable = set()
    networks = set()
    ports = set()
    for service in manifest["services"].values():
        if service.get("profiles"):
            continue
        for mount in service.get("volumes", []):
            if not mount.get("read_only", False):
                source = mount["source"] if mount["type"] == "bind" else manifest["volumes"][mount["source"]]["name"]
                writable.add((mount["type"], source, mount["target"]))
        networks.update(manifest["networks"][name]["name"] for name in service.get("networks", []))
        ports.update((str(port["host_ip"]), str(port["published"]), f"{port['target']}/{port['protocol']}")
                     for port in service.get("ports", []))
    return writable, networks, ports


def inspect_project(contract):
    project = contract["review"]["project"]
    ids = run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}", "--format", "{{.ID}}"])
    identifiers = ids.decode().split()
    require(identifiers and all(re.fullmatch(r"[0-9a-f]{12,64}", value) for value in identifiers),
            "existing Compose project is missing")
    result = json.loads(run(["docker", "inspect", *identifiers]))
    require(isinstance(result, list) and len(result) == len(identifiers), "incomplete project inspection")
    for container in result:
        labels = container["Config"]["Labels"]
        require(labels.get("com.docker.compose.project") == project
                and labels.get("com.docker.compose.project.working_dir") == contract["review"]["project_directory"],
                "existing project identity or directory mismatch")
    return result


def runtime_topology(containers):
    result = {}
    for container in containers:
        labels = container["Config"]["Labels"]
        if labels.get("com.docker.compose.oneoff", "False").lower() == "true":
            require(not container["State"]["Running"], "existing one-off producer is running")
            continue
        name = labels.get("com.docker.compose.service")
        require(name, "missing runtime service identity")
        mounts = [{"type": mount["Type"],
                   "source": mount.get("Name") if mount["Type"] == "volume" else mount["Source"],
                   "target": mount["Destination"], "read_only": not mount["RW"]}
                  for mount in container["Mounts"]]
        ports = [{"target": target, "host_ip": binding["HostIp"], "published": binding["HostPort"]}
                 for target, bindings in container["HostConfig"].get("PortBindings", {}).items()
                 for binding in bindings or []]
        result.setdefault(name, []).append({"id": container["Id"], "image": container["Image"],
                                             "mounts": sorted(mounts, key=lambda item: item["target"]),
                                             "networks": sorted(container["NetworkSettings"]["Networks"]),
                                             "user": container["Config"].get("User") or "",
                                             "group_add": sorted(container["HostConfig"].get("GroupAdd") or []),
                                             "ports": sorted(ports, key=lambda item: encoded(item))})
    return {name: sorted(items, key=lambda item: item["id"]) for name, items in result.items()}


def preflight(contract, manifest):
    containers = inspect_project(contract)
    require(runtime_topology(containers) == contract["review"].get("existing_topology"),
            "reviewed existing service identity or topology drift")
    custody = retained_custody(contract, manifest)
    gateway_id = None
    if custody is not None:
        helper, held = custody
        try:
            helper.verify_records(held, contract["review"], containers)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ContractError("retained gateway runtime identity differs") from None
        gateway_id = held["declaration"]["old_container_id"]
    writable, networks, ports = set(), set(), set()
    for container in containers:
        if container["Id"] == gateway_id:
            continue
        if container["Config"]["Labels"].get("com.docker.compose.oneoff", "False").lower() == "true":
            require(not container["State"]["Running"], "existing one-off producer is running")
            continue
        writable.update((mount["Type"], mount.get("Name") if mount["Type"] == "volume" else mount["Source"],
                         mount["Destination"]) for mount in container["Mounts"] if mount["RW"])
        networks.update(container["NetworkSettings"]["Networks"])
        for target, bindings in container["HostConfig"].get("PortBindings", {}).items():
            ports.update((binding["HostIp"], binding["HostPort"], target) for binding in bindings or [])
    for name, service in manifest["services"].items():
        permitted = {(mount["type"], mount["source"], mount["target"])
                     for previous in contract["review"]["service_mapping"][name]
                     for item in contract["review"]["existing_topology"][previous]
                     for mount in item["mounts"] if not mount["read_only"]}
        requested = expected_topology({**manifest, "services": {name: {**service, "profiles": []}}})[0]
        require(requested <= permitted, "candidate data mount differs from reviewed role mapping")
    require((writable, networks, ports) == expected_topology(manifest),
            "existing project data mounts, networks, or ports differ from reviewed deployment")
    for kind, definitions in (("volume", manifest.get("volumes", {})), ("network", manifest.get("networks", {}))):
        for definition in definitions.values():
            info = json.loads(run(["docker", kind, "inspect", definition["name"]]))
            require(len(info) == 1 and info[0]["Name"] == definition["name"], "existing resource is missing")
            if custody is not None and kind == "network" and definition["name"] in held["configuration"]["container_identity"]["networks"]:
                require(info[0]["Id"] == held["configuration"]["network_id"], "retained gateway network identity differs")


def memory_bytes(value):
    if type(value) is int:
        return value
    match = re.fullmatch(r"([0-9]+)([bkmg]?)", str(value).lower())
    require(match is not None, "memory limit must use explicit integer bytes or b/k/m/g units")
    return int(match[1]) * {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[match[2]]


def runtime_environment(entries):
    pairs = [item.split("=", 1) for item in entries or []]
    require(all(len(pair) == 2 for pair in pairs) and len({pair[0] for pair in pairs}) == len(pairs),
            "duplicate runtime environment key")
    return dict(pairs)


def command_vector(value):
    if value is None:
        return []
    result = shlex.split(value) if isinstance(value, str) else value
    require(isinstance(result, list) and all(isinstance(item, str) for item in result), "invalid runtime command")
    return result


def verify_execution(service, container, image_info, values, *, expected_running=True):
    require(container["Image"] == image_info["Id"] and container["State"]["Running"] is expected_running, "runtime image identity mismatch")
    if expected_running is False:
        require(container["State"].get("Status") == "created" and container.get("RestartCount") == 0
                and container["State"].get("StartedAt") == "0001-01-01T00:00:00Z"
                and not container["State"].get("Restarting") and not container["State"].get("Dead"),
                "initial runtime container has already started or has unknown state")
    actual = container["Config"]
    defaults = image_info["Config"]
    require((actual.get("WorkingDir") or "") == working_directory(service, defaults),
            "runtime working directory mismatch")
    require(normalized_healthcheck(actual.get("Healthcheck")) == expected_healthcheck(service, defaults),
            "runtime healthcheck configuration mismatch")
    for field, key in (("command", "Cmd"), ("entrypoint", "Entrypoint")):
        wanted = service.get(field) if service.get(field) is not None else defaults.get(key)
        require(command_vector(actual.get(key)) == command_vector(wanted), "runtime command or entrypoint mismatch")
    require(runtime_environment(actual.get("Env")) == role_environment(service, defaults, values),
            "runtime environment or role differs from frozen references")
    resources = container["HostConfig"]
    expected_user = service.get("user", defaults.get("User") or "")
    numeric_user(expected_user or "0:0")
    require((actual.get("User") or "") == expected_user
            and sorted(resources.get("GroupAdd") or []) == supplementary_groups(service),
            "runtime user or supplementary groups mismatch")
    require(resources.get("Privileged", False) is False and not resources.get("CapAdd")
            and (resources.get("CapDrop") or []) == service.get("cap_drop", [])
            and security_options(resources.get("SecurityOpt") or []) == security_options(service.get("security_opt", [])),
            "runtime privileges differ from frozen manifest")
    for field, key in (("mem_limit", "Memory"), ("mem_reservation", "MemoryReservation"), ("memswap_limit", "MemorySwap")):
        if field == "memswap_limit" and field not in service:
            # Docker's ordinary default permits swap equal to the memory limit.
            expected = 2 * memory_bytes(service["mem_limit"])
        else:
            expected = memory_bytes(service.get(field, 0))
        require(resources.get(key, 0) == expected, "runtime memory limit mismatch")
    require(resources.get("PidsLimit") == service["pids_limit"], "runtime PID limit mismatch")
    require(resources.get("NanoCpus", 0) == int(Decimal(str(service.get("cpus", 0))) * 10**9)
            and resources.get("CpusetCpus", "") == service.get("cpuset", "")
            and resources.get("CpuShares", 0) == service.get("cpu_shares", 0), "runtime CPU limit mismatch")


def verify_service_execution(contract, manifest, containers, directory, digest):
    values = environment_values(private_file(directory / "environment", sealed=True)[0])
    inspected = {}
    for container in containers:
        labels = container["Config"]["Labels"]
        if labels.get("com.docker.compose.oneoff", "False").lower() == "true":
            continue
        name = labels.get("com.docker.compose.service")
        require(name in manifest["services"], "unexpected runtime service")
        service = manifest["services"][name]
        image_ref = digest if name in contract["app_services"] else service["image"]
        if image_ref not in inspected:
            info = json.loads(run(["docker", "image", "inspect", image_ref]))
            require(len(info) == 1, "missing immutable runtime image")
            inspected[image_ref] = info[0]
        verify_execution(service, container, inspected[image_ref], values)


def verify_app_loaders(contract, manifest, containers, directory, digest):
    by_service = {}
    for container in containers:
        labels = container["Config"]["Labels"]
        if labels.get("com.docker.compose.oneoff", "False").lower() != "true":
            by_service.setdefault(labels.get("com.docker.compose.service"), []).append(container)
    for name in contract["app_services"]:
        service = manifest["services"][name]
        if service.get("profiles"):
            continue
        require(name in by_service and by_service[name], "missing runtime app service")
        for container in by_service[name]:
            require(container["Image"] == digest and container["State"]["Running"], "runtime image identity mismatch")
            mounts = [mount for mount in container["Mounts"] if mount["Destination"] == "/app/config.yaml"]
            require(len(mounts) == 1 and mounts[0]["Type"] == "bind" and mounts[0]["RW"] is False
                    and mounts[0]["Source"] == str(directory / "runtime.yaml"), "runtime config mount mismatch")
            entries = [item.split("=", 1) for item in container["Config"]["Env"]]
            require(all(len(item) == 2 for item in entries)
                    and len({item[0] for item in entries}) == len(entries), "duplicate runtime environment key")
            env = dict(entries)
            require(env.get("AI_FILTER_CONFIG_PATH") == "/app/config.yaml", "runtime config path mismatch")
            values = environment_values(private_file(directory / "environment", sealed=True)[0])
            for key, expected in service.get("environment", {}).items():
                match = REFERENCE.fullmatch(str(expected))
                expected = values[match[1]] if match else str(expected)
                require(env.get(key) == expected, "runtime environment differs from frozen references")
            probe = json.loads(run(["docker", "exec", container["Id"], "python", "-c", PROBE]))
            require(probe == {"config_sha256": contract["files"]["runtime.yaml"], "strategy": contract["review"]["strategy"]},
                    "runtime config or strategy mismatch")


def verify_runtime(contract, manifest, directory, digest):
    _verify_runtime_records(contract, manifest, directory, digest, inspect_project(contract))


def _verify_runtime_records(contract, manifest, directory, digest, containers):
    """All execution checks; caller must bind the complete approved runtime set."""
    verify_service_execution(contract, manifest, containers, directory, digest)
    actual_topology = runtime_topology(containers)
    expected_names = {name for name, service in manifest["services"].items() if not service.get("profiles")}
    require(set(actual_topology) == expected_names, "unexpected runtime service")
    for name in expected_names:
        service = manifest["services"][name]
        expected_mounts = []
        for mount in service.get("volumes", []):
            source = mount["source"] if mount["type"] == "bind" else manifest["volumes"][mount["source"]]["name"]
            expected_mounts.append({"type": mount["type"], "source": source, "target": mount["target"],
                                    "read_only": mount.get("read_only", False)})
        for secret in service.get("secrets", []):
            expected_mounts.append({"type": "bind", "source": manifest["secrets"][secret]["file"],
                                    "target": "/run/secrets/" + secret, "read_only": True})
        networks = sorted(manifest["networks"][key]["name"] for key in service.get("networks", []))
        ports = sorted([{"target": f"{port['target']}/{port['protocol']}", "host_ip": port["host_ip"],
                         "published": str(port["published"])} for port in service.get("ports", [])], key=lambda item: encoded(item))
        replicas = service.get("deploy", {}).get("replicas", 1)
        require(type(replicas) is int and 1 <= replicas <= 16 and len(actual_topology[name]) == replicas,
                "runtime service replica mismatch")
        for actual in actual_topology[name]:
            # Inline Compose configs use engine-selected host sources. Bind the
            # target/read-only status and hash the actual bytes inside its role.
            observed = list(actual["mounts"])
            for config in service.get("configs", []):
                matches = [mount for mount in observed if mount["target"] == config["target"]]
                require(len(matches) == 1 and matches[0]["read_only"], "runtime inline config mount mismatch")
                observed.remove(matches[0])
                content = manifest["configs"][config["source"]]["content"].replace("$$", "$").encode()
                value = run(["docker", "exec", actual["id"], "sha256sum", config["target"]]).decode().split()[0]
                require(value == sha(content), "runtime inline config bytes mismatch")
            require(sorted(observed, key=lambda item: item["target"]) == sorted(expected_mounts, key=lambda item: item["target"])
                    and actual["networks"] == networks and actual["ports"] == ports,
                    "runtime service mount, network, or port mismatch")
    # Verify imports' execution context and all actual mounts before invoking
    # the config loader, so an unreviewed writable directory cannot supply it.
    verify_app_loaders(contract, manifest, containers, directory, digest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("seal", "check", "field", "compose", "image-probe", "preflight", "verify-runtime"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--contract")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--field", choices=("sha256", "manifest", "health_services", "runtime_config_sha256"))
    parser.add_argument("--image", default=os.environ.get("IMAGE_REF", ""))
    for name in ("config", "manifest", "environment", "review", "output"):
        parser.add_argument("--" + name)
    args, remainder = parser.parse_known_args()
    try:
        if args.command == "seal":
            seal(args.root, args.config, args.manifest, args.environment, args.review, args.output)
            print("Private release contract sealed")
            return
        contract, manifest, digest = load(args.contract, args.root, require_fresh=args.fresh)
        directory = Path(args.contract).parent
        if args.command == "field":
            print({"sha256": digest, "manifest": str(directory / "compose.json"),
                   "health_services": " ".join(contract["review"]["health_services"]),
                   "runtime_config_sha256": contract["files"]["runtime.yaml"]}[args.field])
        elif args.command == "compose":
            require(remainder[:1] == ["--"], "invalid private Compose arguments")
            sys.stdout.buffer.write(compose(contract, directory, args.image, remainder[1:]))
        elif args.command == "image-probe":
            image_probe(contract, directory, args.image)
        elif args.command == "preflight":
            preflight(contract, manifest)
        elif args.command == "verify-runtime":
            verify_runtime(contract, manifest, directory, args.image)
    except ContractError as error:
        print(f"Private release refused: {error}", file=sys.stderr)
        raise SystemExit(3) from None
    except Exception:
        print("Private release refused: invalid or unavailable private evidence", file=sys.stderr)
        raise SystemExit(3) from None


if __name__ == "__main__":
    main()
