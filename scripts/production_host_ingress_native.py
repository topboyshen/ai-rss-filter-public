"""Independent Linux synthetic-host proof; never a production deployment tool.

The host is a new network AND mount namespace, not the runner's global host.
An attested offline image load does not preserve or replace RepoDigest checks in
the production adapters. All new processes share one bounded cgroup. Failure
retains originals and live resources; it never force-stops unknown containers.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import select
import selectors
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import production_gateway_switch as gateway  # noqa: E402

CGROUP = Path("/sys/fs/cgroup")
DOCKER = ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock"]
TOOLS = {name: Path(path) for name, path in {
    "docker": "/usr/bin/docker", "dockerd": "/usr/bin/dockerd", "containerd": "/usr/bin/containerd",
    "runc": "/usr/bin/runc", "unshare": "/usr/bin/unshare", "mount": "/usr/bin/mount", "ip": "/usr/sbin/ip",
}.items()}
MEMORY, PIDS, CPU = 768 * 1024**2, 256, "100000 100000"
IMAGE_BYTES, OUTPUT_BYTES, MAX_PROCESSES = 256 * 1024**2, 2 * 1024**2, 8192
LABEL = "ai-filter.synthetic-host-ingress"
LIVE_DAEMONS = []
RETAINERS = {}
DIAGNOSTIC_RESERVE_SECONDS = 5
DIAGNOSTIC_TEXT_BYTES = 16 * 1024
DIAGNOSTICS = None
DIAGNOSTIC_STAGES = {"initial_validation", "host_tools", "source_image", "image_save", "source_binding",
                     "namespace_launch", "namespace_setup", "daemon_start", "image_load", "network_create",
                     "container_create", "container_start", "host_observation", "cleanup", "source_readback"}
DIAGNOSTIC_ERRORS = {"ProofError", "AdmissionError", "AdmissionUncertain", "ContractError", "TimeoutError",
                     "TimeoutExpired", "OSError", "FileNotFoundError", "FileExistsError", "PermissionError",
                     "ProcessLookupError", "ValueError", "TypeError", "KeyError", "JSONDecodeError", "RuntimeError"}
DIAGNOSTIC_LITERALS = {"gateway blocking operation deadline; outcome unknown", "command deadline; outcome unknown",
                       "shared deadline expired", "command failed; outcome retained", "daemon logs EOF unknown",
                       "private daemon shutdown unconfirmed", "unexpected private daemon inventory; retained",
                       "unresolved mutation prevents daemon stop despite empty inventory"}
DIAGNOSTIC_OPERATIONS = {"docker_image_save", "docker_image_load", "docker_network_create", "docker_network_inspect",
                         "docker_create", "docker_start", "docker_inspect", "docker_logs", "docker_kill",
                         "docker_ps", "docker_info", "docker_other", "namespace_launch", "host_tool", "git", "other"}
CONFIG = b"""worker_processes 1;
pid /tmp/nginx.pid;
events { worker_connections 16; }
http { access_log /dev/stdout; error_log /dev/stderr warn;
 server { listen 5000; location = /health { return 200 'ISOLATED_HOST'; } }
}
"""


class ProofError(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise ProofError(message)


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def durable(path, data):
    data = data if isinstance(data, bytes) else encoded(data)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def identity(info):
    return [info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def file_proof(path, limit=IMAGE_BYTES, *, fd=None):
    owned = fd is None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW) if owned else fd
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= limit,
                "bounded regular original required")
        digest, count = hashlib.sha256(), 0
        while chunk := os.read(fd, 1024**2):
            count += len(chunk)
            require(count <= limit, "original grew beyond bound")
            digest.update(chunk)
        require(count == before.st_size and identity(before) == identity(os.fstat(fd))
                == identity(os.lstat(path)), "original identity changed")
        return {"identity": identity(before), "sha256": digest.hexdigest(), "size": count}
    finally:
        if owned:
            os.close(fd)
        else:
            os.lseek(fd, 0, os.SEEK_SET)


def private_directory(path, *, fresh=False):
    require(path.is_absolute() and path == path.resolve() and ".." not in path.parts, "direct absolute directory required")
    for item in [path.parent, *path.parent.parents]:
        info = item.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0
                and (not info.st_mode & 0o022 or info.st_mode & stat.S_ISVTX), "unsafe directory ancestor")
    if fresh:
        path.mkdir(mode=0o700)
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o700,
            "owned private directory required")


def bounded_deadline(value, now=None):
    now = time.monotonic() if now is None else now
    require(type(value) in {int, float} and math.isfinite(value) and 0 < value - now <= 180,
            "original isolated proof deadline required")
    return min(value, now + 180)


def active_deadline(deadline):
    # Reserve inside the original 180 seconds, never start a fresh timeout.
    end = deadline - DIAGNOSTIC_RESERVE_SECONDS
    require(time.monotonic() < end, "original proof has no active budget after diagnostic reserve")
    return end


@contextmanager
def active_phase(deadline):
    """On active timeout, re-arm the original limit before failure unwinding."""
    end, started = active_deadline(deadline), time.monotonic()
    outer_handler = signal.getsignal(signal.SIGALRM)
    outer_timer = signal.getitimer(signal.ITIMER_REAL)
    require(callable(outer_handler) and outer_timer[0] > 0 and outer_timer[1] == 0,
            "original total deadline must enclose active phase")
    inherited_end = min(deadline, started + outer_timer[0])

    def restore():
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, outer_handler)
        signal.setitimer(signal.ITIMER_REAL, max(0.001, inherited_end - time.monotonic()))

    def expired(signum, frame):
        # A blocking fsync in an exception's finally must still hit the original
        # total deadline. Restoring only after yield unwinds would be too late.
        restore()
        if inherited_end <= end:
            outer_handler(signum, frame)
        raise gateway.gate.AdmissionUncertain("gateway blocking operation deadline; outcome unknown")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, max(0.001, min(end, inherited_end) - time.monotonic()))
    try:
        yield
    finally:
        if signal.getsignal(signal.SIGALRM) is expired:
            restore()


def diagnostic_stage(stage):
    if DIAGNOSTICS is not None:
        require(stage in DIAGNOSTIC_STAGES, "unknown host-entry diagnostic stage")
        DIAGNOSTICS["stage"] = stage


def diagnostic_error(error):
    text = str(error)
    return {"type": type(error).__name__ if type(error).__name__ in DIAGNOSTIC_ERRORS else "unrecognized",
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "literal": text if text in DIAGNOSTIC_LITERALS else None}


def diagnostic_operation(args):
    if args[:len(DOCKER)] == DOCKER:
        action = args[len(DOCKER):]
        candidate = "docker_" + "_".join(action[:2]) if action[:1] in (["image"], ["network"]) else "docker_" + (action[0] if action else "other")
        return candidate if candidate in DIAGNOSTIC_OPERATIONS else "docker_other"
    if args[:1] == [str(TOOLS["unshare"])]:
        return "namespace_launch"
    if args[:1] == ["/usr/bin/git"]:
        return "git"
    return "host_tool" if args and args[0] in {str(path) for path in TOOLS.values()} else "other"


def diagnostic_command(args, sequence, record=None, error=None):
    if DIAGNOSTICS is None:
        return
    elapsed = record.get("elapsed") if record else None
    code = record.get("returncode") if record else None
    DIAGNOSTICS["last_command"] = {
        "sequence": sequence, "operation": diagnostic_operation(args),
        "state": "failed" if error else "completed" if record else "started",
        "elapsed_seconds": round(elapsed, 6) if type(elapsed) in {int, float} and math.isfinite(elapsed) and elapsed >= 0 else None,
        "returncode": code if type(code) is int and -255 <= code <= 255 else None,
        "error": diagnostic_error(error) if error else None}


def failure_diagnostic(error):
    current = DIAGNOSTICS or {"stage": "initial_validation", "last_command": None}
    return {"schema": 1, "stage": current["stage"], "last_command": current["last_command"],
            "error": diagnostic_error(error)}


def retain_first_failure(root, error):
    # Preserve before cleanup can change the last command or raise another error.
    if DIAGNOSTICS is None or DIAGNOSTICS.get("first_failure") is not None:
        return
    value = failure_diagnostic(error)
    DIAGNOSTICS["first_failure"] = value
    try:
        durable(root / "FIRST_FAILURE.json", value)
    except BaseException as evidence_error:
        error.add_note("first host-entry failure preservation failed: " + type(evidence_error).__name__)


class Commands:
    def __init__(self, root, deadline):
        self.root, self.deadline, self.sequence, self.peer = root, deadline, 0, None
        self.uncertain_mutation = False
        self.env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "DOCKER_CONFIG": str(root / "client")}

    def run(self, args, *, transfer=False, input_path=None, input_proof=None, output_path=None):
        require(time.monotonic() < self.deadline, "shared deadline expired")
        if self.peer and args[:len(DOCKER)] == DOCKER:
            self.peer()
        self.sequence += 1
        base = self.root / ("command-%04d" % self.sequence)
        stdout, stderr = output_path or base.with_suffix(".stdout"), base.with_suffix(".stderr")
        end = self.deadline if transfer else min(self.deadline, time.monotonic() + 3)
        record = {"argv": args, "started_monotonic": time.monotonic(), "returncode": None,
                  "transfer": transfer, "deadline_monotonic": end}
        diagnostic_command(args, self.sequence)
        source = os.open(input_path, os.O_RDONLY | os.O_NOFOLLOW) if input_path else subprocess.DEVNULL
        process, failure, result = None, None, None
        try:
            if input_path:
                require(file_proof(input_path, fd=source) == input_proof, "actual load input fd differs")
            process = subprocess.Popen(args, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=self.env, start_new_session=True)
            self._capture(process, stdout, stderr, end, IMAGE_BYTES if transfer and output_path else OUTPUT_BYTES)
            record["returncode"] = process.wait(timeout=max(0.001, end - time.monotonic()))
            require(record["returncode"] == 0, "command failed; outcome retained")
            if self.peer and args[:len(DOCKER)] == DOCKER:
                self.peer()
            if input_path:
                require(file_proof(input_path, fd=source) == input_proof, "actual load input fd changed")
            result = b"" if output_path else stdout.read_bytes()
        except BaseException as error:
            failure = error
        finally:
            if input_path:
                os.close(source)
            if process and process.poll() is None:
                # Client only. Never send signals to its daemon/process group.
                try:
                    process.terminate()
                    record["client_term_sent"] = True
                except BaseException as term_error:
                    record["client_term_error_type"] = type(term_error).__name__
                    if failure:
                        failure.add_note("client TERM failed: " + type(term_error).__name__)
                    else:
                        failure = term_error
            record["elapsed"] = time.monotonic() - record["started_monotonic"]
            if process:
                record["returncode"] = process.poll()
            diagnostic_command(args, self.sequence, record, failure)
            try:
                durable(base.with_suffix(".json"), record)
            except BaseException as evidence_error:
                if failure:
                    failure.add_note("command evidence failure: " + type(evidence_error).__name__)
                else:
                    failure = evidence_error
        if failure:
            raise failure
        return result

    @staticmethod
    def _capture(process, stdout, stderr, end, limit, *, close_pipes=True):
        count = 0
        with open(stdout, "xb") as out, open(stderr, "xb") as err, selectors.DefaultSelector() as poll:
            os.chmod(stdout, 0o600)
            os.chmod(stderr, 0o600)
            for stream, target in [(process.stdout, out), (process.stderr, err)]:
                poll.register(stream, selectors.EVENT_READ, target)
            try:
                while poll.get_map():
                    require(time.monotonic() < end, "command deadline; outcome unknown")
                    for key, _ in poll.select(max(0, min(0.1, end - time.monotonic()))):
                        data = os.read(key.fileobj.fileno(), 65536)
                        count += len(data)
                        require(count <= limit, "command output limit; partial retained")
                        if data:
                            key.data.write(data)
                        else:
                            poll.unregister(key.fileobj)
            finally:
                for stream in (out, err):
                    stream.flush()
                    os.fsync(stream.fileno())
                if close_pipes:
                    process.stdout.close()
                    process.stderr.close()

    def docker(self, args, **kwargs):
        try:
            return self.run([*DOCKER, *args], **kwargs).decode().strip()
        except BaseException:
            if args[0] in {"create", "start", "kill"} or args[:2] in (["network", "create"], ["image", "load"]):
                self.uncertain_mutation = True
            raise

    def inspect(self, identifier, *, image=False):
        rows = json.loads(self.docker((["image"] if image else []) + ["inspect", identifier]))
        require(isinstance(rows, list) and len(rows) == 1, "one actual inspect required")
        return rows[0]


def image_binding(row, *, outer):
    require(isinstance(row, dict) and re.fullmatch(r"sha256:[0-9a-f]{64}", row.get("Id", ""))
            and row.get("Os") == "linux" and row.get("Architecture") == "amd64"
            and isinstance(row.get("Config"), dict) and row.get("RootFS", {}).get("Type") == "layers"
            and isinstance(row["RootFS"].get("Layers"), list) and 0 < len(row["RootFS"]["Layers"]) <= 128
            and all(re.fullmatch(r"sha256:[0-9a-f]{64}", x) for x in row["RootFS"]["Layers"]), "actual image identity invalid")
    if outer:
        require("nginx@" + gateway.NGINX_REF.split("@", 1)[1] in row.get("RepoDigests", []),
                "outer pinned RepoDigest missing")
    return {**{key: row[key] for key in ("Id", "Config", "RootFS", "Os", "Architecture")},
            "Variant": row.get("Variant", "")}


def proc_identity(pid):
    root = Path("/proc") / str(pid)
    raw = (root / "stat").read_text()
    tail = raw.rsplit(") ", 1)[1].split()
    lines = (root / "cgroup").read_text().splitlines()
    require(len(lines) == 1 and lines[0].startswith("0::/"), "unified actual process cgroup required")
    return {"pid": pid, "ppid": int(tail[1]), "starttime": int(tail[19]), "cgroup": lines[0][3:],
            "netns": os.stat(root / "ns/net").st_ino, "mntns": os.stat(root / "ns/mnt").st_ino}


def cgroup_setup(owner):
    required = {"cpu", "memory", "pids"}
    require(required <= set((CGROUP / "cgroup.controllers").read_text().split())
            and required <= set((CGROUP / "cgroup.subtree_control").read_text().split()),
            "host cgroup delegation unavailable; global controls unchanged")
    root = CGROUP / ("ai-filter-host-" + owner)
    root.mkdir(mode=0o700)
    for name, value in {"memory.max": str(MEMORY), "memory.swap.max": "0", "pids.max": str(PIDS), "cpu.max": CPU}.items():
        (root / name).write_text(value)
    require(not (root / "cgroup.procs").read_text().strip(), "owned parent must remain empty")
    (root / "cgroup.subtree_control").write_text("+cpu +memory +pids")
    (root / "supervisor").mkdir(mode=0o700)
    (root / "containers").mkdir(mode=0o700)
    (root / "containers/cgroup.subtree_control").write_text("+cpu +memory +pids")
    (root / "supervisor/cgroup.procs").write_text(str(os.getpid()))
    path = "/" + root.name
    require(proc_identity(os.getpid())["cgroup"] == path + "/supervisor", "helper not charged to owned cgroup")
    return path


def resource_snapshot(path):
    root = CGROUP / path.lstrip("/")
    expected = {"memory.max": str(MEMORY), "memory.swap.max": "0", "pids.max": str(PIDS), "cpu.max": CPU}
    actual = {name: (root / name).read_text().strip() for name in expected}
    require(actual == expected and not (root / "cgroup.procs").read_text().strip(), "parent resource limit drift")
    result = {**actual, **{key: (root / key).read_text().strip()
                          for key in ("memory.current", "memory.peak", "memory.events", "pids.current", "cpu.stat")}}
    events = dict(line.split() for line in result["memory.events"].splitlines())
    require(events.get("oom") == events.get("oom_kill") == "0", "namespace resource OOM")
    return result


def process_snapshot(plan, netns, *, container_pid=None):
    rows, count, all_rows = [], 0, {}
    parent = plan["cgroup"]
    container = proc_identity(container_pid) if container_pid is not None else None
    if container:
        require(container["cgroup"].startswith(parent + "/containers/") and container["netns"] != netns,
                "container process namespace/subtree unbound")
    with os.scandir("/proc") as entries:
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            count += 1
            require(count <= MAX_PROCESSES, "host process inventory bound")
            try:
                row = proc_identity(int(entry.name))
            except FileNotFoundError:
                continue
            all_rows[row["pid"]] = row
            if row["cgroup"] == parent or row["cgroup"].startswith(parent + "/"):
                if row["pid"] == plan["parent"]["pid"]:
                    require(row == plan["parent"], "outer supervisor identity drift")
                elif container and (row["cgroup"] == container["cgroup"] or row["cgroup"].startswith(container["cgroup"] + "/")):
                    require(row["netns"] == container["netns"], "container worker network namespace escaped")
                else:
                    require(row["netns"] == netns, "owned process escaped private network namespace")
                rows.append(row)
    require(len(rows) <= PIDS and any(r["pid"] == os.getpid() for r in rows), "owned process inventory missing")
    descendants = {plan["parent"]["pid"]}
    for _ in range(PIDS):
        discovered = {pid for pid, row in all_rows.items() if row["ppid"] in descendants}
        if discovered <= descendants:
            break
        descendants |= discovered
    require(all(all_rows[pid]["cgroup"].startswith(parent + "/") for pid in descendants if pid in all_rows),
            "descendant escaped charged resource subtree")
    if container:
        require(proc_identity(container_pid) == container, "container identity changed during process inventory")
    return rows


MAX_REGISTERED_LINKS = 32


def registered_interface_bound(links, bridge, registered):
    """Exact shape for an owned bridge: one lo, one owned bridge, one owned veth per registered endpoint."""
    if not (bridge and type(registered) is int and 0 <= registered <= MAX_REGISTERED_LINKS - 2
            and len(links) == registered + 2 <= MAX_REGISTERED_LINKS and all(isinstance(row, dict) for row in links)):
        return False
    kinds = [(row.get("ifname"), row.get("linkinfo", {}).get("info_kind") if isinstance(row.get("linkinfo", {}), dict) else None,
              row.get("master")) for row in links]
    return (sum(1 for name, _, _ in kinds if name == "lo") == 1
            and sum(1 for name, kind, _ in kinds if name == bridge and kind == "bridge") == 1
            and sum(1 for name, kind, master in kinds if kind == "veth" and master == bridge and name not in {"lo", bridge}) == registered)


def namespace_violation(observed, bridge, registered=None):
    """Return a fixed reason/location; do not weaken the original route guard."""
    if observed.get("netns_after") != observed["netns"]:
        return {"reason": "namespace_identity", "section": "namespace", "index": None}
    links = observed["links"]
    if not isinstance(links, list) or not (registered_interface_bound(links, bridge, registered) if registered is not None
                                           else 0 < len(links) <= 8):
        return {"reason": "interface_bound", "section": "links", "index": None}
    for index, row in enumerate(links):
        if not isinstance(row, dict) or not isinstance(row.get("linkinfo", {}), dict):
            return {"reason": "interface_shape", "section": "links", "index": index}
        name, kind = row.get("ifname"), row.get("linkinfo", {}).get("info_kind")
        if not (name == "lo" or (bridge and ((name == bridge and kind == "bridge")
                                             or (kind == "veth" and row.get("master") == bridge)))):
            return {"reason": "external_interface", "section": "links", "index": index}
    for family, values in observed["routes_by_family"].items():
        if not isinstance(values, list) or len(values) > 32:
            return {"reason": "route_bound", "section": family, "index": None}
        for index, row in enumerate(values):
            if (not isinstance(row, dict) or not isinstance(row.get("dst"), (str, type(None)))
                    or not isinstance(row.get("dev"), (str, type(None)))):
                return {"reason": "route_shape", "section": family, "index": index}
            if (row.get("dst") in {None, "default", "0.0.0.0/0", "::/0"}
                    or row.get("dev") not in {"lo", bridge} or row.get("gateway")):
                return {"reason": "external_route", "section": family, "index": index}
    return None


def capture_namespace(commands, observed):
    for section, argv in [("links", ["-json", "-details", "link", "show"]),
                          ("ipv4", ["-json", "-4", "route", "show", "table", "all"]),
                          ("ipv6", ["-json", "-6", "route", "show", "table", "all"])]:
        raw = commands.run([str(TOOLS["ip"]), *argv])
        require(len(raw) <= OUTPUT_BYTES, "namespace raw output bound")
        observed["components"][section] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        values = json.loads(raw)
        if section == "links":
            observed["links"] = values
        else:
            observed["routes_by_family"][section] = values


def preserve_namespace_observation(path, observed, *, primary=None):
    if path is None:
        return
    try:
        require(len(encoded(observed)) <= OUTPUT_BYTES, "namespace observation size bound")
        durable(path, observed)
    except BaseException as evidence_error:
        if primary is not None:
            primary.add_note("namespace observation preservation failed: " + type(evidence_error).__name__)
            raise primary from evidence_error
        raise


INTERFACE_NAME_RE = re.compile(r"[A-Za-z0-9_.@-]{1,15}")


def interface_names(links, limit=32):
    """Diagnostic only: Linux interface names (<=15 safe chars) seen in a rejected namespace; never addresses."""
    if not isinstance(links, list):
        return None
    names = []
    for row in links[:limit]:
        name = row.get("ifname") if isinstance(row, dict) else None
        names.append(name if isinstance(name, str) and INTERFACE_NAME_RE.fullmatch(name) else "<invalid>")
    return {"count": len(links), "names": names}


def namespace_snapshot(commands, *, bridge=None, evidence_path=None, registered=None):
    observed = {"schema": 1, "netns": os.stat("/proc/self/ns/net").st_ino, "bridge": bridge, "registered": registered,
                "links": [], "routes_by_family": {}, "components": {}, "status": "capture_incomplete"}
    try:
        capture_namespace(commands, observed)
        observed["netns_after"] = os.stat("/proc/self/ns/net").st_ino
    except BaseException as error:
        observed["error_type"] = type(error).__name__
        observed["error_sha256"] = hashlib.sha256(str(error).encode()).hexdigest()
        preserve_namespace_observation(evidence_path, observed, primary=error)
        raise
    violation = namespace_violation(observed, bridge, registered)
    observed["status"] = "rejected" if violation else "validated"
    observed["violation"] = violation
    messages = {"namespace_identity": "network namespace identity changed during capture",
                "interface_bound": "namespace interface bound", "interface_shape": "namespace interface shape",
                "external_interface": "unexpected external interface in private host", "route_bound": "namespace route bound",
                "route_shape": "namespace route shape", "external_route": "external namespace route"}
    failure = ProofError(messages[violation["reason"]]) if violation else None
    if failure is not None:
        failure.interface_names = interface_names(observed.get("links"))
    preserve_namespace_observation(evidence_path, observed, primary=failure)
    if failure is not None:
        raise failure
    routes = [row for values in observed["routes_by_family"].values() for row in values]
    return {"links": observed["links"], "routes": routes, "netns": observed["netns"]}


def private_namespace_context(plan, expected):
    current = proc_identity(os.getpid())
    require(current == expected and current["netns"] != plan["parent"]["netns"]
            and current["mntns"] != plan["parent"]["mntns"]
            and current["cgroup"] == plan["cgroup"] + "/supervisor"
            and proc_identity(plan["parent"]["pid"]) == plan["parent"],
            "private IPv6 sysctl namespace/resource identity differs")


def ipv6_sysctl(interface, *, write=False):
    require(interface in {"all", "default", "lo"} and type(write) is bool
            and (not write or interface in {"all", "default"}), "illegal private IPv6 sysctl scope")
    path = Path("/proc/sys/net/ipv6/conf") / interface / "disable_ipv6"
    fd = os.open(path, (os.O_RDWR if write else os.O_RDONLY) | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0, "unexpected private IPv6 sysctl inode")
        if write:
            require(os.write(fd, b"1\n") == 2, "short private IPv6 sysctl write")
            os.lseek(fd, 0, os.SEEK_SET)
        value = os.read(fd, 3)
        require(value in {b"0\n", b"1\n"}, "invalid private IPv6 sysctl value")
        return value.decode().strip()
    finally:
        os.close(fd)


def restrict_private_ipv6(commands, root, plan, expected):
    """Restrict this new netns before devices exist; proc writes are runtime state, not fsync durability."""
    record = {"schema": 1, "status": "incomplete", "netns": expected["netns"], "writes": [],
              "before": {}, "after": {}, "kernel_state_durability": "runtime_only_not_fsync"}
    try:
        private_namespace_context(plan, expected)
        before = namespace_snapshot(commands)
        require(before["netns"] == expected["netns"] and [r["ifname"] for r in before["links"]] == ["lo"],
                "private IPv6 restriction requires fresh loopback-only namespace")
        record["before"] = {name: ipv6_sysctl(name) for name in ("all", "default", "lo")}
        for name in ("all", "default"):
            private_namespace_context(plan, expected)
            require(ipv6_sysctl(name, write=True) == "1", "private IPv6 sysctl write readback differs")
            record["writes"].append(name)
            private_namespace_context(plan, expected)
        after = namespace_snapshot(commands)
        require(after["netns"] == expected["netns"] and [r["ifname"] for r in after["links"]] == ["lo"],
                "private IPv6 namespace changed during restriction")
        record["after"] = {name: ipv6_sysctl(name) for name in ("all", "default", "lo")}
        # conf/all is not an aggregate status: check the actual existing interface and future-device default.
        require(record["after"]["default"] == record["after"]["lo"] == "1", "private IPv6 interface/default not disabled")
        private_namespace_context(plan, expected)
        record["status"] = "restricted"
    except BaseException as error:
        record["error_type"] = type(error).__name__
        record["error_sha256"] = hashlib.sha256(str(error).encode()).hexdigest()
        preserve_namespace_observation(root / "PRIVATE_IPV6_RESTRICTION.json", record, primary=error)
        raise
    durable(root / "PRIVATE_IPV6_RESTRICTION.json", record)


def daemon_config(root, plan):
    return {"hosts": ["unix:///var/run/docker.sock"], "data-root": str(root / "data"),
            "exec-root": str(root / "exec"), "pidfile": str(root / "daemon.pid"), "bridge": "none",
            "storage-driver": "vfs", "exec-opts": ["native.cgroupdriver=cgroupfs"],
            "cgroup-parent": plan["cgroup"] + "/containers", "iptables": True, "ip6tables": False,
            "ipv6": False, "userland-proxy": False, "live-restore": True, "dns": ["127.0.0.1"]}


def check_peer(pid, expected):
    require(proc_identity(pid) == expected, "actual daemon process drift")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        sock.connect("/var/run/docker.sock")
        peer = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    require(peer == (pid, 0, 0) and proc_identity(pid) == expected, "private daemon socket peer mismatch")


def wait_until(check, deadline):
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    raise TimeoutError("bounded observation expired")


def start_daemon(commands, root, plan):
    resolver = resolver_target()
    commands.run([str(TOOLS["mount"]), "--make-rprivate", "/"])
    (root / "run").mkdir(mode=0o700)
    commands.run([str(TOOLS["mount"]), "--bind", str(root / "run"), "/run"])
    require(Path("/var/run").resolve() == Path("/run") and not Path("/var/run/docker.sock").exists(),
            "host socket was not shadowed")
    mounts = Path("/proc/self/mountinfo").read_text().splitlines()
    require(len(mounts) <= 4096 and all(not any(field.startswith(("shared:", "master:")) for field in line.split()[6:])
                                      for line in mounts), "private mount propagation not established")
    durable(root / "PRIVATE_MOUNTS.txt", ("\n".join(mounts) + "\n").encode())
    durable(root / "resolv.conf", b"nameserver 127.0.0.1\noptions attempts:1 timeout:1\n")
    if resolver != Path("/etc/resolv.conf"):
        # /etc/resolv.conf is commonly a link into /run. Its target is now in
        # our private bind, so creating it cannot alter the runner's files.
        resolver.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolver.touch(mode=0o600, exist_ok=False)
    commands.run([str(TOOLS["mount"]), "--bind", str(root / "resolv.conf"), "/etc/resolv.conf"])
    commands.run([str(TOOLS["ip"]), "link", "set", "lo", "up"])
    durable(root / "NAMESPACE_BEFORE.json", namespace_snapshot(commands, evidence_path=root / "NAMESPACE_BEFORE_OBSERVATION.json"))
    durable(root / "daemon.json", daemon_config(root, plan))
    process, logs = launch_daemon(commands, root, plan)
    retain_daemons(deadline=commands.deadline)
    actual = proc_identity(process.pid)
    require(actual["cgroup"] == plan["cgroup"] + "/supervisor"
            and actual["netns"] == os.stat("/proc/self/ns/net").st_ino, "daemon namespace/resource escape")
    durable(root / "DAEMON.json", actual)

    def ready():
        require(process.poll() is None, "private daemon exited during startup")
        if not Path("/var/run/docker.sock").exists():
            return False
        check_peer(process.pid, actual)
        return True

    wait_until(ready, min(commands.deadline, time.monotonic() + 10))
    def peer():
        logs.check()
        require(retainer_confirmed(process), "live daemon pipe retainer disappeared")
        check_peer(process.pid, actual)
    commands.peer = peer
    durable(root / "DAEMON_INFO.json", json.loads(commands.docker(["info", "--format", "{{json .}}"])))
    return process, actual, logs


def launch_daemon(commands, root, plan):
    process = subprocess.Popen([str(TOOLS["dockerd"]), "--config-file", str(root / "daemon.json")],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=commands.env, start_new_session=True)
    # Register immediately: even thread.start() can fail at the PID limit.
    # The outermost holder must own the original pipe FDs on that path too.
    index = len(LIVE_DAEMONS)
    LIVE_DAEMONS.append((process, None, root, plan))
    logs = DaemonLogs(process, root)
    LIVE_DAEMONS[index] = (process, logs, root, plan)
    return process, logs


def resolver_target():
    path = Path("/etc/resolv.conf")
    if not path.is_symlink():
        require(stat.S_ISREG(path.lstat().st_mode), "regular host resolver mountpoint required")
        return path
    actual = path.resolve(strict=True)
    require(actual.is_relative_to("/run") and len(actual.parts) <= 8 and stat.S_ISREG(actual.lstat().st_mode),
            "host resolver alias outside isolated run tree")
    return actual


class DaemonLogs:
    """Bound raw output; overflow leaves pipes open/backpressured until retained exit.

    The helper stays alive on an unsafe cleanup, including log overflow, so its
    pipe descriptors cannot force a live daemon to exit via EPIPE. External
    timeout is only a controller timeout; these detached processes remain capped.
    """
    def __init__(self, process, root):
        self.error, self.process, self.thread = None, process, None
        self.thread = threading.Thread(target=self._read, args=(root,), daemon=True)
        self.thread.start()

    def _read(self, root):
        try:
            # Infinite deadline is only for passive pipe preservation. The active
            # protocol is bounded independently; the retained process is capped.
            Commands._capture(self.process, root / "daemon.stdout", root / "daemon.stderr", float("inf"), OUTPUT_BYTES,
                              close_pipes=False)
        except BaseException as error:
            self.error = error

    def check(self):
        if self.error:
            raise ProofError("daemon log bound/write failure; live resources retained") from self.error

    def finish(self, deadline):
        self.thread.join(max(0, deadline - time.monotonic()))
        require(not self.thread.is_alive(), "daemon logs EOF unknown")
        self.check()
        self.process.stdout.close()
        self.process.stderr.close()


def pipe_identity(fd):
    info = os.fstat(fd)
    require(stat.S_ISFIFO(info.st_mode) and fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY,
            "retainer accepts only actual read-end pipes")
    return {"fd": fd, "device": info.st_dev, "inode": info.st_ino}


def retain_daemons(*, deadline):
    retained = []
    for process, _logs, root, plan in LIVE_DAEMONS:
        if process.poll() is not None:
            continue
        if process.pid in RETAINERS:
            child, actual = RETAINERS[process.pid]
            require(child.poll() is None, "live daemon retainer exited unexpectedly")
            retained.append(actual)
            continue
        pipes = [pipe_identity(stream.fileno()) for stream in (process.stdout, process.stderr)]
        for stream, proof in zip(("1", "2"), pipes):
            actual = os.stat(Path("/proc") / str(process.pid) / "fd" / stream)
            require((actual.st_dev, actual.st_ino) == (proof["device"], proof["inode"]), "daemon pipe writer identity changed")
        record = {"schema": 1, "pipes": pipes, "daemon": proc_identity(process.pid), "cgroup": plan["cgroup"],
                  "script": file_proof(Path(__file__)), "cleanup_confirmed": False}
        durable(root / "RETAIN.json", record)
        child = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--repo", str(ROOT),
                                  "--output", str(root), "--retain"], pass_fds=tuple(p["fd"] for p in pipes),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}, start_new_session=True)
        actual = proc_identity(child.pid)
        require(actual["cgroup"] == plan["cgroup"] + "/supervisor"
                and actual["netns"] == record["daemon"]["netns"], "retainer identity escaped")
        durable(root / "RETAINER_PROCESS.json", actual)
        ready = root / "RETAINER_READY.json"
        wait_until(lambda: ready.exists() or _retainer_failed(child), min(deadline, time.monotonic() + 3))
        value = json.loads(ready.read_bytes())
        require(value == {"pid": child.pid, "starttime": actual["starttime"], "holds_only_read_pipes": True},
                "retainer readiness differs")
        safe = {"pid": actual["pid"], "starttime": actual["starttime"], "netns": actual["netns"], "mntns": actual["mntns"],
                "cgroup_sha256": hashlib.sha256(actual["cgroup"].encode()).hexdigest()}
        RETAINERS[process.pid] = (child, safe)
        retained.append(safe)
    return retained


def _retainer_failed(child):
    require(child.poll() is None, "retainer exited without confirmation")
    return False


def retainer_confirmed(process):
    retained = RETAINERS.get(process.pid)
    if retained is None or retained[0].poll() is not None:
        return False
    child, expected = retained
    try:
        actual = proc_identity(child.pid)
        if any(actual[key] != expected[key] for key in ("pid", "starttime", "netns", "mntns")):
            return False
        if hashlib.sha256(actual["cgroup"].encode()).hexdigest() != expected["cgroup_sha256"]:
            return False
        for stream in (process.stdout, process.stderr):
            proof = pipe_identity(stream.fileno())
            child_fd = os.stat(Path("/proc") / str(child.pid) / "fd" / str(proof["fd"]))
            if (child_fd.st_dev, child_fd.st_ino) != (proof["device"], proof["inode"]):
                return False
        return child.poll() is None
    except (OSError, ProofError):
        return False


def retain_pipes(root):
    private_directory(root)
    record = json.loads((root / "RETAIN.json").read_bytes())
    require(record["schema"] == 1 and len(record["pipes"]) == 2 and file_proof(Path(__file__)) == record["script"],
            "retainer frozen identity required")
    actual = proc_identity(os.getpid())
    require(actual["cgroup"] == record["cgroup"] + "/supervisor" and actual["netns"] == record["daemon"]["netns"],
            "retainer namespace/cgroup mismatch")
    poll = select.poll()
    for expected in record["pipes"]:
        require(pipe_identity(expected["fd"]) == expected, "retainer pipe changed")
        poll.register(expected["fd"], select.POLLHUP | select.POLLERR)
    durable(root / "RETAINER_READY.json", {"pid": actual["pid"], "starttime": actual["starttime"], "holds_only_read_pipes": True})
    remaining = {entry["fd"] for entry in record["pipes"]}
    while remaining:
        # Poll pipe closure only; no daemon API, retries, business reads, or
        # protocol window extension. Timeout also handles a lost EOF wakeup.
        for descriptor, events in poll.poll(1000):
            require(events & (select.POLLHUP | select.POLLERR), "unexpected retainer event")
            poll.unregister(descriptor)
            os.close(descriptor)
            remaining.remove(descriptor)
    return 0


def container_args(plan, root, network, port):
    return ["create", "--pull", "never", "--platform", "linux/amd64", "--name", "isolated-host-gateway",
            "--label", LABEL + "=" + plan["owner"], "--network", network, "--publish", "127.0.0.1:" + str(port) + ":5000/tcp",
            "--memory", str(128 * 1024**2), "--memory-swap", str(128 * 1024**2), "--pids-limit", "64", "--cpus", "1",
            "--cgroup-parent", plan["cgroup"] + "/containers", "--security-opt", "no-new-privileges:true",
            "--restart", "no", "--no-healthcheck", "--dns", "127.0.0.1",
            "--mount", "type=bind,src=" + str(root / "nginx.conf") + ",dst=/etc/nginx/nginx.conf,readonly",
            "--entrypoint", "nginx", plan["image"]["Id"], "-g", "daemon off;"]


def verify_container(row, plan, network, port, *, running=True, identifier=None):
    host, state = row["HostConfig"], row["State"]
    require(re.fullmatch(r"[0-9a-f]{64}", row["Id"]) and row["Image"] == plan["image"]["Id"]
            and (identifier is None or row["Id"] == identifier)
            and row["Config"]["Labels"].get(LABEL) == plan["owner"] and row["RestartCount"] == 0
            and host["Memory"] == host["MemorySwap"] == 128 * 1024**2 and host["PidsLimit"] == 64
            and host["NanoCpus"] == 1_000_000_000 and host["CgroupParent"] == plan["cgroup"] + "/containers"
            and host["Privileged"] is False and not host.get("CapAdd") and host["RestartPolicy"]["Name"] == "no"
            and "no-new-privileges:true" in host["SecurityOpt"] and host["Dns"] == ["127.0.0.1"]
            and state["Running"] is running and not any(state.get(k, False) for k in ("OOMKilled", "Restarting", "Dead", "Paused")),
            "actual owned gateway identity/resource/state differs")
    require(set(row["NetworkSettings"]["Networks"]) == {network}, "gateway network differs")
    expected = {"5000/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]}
    require(host["PortBindings"] == expected, "requested loopback publication differs")
    if running:
        ports = row["NetworkSettings"]["Ports"]
        require(isinstance(ports, dict) and {k: v for k, v in ports.items() if v is not None} == expected,
                "actual loopback publication missing or changed")
        actual = proc_identity(state["Pid"])
        require(actual["cgroup"].startswith(plan["cgroup"] + "/containers/"), "container escaped shared cgroup")


def guarded_health(transport, expected_port, host, port, operation):
    require(type(transport) is gateway.NativeTransport and host == "127.0.0.1"
            and type(port) is int and port == expected_port and 1024 <= port <= 65535
            and re.fullmatch(r"[0-9a-f]{64}", operation), "illegal synthetic host target")
    return transport.health(host, port, operation)


def observe_host(commands, root, plan, identifier, network, port, bridge):
    end = min(commands.deadline, time.monotonic() + 10)
    transport = gateway.NativeTransport()
    transport.deadline = end
    operation = hashlib.sha256(encoded({"owner": plan["owner"], "container": identifier})).hexdigest()
    for host, target in (("0.0.0.0", port), ("192.0.2.1", port), ("127.0.0.1", 1)):
        try:
            guarded_health(transport, port, host, target, operation)
        except ProofError:
            continue
        raise ProofError("illegal target was not rejected")

    def healthy():
        before = commands.inspect(identifier)
        verify_container(before, plan, network, port, identifier=identifier)
        durable(root / ("HTTP_INSPECT_%04d.json" % commands.sequence), before)
        try:
            response = guarded_health(transport, port, "127.0.0.1", port, operation)
        except (ConnectionError, OSError):
            return False
        require(response["status"] == 200 and response["body_sha256"] == hashlib.sha256(b"ISOLATED_HOST").hexdigest(),
                "real host HTTP response differs")
        after = commands.inspect(identifier)
        verify_container(after, plan, network, port, identifier=identifier)
        require(before["State"]["Pid"] == after["State"]["Pid"] and before["State"]["StartedAt"] == after["State"]["StartedAt"],
                "gateway changed during HTTP")
        durable(root / "HTTP_AFTER.json", after)
        return response

    old = commands.deadline
    commands.deadline = end
    try:
        response = wait_until(healthy, end)
        actual = commands.inspect(identifier)
        durable(root / "NAMESPACE_AFTER.json", namespace_snapshot(
            commands, bridge=bridge, evidence_path=root / "NAMESPACE_AFTER_OBSERVATION.json"))
        processes = process_snapshot(plan, os.stat("/proc/self/ns/net").st_ino, container_pid=actual["State"]["Pid"])
        durable(root / "PROCESSES.json", processes)
        return {"http": response, "port": port, "container_id": identifier, "illegal_targets_rejected": 3}
    finally:
        commands.deadline = old


def stop_owned(commands, root, plan, daemon, daemon_identity, logs):
    """Do not stop dockerd while ANY container status or ownership is unknown."""
    stopped = []
    end, old = min(commands.deadline, time.monotonic() + 15), commands.deadline
    commands.deadline = end
    try:
        identifiers = commands.docker(["ps", "--all", "--no-trunc", "--quiet"]).splitlines()
        require(len(identifiers) <= 1 and all(re.fullmatch(r"[0-9a-f]{64}", x) for x in identifiers),
                "unexpected private daemon inventory; retained")
        for identifier in identifiers:
            row = commands.inspect(identifier)
            require(row["Id"] == identifier and row["Config"]["Labels"].get(LABEL) == plan["owner"]
                    and row["Image"] == plan["image"]["Id"],
                    "unowned container prevents daemon stop")
            expected = _cleanup_identity(row)
            durable(root / "PRESERVE_INSPECT.json", row)
            commands.docker(["logs", identifier])
            term_requested = False
            if row["State"]["Running"]:
                require(row["State"]["Status"] == "running" and all(type(row["State"].get(key)) is bool
                        and row["State"][key] is False for key in ("Paused", "Dead", "Restarting", "OOMKilled")),
                        "unknown container state prevents TERM")
                commands.docker(["kill", "--signal", "TERM", identifier])
                term_requested = True
            row = wait_until(lambda: _stopped(commands, identifier, expected, term_requested=term_requested), end)
            durable(root / "STOPPED_INSPECT.json", row)
            stopped.append(identifier)
        require(not commands.docker(["ps", "--no-trunc", "--quiet"]), "live containers prevent daemon stop")
        require(not commands.uncertain_mutation, "unresolved mutation prevents daemon stop despite empty inventory")
        durable(root / "RESOURCES_FINAL.json", resource_snapshot(plan["cgroup"]))
        check_peer(daemon.pid, daemon_identity)
        retainer = RETAINERS.get(daemon.pid)
        require(retainer is not None and retainer_confirmed(daemon), "live daemon pipe retainer disappeared")
        daemon.send_signal(signal.SIGTERM)
        wait_until(lambda: daemon.poll() is not None, end)
        require(daemon.returncode == 0, "private daemon shutdown unconfirmed")
        logs.finish(end)
        require(RETAINERS.get(daemon.pid) is retainer, "retainer pipe release unknown")
        wait_until(lambda: retainer[0].poll() is not None, end)
        require(retainer[0].returncode == 0, "retainer pipe release unknown")
        durable(root / "CLEANUP.json", {"all_containers_stopped": True, "ids": stopped, "daemon_returncode": daemon.returncode,
                                       "retainer_returncode": retainer[0].returncode,
                                       "removed": False, "forced_kill": False})
        return True
    finally:
        commands.deadline = old


def _cleanup_identity(row):
    keys = ("Id", "Name", "Image", "Created", "Config", "HostConfig", "Mounts", "RestartCount")
    require(all(key in row for key in keys) and type(row["RestartCount"]) is int and row["RestartCount"] == 0
            and type(row["State"].get("Running")) is bool, "complete original cleanup identity required")
    return deepcopy({key: row[key] for key in keys} | {"StartedAt": row["State"].get("StartedAt")})


def _stopped(commands, identifier, expected, *, term_requested=False):
    row = commands.inspect(identifier)
    require(row["Id"] == identifier and _cleanup_identity(row) == expected,
            "gateway cleanup identity changed")
    state = row["State"]
    require(all(type(state.get(key)) is bool and state[key] is False
                for key in ("OOMKilled", "Paused", "Dead", "Restarting")), "gateway abnormal state during stop")
    if state["Running"]:
        require(state["Status"] == "running", "gateway running state unknown")
        return None
    require(state["Status"] == "exited" and type(state.get("ExitCode")) is int
            and state["ExitCode"] in ({0, 143} if term_requested else {0}), "gateway normal exit unconfirmed")
    moments = []
    for name in ("StartedAt", "FinishedAt"):
        value = state.get(name)
        require(type(value) is str and 0 < len(value) <= 64 and value.endswith("Z"), "gateway exit time unconfirmed")
        try:
            moment = datetime.fromisoformat(value[:-1] + "+00:00")
            require(moment.tzinfo == timezone.utc and moment.timestamp() > 0, "gateway exit time unconfirmed")
            moments.append(moment)
        except (ValueError, OverflowError) as error:
            raise ProofError("gateway exit time unconfirmed") from error
    require(moments[1] >= moments[0], "gateway exit order unconfirmed")
    return row


def inner_proof(root, plan):
    commands = Commands(root, plan.get("active_deadline", plan["deadline"]))
    diagnostic_stage("namespace_setup")
    current = proc_identity(os.getpid())
    require(current["netns"] != plan["parent"]["netns"] and current["mntns"] != plan["parent"]["mntns"]
            and current["cgroup"] == plan["cgroup"] + "/supervisor", "private namespaces/resources not entered")
    (root / "client").mkdir(mode=0o700)
    daemon, actual, logs, result, failure = None, None, None, None, None
    try:
        restrict_private_ipv6(commands, root, plan, current)
        diagnostic_stage("daemon_start")
        daemon, actual, logs = start_daemon(commands, root, plan)
        diagnostic_stage("image_load")
        tar = root.parent / "image.tar"
        require(file_proof(tar) == plan["tar"], "offline archive changed before load")
        commands.docker(["image", "load"], transfer=True, input_path=tar, input_proof=plan["tar"])
        require(file_proof(tar) == plan["tar"], "offline archive changed after load")
        image = commands.inspect(plan["image"]["Id"], image=True)
        durable(root / "LOADED_IMAGE.json", image)
        require(image_binding(image, outer=False) == plan["image"], "loaded config/rootfs/ID mismatch")
        bridge, network = "afh" + plan["owner"][:10], "isolated-host-network"
        diagnostic_stage("network_create")
        network_id = commands.docker(["network", "create", "--driver", "bridge", "--label", LABEL + "=" + plan["owner"],
                                      "--opt", "com.docker.network.bridge.name=" + bridge, network])
        require(re.fullmatch(r"[0-9a-f]{64}", network_id), "network create ACK unknown")
        networks = json.loads(commands.docker(["network", "inspect", network_id]))
        require(len(networks) == 1 and networks[0]["Id"] == network_id and networks[0]["Name"] == network
                and networks[0]["Internal"] is False and networks[0]["Driver"] == "bridge"
                and networks[0]["Labels"].get(LABEL) == plan["owner"]
                and networks[0]["Options"].get("com.docker.network.bridge.name") == bridge, "owned bridge differs")
        durable(root / "NETWORK.json", networks[0])
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        durable(root / "nginx.conf", CONFIG)
        diagnostic_stage("container_create")
        identifier = commands.docker(container_args(plan, root, network, port))
        require(re.fullmatch(r"[0-9a-f]{64}", identifier), "container create ACK unknown")
        diagnostic_stage("container_start")
        commands.docker(["start", identifier])
        diagnostic_stage("host_observation")
        result = observe_host(commands, root, plan, identifier, network, port, bridge)
    except BaseException as error:
        failure = error
        retain_first_failure(root, error)
    finally:
        if daemon and actual:
            try:
                diagnostic_stage("cleanup")
                stop_owned(commands, root, plan, daemon, actual, logs)
            except BaseException as cleanup_error:
                if failure:
                    failure.add_note("cleanup retained: " + type(cleanup_error).__name__)
                else:
                    failure = cleanup_error
                    retain_first_failure(root, cleanup_error)
        if failure:
            raise failure
    return {"schema": 1, "status": "passed", "scope": "independent_synthetic_host_namespace_only", **result,
            "image_id": plan["image"]["Id"], "tar_sha256": plan["tar"]["sha256"], "repo_digest_preserved": False,
            "full_activation_verified": False, "runner_global_loopback_verified": False, "production_access": False,
            "shared_memory_bytes": MEMORY, "shared_pids": PIDS, "shared_cpu_max": CPU, "resources": resource_snapshot(plan["cgroup"])}


def host_tools(root, commands):
    result = {}
    for name, path in TOOLS.items():
        resolved = path.resolve(strict=True)
        info = resolved.stat()
        require(info.st_uid == 0 and stat.S_ISREG(info.st_mode) and not info.st_mode & 0o022, "unsafe host executable")
        result[name] = {"path": str(path), "resolved": str(resolved), **file_proof(resolved)}
        commands.run([str(path), "-Version" if name == "ip" else "--version"])
    durable(root / "TOOLS.json", result)
    return result


def outer_proof(root, deadline):
    private_directory(root, fresh=True)
    for directory in ("client", "inner"):
        (root / directory).mkdir(mode=0o700)
    owner = uuid.uuid4().hex
    cgroup = cgroup_setup(owner)
    commands = Commands(root, active_deadline(deadline))
    diagnostic_stage("host_tools")
    host_tools(root, commands)
    diagnostic_stage("source_image")
    image = commands.inspect(gateway.NGINX_REF, image=True)
    durable(root / "SOURCE_IMAGE.json", image)
    binding = image_binding(image, outer=True)
    diagnostic_stage("image_save")
    commands.docker(["image", "save", binding["Id"]], transfer=True, output_path=root / "image.tar")
    require(image_binding(commands.inspect(gateway.NGINX_REF, image=True), outer=True) == binding,
            "outer image changed during export")
    diagnostic_stage("source_binding")
    plan = {"schema": 1, "owner": owner, "deadline": deadline, "active_deadline": commands.deadline,
            "cgroup": cgroup, "parent": proc_identity(os.getpid()),
            "image": binding, "tar": file_proof(root / "image.tar"), "script": file_proof(Path(__file__)),
            "source_sha": commands.run(["/usr/bin/git", "-C", str(ROOT), "rev-parse", "HEAD"]).decode().strip()}
    require(not commands.run(["/usr/bin/git", "-C", str(ROOT), "status", "--porcelain=v1", "--untracked-files=all"]).strip(),
            "clean immutable checkout required")
    durable(root / "PLAN.json", plan)
    args = [str(TOOLS["unshare"]), "--mount", "--net", "--fork", sys.executable, "-I", "-B", str(Path(__file__).resolve()),
            "--repo", str(ROOT), "--output", str(root / "inner"), "--inside"]
    diagnostic_stage("namespace_launch")
    commands.run(args, transfer=True)
    result = json.loads((root / "inner/RESULT.json").read_bytes())
    require(result.get("status") == "passed" and file_proof(Path(__file__)) == plan["script"], "inner proof/source unconfirmed")
    diagnostic_stage("source_readback")
    require(not commands.run(["/usr/bin/git", "-C", str(ROOT), "status", "--porcelain=v1", "--untracked-files=all"]).strip()
            and commands.run(["/usr/bin/git", "-C", str(ROOT), "rev-parse", "HEAD"]).decode().strip() == plan["source_sha"],
            "public source changed during proof")
    result["source_sha"] = plan["source_sha"]
    result["helper_sha256"] = plan["script"]["sha256"]
    return result


def safe_ports(value):
    if not isinstance(value, dict) or len(value) > 8:
        return {"valid_shape": False}
    result = {}
    for key, rows in value.items():
        if not re.fullmatch(r"[0-9]{1,5}/(?:tcp|udp)", key) or (rows is not None and not isinstance(rows, list)):
            return {"valid_shape": False}
        if rows is None:
            result[key] = None
            continue
        if len(rows) > 8 or any(not isinstance(row, dict) or set(row) != {"HostIp", "HostPort"}
                              or not re.fullmatch(r"[0-9a-fA-F:.]{1,45}", str(row["HostIp"]))
                              or not re.fullmatch(r"[0-9]{1,5}", str(row["HostPort"])) for row in rows):
            return {"valid_shape": False}
        result[key] = rows
    return {"valid_shape": True, "bindings": result}


def safe_container(value):
    state, host = value.get("State", {}), value.get("HostConfig", {})
    return {"id": value.get("Id") if re.fullmatch(r"[0-9a-f]{64}", str(value.get("Id"))) else None,
            "image_id": value.get("Image") if re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("Image"))) else None,
            "state": {key: state[key] for key in ("Running", "Paused", "Restarting", "OOMKilled", "Dead", "Pid", "ExitCode")
                      if type(state.get(key)) in {int, bool}},
            "resources": {key: host[key] for key in ("Memory", "MemorySwap", "PidsLimit", "NanoCpus", "Privileged")
                          if type(host.get(key)) in {int, bool}},
            "cgroup_parent_sha256": hashlib.sha256(str(host.get("CgroupParent")).encode()).hexdigest(),
            "requested_ports": safe_ports(host.get("PortBindings")),
            "actual_ports": safe_ports(value.get("NetworkSettings", {}).get("Ports"))}


def safe_namespace(value):
    links, routes, omitted = [], [], 0
    name = re.compile(r"(?:lo|afh[0-9a-f]{10}|veth[0-9a-f]{1,16})")
    for row in value.get("links", [])[:8]:
        if not name.fullmatch(str(row.get("ifname"))):
            omitted += 1
            continue
        links.append({"name": row["ifname"], "index": row.get("ifindex") if type(row.get("ifindex")) is int else None,
                      "kind": row.get("linkinfo", {}).get("info_kind") if row.get("linkinfo", {}).get("info_kind") in {"bridge", "veth"} else None})
    for row in value.get("routes", [])[:64]:
        try:
            address = str(ipaddress.ip_network(row.get("dst"), strict=False))
        except (ValueError, TypeError):
            omitted += 1
            continue
        if not name.fullmatch(str(row.get("dev"))):
            omitted += 1
            continue
        routes.append({"destination": address, "interface": row["dev"], "has_gateway": "gateway" in row})
    return {"netns": value.get("netns") if type(value.get("netns")) is int else None,
            "links": links, "routes": routes, "omitted_invalid_fields": omitted}


def namespace_route_profile(row, roles):
    if not isinstance(row, dict):
        return {"shape_valid": False}
    text, kind, version, prefix = row.get("dst"), "invalid", None, None
    if text in (None, "default", "0.0.0.0/0", "::/0"):
        kind = "default_or_absent"
    else:
        try:
            address = ipaddress.ip_network(text, strict=False)
            version, prefix = address.version, address.prefixlen
            kind = ("loopback" if address.is_loopback else "link_local" if address.is_link_local else
                    "multicast" if address.is_multicast else "private" if address.is_private else "other")
        except (ValueError, TypeError):
            pass
    return {"shape_valid": True, "destination_class": kind, "address_family": version, "prefix_length": prefix,
            "destination_sha256": hashlib.sha256(encoded(text)).hexdigest(),
            "device_role": roles.get(row.get("dev"), "unknown") if isinstance(row.get("dev"), str) else "unknown",
            "device_sha256": hashlib.sha256(encoded(row.get("dev"))).hexdigest(),
            "gateway_present": bool(row.get("gateway")), "gateway_field_present": "gateway" in row,
            "route_type": row.get("type") if isinstance(row.get("type"), str)
            and row["type"] in {"unicast", "local", "broadcast", "multicast", "unreachable"} else None}


def safe_namespace_observation(value):
    links, routes, roles = [], {}, {"lo": "loopback"}
    bridge = value.get("bridge")
    if isinstance(bridge, str):
        roles[bridge] = "owned_bridge"
    raw_links = value.get("links", [])
    for row in (raw_links[:8] if isinstance(raw_links, list) else []):
        if not isinstance(row, dict):
            links.append({"shape_valid": False})
            continue
        name, info = row.get("ifname"), row.get("linkinfo", {})
        kind = info.get("info_kind") if isinstance(info, dict) else None
        if isinstance(name, str) and bridge and kind == "veth" and row.get("master") == bridge:
            roles[name] = "owned_veth"
        links.append({"name_sha256": hashlib.sha256(encoded(name)).hexdigest(),
                      "role": roles.get(name, "unknown") if isinstance(name, str) else "unknown",
                      "kind": kind if isinstance(kind, str) and kind in {"bridge", "veth"} else None,
                      "index": row.get("ifindex") if type(row.get("ifindex")) is int else None})
    for family, values in value.get("routes_by_family", {}).items():
        if family in {"ipv4", "ipv6"}:
            routes[family] = ([namespace_route_profile(row, roles) for row in values[:32]]
                              if isinstance(values, list) else {"shape_valid": False})
    violation = value.get("violation")
    valid_reasons = {"namespace_identity", "interface_bound", "interface_shape", "external_interface",
                     "route_bound", "route_shape", "external_route"}
    if not isinstance(violation, dict) or violation.get("reason") not in valid_reasons:
        violation = None
    else:
        violation = {"reason": violation["reason"],
                     "section": violation.get("section") if violation.get("section") in {"namespace", "links", "ipv4", "ipv6"} else None,
                     "index": violation.get("index") if type(violation.get("index")) is int else None}
    components = {key: {"bytes": row.get("bytes"), "sha256": row.get("sha256")}
                  for key, row in value.get("components", {}).items() if key in {"links", "ipv4", "ipv6"}
                  and isinstance(row, dict) and type(row.get("bytes")) is int and 0 <= row["bytes"] <= OUTPUT_BYTES
                  and re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256")))}
    return {"status": value.get("status") if value.get("status") in {"validated", "rejected", "capture_incomplete"} else None,
            "netns": value.get("netns") if type(value.get("netns")) is int else None,
            "netns_after": value.get("netns_after") if type(value.get("netns_after")) is int else None,
            "links": links, "routes_by_family": routes, "violation": violation, "components": components,
            "capture_error_type": value.get("error_type") if value.get("error_type") in {
                "JSONDecodeError", "ProofError", "TimeoutError", "OSError", "FileNotFoundError", "PermissionError"} else None,
            "capture_error_sha256": value.get("error_sha256")
            if re.fullmatch(r"[0-9a-f]{64}", str(value.get("error_sha256"))) else None}


def safe_process(value):
    return {**{key: value[key] for key in ("pid", "ppid", "starttime", "netns", "mntns") if type(value.get(key)) is int},
            "cgroup_sha256": hashlib.sha256(str(value.get("cgroup")).encode()).hexdigest()}


def safe_ipv6_restriction(value):
    return {"status": value.get("status") if value.get("status") in {"restricted", "incomplete"} else None,
            "netns": value.get("netns") if type(value.get("netns")) is int else None,
            "writes": [key for key in value.get("writes", []) if key in {"all", "default"}],
            "before": {key: v for key, v in value.get("before", {}).items() if key in {"all", "default", "lo"} and v in {"0", "1"}},
            "after": {key: v for key, v in value.get("after", {}).items() if key in {"all", "default", "lo"} and v in {"0", "1"}},
            "kernel_state_durability": "runtime_only_not_fsync",
            "error_type": value.get("error_type") if value.get("error_type") in {
                "ProofError", "OSError", "FileNotFoundError", "PermissionError"} else None,
            "error_sha256": value.get("error_sha256") if re.fullmatch(r"[0-9a-f]{64}", str(value.get("error_sha256"))) else None}


def read_original_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        proof = file_proof(path, limit=OUTPUT_BYTES, fd=fd)
        data = os.read(fd, OUTPUT_BYTES + 1)
        require(len(data) == proof["size"] and file_proof(path, limit=OUTPUT_BYTES, fd=fd) == proof,
                "original changed while reading profile")
        return json.loads(data)
    finally:
        os.close(fd)


def safe_failure_diagnostic(value):
    require(isinstance(value, dict) and set(value) == {"schema", "stage", "last_command", "error"}
            and type(value["schema"]) is int and value["schema"] == 1
            and isinstance(value["stage"], str) and value["stage"] in DIAGNOSTIC_STAGES, "invalid first-failure diagnostic")

    def checked_error(row):
        require(isinstance(row, dict) and set(row) == {"type", "sha256", "literal"}
                and isinstance(row["type"], str) and row["type"] in DIAGNOSTIC_ERRORS | {"unrecognized"}
                and isinstance(row["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
                and (row["literal"] is None or isinstance(row["literal"], str)
                     and row["literal"] in DIAGNOSTIC_LITERALS
                     and hashlib.sha256(row["literal"].encode()).hexdigest() == row["sha256"]), "invalid diagnostic error")
        return dict(row)

    command = value["last_command"]
    if command is not None:
        require(isinstance(command, dict) and set(command) == {
            "sequence", "operation", "state", "elapsed_seconds", "returncode", "error"}, "invalid diagnostic command")
        elapsed, code = command["elapsed_seconds"], command["returncode"]
        require(type(command["sequence"]) is int and 0 < command["sequence"] <= 4096
                and isinstance(command["operation"], str) and command["operation"] in DIAGNOSTIC_OPERATIONS
                and isinstance(command["state"], str) and command["state"] in {"started", "completed", "failed"}
                and (elapsed is None or type(elapsed) in {int, float} and math.isfinite(elapsed) and elapsed >= 0)
                and (code is None or type(code) is int and -255 <= code <= 255), "invalid diagnostic command values")
        command = {**command, "error": checked_error(command["error"]) if command["error"] is not None else None}
    result = {"schema": 1, "stage": value["stage"], "last_command": command, "error": checked_error(value["error"])}
    require(len(encoded(result)) <= 2048, "first-failure diagnostic byte bound")
    return result


def diagnostic_text_original(path):
    """A bounded derivative of one fixed log; the private original is unchanged."""
    from scripts.capture_release_diagnostics import SENSITIVE_KEY_RE, redact_text

    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        proof = file_proof(path, limit=OUTPUT_BYTES, fd=fd)
        os.lseek(fd, max(0, proof["size"] - DIAGNOSTIC_TEXT_BYTES), os.SEEK_SET)
        raw = os.read(fd, DIAGNOSTIC_TEXT_BYTES)
        require(len(raw) == min(proof["size"], DIAGNOSTIC_TEXT_BYTES)
                and file_proof(path, limit=OUTPUT_BYTES, fd=fd) == proof,
                "diagnostic original changed during export")
    finally:
        os.close(fd)
    # A tail may begin inside a credential value whose key was outside the
    # captured window. Never publish that first partial line.
    if proof["size"] > DIAGNOSTIC_TEXT_BYTES:
        boundary = raw.find(b"\n")
        raw = raw[boundary + 1:] if boundary >= 0 else b""
    # Known secret syntax is removed first. Paths and long opaque identities
    # are unnecessary for this daemon-start diagnostic and stay private too.
    lines, long_line = [], False
    for line in raw.decode("utf-8", errors="replace").split("\n"):
        long_line |= len(line) > 4096  # redact_text's existing per-field limit.
        # Daemon logs are unstructured. CLI flags, environment names and bare
        # Bearer values need not have the assignment syntax understood by the
        # general redactor; publish no text from such a line.
        if SENSITIVE_KEY_RE.search(line):
            lines.append("<sensitive-line-redacted>")
            continue
        text = redact_text(line)
        text = re.sub(r"[0-9a-fA-F]{32,}", "<hex>", text)
        text = re.sub(r"(?<![A-Za-z0-9])/(?:[A-Za-z0-9_.:@+-]+/)*[A-Za-z0-9_.:@+-]+", "<path>", text)
        lines.append(text)
    text = "\n".join(lines)
    rendered = text.encode("utf-8")
    truncated = proof["size"] > DIAGNOSTIC_TEXT_BYTES or long_line or len(rendered) > DIAGNOSTIC_TEXT_BYTES
    text = rendered[:DIAGNOSTIC_TEXT_BYTES].decode("utf-8", errors="ignore")
    return {"bytes": proof["size"], "sha256": proof["sha256"], "text": text,
            "truncated": truncated, "source_window": "tail", "redacted": True,
            "execution_proof": False}


def info_command_profile(path):
    value = read_original_json(path)
    if type(value) is not dict or value.get("argv") != [*DOCKER, "info", "--format", "{{json .}}"]:
        return None
    code, elapsed = value.get("returncode"), value.get("elapsed")
    require((code is None or type(code) is int and -255 <= code <= 255)
            and type(elapsed) in {int, float} and math.isfinite(elapsed) and 0 <= elapsed <= 180,
            "invalid fixed Docker info receipt")
    return {"operation": "docker_info", "returncode": code, "elapsed_seconds": elapsed,
            "client_term_sent": value.get("client_term_sent") is True,
            "outcome": "unknown" if code is None else "completed" if code == 0 else "failed",
            "execution_proof": False}


def safe_stop_profile(value):
    require(type(value) is dict and value.get("status") == "unconfirmed", "invalid retained STOP profile")
    error_type, digest = value.get("error_type"), value.get("error_sha256")
    return {"status": "unconfirmed", "cleanup_claimed": value.get("cleanup_confirmed") is True,
            "cleanup_confirmed": False, "execution_proof": False,
            "error_type": error_type if error_type in DIAGNOSTIC_ERRORS else None,
            "error_sha256": digest if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) else None,
            "diagnostics": safe_failure_diagnostic(value["diagnostics"]) if "diagnostics" in value else None}


def cleanup_completed(value):
    return (value.get("all_containers_stopped") is True
        and type(value.get("daemon_returncode")) is int and value["daemon_returncode"] == 0
        and type(value.get("retainer_returncode")) is int and value["retainer_returncode"] == 0
        and value.get("removed") is False and value.get("forced_kill") is False)


def daemon_start_profiles(root, first):
    """Fixed local reads only, before a larger optional export can fail."""
    command = first.get("last_command")
    if first.get("stage") != "daemon_start" or not command or command["operation"] != "docker_info":
        return None
    name = "command-%04d" % command["sequence"]
    result = {}
    for filename in ("daemon.stderr", name + ".json", name + ".stderr", "STOP.json", "CLEANUP.json"):
        try:
            path = root / filename
            proof = file_proof(path, limit=OUTPUT_BYTES)
            value = profile_for(path)
            require(file_proof(path, limit=OUTPUT_BYTES) == proof, "startup diagnostic original changed")
            require(value is not None, "fixed startup diagnostic profile missing")
            result[filename] = {"status": "available", "original": {"bytes": proof["size"], "sha256": proof["sha256"]},
                                "profile": value}
        except FileNotFoundError:
            result[filename] = {"status": "not_present"}
        except (OSError, ValueError, TypeError, KeyError, ProofError) as error:
            result[filename] = {"status": "evidence_insufficient", "error": diagnostic_error(error)}
    require(len(encoded(result)) <= 40 * 1024, "startup diagnostic byte bound")
    return result


def profile_for(path):
    name = path.name
    if name == "daemon.stderr":
        return diagnostic_text_original(path)
    if re.fullmatch(r"command-[0-9]{4}\.json", name):
        return info_command_profile(path)
    if re.fullmatch(r"command-[0-9]{4}\.stderr", name):
        try:
            receipt = info_command_profile(path.with_suffix(".json"))
        except FileNotFoundError:
            return None
        if receipt is not None:
            return diagnostic_text_original(path)
        return None
    if name == "STOP.json":
        return safe_stop_profile(read_original_json(path))
    if name == "FIRST_FAILURE.json":
        return safe_failure_diagnostic(read_original_json(path))
    if name == "PRIVATE_IPV6_RESTRICTION.json":
        return safe_ipv6_restriction(read_original_json(path))
    if name in {"HTTP_AFTER.json", "PRESERVE_INSPECT.json", "STOPPED_INSPECT.json"}:
        return safe_container(read_original_json(path))
    if name == "CLEANUP.json":
        value = read_original_json(path)
        return {key: value.get(key) for key in ("all_containers_stopped", "daemon_returncode", "retainer_returncode", "removed", "forced_kill")
                if type(value.get(key)) in {int, bool}}
    if name in {"NAMESPACE_BEFORE.json", "NAMESPACE_AFTER.json"}:
        return safe_namespace(read_original_json(path))
    if name in {"NAMESPACE_BEFORE_OBSERVATION.json", "NAMESPACE_AFTER_OBSERVATION.json"}:
        return safe_namespace_observation(read_original_json(path))
    if name in {"DAEMON.json", "RETAINER_PROCESS.json"}:
        return safe_process(read_original_json(path))
    if name == "PROCESSES.json":
        values = read_original_json(path)
        require(isinstance(values, list) and len(values) <= PIDS, "profile process bound")
        return [safe_process(row) for row in values]
    if name == "RESOURCES_FINAL.json":
        value = read_original_json(path)
        return {key: text for key, text in value.items()
                if key in {"memory.max", "memory.swap.max", "pids.max", "cpu.max", "memory.current",
                           "memory.peak", "memory.events", "pids.current", "cpu.stat"}
                and isinstance(text, str) and re.fullmatch(r"[a-z_0-9 \n]+", text)}
    return None


def evidence_packet(root):
    """Fixed flat original inventory; never inspect daemon storage or link targets."""
    hashes, profiles, complete, total = {}, {}, True, 0
    allowed = re.compile(r"(?:command-[0-9]{4}\.(?:json|stdout|stderr)|[A-Z_]+\.(?:json|txt)|"
                         r"PRIVATE_IPV6_RESTRICTION\.json|HTTP_INSPECT_[0-9]{4}\.json|"
                         r"daemon\.(?:json|stdout|stderr)|resolv\.conf|nginx\.conf)")
    for directory in (root, root / "inner"):
        if not directory.exists():
            complete = False
            continue
        with os.scandir(directory) as entries:
            for entry in entries:
                if not allowed.fullmatch(entry.name):
                    continue
                require(len(hashes) < 256, "safe evidence original-count bound")
                name = ("inner/" if directory != root else "") + entry.name
                path = Path(entry.path)
                proof = file_proof(path, limit=OUTPUT_BYTES)
                total += proof["size"]
                require(total <= 16 * 1024**2, "safe evidence total input bound")
                hashes[name] = {"sha256": proof["sha256"], "bytes": proof["size"]}
                profile = profile_for(path)
                require(file_proof(path, limit=OUTPUT_BYTES) == proof, "original changed during profile export")
                if profile is not None:
                    profiles[name] = profile
    required = {"inner/HTTP_AFTER.json", "inner/CLEANUP.json", "inner/RESOURCES_FINAL.json",
                "inner/NAMESPACE_BEFORE.json", "inner/NAMESPACE_AFTER.json", "inner/DAEMON.json", "inner/PROCESSES.json",
                "inner/NAMESPACE_BEFORE_OBSERVATION.json", "inner/NAMESPACE_AFTER_OBSERVATION.json",
                "inner/PRIVATE_IPV6_RESTRICTION.json"}
    ipv6 = profiles.get("inner/PRIVATE_IPV6_RESTRICTION.json", {})
    restricted = (ipv6.get("status") == "restricted" and ipv6.get("writes") == ["all", "default"]
                  and ipv6.get("after", {}).get("default") == ipv6.get("after", {}).get("lo") == "1")
    cleanup = cleanup_completed(profiles.get("inner/CLEANUP.json", {}))
    packet = {"schema": 1, "complete": complete and required <= hashes.keys() and restricted and cleanup,
              "scope": "selected_sanitized_metadata_and_original_hashes", "originals": hashes, "profiles": profiles,
              "omitted_raw_data": ["image_tar_bytes", "daemon_storage_and_socket_trees", "raw_private_text_and_config_values"],
              "full_raw_archive_verified": False}
    require(len(encoded(packet)) <= 512 * 1024, "safe evidence packet bound")
    return packet


def failure_evidence(root, deadline, failure):
    """Prefer the original inner failure within the unchanged outer deadline."""
    require(deadline is not None and time.monotonic() < deadline, "deadline expired; no further original reads")
    with gateway._hard_deadline(min(3, deadline - time.monotonic())):
        path = root / "inner" / "FIRST_FAILURE.json"
        require(not path.parent.is_symlink(), "inner diagnostic directory must not be a link")
        if path.exists() or path.is_symlink():
            # Fixed file before the larger optional inventory; neither follows links.
            failure["first_inner_failure"] = safe_failure_diagnostic(read_original_json(path))
        else:
            stop = path.with_name("STOP.json")
            if stop.exists() or stop.is_symlink():
                value = read_original_json(stop)
                failure["first_inner_failure"] = safe_failure_diagnostic(value.get("diagnostics"))
        if "first_inner_failure" in failure:
            profiles = daemon_start_profiles(path.parent, failure["first_inner_failure"])
            if profiles is not None:
                failure["daemon_start_evidence"] = profiles
        require(time.monotonic() < deadline, "deadline expired; no further original reads")
        failure["safe_evidence"] = evidence_packet(root)
        cleanup = failure["safe_evidence"].get("profiles", {}).get("inner/CLEANUP.json", {})
        failure["cleanup_confirmed"] = cleanup_completed(cleanup)


def _main(argv=None):
    global DIAGNOSTICS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--deadline-monotonic", type=float)
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--retain", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root, result, deadline = args.output, None, None
    previous_diagnostics = DIAGNOSTICS
    try:
        require(sys.platform == "linux" and os.geteuid() == 0 and args.repo == ROOT and ROOT == ROOT.resolve(),
                "fixed Linux root-owned source required")
        require(ROOT.stat().st_uid == 0 and Path(__file__).stat().st_uid == 0, "source must be root-owned")
        os.umask(0o077)
        if args.retain:
            require(not args.inside and args.deadline_monotonic is None, "retainer has no active operation arguments")
            return retain_pipes(root)
        DIAGNOSTICS = {"stage": "initial_validation", "last_command": None, "first_failure": None}
        if args.inside:
            # Detach from the unshare client's group. An outer collector timeout
            # must not kill the namespace holder or the daemon group.
            os.setsid()
            private_directory(root)
            plan = json.loads((root.parent / "PLAN.json").read_bytes())
            require(root.name == "inner" and file_proof(Path(__file__)) == plan["script"]
                    and proc_identity(os.getppid())["cgroup"] == plan["cgroup"] + "/supervisor"
                    and 0 < plan["deadline"] - time.monotonic() <= 180, "retained inner plan identity/deadline invalid")
            deadline = plan["deadline"]
            require(plan.get("active_deadline") == deadline - DIAGNOSTIC_RESERVE_SECONDS,
                    "inner active deadline must preserve original diagnostic reserve")
        else:
            deadline = bounded_deadline(args.deadline_monotonic)
        with gateway._hard_deadline(deadline - time.monotonic()):
            with active_phase(deadline):
                result = inner_proof(root, plan) if args.inside else outer_proof(root, deadline)
            if not args.inside:
                result["safe_evidence"] = evidence_packet(root)
                require(result["safe_evidence"]["complete"], "safe evidence incomplete")
            require(time.monotonic() < deadline, "proof/cleanup/export exceeded original deadline")
            durable(root / "RESULT.json", result)
            require(time.monotonic() < deadline, "result durability exceeded original deadline")
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    except BaseException as error:
        failure = {"schema": 1, "status": "unconfirmed", "error_type": type(error).__name__,
                   "error_sha256": hashlib.sha256(str(error).encode()).hexdigest(), "cleanup_confirmed": False,
                   "next_action": "retain_originals_and_live_resources_no_retry_no_kill", "production_access": False,
                   "full_activation_verified": False}
        failure["diagnostics"] = (DIAGNOSTICS and DIAGNOSTICS.get("first_failure")) or failure_diagnostic(error)
        if not args.inside and not args.retain and root.is_dir() and not root.is_symlink():
            try:
                failure_evidence(root, deadline, failure)
            except BaseException as export_error:
                failure["safe_evidence"] = {"complete": False, "error_type": type(export_error).__name__,
                                            "error_sha256": hashlib.sha256(str(export_error).encode()).hexdigest()}
        if root.is_dir() and not root.is_symlink():
            try:
                durable(root / "STOP.json", failure)
            except BaseException as evidence_error:
                error.add_note("evidence failure: " + type(evidence_error).__name__)
        emit_failure(failure)
        return 1
    finally:
        DIAGNOSTICS = previous_diagnostics


def emit_failure(failure):
    try:
        print(json.dumps(failure, sort_keys=True), flush=True)
    finally:
        # Even an already-confirmed retainer can later die. The original inner
        # holder always retains its read ends while ANY daemon is still live.
        # It performs no active protocol work and is outside the collector's
        # process group. EPIPE on the collector stream cannot bypass this guard.
        while any(process.poll() is None for process, *_ in LIVE_DAEMONS):
            signal.pause()


def main(argv=None):
    try:
        return _main(argv)
    finally:
        # This outermost guard also covers unexpected errors during error
        # reporting itself (for example lstat/fsync/stdout failures).
        while any(process.poll() is None for process, *_ in LIVE_DAEMONS):
            signal.pause()


if __name__ == "__main__":
    raise SystemExit(main())
