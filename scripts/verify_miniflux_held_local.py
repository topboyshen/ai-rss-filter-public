"""Run only new synthetic PG held tests; preserve its volume and safe evidence.

No producer, production adapter, external HTTP or models. Local Go tests execute
inside the private PostgreSQL container, so its 384 MiB / 64 PID limit includes
the test process. CI likewise executes its amd64 test binary inside the bounded
service; only compilation and vet run on the host. Local arm64 is not amd64.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import time

DOCKER = "/opt/homebrew/bin/docker"
SOCKET = "unix://" + str(Path.home() / ".colima/default/docker.sock")
PG = "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
GO = "/private/tmp/golang-outbox126/go/bin/go"
MIGRATION_SHA = "42ee96b4a961887d5387f8a0261ec6f8e9132f78eb362760451c172341640dad"


def recorded_command(argv, *, timeout, env, cwd, password, save):
    """Always preserve partial output for a command we actually attempted."""
    started = time.monotonic()

    def safe(value):
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        return (value or "").replace(password, "<synthetic-secret>")

    record = {"argv": [safe(arg) for arg in argv], "timeout_seconds": timeout}
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                check=False, cwd=cwd, env=env)
    except subprocess.TimeoutExpired as error:
        record.update(exit_code=None, timed_out=True, stdout=safe(error.stdout),
                      stderr=safe(error.stderr), elapsed_seconds=time.monotonic() - started)
        save(record)
        raise RuntimeError("isolated command timed out; partial output retained") from error
    record.update(exit_code=result.returncode, timed_out=False, stdout=safe(result.stdout),
                  stderr=safe(result.stderr), elapsed_seconds=time.monotonic() - started)
    save(record)
    if result.returncode:
        raise RuntimeError("isolated command failed; output retained")
    return result.stdout


def stop_owned(docker, name, owner):
    """Only the exact owned synthetic container; never assume stop succeeded."""
    identity = json.loads(docker("inspect", name))[0]
    if identity.get("Config", {}).get("Labels", {}).get("ai-filter.held-isolation") != owner:
        raise RuntimeError("owned container changed; not stopping it")
    docker("stop", "--time", "10", name, timeout=15)
    stopped = json.loads(docker("inspect", name))[0]
    if stopped.get("Id") != identity.get("Id") or stopped.get("State", {}).get("Running") is not False:
        raise RuntimeError("owned container stop not confirmed")
    if stopped.get("State", {}).get("ExitCode") != 0:
        raise RuntimeError("owned synthetic database stopped with nonzero exit")
    return {"container_id": stopped["Id"], "state": stopped["State"], "stop_confirmed": True}


def run(source: Path, repo: Path, output: Path) -> dict:
    migration = source / "internal/database/webhook_outbox.go"
    if hashlib.sha256(migration.read_bytes()).hexdigest() != MIGRATION_SHA:
        raise ValueError("fixed actual migration mismatch")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    owner = "held-isolation-" + secrets.token_hex(6)
    names = {kind: owner + "-" + kind for kind in ("pg", "net", "volume")}
    password = secrets.token_hex(24)
    sequence = 0
    started = time.monotonic()

    def save(name, value):
        with (output / name).open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")

    def command(argv, timeout=30, env=None, cwd=None):
        nonlocal sequence
        sequence += 1
        return recorded_command(argv, timeout=timeout, env=env, cwd=cwd, password=password,
                                save=lambda record: save(f"COMMAND-{sequence:03d}.json", record))

    def docker(*args, **kwargs):
        return command([DOCKER, "-H", SOCKET, *args], **kwargs)

    save("PLAN.json", {"owner": owner, "names": names, "migration_sha256": MIGRATION_SHA,
                       "production_access": False, "model_calls": 0,
                       "resources": {"memory_bytes": 384 * 1024 * 1024, "pids": 64},
                       "source_sha256": {str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest()
                                         for path in sorted((repo / "ops/miniflux/heldtool").glob("*"))
                                         if path.is_file()}})
    active = False
    try:
        image = json.loads(docker("image", "inspect", PG))[0]
        if PG not in image.get("RepoDigests", []) or image.get("Architecture") != "arm64":
            raise ValueError("fixed native arm64 PG identity mismatch")
        save("IMAGE.json", {key: image.get(key) for key in ("Id", "RepoDigests", "Architecture")})
        docker("network", "create", "--internal", "--label", "ai-filter.held-isolation=" + owner, names["net"])
        docker("volume", "create", "--label", "ai-filter.held-isolation=" + owner, names["volume"])
        docker("run", "-d", "--name", names["pg"], "--label", "ai-filter.held-isolation=" + owner,
               "--network", names["net"], "--memory", "384m", "--memory-swap", "384m",
               "--cpus", "1", "--pids-limit", "64", "--security-opt", "no-new-privileges",
               "--mount", "type=volume,src=" + names["volume"] + ",dst=/var/lib/postgresql/data",
               "-e", "POSTGRES_DB=synthetic_outbox", "-e", "POSTGRES_PASSWORD=" + password, PG)
        active = True
        for _ in range(20):
            time.sleep(0.5)
            try:
                docker("exec", names["pg"], "pg_isready", "-U", "postgres", "-d", "synthetic_outbox", timeout=3)
                break
            except RuntimeError:
                continue
        else:
            raise RuntimeError("synthetic PG readiness timeout")
        env = {**os.environ, "GOTOOLCHAIN": "local", "GOCACHE": "/private/tmp/golang-outbox126/cache",
               "GOPATH": "/private/tmp/golang-outbox126/path", "GOPROXY": "off", "GOSUMDB": "off",
               "GOOS": "linux", "GOARCH": "arm64", "CGO_ENABLED": "0"}
        binary = output / "held.test"
        command([GO, "test", "-c", "-o", str(binary), "."], timeout=90, env=env,
                cwd=repo / "ops/miniflux/heldtool")
        docker("cp", str(binary), names["pg"] + ":/tmp/held.test")
        docker("exec", names["pg"], "mkdir", "-p", "/tmp/fixed-source/internal/database")
        docker("cp", str(migration), names["pg"] + ":/tmp/fixed-source/internal/database/webhook_outbox.go")
        docker("exec", "-e", "MINIFLUX_OUTBOX_TEST_DSN=postgres://postgres:" + password
               + "@127.0.0.1:5432/synthetic_outbox?sslmode=disable",
               "-e", "MINIFLUX_PATCHED_SOURCE=/tmp/fixed-source", names["pg"],
               "/tmp/held.test", "-test.v", "-test.count=1", timeout=90)
        actual = json.loads(docker("inspect", names["pg"]))[0]
        save("CONTAINER.json", {"Id": actual["Id"], "Image": actual["Image"],
                                "State": actual["State"], "HostConfig": {
                                    key: actual["HostConfig"][key]
                                    for key in ("Memory", "MemorySwap", "PidsLimit", "NanoCpus")}})
        expected = {"Memory": 384 * 1024 * 1024, "MemorySwap": 384 * 1024 * 1024,
                    "PidsLimit": 64, "NanoCpus": 1000000000}
        if actual["State"].get("OOMKilled") is not False or any(
                actual["HostConfig"].get(key) != value for key, value in expected.items()):
            raise ValueError("actual isolation resource identity or OOM check failed")
    except Exception as error:
        save("FAILURE.json", {"type": type(error).__name__, "message": str(error).replace(password, "<synthetic-secret>")})
        raise
    finally:
        if active:
            try:
                save("STOP_CONFIRMED.json", stop_owned(docker, names["pg"], owner))
            except Exception as error:
                failure = {"type": type(error).__name__, "message": str(error).replace(password, "<synthetic-secret>"),
                           "stop_confirmed": False, "phase": "owned_stop"}
                save("STOP_FAILURE.json", failure)
                if not (output / "FAILURE.json").exists():
                    save("FAILURE.json", failure)
                raise
    save("RESULT.json", {"status": "passed", "elapsed_seconds": time.monotonic() - started,
                         "retained_volume": names["volume"], "production_access": False,
                         "models": 0, "real_postgres": True,
                         "actual_locked_sender_schema": True,
                         "process_reopen_not_pg_restart": True,
                         "owned_stop_confirmed": True,
                         "real_disk_enospc": False, "production_adapter": False})
    return {"status": "passed", "output": str(output)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if args.run:
        print(json.dumps(run(args.source.resolve(), args.repo.resolve(), args.output.resolve())))
    else:
        print(json.dumps({"execution": False, "image": PG, "socket": SOCKET}))
