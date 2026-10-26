"""Actual bounded synthetic PG + loopback held execution; no production adapter."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import time


def run(repo, source, output):
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    spec = importlib.util.spec_from_file_location("held_local", repo / "scripts/verify_miniflux_held_local.py")
    support = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(support)
    migration = source / "internal/database/webhook_outbox.go"
    if hashlib.sha256(migration.read_bytes()).hexdigest() != support.MIGRATION_SHA:
        raise ValueError("fixed migration mismatch")
    owner = "held-execute-" + secrets.token_hex(6)
    names = {kind: owner + "-" + kind for kind in ("pg", "net", "volume")}
    password, sequence = "synthetic-ci-only", 0

    def save(name, value):
        with (output / name).open("x", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")

    def command(argv, timeout=30, env=None, cwd=None):
        nonlocal sequence
        sequence += 1
        return support.recorded_command(argv, timeout=timeout, env=env, cwd=cwd, password=password,
                                        save=lambda value: save(f"COMMAND-{sequence:03d}.json", value))

    def docker(*args, **kwargs):
        return command([support.DOCKER, "-H", support.SOCKET, *args], **kwargs)

    files = list((repo / "ops/miniflux/heldtool").glob("*")) + list((repo / "ops/miniflux/held-access").glob("*.sql"))
    save("PLAN.json", {"owner": owner, "names": names, "production": False, "models": 0,
                       "architecture": "local arm64, not native amd64 CI",
                       "source_sha256": {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest()
                                         for p in files if p.is_file()}})
    active = False
    try:
        image = json.loads(docker("image", "inspect", support.PG))[0]
        if support.PG not in image["RepoDigests"] or image["Architecture"] != "arm64":
            raise ValueError("fixed local native image required")
        save("IMAGE.json", {k: image[k] for k in ("Id", "RepoDigests", "Architecture")})
        docker("network", "create", "--internal", "--label", "ai-filter.held-isolation=" + owner, names["net"])
        docker("volume", "create", "--label", "ai-filter.held-isolation=" + owner, names["volume"])
        docker("run", "-d", "--name", names["pg"], "--label", "ai-filter.held-isolation=" + owner,
               "--network", names["net"], "--memory", "384m", "--memory-swap", "384m", "--pids-limit", "64",
               "--cpus", "1", "--security-opt", "no-new-privileges", "--mount",
               "type=volume,src=" + names["volume"] + ",dst=/var/lib/postgresql/data",
               "-e", "POSTGRES_DB=synthetic_outbox", "-e", "POSTGRES_PASSWORD=" + password, support.PG)
        active = True
        for _ in range(20):
            time.sleep(0.5)
            try:
                docker("exec", names["pg"], "pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-d", "synthetic_outbox")
                break
            except RuntimeError:
                continue
        else:
            raise RuntimeError("fixture readiness failed")
        env = {**os.environ, "GOTOOLCHAIN": "local", "GOCACHE": "/private/tmp/golang-outbox126/cache",
               "GOPATH": "/private/tmp/golang-outbox126/path", "GOPROXY": "off", "GOSUMDB": "off",
               "GOOS": "linux", "GOARCH": "arm64", "CGO_ENABLED": "0"}
        binary = output / "held.test"
        command([support.GO, "test", "-c", "-o", str(binary), "."], timeout=90, env=env, cwd=repo / "ops/miniflux/heldtool")
        docker("cp", str(binary), names["pg"] + ":/tmp/held.test")
        docker("exec", names["pg"], "mkdir", "-p", "/tmp/fixed-source/internal/database", "/tmp/held-access")
        docker("cp", str(migration), names["pg"] + ":/tmp/fixed-source/internal/database/webhook_outbox.go")
        for name in ("install.sql", "execution.sql"):
            docker("cp", str(repo / "ops/miniflux/held-access" / name), names["pg"] + ":/tmp/held-access/" + name)
        docker("exec", "-e", "MINIFLUX_OUTBOX_TEST_DSN=postgres://postgres:" + password + "@127.0.0.1:5432/synthetic_outbox?sslmode=disable",
               "-e", "MINIFLUX_PATCHED_SOURCE=/tmp/fixed-source", "-e", "HELD_ACCESS_SQL_DIR=/tmp/held-access",
               names["pg"], "/tmp/held.test", "-test.v", "-test.count=1", "-test.run", "Access", timeout=90)
        value = json.loads(docker("inspect", names["pg"]))[0]
        limits = {k: value["HostConfig"][k] for k in ("Memory", "MemorySwap", "PidsLimit", "NanoCpus")}
        save("CONTAINER.json", {"Id": value["Id"], "Image": value["Image"], "State": value["State"], "limits": limits})
        if limits != {"Memory": 402653184, "MemorySwap": 402653184, "PidsLimit": 64, "NanoCpus": 1000000000} or value["State"]["OOMKilled"]:
            raise ValueError("resource identity changed")
        save("CGROUP.json", {key: docker("exec", names["pg"], "cat", "/sys/fs/cgroup/" + key)
                             for key in ("memory.events", "memory.peak")})
    except Exception as error:
        save("FAILURE.json", {"type": type(error).__name__, "reason": str(error)})
        raise
    finally:
        if active:
            try:
                save("STOP_CONFIRMED.json", support.stop_owned(docker, names["pg"], owner))
            except Exception as error:
                save("STOP_FAILURE.json", {"type": type(error).__name__, "stop_confirmed": False})
                raise
    result = {"status": "passed_actual_pg_loopback", "volume_retained": names["volume"], "production": False,
              "source_actual_fixed_schema": True, "production_adapter": False, "models": 0}
    save("RESULT.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.repo, args.source, args.output) if args.run else {"run": False, "production": False}))
