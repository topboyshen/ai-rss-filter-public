"""Native AMD64 fixture for real inspect/env/mount + receiver stop/drain HTTP.

Two pinned Python containers run only synthetic lease work. This does not run
the production image, a sender, migrations, models or a deployment. All source,
inspect, admission originals and failed-operation evidence remain retained.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from services import admission_fence as fence  # noqa: E402
from services.receiver_handoff_control import ControlServer, ReceiverControl  # noqa: E402
import production_receiver_driver as driver  # noqa: E402
import verify_handoff_operations_native as native  # noqa: E402

CHILD = """
import os,pathlib,time
from services.admission_fence import _configured, AdmissionClosed
f=_configured()
role=os.environ['AI_FILTER_ROLE']
signals=pathlib.Path('/signals')
lease=f.reserve_lease('synthetic_'+role,charge_bytes=1)
(signals/(role+'.ready')).write_text('synthetic lease active')
deadline=time.monotonic()+90
while not (signals/'finish').exists():
    assert time.monotonic()<deadline, 'fixture deadline'
    time.sleep(0.02)
try:
    f.reserve_lease('refused_new_'+role)
except AdmissionClosed:
    pass
else:
    raise AssertionError('new producer admitted after stop')
lease.finish(True)
drain=f.reserve_lease('durable_'+role,kind='drain')
drain.finish(True)
(signals/(role+'.finished')).write_text('new intake refused; durable drain finished')
while not (signals/'exit').exists():
    assert time.monotonic()<deadline, 'fixture deadline'
    time.sleep(0.02)
