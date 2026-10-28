"""Actual first-CLOSED Go gate, retained-container lifecycle and T0 activation.

Only new synthetic storage and an internal network (or a strictly attested
private host namespace with its own daemon and bridge) are used. The real worker
filters the sole Synthetic article through an explicit blacklist; llm=[] and
no external network means this proof makes no model or scoring-quality claim.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts import private_release_contract as private, production_activation_fixture as fixture  # noqa: E402
from scripts import verify_production_startup_native as startup_native  # noqa: E402
from scripts import production_gateway_native as gateway_native  # noqa: E402
from scripts import production_native_deadline as native_deadline  # noqa: E402
from scripts.verify_miniflux_pause_native import Harness, CLIENT, image_checks, source_binding, sha  # noqa: E402
from scripts.miniflux_host_joint_native import component  # noqa: E402
from scripts.miniflux_handoff_adapter import source_sha256 as bridge_source, CutoverError  # noqa: E402
from production_sender_driver import SenderActivationDriver, PostgresReader, sources as sender_sources  # noqa: E402
from scripts.sender_drain_inventory import IDENTITY_SQL, IDENTITY_KEYS  # noqa: E402
from services import admission_fence as gate  # noqa: E402

REDIS = "redis:7-alpine@sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99"


class LostAck(RuntimeError):
    """The synthetic transport discarded a response after the real action."""


def operation(label):
    return hashlib.sha256(("synthetic-production-activation:" + label).encode()).hexdigest()


def require_rejected(function):
    try:
        function()
    except (ValueError, gate.AdmissionError, CutoverError, FileNotFoundError):
        return
    raise AssertionError("unsafe synthetic request unexpectedly accepted")


class ActivationHarness(Harness):
    sender_role = "miniflux"

    def __init__(self, *args, private_host_context=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.private_host_context = private_host_context
        if private_host_context is not None:
            from scripts.production_activation_host_native import PrivateHostContext
            gate._require(type(private_host_context) is PrivateHostContext, "exact private host context required")
            private_host_context.check()
            self.started = private_host_context.plan["started"]

    def command(self, *args, **kwargs):
        context = getattr(self, "private_host_context", None)
        if context is not None:
            context.check(final=kwargs.get("final", False))
            from scripts.production_activation_host_native import remaining
            end = context.plan["deadline"] if kwargs.get("final", False) else context.plan["active_deadline"]
            kwargs["timeout"] = min(kwargs.get("timeout", 30), remaining(end))
        try:
            result = super().command(*args, **kwargs)
            if context is not None:
                saved = gate._read(self.output / f"command-{self.sequence:04d}.json")
                if saved.get("rc") is None:
                    context.commands.uncertain_mutation = True
                context.check(final=kwargs.get("final", False))
            return result
        except BaseException as error:
            if context is not None:
                context.commands.uncertain_mutation = True
                try:
                    context.check(final=kwargs.get("final", False))
                except BaseException as drift:
                    error.add_note("private host identity observation failed: " + type(drift).__name__)
            raise

    def prepare_network(self):
        if self.private_host_context is None:
            return super().prepare_network()
        network = self.private_host_context.prepare_network(self)
        fixture.save(self.output / "NETWORK.json", network)

    def compose_parser_command(self, args):
        allowed = (("compose", "version"), ("compose", "create", "--help"),
                   ("compose", "create", "--no-deps", "--help"))
        gate._require(tuple(args) in allowed, "only static Compose parser commands permitted")
        self.sequence += 1
        path = self.output / f"command-{self.sequence:04d}.json"
        gate._require(not gate._present(path), "static Compose evidence already exists")
        command = ["docker", "--host", "unix:///var/run/docker.sock", *args]
        output, total, code, error = [bytearray(), bytearray()], 0, None, None
        context = getattr(self, "private_host_context", None)
        deadline = min(time.monotonic() + 5, context.plan["active_deadline"]) if context else time.monotonic() + 5
        try:
            with subprocess.Popen(command, env=self.env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True) as process:
                try:
                    with selectors.DefaultSelector() as selector:
                        for index, pipe in enumerate((process.stdout, process.stderr)):
                            selector.register(pipe, selectors.EVENT_READ, index)
                        while selector.get_map():
                            gate._require(time.monotonic() < deadline, "static Compose parser deadline")
                            for key, _events in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                                chunk = os.read(key.fileobj.fileno(), 4096)
                                if not chunk:
                                    selector.unregister(key.fileobj)
                                    continue
                                total += len(chunk)
                                gate._require(total <= 8 * 1024, "static Compose parser output bound")
                                output[key.data].extend(chunk)
                        code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
        except BaseException as failure:
            error = type(failure).__name__
            raise
        finally:
            gate._write(path, {"argv": command, "rc": code, "error_type": error,
                "stdout": output[0].decode(errors="replace"), "stderr": output[1].decode(errors="replace"),
                "timeout_seconds": 5, "max_output_bytes": 8 * 1024, "daemon_mutation": False})
        return gate._read(path)

    def compose_parser_evidence(self):
        """CLI parser only: no manifest, container, network, or daemon mutation."""
        version = self.compose_parser_command(["compose", "version"])
        gate._require(version["rc"] == 0 and "Docker Compose version" in version["stdout"], "actual Compose plugin version required")
        help_record = self.compose_parser_command(["compose", "create", "--help"])
        gate._require(help_record["rc"] == 0 and all(flag in help_record["stdout"] for flag in ("--no-recreate", "--no-build", "--pull")),
                      "actual Compose create help required")
        refusal = self.compose_parser_command(["compose", "create", "--no-deps", "--help"])
        record_path = self.output / f"command-{self.sequence:04d}.json"
        gate._require(type(refusal["rc"]) is int and refusal["rc"] != 0
                      and "unknown flag: --no-deps" in refusal["stderr"],
                      "unsupported create flag was not rejected by the actual CLI parser")
        fixture.save(self.output / "COMPOSE_PARSER.json", {"version": version["stdout"].strip(),
            "create_no_deps_rejected": True, "rejection_record": record_path.name,
            "daemon_mutation": False, "production_access": False})

    def stage(self, name):
        if getattr(self, "private_host_context", None) is not None:
            self.private_host_context.check()
            self.private_host_context.check_running_inventory(self)
        sequence = getattr(self, "stage_sequence", 0) + 1
        gate._require(sequence <= 32, "synthetic stage budget exceeded")
        self.stage_sequence, self.current_stage = sequence, name
        fixture.save(self.output / f"STAGE_{sequence:02d}.json", {"stage": name,
            "elapsed_seconds": time.monotonic() - self.started, "production_access": False})
        print("activation_native_stage=" + name, flush=True)

    def inspect_role(self, role):
        return json.loads(self.command(["inspect", self.names[role]]))[0]

    def redis(self):
        info = json.loads(self.command(["image", "inspect", REDIS]))[0]
        image_checks(info, REDIS)
        self.launch("redis", info["Id"], ["--read-only", "--cap-drop=ALL", "--user", "999:999",
                    "--tmpfs", "/data:rw,nosuid,nodev,size=16777216,uid=999,gid=999"],
                    ["redis-server", "--save", "", "--appendonly", "no", "--maxmemory", "64mb",
                     "--maxmemory-policy", "noeviction"], memory=128 * 1024**2, pids=64)
        self.wait(lambda: self.command(["exec", self.names["redis"], "redis-cli", "ping"]).strip() == "PONG", readiness=True)

    def activation_driver(self, p, initial_operation):
        import psycopg
        pg = self.inspect_role("pg")
        address = pg["NetworkSettings"]["Networks"][self.network]["IPAddress"]
        gate._require(address, "owned PostgreSQL address absent")
        dsn = f"postgres://synthetic:{self.password}@{address}:5432/synthetic_pause?sslmode=disable"
        path = p["directory"] / "postgres.json"
        fixture.save(path, {"dsn": dsn})
        with psycopg.connect(dsn, autocommit=True, connect_timeout=3) as connection:
            connection.execute("SET statement_timeout='3000ms'")
            identity = dict(zip(IDENTITY_KEYS, connection.execute(IDENTITY_SQL).fetchone()))
        gate._require(identity["schema_version"] == 135, "actual Go database migration135 required")
        sender = self.inspect_role("sender")
        binding = {"schema": 1, "scope": "instrumented_sender_only", "run_id": self.owner,
            "control_id": "miniflux", "component_name": sender["Name"].lstrip("/"), "component": component(sender),
            "manifest": self.manifest, "application_source_sha256": sha(self.repo / "ops/miniflux/pause-source-lock.json"),
            "source_sha256": bridge_source(), "generation": 0, "initial_operation_id": initial_operation,
            "entrances": ["miniflux_scheduler", "miniflux_manual", "legacy_async_sender"]}
        now = int(time.time())
        plan = {"schema": 1, "scope": "instrumented_sender_only", "binding": binding,
                "postgres_identity": identity, "window": {"not_before": now - 1, "expires_at": now + 600},
                "sources": sender_sources()}
        transport = ActivationTransport(self)
        driver = SenderActivationDriver(plan, transport, PostgresReader(path, identity), p["directory"] / "sender-control")
        p.update({"dsn": path, "postgres_fixture": {"expected_identity": identity, "manifest": self.manifest},
                  "closed_operation": initial_operation, "sender_transport": transport})
        return driver

    def receiver_http(self, container, path):
        payload = {"url": "http://127.0.0.1:5000" + path, "method": "GET", "value": None}
        result = self.command(["exec", "-i", container, "python", "-I", "-c", CLIENT], stdin=json.dumps(payload), timeout=15)
        return json.loads(result)

    def consume_after_open(self, p, receiver):
        category = self.http("sender", "/v1/categories")[0]["id"]
        self.feed_id = self.http("sender", "/v1/feeds", "POST",
            {"feed_url": "http://fixture:8080/feed", "category_id": category}, expected=201)["feed_id"]
        self.sql("UPDATE integrations SET webhook_enabled=true,webhook_url='http://ai-filter-api:5000/webhook',"
                 "webhook_secret='synthetic-webhook-secret' WHERE user_id=1;")
        self.control(["seed", "activation-original"])
        self.refresh()
        acked = self.wait(lambda: self.proved_row("activation-original"), seconds=45)
        raw = base64.b64decode(acked["body_b64"], validate=True)
        from services.webhook_receipts import ReceiptStore
        store = ReceiptStore(p["paths"]["spool"])
        receipt = self.wait(lambda: completed_receipt(store, acked["delivery_id"], raw), seconds=45)
        gate._require(receiver.query_resume()["phase"] == "open", "receiver changed during actual delivery")
        fixture.save(p["directory"] / "ACTUAL_DELIVERY.json", {"sender": acked, "receiver": receipt,
            "body_sha256": hashlib.sha256(raw).hexdigest(), "deterministic_blacklist": "Synthetic",
            "real_worker_consumption": True, "real_model_calls": 0, "scoring_quality_proven": False})


class ActivationTransport:
    def __init__(self, harness):
        self.harness, self.drop, self.posts = harness, set(), []

    def inspect(self, identifier):
        row = self.harness.inspect_role("sender")
        gate._require(row["Id"] == identifier, "exact sender container changed")
        return [row]

    def request(self, method, route, value=None):
        if method == "POST":
            self.posts.append(route)
        result = self.harness.private_exchange(route, method, value)
        if method == "POST" and route in self.drop:
            self.drop.remove(route)
            fixture.save(self.harness.output / ("LOST_ACK_" + route.lstrip("/") + ".json"),
                         {"status": result[0], "actual_response": result[1], "request": value})
            raise LostAck("synthetic response dropped after actual PostgreSQL operation")
        return result


def completed_receipt(store, delivery_id, raw):
    fd = gate._lock(store.spool / ".lock", True, 0.1)
    try:
        # Read and validate one original under the real spool lock. This
        # observer does not use load(), which may recover pending writes.
        receipt = store._read_record(store.directory / (delivery_id + ".json"), delivery_id)
        if not receipt or receipt["status"] != "completed":
            return None
        gate._require(base64.b64decode(receipt["raw_body_b64"], validate=True) == raw,
                      "actual raw delivery differs after consumer completion")
        return receipt
    finally:
        os.close(fd)


def closed_initialization(harness, p):
    initial = operation("sender-initial-closed")
    driver = harness.activation_driver(p, initial)
    transport = p["sender_transport"]
    transport.drop.add("/initialize-closed")
    try:
        driver.initialize_closed(initial)
    except LostAck:
        receipt = driver.query_initialize_closed(initial)
    else:
        raise AssertionError("synthetic initialize ACK was not dropped")
    gate._require(receipt["state"] == "closed" and receipt["generation"] == 0, "first CLOSED result differs")
    require_rejected(lambda: driver.initialize_closed(initial))
    gate._require(transport.posts.count("/initialize-closed") == 1, "initial close was reposted")
    harness.http("sender", "/v1/categories", expected=503)
    fixture.save(p["directory"] / "INITIAL_CLOSED.json", {"receipt": receipt, "postgres": driver.bind(),
                 "post_count": 1, "historical_coverage": "unknown_before_T0_no_replay"})
    return driver


def create_and_start(session, p):
    from services import production_container_lifecycle as lifecycle
    paths = {"database": "/app/data/fixture.db", "config": "/app/config.yaml",
             "freeze": "/app/data/admission_fence", "state": "/app/data/startup_slots"}
    args = (session, p["paths"]["grants"], p["paths"]["slots"], p["paths"]["lifecycle"])
    created = lifecycle.create(*args, operation_id=p["operation"], paths=paths, web_workers=2, threads=4)
    original = private.run
    calls = []
    def dropped_start(command):
        result = original(command)
        if command[:2] == ["docker", "start"]:
            calls.append(command)
            raise LostAck("synthetic Docker start ACK discarded after actual start")
        return result
    try:
        private.run = dropped_start
        try:
            lifecycle.start(*args, operation_id=p["operation"])
        except LostAck:
            pass
        else:
            raise AssertionError("start ACK was not discarded")
    finally:
        private.run = original
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        result = lifecycle.query(*args, operation_id=p["operation"])
        if result["status"] == "closed_initialized":
            gate._require(len(calls) == 1, "Docker start repeated")
            require_rejected(lambda: lifecycle.start(*args, operation_id=p["operation"]))
            return created, result
        time.sleep(0.1)
    raise TimeoutError("actual candidate startup did not confirm")


def actual_namespace_transport(harness, p, stopped):
    """Only this synthetic Go container; the production transport has no sudo."""
    record = harness.inspect_role("sender")
    # Root originals stay outside the runner's unprivileged upload inventory.
    # The fixed helper exports bounded redacted copies with original hashes.
    original = "/tmp/activation-namespace-original-" + harness.owner
    args = ["/usr/bin/sudo", "-n", "--", "/usr/bin/python3", "-I", "-B",
            str(harness.repo / "scripts/production_namespace_native.py"),
            "--container-id", record["Id"], "--owner", harness.owner,
            "--image-id", record["Image"], "--manifest", harness.manifest,
            "--network", harness.network, "--stop-operation", stopped["sender"]["operation_id"],
            "--evidence-root", original]
    context = getattr(harness, "private_host_context", None)
    if context is not None:
        context.check()
        args += ["--private-host-context", str(context.path), "--private-host-context-sha256", context.proof_sha256]
    path = p["directory"] / "NAMESPACE_PROCESS"
    startup_native.archive_command(args, path, seconds=35, limit=2 * 1024**2,
        child_umask=0o077, stdin_bytes=json.dumps({"token": harness.token}).encode())
    result = json.loads(Path(str(path) + ".stdout").read_bytes())
    gate._require(result["status"] == "passed" and result["namespace_transport"] is True
                  and result["post_count"] == 1 and result["queue_drained"] is False,
                  "actual fixed network namespace transport proof missing")
    fixture.save(p["directory"] / "ACTUAL_NAMESPACE_TRANSPORT.json", result)


def finite_native_consumption(harness, p, receiver, driver):
    from services import production_admission_lifecycle as lifetime
    from scripts import production_admission_supervisor as supervisor
    harness.stage("actual_finite_admission_lease")
    now = int(time.time())
    policy = {"schema": lifetime.SCHEMA, "operation_id": operation("synthetic-continuation-authority"),
              "approved_by": "synthetic-native-workflow", "not_before": now - 1, "expires_at": now + 900,
              "lease_seconds": 300, "renew_before_seconds": 60, "max_renewals": 4,
              "receiver_stop_records": 127, "receiver_stop_bytes": 7 * 1024**2,
              "sender_stop_records": 100, "sender_stop_bytes": 7 * 1024**2,
              "minimum_free_bytes": 64 * 1024**2}
    authority = lifetime.initialize(receiver, driver, policy)
    lease = lifetime.query(receiver, driver, operation_id=authority["lease"]["operation_id"])
    fixture.save(p["directory"] / "ACTUAL_FINITE_LEASE.json", {"authority": authority, "query": lease,
                 "real_sender_and_receiver": True, "production_access": False})
    ongoing = supervisor.prepare(receiver, driver)
    harness.stage("actual_go_to_real_worker_deterministic_consumption")
    harness.consume_after_open(p, receiver)
    return ongoing


def activate(harness, p, driver, migration_root, native_driver, *, release_lock=None):
    from services.production_activation_control import locked_activation
    from services.production_container_lifecycle import verify_active
    with locked_activation(migration_root, p["paths"]["grants"], p["paths"]["slots"],
                           operation_id=p["activation_operation"], release_lock=release_lock) as receiver:
        harness.stage("actual_consumer_prepare_while_closed")
        require_rejected(lambda: driver.production_resume(p["closed_operation"], 0, object()))
        require_rejected(receiver.resume)
        state = gate._read(p["paths"]["fence"] / "state.json")
        intents, pending = p["fence"]._operation_inventory()
        gate._require(state["phase"] == "frozen" and state["generation"] == 0 and not intents and not pending
                      and p["sender_transport"].posts == ["/initialize-closed"], "early resume changed a real boundary")
        fixture.save(p["directory"] / "EARLY_RESUME_REJECTIONS.json", {
            "receiver_state": state, "receiver_operation_count": 0,
            "sender_post_paths": list(p["sender_transport"].posts),
            "fake_receiver_capability_rejected": True, "receiver_before_ready_rejected": True})
        prepared = receiver.prepare()
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            observed = receiver.query()
            gate._require(observed["status"] != "activation_uncertain", "actual subsystem preparation failed")
            if observed["status"] == "business_prepared":
                break
            time.sleep(0.1)
        else:
            raise TimeoutError("actual consumer threads did not confirm")
        confirmed = receiver.confirm()
        harness.stage("actual_fixed_gateway_switch_while_closed")
        from scripts.production_gateway_switch import GatewaySwitch
        gateway = GatewaySwitch(receiver, p["gateway_plan"], p["gateway_evidence"])
        switched = gateway.switch()
        checked_gateway = gateway.query()
        fixture.save(p["directory"] / "ACTUAL_GATEWAY.json", {"switch": switched, "query": checked_gateway,
                     "real_nginx_and_http": True, "production_access": False})
        from scripts import production_admission_watchdog as watchdog
        from scripts.controlled_production_release import _actual_child_receipt
        guardian = watchdog.prepare(receiver, native_driver)
        p["watchdog_prepared"] = guardian
        child = watchdog.start(guardian["plan_path"])
        p["watchdog_child"] = child
        guardian_root = Path(guardian["plan_path"]).parent
        ready = _actual_child_receipt(child, lambda: watchdog.query_ready(guardian["plan_path"], child.pid), 8,
                                      receipt_path=guardian_root / "pulse.json", stop_paths=(guardian_root / "STOP.json",))
        fixture.save(p["directory"] / "ACTUAL_PREOPEN_GUARDIAN.json", {"plan": guardian, "ready": ready,
                     "actual_process": True, "transport_scope": "synthetic_loopback_to_owned_go"})
        harness.stage("receiver_resume_and_independent_runtime_verification")
        resumed = receiver.resume()
        verified = verify_active(receiver, p["paths"]["lifecycle"], operation_id=p["operation"])
        harness.stage("sender_resume_lost_ack_exact_query")
        transport = p["sender_transport"]
        transport.drop.add("/production-resume")
        try:
            driver.production_resume(p["closed_operation"], 0, receiver)
        except LostAck:
            intent = gate._read(driver.control.root / "production_resume.intent.json")
            sender = driver.query_production_resume(intent["operation_id"], receiver)
        else:
            raise AssertionError("synthetic sender resume ACK was not dropped")
        gate._require(transport.posts.count("/production-resume") == 1, "sender resume was reposted")
        fixture.save(p["directory"] / "ACTIVATION.json", {"prepared": prepared, "confirmed": confirmed,
            "receiver": resumed, "runtime_verified": verified, "sender": sender,
            "sender_resume_post_count": 1, "production_access": False})
        ongoing = finite_native_consumption(harness, p, receiver, native_driver)
        return {"watchdog": guardian, "supervisor": ongoing}


def complete_native_supervision(harness, p, active, native_driver, migration_root):
    from scripts.controlled_production_release import _handoff
    from services.production_activation_control import locked_activation
    from services import production_admission_lifecycle as lifetime
    harness.stage("actual_same_guardian_handoff_and_live_tick")
    live = _handoff(active)
    fixture.save(p["directory"] / "ACTUAL_SUPERVISOR_TICK.json", {"tick": live, "same_guardian_pid": True,
        "actual_go_and_pg": True, "transport_scope": "synthetic_loopback_to_owned_go", "production_access": False})
    harness.stage("actual_restrictive_stop_without_false_drain")
    with locked_activation(migration_root, p["paths"]["grants"], p["paths"]["slots"],
                           operation_id=p["activation_operation"]) as receiver:
        stopped = lifetime.stop_new(receiver, native_driver, reason_code="operator")
    gate._require(stopped["status"] == "producers_stopped_drain_pending" and stopped["drained"] is False,
                  "actual restrictive stop not confirmed")
    fixture.save(p["directory"] / "ACTUAL_RESTRICTIVE_STOP.json", stopped)
    child = p["watchdog_child"]
    gate._require(child.pid == live["owner"]["pid"], "native guardian child differs from actual live owner")
    status = child.wait(timeout=25)
    gate._require(status == 0, "actual guardian did not exit after confirmed original controlled stop")
    fixture.save(p["directory"] / "ACTUAL_GUARDIAN_EXIT.json", {"pid": live["owner"]["pid"], "exit_code": status,
                 "actual_child_reaped": True, "forced_kill": False})
    harness.stage("actual_fixed_network_namespace_transport")
    actual_namespace_transport(harness, p, stopped)


def retained_native_phases(harness, p, driver, native_driver, permit, migration_root):
    from services import production_migration as migration
    from services.production_activation_control import arm
    from services.production_release_lock import retained_release_lock
    with retained_release_lock(permit["locks"]["release"]["path"]) as release_lock:
        with migration.locked_session(migration_root, permit, release_lock=release_lock) as session:
            session.authorize()
            completed = session.migrate()
            created, started = create_and_start(session, p)
            web = created["containers"]["web"]
            gate._require(harness.receiver_http(web, "/refresh")["status"] == 503, "closed receiver performed business")
            from services.production_admission_lifecycle import source_sha256
            image_source = harness.command(["exec", web, "python", "-I", "-B", "-c",
                "import sys;sys.path.insert(0,'/app');"
                "from services.production_admission_lifecycle import source_sha256;print(source_sha256())"], timeout=5).strip()
            gate._require(image_source == source_sha256(), "actual runtime continuation source bytes differ")
            fixture.save(p["directory"] / "ACTUAL_IMAGE_ADMISSION_SOURCES.json", {
                "container_id": web, "source_sha256": image_source, "actual_image_execution": True,
                "production_access": False})
            armed = arm(session, p["paths"]["grants"], p["paths"]["slots"], operation_id=p["activation_operation"])
            fixture.save(p["directory"] / "CLOSED_LIFECYCLE.json", {"migration": completed, "created": created,
                "started": started, "armed": armed, "old_container_ids": sorted(p["previous_containers"]),
                "old_binary_execution_proven": False})
        return activate(harness, p, driver, migration_root, native_driver, release_lock=release_lock)


def abort_native_guardian(p):
    """Keep the real transport alive until independent restrictive calls finish."""
    from scripts import production_admission_watchdog as watchdog
    prepared = p.get("watchdog_prepared")
    if prepared is None:
        return
    try:
        value = watchdog.abort(prepared["plan_path"])
    except BaseException as error:
        value = {"status": "stop_unconfirmed", "error_type": type(error).__name__,
                 "message_sha256": hashlib.sha256(str(error).encode()).hexdigest()}
    fixture.save(p["directory"] / "NATIVE_GUARDIAN_FAILURE_STOP.json", value)
    child = p.get("watchdog_child")
    if child is not None:
        try:
            status = child.wait(timeout=8)
            exited = {"pid": child.pid, "exit_code": status, "actual_child_reaped": True,
                      "forced_kill": False, "bridge_still_available": True}
        except subprocess.TimeoutExpired:
            exited = {"pid": child.pid, "status": "exit_unconfirmed", "actual_child_reaped": False,
                      "forced_kill": False, "bridge_still_available": True}
        fixture.save(p["directory"] / "NATIVE_GUARDIAN_FAILURE_EXIT.json", exited)


def scenario(harness, output):
    from scripts.production_sender_native_bridge import NativeControlBridge
    harness.stage("never_opened_receiver_disk")
    p = fixture.prepare_disk(output / "receiver", private.source_identity(harness.repo), harness.network, harness.python_id)
    p["activation_operation"] = operation("receiver-activation")
    try:
        harness.sender_contract = p["strategy"]
        harness.stage("actual_go_schema135_first_closed")
        harness.launch_sender()
        driver = closed_initialization(harness, p)
        harness.stage("retain_old_stopped_containers_and_seal")
        gateway_native.prepare(harness, p)
        sealed = fixture.seal_inputs(harness.repo, p)
        contract, _manifest, contract_sha = private.load(sealed, harness.repo, require_fresh=True)
        harness.stage("build_exact_application_image")
        if getattr(harness, "private_host_context", None) is None:
            digest = startup_native.build_candidate(harness.repo, p["directory"], p["source"],
                        "ai-filter-activation-native:" + p["project"], contract, contract_sha)
        else:
            digest = harness.private_host_context.build_candidate(p, sealed, contract_sha)
        harness.stage("probe_exact_application_image")
        if getattr(harness, "private_host_context", None) is None:
            startup_native.probe_candidate(contract, sealed.parent, digest, p["directory"])
        else:
            startup_native.probe_candidate(contract, sealed.parent, digest, p["directory"],
                                           deadline=harness.private_host_context.plan["active_deadline"])
        harness.stage("bind_exact_migration_permit")
        permit = fixture.migration_permit(p, sealed, digest)
        gateway_native.bind_plan(p, permit)
        migration_root = p["directory"] / "migration"
        harness.stage("actual_locked_migration_create_grant_start")
        token_file = p["directory"] / "synthetic-control-token.json"
        with NativeControlBridge(harness) as bridge:
            native_driver = SenderActivationDriver(driver.plan, bridge.transport(token_file), driver.postgres, driver.root)
            try:
                active = retained_native_phases(harness, p, driver, native_driver, permit, migration_root)
                complete_native_supervision(harness, p, active, native_driver, migration_root)
            except BaseException:
                abort_native_guardian(p)
                raise
    finally:
        with native_deadline.final_phase():
            try:
                try:
                    gateway_native.preserve(harness, p)
                finally:
                    startup_native.preserve_scenario(p)
            finally:
                p["anchor"].close()


def record_failure(error, repo, output, stage):
    failure = {"type": type(error).__name__, "message_sha256": hashlib.sha256(str(error).encode()).hexdigest(),
               "locations": [{"file": Path(row.filename).name, "function": row.name, "line": row.lineno}
                             for row in traceback.extract_tb(error.__traceback__) if Path(row.filename).resolve().is_relative_to(repo)]}
    print("activation_native_failure=" + json.dumps({"type": failure["type"],
          "message_sha256": failure["message_sha256"], "stage": stage}, sort_keys=True), flush=True)
    fixture.save(output / "FAILURE.json", failure)
    return failure


def execute(repo, source, image_id, binary, output, *, private_host_context=None):
    binding = source_binding(repo, source, binary)
    harness = ActivationHarness(repo, source, image_id, binary, output, private_host_context=private_host_context)
    fixture.save(output / "SOURCE.json", binding)
    failure, evidence_errors = None, []
    try:
        harness.stage("actual_compose_parser_metadata")
        harness.compose_parser_evidence()
        harness.stage("bounded_internal_network_and_real_redis")
        harness.prepare()
        harness.redis()
        scenario(harness, output)
    except BaseException as error:
        failure = record_failure(error, repo, output, harness.current_stage)
    finally:
        with native_deadline.final_phase():
            if "pg" in harness.names:
                try:
                    harness.command(["exec", harness.names["pg"], "pg_dump", "-U", "synthetic", "-d", "synthetic_pause"],
                                    timeout=30, final=True)
                except Exception as error:
                    evidence_errors.append(type(error).__name__)
            evidence_errors.extend(harness.preserve())
    report = {"schema": "production-activation-native-v1", "source_sha": binding["revision"],
        "status": "passed" if failure is None and not evidence_errors else "failed_or_evidence_insufficient",
        "failure": failure, "evidence_errors": evidence_errors, "elapsed_seconds": time.monotonic() - harness.started,
        "last_stage": harness.current_stage,
        "synthetic_only": True, "production_access": False, "existing_126_modified": False, "real_models": 0,
        "limits": ["unknown_before_T0_no_replay", "old_binary_not_executed", "no_power_loss_proof", "no_model_quality_proof"],
        "app_memory_bytes": 768 * 1024**2, "app_pids": 256, "pg_memory_bytes": 384 * 1024**2, "pg_pids": 64}
    fixture.save(output / "RESULT.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "source", "binary", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--image-id", required=True)
    args = parser.parse_args()
    report = execute(args.repo.resolve(), args.source.resolve(), args.image_id, args.binary.resolve(), args.output.resolve())
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
