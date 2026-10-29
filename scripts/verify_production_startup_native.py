"""Real Dockerfile/Gunicorn N=1..4 and worker, with held PostgreSQL locks.

Only new synthetic storage/projects/schema are accepted. No production switch,
legacy binary execution, model request, rollback or power-loss proof occurs.
"""
from __future__ import annotations

import argparse
import ast
import builtins
import hashlib
import json
import math
import os
import re
import selectors
import shutil
import stat
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import time
import traceback
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import private_release_contract as private  # noqa: E402
from scripts import production_native_deadline as native_deadline  # noqa: E402
from scripts import production_startup_fixture as fixture  # noqa: E402
from scripts.verify_private_release_native import advance_stage, save  # noqa: E402
from services import admission_fence as gate, controlled_startup as isolation, production_migration as migration  # noqa: E402
from services import production_startup_control as control  # noqa: E402

DIAGNOSTIC_ROOT = Path(__file__).resolve().parents[1]
DIAGNOSTIC_BYTES = 4096
DIAGNOSTIC_FRAMES = 8
DIAGNOSTIC_INPUT_BYTES = (512 * 1024, 64 * 1024)


def _diagnostic_path(name, root):
    """Resolve only a unique fixed public-source path, never child input."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,100}\.py", name):
        return None
    paths = [root / directory / name for directory in ("", "scripts", "services", "core", "db", "utils", "workers", "routes", "modules")]
    paths = [path for path in paths if path.is_file()]
    if len(paths) != 1:
        return None
    path = paths[0]
    if path.resolve() != path or not path.is_relative_to(root):
        return None
    return path


def _diagnostic_source(name, root):
    """Read only a unique, bounded public source; never a child-supplied path."""
    path = _diagnostic_path(name, root)
    if path is None:
        return None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 256 * 1024:
            return None
        raw = os.read(fd, 256 * 1024 + 1)
        after = os.fstat(fd)
        fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns")
        if len(raw) != info.st_size or any(getattr(after, key) != getattr(info, key) for key in fields):
            return None
        return path, ast.parse(raw)
    finally:
        os.close(fd)


def _diagnostic_frame(frame, root):
    if not isinstance(frame, dict) or set(frame) != {"file", "line", "function"}:
        return None
    line, function = frame["line"], frame["function"]
    if (type(line) is not int or not 0 < line <= 1_000_000 or not isinstance(function, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,100}|<module>", function)):
        return None
    source = _diagnostic_source(frame["file"], root)
    if source is None:
        return None
    path, tree = source
    nodes = list(ast.walk(tree))
    if line > max((getattr(node, "end_lineno", 0) or 0 for node in nodes), default=0):
        return None
    scopes = [node for node in nodes if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
              and node.lineno <= line <= node.end_lineno]
    scope = min(scopes, key=lambda node: node.end_lineno - node.lineno).name if scopes else "<module>"
    if function != scope:
        return None
    classes = {name for name, value in vars(builtins).items()
               if isinstance(value, type) and issubclass(value, BaseException)}
    declared = [node for node in nodes if isinstance(node, ast.ClassDef) and len(node.name) <= 100]
    for _round in range(len(declared)):
        classes.update(node.name for node in declared
                       if any(isinstance(base, ast.Name) and base.id in classes for base in node.bases))
    return {"file": path.name, "line": line, "function": scope}, classes


def _validated_diagnostic(exception_type, locations, *, root=None):
    root = (root or DIAGNOSTIC_ROOT).resolve()
    frames, classes = [], set()
    if isinstance(locations, list) and len(locations) <= 64:
        for frame in locations[-DIAGNOSTIC_FRAMES:]:
            result = _diagnostic_frame(frame, root)
            if result is not None:
                row, declared = result
                frames.append(row)
                classes.update(declared)
    allowed = {name for name, value in vars(builtins).items()
               if isinstance(value, type) and issubclass(value, BaseException)} | classes
    kind = exception_type if isinstance(exception_type, str) and exception_type in allowed else "unknown"
    return {"exception_type": kind, "locations": frames[-DIAGNOSTIC_FRAMES:]}


def public_exception_locations(error, *, root=None):
    """Actual local traceback, restricted to public-source coordinates only."""
    root = (root or DIAGNOSTIC_ROOT).resolve()
    frames, current = [], error.__traceback__
    for _index in range(64):
        if current is None:
            break
        code = current.tb_frame.f_code
        path = Path(code.co_filename)
        if path.is_absolute() and path.is_relative_to(root) and path == _diagnostic_path(path.name, root):
            frames.append({"file": path.name, "line": current.tb_lineno, "function": code.co_name})
        current = current.tb_next
    if current is not None:
        return []  # Incomplete extraction is not a diagnostic backtrace.
    return _validated_diagnostic(type(error).__name__, frames, root=root)["locations"]


def _stdout_diagnostic(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("ambiguous diagnostic JSON")
            value[key] = item
        return value
    packet = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(packet, dict) or packet.get("status") != "unconfirmed":
        return None
    if packet.get("safe_export_complete") is False:
        return packet.get("error_type"), packet.get("locations", [])
    files = packet.get("safe_evidence", {}).get("files", {})
    failure = files.get("FAILURE.json", {}).get("content")
    if failure is None:
        result = files.get("RESULT.json", {}).get("content", {})
        failure = result.get("failure")
    if isinstance(failure, dict):
        return failure.get("type"), failure.get("locations", [])
    return None


def _stderr_diagnostic(raw):
    """Only a complete conventional traceback; arbitrary text is not a frame."""
    text = raw.decode("utf-8", errors="strict")
    if not text.endswith("\n"):
        return None
    lines = text.splitlines()
    if not lines or lines[0] != "Traceback (most recent call last):":
        return None
    final = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]{0,100})(?::[^\r\n]*)?", lines[-1])
    if final is None:
        return None
    frames, current = [], False
    for line in lines[1:-1]:
        match = re.fullmatch(r'  File "([^"\r\n]{1,1024})", line ([1-9][0-9]{0,6}), in ([A-Za-z_][A-Za-z0-9_]{0,100}|<module>)', line)
        if match:
            current = True
            path = Path(match[1])
            # stderr paths must be the actual local public source, not merely
            # a matching basename outside it. Structured exports contain no path.
            root = DIAGNOSTIC_ROOT.resolve()
            if path.is_absolute() and path.is_relative_to(root) and path == _diagnostic_path(path.name, root):
                frames.append({"file": path.name, "line": int(match[2]), "function": match[3]})
        elif not current or not line.startswith("    "):
            return None
    return (final[1], frames) if current and len(frames) <= 64 else None


def _emit_archive_diagnostic(captured, complete):
    """Untrusted output yields diagnostic candidates, never execution proof."""
    value = {"exception_type": "unknown", "locations": []}
    if complete:
        for raw, parser in zip(captured, (_stdout_diagnostic, _stderr_diagnostic)):
            try:
                candidate = parser(bytes(raw)) if raw is not None else None
                if candidate is not None:
                    checked = _validated_diagnostic(*candidate)
                    if checked["locations"] or value["exception_type"] == "unknown":
                        value = checked
            except Exception:
                continue  # No child text or parser exception is printed.
    record = {"schema": 1, "native_command_diagnostic": "source_validated_candidates",
              "execution_proof": False, "status": "locations_available" if value["locations"] else "no_verified_frames", **value}
    try:
        raw = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        if len(raw.encode()) <= DIAGNOSTIC_BYTES:
            sys.stderr.write(raw)
            sys.stderr.flush()
    except Exception:
        pass  # Diagnostic output must never replace the original failure.


def wait_for_slots(session, p, expected, *, attempts=None, seconds=40):
    deadline = time.monotonic() + native_deadline.clamp(seconds)
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


def scenario(root, workspace, workers, raw_dsn, miniflux_source, stage, report, shared_source):
    stage(f"n{workers}_prepare_disk_and_actual_pg")
    p = fixture.prepare(workspace / f"n{workers}", private.source_identity(root), workers, raw_dsn, miniflux_source)
    sealed, digest = None, None
    try:
        stage(f"n{workers}_seal_created_synthetic_project")
        sealed = fixture.seal_inputs(root, p)
        contract, _manifest, contract_sha = private.load(sealed, root, require_fresh=True)
        stage(f"n{workers}_build_actual_application_image")
        digest = build_candidate(root, p["directory"], p["source"], "ai-filter-startup-native:" + p["project"], contract, contract_sha, shared_source=shared_source)
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



def _finish_archive_outputs(files, path, failure, captured, complete):
    errors = []
    for stream in files:
        try:
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException as error:
            errors.append(error)
        try:
            stream.close()
        except BaseException as error:
            errors.append(error)
    try:
        gate._sync_directory(path.parent)
    except BaseException as error:
        errors.append(error)
    if failure is not None:
        try:
            _emit_archive_diagnostic(captured, complete)
        except BaseException as error:
            errors.append(error)
        for error in errors:
            failure.add_note("synthetic command output preservation failed: " + type(error).__name__)
    elif errors:
        raise errors[0]


def archive_command(args, path, *, seconds=120, limit=8 * 1024**2, child_umask=-1, stdin_bytes=None):
    """Bound both pipes while retaining original synthetic tool output."""
    private.require(stdin_bytes is None or (isinstance(stdin_bytes, bytes) and len(stdin_bytes) <= 256),
                    "fixed synthetic command stdin bound exceeded")
    total, complete, failure = 0, False, None
    captured = [bytearray(), bytearray()]
    deadline = time.monotonic() + native_deadline.clamp(seconds)
    files = []
    try:
        for suffix in (".stdout", ".stderr"):
            stream = open(str(path) + suffix, "xb", buffering=0)
            files.append(stream)
            os.chmod(stream.name, 0o600)
        with native_deadline.process(subprocess.Popen, args, stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                              umask=child_umask) as process:
            try:
                if stdin_bytes is not None:
                    process.stdin.write(stdin_bytes)
                    process.stdin.close()
                with selectors.DefaultSelector() as selector:
                    for index, (pipe, stream) in enumerate(zip((process.stdout, process.stderr), files)):
                        selector.register(pipe, selectors.EVENT_READ, (index, stream))
                    while selector.get_map():
                        private.require(time.monotonic() < deadline, "synthetic command deadline exceeded")
                        for key, _events in selector.select(min(1, max(0, deadline - time.monotonic()))):
                            chunk = os.read(key.fileobj.fileno(), 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            total += len(chunk)
                            private.require(total <= limit, "synthetic command evidence byte bound exceeded")
                            index, stream = key.data
                            private.require(stream.write(chunk) == len(chunk), "synthetic command evidence short write")
                            os.fsync(stream.fileno())
                            private.require(time.monotonic() < deadline, "synthetic command deadline exceeded")
                            if captured[index] is not None:
                                if len(captured[index]) + len(chunk) > DIAGNOSTIC_INPUT_BYTES[index]:
                                    captured[index] = None
                                else:
                                    captured[index].extend(chunk)
                    complete = True
                    private.require(process.wait(timeout=max(0.01, deadline - time.monotonic())) == 0,
                                    "synthetic command failed; original evidence retained")
            finally:
                if process.poll() is None:
                    native_deadline.finish_client(process, sys.exc_info()[1])
    except BaseException as error:
        failure = error
        raise
    finally:
        _finish_archive_outputs(files, path, failure, captured, complete)


def extract_public_source(archive, context, evidence, *, deadline=None):
    # The outer workspace remains private. Only clean public Git archive files
    # need normal image-source modes: Docker COPY makes them root-owned while
    # the reviewed application UID must still be able to read/execute them.
    archive_command(["tar", "-xf", str(archive), "--no-same-permissions", "-C", str(context)], evidence,
                    child_umask=0o022, seconds=phase_seconds(120, deadline))



SOURCE_BYTES = 128 * 1024**2
SOURCE_ENTRIES = 4096
SOURCE_ID_FIELDS = ("st_dev", "st_ino", "st_uid", "st_gid", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")


def _source_identity(info):
    return {name: getattr(info, name) for name in SOURCE_ID_FIELDS}


def _source_file(fd):
    before = os.fstat(fd)
    private.require(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid() and before.st_nlink == 1,
                    "public source must be an owned unlinked regular file")
    private.require(before.st_size <= SOURCE_BYTES, "public source file bound exceeded")
    os.lseek(fd, 0, os.SEEK_SET)
    digest, total = hashlib.sha256(), 0
    while chunk := os.read(fd, 1024 * 1024):
        total += len(chunk)
        private.require(total <= SOURCE_BYTES, "public source read bound exceeded")
        digest.update(chunk)
    private.require(total == before.st_size and _source_identity(os.fstat(fd)) == _source_identity(before),
                    "public source changed during read")
    return {"identity": _source_identity(before), "sha256": digest.hexdigest()}


def _source_tree(fd):
    records, total = {}, 0
    def visit(directory, prefix, depth):
        nonlocal total
        before = os.fstat(directory)
        private.require(depth <= 32 and stat.S_ISDIR(before.st_mode) and before.st_uid == os.geteuid(),
                        "public source directory identity differs")
        records[prefix or "."] = {"identity": _source_identity(before), "kind": "directory"}
        private.require(len(records) <= SOURCE_ENTRIES, "public source entry bound exceeded")
        with os.scandir(directory) as entries:
            for entry in entries:
                private.require(len(records) < SOURCE_ENTRIES, "public source entry bound exceeded")
                name = entry.name
                path = prefix + "/" + name if prefix else name
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                private.require(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode), "public source link or special file rejected")
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                child = os.open(name, flags | (os.O_DIRECTORY if stat.S_ISDIR(info.st_mode) else 0), dir_fd=directory)
                try:
                    private.require(_source_identity(os.fstat(child)) == _source_identity(info), "public source entry replaced")
                    if stat.S_ISDIR(info.st_mode):
                        visit(child, path, depth + 1)
                    else:
                        records[path] = {"kind": "file", **_source_file(child)}
                        total += info.st_size
                        private.require(total <= SOURCE_BYTES, "public source tree byte bound exceeded")
                    private.require(_source_identity(os.stat(name, dir_fd=directory, follow_symlinks=False)) == _source_identity(info),
                                    "public source path replaced")
                    private.require(len(records) <= SOURCE_ENTRIES, "public source entry bound exceeded")
                finally:
                    os.close(child)
        private.require(_source_identity(os.fstat(directory)) == _source_identity(before), "public source directory changed")
    visit(fd, "", 0)
    return records


def _source_content(records):
    result = {}
    for name, item in records.items():
        info = item["identity"]
        row = {"kind": item["kind"], "mode": stat.S_IMODE(info["st_mode"])}
        if row["kind"] == "file":
            row.update(size=info["st_size"], sha256=item["sha256"])
        result[name] = row
    return result


def _archive_content(fd):
    expected, total = {}, 0
    os.lseek(fd, 0, os.SEEK_SET)
    with os.fdopen(os.dup(fd), "rb") as stream, tarfile.open(fileobj=stream, mode="r:") as archive:
        for member in archive:
            name = member.name.rstrip("/")
            parts = PurePosixPath(name).parts
            private.require(name and not name.startswith("/") and "\\" not in name and "\x00" not in name
                            and all(part not in ("", ".", "..") for part in name.split("/")) and len(parts) <= 32,
                            "public source archive path rejected")
            private.require(name not in expected and (member.isdir() or member.isfile()), "public source archive link or duplicate rejected")
            row = {"kind": "directory" if member.isdir() else "file", "mode": member.mode & 0o777 & ~0o022}
            if member.isfile():
                total += member.size
                private.require(0 <= member.size <= SOURCE_BYTES and total <= SOURCE_BYTES, "public source archive expanded bound exceeded")
                with archive.extractfile(member) as handle:
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                row.update(size=member.size, sha256=digest.hexdigest())
            expected[name] = row
            private.require(len(expected) < SOURCE_ENTRIES, "public source archive entry bound exceeded")
    return expected


class SharedPublicSource:
    """One private, checked public tree; not kernel-enforced immutability.

    Preserve Git extraction modes, including executable/readable image inputs.
    Same-UID mutation between a check and Docker's read remains a host trust
    boundary; the post-build full check rejects all observed changes.
    """

    def __init__(self, root, workspace, source):
        self.root, self.directory, self.source = Path(root), Path(workspace), dict(source)
        self.context = self.directory / "context"
        self.fds, self.outer_fd = [], None
        try:
            private.require(private.source_identity(self.root) == self.source, "shared public source identity differs")
            outer = self.directory.parent.lstat()
            private.require(self.directory.parent.resolve() == self.directory.parent and stat.S_ISDIR(outer.st_mode)
                            and outer.st_uid == os.geteuid() and stat.S_IMODE(outer.st_mode) == 0o700,
                            "shared public source parent must be private and direct")
            self.outer_fd = os.open(self.directory.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.outer_identity = _source_identity(outer)
            private.require(_source_identity(os.fstat(self.outer_fd)) == self.outer_identity, "shared public source parent changed")
            os.mkdir(self.directory.name, mode=0o700, dir_fd=self.outer_fd)
            parent = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.fds.append(parent)
            self.parent_identity = _source_identity(os.fstat(parent))
            private.require(self.parent_identity["st_uid"] == os.geteuid() and stat.S_IMODE(self.parent_identity["st_mode"]) == 0o700,
                            "shared public source directory must be private")
            archive = self.directory / "source.tar"
            archive_command(["git", "-C", str(root), "archive", "--format=tar", "--output=" + str(archive), source["source_sha"]],
                            self.directory / "git-archive")
            archive.chmod(0o600)
            archive_fd = os.open("source.tar", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            self.fds.append(archive_fd)
            self.archive = _source_file(archive_fd)
            expected = _archive_content(archive_fd)
            self.context.mkdir(mode=0o700)
            extract_public_source(archive, self.context, self.directory / "archive-extract")
            context_fd = os.open("context", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            self.fds.append(context_fd)
            self.tree = _source_tree(context_fd)
            content = _source_content(self.tree)
            private.require({key: value for key, value in content.items() if key != "."} == expected,
                            "extracted public source differs from actual archive")
            self.reference = {"schema": "shared-public-source-v1", "source": self.source, "context": str(self.context),
                              "archive_sha256": self.archive["sha256"], "tree_sha256": private.sha(private.encoded(content)),
                              "inventory_sha256": private.sha(private.encoded(self.tree)), "entries": len(self.tree)}
            save(self.directory / "SOURCE.json", {**self.reference, "archive": self.archive, "tree": self.tree})
            gate._sync_directory(self.directory)
            self.check(root, source)
        except BaseException:
            self.close()
            raise

    def _check_parent(self, parent):
        private.require(self.outer_fd is not None and self.directory.parent.resolve() == self.directory.parent,
                        "shared public source parent alias rejected")
        outer = self.directory.parent.lstat()
        for key in ("st_dev", "st_ino", "st_uid", "st_gid", "st_mode"):
            private.require(getattr(outer, key) == self.outer_identity[key] == getattr(os.fstat(self.outer_fd), key),
                            "shared public source parent changed")
        current = self.directory.lstat()
        for key in ("st_dev", "st_ino", "st_uid", "st_gid", "st_mode"):
            private.require(getattr(current, key) == self.parent_identity[key] == getattr(os.fstat(parent), key),
                            "shared public source directory replaced")

    def check(self, root, source):
        private.require(Path(root) == self.root and source == self.source and private.source_identity(root) == self.source,
                        "shared public source identity differs")
        private.require(len(self.fds) == 3, "shared public source is closed")
        parent, archive, context = self.fds
        self._check_parent(parent)
        for name, fd, expected in (("source.tar", archive, self.archive["identity"]), ("context", context, self.tree["."]["identity"])):
            private.require(_source_identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) == expected
                            and _source_identity(os.fstat(fd)) == expected, "shared public source path changed")
        private.require(_source_file(archive) == self.archive and _source_tree(context) == self.tree, "shared public source content changed")
        # Recheck the named anchors after walking, including directory replacement.
        self._check_parent(parent)
        private.require(_source_identity(os.stat("context", dir_fd=parent, follow_symlinks=False)) == self.tree["."]["identity"]
                        and _source_identity(os.stat("source.tar", dir_fd=parent, follow_symlinks=False)) == self.archive["identity"],
                        "shared public source anchor changed during check")
        return dict(self.reference)

    def close(self):
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds.clear()
        if self.outer_fd is not None:
            os.close(self.outer_fd)
            self.outer_fd = None


def phase_seconds(default, deadline):
    if deadline is None:
        return default
    private.require(type(deadline) in {int, float} and math.isfinite(deadline), "original native build deadline required")
    seconds = deadline - time.monotonic()
    private.require(0 < seconds <= 600, "original native build deadline expired or replaced")
    return min(default, seconds)


def probe_candidate(contract, directory, digest, workspace, *, deadline=None):
    sequence = 0
    def retained_run(args):
        nonlocal sequence
        path = workspace / f"image-probe-{sequence}"
        sequence += 1
        # These inputs are synthetic only. Production probing keeps its normal
        # no-output behavior; this transport retains bounded original stderr.
        archive_command(args, path, seconds=phase_seconds(120, deadline), limit=private.MAX_BYTES)
        return Path(str(path) + ".stdout").read_bytes()
    private.image_probe(contract, directory, digest, runner=retained_run)


def _oneoff_public_source(root, workspace, source, *, deadline=None):
    context = workspace / "context"
    context.mkdir(mode=0o700)
    archive = workspace / "source.tar"
    archive_command(["git", "-C", str(root), "archive", "--format=tar", "--output=" + str(archive), source["source_sha"]],
                    workspace / "git-archive", seconds=phase_seconds(120, deadline))
    archive.chmod(0o600)
    private.require(archive.stat().st_size <= 128 * 1024**2, "synthetic source archive bound exceeded")
    if deadline is None:
        extract_public_source(archive, context, workspace / "archive-extract")
    else:
        extract_public_source(archive, context, workspace / "archive-extract", deadline=deadline)
    return context


def pip_index_build_args(environ):
    """CI-only package index override; unset keeps the Dockerfile default."""
    value = environ.get("STARTUP_PIP_INDEX_URL", "")
    if not value:
        return []
    parsed = urllib.parse.urlsplit(value)
    private.require(parsed.scheme == "https" and bool(parsed.hostname) and "@" not in parsed.netloc
                    and not parsed.query and not parsed.fragment and value.isprintable() and " " not in value,
                    "STARTUP_PIP_INDEX_URL must be a credential-free https URL")
    return ["--build-arg", "PIP_INDEX_URL=" + value]


def build_candidate(root, workspace, source, image, contract, contract_sha, *, shared_source=None, deadline=None):
    phase_seconds(600, deadline)
    if shared_source is None:
        context = (_oneoff_public_source(root, workspace, source) if deadline is None else
                   _oneoff_public_source(root, workspace, source, deadline=deadline))
    else:
        private.require(type(shared_source) is SharedPublicSource, "actual shared public source required")
        reference = shared_source.check(root, source)
        save(workspace / "shared-source-before.json", reference)
        context = shared_source.context
    labels = {"AI_FILTER_BUILD_VERSION": image, "AI_FILTER_BUILD_GIT_SHA": source["source_sha"],
              "AI_FILTER_BUILD_SOURCE_ID": source["source_sha"] + ":clean", "AI_FILTER_BUILD_MANIFEST_SHA": contract["files"]["compose.json"],
              "AI_FILTER_BUILD_CONFIG_SHA": source["example_config_sha256"], "AI_FILTER_PRIVATE_RELEASE_SHA": contract_sha,
              "AI_FILTER_RUNTIME_CONFIG_SHA": contract["files"]["runtime.yaml"]}
    args = ["docker", "build", "--platform", "linux/amd64", "--tag", image]
    for key, value in labels.items():
        args += ["--build-arg", key + "=" + value]
    args += pip_index_build_args(os.environ)
    try:
        archive_command([*args, str(context)], workspace / "application-build", seconds=phase_seconds(600, deadline))
    finally:
        original_failure = sys.exc_info()[1]
        if shared_source is not None:
            try:
                save(workspace / "shared-source-after.json", shared_source.check(root, source))
            except BaseException as verification_error:
                if original_failure is not None:
                    raise original_failure from verification_error
                raise
    if deadline is None:
        image_info = json.loads(private.run(["docker", "image", "inspect", image]))[0]
    else:
        inspect_path = workspace / "bounded-image-inspect"
        archive_command(["docker", "image", "inspect", image], inspect_path,
                        seconds=phase_seconds(120, deadline), limit=private.MAX_BYTES)
        image_info = json.loads(Path(str(inspect_path) + ".stdout").read_bytes())[0]
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
    shared_source = None
    try:
        stage("native_architecture_and_pinned_base")
        private.require(private.run(["docker", "info", "--format", "{{.Architecture}}"]).strip() in {b"amd64", b"x86_64"}, "native amd64 required")
        base = next(line.split()[1] for line in (root / "Dockerfile").read_text().splitlines() if line.startswith("FROM "))
        # Separate bounded preparation, preserving the short inspection timeout.
        archive_command(["docker", "pull", base], output / "pinned-base-pull", seconds=180)
        shared_source = SharedPublicSource(root, workspace / "s", source)
        report["shared_public_source"] = shared_source.reference
        for workers in range(1, 5):
            scenario(root, workspace, workers, parsed["dsn"], miniflux_source, stage, report, shared_source)
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
        if shared_source is not None:
            shared_source.close()
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