"""


def wait_for(predicate, seconds=20):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("synthetic fixture deadline")
        time.sleep(0.02)


def run(repo, output):
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("native Linux AMD64 required before Docker access")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    if repo != ROOT or native.subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], timeout=5).strip():
        raise ValueError("exact clean source checkout required")
    revision = native.subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, timeout=5).strip()
    info = json.loads(native.checked(native.DOCKER + ["info", "--format", "{{json .}}"], output / "docker-info"))
    native.native_host(info)
    image = json.loads(native.checked(native.DOCKER + ["image", "inspect", native.IMAGE], output / "image"))[0]
    if image.get("Architecture") != "amd64" or image.get("Os") != "linux":
        raise ValueError("fixture image is not native AMD64")
    now = int(time.time())
    binding = {"revision": revision, "image_id": image["Id"], "strategy": "synthetic-no-models",
               "source_sha256": fence.source_sha256(), "deployment_nonce": uuid.uuid4().hex}
    fence.provision(output / "fence", binding, {"not_before": now - 1, "expires_at": now + 120,
                    "max_admissions": 4, "max_bytes": 16})
    gate = fence.Fence(output / "fence", binding)
    gate.open_trial("1" * 64, 0, "synthetic native fixture only")
    signals = output / "signals"
    signals.mkdir(mode=0o700)
    spool = output / "spool"
    spool.mkdir(mode=0o700)
    fd = os.open(spool / ".lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    store = driver.receipt_format.ReceiptStore(spool)
    store.accept(b"synthetic exact original", {"entries": []}, strategy="synthetic", revision=revision)
    control = ReceiverControl(gate)
    token_file = output / "private-token.json"
    driver.durable_write(token_file, {"token": uuid.uuid4().hex + uuid.uuid4().hex})
    token = driver.read_private(token_file)["token"]
    server = ControlServer(control, token)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    names, finished = [], False
    try:
        for role in ("web", "worker"):
            name = "receiver-synthetic-" + role + "-" + uuid.uuid4().hex[:12]
            args = native.DOCKER + ["create", "--name", name, "--label", "ai-filter.synthetic-operations=" + name,
                "--label", "org.opencontainers.image.revision=" + revision, "--user", f"{os.getuid()}:{os.getgid()}",
                "--network=none", "--read-only", "--no-healthcheck", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--memory=768m", "--memory-swap=768m", "--pids-limit=256", "--cpus=1",
                "--mount", f"type=bind,src={repo},dst=/work,readonly",
                "--mount", f"type=bind,src={gate.root},dst=/fixture/fence",
                "--mount", f"type=bind,src={spool},dst=/fixture/spool",
                "--mount", f"type=bind,src={signals},dst=/signals", "--workdir=/work",
                "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "AI_FILTER_ROLE=" + role,
                "--env", "AI_FILTER_ADMISSION_MODE=enforced", "--env", "AI_FILTER_ADMISSION_ROOT=/fixture/fence",
                "--env", "WEBHOOK_SPOOL_DIR=/fixture/spool",
                "--env", "AI_FILTER_ADMISSION_BINDING=" + fence._encoded(binding).decode(),
                "--entrypoint=python", image["Id"], "-B", "-u", "-c", CHILD]
            native.checked(args, output / (role + "-create"))
            names.append(name)
            native.checked(native.DOCKER + ["start", name], output / (role + "-start"))
        wait_for(lambda: all((signals / (role + ".ready")).exists() for role in ("web", "worker")))
        rows = json.loads(native.checked(native.DOCKER + ["inspect", *names], output / "actual-inspect"))
        components = {}
        for row in rows:
            env = dict(value.split("=", 1) for value in row["Config"]["Env"])
            components[row["Name"].lstrip("/")] = {"id": row["Id"], "image": row["Image"],
                "revision": row["Config"]["Labels"]["org.opencontainers.image.revision"],
                "started_at": row["State"]["StartedAt"], "restart_count": row["RestartCount"],
                "role": env["AI_FILTER_ROLE"], "user": row["Config"]["User"], "memory": row["HostConfig"]["Memory"],
                "pids": row["HostConfig"]["PidsLimit"], "mounts": [{k: mount[k] for k in ("Type", "Source", "Destination", "RW")}
                    for mount in row["Mounts"]]}
        plan = {"schema": 1, "scope": "instrumented_receiver_only", "sources": driver.sources(),
                "components": components, "registration": control.registration(), "host_fence_root": str(gate.root),
                "container_fence_root": "/fixture/fence", "container_spool_root": "/fixture/spool",
                "spool": str(spool), "spool_identity": driver.spool_identity(spool),
                "not_before": now - 1, "expires_at": now + 110, "owner": "native synthetic fixture",
                "capacity": {"stop_records": 1, "stop_bytes": 200 * 1024**2,
                             "minimum_free_bytes": driver.receipt_format.MAX_ALIAS_RESERVATION}}
        driver.durable_write(output / "PLAN.json", plan)
        transport = driver.NativeTransport(f"http://127.0.0.1:{server.server_port}", token_file)
        controller = driver.ReceiverDriver(plan, transport, output / "operations")
        stopped = controller.capacity_tick("2" * 64)
        assert stopped["receipt"]["phase"] == "draining" and not stopped["receipt"]["activity_drained"]
        (signals / "finish").write_text("synthetic natural completion")
        wait_for(lambda: all((signals / (role + ".finished")).exists() for role in ("web", "worker")))
        frozen = controller.operate("freeze", "3" * 64, "synthetic consumers finished naturally")
        assert frozen["phase"] == "frozen" and frozen["activity_drained"] and not frozen["switch_authorized"]
        (signals / "exit").write_text("synthetic exit")
        for name in names:
            native.checked(native.DOCKER + ["wait", name], output / (name + "-wait"), 15)
            row = json.loads(native.checked(native.DOCKER + ["inspect", name], output / (name + "-final-inspect")))[0]
            assert row["State"]["ExitCode"] == 0 and row["State"]["OOMKilled"] is False
            assert row["HostConfig"]["Memory"] == native.MEMORY and row["HostConfig"]["PidsLimit"] == native.PIDS
            assert row["HostConfig"]["NetworkMode"] == "none"
        native.save(output / "RESULT.json", {"status": "passed", "native_amd64": True,
            "actual_container_identity_environment_mounts": True, "capacity_stop_new_then_natural_drain_then_freeze": True,
            "container_names": names, "production_image_tested": False, "production_access": False,
            "first_takeover_proven": False, "sender_controlled": False, "models": 0, "switch_authorized": False})
        finished = True
    except Exception as error:
        native.save(output / "FAILURE.json", {"type": type(error).__name__, "message": str(error), "containers": names})
        raise
    finally:
        if not finished:
            for name in names:
                native.preserve_failed_container(name, output / name)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.repo.resolve(), args.output.resolve())
