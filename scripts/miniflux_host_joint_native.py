"""Actual isolated sender/host-cron stop proof, never production coverage.

Called by the fixed-image AMD64 harness. It covers only the three sender
groups and the explicitly registered synthetic old cron job, not all ingress.
"""
from __future__ import annotations

import hashlib
import base64
import json
import os
from pathlib import Path
import select
import shlex
import subprocess
import sys
import time

from durable_handoff_cutover import CutoverError, durable_write, require
import handoff_host_cron as cron
from handoff_runtime_adapter import legacy_cron_binding
import miniflux_handoff_adapter as bridge

WRAPPER_OUTPUT_LIMIT = 16384


def _stream_evidence(value):
    raw = bytes(value["prefix"])
    return {"prefix_base64": base64.b64encode(raw).decode(),
            "prefix_text_utf8_replace": raw.decode("utf-8", errors="replace"),
            "prefix_sha256": hashlib.sha256(raw).hexdigest(), "captured_bytes": len(raw),
            "observed_bytes": value["observed"], "truncated": value["observed"] > len(raw),
            "eof_observed": value["eof"], "limit_bytes": WRAPPER_OUTPUT_LIMIT}


def capture_wrapper(process, root, identity, seconds=5):
    """Bound both pipe prefixes; a deadline never implies the child stopped."""
    started = time.monotonic()
    deadline = started + seconds
    streams = {process.stdout: "stdout", process.stderr: "stderr"}
    values = {name: {"prefix": bytearray(), "observed": 0, "eof": False} for name in streams.values()}
    failure = None
    try:
        while streams or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, seconds)
            if not streams:
                process.wait(timeout=remaining)
                continue
            ready, _, _ = select.select(list(streams), [], [], remaining)
            for stream in ready:
                value = values[streams[stream]]
                chunk = os.read(stream.fileno(), 4096)
                if not chunk:
                    value["eof"] = True
                    del streams[stream]
                    continue
                value["observed"] += len(chunk)
                value["prefix"].extend(chunk[:max(0, WRAPPER_OUTPUT_LIMIT - len(value["prefix"]))])
    except Exception as error:
        failure = error
    timed_out = isinstance(failure, subprocess.TimeoutExpired)
    evidence = {"schema": 1, "pid": process.pid, "argv": list(process.args), "identity": identity,
                "returncode": process.poll(), "elapsed_seconds": time.monotonic() - started,
                "timeout_seconds": seconds, "timed_out": timed_out, "killed": False, "retried": False,
                "outcome": "unknown_may_still_be_running" if failure else "exited",
                "capture_error_type": type(failure).__name__ if failure else None,
                "capture_error": str(failure) if failure else None,
                **{name: _stream_evidence(value) for name, value in values.items()}}
    path = root / "wrapper-process.json"
    require(not path.exists() and not path.with_suffix(".json.pending").exists(), "wrapper evidence already exists")
    durable_write(path, evidence)
    if failure:
        durable_write(root / "wrapper-unconfirmed.json", evidence)
        raise failure
    process.stdout.close()
    process.stderr.close()
    return bytes(values["stdout"]["prefix"]), evidence


def component(row):
    """Bind fresh actual inspect, including the harness's explicit source mount."""
    return {"id": row["Id"], "image": row["Image"],
            "revision": row["Config"]["Labels"]["org.opencontainers.image.revision"],
            "started_at": row["State"]["StartedAt"], "restart_count": row["RestartCount"],
            "role": row["Config"]["Labels"]["io.ai-filter.handoff.role"], "user": row["Config"]["User"] or "0",
            "memory": row["HostConfig"]["Memory"], "pids": row["HostConfig"]["PidsLimit"],
            "mounts": [{key: value[key] for key in ("Type", "Source", "Destination", "RW")}
                       for value in row["Mounts"]]}


def handshake(child, deadline):
    value = b""
    while value != b"ready\n":
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([child.stdout], [], [], remaining)[0]:
            raise TimeoutError("owned cron child handshake unconfirmed")
        part = os.read(child.stdout.fileno(), 1)
        require(bool(part) and len(value) < 6, "owned cron child handshake invalid")
        value += part


