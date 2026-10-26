"""Create one private local PG fixture; retain its volume and all evidence."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

from verify import FIXED_PG

DOCKER = "/opt/homebrew/bin/docker"
SOCKET = "unix://" + str(Path.home() / ".colima/default/docker.sock")


def run(source, output, *, legacy=False):
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    owner = "held-access-" + secrets.token_hex(6)
    names = {kind: owner + "-" + kind for kind in ("pg", "net", "volume")}
    sequence = 0

    def save(name, value):
        fd = os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")

    def command(args, timeout=20):
        nonlocal sequence
        sequence += 1
        started = time.monotonic()
        try:
            result = subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as error:
            def text(value):
                return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")
            save(f"LIFECYCLE-{sequence:03d}.json", {"argv": args, "exit_code": None, "timed_out": True, "stdout": text(error.stdout), "stderr": text(error.stderr), "elapsed_seconds": time.monotonic() - started})
            raise
        save(f"LIFECYCLE-{sequence:03d}.json", {"argv": args, "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr, "elapsed_seconds": time.monotonic() - started})
        if result.returncode:
            raise RuntimeError("owned synthetic command failed")
        return result.stdout

    def docker(*args, **kwargs):
        return command([DOCKER, "-H", SOCKET, *args], **kwargs)

    save("PLAN.json", {"owner": owner, "names": names, "production": False, "http": 0, "models": 0,
                       "network": "internal only; no ports; disposable synthetic trust auth", "retention": "no resource deletion"})
    active = False
    try:
        docker("network", "create", "--internal", "--label", "ai-filter.held-access=" + owner, names["net"])
        docker("volume", "create", "--label", "ai-filter.held-access=" + owner, names["volume"])
        docker("run", "-d", "--name", names["pg"], "--network", names["net"], "--label", "ai-filter.held-access=" + owner,
               "--memory", "384m", "--memory-swap", "384m", "--pids-limit", "64", "--cpus", "1",
               "--security-opt", "no-new-privileges", "--mount", "type=volume,src=" + names["volume"] + ",dst=/var/lib/postgresql/data",
               "-e", "POSTGRES_DB=synthetic_outbox", "-e", "POSTGRES_HOST_AUTH_METHOD=trust", FIXED_PG)
        active = True
        for _ in range(20):
            time.sleep(0.5)
            try:
                # The official entrypoint's temporary initialization server is
                # socket-only. TCP readiness avoids racing its planned shutdown.
                docker("exec", names["pg"], "pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-d", "synthetic_outbox")
                break
            except RuntimeError:
                continue
        else:
            raise RuntimeError("synthetic PG did not become ready")
        verifier = "verify_legacy.py" if legacy else "verify.py"
        command([sys.executable, str(Path(__file__).with_name(verifier)), "--container", names["pg"], "--source", str(source), "--output", str(output / "permissions"), "--docker", DOCKER, "--docker-host", SOCKET, "--run"], timeout=180)
    except Exception as error:
        save("FAILURE.json", {"type": type(error).__name__, "reason": str(error)})
        raise
    finally:
        if active:
            try:
                owner_read = docker("inspect", "--format", '{{index .Config.Labels "ai-filter.held-access"}}', names["pg"]).strip()
                if owner_read != owner:
                    raise RuntimeError("owned container identity changed")
                docker("stop", "--time", "10", names["pg"])
                state = docker("inspect", "--format", "{{json .State}}", names["pg"])
                value = json.loads(state)
                if value["Running"] or value["ExitCode"] != 0:
                    raise RuntimeError("synthetic stop not confirmed")
                save("STOP_CONFIRMED.json", value)
            except Exception as error:
                save("STOP_FAILURE.json", {"type": type(error).__name__, "reason": str(error)})
                raise
    result = {"status": "passed_actual_pg", "owner": owner, "volume_retained": names["volume"], "stop_confirmed": True, "production": False}
    save("RESULT.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--legacy", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.source, args.output, legacy=args.legacy) if args.run else {"run": False, "production": False}))
