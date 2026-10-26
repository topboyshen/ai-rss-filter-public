"""Real isolated PostgreSQL legacy permissions/shared capacity. Never forward.

Only fixed, already-patched source and an explicitly named bounded synthetic
container are accepted. All fixtures and command evidence are retained.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import re
import time


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HERE = Path(__file__).resolve().parent
base = load("legacy_audit_base", HERE / "verify.py")
observer = load("legacy_capacity", HERE.parents[2] / "scripts/observe_handoff_capacity.py")
inventory = load("legacy_independent_inventory", HERE.parents[2] / "scripts/independent_drain_inventory.py")
LEGACY_MIGRATION_SHA = "23a383bb5a462ed49a14d7339ebe12585d7ced1e65bd7ce4028931e011e996f2"


def require_cgroup(records):
    """Missing/invalid measurements are missing evidence, never zero OOM."""
    for metric in ("memory.events", "memory.peak"):
        item = records.get(metric)
        if not isinstance(item, dict) or item.get("available") is not True or not isinstance(item.get("raw"), str):
            raise AssertionError("required cgroup evidence unavailable: " + metric)
    peak = records["memory.peak"]["raw"].strip()
    if not re.fullmatch(r"[0-9]+", peak):
        raise AssertionError("invalid cgroup peak")
    events = {}
    for line in records["memory.events"]["raw"].splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[0] in events or not re.fullmatch(r"[0-9]+", fields[1]):
            raise AssertionError("invalid cgroup event counter")
        events[fields[0]] = int(fields[1])
    if not {"oom", "oom_kill"} <= events.keys():
        raise AssertionError("required cgroup OOM counters missing")
    if any(events.get(key, 0) for key in ("oom", "oom_kill", "oom_group_kill")):
        raise AssertionError("isolated container has OOM events")
    return {"memory_peak_bytes": int(peak), "memory_events": events}


def require_same_container(before, after):
    """A new process/container cannot erase OOM counters and still pass."""
    for key in ("id", "image", "configured_image", "memory", "memory_swap", "pids", "cpu", "mounts"):
        if key not in before or key not in after or before[key] != after[key]:
            raise AssertionError("synthetic container identity/configuration changed: " + key)
    for key in ("id", "image", "configured_image"):
        if not isinstance(before[key], str) or not before[key]:
            raise AssertionError("synthetic container identity missing: " + key)
    start = before.get("state", {}).get("StartedAt")
    if not isinstance(start, str) or not start or start.startswith("0001-") or after.get("state", {}).get("StartedAt") != start:
        raise AssertionError("synthetic container restarted or start identity missing")


def require_sql_rejection(record, expected):
    errors = re.findall(r"^ERROR:\s+([^\n]+)$", record.get("stderr", ""), re.M)
    if record.get("exit_code") != 3 or record.get("timed_out") is not False or errors != [expected]:
        raise AssertionError("negative SQL lacks the exact expected server rejection")


class Runner(base.Runner):
    def create_source(self, schema):
        if not base.IDENTIFIER.fullmatch(schema) or not schema.startswith(self.names["SOURCE"]):
            raise ValueError("owned isolated source required")
        statements = []
        for filename, expected in (("webhook_outbox.go", base.MIGRATION_SHA),
                                   ("legacy_handoff.go", LEGACY_MIGRATION_SHA)):
            raw = (self.source / "internal/database" / filename).read_bytes()
            if base.sha(raw) != expected:
                raise ValueError("fixed migration source mismatch: " + filename)
            match = re.findall(r"tx\.Exec\(`(.*?)`\)", raw.decode(), re.S)
            if len(match) != 1:
                raise ValueError("one exact original migration required")
            statements.append(match[0])
        self.sql(f"CREATE SCHEMA {schema}; SET search_path={schema},pg_catalog;" + "\n".join(statements))

    def install(self):
        self.create_source(self.names["SOURCE"])
        self.sql(base.render((HERE / "legacy_audit.sql").read_text(), self.names))
        for login in self.logins.values():
            self.sql(f"CREATE ROLE {login} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;")
        for login, roles in {"review": ["REVIEWER"], "execute": ["EXECUTOR"],
                             "audit": ["AUDITOR"], "dual": ["REVIEWER", "EXECUTOR"]}.items():
            self.sql("".join(f"GRANT {self.names[role]} TO {self.logins[login]};" for role in roles))
        source = self.names["SOURCE"]
        self.sql(f"INSERT INTO {source}.webhook_outbox(delivery_id,body_sha256,user_id,feed_id,entry_id,target_url,signature,contract,body,state,attempts) "
                 f"VALUES('{self.delivery}','{self.delivery}',1,1,1,'http://synthetic.invalid/webhook',repeat('a',64),'synthetic-contract',convert_to('synthetic held message','UTF8'),'held',3);")
        self.seed_legacy(source, "old legacy original", "8 days")
        self.seed_legacy(source, "recent legacy original", "1 minute")
        self.legacy_id = base.sha(b"old legacy original")
        self.record("fixed_0001_and_0002_migration_tables_installed_in_owned_synthetic_schema")

    def seed_legacy(self, schema, body, age):
        delivery = base.sha(body.encode())
        self.sql(f"INSERT INTO {schema}.webhook_legacy_originals(delivery_id,body_sha256,event_type,user_id,feed_id,entry_id,target_url,signature,contract,body,created_at) "
                 f"VALUES('{delivery}','{delivery}','save_entry',1,1,1,'http://synthetic.invalid/webhook',repeat('a',64),'synthetic-contract',convert_to({base.literal(body)},'UTF8'),now()-interval {base.literal(age)});")

    def snapshot(self, actor="review", identity=None):
        return json.loads(self.sql(f"SELECT {self.s}.legacy_snapshot({base.literal(identity or self.legacy_id)});", actor))

    def check_login(self, actor):
        actual = json.loads(self.sql("SELECT json_build_object('session_user',session_user,'current_user',current_user);", actor))
        if actual != {"session_user": self.logins[actor], "current_user": self.logins[actor]}:
            raise AssertionError("independent SQL login identity mismatch")
        return actual

    def expect_denied(self, query, actor, expected):
        self.sql(query, actor, False)
        name = f"COMMAND-{self.number:03d}.json"
        require_sql_rejection(json.loads((self.output / name).read_text()), expected)
        return {"actor": actor, "expected_server_rejection": expected, "command_evidence": name}

    def permission_cases(self):
        table = self.names["SOURCE"] + ".webhook_legacy_originals"
        logins = {actor: self.check_login(actor) for actor in self.logins}
        rejections = []
        for actor in ("review", "execute", "audit", "none", "dual"):
            for query in (f"SELECT body,target_url,signature FROM {table}",
                          f"UPDATE {table} SET state='held'", f"DELETE FROM {table}",
                          f"TRUNCATE {table}", f"ALTER TABLE {table} DISABLE TRIGGER ALL"):
                rejections.append(self.expect_denied(query + ";", actor, "permission denied for schema " + self.names["SOURCE"]))
            expected = "permission denied for schema " + self.s if actor == "none" else "permission denied for function assert_legacy_reader"
            rejections.append(self.expect_denied(f"SELECT {self.s}.assert_legacy_reader();", actor, expected))
        for actor in ("execute", "none", "dual"):
            for function, args in (("legacy_snapshot", "'" + self.legacy_id + "'"), ("shared_inventory", "")):
                expected = {"execute": "permission denied for function " + function, "none": "permission denied for schema " + self.s,
                            "dual": "isolated independent audit login required; no executor access"}[actor]
                rejections.append(self.expect_denied(f"SELECT {self.s}.{function}({args});", actor, expected))
        rejections.append(self.expect_denied(f"SET ROLE {self.names['REVIEWER']}; SELECT {self.s}.legacy_snapshot('{self.legacy_id}');", "dual",
                                             "isolated independent audit login required; no executor access"))
        roles = self.sql("SELECT json_agg(json_build_object('role',rolname,'superuser',rolsuper,'createdb',rolcreatedb,'createrole',rolcreaterole,'bypassrls',rolbypassrls)) FROM pg_roles WHERE rolname IN (" + ",".join(base.literal(x) for x in self.logins.values()) + ");")
        facts = json.loads(roles)
        if len(facts) != 5 or any(any(r[k] for k in ("superuser", "createdb", "createrole", "bypassrls")) for r in facts):
            raise AssertionError("synthetic login unexpectedly privileged")
        self.save("ROLES.json", {"actual_pg_roles": facts, "successful_login_identities": logins,
                                 "specific_server_rejections": rejections, "production_roles_checked": False})
        self.record("independent_session_roles_deny_raw_read_all_mutation_and_public_internal_execution")

    def identity_cases(self):
        before = self.snapshot()
        if before != self.snapshot("audit") or before["body_sha256"] != self.legacy_id:
            raise AssertionError("audited original identity mismatch")
        if any(k in before for k in ("body", "target_url", "signature")) or before["forwarding_allowed"]:
            raise AssertionError("private original or forwarding capability exposed")
        for delivery in ("f" * 64, "wrong", "a' OR true --"):
            self.sql(f"SELECT {self.s}.legacy_snapshot({base.literal(delivery)});", "review", False)
        self.sql(f"SELECT {self.s}.legacy_snapshot(NULL);", "review", False)
        table = self.names["SOURCE"] + ".webhook_legacy_originals"
        self.sql(f"UPDATE {table} SET target_url='changed';", expect_ok=False)
        self.sql(f"DELETE FROM {table};", expect_ok=False)
        # Aborted read transaction cannot consume, release, reset or change the original.
        self.sql(f"BEGIN; SELECT {self.s}.legacy_snapshot('{self.legacy_id}'); SELECT 1/0; COMMIT;", "review", False)
        if self.snapshot() != before:
            raise AssertionError("failed or aborted audit changed retained original")
        self.save("LEGACY_SNAPSHOT.json", before)
        self.record("exact_identity_unknown_row_immutable_original_and_transaction_abort")

    def concurrent_cases(self):
        before = self.snapshot()
        with ThreadPoolExecutor(max_workers=6) as pool:
            values = list(pool.map(lambda i: self.snapshot("review" if i % 2 else "audit"), range(6)))
        if values != [before] * 6:
            raise AssertionError("independent concurrent audits diverged")
        inventory = json.loads(self.sql(f"SELECT {self.s}.shared_inventory();", "audit"))
        if inventory["rows"] != 3 or inventory["legacy_rows"] != 2 or inventory["switch_authorized"]:
            raise AssertionError("held originals omitted from shared audit")
        self.save("AUDIT_SHARED.json", inventory)
        self.record("six_independent_read_sessions_no_delivery_or_state_transition")

    def actual_sample(self, schema):
        query = (
            "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SET LOCAL statement_timeout='3s'; SET LOCAL lock_timeout='3s';"
            f"SET LOCAL search_path={schema},pg_catalog;"
            "SELECT json_build_object('read_only',current_setting('transaction_read_only'),'isolation',current_setting('transaction_isolation'));"
            f"WITH i(database_name,schema_name,system_id,postmaster_start,table_oid,legacy_table_oid) AS ({observer.PG_IDENTITY_SQL}) SELECT row_to_json(i) FROM i;"
            f"WITH s(source,state,n,charged,attempts,missing,old_ack,old_unacked) AS ({observer.PG_SAMPLE_SQL}) SELECT coalesce(json_agg(json_build_array(source,state,n,charged,attempts,missing,old_ack,old_unacked)),'[]') FROM s;"
            "ROLLBACK;")
        mode, identity, rows = map(json.loads, self.sql(query).splitlines())
        if mode != {"read_only": "on", "isolation": "repeatable read"} or identity["schema_name"] != schema or identity["table_oid"] == identity["legacy_table_oid"]:
            raise AssertionError("actual two-table identity/transaction mismatch")
        return {"transaction": mode, "identity": identity, "rows": rows}

    def shared_cases(self):
        sample = self.actual_sample(self.names["SOURCE"])
        counts = observer._postgres_counts(sample["rows"])
        expected = sum(len(b.encode()) + len("http://synthetic.invalid/webhook") + len("synthetic-contract") + 1024
                       for b in ("synthetic held message", "old legacy original", "recent legacy original"))
        observer.validate_sender_sample(counts)
        if counts["rows"] != 3 or counts["charged_bytes"] != expected or counts["legacy"]["older_than_7_days"] != 1:
            raise AssertionError("shared sample dropped or discounted legacy")
        blockers = inventory.sender_blockers(counts)
        if blockers != ["sender_held_retained", "sender_legacy_held"]:
            raise AssertionError("actual held originals disappeared from independent drain")
        empty = self.names["SOURCE"] + "_empty"
        self.create_source(empty)
        zero = self.actual_sample(empty)
        zero["counts"] = observer._postgres_counts(zero["rows"])
        if zero["counts"]["rows"] != 0 or inventory.sender_blockers(zero["counts"]):
            raise AssertionError("actual empty two-table sample is not empty")
        self.save("SHARED_EMPTY.json", zero)
        self.save("SHARED_SQL.json", {**sample, "counts": counts, "independent_sender_blockers": blockers,
                                      "db_api_transport_tested": False,
                                      "file_locks_combined_with_pg_tested_here": False})
        missing = self.names["SOURCE"] + "_missing"
        self.sql(f"CREATE SCHEMA {missing}; CREATE TABLE {missing}.webhook_outbox (LIKE {self.outbox} INCLUDING ALL);")
        self.sql(f"SET search_path={missing},pg_catalog; {observer.PG_IDENTITY_SQL};", expect_ok=False)
        self.record("actual_read_only_rr_shared_capacity_and_missing_legacy_is_not_zero")

    def boundary_cases(self, suffix, bodies, size):
        schema = self.names["SOURCE"] + suffix
        self.create_source(schema)
        audit = {key: value + suffix for key, value in self.names.items()}
        self.sql(base.render((HERE / "legacy_audit.sql").read_text(), audit))
        self.sql(f"GRANT {audit['REVIEWER']} TO {self.logins['review']};")
        # Exact source-schema fixture materialization, not HTTP/admission history.
        body = f"repeat('x',{size}-8)||lpad(n::text,8,'0')"
        self.sql(f"INSERT INTO {schema}.webhook_outbox(delivery_id,body_sha256,user_id,feed_id,entry_id,target_url,signature,contract,body,state,attempts) "
                 f"SELECT encode(sha256(convert_to(b,'UTF8')),'hex'),encode(sha256(convert_to(b,'UTF8')),'hex'),1,1,n,'http://synthetic.invalid/webhook',repeat('a',64),'synthetic-contract',convert_to(b,'UTF8'),'held',0 FROM (SELECT n,{body} b FROM generate_series(1,{bodies}) n) fixture;")
        first = "legacy " + "y" * (size - 7)
        self.seed_legacy(schema, first, "8 days")
        below = self.actual_sample(schema)
        below["counts"] = observer._postgres_counts(below["rows"])
        below["security_definer_inventory"] = json.loads(self.sql(f"SELECT {audit['SCHEMA']}.shared_inventory();", "review"))
        for key in ("rows", "charged_bytes"):
            if below["security_definer_inventory"][key] != below["counts"][key]:
                raise AssertionError("actual audit function and observer disagree")
        self.save(suffix + "_BELOW.json", below)
        self.seed_legacy(schema, "legacy " + "z" * (size - 7), "8 days")
        exceeded = self.actual_sample(schema)
        try:
            observer._postgres_counts(exceeded["rows"])
        except observer.ObservationError as error:
            exceeded.update(rejected=True, reason=str(error))
        else:
            raise AssertionError("shared quota exceeded but observer accepted")
        self.sql(f"SELECT {audit['SCHEMA']}.shared_inventory();", "review", False)
        command_name = f"COMMAND-{self.number:03d}.json"
        command = json.loads((self.output / command_name).read_text())
        if "shared hard quota exceeded" not in command["stderr"]:
            raise AssertionError("audit function failed for an unexpected reason")
        exceeded["security_definer_inventory"] = {"rejected": True, "reason": "shared hard quota exceeded", "command_evidence": command_name}
        self.save(suffix + "_EXCEEDED.json", exceeded)
        self.record("actual_shared" + suffix + "_boundary_held_never_cleanup_credit")

    def export(self):
        # 256MiB logical byte fixture would expand to >512MiB in pg_dump bytea.
        # Keep all synthetic rows in PG; export their exact hashes/lengths, not
        # giant repeated bytes. Smaller permission/failure fixtures remain full.
        schema = self.names["SOURCE"] + "_bytes"
        exists = self.sql(f"SELECT to_regclass('{schema}.webhook_outbox') IS NOT NULL;") == "t"
        if exists:
            for table in ("webhook_outbox", "webhook_legacy_originals"):
                rows = self.sql(f"SELECT coalesce(json_agg(json_build_object('delivery_id',delivery_id,'body_sha256',body_sha256,'body_bytes',octet_length(body),'state',state,'charged_bytes',octet_length(body)+octet_length(target_url)+octet_length(contract)+1024)),'[]') FROM {schema}.{table};")
                self.save("BYTE_FIXTURE_" + table + ".json", json.loads(rows))
        dump = self.command(self.docker + ["exec", self.container, "pg_dump", "-U", "postgres", "-d", "synthetic_outbox",
                                           "--schema=" + self.s + "*", "--schema=" + self.names["SOURCE"] + "*",
                                           "--exclude-table-data=" + schema + ".webhook_outbox",
                                           "--exclude-table-data=" + schema + ".webhook_legacy_originals"])
        self.save("SYNTHETIC_DUMP.json", {"exit_code": dump.returncode, "sha256": base.sha(dump.stdout.encode()),
                                        "sql": dump.stdout, "large_byte_fixture_raw_not_exported": True,
                                        "large_byte_fixture_scope": "deterministically generated synthetic raw retained in PG, individual hashes/lengths exported; CI container is ephemeral"})
        if dump.returncode:
            raise RuntimeError("synthetic evidence export failed")

    def run(self):
        self.output.mkdir(mode=0o700, parents=True, exist_ok=False)
        started = time.monotonic()
        self.save("PLAN.json", {"names": self.names, "logins": self.logins, "production": False, "http": 0, "models": 0,
                               "outbox_migration_sha256": base.MIGRATION_SHA, "legacy_migration_sha256": LEGACY_MIGRATION_SHA,
                               "installer_sha256": base.sha((HERE / "legacy_audit.sql").read_bytes()),
                               "capacity_sha256": base.sha(Path(observer.__file__).read_bytes()),
                               "scope": "synthetic materialization; no sender, no receiver, no release or compensation"})
        try:
            identity = self.inspect("BEFORE.json")
            require_cgroup(self.cgroup("CGROUP_BEFORE.json"))
            self.install()
            self.permission_cases()
            self.identity_cases()
            self.concurrent_cases()
            self.shared_cases()
            self.boundary_cases("_rows", 4999, 32)
            self.boundary_cases("_bytes", 254, 1048576)
            cgroup = require_cgroup(self.cgroup("CGROUP_AFTER.json"))
            after = self.inspect("AFTER.json")
            require_same_container(identity, after)
            result = {"status": "passed_actual_pg", "cases": self.cases, "elapsed_seconds": time.monotonic() - started,
                      "actual_resources": {key: identity[key] for key in ("memory", "memory_swap", "pids", "cpu")}, "cgroup": cgroup,
                      "production": False, "models": 0, "http": 0, "forwarding_allowed": False,
                      "container_identity_unchanged": True,
                      "production_role_bindings_verified": False, "db_api_transport_tested": False}
        except Exception as error:
            self.save("FAILURE.json", {"type": type(error).__name__, "reason": str(error), "cases": self.cases})
            raise
        finally:
            try:
                self.export()
            except Exception as error:
                self.save("EXPORT_FAILURE.json", {"type": type(error).__name__, "reason": str(error)})
                raise
        self.save("RESULT.json", result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--docker-host")
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if not args.run:
        print(json.dumps({"run": False, "production": False, "http": 0, "models": 0, "forwarding_allowed": False}))
        return
    print(json.dumps(Runner(args.container, args.source, args.output, args.docker, args.docker_host).run()))


if __name__ == "__main__":
    main()
