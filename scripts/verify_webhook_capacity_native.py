"""Build exact baseline/candidate images and run bounded native-amd64 probes.

No production Docker contexts, binds, secrets, network or model transport are
used. Containers and private synthetic volumes are retained for the CI job's
lifetime. Candidate failures, missing results, OOMs or timeouts fail this gate;
the immutable old baseline is measured, not assumed to OOM or required to pass.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import tarfile
import threading
import uuid

BASELINE = "0f3f56120f95c2ddcec81f5d38b1287cfce2275c"
BASELINE_DIRECTORY = Path("tests/fixtures/webhook_capacity_baseline")
BASELINE_ARCHIVE_SHA256 = "71209f0961fc8306f606ac71e7c22daf8a82047cc9d1784287cd5eeb8388e278"
BASELINE_MANIFEST_SHA256 = "b29a8df0ac2a0f429900bfdfef114bcc39c5609620d518fbc91b5560bbd18245"
BASELINE_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024
BASELINE_MAX_SOURCE_BYTES = 8 * 1024 * 1024
MEMORY = 768 * 1024 * 1024
PIDS = 256
CASES = ("allocation", "gradient", "bytes", "maxbody", "partial", "restart", "enospc",
         "alias", "alias-restart", "alias-sync", "alias-capacity", "alias-intent", "alias-intent-restart",
         "alias-inventory")
SOURCE_FILES = ("app.py", "routes/webhook_routes.py", "services/webhook_receipts.py")


def save(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command(argv, directory, *, cwd=None, timeout=60, input_bytes=None):
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    with (directory / "stdout.log").open("xb") as stdout, (directory / "stderr.log").open("xb") as stderr:
        try:
            result = subprocess.run(argv, cwd=cwd, input=input_bytes, stdout=stdout,
                                    stderr=stderr, timeout=timeout, check=False)
            rc, error = result.returncode, None
        except subprocess.TimeoutExpired:
            rc, error = None, "client_timeout"
    save(directory / "COMMAND.json", {"argv": argv, "started_utc": started,
         "finished_utc": datetime.now(timezone.utc).isoformat(), "exit_code": rc, "error": error})
    if rc != 0:
        raise RuntimeError(f"command failed ({rc}/{error}); evidence={directory}")
    return (directory / "stdout.log").read_text()


def native_identity(info, *, system=None, machine=None, binfmt=None):
    system = platform.system() if system is None else system
    machine = platform.machine() if machine is None else machine
    if system != "Linux" or machine not in {"x86_64", "AMD64"}:
        raise ValueError("native Linux AMD64 host required; no macOS/ARM emulation")
    if info.get("OSType") != "linux" or info.get("Architecture") not in {"x86_64", "amd64"}:
        raise ValueError("Docker daemon must also be native Linux AMD64")
    if binfmt:
        raise ValueError("AMD64 emulator handler detected; native gate cannot use it")
    return {"system": system, "machine": machine, "docker_architecture": info["Architecture"],
            "docker_os": info["OSType"], "amd64_binfmt_handlers": [], "native_amd64": True}


def verify_environment(docker, out):
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "AMD64"}:
        raise ValueError("native Linux AMD64 required before Docker access")
    info = json.loads(command(docker + ["info", "--format", "{{json .}}"], out / "docker-info"))
    handlers = []
    for item in Path("/proc/sys/fs/binfmt_misc").glob("*"):
        if any(word in item.name.lower() for word in ("x86_64", "amd64", "rosetta")):
            if "enabled" in item.read_text(errors="replace"):
                handlers.append(item.name)
    identity = native_identity(info, binfmt=handlers)
    save(out / "HOST.json", identity)
    return identity


def extract_revision(repo, revision, destination):
    data = subprocess.check_output(["git", "archive", "--format=tar", revision], cwd=repo, timeout=30)
    destination.mkdir(mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
                raise ValueError("unsafe git archive member")
        archive.extractall(destination, filter="data")
    return hashlib.sha256(data).hexdigest()


def baseline_file(path, *, maximum, expected_hash):
    """Read only the frozen ordinary file, without following a final symlink."""
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise ValueError("baseline input is not a bounded ordinary file")
        data = stream.read(maximum + 1)
    if len(data) > maximum or digest_bytes(data) != expected_hash:
        raise ValueError("baseline input hash/size mismatch")
    return data


def baseline_inputs(repo):
    folder = repo / BASELINE_DIRECTORY
    current = repo
    for part in BASELINE_DIRECTORY.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("baseline source directory must not be a symlink")
    manifest = json.loads(baseline_file(folder / "MANIFEST.json", maximum=128 * 1024,
                                      expected_hash=BASELINE_MANIFEST_SHA256))
    if (manifest.get("format") != 1 or manifest.get("source_revision") != BASELINE
            or manifest.get("kind") != "sanitized_minimal_build_context"
            or manifest.get("source_history_included") is not False
            or manifest.get("database_or_business_records_included") is not False):
        raise ValueError("baseline manifest identity mismatch")
    data = baseline_file(folder / "baseline.tar.xz", maximum=BASELINE_MAX_ARCHIVE_BYTES,
                         expected_hash=BASELINE_ARCHIVE_SHA256)
    return manifest, data


def baseline_members(manifest, data):
    rows = manifest.get("files")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 512:
        raise ValueError("invalid baseline file manifest")
    expected = {}
    for row in rows:
        name = row["path"]
        path = Path(name)
        if (not name or path.is_absolute() or ".." in path.parts or "." in name.split("/")
                or path.as_posix() != name or name in expected or row["mode"] not in (0o644, 0o755)
                or type(row["bytes"]) is not int or not 0 <= row["bytes"] <= BASELINE_MAX_SOURCE_BYTES):
            raise ValueError("unsafe baseline manifest entry")
        expected[name] = row
    if sum(row["bytes"] for row in rows) > BASELINE_MAX_SOURCE_BYTES:
        raise ValueError("baseline expanded source budget exceeded")
    result = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as archive:
        seen = set()
        for member in archive:
            row = expected.get(member.name)
            if (row is None or member.name in seen or not member.isreg() or member.linkname
                    or member.size != row["bytes"] or member.mode != row["mode"]):
                raise ValueError("baseline archive entry mismatch")
            value = archive.extractfile(member).read(row["bytes"] + 1)
            if len(value) != row["bytes"] or digest_bytes(value) != row["sha256"]:
                raise ValueError("baseline source content mismatch")
            result.append((row, value))
            seen.add(member.name)
    if seen != set(expected):
        raise ValueError("baseline archive is missing declared sources")
    return result


def extract_baseline(repo, destination):
    """Use audited frozen inputs, never fetch or expose the old Git ancestors."""
    manifest, data = baseline_inputs(repo)
    members = baseline_members(manifest, data)
    destination.mkdir(mode=0o700)
    for row, value in members:
        target = destination / row["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(value)
        target.chmod(row["mode"])
    return {"archive_sha256": digest_bytes(data), "manifest_sha256": BASELINE_MANIFEST_SHA256,
            "source_kind": manifest["kind"], "source_revision": BASELINE,
            "source_history_included": False, "sanitizations": manifest["sanitizations"]}


def build_image(docker, repo, out, role, revision, run_id):
    folder = out / role
    folder.mkdir(mode=0o700)
    context = folder / "source"
    if role == "baseline":
        if revision != BASELINE:
            raise ValueError("baseline revision is fixed")
        provenance = extract_baseline(repo, context)
    else:
        provenance = {"archive_sha256": extract_revision(repo, revision, context),
                      "source_kind": "git_archive", "source_revision": revision}
    source_identity = (revision + ":public-baseline:" + provenance["archive_sha256"]
                       if role == "baseline" else revision + ":clean")
    tag = f"ai-filter-capacity-{role}:{run_id}-{revision[:12]}"
    version = tag
    argv = docker + ["build", "--pull=false", "--platform=linux/amd64", "--tag", tag,
                    "--label", "ai-filter.synthetic-capacity=" + run_id,
                    "--build-arg", "AI_FILTER_BUILD_VERSION=" + version,
                    "--build-arg", "AI_FILTER_BUILD_GIT_SHA=" + revision,
                    "--build-arg", "AI_FILTER_BUILD_SOURCE_ID=" + source_identity,
                    "--build-arg", "AI_FILTER_BUILD_MANIFEST_SHA=" + sha(context / "docker-compose.yml"),
                    "--build-arg", "AI_FILTER_BUILD_CONFIG_SHA=" + sha(context / "config.yaml"), str(context)]
    command(argv, folder / "build", timeout=900)
    image = json.loads(command(docker + ["image", "inspect", tag], folder / "inspect"))[0]
    labels = image.get("Config", {}).get("Labels", {})
    if (image["Architecture"] != "amd64" or labels.get("org.opencontainers.image.revision") != revision
            or labels.get("io.ai-filter.source.identity") != source_identity):
        raise ValueError("candidate image platform/revision identity mismatch")
    source_hashes = {name: sha(context / name) for name in SOURCE_FILES}
    record = {"tag": tag, "image_id": image["Id"], "revision": revision, **provenance,
              "source_identity": source_identity, "source_sha256": source_hashes, "image_created": image["Created"]}
    save(folder / "IMAGE.json", record)
    return record


def summarize_case(rc, final, state, *, expected_image, timed_out=False):
    config = state.get("HostConfig", {})
    runtime = state.get("State", {})
    limits = (config.get("Memory") == MEMORY and config.get("MemorySwap") == MEMORY
              and config.get("PidsLimit") == PIDS and config.get("NetworkMode") == "none")
    image_matches = bool(expected_image) and state.get("Image") == expected_image
    passed = (rc == 0 and runtime.get("ExitCode") == 0 and image_matches
              and final is not None and final.get("status") == "passed"
              and limits and not timed_out and not runtime.get("OOMKilled") and not runtime.get("Running"))
    return {"status": "passed" if passed else "failed", "exit_code": rc, "timed_out": timed_out,
            "required_resource_limits": limits, "oom_killed": runtime.get("OOMKilled"),
            "container_running": runtime.get("Running"), "container_exit_code": runtime.get("ExitCode"),
            "image_matches": image_matches,
            "final": final}


def sample_stats(docker, container, path, done):
    with path.open("x") as stream:
        while not done.wait(2):
            try:
                result = subprocess.run(docker + ["stats", "--no-stream", "--format", "{{json .}}", container],
                                        capture_output=True, text=True, timeout=8, check=False)
                value = {"utc": datetime.now(timezone.utc).isoformat(), "rc": result.returncode,
                         "stdout": result.stdout, "stderr": result.stderr}
            except subprocess.TimeoutExpired:
                value = {"error": "stats_client_timeout"}
            stream.write(json.dumps(value) + "\n")
            stream.flush()


def run_case(docker, out, image, case, run_id, probe, volume):
    folder = out / case
    folder.mkdir(mode=0o700)
    name = f"capacity-{run_id}-{out.name}-{case}"
    argv = docker + ["run", "-i", "--name", name, "--label", "ai-filter.synthetic-capacity=" + run_id,
                    "--platform=linux/amd64", "--network=none", "--no-healthcheck", "--read-only",
                    "--cap-drop=ALL", "--security-opt=no-new-privileges", "--memory=768m",
                    "--memory-swap=768m", "--pids-limit=256", "--cpus=1",
                    "--mount", f"type=volume,src={volume},dst=/state",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,size=16777216"]
    if case == "enospc":
        argv += ["--tmpfs", "/limited:rw,nosuid,nodev,size=16777216"]
    argv += ["--entrypoint=python", image["image_id"], "-I", "-B", "-", "--case", case,
             "--revision", image["revision"], "--source-hashes", json.dumps(image["source_sha256"])]
    save(folder / "ATTEMPT.json", {"argv": argv, "probe_sha256": digest_bytes(probe),
         "started_utc": datetime.now(timezone.utc).isoformat(), "timeout_seconds": 300,
         "volume_retained": volume, "container_retained": name})
    done = threading.Event()
    sampler = threading.Thread(target=sample_stats, args=(docker, name, folder / "stats.jsonl", done), daemon=True)
    sampler.start()
    timed_out = False
    with (folder / "stdout.jsonl").open("xb") as stdout, (folder / "stderr.log").open("xb") as stderr:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr)
        try:
            process.communicate(probe, timeout=300)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Only this newly owned synthetic container; never production.
            subprocess.run(docker + ["stop", "--time", "2", name], timeout=15, capture_output=True, check=False)
            process.kill()
            process.wait(timeout=10)
    done.set()
    sampler.join(timeout=10)
    state = json.loads(command(docker + ["inspect", name], folder / "inspect"))[0]
    rows = []
    for line in (folder / "stdout.jsonl").read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    final = next((row for row in reversed(rows) if row.get("event") == "final"), None)
    result = summarize_case(process.returncode, final, state, expected_image=image["image_id"], timed_out=timed_out)
    if case in {"partial", "restart", "enospc"} or case.startswith("alias"):
        try:
            command(docker + ["cp", name + ":/state/artifacts-" + case, str(folder / "evidence")],
                    folder / "copy-evidence")
            manifest = json.loads((folder / "evidence/MANIFEST.json").read_text())
            for row in manifest["files"]:
                if sha(folder / "evidence" / row["path"]) != row["sha256"]:
                    raise ValueError("copied evidence hash mismatch")
            result["artifact_copy_verified"] = True
        except Exception as error:
            result.update(status="failed", artifact_copy_verified=False, artifact_error=str(error))
    result.update(volume_retained=volume, container_retained=name, image_id=state["Image"],
                  finished_utc=datetime.now(timezone.utc).isoformat())
    save(folder / "RESULT.json", result)
    return result


def digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def run_image(docker, out, image, run_id, probe):
    volume = f"capacity-{run_id}-{out.name}-state"
    command(docker + ["volume", "create", "--label", "ai-filter.synthetic-capacity=" + run_id, volume], out / "volume")
    results = {}
    for case in CASES:
        results[case] = run_case(docker, out, image, case, run_id, probe, volume)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", default=BASELINE, choices=(BASELINE,))
    args = parser.parse_args(argv)
    out = args.output.resolve()
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    # Never inherit a user's remote context/DOCKER_HOST (in particular JD).
    docker = ["docker", "--host", "unix:///var/run/docker.sock"]
    result = {"status": "failed", "production_access": False, "model_calls": 0,
              "resource_limits": {"memory_bytes": MEMORY, "pids": PIDS, "cpus": 1},
              "baseline_expected_to_pass": False}
    try:
        result["host"] = verify_environment(docker, out)
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.repo, text=True).strip()
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("full SHA required")
        dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=args.repo, text=True)
        if dirty:
            raise ValueError("exact committed clean checkout required")
        probe = (args.repo / "scripts/webhook_capacity_probe.py").read_bytes()
        run_id = uuid.uuid4().hex[:12]
        candidate = build_image(docker, args.repo, out, "candidate", revision, run_id)
        baseline = build_image(docker, args.repo, out, "baseline", args.baseline, run_id)
        result["baseline"] = run_image(docker, out / "baseline", baseline, run_id, probe)
        result["candidate"] = run_image(docker, out / "candidate", candidate, run_id, probe)
        passed = all(row["status"] == "passed" for row in result["candidate"].values())
        result["status"] = "passed" if passed else "failed"
    except Exception as error:
        result.update(error_type=type(error).__name__, error=str(error))
    save(out / "RESULT.json", result)
    # All inputs are synthetic; keep measured outcomes visible even when the
    # artifact download is unavailable. Raw source bodies are never in result.
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
