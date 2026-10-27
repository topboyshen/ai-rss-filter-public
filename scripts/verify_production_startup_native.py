"""Real Dockerfile/Gunicorn N=1..4 and worker, with held PostgreSQL locks.

Only new synthetic storage/projects/schema are accepted. No production switch,
legacy binary execution, model request, rollback or power-loss proof occurs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import selectors
import signal
import shutil
from pathlib import Path
import subprocess
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import private_release_contract as private  # noqa: E402
from scripts import production_startup_fixture as fixture  # noqa: E402
from scripts.verify_private_release_native import advance_stage, save  # noqa: E402
from services import admission_fence as gate, controlled_startup as isolation, production_migration as migration  # noqa: E402
from services import production_startup_control as control  # noqa: E402


def wait_for_slots(session, p, expected, *, attempts=None, seconds=40):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        results = {}
        for slot in expected:
            attempt = (attempts or {}).get(slot, 0)
            path = p["paths"]["slots"] / f"{slot}.{attempt}.json"
            if not gate._present(path):
                continue
            receipt = isolation._read_complete(path)
            result = control.query(session, p["paths"]["grants"], p["paths"]["slots"], slot=slot, attempt=attempt,
                                   operation_id=receipt["attempt_operation_id"])
            if result["status"] == "closed_initialized":
                results[slot] = result
        if set(results) == set(expected):
            return results
        time.sleep(0.1)
    raise RuntimeError("synthetic closed slots did not initialize before deadline")


def kill_owner(p, slot):
    receipt = isolation._read_complete(p["paths"]["slots"] / f"{slot}.0.json")
    if receipt["role"] == "worker":
        private.run(["docker", "kill", "--signal", "KILL", receipt["container_id"]])
    else:
        private.run(["docker", "exec", receipt["container_id"], "python", "-I", "-c",
                     f"import os,signal;os.kill({receipt['owner']['pid']},signal.SIGKILL)"])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        _record, live = control._receipt(p["paths"]["slots"], p["descriptor"], slot, 0)
        container = control._container(receipt["container_id"])
        if not live and (not container["State"]["Running"] or control._actual_process(receipt["container_id"], receipt["owner"]) is None):
            return receipt
        time.sleep(0.1)
    raise RuntimeError("synthetic original owner remains unresolved")


def negative_duplicate(container):
    code = ("from services import production_startup as s\n"
            "from services.admission_fence import AdmissionError\n"
            "for slot in ('web-1','web-999'):\n"
            " try:s.claim('web',slot=slot)\n"
            " except AdmissionError:pass\n"
            " else:raise SystemExit(23)\n")
    private.run(["docker", "exec", container, "python", "-I", "-c", "import sys;sys.path.insert(0,'/app');\n" + code])


def prove_closed(p, api):
    # Requests stay inside the container's isolated network namespace. No key,
    # body, response content or private environment is printed.
    code = ("import urllib.request,urllib.error\n"
            "for path in ('/refresh','/dashboard?llm=1','/dashboard?push=1'):\n"
            " try:r=urllib.request.urlopen('http://127.0.0.1:5000'+path,timeout=3)\n"
            " except urllib.error.HTTPError as e: assert e.code==503\n"
            " else:raise SystemExit(24)\n")
    private.run(["docker", "exec", api, "python", "-I", "-c", code])
    # Closed worker has no business threads. Read process task names from /proc
    # rather than calling a diagnostic that could initialize those subsystems.
    worker = isolation._read_complete(p["paths"]["slots"] / "worker-1.0.json")
    code = ("from pathlib import Path\n"
            f"p=Path('/proc/{worker['owner']['pid']}/task')\n"
            "assert len(list(p.iterdir()))==1\n")
    private.run(["docker", "exec", worker["container_id"], "python", "-I", "-c", code])


def replace_worker(session, p, contract, sealed, digest, old):
    # A new worker container is required. Keep the exited original by renaming
    # it and clearing only Compose's synthetic discovery label is not possible;
    # use Docker create with the exact frozen service arguments instead.
    service = p["manifest"]["services"]["ai-filter-worker"]
    env = private.environment_values(private.private_file(sealed.parent / "environment", sealed=True)[0])
    image = json.loads(private.run(["docker", "image", "inspect", digest]))[0]
    args = ["docker", "create", "--network", "none", "--memory", "768m", "--memory-swap", "768m", "--pids-limit", "256", "--cpus", "1", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--user", service["user"], "--workdir", "/app"]
    for key, value in {"com.docker.compose.project": p["project"], "com.docker.compose.service": "ai-filter-worker",
                       "com.docker.compose.project.working_dir": str(p["paths"]["project"])}.items():
        args += ["--label", key + "=" + value]
    for mount in service["volumes"]:
        source = str(sealed.parent / "runtime.yaml") if mount["source"] == private.RUNTIME else mount["source"]
        args += ["--mount", f"type=bind,src={source},dst={mount['target']}" + (",readonly" if mount.get("read_only") else "")]
    with private.probe_environment_file(p["directory"], private.role_environment(service, image["Config"], env)) as env_file:
        new = private.run([*args, "--env-file", env_file, "--no-healthcheck", digest, *service["command"]]).decode().strip()
    control.replace(session, p["paths"]["grants"], p["paths"]["slots"], slot="worker-1", operation_id=gate.secrets.token_hex(32),
                    previous_receipt_sha256=gate._hash(old), container_id=new)
    private.run(["docker", "start", new])
    wait_for_slots(session, p, ["worker-1"], attempts={"worker-1": 1})


def scenario(root, workspace, workers, raw_dsn, miniflux_source, stage, report):
    stage(f"n{workers}_prepare_disk_and_actual_pg")
    p = fixture.prepare(workspace / f"n{workers}", private.source_identity(root), workers, raw_dsn, miniflux_source)
    sealed, digest = None, None
    try:
        stage(f"n{workers}_seal_created_synthetic_project")
        sealed = fixture.seal_inputs(root, p)
        contract, _manifest, contract_sha = private.load(sealed, root, require_fresh=True)
        stage(f"n{workers}_build_actual_application_image")
        digest = build_candidate(root, p["directory"], p["source"], "ai-filter-startup-native:" + p["project"], contract, contract_sha)
        stage(f"n{workers}_probe_actual_image_configuration")
        probe_candidate(contract, sealed.parent, digest, p["directory"])
        stage(f"n{workers}_prepare_migration_permit")
        permit = fixture.migration_permit(p, sealed, digest)
        stage(f"n{workers}_create_actual_application_containers")
        private.compose(contract, sealed.parent, digest, ["-f", str(sealed.parent / "compose.json"), "create", "--force-recreate", "--no-build", "--pull", "never"])
        records = private.inspect_project(contract)
        by_role = {private.runtime_environment(r["Config"]["Env"])["AI_FILTER_ROLE"]: r["Id"] for r in records}
        with migration.locked_session(p["directory"] / "migration", permit) as session:
            stage(f"n{workers}_journaled_migration_under_actual_locks")
            session.authorize()
            completed = session.migrate()
            gate._require(completed["status"] == "migration_completed", "synthetic migration incomplete")
            descriptor = control.authorize(session, p["paths"]["grants"], operation_id=p["operation"], web_workers=workers, threads=4,
                paths={"database": "/app/data/business/fixture.db", "config": "/app/config.yaml", "freeze": "/app/data/admission_fence",
                       "state": "/app/data/startup_slots"})
            p["descriptor"] = descriptor
            for slot, role in descriptor["slots"].items():
                control.grant(session, p["paths"]["grants"], p["paths"]["slots"], slot=slot, container_id=by_role[role],
                              attempt_operation_id=gate.secrets.token_hex(32))
            stage(f"n{workers}_actual_gunicorn_and_closed_worker")
            private.run(["docker", "start", *by_role.values()])
            outcomes = wait_for_slots(session, p, descriptor["slots"])
            original = {slot: isolation._read_complete(p["paths"]["slots"] / f"{slot}.0.json") for slot in descriptor["slots"]}
            gate._require(len({row["owner_nonce"] for row in original.values()}) == workers + 1, "process owner nonces not independent")
            web = [row for row in original.values() if row["role"] == "web"]
            gate._require(len({row["owner"]["pid"] for row in web}) == workers, "web slots do not have independent processes")
            negative_duplicate(by_role["web"])
            prove_closed(p, by_role["web"])
            session.recheck_inventory()
            stage(f"n{workers}_one_web_child_crash_finite_replacement")
            old_web = kill_owner(p, "web-1")
            for slot in (s for s in descriptor["slots"] if s.startswith("web-") and s != "web-1"):
                actual = control.query(session, p["paths"]["grants"], p["paths"]["slots"], slot=slot, attempt=0,
                                       operation_id=original[slot]["attempt_operation_id"])
                gate._require(actual["status"] == "closed_initialized", "healthy sibling lost after one child crash")
            gate._require(not gate._present(p["paths"]["slots"] / "web-1.1.json"), "unreviewed child replacement occurred")
            control.replace(session, p["paths"]["grants"], p["paths"]["slots"], slot="web-1", operation_id=gate.secrets.token_hex(32),
                            previous_receipt_sha256=gate._hash(old_web), container_id=by_role["web"])
            wait_for_slots(session, p, ["web-1"], attempts={"web-1": 1})
            stage(f"n{workers}_closed_worker_crash_new_container_replacement")
            old_worker = kill_owner(p, "worker-1")
            replace_worker(session, p, contract, sealed, digest, old_worker)
            session.recheck_inventory()
            gate._require(session.query_migration(permit["operation_id"]) == completed, "startup changed migrated database")
            gate._require(all(isolation._read_complete(p["paths"]["slots"] / f"{slot}.0.json") == value for slot, value in original.items()),
                          "original slot evidence changed")
            report.setdefault("scenarios", []).append({"web_workers": workers, "worker_slots": 1, "image_id": digest,
                "migration_steps": len(completed["completed_steps"]), "actual_pg_locks": True, "independent_web_processes": workers,
                "web_replacement": 1, "worker_new_container_replacement": 1, "original_slot_hashes": {k: gate._hash(v) for k, v in original.items()},
                "all_initial_queries": outcomes, "business_open": False, "switch_authorized": False})
    finally:
        original_failure = sys.exc_info()[0] is not None
        try:
            preserve_scenario(p)
        except BaseException as evidence_error:
            report.setdefault("evidence_failures", []).append({"workers": workers, "error_type": type(evidence_error).__name__,
                "message_sha256": hashlib.sha256(str(evidence_error).encode()).hexdigest(), "containers_retained": True})
            if not original_failure:
                raise
        finally:
            p["anchor"].close()
            p["postgres"].close()



def archive_command(args, path, *, seconds=120, limit=8 * 1024**2, child_umask=-1):
    """Bound both pipes while retaining original synthetic tool output."""
    total = 0
    deadline = time.monotonic() + seconds
    files = [open(str(path) + suffix, "xb") for suffix in (".stdout", ".stderr")]
    for stream in files:
        os.chmod(stream.name, 0o600)
    try:
        with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                              umask=child_umask) as process:
            try:
                with selectors.DefaultSelector() as selector:
                    for pipe, stream in zip((process.stdout, process.stderr), files):
                        selector.register(pipe, selectors.EVENT_READ, stream)
                    while selector.get_map():
                        private.require(time.monotonic() < deadline, "synthetic command deadline exceeded")
                        for key, _events in selector.select(min(1, max(0, deadline - time.monotonic()))):
                            chunk = os.read(key.fileobj.fileno(), 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            total += len(chunk)
                            private.require(total <= limit, "synthetic command evidence byte bound exceeded")
                            key.data.write(chunk)
                    private.require(process.wait(timeout=max(0.01, deadline - time.monotonic())) == 0,
                                    "synthetic command failed; original evidence retained")
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
    finally:
        for stream in files:
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
        gate._sync_directory(path.parent)


def extract_public_source(archive, context, evidence):
    # The outer workspace remains private. Only clean public Git archive files
    # need normal image-source modes: Docker COPY makes them root-owned while
    # the reviewed application UID must still be able to read/execute them.
    archive_command(["tar", "-xf", str(archive), "--no-same-permissions", "-C", str(context)], evidence, child_umask=0o022)


def probe_candidate(contract, directory, digest, workspace):
    sequence = 0
    def retained_run(args):
        nonlocal sequence
        path = workspace / f"image-probe-{sequence}"
        sequence += 1
        # These inputs are synthetic only. Production probing keeps its normal
        # no-output behavior; this transport retains bounded original stderr.
        archive_command(args, path, seconds=120, limit=private.MAX_BYTES)
        return Path(str(path) + ".stdout").read_bytes()
    private.image_probe(contract, directory, digest, runner=retained_run)


def build_candidate(root, workspace, source, image, contract, contract_sha):
    context = workspace / "context"
    context.mkdir(mode=0o700)
    archive = workspace / "source.tar"
    archive_command(["git", "-C", str(root), "archive", "--format=tar", "--output=" + str(archive), source["source_sha"]],
                    workspace / "git-archive")
    archive.chmod(0o600)
    private.require(archive.stat().st_size <= 128 * 1024**2, "synthetic source archive bound exceeded")
    extract_public_source(archive, context, workspace / "archive-extract")
    labels = {"AI_FILTER_BUILD_VERSION": image, "AI_FILTER_BUILD_GIT_SHA": source["source_sha"],
              "AI_FILTER_BUILD_SOURCE_ID": source["source_sha"] + ":clean", "AI_FILTER_BUILD_MANIFEST_SHA": contract["files"]["compose.json"],
              "AI_FILTER_BUILD_CONFIG_SHA": source["example_config_sha256"], "AI_FILTER_PRIVATE_RELEASE_SHA": contract_sha,
              "AI_FILTER_RUNTIME_CONFIG_SHA": contract["files"]["runtime.yaml"]}
    args = ["docker", "build", "--platform", "linux/amd64", "--tag", image]
    for key, value in labels.items():
        args += ["--build-arg", key + "=" + value]
    archive_command([*args, str(context)], workspace / "application-build", seconds=600)
    image_info = json.loads(private.run(["docker", "image", "inspect", image]))[0]
    save(workspace / "image-inspect.json", image_info)
    for key, value in {"io.ai-filter.private-release.sha256": contract_sha,
                       "io.ai-filter.runtime-config.sha256": contract["files"]["runtime.yaml"],
                       "io.ai-filter.config.sha256": source["example_config_sha256"],
                       "org.opencontainers.image.revision": source["source_sha"]}.items():
        private.require(image_info["Config"]["Labels"].get(key) == value, "native candidate label differs")
    return image_info["Id"]


def preserve_scenario(p):
    evidence = p["directory"] / "container-evidence"
    evidence.mkdir(mode=0o700)
    ids = private.run(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + p["project"]]).decode().split()
    gate._require(all(len(item) in (12, 64) and all(c in "0123456789abcdef" for c in item) for item in ids), "invalid evidence container identity")
    for identifier in ids:
        archive_command(["docker", "inspect", identifier], evidence / (identifier + "-inspect"))
        archive_command(["docker", "logs", "--timestamps", identifier], evidence / (identifier + "-logs"))
        archive_command(["docker", "stats", "--no-stream", "--format", "{{json .}}", identifier], evidence / (identifier + "-stats"))
        record = json.loads((evidence / (identifier + "-inspect.stdout")).read_bytes())[0]
        if record["State"]["Running"]:
            code = ("import json;from pathlib import Path;p=Path('/sys/fs/cgroup');"
                    "names=['memory.max','memory.peak','memory.current','memory.events','memory.swap.max','memory.swap.current','cpu.max','pids.max','pids.current'];"
                    "print(json.dumps({n:(p/n).read_text()[:16384] for n in names if (p/n).exists()}))")
            archive_command(["docker", "exec", identifier, "python", "-I", "-c", code], evidence / (identifier + "-cgroup"))
    database_evidence = evidence / "database-before-close"
    database_evidence.mkdir(mode=0o700)
    for suffix in ("", "-wal", "-shm"):
        source = Path(str(p["database"]) + suffix)
        if source.exists():
            gate._require(source.stat().st_size <= 64 * 1024**2, "synthetic DB evidence size exceeded")
            target = database_evidence / source.name
            shutil.copyfile(source, target)
            target.chmod(0o600)
            gate._confirm_file(target)
    gate._sync_directory(database_evidence)
    gate._sync_directory(evidence)
    if ids:
        archive_command(["docker", "rm", "-f", *ids], evidence / "synthetic-container-removal")


def execute(root, output, postgres_dsn_file, miniflux_source):
    private.require(sys.platform.startswith("linux"), "native Linux required")
    source = private.source_identity(root)
    raw, _metadata = private.private_file(postgres_dsn_file)
    parsed = gate._parsed(raw)
    private.require(set(parsed) == {"dsn"}, "exact private synthetic DSN file required")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    path = output / "report.json"
    report = {"schema": "production-startup-native-v1", "source_sha": source["source_sha"], "status": "started",
              "synthetic_only": True, "business_open": False, "switch_authorized": False,
              "limitations": ["schema134_seed_not_legacy_pg_upgrade", "no_old_binary_execution", "no_production_cutover", "no_power_loss_proof"]}
    workspace = output / "private-workspace"
    workspace.mkdir(mode=0o700)
    def stage(name):
        advance_stage(path, report, name)
        print("startup_native_stage=" + name, flush=True)
    try:
        stage("native_architecture_and_pinned_base")
        private.require(private.run(["docker", "info", "--format", "{{.Architecture}}"]).strip() in {b"amd64", b"x86_64"}, "native amd64 required")
        base = next(line.split()[1] for line in (root / "Dockerfile").read_text().splitlines() if line.startswith("FROM "))
        # Separate bounded preparation, preserving the short inspection timeout.
        archive_command(["docker", "pull", base], output / "pinned-base-pull", seconds=180)
        for workers in range(1, 5):
            scenario(root, workspace, workers, parsed["dsn"], miniflux_source, stage, report)
        report["events"].append({"stage": report["stage"], "state": "completed", "at_ns": time.time_ns()})
        report["status"] = "passed"
    except BaseException as error:
        report["status"] = "failed"
        report["failure"] = {"type": type(error).__name__, "message_sha256": hashlib.sha256(str(error).encode()).hexdigest(),
            "locations": [{"file": Path(f.filename).name, "function": f.name, "line": f.lineno}
                          for f in traceback.extract_tb(error.__traceback__) if Path(f.filename).resolve().is_relative_to(root)]}
        report["events"].append({"stage": report.get("stage"), "state": "failed", "at_ns": time.time_ns()})
        raise
    finally:
        save(path, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "output", "postgres-dsn-file", "miniflux-source"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    try:
        execute(args.repo.resolve(), args.output.resolve(), args.postgres_dsn_file.resolve(), args.miniflux_source.resolve())
    except BaseException:
        print("Production startup native failed; retained report identifies the stage", file=sys.stderr)
        return 1
    print("Production startup native passed (synthetic only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
