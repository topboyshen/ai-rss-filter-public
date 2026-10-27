"""Synthetic-only schema-134 fixture from exact reviewed Miniflux SQL.

This seeds a new retained schema; it is NOT evidence that database.Migrate
upgrades 132/133 transactionally. That evidence belongs to the Go native tests.
No existing schema is altered, no schema is dropped, and no production DSN is
accepted. Callers retain the returned DSN privately; it is never printed here.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import secrets
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

from scripts import sender_drain_inventory as inventory
from services.sender_drain_contract import HEX64, require

LOCK = Path(__file__).resolve().parents[1] / "ops/miniflux/pause-source-lock.json"
DDL = (
    ("internal/database/webhook_outbox.go", r"tx.Exec\(`(.*?)`\)"),
    ("internal/handoff/handoff.go", r"const SchemaSQL = `(.*?)`"),
    ("internal/database/legacy_handoff.go", r"tx.Exec\(`(.*?)`\)"),
    ("internal/handoff/drain.go", r"const DrainSchemaSQL = `(.*?)`"),
)


def phase(value):
    print("sender_pg_phase=" + value, flush=True)


def source_sql(source, relative, pattern):
    source = Path(source)
    require(source.is_absolute(), "absolute fixed Miniflux source required")
    lock = json.loads(LOCK.read_text())
    raw = (source / relative).read_bytes()
    require(hashlib.sha256(raw).hexdigest() == lock["patched_files_sha256"][relative], "fixture source hash differs")
    found = re.findall(pattern, raw.decode(), re.S)
    require(len(found) == 1, "one exact source SQL block required")
    return found[0]


def fixture_dsn(raw, schema):
    url = urlsplit(raw)
    require(url.scheme in {"postgres", "postgresql"} and url.hostname in {"127.0.0.1", "localhost"}
            and url.path == "/synthetic_outbox" and not url.fragment,
            "only loopback synthetic_outbox fixture DSN allowed")
    query = parse_qs(url.query)
    require(set(query) <= {"sslmode", "connect_timeout"}
            and re.fullmatch(r"sender_fixture_[0-9a-f]{24}", schema), "unrecognized fixture connection override")
    query["options"] = ["-c search_path=" + schema]
    # libpq URI decoding does not treat form-encoded '+' as a space.
    return urlunsplit((url.scheme, url.netloc, url.path, urlencode(query, doseq=True, quote_via=quote), ""))


def create_fixture(raw_dsn, miniflux_source):
    """Return (live connection, private fixture identity); retain schema on exit."""
    schema = "sender_fixture_" + secrets.token_hex(12)
    dsn = fixture_dsn(raw_dsn, schema)
    statements = [source_sql(miniflux_source, name, pattern) for name, pattern in DDL]
    import psycopg
    phase("before_schema_create_" + schema)
    with psycopg.connect(raw_dsn, autocommit=True, connect_timeout=3) as admin:
        admin.execute("CREATE SCHEMA " + schema)
    connection = psycopg.connect(dsn, autocommit=True, connect_timeout=3)
    try:
        connection.execute("SET statement_timeout='3000ms'")
        for statement in statements:
            connection.execute(statement)
        connection.execute("CREATE TABLE schema_version(version text not null); INSERT INTO schema_version VALUES('134')")
        value = {"dsn": dsn, "schema": schema, "manifest": "5" * 64, "initial_operation_id": "8" * 64,
                 "stop_operation_id": "9" * 64, "generation": 3}
        connection.execute("INSERT INTO handoff_state VALUES(1,%s,3,'draining',%s)",
                           (value["manifest"], value["stop_operation_id"]))
        connection.execute("""INSERT INTO handoff_operations(operation_id,manifest,action,previous_generation,generation,prior_operation,status,confirmed_at)
 VALUES(%s,%s,'stop_new',2,3,%s,'confirmed',clock_timestamp())""",
                           (value["stop_operation_id"], value["manifest"], value["initial_operation_id"]))
        value["expected_identity"] = dict(zip(inventory.IDENTITY_KEYS, connection.execute(inventory.IDENTITY_SQL).fetchone()))
        phase("source_bound_fixture_ready_" + schema)
        return connection, value
    except BaseException:
        connection.close()
        raise


def close_fixture(connection, fixture):
    """Seed the confirmed pause of this synthetic gate; never remove held rows."""
    manifest = fixture["manifest"]
    require(HEX64.fullmatch(manifest), "fixture manifest required")
    actual = connection.execute("SELECT manifest,generation,phase,current_operation FROM handoff_state WHERE id=1").fetchone()
    require(actual == (manifest, fixture["generation"], "draining", fixture["stop_operation_id"]), "fixture gate differs")
    operation = "d" * 64
    with connection.transaction():
        connection.execute("UPDATE webhook_outbox SET state='acked',acked_at=clock_timestamp() WHERE state IN ('pending','inflight')")
        connection.execute("UPDATE handoff_state SET phase='closed',generation=generation+1,current_operation=%s", (operation,))
        connection.execute("""INSERT INTO handoff_operations(operation_id,manifest,action,previous_generation,generation,prior_operation,status,confirmed_at)
 VALUES(%s,%s,'pause',%s,%s,%s,'confirmed',clock_timestamp())""",
                           (operation, manifest, fixture["generation"], fixture["generation"] + 1, fixture["stop_operation_id"]))
    phase("synthetic_closed_marker_seeded_" + fixture["schema"])
    return operation
