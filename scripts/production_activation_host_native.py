"""Complete synthetic activation inside a private Linux host, never production.

Fixed dependencies are genuinely pulled from a hash-verified offline mirror.
Only the once-requested sealed candidate build uses the original outer daemon.
No image metadata, actual port mapping, or production transport is fabricated.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
import uuid
from contextlib import redirect_stderr, redirect_stdout

# Only the real script entry emits this fixed, pre-project-import observation.
# Canonical delegation carries this ORIGINAL clock into _main without resetting
# the outer budget; an ordinary import neither observes time nor writes output.
CLI_STARTED, CLI_PHASE_OFFSET = None, 0
if __name__ == "__main__":
    CLI_STARTED, CLI_PHASE_OFFSET = time.monotonic(), 1
    _CLI_BEGIN = b'{"event":"full_activation_cli","phase":"script_start","sequence":1}\n'
    if os.write(2, _CLI_BEGIN) != len(_CLI_BEGIN):
        raise RuntimeError("native CLI progress write incomplete")

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "scripts"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from scripts import production_host_ingress_native as host  # noqa: E402
from scripts import private_release_contract as private  # noqa: E402
from scripts import verify_production_startup_native as startup  # noqa: E402
from scripts import production_native_deadline as native_deadline  # noqa: E402
from services import admission_fence as gate  # noqa: E402

MAX_SECONDS, FINAL_SECONDS = 600, 20
IMAGE_LIMIT, LOG_LIMIT = 1024**3, 4 * 1024**2
DOCKER = ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock"]
SUPERVISOR = {"memory.max": str(768 * 1024**2), "memory.swap.max": "0", "pids.max": "256", "cpu.max": "100000 100000"}
LABEL = "ai-filter.synthetic-miniflux-pause"
CONTEXT_SCHEMA = "full-private-activation-host-v1"
PHASES = {"initial_validation", "dependency_inspect", "sender_export", "offline_bundle", "namespace_launch",
          "namespace_setup", "namespace_daemon", "dependency_pull", "network_setup", "full_activation",
          "candidate_build", "child_observation", "cleanup", "evidence_export"}
PHASE, PHASE_SEQUENCE = "initial_validation", CLI_PHASE_OFFSET
CLI_PHASES = {"canonical_import_before", "canonical_import_verified", "canonical_main_enter",
              "arguments_before", "arguments_verified"}
ERROR_TYPES = {"AdmissionError", "AdmissionUncertain", "ContractError", "ProofError", "ValueError", "TypeError",
               "KeyError", "RuntimeError", "TimeoutError", "OSError", "FileNotFoundError", "FileExistsError",
               "PermissionError", "ProcessLookupError", "JSONDecodeError", "CalledProcessError", "TimeoutExpired"}
ACTIVATION_STAGES = {"actual_finite_admission_lease", "actual_go_to_real_worker_deterministic_consumption",
    "actual_consumer_prepare_while_closed", "actual_fixed_gateway_switch_while_closed",
    "receiver_resume_and_independent_runtime_verification", "sender_resume_lost_ack_exact_query",
    "actual_same_guardian_handoff_and_live_tick", "actual_restrictive_stop_without_false_drain",
    "actual_fixed_network_namespace_transport", "never_opened_receiver_disk", "actual_go_schema135_first_closed",
    "retain_old_stopped_containers_and_seal", "build_exact_application_image", "probe_exact_application_image",
    "bind_exact_migration_permit", "actual_locked_migration_create_grant_start", "actual_compose_parser_metadata",
    "bounded_internal_network_and_real_redis"}


def phase(root, name):
    global PHASE, PHASE_SEQUENCE
    gate._require(type(name) is str and name in PHASES, "unknown native phase")
    PHASE, PHASE_SEQUENCE = name, PHASE_SEQUENCE + 1
    gate._require(PHASE_SEQUENCE <= 64, "full-host phase observation bound")
    record(root / ("PHASE_%03d.json" % PHASE_SEQUENCE), {"phase": name, "monotonic": time.monotonic()})
    # The outer Python diagnostic redirect must not hide bounded progress from
    # PROCESS.stderr. Never include paths, exception text, argv, or input data.
    progress = host.encoded({"event": "full_activation_phase", "phase": name, "sequence": PHASE_SEQUENCE})
    gate._require(len(progress) <= 192 and os.write(2, progress) == len(progress), "native phase progress write incomplete")


def cli_progress(name):
    global PHASE_SEQUENCE
    gate._require(type(name) is str and name in CLI_PHASES, "unknown native CLI phase")
    PHASE_SEQUENCE += 1
    gate._require(PHASE_SEQUENCE <= 64, "full-host phase observation bound")
    progress = host.encoded({"event": "full_activation_cli", "phase": name, "sequence": PHASE_SEQUENCE})
    gate._require(len(progress) <= 192 and os.write(2, progress) == len(progress), "native CLI progress write incomplete")


def cli_start(value):
    now = time.monotonic()
    gate._require(type(value) in {int, float} and math.isfinite(value)
                  and value <= now and now - value < MAX_SECONDS,
                  "original native CLI start required")
    return value


def error_type(error):
    return type(error).__name__ if type(error).__name__ in ERROR_TYPES else "unrecognized_exception"


def remaining(deadline, *, maximum=MAX_SECONDS):
    gate._require(type(deadline) in {int, float} and math.isfinite(deadline), "original full-host deadline required")
    value = deadline - time.monotonic()
    gate._require(0 < value <= maximum, "full-host shared deadline expired or replaced")
    return value


def record(path, value, *, sealed=False):
    if not sealed:
        host.durable(path, value)
        return host.file_proof(path, limit=host.OUTPUT_BYTES)
    # Publish only an already-fsynced 0400 inode. A visible temporary hard link
    # would violate private_file's exact nlink=1 guard, so it is never used.
    temporary = path.with_name(path.name + ".sealed-prepared-" + uuid.uuid4().hex)
    host.durable(temporary, value)
    os.chmod(temporary, 0o400, follow_symlinks=False)
    fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    rename_noreplace(temporary, path)
    gate._sync_directory(path.parent)
    return host.file_proof(path, limit=host.OUTPUT_BYTES)


def rename_noreplace(source, target):
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "linux":
        function = library.renameat2
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = function(-100, os.fsencode(source), -100, os.fsencode(target), 1)  # RENAME_NOREPLACE
    elif sys.platform == "darwin":  # Local unit tests only; native CLI is Linux-only.
        function = library.renamex_np
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = function(os.fsencode(source), os.fsencode(target), 4)  # SDK sys/stdio.h: RENAME_EXCL
    else:
        raise OSError("atomic exclusive rename unavailable; no replacement fallback")
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(target))


def original(path, digest=None):
    path = private.safe_path(path)
    data, metadata = private.private_file(path, sealed=True)
    gate._require(digest is None or hashlib.sha256(data).hexdigest() == digest, "frozen private-host proof changed")
    return json.loads(data), metadata


def peer_identity():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.5)
        connection.connect("/var/run/docker.sock")
        pid, uid, gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    gate._require(uid == gid == 0, "actual Docker socket root peer required")
    return host.proc_identity(pid)


def resource_check(plan):
    leaf = host.CGROUP / plan["cgroup"].lstrip("/") / "supervisor"
    actual = {key: (leaf / key).read_text().strip() for key in SUPERVISOR}
    gate._require(actual == SUPERVISOR, "private host supervisor resource drift")
    events = dict(line.split() for line in (leaf / "memory.events").read_text().splitlines())
    gate._require(events.get("oom") == events.get("oom_kill") == "0", "private host supervisor OOM")
    return {**actual, "memory.peak": (leaf / "memory.peak").read_text().strip(), "memory.events": events,
            "scope": "supervisor_and_private_daemon_only_not_all_application_containers"}


def process_check(plan, holder, daemon, *, final=False):
    remaining(plan["deadline"] if final else plan["active_deadline"])
    current = host.proc_identity(os.getpid())
    gate._require(current["netns"] == holder["netns"] != plan["parent"]["netns"]
                  and current["mntns"] == holder["mntns"] != plan["parent"]["mntns"]
                  and current["cgroup"] == holder["cgroup"] == plan["cgroup"] + "/supervisor"
                  and host.proc_identity(holder["pid"]) == holder
                  and host.proc_identity(plan["parent"]["pid"]) == plan["parent"], "private host namespace/lifetime drift")
    gate._require(host.file_proof(Path(__file__)) == plan["wrapper_source"], "private host wrapper source changed")
    host.check_peer(daemon["pid"], daemon)
    gate._require(daemon["netns"] == holder["netns"] and daemon["mntns"] == holder["mntns"]
                  and daemon["cgroup"] == holder["cgroup"], "private Docker daemon escaped host")
    resource_check(plan)


def validate_network(proof, row, owner, commands):
    gate._require(row["Id"] == proof["network_id"] and row["Name"] == proof["network_name"]
                  and row["Internal"] is False and row["Driver"] == "bridge" and row["Scope"] == "local"
                  and row["Labels"].get(LABEL) == owner == proof["owner"]
                  and row["Options"].get("com.docker.network.bridge.name") == proof["bridge"]
                  and re.fullmatch(r"[0-9a-f]{64}", row["Id"]), "exact private host network binding required")
    observation = commands.root / ("NAMESPACE_CHECK_%04d.json" % commands.sequence)
    endpoints = row.get("Containers")
    gate._require(isinstance(endpoints, dict) and all(re.fullmatch(r"[0-9a-f]{64}", key) for key in endpoints),
                  "exact private host network endpoints required")
    # Every veth on the owned bridge must correspond to an endpoint Docker registered on this network.
    state = host.namespace_snapshot(commands, bridge=proof["bridge"], evidence_path=observation, registered=len(endpoints))
    gate._require(state["netns"] == proof["holder"]["netns"], "private host network namespace changed")


def require_context_network(path, digest, row, owner):
    proof, _ = original(path, digest)
    gate._require(proof.get("schema") == CONTEXT_SCHEMA, "unsupported full private host proof")
    plan, _ = original(Path(proof["plan_path"]), proof["plan_sha256"])
    process_check(plan, proof["holder"], proof["daemon"])
    commands = host.Commands(path.parent / ("namespace-observer-" + uuid.uuid4().hex), plan["active_deadline"])
    commands.root.mkdir(mode=0o700, exist_ok=False)
    commands.peer = lambda: host.check_peer(proof["daemon"]["pid"], proof["daemon"])
    validate_network(proof, row, owner, commands)
    return proof


def require_harness_network(harness, row):
    context = getattr(harness, "private_host_context", None)
    gate._require(type(context) is PrivateHostContext, "ordinary bridge requires the exact full private-host context")
    context.check_network(row, harness.owner)


class PrivateHostContext:
    def __init__(self, root, plan, commands, holder, daemon):
        self.root, self.plan, self.commands = root, plan, commands
        self.holder, self.daemon = holder, daemon
        self.path, self.proof_sha256, self.network = root / "HOST_CONTEXT.json", None, None
        self.project, self.candidate, self.receiver = None, None, None

    def check(self, *, final=False):
        process_check(self.plan, self.holder, self.daemon, final=final)
        if self.proof_sha256 is not None:
            proof, _ = original(self.path, self.proof_sha256)
            gate._require(proof["holder"] == self.holder and proof["daemon"] == self.daemon,
                          "private host context execution changed")

    def prepare_network(self, harness):
        self.check()
        phase(self.root, "network_setup")
        gate._require(self.network is None and not self.path.exists(), "private host network already bound")
        bridge = "afa" + harness.owner[:10]
        self.commands.docker(["network", "create", "--driver", "bridge", "--label", LABEL + "=" + harness.owner,
                              "--opt", "com.docker.network.bridge.name=" + bridge, harness.network])
        row = json.loads(self.commands.docker(["network", "inspect", harness.network]))[0]
        plan_path = self.root.parent / "PLAN.json"
        proof = {"schema": CONTEXT_SCHEMA, "plan_path": str(plan_path),
                 "plan_sha256": host.file_proof(plan_path, limit=host.OUTPUT_BYTES)["sha256"],
                 "holder": self.holder, "daemon": self.daemon, "owner": harness.owner,
                 "network_id": row["Id"], "network_name": harness.network, "bridge": bridge}
        validate_network(proof, row, harness.owner, self.commands)
        self.proof_sha256 = record(self.path, proof, sealed=True)["sha256"]
        self.network = proof
        phase(self.root, "full_activation")
        return row

    def check_network(self, row, owner):
        self.check()
        gate._require(self.network is not None, "private host network has no frozen proof")
        validate_network(self.network, row, owner, self.commands)

    def check_running_inventory(self, harness):
        self.check()
        ids = self.commands.docker(["ps", "--quiet", "--no-trunc"]).splitlines()
        gate._require(len(ids) <= 64 and all(re.fullmatch(r"[0-9a-f]{64}", x) for x in ids),
                      "bounded complete private daemon inventory required")
        for identifier in ids:
            row = self.commands.inspect(identifier)
            actual = host.proc_identity(row["State"]["Pid"])
            limits = row["HostConfig"]
            labels = row["Config"]["Labels"] or {}
            role = next((key for key, name in harness.names.items() if row["Name"] == "/" + name), None)
            owned = role is not None and labels.get(LABEL) == harness.owner and row["Image"] == harness.images[role]
            receiver = self.receiver or {}
            service = labels.get("com.docker.compose.service")
            application = (self.project is not None and service in receiver.get("manifest", {}).get("services", {})
                           and labels.get("com.docker.compose.project") == self.project
                           and labels.get("com.docker.compose.project.working_dir") == str(receiver["paths"]["project"])
                           and row["Name"] == "/" + self.project + "-" + service + "-1" and row["Image"] == self.candidate)
            gateway_plan = receiver.get("gateway_plan", {})
            gateway_operation = gateway_plan.get("operation_id")
            gateway_owned = False
            if gateway_operation:
                from scripts.production_gateway_switch import gateway_name
                purpose = labels.get("io.ai-filter.gateway.purpose")
                name = gateway_name(gateway_operation) + ("-config-test" if purpose == "config-test" else "")
                gateway_owned = (purpose in {"config-test", "gateway"} and row["Name"] == "/" + name
                                 and labels.get("io.ai-filter.gateway.operation") == gateway_operation
                                 and labels.get("io.ai-filter.gateway.plan") == gate._hash(gateway_plan))
            expected_images = {entry["Id"] for entry in self.plan["images"].values()} | {self.plan["sender"]["Id"], self.candidate}
            expected_memory, expected_pids = 768 * 1024**2, 256
            if row["Name"] == "/" + harness.names.get("pg", ""):
                expected_memory, expected_pids = 384 * 1024**2, 64
            elif row["Image"] in {self.plan["images"][ref]["Id"] for ref in self.plan["references"] if ref.startswith(("nginx:", "redis:"))}:
                expected_memory, expected_pids = 128 * 1024**2, 64
            gate._require(actual["cgroup"].startswith(self.plan["cgroup"] + "/containers/")
                          and actual["netns"] != self.holder["netns"] and actual["mntns"] != self.holder["mntns"]
                          and (owned or application or gateway_owned) and row["Image"] in expected_images
                          and limits["Memory"] == expected_memory
                          and limits["MemorySwap"] == expected_memory and limits["PidsLimit"] == expected_pids
                          and limits["NanoCpus"] == 1_000_000_000 and not row["State"]["OOMKilled"],
                          "actual application cgroup placement/resource boundary differs")

    def build_candidate(self, p, sealed, contract_sha):
        self.check()
        phase(self.root, "candidate_build")
        expected = self.root / "activation" / "receiver"
        gate._require(p["directory"] == expected and sealed == expected / "sealed/contract.json",
                      "only the fixed sealed activation build is permitted")
        gate._require(self.project is None, "candidate build request already issued")
        self.project, self.receiver = p["project"], p
        request = {"schema": 1, "contract_sha256": contract_sha,
                   "source": p["source"], "image": "ai-filter-activation-native:" + p["project"]}
        request_proof = record(self.root.parent / "BUILD_REQUEST.json", request, sealed=True)
        result_path = self.root.parent / "BUILD_RESULT.json"
        host.wait_until(lambda: result_path.exists(), self.plan["active_deadline"])
        result, _ = original(result_path)
        gate._require(result.get("status") == "built" and result["request_sha256"] == request_proof["sha256"],
                      "outer sealed candidate build unconfirmed")
        tar = self.root.parent / "candidate.tar"
        gate._require(host.file_proof(tar, limit=IMAGE_LIMIT) == result["archive"], "candidate archive identity changed")
        self.commands.docker(["image", "load"], transfer=True, input_path=tar, input_proof=result["archive"])
        row = self.commands.inspect(result["image"]["Id"], image=True)
        gate._require(host.image_binding(row, outer=False) == result["image"], "actual loaded candidate image differs")
        gate._require(host.file_proof(tar, limit=IMAGE_LIMIT) == result["archive"], "candidate archive changed after load")
        self.check()
        self.candidate = row["Id"]
        phase(self.root, "full_activation")
        return row["Id"]


class FullCommands(host.Commands):
    """The only larger stream is a fixed, identity-bound Docker save/load."""

    def run(self, args, **kwargs):
        output, source = kwargs.get("output_path"), kwargs.get("input_path")
        if output is None and source is None:
            return super().run(args, **kwargs)
        gate._require(kwargs.get("transfer") is True and args[:len(DOCKER)] == DOCKER
                      and (args[len(DOCKER):] == ["image", "load"] if source else
                           len(args) == len(DOCKER) + 3 and args[len(DOCKER):len(DOCKER) + 2] == ["image", "save"]
                           and re.fullmatch(r"sha256:[0-9a-f]{64}", args[-1])), "only fixed image transfer may use larger bound")
        return self.transfer(args, source, kwargs.get("input_proof"), output)

    def docker(self, args, **kwargs):
        try:
            return super().docker(args, **kwargs)
        except BaseException:
            if args[0] not in {"inspect", "ps", "info", "logs", "version"}:
                self.uncertain_mutation = True
            raise

    def transfer(self, args, source, proof, output):
        remaining(self.deadline)
        if self.peer:
            self.peer()
        self.sequence += 1
        base = self.root / ("command-%04d" % self.sequence)
        out, err = output or base.with_suffix(".stdout"), base.with_suffix(".stderr")
        receipt = {"argv": args, "started_monotonic": time.monotonic(), "returncode": None, "transfer": True,
                   "deadline_monotonic": self.deadline, "max_output_bytes": IMAGE_LIMIT if output else host.OUTPUT_BYTES}
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW) if source else subprocess.DEVNULL
        process, failure, result = None, None, None
        try:
            if source:
                gate._require(host.file_proof(source, limit=IMAGE_LIMIT, fd=descriptor) == proof, "exact image input descriptor differs")
            process = subprocess.Popen(args, stdin=descriptor, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=self.env, start_new_session=True)
            self._capture(process, out, err, self.deadline, receipt["max_output_bytes"])
            receipt["returncode"] = process.wait(timeout=remaining(self.deadline))
            gate._require(receipt["returncode"] == 0, "actual image transfer failed")
            if self.peer:
                self.peer()
            if source:
                gate._require(host.file_proof(source, limit=IMAGE_LIMIT, fd=descriptor) == proof, "image descriptor changed during load")
            result = b"" if output else out.read_bytes()
        except BaseException as error:
            failure = error
            self.uncertain_mutation = True
        finally:
            if source:
                try:
                    os.close(descriptor)
                except BaseException as error:
                    if failure:
                        failure.add_note("image input descriptor close failed: " + type(error).__name__)
                    else:
                        failure = error
            if process and process.poll() is None:
                try:
                    process.terminate()  # Client only; never its daemon or namespace holder.
                except BaseException as error:
                    receipt["client_term_error_type"] = type(error).__name__
                    if failure:
                        failure.add_note("image transfer client TERM failed: " + type(error).__name__)
                    else:
                        failure = error
            receipt["elapsed"] = time.monotonic() - receipt["started_monotonic"]
            try:
                record(base.with_suffix(".json"), receipt)
            except BaseException as error:
                if failure:
                    failure.add_note("image transfer evidence failed: " + type(error).__name__)
                else:
                    failure = error
        if failure:
            self.uncertain_mutation = True
            raise failure
        return result


def cgroup_setup(owner):
    required = {"cpu", "memory", "pids"}
    gate._require(required <= set((host.CGROUP / "cgroup.controllers").read_text().split())
                  and required <= set((host.CGROUP / "cgroup.subtree_control").read_text().split()),
                  "delegated host cgroup required; no global cgroup change permitted")
    root = host.CGROUP / ("ai-filter-activation-" + owner)
    root.mkdir(mode=0o700)
    (root / "cgroup.subtree_control").write_text("+cpu +memory +pids")
    for name in ("supervisor", "containers"):
        (root / name).mkdir(mode=0o700)
    (root / "containers/cgroup.subtree_control").write_text("+cpu +memory +pids")
    for name, value in SUPERVISOR.items():
        (root / "supervisor" / name).write_text(value)
    (root / "supervisor/cgroup.procs").write_text(str(os.getpid()))
    gate._require(host.proc_identity(os.getpid())["cgroup"] == "/" + root.name + "/supervisor",
                  "supervisor not charged to exact owned resource group")
    return "/" + root.name


def prepare_mounts(commands, root):
    resolver = host.resolver_target()
    commands.run([str(host.TOOLS["mount"]), "--make-rprivate", "/"])
    (root / "run").mkdir(mode=0o700)
    commands.run([str(host.TOOLS["mount"]), "--bind", str(root / "run"), "/run"])
    gate._require(Path("/var/run").resolve() == Path("/run") and not Path("/var/run/docker.sock").exists(),
                  "outer Docker socket not shadowed")
    mounts = Path("/proc/self/mountinfo").read_text().splitlines()
    gate._require(len(mounts) <= 4096 and all(not any(field.startswith(("shared:", "master:"))
                  for field in line.split()[6:]) for line in mounts), "private mount propagation unconfirmed")
    record(root / "PRIVATE_MOUNTS.txt", ("\n".join(mounts) + "\n").encode())
    record(root / "resolv.conf", b"nameserver 127.0.0.1\noptions attempts:1 timeout:1\n")
    if resolver != Path("/etc/resolv.conf"):
        resolver.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolver.touch(mode=0o600, exist_ok=False)
    commands.run([str(host.TOOLS["mount"]), "--bind", str(root / "resolv.conf"), "/etc/resolv.conf"])
    commands.run([str(host.TOOLS["ip"]), "link", "set", "lo", "up"])


def start_daemon(commands, root, plan, mirror):
    config = {**host.daemon_config(root, plan), "registry-mirrors": [mirror]}
    record(root / "daemon.json", config)
    process, logs = host.launch_daemon(commands, root, plan)
    host.retain_daemons(deadline=commands.deadline)
    actual = host.proc_identity(process.pid)
    holder = host.proc_identity(os.getpid())
    gate._require(all(actual[key] == holder[key] for key in ("netns", "mntns", "cgroup")), "private daemon escaped owned host")
    record(root / "DAEMON.json", actual)

    def ready():
        gate._require(process.poll() is None, "private daemon exited before socket ready")
        if not Path("/var/run/docker.sock").exists():
            return False
        host.check_peer(process.pid, actual)
        return True

    host.wait_until(ready, min(commands.deadline, time.monotonic() + 10))

    def peer():
        logs.check()
        gate._require(host.retainer_confirmed(process), "live daemon FD retainer absent")
        host.check_peer(process.pid, actual)

    commands.peer = peer
    record(root / "DAEMON_INFO.json", json.loads(commands.docker(["info", "--format", "{{json .}}"])))
    return process, actual, logs


def load_dependencies(commands, root, plan):
    from scripts.verify_miniflux_pause_native import image_checks
    for number, reference in enumerate(plan["references"]):
        commands.docker(["pull", "--platform", "linux/amd64", reference], transfer=True)
        row = commands.inspect(reference, image=True)
        image_checks(row, reference)
        gate._require(host.image_binding(row, outer=False) == plan["images"][reference], "genuine private pull differs from outer image")
        record(root / ("DEPENDENCY_%02d.json" % number), row)
    archive = root.parent / "sender.tar"
    gate._require(host.file_proof(archive, limit=IMAGE_LIMIT) == plan["sender_archive"], "sender archive changed")
    commands.docker(["image", "load"], transfer=True, input_path=archive, input_proof=plan["sender_archive"])
    gate._require(host.image_binding(commands.inspect(plan["sender"]["Id"], image=True), outer=False) == plan["sender"],
                  "actual loaded sender image differs")


def stop_daemon(context, process, logs):
    """A missing/unknown/running container never authorizes killing the daemon."""
    context.check(final=True)
    context.commands.deadline = context.plan["deadline"]
    ids = context.commands.docker(["ps", "--quiet", "--no-trunc"]).splitlines()
    gate._require(ids == [] and not context.commands.uncertain_mutation, "running or uncertain containers: retain private daemon")
    host.check_peer(process.pid, context.daemon)
    process.send_signal(signal.SIGTERM)
    code = process.wait(timeout=remaining(context.plan["deadline"]))
    gate._require(code == 0, "private daemon normal stop unconfirmed")
    logs.finish(context.plan["deadline"])
    retained = host.RETAINERS.get(process.pid)
    gate._require(retained is not None and retained[0].wait(timeout=remaining(context.plan["deadline"])) == 0,
                  "daemon FD retainer did not observe closure")
    receipt = {"status": "stopped", "all_containers_stopped": True, "daemon_returncode": code,
               "forced_kill": False, "removed": False}
    record(context.root / "DAEMON_STOP.json", receipt)
    return receipt


def inner_execute(root, plan):
    from scripts import production_offline_registry as registry
    from scripts import verify_production_activation_native as activation
    os.setsid()
    holder = host.proc_identity(os.getpid())
    commands = FullCommands(root, plan["active_deadline"])
    phase(root, "namespace_setup")
    host.restrict_private_ipv6(commands, root, plan, holder)
    prepare_mounts(commands, root)
    record(root / "HOLDER.json", holder)
    process, context, failure, activation_result, stopped, failed_phase = None, None, None, None, None, None
    try:
        with registry.serve_bundle(root.parent / "bundle", plan["bundle"], expected_netns=holder["netns"],
                                   parent_identity=plan["parent"], deadline=plan["active_deadline"]) as mirror:
            phase(root, "namespace_daemon")
            process, daemon, logs = start_daemon(commands, root, plan, mirror.url)
            context = PrivateHostContext(root, plan, commands, holder, daemon)
            context.check()
            phase(root, "dependency_pull")
            load_dependencies(commands, root, plan)
            phase(root, "full_activation")
            with native_deadline.scope(plan["active_deadline"], plan["deadline"]):
                activation_result = activation.execute(Path(plan["repo"]), Path(plan["source"]), plan["sender"]["Id"],
                                                       Path(plan["binary"]), root / "activation", private_host_context=context)
            gate._require(activation_result["status"] == "passed", "original complete activation did not pass")
            context.check()
    except BaseException as error:
        failure, failed_phase = error, PHASE
        if context is not None:
            # Original activation also contains direct private.run/archive
            # mutators. A failed full scenario cannot be cleared by ps=[];
            # their final ACK/durability may be unknown even after cleanup.
            context.commands.uncertain_mutation = True
    finally:
        if process is not None and context is not None:
            try:
                phase(root, "cleanup")
                stopped = stop_daemon(context, process, logs)
            except BaseException as error:
                if failure:
                    failure.add_note("private daemon retained: " + type(error).__name__)
                else:
                    failure = error
        try:
            record(root / "RESOURCES_FINAL.json", resource_check(plan))
        except BaseException as error:
            if failure:
                failure.add_note("final resource evidence failed: " + type(error).__name__)
            else:
                failure = error
    result = {"status": "passed" if failure is None and stopped else "failed_or_evidence_insufficient",
              "phase": failed_phase or PHASE,
              "activation_result": activation_result, "daemon_stop": stopped,
              "error_type": error_type(failure) if failure else None,
              "error_sha256": hashlib.sha256(str(failure).encode()).hexdigest() if failure else None,
              "retained_live_daemon": any(item.poll() is None for item, _log, owned, _plan in host.LIVE_DAEMONS if owned == root),
              "production_access": False, "real_models": 0, "existing_126_modified": False}
    record(root / "INNER_RESULT.json", result, sealed=True)
    return result


def references():
    from scripts.verify_miniflux_pause_native import PG_IMAGE, PYTHON_IMAGE
    from scripts.verify_production_activation_native import REDIS
    from scripts.production_gateway_switch import NGINX_REF
    return [PYTHON_IMAGE, PG_IMAGE, REDIS, NGINX_REF]


def outer_plan(args, root, started):
    from scripts import production_offline_registry as registry
    from scripts.verify_miniflux_pause_native import image_checks, source_binding
    deadline = started + MAX_SECONDS
    active_deadline = deadline - FINAL_SECONDS
    cgroup = cgroup_setup(uuid.uuid4().hex)
    commands = FullCommands(root, active_deadline)
    phase(root, "dependency_inspect")
    host.host_tools(root, commands)
    binding = source_binding(args.repo, args.source, args.binary)
    source = private.source_identity(args.repo)
    outer_peer = peer_identity()
    commands.peer = lambda: host.check_peer(outer_peer["pid"], outer_peer)
    images = {}
    for number, reference in enumerate(references()):
        row = commands.inspect(reference, image=True)
        image_checks(row, reference)
        images[reference] = host.image_binding(row, outer=False)
        record(root / ("OUTER_DEPENDENCY_%02d.json" % number), row)
    sender = host.image_binding(commands.inspect(args.image_id, image=True), outer=False)
    gate._require(sender["Id"] == args.image_id, "exact outer sender image required")
    phase(root, "sender_export")
    commands.docker(["image", "save", args.image_id], transfer=True, output_path=root / "sender.tar")
    gate._require(host.image_binding(commands.inspect(args.image_id, image=True), outer=False) == sender,
                  "outer sender changed during export")
    phase(root, "offline_bundle")
    bundle = registry.prepare_bundle(root / "bundle", references(), active_deadline)
    plan = {"schema": CONTEXT_SCHEMA, "started": started, "deadline": deadline, "active_deadline": active_deadline,
            "repo": str(args.repo), "source": str(args.source), "binary": str(args.binary), "source_identity": source,
            "source_binding": binding, "wrapper_source": host.file_proof(Path(__file__)),
            "cgroup": cgroup, "parent": host.proc_identity(os.getpid()), "outer_peer": outer_peer,
            "references": references(), "images": images, "sender": sender,
            "sender_archive": host.file_proof(root / "sender.tar", limit=IMAGE_LIMIT), "bundle": bundle}
    resource_check(plan)
    record(root / "PLAN.json", plan, sealed=True)
    return plan, commands


def checked_build_request(root, plan):
    request, proof = original(root / "BUILD_REQUEST.json")
    gate._require(set(request) == {"schema", "contract_sha256", "source", "image"} and request["schema"] == 1
                  and request["source"] == plan["source_identity"]
                  and re.fullmatch(r"[0-9a-f]{64}", request["contract_sha256"]), "exact sealed build request required")
    sealed = root / "inner/activation/receiver/sealed/contract.json"
    contract, _manifest, digest = private.load(sealed, Path(plan["repo"]), require_fresh=True)
    gate._require(digest == request["contract_sha256"] and
                  request["image"] == "ai-filter-activation-native:" + contract["review"]["project"],
                  "candidate build request does not bind the reviewed sealed contract")
    return request, proof, contract


def build_requested_candidate(root, plan, commands):
    phase(root, "candidate_build")
    request, proof, contract = checked_build_request(root, plan)
    gate._require(peer_identity() == plan["outer_peer"] and private.source_identity(Path(plan["repo"])) == plan["source_identity"],
                  "outer build source or daemon peer changed")
    workspace = root / "outer-build"
    workspace.mkdir(mode=0o700, exist_ok=False)
    result, failure = {"status": "failed", "request_sha256": proof["sha256"]}, None
    try:
        digest = startup.build_candidate(Path(plan["repo"]), workspace, request["source"], request["image"],
                                         contract, request["contract_sha256"], deadline=plan["active_deadline"])
        image = host.image_binding(commands.inspect(digest, image=True), outer=False)
        commands.docker(["image", "save", digest], transfer=True, output_path=root / "candidate.tar")
        gate._require(host.image_binding(commands.inspect(digest, image=True), outer=False) == image
                      and peer_identity() == plan["outer_peer"], "built candidate or actual outer daemon changed")
        result.update(status="built", archive=host.file_proof(root / "candidate.tar", limit=IMAGE_LIMIT), image=image)
    except BaseException as error:
        failure = error
        result.update(error_type=type(error).__name__, error_sha256=hashlib.sha256(str(error).encode()).hexdigest())
        command, returncode = getattr(error, "command", None), getattr(error, "returncode", None)
        result.update(failed_command=command if command in OUTER_BUILD_COMMANDS else None,
                      returncode=returncode if type(returncode) is int else None)
    try:
        record(root / "BUILD_RESULT.json", result, sealed=True)
    except BaseException as evidence_error:
        if failure:
            failure.add_note("candidate build receipt could not be persisted: " + type(evidence_error).__name__)
            raise failure from evidence_error
        raise
    if failure:
        raise failure


def sealed_child_result(root):
    path = root / "inner/INNER_RESULT.json"
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    result, _proof = original(path)
    gate._require(type(result) is dict and result.get("status") in {"passed", "failed_or_evidence_insufficient"},
                  "sealed child terminal status required")
    return result


def observe_child(process, root, plan, commands):
    requested, build_failure = False, None
    while process.poll() is None:
        remaining(plan["deadline"])
        gate._require(peer_identity() == plan["outer_peer"], "outer daemon socket peer changed")
        result = sealed_child_result(root)
        if result is not None and result["status"] == "failed_or_evidence_insufficient":
            if build_failure:
                raise build_failure
            return result  # Retained FD holders are not protocol progress.
        if (root / "BUILD_REQUEST.json").exists() and not requested:
            requested = True  # One attempt only, including a failed or incomplete request.
            try:
                remaining(plan["active_deadline"])
                build_requested_candidate(root, plan, commands)
            except BaseException as error:
                build_failure = error
            if build_failure is None:
                phase(root, "child_observation")
        for name in ("inner.stdout", "inner.stderr"):
            gate._require((root / name).stat().st_size <= LOG_LIMIT, "private child log output bound")
        time.sleep(min(0.05, remaining(plan["deadline"])))
    if build_failure:
        raise build_failure
    result = sealed_child_result(root)
    gate._require(result is not None, "sealed child terminal result missing")
    if result["status"] == "failed_or_evidence_insufficient":
        return result
    gate._require(process.returncode == 0, "full private host child failed; originals retained")
    gate._require(result.get("status") == "passed" and result.get("activation_result", {}).get("status") == "passed",
                  "complete private activation or normal cleanup unconfirmed")
    gate._require(requested and (root / "BUILD_RESULT.json").exists(), "complete activation did not perform sealed candidate build")
    return result


def outer_execute(args, root, started):
    plan, commands = outer_plan(args, root, started)
    phase(root, "namespace_launch")
    args_inner = [str(host.TOOLS["unshare"]), "--mount", "--net", "--fork", sys.executable, "-I", "-B", str(Path(__file__).resolve()),
                  "--repo", str(args.repo), "--source", str(args.source), "--binary", str(args.binary),
                  "--image-id", args.image_id, "--output", str(root / "inner"), "--inside"]
    (root / "inner").mkdir(mode=0o700)
    process, streams = None, []
    try:
        for name in ("inner.stdout", "inner.stderr"):
            streams.append(os.fdopen(os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb"))
        process = subprocess.Popen(args_inner, stdin=subprocess.DEVNULL, stdout=streams[0], stderr=streams[1],
                                   env=commands.env, start_new_session=True)
        phase(root, "child_observation")
        result = observe_child(process, root, plan, commands)
        gate._require(private.source_identity(args.repo) == plan["source_identity"]
                      and host.file_proof(Path(__file__)) == plan["wrapper_source"], "source changed during full proof")
        return result
    finally:
        preserve_outer_outputs(process, root, plan, streams, sys.exc_info()[1])


def preserve_outer_outputs(process, root, plan, streams, primary):
    failures = []
    if process and process.poll() is None:
        try:
            # The child holder is detached. Never kill it or its daemon.
            record(root / "OUTER_CHILD_UNCONFIRMED.json", {"unshare_pid": process.pid, "forced_kill": False,
                   "status": "retained_unconfirmed", "deadline_monotonic": plan["deadline"]})
        except BaseException as error:
            failures.append(error)
    for stream in streams:
        try:
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException as error:
            failures.append(error)
        try:
            stream.close()
        except BaseException as error:
            failures.append(error)
    if primary:
        for error in failures:
            primary.add_note("outer child evidence failed: " + type(error).__name__)
    elif failures:
        raise failures[0]


def safe_activation(value):
    """Project report is still untrusted input to the public summary."""
    result = {"schema": "production-activation-native-v1", "status": value.get("status") if isinstance(value.get("status"), str) and value.get("status") in {
        "passed", "failed_or_evidence_insufficient"} else "unrecognized",
        "last_stage_sha256": hashlib.sha256(host.encoded(value.get("last_stage"))).hexdigest()}
    result["last_stage"] = value.get("last_stage") if isinstance(value.get("last_stage"), str) and value.get("last_stage") in ACTIVATION_STAGES else None
    for key in ("source_sha",):
        result[key] = value.get(key) if re.fullmatch(r"[0-9a-f]{40}", str(value.get(key))) else None
    for key in ("elapsed_seconds", "app_memory_bytes", "app_pids", "pg_memory_bytes", "pg_pids", "real_models"):
        item = value.get(key)
        result[key] = item if type(item) in {int, float} and math.isfinite(item) and item >= 0 else None
    failure = value.get("failure")
    if isinstance(failure, dict):
        result["failure_message_sha256"] = (failure.get("message_sha256") if
            re.fullmatch(r"[0-9a-f]{64}", str(failure.get("message_sha256"))) else None)
        result["failure_type_sha256"] = hashlib.sha256(host.encoded(failure.get("type"))).hexdigest()
        kind = failure.get("type")
        result["failure_type"] = kind if isinstance(kind, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,80}", kind) else None
        for key, limit in (("message_literal", 200), ("namespace_helper", 800), ("guardian_handoff", 800)):
            item = failure.get(key)
            result["failure_" + key] = (item if isinstance(item, str) and 0 < len(item) <= limit
                                        and re.fullmatch(r"[ -~]+", item) and not re.search(r"[0-9a-fA-F]{32,}", item) else None)
    result["evidence_error_count"] = len(value.get("evidence_errors", [])) if isinstance(value.get("evidence_errors"), list) else None
    return result


def safe_inner_result(value):
    return {"status": value.get("status") if type(value.get("status")) is str and value.get("status") in {"passed", "failed_or_evidence_insufficient"} else None,
            "phase": value.get("phase") if type(value.get("phase")) is str and value.get("phase") in PHASES else None,
            "retained_live_daemon": value.get("retained_live_daemon") if type(value.get("retained_live_daemon")) is bool else None,
            "error_type": value.get("error_type") if type(value.get("error_type")) is str and value.get("error_type") in ERROR_TYPES else None,
            "error_sha256": value.get("error_sha256") if re.fullmatch(r"[0-9a-f]{64}", str(value.get("error_sha256"))) else None}


GATEWAY_COMMAND_RE = re.compile(r"inner/activation/receiver/gateway-native/switch/[a-z][a-z-]{0,40}\.(?:stderr|command\.json)")
GATEWAY_TEXT_CAP, GATEWAY_TEXT_BUDGET = 64 * 1024, 160 * 1024
OUTER_BUILD_COMMANDS = ("git-archive", "archive-extract", "application-build", "bounded-image-inspect")
OUTER_BUILD_COMMAND_RE = re.compile(r"outer-build/(?:" + "|".join(OUTER_BUILD_COMMANDS) + r")\.stderr")
OUTER_BUILD_TEXT_CAP, OUTER_BUILD_TEXT_BUDGET = 16 * 1024, 48 * 1024


def outer_build_command_exports(items, deadline):
    """Tail of failed outer build command stderr: capped, per-line redacted, size+hash bound to the original."""
    from scripts.capture_release_diagnostics import redact_text

    exports, used = [], 0
    for path, relative, proof in sorted(items, key=lambda item: item[1]):
        remaining(deadline)
        row = {"tag": "outer_build_command_stderr", "path": relative, "bytes": proof["size"], "sha256": proof["sha256"],
               "truncated": proof["size"] > OUTER_BUILD_TEXT_CAP, "text": None}
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.lseek(fd, max(0, proof["size"] - OUTER_BUILD_TEXT_CAP), os.SEEK_SET)
            tail = os.read(fd, OUTER_BUILD_TEXT_CAP)
        finally:
            os.close(fd)
        lines = tail.decode("utf-8", errors="replace").split("\n")
        text = "\n".join(re.sub(r"[0-9a-fA-F]{32,}", "<hex>", redact_text(line)) for line in lines)
        row["line_count"] = len(lines)
        if row["truncated"]:
            text = f"[TRUNCATED: last {len(tail)} of {proof['size']} bytes]\n" + text
        size = len(host.encoded(text))
        if used + size > OUTER_BUILD_TEXT_BUDGET:
            row["reason"] = "export_budget_exhausted"
        else:
            used, row["text"] = used + size, text
        exports.append(row)
    return exports


def safe_build_result(value):
    command, returncode, kind = value.get("failed_command"), value.get("returncode"), value.get("error_type")
    return {"status": value.get("status") if value.get("status") in {"built", "failed"} else None,
            "error_type": kind if isinstance(kind, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,80}", kind) else None,
            "failed_command": command if command in OUTER_BUILD_COMMANDS else None,
            "returncode": returncode if type(returncode) is int else None}


def gateway_command_exports(items, deadline):
    """Export only gateway Docker command stderr/receipts: capped, redacted, size+hash bound to the original."""
    from scripts.capture_release_diagnostics import redact_text

    exports, used = [], 0
    for path, relative, proof in sorted(items, key=lambda item: item[1]):
        remaining(deadline)
        row = {"tag": "gateway_command_stderr", "path": relative, "bytes": proof["size"], "sha256": proof["sha256"],
               "truncated": proof["size"] > GATEWAY_TEXT_CAP, "text": None}
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            head = os.read(fd, GATEWAY_TEXT_CAP)
        finally:
            os.close(fd)
        # Redact per line: keeps line structure and avoids redact_text's 4096-char whole-field cap.
        text = "\n".join(redact_text(line) for line in head.decode("utf-8", errors="replace").split("\n"))
        if row["truncated"]:
            text += f"\n[TRUNCATED: first {len(head)} of {proof['size']} bytes]"
        size = len(host.encoded(text))
        if used + size > GATEWAY_TEXT_BUDGET:
            row["reason"] = "export_budget_exhausted"
        else:
            used, row["text"] = used + size, text
        exports.append(row)
    return exports


def safe_inventory(root, deadline):
    """Hash bounded originals; explicitly exclude storage trees and raw payloads."""
    records, omissions, total, gateway_texts, outer_build_texts = [], [], 0, [], []
    excluded = {root / "inner/data", root / "inner/exec", root / "inner/run", root / "bundle/objects",
                root / "outer-build/context"}
    for directory, dirs, files in os.walk(root, followlinks=False):
        remaining(deadline)
        current = Path(directory)
        gate._require(len(records) + len(omissions) < 4096, "full-host original inventory count bound")
        for name in list(dirs):
            remaining(deadline)
            gate._require(len(records) + len(omissions) < 4096, "full-host original inventory count bound")
            path = current / name
            if path in excluded or path.is_symlink():
                dirs.remove(name)
                omissions.append({"path_sha256": hashlib.sha256(str(path.relative_to(root)).encode()).hexdigest(),
                                  "reason": "runtime_storage_or_link_not_followed"})
        for name in files:
            remaining(deadline)
            gate._require(len(records) + len(omissions) < 4096, "full-host original inventory count bound")
            path = current / name
            relative = str(path.relative_to(root))
            entry = {"path_sha256": hashlib.sha256(relative.encode()).hexdigest()}
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or path.is_symlink():
                omissions.append({**entry, "reason": "non_regular_not_followed"})
                continue
            if info.st_size > 8 * 1024**2 or total + info.st_size > 128 * 1024**2:
                omissions.append({**entry, "reason": "original_bytes_retained_outside_selected_packet", "bytes": info.st_size})
                continue
            proof = host.file_proof(path, limit=8 * 1024**2)
            total += proof["size"]
            records.append({**entry, "sha256": proof["sha256"], "bytes": proof["size"]})
            if GATEWAY_COMMAND_RE.fullmatch(relative):
                gateway_texts.append((path, relative, proof))
            if OUTER_BUILD_COMMAND_RE.fullmatch(relative) and proof["size"]:
                outer_build_texts.append((path, relative, proof))
    required = {"inner/INNER_RESULT.json", "inner/activation/RESULT.json", "inner/DAEMON_STOP.json", "inner/RESOURCES_FINAL.json"}
    expected = {hashlib.sha256(name.encode()).hexdigest() for name in required}
    complete = expected <= {row["path_sha256"] for row in records}
    profiles = {}
    for name in ("inner/activation/RESULT.json", "inner/INNER_RESULT.json", "inner/DAEMON_STOP.json", "inner/RESOURCES_FINAL.json"):
        remaining(deadline)
        path = root / name
        if path.exists():
            value = original(path)[0] if name.endswith("INNER_RESULT.json") else host.read_original_json(path)
            if name.endswith("activation/RESULT.json"):
                profiles[name] = safe_activation(value)
            elif name.endswith("INNER_RESULT.json"):
                profiles[name] = safe_inner_result(value)
            elif name.endswith("DAEMON_STOP.json"):
                profiles[name] = {key: value.get(key) for key in ("all_containers_stopped", "forced_kill", "removed")
                                  if type(value.get(key)) is bool}
                profiles[name]["daemon_returncode"] = value.get("daemon_returncode") if type(value.get("daemon_returncode")) is int else None
            else:
                profiles[name] = {key: value.get(key) for key in SUPERVISOR if value.get(key) == SUPERVISOR[key]}
    if (root / "BUILD_RESULT.json").exists():
        remaining(deadline)
        profiles["BUILD_RESULT.json"] = safe_build_result(host.read_original_json(root / "BUILD_RESULT.json"))
    packet = {"schema": 1, "scope": "selected_sanitized_original_hash_inventory", "complete": complete,
              "originals": records, "omissions": omissions, "hashed_original_bytes": total,
              "profiles": profiles, "full_raw_archive_verified": False, "raw_originals_preserved_privately": True,
              "gateway_command_stderr": gateway_command_exports(gateway_texts, deadline),
              "outer_build_command_stderr": outer_build_command_exports(outer_build_texts, deadline)}
    gate._require(len(host.encoded(packet)) <= 384 * 1024, "selected full-host evidence encoding bound")
    remaining(deadline)
    return packet


def _arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "source", "binary", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    gate._require(sys.platform == "linux" and os.uname().machine == "x86_64" and os.geteuid() == 0
                  and args.repo == ROOT and ROOT == ROOT.resolve(), "fixed native Linux amd64 root source required")
    gate._require(re.fullmatch(r"sha256:[0-9a-f]{64}", args.image_id), "full actual sender image ID required")
    for path in (args.repo, args.source, args.binary, args.output.parent):
        private.safe_path(path)
        info = path.stat()
        gate._require(info.st_uid == 0 and not stat.S_IMODE(info.st_mode) & 0o022, "root-owned non-writable fixed inputs required")
    gate._require(Path(__file__).stat().st_uid == 0, "root-owned wrapper source required")
    return args


def _main(argv=None, *, _cli_started=None):
    started = time.monotonic() if _cli_started is None else cli_start(_cli_started)
    result, failure, root, failed_phase = None, None, None, None
    try:
        if _cli_started is not None:
            cli_progress("arguments_before")
        args = _arguments(argv)
        if _cli_started is not None:
            cli_progress("arguments_verified")
        os.umask(0o077)
        root = args.output
        if args.inside:
            private.private_directory(root)
            plan, _ = original(root.parent / "PLAN.json")
            gate._require(root.name == "inner" and args.repo == Path(plan["repo"]) and args.source == Path(plan["source"])
                          and args.binary == Path(plan["binary"]) and args.image_id == plan["sender"]["Id"]
                          and host.file_proof(Path(__file__)) == plan["wrapper_source"], "inner frozen invocation differs")
            from scripts.production_gateway_switch import _hard_deadline
            with _hard_deadline(remaining(plan["deadline"])):
                result = inner_execute(root, plan)
            return 0 if result["status"] == "passed" else 1
        host.private_directory(root, fresh=True)
        from scripts.production_gateway_switch import _hard_deadline
        with open(root / "OUTER_DIAGNOSTICS.txt", "x") as diagnostics, redirect_stdout(diagnostics), redirect_stderr(diagnostics):
            with _hard_deadline(remaining(started + MAX_SECONDS)), native_deadline.scope(started + MAX_SECONDS - FINAL_SECONDS, started + MAX_SECONDS):
                phase(root, "initial_validation")
                result = outer_execute(args, root, started)
    except BaseException as error:
        failure, failed_phase = error, PHASE
    packet = None
    if root is not None and root.is_dir():
        try:
            from scripts.production_gateway_switch import _hard_deadline
            with _hard_deadline(remaining(started + MAX_SECONDS)):
                phase(root, "evidence_export")
                packet = safe_inventory(root, started + MAX_SECONDS)
                record(root / "SAFE_INVENTORY.json", packet)
        except BaseException as error:
            if failure:
                failure.add_note("safe original inventory failed: " + type(error).__name__)
            else:
                failure, failed_phase = error, PHASE
    passed = failure is None and result is not None and result.get("status") == "passed" and packet and packet["complete"]
    inner_profile = safe_inner_result(result) if result else None
    summary = {"schema": 1, "scope": "full_synthetic_private_host_activation", "status": "passed" if passed else "failed_or_evidence_insufficient",
               "phase": failed_phase or (inner_profile["phase"] if inner_profile and result["status"] != "passed" else None) or PHASE,
               "full_activation_verified": bool(passed), "safe_evidence": packet, "full_raw_archive_verified": False,
               "activation": safe_activation(result["activation_result"]) if result and result.get("activation_result") else None,
               "inner_result": inner_profile,
               "error_type": error_type(failure) if failure else inner_profile and inner_profile["error_type"],
               "error_sha256": hashlib.sha256(str(failure).encode()).hexdigest() if failure else inner_profile and inner_profile["error_sha256"],
               "elapsed_seconds": time.monotonic() - started, "production_access": False, "real_models": 0, "existing_126_modified": False}
    gate._require(len(host.encoded(summary)) <= 512 * 1024, "full-host summary encoding bound")
    if root is not None and root.is_dir():
        try:
            from scripts.production_gateway_switch import _hard_deadline
            with _hard_deadline(remaining(started + MAX_SECONDS)):
                record(root / "FULL_RESULT.json", summary)
        except BaseException as error:
            summary.update(status="failed_or_evidence_insufficient", full_activation_verified=False,
                           result_persist_error_type=error_type(error))
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")), flush=True)
    return 0 if summary["full_activation_verified"] else 1


def main(argv=None, *, _cli_started=None):
    try:
        if _cli_started is not None:
            cli_progress("canonical_main_enter")
        return _main(argv) if _cli_started is None else _main(argv, _cli_started=_cli_started)
    finally:
        # Including launch/retainer/fsync/stdout failures: the detached namespace
        # holder keeps its ORIGINAL read FDs while any registered daemon is live.
        # This is passive bounded-resource retention, never protocol retries.
        while any(process.poll() is None for process, *_ in host.LIVE_DAEMONS):
            signal.pause()


def cli(argv=None):
    # A script's __main__ classes are distinct from this canonical module's
    # classes. Delegate both outer and --inside invocations before creating any
    # context; all exact type, argument, source and runtime guards stay in main.
    cli_progress("canonical_import_before")
    from scripts import production_activation_host_native as canonical
    expected = Path(__file__).resolve()
    spec = getattr(canonical, "__spec__", None)
    gate._require(canonical.__name__ == "scripts.production_activation_host_native"
                  and spec is not None and spec.name == canonical.__name__
                  and spec.origin == str(expected)
                  and Path(canonical.__file__).resolve() == expected,
                  "canonical full-host CLI source required")
    if canonical.__dict__ is not globals():
        gate._require(canonical.PHASE_SEQUENCE == 0, "canonical CLI observations already started")
        canonical.PHASE_SEQUENCE = PHASE_SEQUENCE
    canonical.cli_progress("canonical_import_verified")
    return canonical.main(argv) if CLI_STARTED is None else canonical.main(argv, _cli_started=CLI_STARTED)


if __name__ == "__main__":
    raise SystemExit(cli())
