"""Finite, source-bound ingress stop driver, executable only through isolation APIs.

No production CLI, rollout, resume, kill or cleanup is implemented. Native
transport collects real Docker inspect and crontab/file evidence, then uses
authenticated loopback controls. Inspect alone never proves ingress stopped.
"""
from __future__ import annotations

import hashlib
import http.client
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import time
from urllib.parse import quote, urlsplit

from durable_handoff_cutover import encoded, parsed, private_directory, read_private, require, durable_write
from handoff_host_cron import HostCronGate, locked, present

ENTRANCES = (
    "miniflux_scheduler", "miniflux_manual", "legacy_async_sender", "webhook", "webhook_bypass",
    "manual_refresh", "filter", "filter_multi", "briefing_llm", "feedback", "management_writes",
    "natural_cron", "enrichment_cron", "cron_maintenance", "market_close", "security_master", "outside_compose",
)
MAX_RAW_BYTES = 4 * 1024 * 1024
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
CONTROL = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


def sha(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def _integer(value, low=0, high=2**63 - 2):
    return type(value) is int and low <= value <= high


def _checked_plan(plan):
    keys = {"schema", "scope", "run_id", "not_before", "expires_at", "components", "controls", "cron"}
    require(isinstance(plan, dict) and set(plan) == keys and type(plan["schema"]) is int
            and plan["schema"] == 1 and plan["scope"] == "isolation", "explicit isolation plan required")
    require(re.fullmatch(r"[0-9a-f]{32}", plan["run_id"]) and _integer(plan["not_before"], 1)
            and _integer(plan["expires_at"], 1) and 0 < plan["expires_at"] - plan["not_before"] <= 600, "invalid finite run")
    require(isinstance(plan["components"], dict) and 1 <= len(plan["components"]) <= 8, "bounded actual components required")
    for name, item in plan["components"].items():
        _component(name, item)
    require(isinstance(plan["controls"], dict) and 1 <= len(plan["controls"]) <= 32, "finite controls required")
    entrances = []
    for control_id, binding in plan["controls"].items():
        require(CONTROL.fullmatch(control_id) and set(binding) == {"entrances", "binding_sha256", "source_sha256", "generation"}
                and HEX64.fullmatch(binding["binding_sha256"]) and HEX64.fullmatch(binding["source_sha256"])
                and _integer(binding["generation"]), "invalid bound control")
        require(isinstance(binding["entrances"], list) and binding["entrances"], "control has no entrance")
        entrances.extend(binding["entrances"])
    require(sorted(entrances) == sorted(ENTRANCES), "missing, duplicate or unknown producer entrance")
    _cron_plan(plan["cron"], plan["controls"])
    return plan


def _component(name, item, *, allow_external_postgres=False):
    keys = {"id", "image", "revision", "started_at", "restart_count", "role", "user", "memory", "pids", "mounts"}
    external = isinstance(item, dict) and "external_postgres" in item
    require(CONTROL.fullmatch(name) and isinstance(item, dict)
            and set(item) == keys | ({"external_postgres"} if external else set()), "exact component fields required")
    require(not external or (allow_external_postgres and item["role"] == "miniflux"), "external PostgreSQL policy scope differs")
    require(HEX64.fullmatch(item["id"]) and re.fullmatch(r"sha256:[0-9a-f]{64}", item["image"])
            and re.fullmatch(r"[0-9a-f]{40}", item["revision"]) and isinstance(item["started_at"], str)
            and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z", item["started_at"])
            and _integer(item["restart_count"]) and isinstance(item["role"], str) and item["role"]
            and isinstance(item["user"], str) and re.fullmatch(r"\d+(?::\d+)?", item["user"])
            and _integer(item["memory"], 1, 768 * 1024 * 1024) and _integer(item["pids"], 1, 256), "bad component identity/limits")
    require(isinstance(item["mounts"], list) and (item["mounts"] == [] if external else 1 <= len(item["mounts"]) <= 16),
            "bound shared mounts required")
    if external:
        from services.sender_drain_contract import validate_external_postgres
        validate_external_postgres(item["external_postgres"])
    for mount in item["mounts"]:
        require(set(mount) == {"Type", "Source", "Destination", "RW"} and mount["Type"] in {"bind", "volume"}
                and Path(mount["Source"]).is_absolute() and Path(mount["Destination"]).is_absolute()
                and type(mount["RW"]) is bool, "mount binding differs")


def _cron_plan(cron, controls):
    require(isinstance(cron, dict) and set(cron) in ({"crontab_sha256", "wrappers", "jobs"},
            {"crontab_sha256", "wrappers", "jobs", "legacy_binding"})
            and HEX64.fullmatch(cron["crontab_sha256"]) and isinstance(cron["wrappers"], dict)
            and 1 <= len(cron["wrappers"]) <= 16 and isinstance(cron["jobs"], list)
            and 1 <= len(cron["jobs"]) <= 32, "cron coverage required")
    for path, identity in cron["wrappers"].items():
        require(Path(path).is_absolute() and set(identity) == {"sha256", "uid", "mode"}
                and HEX64.fullmatch(identity["sha256"]) and _integer(identity["uid"])
                and identity["mode"] in (0o600, 0o700) and type(identity["mode"]) is int, "unsafe installed wrapper")
    for job in cron["jobs"]:
        require(set(job) == {"line", "control_id"} and job["control_id"] in controls
                and isinstance(job["line"], str) and "\n" not in job["line"], "job lacks installed controlled wrapper")
        _cron_command(job["line"], cron["wrappers"])


def _cron_lines(raw):
    require(isinstance(raw, str) and len(raw.encode()) <= 65536, "bounded complete crontab required")
    return [line for line in raw.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def legacy_cron_binding(original, installed, replacements, wrappers, *, gate_factory=HostCronGate):
    """Bind every old line byte-for-byte to an actual registered child argv.

    Only a supplied private isolation archive is read; no crontab is installed.
    Same command names or task counts are insufficient. Shell syntax and special
    schedules remain unsupported, rather than silently reinterpreted.
    """
    old_lines, new_lines = _cron_lines(original), _cron_lines(installed)
    environment = re.compile(r"(?:SHELL|PATH|MAILTO|TZ|CRON_TZ)=.*")
    old_env = [line for line in old_lines if environment.fullmatch(line)]
    new_env = [line for line in new_lines if environment.fullmatch(line)]
    require(old_env == new_env, "cron execution environment changed")
    old_jobs = [line for line in old_lines if not environment.fullmatch(line)]
    new_jobs = [line for line in new_lines if not environment.fullmatch(line)]
    require(isinstance(replacements, list) and 0 < len(replacements) <= 32
            and len(old_jobs) == len(new_jobs) == len(replacements), "old cron coverage incomplete")
    require(all(isinstance(row, dict) and set(row) == {"old_line", "new_line", "control_id"}
                for row in replacements), "exact old/new mapping required")
    require(sorted(row["old_line"] for row in replacements) == sorted(old_jobs)
            and sorted(row["new_line"] for row in replacements) == sorted(new_jobs), "old cron line missing/changed/duplicated")
    commands = []
    for row in replacements:
        require(not any(char in row["old_line"] for char in (";", "|", "&", ">", "<", "`", "$", "\x00")), "legacy shell syntax unsupported")
        argv = shlex.split(row["old_line"])
        require(len(argv) >= 6 and all(re.fullmatch(r"[0-9*/,-]+", x) for x in argv[:5])
                and Path(argv[5]).is_absolute(), "unknown old cron command")
        root, command = _cron_command(row["new_line"], wrappers)
        require(shlex.split(row["new_line"])[:5] == argv[:5], "old cron schedule changed")
        gate = gate_factory(root)
        require(gate.manifest["control_id"] == row["control_id"]
                and gate.manifest["commands"].get(command) == argv[5:], "old command is not actual registered argv")
        commands.append({**row, "command": command, "argv_sha256": sha(argv[5:]), "registration": gate.registration()})
    return {"schema": 1, "scope": "isolation", "original_crontab": original, "installed_crontab": installed,
            "original_sha256": hashlib.sha256(original.encode()).hexdigest(),
            "installed_sha256": hashlib.sha256(installed.encode()).hexdigest(), "replacements": commands,
            "installed_on_production": False}


def _cron_command(line, wrappers):
    args = shlex.split(line)
    require(len(args) == 13 and all(re.fullmatch(r"[0-9*/,-]+", value) for value in args[:5])
            and Path(args[5]).is_absolute() and args[6] == "-B" and args[7] in wrappers
            and args[8] == "--root" and Path(args[9]).is_absolute()
            and args[10] == "--command" and CONTROL.fullmatch(args[11])
            and args[12] == "--isolation", "cron must invoke the exact isolated registered wrapper")
    return args[9], args[11]


def inspect_binding(rows, expected, *, allow_external_postgres=False):
    require(isinstance(rows, list) and len(rows) == len(expected), "inspect component count differs")
    observed = {}
    for row in rows:
        name = str(row["Name"]).lstrip("/")
        require(name in expected and name not in observed, "unknown/duplicate actual container")
        env = {}
        for value in row["Config"].get("Env", []):
            key, _, value = value.partition("=")
            require(key not in env, "duplicate environment binding")
            env[key] = value
        role = env.get("AI_FILTER_ROLE") or row["Config"]["Labels"].get("io.ai-filter.handoff.role")
        state = row["State"]
        require(state["Running"] is True and state.get("OOMKilled") is False
                and state.get("Paused") is False and state.get("Restarting") is False, "runtime not stable/running")
        raw_user = row["Config"].get("User", "") or "0"
        value = {"id": row["Id"], "image": row["Image"], "revision": row["Config"]["Labels"].get("org.opencontainers.image.revision"),
                 "started_at": state["StartedAt"], "restart_count": row["RestartCount"], "role": role, "user": raw_user,
                 "memory": row["HostConfig"]["Memory"], "pids": row["HostConfig"]["PidsLimit"],
                 "mounts": [{key: mount[key] for key in ("Type", "Source", "Destination", "RW")} for mount in row["Mounts"]]}
        if "external_postgres" in expected[name]:
            from services.sender_drain_contract import external_postgres_identity
            require(row["Mounts"] == [] and role == "miniflux" and not env.get("DATABASE_URL_FILE"),
                    "actual external PostgreSQL mount policy differs")
            value["external_postgres"] = external_postgres_identity(env.get("DATABASE_URL"), row["HostConfig"].get("ReadonlyRootfs"))
        _component(name, value, allow_external_postgres=allow_external_postgres)
        value["mounts"] = sorted(value["mounts"], key=lambda m: m["Destination"])
        bound = {**expected[name], "mounts": sorted(expected[name]["mounts"], key=lambda m: m["Destination"])}
        require(value == bound, "actual runtime image/role/mount/limits identity changed")
        observed[name] = value
    return observed


def coverage_binding(raw, plan):
    keys = {"crontab", "wrappers", "registrations"}
    if "legacy_binding" in plan["cron"]:
        keys.add("legacy_binding")
    require(isinstance(raw, dict) and set(raw) == keys, "raw coverage missing")
    require(isinstance(raw["crontab"], str) and len(raw["crontab"].encode()) <= 65536
            and hashlib.sha256(raw["crontab"].encode()).hexdigest() == plan["cron"]["crontab_sha256"], "actual crontab changed")
    lines = [line.strip() for line in raw["crontab"].splitlines() if line.strip() and not line.lstrip().startswith("#")]
    jobs = [line for line in lines if not re.fullmatch(r"(?:SHELL|PATH|MAILTO|TZ|CRON_TZ)=.*", line)]
    require(sorted(jobs) == sorted(job["line"].strip() for job in plan["cron"]["jobs"]), "uncontrolled or missing cron job")
    require(raw["wrappers"] == plan["cron"]["wrappers"], "installed wrapper content/permissions changed")
    expected = {name: {key: value[key] for key in ("binding_sha256", "source_sha256", "entrances")}
                for name, value in plan["controls"].items()}
    require(raw["registrations"] == expected, "actual ingress/control registration incomplete")
    if "legacy_binding" in plan["cron"]:
        require(raw["legacy_binding"] == plan["cron"]["legacy_binding"], "actual old cron archive/coverage differs")
    return raw


def _receipt(raw, control_id, binding, operation_id, reason):
    expected = {"schema": 1, "control_id": control_id, "operation_id": operation_id,
                "binding_sha256": binding["binding_sha256"], "source_sha256": binding["source_sha256"],
                "entrances": binding["entrances"], "previous_generation": binding["generation"],
                "generation": binding["generation"] + 1, "reason": reason,
                "state": "closed", "durable": True, "inflight": 0, "unknown": 0}
    require(isinstance(raw, dict) and set(raw) == set(expected)
            and all(type(raw[key]) is type(value) and raw[key] == value for key, value in expected.items()),
            "incomplete/wrong-operation/non-durable stop receipt")
    return raw


class RuntimeHandoffAdapter:
    def __init__(self, plan, transport, evidence_root):
        self.plan, self.transport = _checked_plan(plan), transport
        self.root = Path(evidence_root)
        private_directory(self.root, create=True)
        self.plan_sha256 = sha(plan)
        self.deadline = time.monotonic() + max(0, plan["expires_at"] - time.time())
        lock = self.root / "operation.lock"
        if not present(lock):
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _window(self):
        require(self.plan["not_before"] <= time.time() < self.plan["expires_at"]
                and time.monotonic() < self.deadline, "authorized observation/operation window expired")

    def inspect_and_bind(self):
        self._window()
        ids = tuple(value["id"] for value in self.plan["components"].values())
        raw = self.transport.inspect(ids)
        coverage = self.transport.coverage()
        observed = inspect_binding(raw, self.plan["components"])
        coverage_binding(coverage, self.plan)
        return {"raw_inspect": raw, "coverage": coverage, "identity": observed, "identity_sha256": sha(observed),
                "coverage_sha256": sha(coverage), "scope": "isolation", "switch_authorized": False}

    def _intent(self, operation_id, reason):
        require(isinstance(operation_id, str) and HEX64.fullmatch(operation_id)
                and isinstance(reason, str) and 0 < len(reason) <= 256, "exact operation/reason required")
        return {"schema": 1, "operation_id": operation_id, "reason": reason, "plan_sha256": self.plan_sha256}

    def _stop_path(self, operation_id):
        require(isinstance(operation_id, str) and HEX64.fullmatch(operation_id), "bad operation identity")
        return self.root / (operation_id + ".intent.json")

    def stop_producers(self, operation_id, reason):
        with locked(self.root / "operation.lock", True):
            path = self._stop_path(operation_id)
            require(not any(self.root.glob("*.intent.json*")), "operation already attempted; query exact original only")
            binding = self.inspect_and_bind()
            intent = self._intent(operation_id, reason)
            durable_write(self.root / (operation_id + ".binding.json"), binding)
            durable_write(path, intent)
            for control_id in self.plan["controls"]:
                self._issue(intent, control_id)
            return self._result(intent)

    def _issue(self, intent, control_id):
        self._window()
        binding = self.plan["controls"][control_id]
        child_op = sha({"operation_id": intent["operation_id"], "control_id": control_id, "plan_sha256": self.plan_sha256})
        attempt = {**intent, "control_id": control_id, "child_operation_id": child_op}
        prefix = self.root / (intent["operation_id"] + "." + control_id)
        require(not present(prefix.with_suffix(prefix.suffix + ".attempt.json")), "control already attempted")
        durable_write(Path(str(prefix) + ".attempt.json"), attempt)
        raw = self.transport.stop(control_id, child_op, binding["generation"], intent["reason"])
        receipt = _receipt(raw, control_id, binding, child_op, intent["reason"])
        # A second independent protocol read confirms the exact persistent
        # receipt. Ordinary 202/boolean and unbound receipt are never enough.
        queried = self.transport.query(control_id, child_op)
        require(_receipt(queried, control_id, binding, child_op, intent["reason"]) == receipt, "stop query disagrees")
        durable_write(Path(str(prefix) + ".receipt.json"), receipt)

    def _load_intent(self, operation_id):
        path = self._stop_path(operation_id)
        require(not present(Path(str(path) + ".pending")), "partial local stop intent; no control query")
        intent = read_private(path)
        require(intent == self._intent(operation_id, intent["reason"]), "saved stop intent changed")
        return intent

    def query_operation(self, operation_id):
        with locked(self.root / "operation.lock", True):
            self.inspect_and_bind()
            intent = self._load_intent(operation_id)
            for control_id, binding in self.plan["controls"].items():
                prefix = str(self.root / (operation_id + "." + control_id))
                require(not present(Path(prefix + ".attempt.json.pending")), "partial attempt: preserve, no blind resend")
                if not present(Path(prefix + ".attempt.json")):
                    continue
                child_op = sha({"operation_id": operation_id, "control_id": control_id, "plan_sha256": self.plan_sha256})
                require(read_private(Path(prefix + ".attempt.json")) == {**intent, "control_id": control_id,
                        "child_operation_id": child_op}, "saved attempt changed")
                raw = self.transport.query(control_id, child_op)
                receipt = _receipt(raw, control_id, binding, child_op, intent["reason"])
                path = Path(prefix + ".receipt.json")
                if present(path):
                    require(read_private(path) == receipt, "conflicting saved receipt; preserve original")
                durable_write(path, receipt)
            return self._result(intent)

    def _result(self, intent):
        final_binding = self.inspect_and_bind()
        receipts, remaining = {}, []
        for control_id, binding in self.plan["controls"].items():
            path = self.root / (intent["operation_id"] + "." + control_id + ".receipt.json")
            if not present(path):
                remaining.append(control_id)
                continue
            child_op = sha({"operation_id": intent["operation_id"], "control_id": control_id, "plan_sha256": self.plan_sha256})
            require(not present(Path(str(path) + ".pending")), "stop receipt sync unconfirmed")
            receipts[control_id] = _receipt(read_private(path), control_id, binding, child_op, intent["reason"])
        return {"status": "listed_producers_stopped" if not remaining else "stop_incomplete", **intent,
                "receipts": receipts, "remaining_controls": remaining, "identity_sha256": final_binding["identity_sha256"],
                "scope": "isolation", "production_adapter_bound": False, "queue_checked": False, "switch_authorized": False}


class NativeDockerTransport:
    """Actual finite inspect/crontab/file/loopback protocol, without a live CLI.

Caller injects a process runner only for deterministic unit tests; those tests
are not Docker proof. Native integration must use the default subprocess runner.
"""
    def __init__(self, endpoints, wrappers, *, docker="/usr/bin/docker", runner=subprocess.run, controls=None, legacy_cron=None):
        controls = {} if controls is None else controls
        require(Path(docker).is_absolute() and isinstance(endpoints, dict) and isinstance(controls, dict)
                and 1 <= len(endpoints) + len(controls) <= 32 and not set(endpoints) & set(controls),
                "fixed disjoint Docker/control targets required")
        self.docker, self.runner, self.endpoints, self.wrappers = docker, runner, endpoints, wrappers
        self.controls = controls
        self.legacy_cron = legacy_cron
        for name, item in endpoints.items():
            url = urlsplit(item["url"])
            require(CONTROL.fullmatch(name) and set(item) == {"url", "token"} and url.scheme == "http"
                    and url.hostname == "127.0.0.1" and url.port and url.path in ("", "/")
                    and not url.query and not url.fragment and not url.username and not url.password
                    and HEX64.fullmatch(item["token"]), "only authenticated loopback isolation controls allowed")

    def _command(self, argv):
        result = self.runner(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=5, check=False,
                             env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, shell=False)
        require(result.returncode == 0 and isinstance(result.stdout, bytes) and len(result.stdout) <= MAX_RAW_BYTES,
                "actual command failed/oversized; no retry")
        return result.stdout

    def inspect(self, container_ids):
        require(isinstance(container_ids, tuple) and 1 <= len(container_ids) <= 8
                and all(HEX64.fullmatch(value) for value in container_ids), "full container ids required")
        return parsed(self._command([self.docker, "--host", "unix:///var/run/docker.sock", "inspect", *container_ids]))

    def coverage(self):
        crontab = self._command(["/usr/bin/crontab", "-l"]).decode()
        wrappers = {}
        for name in self.wrappers:
            path = Path(name)
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= 1024 * 1024, "unsafe installed wrapper")
                raw = stream.read(before.st_size + 1)
                after = os.fstat(stream.fileno())
                require(len(raw) == before.st_size and before.st_mtime_ns == after.st_mtime_ns
                        and before.st_size == after.st_size, "wrapper changed during read")
                wrappers[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "uid": before.st_uid, "mode": stat.S_IMODE(before.st_mode)}
        registrations = {name: self._http(name, "GET", "/registration") for name in self.endpoints}
        registrations.update({name: control.registration() for name, control in self.controls.items()})
        for line in crontab.splitlines():
            if not line.strip() or line.lstrip().startswith("#") or re.fullmatch(r"(?:SHELL|PATH|MAILTO|TZ|CRON_TZ)=.*", line.strip()):
                continue
            root, command = _cron_command(line, wrappers)
            gate = HostCronGate(root)
            require(command in gate.manifest["commands"] and gate.manifest["control_id"] in registrations
                    and gate.registration() == registrations[gate.manifest["control_id"]], "cron gate differs from actual endpoint gate")
        result = {"crontab": crontab, "wrappers": wrappers, "registrations": registrations}
        if self.legacy_cron is not None:
            value = self.legacy_cron
            require(isinstance(value, dict) and set(value) == {"archive", "replacements"}, "exact legacy archive inputs required")
            archive = read_private(Path(value["archive"]))
            require(isinstance(archive, dict) and set(archive) == {"crontab"}, "private original crontab archive required")
            result["legacy_binding"] = legacy_cron_binding(archive["crontab"], crontab, value["replacements"], wrappers)
        return result

    def _http(self, control_id, method, path, payload=None):
        item = self.endpoints[control_id]
        url = urlsplit(item["url"])
        connection = http.client.HTTPConnection("127.0.0.1", url.port, timeout=2)
        try:
            body = encoded(payload) if payload is not None else None
            connection.request(method, path, body=body, headers={"Authorization": "Bearer " + item["token"], "Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read(65537)
            require(response.status == 200 and len(raw) <= 65536, "control response incomplete/non-success/oversized")
            return parsed(raw)
        finally:
            connection.close()

    def stop(self, control_id, operation_id, generation, reason):
        if control_id in self.controls:
            return self.controls[control_id].stop(operation_id, generation, reason)
        return self._http(control_id, "POST", "/stop", {"operation_id": operation_id, "generation": generation, "reason": reason})

    def query(self, control_id, operation_id):
        require(HEX64.fullmatch(operation_id), "full operation id required")
        if control_id in self.controls:
            return self.controls[control_id].query(operation_id)
        return self._http(control_id, "GET", "/operations/" + quote(operation_id, safe=""))
