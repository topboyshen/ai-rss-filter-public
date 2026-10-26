"""Actual PostgreSQL permission/CAS verification, never production execution.

Host stdlib orchestration only; all SQL runs via psql inside the explicitly
identified bounded synthetic PostgreSQL container. No HTTP or model calls.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time

FIXED_PG = "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
MIGRATION_SHA = "42ee96b4a961887d5387f8a0261ec6f8e9132f78eb362760451c172341640dad"
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def literal(value):
    """SQL data literal only, never use to substitute an identifier."""
    return "'" + str(value).replace("'", "''") + "'"


def render(template, values):
    expected = {"OWNER", "REVIEWER", "EXECUTOR", "AUDITOR", "SCHEMA", "SOURCE"}
    if set(values) != expected or any(not IDENTIFIER.fullmatch(value) for value in values.values()):
        raise ValueError("exact safe SQL identifiers required")
    if not values["SCHEMA"].startswith("held_access_isolation_") or not values["SOURCE"].startswith("held_source_isolation_"):
        raise ValueError("isolated schemas required")
    for key, value in values.items():
        template = template.replace("__" + key + "__", value)
    if re.search(r"__[A-Z_]+__", template):
        raise ValueError("unresolved SQL placeholder")
    return template


class Runner:
    def __init__(self, container, source, output, docker="docker", docker_host=None):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", container):
            raise ValueError("exact container name or ID required")
        self.container, self.source, self.output = container, source, output
        self.docker = [docker] + (["-H", docker_host] if docker_host else [])
        self.number = 0
        self.lock = threading.Lock()
        self.cases = []
        suffix = secrets.token_hex(8)
        self.names = {"SCHEMA": "held_access_isolation_" + suffix, "SOURCE": "held_source_isolation_" + suffix}
        self.names.update({key: "ha_" + key.lower() + "_" + suffix for key in ("OWNER", "REVIEWER", "EXECUTOR", "AUDITOR")})
        self.logins = {role: "ha_" + role + "_" + suffix for role in ("review", "execute", "audit", "none", "dual")}
        self.s = self.names["SCHEMA"]
        self.outbox = self.names["SOURCE"] + ".webhook_outbox"
        self.delivery = sha(b"synthetic held message")
        self.note = sha(b"reviewed exact original; unknown delivery risk accepted")

    def save(self, name, data):
        fd = os.open(self.output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")

    def command(self, args, stdin=None, timeout=15):
        with self.lock:
            self.number += 1
            number = self.number
        started = time.monotonic()
        record = {"argv": args, "stdin_sha256": sha((stdin or "").encode()), "timeout_seconds": timeout}
        try:
            result = subprocess.run(args, input=stdin, text=True, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as error:
            def text(value):
                return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")
            record.update(exit_code=None, timed_out=True, stdout=text(error.stdout), stderr=text(error.stderr), elapsed_seconds=time.monotonic() - started)
            self.save(f"COMMAND-{number:03d}.json", record)
            raise RuntimeError("bounded synthetic command timed out") from error
        record.update(exit_code=result.returncode, timed_out=False, stdout=result.stdout, stderr=result.stderr, elapsed_seconds=time.monotonic() - started)
        self.save(f"COMMAND-{number:03d}.json", record)
        return result

    def sql(self, statement, actor=None, expect_ok=True):
        login = self.logins[actor] if actor else "postgres"
        text = "SET statement_timeout='3s'; SET lock_timeout='3s'; SET timezone='UTC'; " + statement
        result = self.command(self.docker + ["exec", "-i", self.container, "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-U", login, "-d", "synthetic_outbox"], text)
        if (result.returncode == 0) != expect_ok:
            raise AssertionError("unexpected real PostgreSQL permission/result; see command evidence")
        return result.stdout.strip()

    def inspect(self, name):
        # Exclude Env and complete command lines: only resource/identity fields.
        fmt = '{"id":{{json .Id}},"image":{{json .Image}},"configured_image":{{json .Config.Image}},"state":{{json .State}},"memory":{{.HostConfig.Memory}},"memory_swap":{{.HostConfig.MemorySwap}},"pids":{{.HostConfig.PidsLimit}},"cpu":{{.HostConfig.NanoCpus}},"mounts":{{json .Mounts}}}'
        result = self.command(self.docker + ["inspect", "--format", fmt, self.container])
        if result.returncode:
            raise RuntimeError("synthetic container inspect failed")
        value = json.loads(result.stdout)
        if value["configured_image"] != FIXED_PG or not 0 < value["memory"] <= 768 * 1024 * 1024 or not 0 < value["pids"] <= 256:
            raise ValueError("fixed PG and bounded resources required")
        if not 0 < value["cpu"] <= 1_000_000_000 or not value["memory"] <= value["memory_swap"] <= 768 * 1024 * 1024:
            raise ValueError("bounded CPU and memory-plus-swap required")
        if not value["state"].get("Running") or value["state"].get("OOMKilled") or any(item["Type"] == "bind" for item in value["mounts"]):
            raise ValueError("healthy isolated database with no host bind mounts required")
        self.save(name, value)
        return value

    def cgroup(self, name):
        records = {}
        for metric in ("memory.events", "memory.peak"):
            result = self.command(self.docker + ["exec", self.container, "cat", "/sys/fs/cgroup/" + metric])
            records[metric] = {"available": result.returncode == 0, "raw": result.stdout if result.returncode == 0 else None}
        self.save(name, records)
        return records

    def install(self):
        path = self.source / "internal/database/webhook_outbox.go"
        raw = path.read_bytes()
        if sha(raw) != MIGRATION_SHA:
            raise ValueError("actual fixed sender migration SHA mismatch")
        matches = re.findall(r"tx\.Exec\(`(.*?)`\)", raw.decode(), re.S)
        if len(matches) != 1:
            raise ValueError("one locked actual migration required")
        self.sql(f"CREATE SCHEMA {self.names['SOURCE']}; SET search_path={self.names['SOURCE']},pg_catalog;" + matches[0])
        self.sql(render((Path(__file__).parent / "install.sql").read_text(), self.names))
        for role, login in self.logins.items():
            self.sql(f"CREATE ROLE {login} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;")
        grants = {"review": ["REVIEWER"], "execute": ["EXECUTOR"], "audit": ["AUDITOR"], "dual": ["REVIEWER", "EXECUTOR"]}
        for login, roles in grants.items():
            self.sql("".join(f"GRANT {self.names[role]} TO {self.logins[login]};" for role in roles))
        self.sql(f"INSERT INTO {self.outbox}(delivery_id,body_sha256,user_id,feed_id,entry_id,target_url,signature,contract,body,state,attempts) VALUES('{self.delivery}','{self.delivery}',1,1,1,'http://synthetic.invalid/webhook',repeat('a',64),'synthetic-contract',convert_to('synthetic held message','UTF8'),'held',3);")
        self.refresh_marker()

    def refresh_marker(self):
        database_identity = self.sql(f"SELECT encode(sha256(convert_to(jsonb_build_object('system_identifier',(SELECT system_identifier::text FROM pg_control_system()),'database',current_database(),'postmaster_start',pg_postmaster_start_time(),'outbox_oid','{self.outbox}'::regclass::oid)::text,'UTF8')),'hex');")
        manifest = {"schema": 1, "mode": "isolation", "run_id": self.s, "database_identity": database_identity,
                    "producer_inventory": [], "sender_inventory": [], "adapter_identity": "held-access-synthetic-pg-v1"}
        manifest_hash = sha(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode())
        if not (self.output / "FIXTURE_MANIFEST.json").exists():
            self.save("FIXTURE_MANIFEST.json", {"manifest": manifest, "manifest_sha256": manifest_hash,
                                              "scope": "no producer or sender started in owned fixture; not a production stop attestation"})
        self.sql(f"INSERT INTO {self.s}.control_marker VALUES(true,'{database_identity}','{self.s}','{manifest_hash}','held-access-synthetic-pg-v1',clock_timestamp(),true,true,true,'') ON CONFLICT(singleton) DO UPDATE SET observed_at=EXCLUDED.observed_at;")

    def plan(self, operation, action="retry_once"):
        return json.loads(self.sql(f"SELECT {self.s}.make_plan('{self.delivery}',{literal(action)},'{operation}');", "review"))

    def proof(self, operation):
        return json.loads(self.sql(f"SELECT jsonb_build_object('schema',1,'mode','isolation','operation_id','{operation}','database_identity',database_identity,'run_id',run_id,'manifest_sha256',manifest_sha256,'adapter_identity',adapter_identity,'observed_at',observed_at,'producers_closed',true,'sender_stopped',true,'no_active_sender_leases',true,'stopped_operation_id',stopped_operation_id) FROM {self.s}.control_marker;"))

    def approve(self, plan, proof, actor="review", expect_ok=True):
        text = f"SELECT {self.s}.approve({literal(json.dumps(plan))}::jsonb,{literal(json.dumps(proof))}::jsonb,'{self.note}');"
        result = self.sql(text, actor, expect_ok)
        return json.loads(result) if expect_ok else None

    def reserve_sql(self, approval, note=None):
        return f"SELECT {self.s}.reserve('{approval['operation_id']}','{approval['plan_sha256']}','{approval['proof_sha256']}',{literal(note or self.note)});"

    def record(self, name):
        self.cases.append({"case": name, "status": "passed_actual_pg"})

    def permissions(self):
        for actor in ("review", "execute", "audit", "none"):
            for statement in (f"SELECT body,signature FROM {self.outbox}", f"UPDATE {self.outbox} SET attempts=0", f"UPDATE {self.outbox} SET target_url='changed'", f"DELETE FROM {self.outbox}", f"ALTER TABLE {self.outbox} DISABLE TRIGGER ALL", f"DELETE FROM {self.s}.audit", f"UPDATE {self.s}.audit SET phase='reserved'", f"TRUNCATE {self.s}.audit", f"INSERT INTO {self.s}.audit VALUES('x','approved',session_user,'x','x',now())", f"SELECT {self.s}.snapshot('{self.delivery}')"):
                self.sql(statement + ";", actor, False)
        self.sql(f"SELECT count(*) FROM {self.s}.audit;", "audit")
        self.sql(f"SELECT {self.s}.make_plan('{self.delivery}','retry_once',repeat('1',32));", "execute", False)
        self.sql(f"SELECT {self.s}.make_plan('{self.delivery}','retry_once',repeat('1',32));", "dual", False)
        self.sql(f"SET ROLE {self.names['REVIEWER']}; SELECT {self.s}.make_plan('{self.delivery}','retry_once',repeat('1',32));", "dual", False)
        self.sql(f"SELECT {self.s}.make_plan(repeat('f',64),'retry_once',repeat('1',32));", "review", False)
        self.sql(f"SELECT {self.s}.make_plan('{self.delivery}',NULL,repeat('1',32));", "review", False)
        self.record("roles_no_raw_write_delete_alter_or_public_internal_execute; session_user_not_current_role")

    def binding(self):
        operation = "1" * 32
        plan, proof = self.plan(operation), self.proof(operation)
        for key in ("body_sha256", "snapshot_sha256", "endpoint_sha256", "contract", "created_at"):
            wrong = {**plan, key: "wrong"}
            self.approve(wrong, proof, expect_ok=False)
        for key, value in (("mode", "production"), ("database_identity", "d" * 64), ("producers_closed", False), ("operation_id", "2" * 32)):
            self.approve(plan, {**proof, key: value}, expect_ok=False)
        self.approve(plan, proof, "execute", False)
        approval = self.approve(plan, proof)
        self.sql(self.reserve_sql(approval), "review", False)
        self.sql(self.reserve_sql({**approval, "plan_sha256": "0" * 64}), "execute", False)
        self.sql(self.reserve_sql(approval, "0" * 64), "execute", False)
        self.sql(self.reserve_sql(approval).replace(literal(self.note), "NULL"), "execute", False)
        self.sql(f"UPDATE {self.outbox} SET attempts=4;")
        self.sql(self.reserve_sql(approval), "execute", False)
        self.sql(f"UPDATE {self.outbox} SET attempts=3;")
        self.record("unknown_row_wrong_identity_proof_approval_and_snapshot_drift_rejected")
        return approval

    def concurrent(self, approval):
        sql = self.reserve_sql(approval)
        def call(_):
            result = self.command(self.docker + ["exec", "-i", self.container, "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-U", self.logins["execute"], "-d", "synthetic_outbox"], "SET statement_timeout='3s'; SET lock_timeout='3s';" + sql)
            return result.returncode == 0
        with ThreadPoolExecutor(max_workers=6) as pool:
            values = list(pool.map(call, range(6)))
        if values.count(True) != 1:
            raise AssertionError("concurrent exact approval must reserve once")
        count = self.sql(f"SELECT count(*) FROM {self.s}.reservations;")
        if count != "1":
            raise AssertionError("duplicate reservation")
        self.sql(sql, "execute", False)
        self.record("six_independent_pg_sessions_exactly_one_reservation_duplicate_rejected")

    def abort_and_budget(self):
        self.refresh_marker()
        operation = "2" * 32
        approval = self.approve(self.plan(operation), self.proof(operation))
        self.sql("BEGIN;" + self.reserve_sql(approval) + "SELECT 1/0;COMMIT;", "execute", False)
        if self.sql(f"SELECT count(*) FROM {self.s}.reservations WHERE operation_id='{operation}';") != "0":
            raise AssertionError("aborted reservation survived")
        self.sql(self.reserve_sql(approval), "execute")
        self.sql(f"UPDATE {self.outbox} SET attempts=20;")
        self.approve(self.plan("3" * 32), self.proof("3" * 32), expect_ok=False)
        receipt = self.approve(self.plan("3" * 32, "confirm_receipt"), self.proof("3" * 32))
        self.sql(self.reserve_sql(receipt), "execute")
        if self.sql(f"SELECT attempts||':'||state FROM {self.outbox};") != "20:held":
            raise AssertionError("access layer mutated sender state")
        self.sql(f"UPDATE {self.outbox} SET attempts=3;")
        self.record("transaction_abort_rolls_back_reservation_and_audit; exhaustion_only_receipt_authorization")

    def age_boundary(self):
        self.sql(f"UPDATE {self.s}.control_marker SET observed_at=clock_timestamp()-interval '61 seconds';")
        self.approve(self.plan("5" * 32), self.proof("5" * 32), expect_ok=False)
        self.refresh_marker()
        self.sql(f"UPDATE {self.outbox} SET lease_token='manual:'||repeat('a',32);")
        self.approve(self.plan("5" * 32), self.proof("5" * 32), expect_ok=False)
        self.sql(f"UPDATE {self.outbox} SET lease_token=NULL;")
        original = self.delivery
        self.delivery = sha(b"synthetic seven-day held")
        self.sql(f"INSERT INTO {self.outbox}(delivery_id,body_sha256,user_id,feed_id,entry_id,target_url,signature,contract,body,state,attempts,created_at) VALUES('{self.delivery}','{self.delivery}',1,1,99,'http://synthetic.invalid/webhook',repeat('a',64),'synthetic-contract',convert_to('synthetic seven-day held','UTF8'),'held',1,now()-interval '7 days');")
        self.approve(self.plan("6" * 32), self.proof("6" * 32), expect_ok=False)
        approval = self.approve(self.plan("6" * 32, "confirm_receipt"), self.proof("6" * 32))
        self.sql(self.reserve_sql(approval), "execute")
        self.delivery = original
        self.record("seven_days_rejects_retry_without_reset_receipt_intent_only; stale_proof_manual_lease_rejected")

    def capacity(self):
        # Admin-only synthetic prefill: not a history of approvals/requests.
        self.sql(f"INSERT INTO {self.s}.approvals SELECT md5('prefill'||i),'{self.delivery}','{{}}','{{}}',repeat('a',64),repeat('b',64),'synthetic-prefill',repeat('c',64),now(),8192,2 FROM generate_series(1,2048-(SELECT count(*) FROM {self.s}.approvals))i;")
        before = self.sql(f"SELECT count(*)||':'||(SELECT count(*) FROM {self.s}.audit) FROM {self.s}.approvals;")
        self.approve(self.plan("4" * 32), self.proof("4" * 32), expect_ok=False)
        after = self.sql(f"SELECT count(*)||':'||(SELECT count(*) FROM {self.s}.audit) FROM {self.s}.approvals;")
        if before != after:
            raise AssertionError("capacity rejection left evidence partial")
        self.record("audit_reservation_16mib_cap_before_admission_no_partial_write")

    def capacity_observer_sql(self):
        path = Path(__file__).resolve().parents[3] / "scripts/observe_handoff_capacity.py"
        spec = importlib.util.spec_from_file_location("held_access_capacity_observer", path)
        observer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(observer)
        # Admin-only synthetic fixture, after all held authorization scenarios.
        for index, state in enumerate(("pending", "inflight", "acked", "held"), 2):
            body = "synthetic capacity " + str(index)
            delivery = sha(body.encode())
            age = "8 days" if state in {"acked", "held"} else "1 minute"
            ack = "now()-interval '7 days'" if state == "acked" else "NULL"
            self.sql(f"INSERT INTO {self.outbox}(delivery_id,body_sha256,user_id,feed_id,entry_id,target_url,signature,contract,body,state,attempts,created_at,acked_at) VALUES('{delivery}','{delivery}',1,1,{index},'http://synthetic.invalid/webhook',repeat('a',64),'synthetic-contract',convert_to('{body}','UTF8'),'{state}',2,now()-interval '{age}',{ack});")
        query = (
            "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SET LOCAL statement_timeout='3s'; SET LOCAL lock_timeout='3s';"
            f"SET LOCAL search_path={self.names['SOURCE']},pg_catalog;"
            "SELECT json_build_object('read_only',current_setting('transaction_read_only'),'isolation',current_setting('transaction_isolation'));"
            f"WITH i(database_name,schema_name,system_id,postmaster_start,table_oid) AS ({observer.PG_OUTBOX_IDENTITY_SQL}) SELECT row_to_json(i) FROM i;"
            f"WITH s(state,n,charged,attempts,missing,old_ack,old_unacked) AS ({observer.PG_OUTBOX_SAMPLE_SQL}) SELECT coalesce(json_agg(json_build_array(state,n,charged,attempts,missing,old_ack,old_unacked)),'[]') FROM s;"
            "COMMIT;"
        )
        lines = self.sql(query).splitlines()
        mode, identity, rows = map(json.loads, lines)
        counts = observer._outbox_counts(rows)
        if mode != {"read_only": "on", "isolation": "repeatable read"} or identity["database_name"] != "synthetic_outbox" or identity["schema_name"] != self.names["SOURCE"]:
            raise AssertionError("actual capacity SQL transaction or identity mismatch")
        expected_bytes = sum(len(body.encode()) + len("http://synthetic.invalid/webhook") + len("synthetic-contract") + 1024 for body in ["synthetic held message", "synthetic seven-day held", *["synthetic capacity " + str(i) for i in range(2, 6)]])
        if counts != {"rows": 6, "charged_bytes": expected_bytes, "states": {"pending": 1, "inflight": 1, "held": 3, "acked": 1}, "max_attempts": 3, "expired_unacknowledged": 2, "acked_at_least_7_days": 1, "cleanup_credit": 0}:
            raise AssertionError("actual SQL aggregate/counting mismatch")
        self.save("CAPACITY_SQL.json", {"status": "passed_actual_sql_psql", "coverage": "outbox_only_0001_not_a_drain_proof", "source_sha256": sha(path.read_bytes()), "transaction": mode, "identity": identity, "rows": rows, "counts": counts, "db_api_transport_tested": False})
        self.record("actual_capacity_observer_sql_repeatable_read_read_only_identity_and_charged_bytes")

    def run(self):
        self.output.mkdir(mode=0o700, parents=True, exist_ok=False)
        started = time.monotonic()
        self.save("PLAN.json", {"names": self.names, "logins": self.logins, "production": False, "models": 0, "authorization_only_no_forwarding": True, "migration_sha256": MIGRATION_SHA, "installer_sha256": sha((Path(__file__).parent / "install.sql").read_bytes())})
        try:
            identity = self.inspect("BEFORE.json")
            before_cgroup = self.cgroup("CGROUP_BEFORE.json")
            self.install()
            self.permissions()
            approval = self.binding()
            self.concurrent(approval)
            self.abort_and_budget()
            self.age_boundary()
            self.capacity()
            self.capacity_observer_sql()
            self.inspect("AFTER.json")
            after_cgroup = self.cgroup("CGROUP_AFTER.json")
            for key in ("oom", "oom_kill", "oom_group_kill"):
                def events(record):
                    raw = record["memory.events"]["raw"] or ""
                    return {line.split()[0]: int(line.split()[1]) for line in raw.splitlines()}
                if events(after_cgroup).get(key, 0) > events(before_cgroup).get(key, 0):
                    raise AssertionError("cgroup OOM event during permission validation")
            dump = self.command(self.docker + ["exec", self.container, "pg_dump", "-U", "postgres", "-d", "synthetic_outbox", "--schema=" + self.s, "--schema=" + self.names["SOURCE"]])
            if dump.returncode:
                raise RuntimeError("synthetic evidence export failed")
            self.save("SYNTHETIC_DUMP.json", {"sha256": sha(dump.stdout.encode()), "sql": dump.stdout})
            result = {"status": "passed_actual_pg", "cases": self.cases, "elapsed_seconds": time.monotonic() - started, "container_id": identity["id"], "actual_resources": {key: identity[key] for key in ("memory", "memory_swap", "pids", "cpu")}, "cgroup_metrics_available": all(value["available"] for value in after_cgroup.values()), "oom_scope": "container flag plus available cgroup events; not host-wide OOM proof", "production": False, "http": 0, "models": 0, "outbox_application_behavior_changed": False, "production_adapter": False}
            self.save("RESULT.json", result)
            return result
        except Exception as error:
            self.save("FAILURE.json", {"type": type(error).__name__, "reason": str(error), "completed_cases": self.cases, "production": False})
            raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--docker-host")
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if not args.run:
        print(json.dumps({"run": False, "fixed_pg": FIXED_PG, "production": False}))
        return
    runner = Runner(args.container, args.source, args.output, args.docker, args.docker_host)
    print(json.dumps(runner.run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
