"""Opt-in local synthetic PostgreSQL validation. Never contacts production.

Uses a fixed Colima socket and fixed PostgreSQL digest, an internal network,
private named volume with no published ports. The compiled test binary executes
inside this synthetic database container and connects only to loopback. Keeps all evidence and
the database volume; stops only this run's container. No existing image or tag
is changed. Go's module network is disabled during the actual tests.
"""
from __future__ import annotations

import argparse
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


def run(source: Path, output: Path) -> dict:
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    password = secrets.token_hex(24)
    owner = "outbox-local-" + secrets.token_hex(6)
    names = {key: owner + "-" + key for key in ("pg", "network", "volume")}
    sequence = 0
    started = time.monotonic()

    def save(name, value):
        with (output / name).open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")

    def command(argv, timeout=30, env=None):
        nonlocal sequence
        sequence += 1
        begin = time.monotonic()
        result = subprocess.run(argv, text=True, capture_output=True, timeout=timeout,
                                env=env, check=False, cwd=source)
        safe = {"argv": [x.replace(password, "<synthetic-secret>") for x in argv],
                "exit_code": result.returncode,
                "stdout": result.stdout.replace(password, "<synthetic-secret>"),
                "stderr": result.stderr.replace(password, "<synthetic-secret>"),
                "elapsed_seconds": time.monotonic() - begin}
        save(f"command-{sequence:03d}.json", safe)
        if result.returncode:
            raise RuntimeError(f"isolated command {sequence} failed")
        return result.stdout

    def docker(*args, **kwargs):
        return command([DOCKER, "-H", SOCKET, *args], **kwargs)

    save("PLAN.json", {"owner": owner, "names": names, "source": str(source),
                       "image": PG, "production_access": False, "model_calls": 0})
    active = False
    try:
        image = json.loads(docker("image", "inspect", PG))[0]
        if PG not in image.get("RepoDigests", []) or image.get("Architecture") != "arm64":
            raise ValueError("fixed PostgreSQL image identity mismatch")
        save("IMAGE.json", {key: image.get(key) for key in ("Id", "RepoDigests", "Architecture")})
        docker("network", "create", "--internal", "--label", "ai-filter.synthetic-outbox=" + owner, names["network"])
        docker("volume", "create", "--label", "ai-filter.synthetic-outbox=" + owner, names["volume"])
        docker("run", "-d", "--name", names["pg"], "--label", "ai-filter.synthetic-outbox=" + owner,
               "--network", names["network"], "--memory", "384m", "--memory-swap", "384m",
               "--cpus", "1", "--pids-limit", "64", "--security-opt", "no-new-privileges",
               "--mount", "type=volume,src=" + names["volume"] + ",dst=/var/lib/postgresql/data",
               "-e", "POSTGRES_DB=synthetic_outbox",
               "-e", "POSTGRES_PASSWORD=" + password, PG)
        active = True
        ready = False
        for _ in range(20):
            time.sleep(0.5)
            try:
                docker("exec", names["pg"], "pg_isready", "-U", "postgres", "-d", "synthetic_outbox", timeout=3)
                ready = True
                break
            except RuntimeError:
                pass
        if not ready:
            raise RuntimeError("isolated PostgreSQL readiness timeout")
        env = {**os.environ, "GOTOOLCHAIN": "local", "GOCACHE": "/private/tmp/golang-outbox126/cache",
               "GOPATH": "/private/tmp/golang-outbox126/path", "GOPROXY": "off", "GOSUMDB": "off",
               "GOOS": "linux", "GOARCH": "arm64", "CGO_ENABLED": "0"}
        binary = output / "storage.test"
        command([GO, "test", "-c", "-o", str(binary), "./internal/storage"], timeout=90, env=env)
        docker("cp", str(binary), names["pg"] + ":/tmp/storage.test")
        docker("exec", "-e", "MINIFLUX_OUTBOX_TEST_DSN=postgres://postgres:" + password
               + "@127.0.0.1:5432/synthetic_outbox?sslmode=disable", names["pg"],
               "/tmp/storage.test", "-test.v", "-test.count=1", "-test.run=TestPostgresOutbox", timeout=90)
        common = ["exec", "-e", "MINIFLUX_OUTBOX_TEST_DSN=postgres://postgres:" + password
                  + "@127.0.0.1:5432/synthetic_outbox?sslmode=disable"]
        docker(*common, "-e", "MINIFLUX_OUTBOX_RESTART_PHASE=prepare", names["pg"],
               "/tmp/storage.test", "-test.v", "-test.run=TestPostgresOutboxRestartPrepare", timeout=30)
        identity = json.loads(docker("inspect", names["pg"]))[0]
        if identity.get("Config", {}).get("Labels", {}).get("ai-filter.synthetic-outbox") != owner:
            raise ValueError("owned synthetic container identity changed")
        docker("kill", "--signal", "KILL", names["pg"])
        docker("start", names["pg"])
        ready = False
        for _ in range(20):
            time.sleep(0.5)
            try:
                docker("exec", names["pg"], "pg_isready", "-U", "postgres", "-d", "synthetic_outbox", timeout=3)
                ready = True
                break
            except RuntimeError:
                pass
        if not ready:
            raise RuntimeError("isolated restart readiness timeout")
        docker(*common, "-e", "MINIFLUX_OUTBOX_RESTART_PHASE=verify", names["pg"],
               "/tmp/storage.test", "-test.v", "-test.run=TestPostgresOutboxRestartVerify", timeout=30)
        save("RESULT.json", {"status": "passed", "elapsed_seconds": time.monotonic() - started,
                             "retained_volume": names["volume"], "retained_network": names["network"],
                             "actual_postgres": True, "postgres_unclean_restart": True,
                             "sender_process_recreated": True, "lease_expiry_simulated": True,
                             "power_loss_proven": False,
                             "production_access": False, "model_calls": 0})
        return {"status": "passed", "output": str(output)}
    except Exception as error:
        save("FAILURE.json", {"type": type(error).__name__, "message": str(error).replace(password, "<synthetic-secret>")})
        raise
    finally:
        if active:
            # Exact owned name only; no cleanup of the evidence volume or network.
            docker("stop", "--time", "10", names["pg"], timeout=15)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if not args.run:
        print(json.dumps({"execution": False, "image": PG, "socket": SOCKET}))
    else:
        print(json.dumps(run(args.source.resolve(), args.output.resolve())))
