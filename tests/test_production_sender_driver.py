"""Protocol fixtures only; real fixed-image/PG evidence is a separate native gate."""

import copy
import hashlib
from pathlib import Path
import sys
import time

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import production_sender_driver as sender  # noqa: E402
from miniflux_handoff_adapter import (  # noqa: E402
    StopNewPending,
    PausePending,
    source_sha256,
)
from sender_drain_inventory import PostgresReader  # noqa: E402
from durable_handoff_cutover import CutoverError, read_private  # noqa: E402
from test_sender_drain_contract import synthetic_manifest  # noqa: E402


class Transport:
    def __init__(self, binding, rows):
        self.binding, self.rows = binding, rows
        self.current = binding["initial_operation_id"]
        self.generation = binding["generation"]
        self.state, self.action = "open", "initialize"
        self.active = self.uncertain = self.producers = self.producer_unknown = 0
        self.calls = []
        self.lost = False

    def inspect(self, _):
        return copy.deepcopy(self.rows)

    def receipt(self):
        confirmed = self.state == "open" or (
            not (self.producers or self.producer_unknown)
            if self.action == "stop_new"
            else not (self.active or self.uncertain)
        )
        if confirmed and self.action == "pause":
            self.state = "closed"
        value = {
            "schema": 1,
            "manifest": self.binding["manifest"],
            "operation_id": self.current,
            "action": self.action,
            "previous_generation": self.generation - 1,
            "generation": self.generation,
            "state": self.state,
            "durable": True,
            "confirmed": confirmed,
            "active_leases": self.active,
            "uncertain_leases": self.uncertain,
        }
        if self.action == "stop_new":
            value.update(
                active_producer_leases=self.producers,
                uncertain_producer_leases=self.producer_unknown,
            )
        return value

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path == "/registration":
            return 200, {
                "schema": 1,
                "manifest": self.binding["manifest"],
                "protocol": "miniflux-pg-handoff-v1",
                "business_open_not_assumed": True,
            }
        if method == "POST":
            if path == "/stop-new":
                assert self.state == "open"
                self.state, self.action = "draining", "stop_new"
            elif path == "/pause":
                assert self.state == "draining"
                self.state, self.action = "closing", "pause"
            else:
                raise AssertionError(path)
            self.current, self.generation = payload["operation_id"], self.generation + 1
            if self.lost:
                raise TimeoutError("synthetic lost ACK after commit")
        elif path != "/operations/" + self.current:
            return 409, {"confirmed": False, "result": {}}
        value = self.receipt()
        return (200 if value["confirmed"] else 409), {
            "confirmed": value["confirmed"],
            "result": value,
        }


class Reader(PostgresReader):
    def __init__(self, identity, sample, transport):
        self.expected, self.sample, self.transport = identity, sample, transport

    def snapshot(self):
        t = self.transport
        return {
            **copy.deepcopy(self.sample),
            "gate": {
                "manifest": t.binding["manifest"],
                "generation": t.generation,
                "phase": t.state,
                "current_operation": t.current,
            },
            "retained_leases": {
                "records": 4,
                "active": t.active,
                "uncertain": t.uncertain,
                "active_producers": t.producers,
                "uncertain_producers": t.producer_unknown,
            },
            "retained_operations": 3,
        }


@pytest.fixture
def setup(tmp_path):
    manifest, _ = synthetic_manifest(tmp_path)
    binding = manifest["sender_binding"]
    binding["source_sha256"] = source_sha256()
    binding["application_source_sha256"] = hashlib.sha256(
        (SCRIPTS.parent / "ops/miniflux/pause-source-lock.json").read_bytes()
    ).hexdigest()
    c = binding["component"]
    rows = [
        {
            "Id": c["id"],
            "Name": "/sender",
            "Image": c["image"],
            "RestartCount": 0,
            "Config": {
                "Env": ["HANDOFF_ENABLED=1", "HANDOFF_MANIFEST=" + binding["manifest"]],
                "User": "",
                "Labels": {
                    "org.opencontainers.image.revision": c["revision"],
                    "io.ai-filter.handoff.role": "miniflux",
                },
            },
            "State": {
                "Running": True,
                "OOMKilled": False,
                "Paused": False,
                "Restarting": False,
                "StartedAt": c["started_at"],
            },
            "HostConfig": {"Memory": c["memory"], "PidsLimit": c["pids"]},
            "Mounts": c["mounts"],
        }
    ]
    transport = Transport(binding, rows)
    reader = Reader(
        manifest["postgres_identity"], manifest["inventory_summary"], transport
    )
    plan = {
        "schema": 1,
        "scope": "instrumented_sender_only",
        "binding": binding,
        "postgres_identity": reader.expected,
        "window": manifest["window"],
        "sources": sender.sources(),
    }
    driver = sender.SenderDriver(plan, transport, reader, tmp_path / "driver")
    return driver, transport, reader


