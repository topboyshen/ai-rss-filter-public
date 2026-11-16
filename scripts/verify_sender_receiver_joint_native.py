"""New owned Go -> real Flask -> queue/receipt -> frozen PG rehearsal.

The receiver scorer is explicitly fake. Raw delivery, HMAC, durable queues,
ReceiptStore, PostgreSQL, control operations and retained locks are real. This
uses fresh storage; prior native held originals and failures stay untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

# This is a direct host CLI; application modules live beside scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_miniflux_pause_native import (Harness, CLIENT, MEMORY, PIDS, save, sha,
                                        image_checks, resource_checks, source_binding)
from miniflux_host_joint_native import component
from miniflux_handoff_adapter import source_sha256 as bridge_source, StopNewPending, PausePending
from production_sender_driver import SenderDriver, sources as sender_sources
from sender_drain_inventory import PostgresReader, IDENTITY_SQL, IDENTITY_KEYS
from durable_handoff_cutover import durable_write, private_directory, require
from services import admission_fence as gate

STRATEGY = "native-joint-synthetic-v1"
CASE = "CASE_REAL_GO_FLASK_DURABLE_DRAIN_AND_FROZEN_INVENTORY.json"


def operation(name):
    return hashlib.sha256(("native:sender-receiver-joint:" + name).encode()).hexdigest()


class Joint(Harness):
    sender_role, sender_contract = "miniflux", STRATEGY

    def inspect_role(self, name):
        return json.loads(self.command(["inspect", self.names[name]]))[0]

    def exchange_receiver(self, method, path, value=None):
        row = self.inspect_role("receiver")
        resource_checks(row, self.receiver_image, self.owner, self.network)
        payload = {"url": "http://127.0.0.1:8766" + path, "method": method,
                   "value": value, "auth": "Bearer " + self.token}
        raw = self.command(["exec", "-i", self.names["receiver"], "python", "-B", "-c", CLIENT],
                           stdin=json.dumps(payload), timeout=15)
        result = json.loads(raw)
        require(result["status"] == 200, "actual receiver control unconfirmed; query exact operation")
        return json.loads(result["body"])

    def prepare_receiver_files(self, receiver_image):
        self.receiver_image = receiver_image
        row = json.loads(self.command(["image", "inspect", receiver_image]))[0]
        image_checks(row, receiver_image)
        revision = json.loads((self.output / "SOURCE.json").read_text())["revision"]
        require(row["Config"]["Labels"]["org.opencontainers.image.revision"] == revision,
                "actual receiver image revision differs")
        self.state = self.output / "receiver-state"
        private_directory(self.state, create=True)
        durable_write(self.state / "synthetic-owner.json", {"schema":1,"synthetic":True,"nonce":operation(self.owner)})
        durable_write(self.state / "token.json", {"token":self.token})
        durable_write(self.state / "config.json", {"scoring_strategy_version":STRATEGY,
                      "feeds":[{"url":"http://fixture:8080/feed","name":"Synthetic joint feed"}]})
        self.receiver_binding = {"revision":revision,"image_id":receiver_image,"strategy":STRATEGY,
                                 "source_sha256":gate.source_sha256(),"deployment_nonce":self.owner}
        now = int(time.time())
        self.window = {"not_before":now-1,"expires_at":now+300}
        gate.provision(self.state / "fence", self.receiver_binding,
                       {**self.window,"max_admissions":256,"max_bytes":8*1024**2})
        self.fence = gate.Fence(self.state / "fence", self.receiver_binding)
        self.fence.open_trial(operation("receiver-initial-open"), 0, "owned synthetic receiver initial admission")

    def launch_receivers(self):
        for name, role in (("receiver", "web"), ("worker", "worker")):
            env = {"AI_FILTER_ROLE":role,"WEBHOOK_SPOOL_DIR":str(self.state / "spool"),
                   "AI_FILTER_ADMISSION_ROOT":str(self.state / "fence"),"AI_FILTER_ADMISSION_MODE":"enforced",
                   "AI_FILTER_ADMISSION_BINDING":json.dumps(self.receiver_binding),
                   "AI_FILTER_REVISION":self.receiver_binding["revision"],
                   "AI_FILTER_DB_PATH":str(self.state / "unused.sqlite"),
                   "MINIFLUX_WEBHOOK_SECRET":"synthetic-webhook-secret","PYTHONPATH":"/app"}
            extra = ["--read-only","--cap-drop=ALL","--user",f"{os.getuid()}:{os.getgid()}",
                     "--label","io.ai-filter.handoff.role="+role,
                     "--tmpfs","/tmp:rw,nosuid,nodev,size=33554432",
                     "--mount",f"type=bind,src={self.state},dst={self.state}",
                     "--mount",f"type=bind,src={self.repo / 'scripts/receiver_drain_fixture_runtime.py'},dst=/fixture_runtime.py,readonly"]
            for key, value in env.items():
                extra += ["-e", key + "=" + value]
            command = ["python","-B","/fixture_runtime.py","--root",str(self.state),"--token-file",str(self.state / "token.json"),
                       "--seconds","300","--max-consume","16"] + (["--worker"] if role == "worker" else [])
            self.launch(name, self.receiver_image, extra, command)
        self.wait(lambda: (self.state / "ready.json").exists() and (self.state / "worker-ready.json").exists(), seconds=30)
        self.ready = gate._read(self.state / "ready.json")
        require(self.ready["model"] == "fake", "native scoring boundary differs")

    def sender_driver(self, initial):
        import psycopg
        pg = self.inspect_role("pg")
        resource_checks(pg, self.images["pg"], self.owner, self.network, 384*1024**2, 64)
        address = pg["NetworkSettings"]["Networks"][self.network]["IPAddress"]
        require(address, "owned PostgreSQL internal address absent")
        dsn = f"postgres://synthetic:{self.password}@{address}:5432/synthetic_pause?sslmode=disable"
        path = self.output / "sender-dsn.json"
        durable_write(path, {"dsn":dsn})
        with psycopg.connect(dsn, autocommit=True, connect_timeout=3) as connection:
            connection.execute("SET statement_timeout='3000ms'")
            identity = dict(zip(IDENTITY_KEYS, connection.execute(IDENTITY_SQL).fetchone()))
        sender = self.inspect_role("sender")
        binding = {"schema":1,"scope":"instrumented_sender_only","run_id":self.owner,
                   "control_id":"miniflux","component_name":sender["Name"].lstrip("/"),"component":component(sender),
                   "manifest":self.manifest,"application_source_sha256":sha(self.repo / "ops/miniflux/pause-source-lock.json"),
                   "source_sha256":bridge_source(),"generation":initial["generation"],"initial_operation_id":initial["operation_id"],
                   "entrances":["miniflux_scheduler","miniflux_manual","legacy_async_sender"]}
        plan = {"schema":1,"scope":"instrumented_sender_only","binding":binding,"postgres_identity":identity,
                "window":self.window,"sources":sender_sources()}
        harness = self
        class Transport:
            def inspect(self, identifier):
                row = harness.inspect_role("sender")
                require(row["Id"] == identifier, "fixed actual sender changed")
                return [row]
            def request(self, method, route, value=None):
                return harness.private_exchange(route, method, value)
        return SenderDriver(plan, Transport(), PostgresReader(path, identity), self.state / "fence/sender")

    def receiver_driver(self):
        from production_receiver_driver import ReceiverDriver, sources
        from observe_handoff_capacity import spool_identity
        harness = self
        class Transport:
            def inspect(self, identifiers):
                rows = [harness.inspect_role(name) for name in ("receiver", "worker")]
                require({row["Id"] for row in rows} == set(identifiers), "fixed actual receiver components changed")
                return rows
            def request(self, method, path, value=None):
                return harness.exchange_receiver(method, path, value)
        rows = [self.inspect_role(name) for name in ("receiver","worker")]
        plan = {"schema":1,"scope":"instrumented_receiver_only","sources":sources(),
                "components":{row["Name"].lstrip("/"):component(row) for row in rows},"registration":self.ready["registration"],
                "host_fence_root":str(self.state / "fence"),"container_fence_root":str(self.state / "fence"),
                "spool":str(self.state / "spool"),"container_spool_root":str(self.state / "spool"),
                "spool_identity":spool_identity(self.state / "spool"), **self.window,"owner":"owned-native-synthetic",
                "capacity":{"stop_records":128,"stop_bytes":8*1024**2,"minimum_free_bytes":64*1024**2}}
        return ReceiverDriver(plan, Transport(), self.output / "receiver-controller")

    def scenario(self):
        self.launch_sender()
        initialize = operation("sender-initialize")
        initial = self.checked_receipt(self.private_control("/initialize", "POST", {"operation_id":initialize}),
                                       initialize, "open", action="initialize")
        category = self.http("sender", "/v1/categories")[0]["id"]
        self.feed_id = self.http("sender", "/v1/feeds", "POST", {"feed_url":"http://fixture:8080/feed","category_id":category}, expected=201)["feed_id"]
        self.sql("UPDATE integrations SET webhook_enabled=true,webhook_url='http://receiver:5000/webhook',webhook_secret='synthetic-webhook-secret' WHERE user_id=1;")
        self.items = ["seed", "joint-original"]
        self.control(self.items)
        self.refresh()
        rows = self.wait(lambda: rows if len(rows := self.outbox()) == 1 else None, seconds=10)
        require(rows[0]["state"] in {"pending","inflight"}, "real durable original absent before receiver startup")
        driver = self.sender_driver(initial)
        stop = operation("sender-stop-new")
        try:
            stopped = driver.stop_new(stop, "native real Go producer stop before exact transfer")
        except StopNewPending:
            stopped = driver.query_stop_new(stop)
        self.refresh(expected=503)
        sealed = driver.seal_originals(stop)
        save(self.output / "PRODUCER_STOP_AND_RAW.json", {"receipt":stopped,"sealed":sealed,"original_rows":rows})
        self.launch_receivers()
        receiver = self.receiver_driver()
        receiver_stop = receiver.operate("stop_new", operation("receiver-stop-new"), "native exact originals only")
        from receiver_drain_driver import ReceiverDrainDriver
        from services.receiver_drain_transfer import sources as transfer_sources
        from services.receiver_drain_storage import identity
        request = {"schema":1,"operation_id":operation("transfer"),"stop_operation_id":receiver_stop["operation_id"],
                   "generation":receiver_stop["generation"],"receiver_binding":self.receiver_binding,
                   "receiver_manifest_sha256":self.ready["registration"]["manifest_sha256"],"source_sha256":transfer_sources(),
                   "spool":str(self.state / "spool"),"spool_identity":identity(str(self.state / "spool")),
                   "sender_bundle":sealed["bundle"],"sender_manifest_sha256":sealed["manifest_sha256"],
                   "sender_binding":driver.plan["binding"],"postgres_identity":driver.plan["postgres_identity"],
                   "endpoint":"http://receiver:5000/webhook","contract":STRATEGY,"config_sha256":self.ready["config_sha256"],
                   "body_limit":self.ready["body_limit"],"max_batches":4,"max_replays":16}
        transfer = ReceiverDrainDriver(receiver, request, self.output / "transfer-controller")
        receipt = transfer.prepare()
        require(receipt["state"] == "complete" and receipt["confirmed"] == 1, "exact real queue transfer incomplete")
        save(self.output / "REAL_TRANSFER.json", receipt)
        # This ACK is produced by the real ordinary Flask route after the Go
        # sender POSTs its unchanged body/signature. No SQL ACK is seeded here.
        acked = self.wait(lambda: self.proved_row("joint-original"), seconds=75)
        require(acked["delivery_id"] == rows[0]["delivery_id"], "Go ACK changed original identity")
        pause = operation("sender-pause")
        try:
            frozen_sender = driver.freeze(pause, stop, "native independently drained sender full pause")
        except PausePending:
            frozen_sender = driver.query_freeze(pause)
        from independent_drain_inventory import directory_identity, _identity
        from joint_drain_controller import JointDrainController, sources as joint_sources, _file_identity
        from services.receiver_drain_storage import create_lock
        spec = {"spool":str(self.state / "spool"),"journal":str(self.state / "journal"),
                "database":str(self.state / "fixture.sqlite"),"identities":{
                    "spool":directory_identity(self.state / "spool"),"journal":directory_identity(self.state / "journal"),
                    "database":_identity((self.state / "fixture.sqlite").stat())}}
        for name in ("release", "refresh"):
            create_lock(self.state / (name + ".lock"))
        plan = {"schema":1,"scope":"instrumented_joint_drain_only","sender_plan_sha256":driver.plan_sha256,
                "receiver_plan_sha256":receiver.plan_sha256,"operations":{
                    "sender_stop":stop,"receiver_stop":receiver_stop["operation_id"],"transfer":request["operation_id"],
                    "sender_freeze":pause,"receiver_freeze":operation("receiver-freeze")},
                "transfer_policy":{key:request[key] for key in ("endpoint","contract","config_sha256","body_limit",
                                                               "max_batches","max_replays","source_sha256")},
                "inventory":spec,"locks":{name:_file_identity(self.state / (name + ".lock")) for name in ("release","refresh")},
                "source_sha256":joint_sources()}
        controller = JointDrainController(driver, receiver, plan, self.output / "joint-controller")
        boundary = controller.confirm_existing_boundaries(transfer)
        # Real sender ACK is transfer of durable ownership, not receiver queue
        # completion. The joint controller must leave these consumers enabled.
        try:
            controller.freeze_receiver()
        except Exception as failure:
            require("not independently drained" in str(failure), "unexpected negative joint freeze failure")
        else:
            raise ValueError("receiver froze while its real accepted task remained")
        blocked = self.fence.snapshot()
        require(blocked["phase"] == "draining"
                and not (controller.root / "receiver_freeze.intent.json").exists()
                and not (self.fence.root / "operations" / (operation("receiver-freeze") + ".intent.json")).exists(),
                "nonempty receiver created a freeze intent or stopped consumers")
        save(self.output / "REAL_QUEUE_BLOCKED_FREEZE.json", {"boundary":boundary,"fence":blocked,
             "freeze_intent_absent":True,"receiver_consumers_remain_enabled":True,"switch_authorized":False})
        consume = json.loads(self.command(["exec", "-i", self.names["receiver"], "python", "-B", "-c", CLIENT],
            stdin=json.dumps({"url":"http://127.0.0.1:5000/__synthetic__/consume","method":"POST","value":None,"auth":"Bearer "+self.token})))
        require(consume["status"] == 200 and json.loads(consume["body"])["consumed"] is True, "real queued task did not complete")
        require(not (self.state / "unused.sqlite").exists(), "synthetic receiver initialized an unrequested application database")
        scores = gate._read(self.state / "synthetic-scores.json")
        require(scores["real_model_calls"] == 0 and len(scores["calls"]) == 1
                and scores["calls"][0]["strategy"] == STRATEGY, "synthetic scoring evidence differs")
        frozen_receiver = controller.freeze_receiver()
        observed = frozen_receiver["inventory"]
        require(observed["storage_drained"] and not observed["blockers"]
                and observed["receiver"]["states"]["completed"] == 1, "real completed receipt or locked zero proof absent")
        self.case("REAL_GO_FLASK_DURABLE_DRAIN_AND_FROZEN_INVENTORY", {
            "sender_stop":stopped,"receiver_stop":receiver_stop,"transfer":receipt,"go_acked_original":acked,
            "sender_pause":frozen_sender,"receiver_freeze":frozen_receiver,"independent_inventory":observed,
            "synthetic_scoring_evidence":scores,"nonempty_freeze_refused":blocked,
            "scoring":"explicit synthetic fake; real models=0","switch_authorized":False,
            "old_uninstrumented_originals_proven":False,"webhook_and_receipt_store_are_real":True})



def run(repo, source, sender_image, receiver_image, binary, output):
    binding = source_binding(repo, source, binary)
    harness = Joint(repo, source, sender_image, binary, output)
    save(output / "SOURCE.json", {**binding,"joint_source_sha256":sha(Path(__file__)),
                                 "receiver_fixture_sha256":sha(repo / "scripts/receiver_drain_fixture_runtime.py")})
    error, evidence_errors = None, []
    try:
        harness.prepare()
        harness.prepare_receiver_files(receiver_image)
        harness.scenario()
        require((output / CASE).is_file(), "complete real joint stage evidence absent")
    except Exception as failure:
        error = {"type":type(failure).__name__,"message":harness.redacted(str(failure))}
        save(output / "FAILURE.json", error)
    finally:
        if "pg" in harness.names:
            try:
                harness.command(["exec", harness.names["pg"],"pg_dump","-U","synthetic","-d","synthetic_pause"], final=True)
            except Exception as failure:
                evidence_errors.append("synthetic_pg_dump:"+str(failure))
        evidence_errors.extend(harness.preserve())
    result = {"status":"passed" if error is None and not evidence_errors else "failed_or_evidence_insufficient",
              "error":error,"evidence_errors":evidence_errors,"elapsed_seconds":time.monotonic()-harness.started,
              "sender_memory_bytes":MEMORY,"sender_pids":PIDS,"pg_memory_bytes":384*1024**2,"pg_pids":64,
              "real_models":0,"scorer":"explicit fake","production_access":False,"deployment":False,
              "existing_126_modified":False,"switch_authorized":False,"retained_network":harness.network}
    save(output / "RESULT.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "source", "binary", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--sender-image", required=True)
    parser.add_argument("--receiver-image", required=True)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if not args.run:
        print(json.dumps({"status":"plan_only","production_access":False,"real_models":0}))
        return 0
    result = run(args.repo.resolve(),args.source.resolve(),args.sender_image,args.receiver_image,args.binary.resolve(),args.output.resolve())
    print(json.dumps(result))
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
