"""Bounded identity evidence only; never a drain, migration or release permit.

No SSH, database, HTTP recovery, model, file creation, lock acquisition or Docker
mutation is implemented here. A future authorized caller may deliver this file
via stdin and collect stdout locally. The explicit manifest is not a claim that
legacy originals exist or that all producers have been stopped.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import time

MAX_OUTPUT = 1024 * 1024
MAX_FILE = 128 * 1024
TOTAL_SECONDS = 25
COMMAND_SECONDS = 5
HEX = re.compile(r"[a-f0-9]{64}\Z")
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")


class EvidenceFailure(RuntimeError):
    """Fixed sanitized reason, never raw subprocess output or input values."""


def require(value, reason):
    if not value:
        raise EvidenceFailure(reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def safe_parents(path):
    for parent in Path(path).parents:
        require(not stat.S_ISLNK(parent.lstat().st_mode), "symlink_parent")


def private_read(path, limit=MAX_FILE, strict_private=True):
    safe_parents(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        owners = {os.geteuid()} if strict_private else {0, os.geteuid()}
        modes = {0o600} if strict_private else {0o400, 0o600, 0o444, 0o644}
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                and before.st_uid in owners and stat.S_IMODE(before.st_mode) in modes
                and before.st_size <= limit, "unsafe_private_input")
        raw = os.read(fd, limit + 1)
        after = os.fstat(fd)
        def signature(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        require(len(raw) == before.st_size and signature(before) == signature(after)
                == signature(Path(path).lstat()), "private_input_changed")
        return raw
    finally:
        os.close(fd)


def parse_manifest(raw):
    def unique(items):
        out = {}
        for key, value in items:
            require(key not in out, "duplicate_manifest_key")
            out[key] = value
        return out
    require(len(raw) <= MAX_FILE, "manifest_size_limit")
    value = json.loads(raw, object_pairs_hook=unique,
                       parse_constant=lambda _: require(False, "nonfinite_manifest"))
    require(isinstance(value, dict) and set(value) == {
        "schema", "docker_binary", "docker_sha256", "containers", "images", "files"},
        "manifest_fields")
    require(value["schema"] == "takeover-readonly-preflight-v1", "manifest_schema")
    require(value["docker_binary"] in {"/usr/bin/docker", "/usr/local/bin/docker"}
            and isinstance(value["docker_sha256"], str) and HEX.fullmatch(value["docker_sha256"]),
            "docker_binary_binding")
    for key, maximum in (("containers", 8), ("images", 4)):
        items = value[key]
        require(isinstance(items, list) and 1 <= len(items) <= maximum
                and all(isinstance(item, str) for item in items)
                and len(set(items)) == len(items), "target_count_or_duplicates")
        pattern = NAME if key == "containers" else re.compile(r"sha256:[a-f0-9]{64}\Z")
        require(all(pattern.fullmatch(item) for item in items), "target_identity_format")
    require(isinstance(value["files"], list) and len(value["files"]) <= 16, "file_target_limit")
    for item in value["files"]:
        require(isinstance(item, dict) and set(item) == {"path", "kind"}
                and isinstance(item["path"], str) and item["path"].startswith("/")
                and "\x00" not in item["path"] and ".." not in Path(item["path"]).parts
                and item["kind"] in {"metadata", "sha256"}, "file_target_binding")
    require(len({item["path"] for item in value["files"]}) == len(value["files"]), "duplicate_file")
    return value


def executable_identity(path, expected):
    current = Path(path)
    for parent in [*reversed(current.parents), current]:
        info = parent.lstat()
        require(not stat.S_ISLNK(info.st_mode) and not info.st_mode & 0o022
                and info.st_uid in {0, os.geteuid()}, "unsafe_docker_executable")
    require(current.is_file() and os.access(current, os.X_OK), "docker_not_executable")
    h, size, started = hashlib.sha256(), 0, time.monotonic()
    before = current.stat()
    require(before.st_size <= 256 * 1024**2, "docker_executable_size")
    with current.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            size += len(chunk)
            require(size <= 256 * 1024**2 and time.monotonic() - started < COMMAND_SECONDS,
                    "docker_executable_budget")
            h.update(chunk)
    after = current.stat()
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
            "docker_executable_changed")
    require(h.hexdigest() == expected, "docker_executable_changed")


def capture(argv, timeout):
    """Kill only this owned CLI client on bounds; never the daemon/container."""
    started = time.monotonic()
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                            stdin=subprocess.DEVNULL)
    selector = selectors.DefaultSelector()
    for stream in (proc.stdout, proc.stderr):
        selector.register(stream, selectors.EVENT_READ)
    output, size = bytearray(), 0
    try:
        while selector.get_map():
            left = timeout - (time.monotonic() - started)
            require(left > 0, "command_timeout")
            for key, _ in selector.select(min(left, 0.1)):
                chunk = key.fileobj.read1(65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                size += len(chunk)
                require(size <= MAX_OUTPUT, "command_output_limit")
                if key.fileobj is proc.stdout:
                    output.extend(chunk)
        left = timeout - (time.monotonic() - started)
        require(left > 0, "command_timeout")
        require(proc.wait(timeout=left) == 0, "command_failed")
        return bytes(output)
    except subprocess.TimeoutExpired:
        raise EvidenceFailure("command_timeout") from None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=1)
        selector.close()
        proc.stdout.close()
        proc.stderr.close()


def container_rows(raw, names):
    rows = json.loads(raw)
    require(isinstance(rows, list) and len(rows) == len(names), "container_count")
    require({row.get("Name", "").lstrip("/") for row in rows} == set(names), "container_names")
    result = []
    for row in rows:
        config, host, state = row["Config"], row["HostConfig"], row["State"]
        labels = config.get("Labels") or {}
        result.append({"name": row["Name"], "id": row["Id"], "image_id": row["Image"],
            "revision": labels.get("org.opencontainers.image.revision"),
            "running": state["Running"], "started_at": state["StartedAt"],
            "restart_count": row["RestartCount"], "oom_killed": state["OOMKilled"],
            "health": state.get("Health", {}).get("Status"),
            "memory": host.get("Memory"), "memory_swap": host.get("MemorySwap"),
            "pids": host.get("PidsLimit"), "nano_cpus": host.get("NanoCpus"),
            "user": config.get("User"), "working_dir": config.get("WorkingDir"),
            "execution_sha256": digest(json.dumps({k: config.get(k) for k in
                ("Cmd", "Entrypoint", "Env", "Healthcheck")}, sort_keys=True).encode()),
            "mounts_sha256": digest(json.dumps(row.get("Mounts", []), sort_keys=True).encode())})
    return result


def image_rows(raw, images):
    rows = json.loads(raw)
    require(isinstance(rows, list) and len(rows) == len(images)
            and {row["Id"] for row in rows} == set(images), "retained_image_identity")
    return [{"id": row["Id"], "architecture": row.get("Architecture"), "os": row.get("Os"),
             "revision": (row.get("Config", {}).get("Labels") or {}).get(
                 "org.opencontainers.image.revision")} for row in rows]


def file_fact(item):
    path = Path(item["path"])
    safe_parents(path)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "unsafe_evidence_file")
    result = {"path_sha256": digest(os.fsencode(path)), "device": info.st_dev,
              "inode": info.st_ino, "uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode),
              "bytes": info.st_size, "mtime_ns": info.st_mtime_ns}
    if item["kind"] == "sha256":
        result["content_sha256"] = digest(private_read(path, strict_private=False))
    return result


def collect(manifest, emit, run=capture, clock=time.monotonic, bind=executable_identity):
    start, completed = clock(), []
    emit({"event": "start", "utc": utc(), "read_only": True,
          "database_queries": 0, "http_requests": 0, "budget_seconds": TOTAL_SECONDS,
          "manifest_binding_sha256": digest(json.dumps(manifest, sort_keys=True).encode())})
    docker = [manifest["docker_binary"], "--host", "unix:///var/run/docker.sock"]
    phases = [("executable", lambda: bind(manifest["docker_binary"], manifest["docker_sha256"])),
              ("containers", lambda: container_rows(run(docker + ["container", "inspect"]
                   + manifest["containers"], COMMAND_SECONDS), manifest["containers"])),
              ("retained_images", lambda: image_rows(run(docker + ["image", "inspect"]
                   + manifest["images"], COMMAND_SECONDS), manifest["images"]))]
    phases.extend((f"file_{i}", lambda item=item: file_fact(item))
                  for i, item in enumerate(manifest["files"]))
    for phase, operation in phases:
        try:
            require(TOTAL_SECONDS - (clock() - start) >= COMMAND_SECONDS + 1, "budget_insufficient")
            phase_start = clock()
            result = operation()
            require(clock() - start <= TOTAL_SECONDS, "total_timeout")
        except Exception as error:
            reason = str(error) if isinstance(error, EvidenceFailure) else "evidence_unavailable"
            emit({"event": "failure", "utc": utc(), "phase": phase, "reason": reason,
                  "completed_phases": completed, "elapsed_seconds": clock() - start})
            return 2
        completed.append(phase)
        emit({"event": "phase", "utc": utc(), "phase": phase, "evidence": result,
              "elapsed_seconds": clock() - phase_start})
    emit({"event": "finish", "utc": utc(), "status": "bounded_identity_snapshot",
          "elapsed_seconds": clock() - start, "release_authorized": False,
          "legacy_originals_proven": False, "queue_empty_proven": False,
          "producer_stop_proven": False, "database_compatibility_proven": False})
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--collect", action="store_true", help="Requires separately authorized host execution")
    args = parser.parse_args()
    try:
        raw = private_read(args.manifest)
        value = parse_manifest(raw)
        if not args.collect:
            print(json.dumps({"status": "plan_only", "manifest_file_sha256": digest(raw),
                              "production_queries": 0, "release_authorized": False}))
            return 0
        print(json.dumps({"event": "input_binding", "manifest_file_sha256": digest(raw)}), flush=True)
        return collect(value, lambda row: print(json.dumps(row), flush=True))
    except Exception as error:
        reason = str(error) if isinstance(error, EvidenceFailure) else "evidence_unavailable"
        print(json.dumps({"event": "failure", "reason": reason, "release_authorized": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
