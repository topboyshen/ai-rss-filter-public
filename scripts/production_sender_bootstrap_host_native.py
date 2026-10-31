"""Exact host131 bootstrap inside a real private daemon host, never production.

The ordinary bridge is confined by independent network/mount namespaces and a
live, source-bound root daemon context. No bridge is opened on the runner's
global Docker daemon. The original 180 seconds include preparation and cleanup.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "scripts"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from services import admission_fence as gate  # noqa: E402
from scripts import production_activation_host_native as isolated  # noqa: E402
from scripts import production_host_ingress_native as host  # noqa: E402
from scripts import production_native_deadline as deadline  # noqa: E402
from scripts import production_sender_bootstrap_native as bootstrap  # noqa: E402
from scripts import private_release_contract as private  # noqa: E402

MODE, MAX_SECONDS, FINAL_SECONDS = "private-host-loopback131-v1", 180, 20


def native_host_identity(context):
    """A receipt cannot satisfy this capability: recheck the actual live host."""
    gate._require(type(context) is isolated.PrivateHostContext, "exact live private host context required")
    plan = context.plan
    state = deadline.STATE.get()
    final = state is not None and state[0] == plan.get("deadline")
    gate._require(plan.get("bootstrap_mode") == MODE and plan.get("gateway_custody") is False
                  and plan["deadline"] == plan["started"] + MAX_SECONDS
                  and plan["active_deadline"] == plan["deadline"] - FINAL_SECONDS,
                  "exact original host131 budget and mode required")
    frozen, _ = isolated.original(context.root.parent / "PLAN.json")
    gate._require(frozen == plan and plan["bootstrap_wrapper"] == host.file_proof(Path(__file__))
                  and plan["bootstrap_source_sha256"] == hashlib.sha256(Path(bootstrap.__file__).read_bytes()).hexdigest()
                  and plan["postgres_transport_source_sha256"] == bootstrap.local_pg.source_sha256(),
                  "frozen native host131 source or plan differs")
    context.check(final=final)
    actual = host.proc_identity(os.getpid())
    gate._require(os.geteuid() == 0 and actual == context.holder
                  and actual["netns"] == context.daemon["netns"] != plan["parent"]["netns"]
                  and actual["mntns"] == context.daemon["mntns"] != plan["parent"]["mntns"],
                  "not the actual independent daemon host")
    process = bootstrap.local_pg._process(context.daemon["pid"])
    gate._require(process["uid"] == 0 and os.readlink(f"/proc/{context.daemon['pid']}/exe") == "/usr/bin/dockerd"
                  and host.file_proof(host.TOOLS["dockerd"]) == plan["daemon_binary"],
                  "private daemon binary or root identity differs")
    host.check_peer(context.daemon["pid"], context.daemon)
    previous_deadline = context.commands.deadline
    context.commands.deadline = plan["deadline"] if final else plan["active_deadline"]
    try:
        if context.network is None:
            observed = host.namespace_snapshot(context.commands,
                evidence_path=context.root / ("NAMESPACE_HOST_%04d.json" % context.commands.sequence))
            gate._require(observed["netns"] == context.holder["netns"], "initial private namespace differs")
        else:
            frozen_network, _ = isolated.original(context.path, context.proof_sha256)
            gate._require(frozen_network == context.network, "frozen private network capability differs")
            row = context.commands.inspect(context.network["network_id"])
            isolated.validate_network(context.network, row, context.network["owner"], context.commands)
    finally:
        context.commands.deadline = previous_deadline
    context.check(final=final)
    own = os.stat("/proc/self/ns/net")
    boot = bootstrap.local_pg._small("/proc/sys/kernel/random/boot_id", 64).decode("ascii").strip()
    return {"boot_id": boot, "network_device": own.st_dev, "network_inode": own.st_ino}


def references():
    from scripts.verify_miniflux_pause_native import PG_IMAGE, PYTHON_IMAGE
    return [PYTHON_IMAGE, PG_IMAGE, bootstrap._base_image(ROOT)]


def outer_plan(args, root, started):
    from scripts import production_offline_registry as registry
    from scripts.verify_miniflux_pause_native import image_checks, source_binding
    end = started + MAX_SECONDS
    cgroup = isolated.cgroup_setup(uuid.uuid4().hex)
    commands = isolated.FullCommands(root, end - FINAL_SECONDS)
    host.host_tools(root, commands)
    binding = source_binding(args.repo, args.source, args.binary)
    source = private.source_identity(args.repo)
    peer = isolated.peer_identity()
    commands.peer = lambda: host.check_peer(peer["pid"], peer)
    images = {}
    for number, reference in enumerate(references()):
        if reference == bootstrap._base_image(ROOT):
            # The original seed131 explicitly pulls this fixed old binary.
            # BuildKit's FROM cache is not an Engine/RepoDigest observation.
            commands.docker(["pull", "--platform", "linux/amd64", reference], transfer=True)
        row = commands.inspect(reference, image=True)
        image_checks(row, reference)
        images[reference] = host.image_binding(row, outer=False)
        isolated.record(root / ("OUTER_DEPENDENCY_%02d.json" % number), row)
    sender = host.image_binding(commands.inspect(args.image_id, image=True), outer=False)
    gate._require(sender["Id"] == args.image_id, "actual fixed sender required")
    commands.docker(["image", "save", args.image_id], transfer=True, output_path=root / "sender.tar")
    gate._require(host.image_binding(commands.inspect(args.image_id, image=True), outer=False) == sender,
                  "sender changed during export")
    bundle = registry.prepare_bundle(root / "bundle", references(), commands.deadline)
    plan = {"schema": isolated.CONTEXT_SCHEMA, "bootstrap_mode": MODE, "gateway_custody": False,
        "started": started, "deadline": end, "active_deadline": end - FINAL_SECONDS,
        "repo": str(args.repo), "source": str(args.source), "binary": str(args.binary),
        "build_evidence": str(args.build_evidence), "source_identity": source, "source_binding": binding,
        "bootstrap_wrapper": host.file_proof(Path(__file__)),
        "bootstrap_source_sha256": hashlib.sha256(Path(bootstrap.__file__).read_bytes()).hexdigest(),
        "postgres_transport_source_sha256": bootstrap.local_pg.source_sha256(),
        "wrapper_source": host.file_proof(Path(isolated.__file__)), "daemon_binary": host.file_proof(host.TOOLS["dockerd"]),
        "cgroup": cgroup, "parent": host.proc_identity(os.getpid()), "outer_peer": peer,
        "references": references(), "images": images, "sender": sender,
        "sender_archive": host.file_proof(root / "sender.tar", limit=isolated.IMAGE_LIMIT), "bundle": bundle}
    isolated.resource_check(plan)
    isolated.record(root / "PLAN.json", plan, sealed=True)
    return plan, commands


def inner_execute(root, plan):
    from scripts import production_offline_registry as registry
    os.setsid()
    holder = host.proc_identity(os.getpid())
    commands = isolated.FullCommands(root, plan["active_deadline"])
    host.restrict_private_ipv6(commands, root, plan, holder)
    isolated.prepare_mounts(commands, root)
    isolated.record(root / "HOLDER.json", holder)
    process = context = logs = result = stopped = failure = None
    try:
        with registry.serve_bundle(root.parent / "bundle", plan["bundle"], expected_netns=holder["netns"],
                                   parent_identity=plan["parent"], deadline=plan["active_deadline"]) as mirror:
            process, daemon, logs = isolated.start_daemon(commands, root, plan, mirror.url)
            context = isolated.PrivateHostContext(root, plan, commands, holder, daemon)
            context.check()
            isolated.load_dependencies(commands, root, plan)
            with deadline.scope(plan["active_deadline"], plan["deadline"]):
                with bootstrap.local_pg.native_private_host(context):
                    result = bootstrap.execute(Path(plan["repo"]), Path(plan["source"]), plan["sender"]["Id"],
                        Path(plan["binary"]), root / "bootstrap", Path(plan["build_evidence"]),
                        predecessor_schema=131, host_loopback=True, private_host_context=context)
            gate._require(result["status"] == "passed", "actual private host131 bootstrap did not pass")
    except BaseException as error:
        failure = error
        if context is not None:
            context.commands.uncertain_mutation = True
    finally:
        if process is not None and context is not None:
            try:
                stopped = isolated.stop_daemon(context, process, logs)
            except BaseException as error:
                if failure is not None:
                    failure.add_note("private daemon retained: " + type(error).__name__)
                else:
                    failure = error
    value = {"status": "passed" if failure is None and stopped is not None else "failed_or_evidence_insufficient",
        "bootstrap_result": result, "daemon_stop": stopped,
        "error_type": type(failure).__name__ if failure else None,
        "error_sha256": hashlib.sha256(str(failure).encode()).hexdigest() if failure else None,
        "production_access": False, "real_models": 0, "existing_126_modified": False,
        "elapsed_seconds": time.monotonic() - plan["started"], "operation_seconds": MAX_SECONDS}
    isolated.record(root / "INNER_RESULT.json", value, sealed=True)
    return value


def observe_child(process, root, plan):
    while process.poll() is None:
        isolated.remaining(plan["deadline"])
        gate._require(isolated.peer_identity() == plan["outer_peer"], "outer daemon changed")
        value = isolated.sealed_child_result(root)
        if value is not None:
            if value["status"] == "passed":
                code = process.wait(timeout=isolated.remaining(plan["deadline"]))
                gate._require(code == 0, "private host131 normal child exit unconfirmed")
            return value
        for name in ("inner.stdout", "inner.stderr"):
            gate._require((root / name).stat().st_size <= isolated.LOG_LIMIT, "private host131 log bound")
        time.sleep(min(0.05, isolated.remaining(plan["deadline"])))
    value = isolated.sealed_child_result(root)
    gate._require(value is not None and process.returncode == (0 if value["status"] == "passed" else 1),
                  "private host131 normal exit/result unconfirmed")
    return value


def outer_execute(args, root, started):
    plan, _ = outer_plan(args, root, started)
    (root / "inner").mkdir(mode=0o700)
    argv = [str(host.TOOLS["unshare"]), "--mount", "--net", "--fork", sys.executable, "-I", "-B", str(Path(__file__)),
        "--repo", str(args.repo), "--source", str(args.source), "--binary", str(args.binary),
        "--image-id", args.image_id, "--build-evidence", str(args.build_evidence), "--output", str(root / "inner"), "--inside"]
    process, streams = None, []
    try:
        for name in ("inner.stdout", "inner.stderr"):
            streams.append(os.fdopen(os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb"))
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=streams[0], stderr=streams[1],
            env=host.Commands(root, plan["active_deadline"]).env, start_new_session=True)
        result = observe_child(process, root, plan)
        gate._require(private.source_identity(args.repo) == plan["source_identity"]
                      and host.file_proof(Path(__file__)) == plan["bootstrap_wrapper"], "native source changed")
        return result
    finally:
        isolated.preserve_outer_outputs(process, root, plan, streams, sys.exc_info()[1])


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "source", "binary", "build-evidence", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    for name in ("repo", "source", "binary", "build_evidence", "output"):
        value = getattr(args, name)
        gate._require(value.is_absolute() and value == value.resolve(), "direct native path required")
    gate._require(args.repo == ROOT and os.geteuid() == 0 and sys.platform == "linux", "fixed Linux root source required")
    return args


def main(argv=None):
    started, previous = time.monotonic(), os.umask(0o077)
    root, failure, result = None, None, None
    try:
        args = arguments(argv)
        root = args.output
        from scripts.production_gateway_switch import _hard_deadline
        if args.inside:
            private.private_directory(root)
            plan, _ = isolated.original(root.parent / "PLAN.json")
            gate._require(root.name == "inner" and plan["bootstrap_mode"] == MODE
                and all(str(getattr(args, key)) == plan[key] for key in ("repo", "source", "binary", "build_evidence"))
                and args.image_id == plan["sender"]["Id"] and host.file_proof(Path(__file__)) == plan["bootstrap_wrapper"],
                "frozen host131 inner invocation differs")
            with _hard_deadline(isolated.remaining(plan["deadline"])):
                result = inner_execute(root, plan)
            return 0 if result["status"] == "passed" else 1
        host.private_directory(root, fresh=True)
        with _hard_deadline(isolated.remaining(started + MAX_SECONDS)):
            result = outer_execute(args, root, started)
    except BaseException as error:
        failure = {"error_type": type(error).__name__, "error_sha256": hashlib.sha256(str(error).encode()).hexdigest()}
    finally:
        os.umask(previous)
    packet = {"status": "passed" if failure is None and result and result["status"] == "passed" else "unconfirmed",
        "private_host": {"mode": MODE, "operation_seconds": MAX_SECONDS,
                         "result": result, "failure": failure}, "safe_evidence": {"complete": False, "files": {}}}
    if root is not None and (root / "inner/bootstrap/RESULT.json").exists():
        try:
            packet["safe_evidence"] = bootstrap.safe_export(root / "inner/bootstrap")
        except BaseException as error:
            packet["status"] = "unconfirmed"
            packet["safe_evidence"] = {"complete": False, "files": {}, "error_type": type(error).__name__}
    print(gate._encoded(packet).decode(), flush=True)
    return 0 if packet["status"] == "passed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