def release_child(child, root, deadline):
    try:
        child.communicate(input=b"release", timeout=max(0.001, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        durable_write(root / "child-unconfirmed.json", {"schema": 1, "pid": child.pid,
             "outcome": "unknown_may_still_be_running", "killed": False, "retried": False})
        raise


def old_cron_proof(root, run_id):
    root.mkdir(mode=0o700)
    installed = root / "installed"
    installed.mkdir(mode=0o700)
    for name in ("handoff_host_cron.py", "durable_handoff_cutover.py"):
        target = installed / name
        with target.open("xb") as stream:
            stream.write(Path(__file__).with_name(name).read_bytes())
        target.chmod(0o700)
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "--cron-child"]
    gate_root = root / "gate"
    cron.provision(gate_root, {"schema": 1, "scope": "isolation", "run_id": run_id,
         "control_id": "synthetic_cron", "source_sha256": cron.source_sha256(),
         "entrances": ["natural_cron"], "commands": {"legacy": command}})
    wrapper = installed / "handoff_host_cron.py"
    wrapper_argv = [sys.executable, "-B", str(wrapper), "--root", str(gate_root),
                    "--command", "legacy", "--isolation"]
    old = "*/30 8-16 * * * " + shlex.join(command) + "\n"
    new = "*/30 8-16 * * * " + shlex.join(wrapper_argv) + "\n"
    identity = {str(wrapper): {"sha256": hashlib.sha256(wrapper.read_bytes()).hexdigest(),
                              "uid": os.getuid(), "mode": 0o700}}
    binding = legacy_cron_binding(old, new, [{"old_line": old.rstrip("\n"),
          "new_line": new.rstrip("\n"), "control_id": "synthetic_cron"}], identity)
    durable_write(root / "legacy-binding.json", binding)
    gate = cron.HostCronGate(gate_root)
    wrapper_process = subprocess.Popen(wrapper_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    wrapper_output, captured = capture_wrapper(wrapper_process, root, {
        "run_id": run_id, "uid": os.getuid(), "installed_files_sha256": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(installed.iterdir())}})
    require(wrapper_process.returncode == 0, "actual installed isolation wrapper failed")
    require(not captured["stdout"]["truncated"] and not captured["stderr"]["truncated"],
            "installed wrapper output exceeded evidence bound")
    wrapper_receipt = json.loads(wrapper_output)
    require(wrapper_receipt == {"returncode": 0, "scope": "isolation", "switch_authorized": False},
            "installed wrapper result differs")
    operation = "8" * 64
    with gate.activity("legacy") as active:
        deadline = time.monotonic() + 5
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, pass_fds=active["pass_fds"])
        try:
            handshake(child, deadline)
            try:
                gate.stop(operation, 0, "synthetic joint stop", wait_seconds=0)
            except TimeoutError:
                pass
            else:
                raise ValueError("inflight cron falsely confirmed stopped")
            try:
                gate.run_registered("legacy")
            except CutoverError:
                pass
            else:
                raise ValueError("new cron child admitted after close")
        finally:
            # Release this owned synthetic child; never kill, clean or retry it.
            release_child(child, root, deadline)
            active["returncode"] = child.returncode
    receipt = gate.query(operation)
    require(receipt["state"] == "closed" and receipt["inflight"] == receipt["unknown"] == 0,
            "same host operation not durably closed")
    return {"binding": binding, "receipt": receipt, "installed_wrapper": wrapper_receipt,
            "actual_child_returncode": child.returncode,
            "production_crontab_read_or_installed": False}


def run(harness, prior):
    root = harness.output / "host-joint"
    host = old_cron_proof(root, harness.owner)
    resumed = harness.resume(prior, "5" * 64)
    harness.sql("UPDATE integrations SET readeck_enabled=true WHERE user_id=1;")
    before = len([r for r in harness.events() if r["kind"] == "legacy_received"])
    harness.control(harness.items, hold=True)
    harness.http("sender", f"/v1/entries/{harness.entry_id}/save", "POST", expected=202)
    received = harness.wait(lambda: (rows[-1] if len(rows := [r for r in harness.events()
                             if r["kind"] == "legacy_received" and r["held"]]) > before else None), seconds=4)
    def inspect():
        return json.loads(harness.command(["inspect", harness.names["sender"]]))
    row = inspect()[0]
    binding = {"schema": 1, "scope": "isolation", "run_id": harness.owner, "control_id": "miniflux",
               "component_name": row["Name"].lstrip("/"), "component": component(row),
               "manifest": harness.manifest, "application_source_sha256":
               hashlib.sha256((harness.repo / "ops/miniflux/pause-source-lock.json").read_bytes()).hexdigest(),
               "source_sha256": bridge.source_sha256(), "generation": resumed["generation"],
               "initial_operation_id": "5" * 64, "entrances": list(bridge.ENTRANCES)}

    class Client:
        def request(self, method, path, payload):
            return harness.private_exchange(path, method, payload)

    control = bridge.MinifluxPauseControl(binding, Client(), inspect, root / "miniflux")
    operation = "6" * 64
    try:
        control.stop(operation, resumed["generation"], "synthetic joint stop")
    except bridge.PausePending:
        pass
    else:
        raise ValueError("actual sender inflight falsely confirmed stopped")
    pending = json.loads((root / "miniflux/pause-response.json").read_text())
    require(pending["status"] == 409 and pending["response"]["confirmed"] is False,
            "actual pending response not retained")
    harness.refresh(expected=503)
    harness.control(harness.items, release=True)

    def query():
        try:
            return control.query(operation)
        except bridge.PausePending:
            return None

    receipt = harness.wait(query, seconds=15)
    postgres = harness.query_closed(operation)
    harness.case("HOST_OLD_CRON_AND_ACTUAL_SENDER_BRIDGE", {"host": host,
         "sender_binding": binding, "pending": pending, "handshake": received,
         "sender_receipt": receipt, "postgres": postgres,
         "scope": "three sender groups and one actual registered synthetic cron child only",
         "other_application_ingress_covered": False, "queue_empty_proven": False,
         "production_adapter_bound": False, "switch_authorized": False})


if __name__ == "__main__":
    if sys.argv[1:] != ["--cron-child"]:
        raise SystemExit("only an isolated registered cron child")
    print("ready", flush=True)
    sys.stdin.buffer.read(1)