def clear(reader):
    reader.sample["states"]["pending"] = 0
    reader.sample["states"]["acked"] = 1


def posts(t):
    return [(method, path) for method, path, _ in t.calls if method == "POST"]


def test_producer_stop_keeps_consumers_then_independent_inventory_and_full_pause(setup):
    d, t, r = setup
    t.active = 2
    receipt = d.stop_new("b" * 64, "synthetic staged stop")
    assert receipt["state"] == "draining" and receipt["active_leases"] == 2
    assert d.inventory("b" * 64)["blockers"] == ["outbox_pending"]
    with pytest.raises(CutoverError):
        d.freeze("c" * 64, "b" * 64, "not drained")
    assert posts(t) == [("POST", "/stop-new")]
    clear(r)
    with pytest.raises(PausePending):
        d.freeze("c" * 64, "b" * 64, "after independent inventory")
    original = (d.control.root / "pause-response.json").read_bytes()
    t.active = 0
    result = d.query_freeze("c" * 64)
    assert (
        result["receipt"]["state"] == "closed"
        and not result["inventory"]["blockers"]
        and result["switch_authorized"] is False
    )
    assert (d.control.root / "pause-response.json").read_bytes() == original
    assert posts(t) == [("POST", "/stop-new"), ("POST", "/pause")]


@pytest.mark.parametrize("stage", ["stop", "pause"])
def test_lost_ack_restart_only_queries_exact_operation(setup, stage):
    d, t, r = setup
    if stage == "pause":
        d.stop_new("b" * 64, "stop")
        clear(r)
    t.lost = True
    with pytest.raises(TimeoutError):
        d.stop_new("b" * 64, "stop") if stage == "stop" else d.freeze(
            "c" * 64, "b" * 64, "freeze"
        )
    restored = sender.SenderDriver(d.plan, t, r, d.root)
    value = (
        restored.query_stop_new("b" * 64)
        if stage == "stop"
        else restored.query_freeze("c" * 64)["receipt"]
    )
    assert value["confirmed"] is True
    with pytest.raises(CutoverError):
        restored.stop_new("b" * 64, "stop") if stage == "stop" else restored.freeze(
            "c" * 64, "b" * 64, "freeze"
        )
    assert len(posts(t)) == (1 if stage == "stop" else 2)


def test_unknown_producers_never_seal_or_freeze(setup):
    d, t, r = setup
    t.producer_unknown = t.uncertain = 1
    with pytest.raises(StopNewPending):
        d.stop_new("b" * 64, "stop")
    with pytest.raises(StopNewPending):
        d.query_stop_new("b" * 64)
    with pytest.raises(CutoverError):
        d.freeze("c" * 64, "b" * 64, "bad freeze")
    assert len(posts(t)) == 1
    assert not (d.control.root / "stop_new.confirmed.json").exists()


def test_held_originals_block_but_explicit_abort_preserves_them(setup):
    d, t, r = setup
    d.stop_new("b" * 64, "stop")
    r.sample["states"].update(pending=0, held=1)
    with pytest.raises(CutoverError):
        d.freeze("c" * 64, "b" * 64, "held is not drained")
    value = d.abort_window("c" * 64, "b" * 64, "retained capacity abort")
    assert value["window_aborted"] and value["queue_checked"] is False
    assert r.sample["states"]["held"] == 1
    with pytest.raises(CutoverError):
        d.query_freeze("c" * 64)


def test_changed_identity_or_expired_window_blocks_before_mutation(setup):
    d, t, r = setup
    t.rows[0]["Image"] = "sha256:" + "f" * 64
    with pytest.raises(CutoverError):
        d.stop_new("b" * 64, "wrong image")
    assert posts(t) == []
    t.rows[0]["Image"] = d.plan["binding"]["component"]["image"]
    d.deadline = time.monotonic() - 1
    with pytest.raises(CutoverError):
        d.stop_new("b" * 64, "expired")
    assert posts(t) == []


def test_staged_interface_cannot_call_legacy_full_stop_or_resume(setup):
    d, _, _ = setup
    for name in ("stop", "resume", "query_resume"):
        with pytest.raises(CutoverError):
            getattr(d.control, name)(None)
    assert read_private(d.root / "plan.json") == d.plan
