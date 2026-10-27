"""Synthetic protocol/ownership tests; these are not actual PG or receiver proof."""
import copy
import sys
import time
import types
from unittest.mock import patch

import pytest

import test_miniflux_handoff_adapter as legacy_tests
from test_miniflux_handoff_adapter import ProtocolClient
import miniflux_handoff_adapter as bridge
from durable_handoff_cutover import CutoverError, read_private


class ActivationClient(ProtocolClient):
    def __init__(self, binding):
        super().__init__(binding)
        self.state, self.action = "missing", ""
        self.schema = 135

    def request(self, method, path, payload=None):
        if path == "/activation-registration":
            self.calls.append((method, path, payload))
            return 200, {"schema":1,"manifest":self.binding["manifest"],"protocol":"miniflux-pg-activation-v1",
                         "database_schema":self.schema,"capabilities":["initialize_closed","production_resume"],
                         "business_open_not_assumed":True}
        if method == "POST":
            self.calls.append((method, path, copy.deepcopy(payload)))
            if path == "/initialize-closed":
                assert self.state == "missing"
                self.action, self.state, self.generation = "initialize_closed", "closed", 0
            elif path == "/production-resume":
                assert self.state == "closed" and payload["closed_operation_id"] == self.current
                self.action, self.state, self.generation = "resume", "open", self.generation + 1
            else:
                raise AssertionError(path)
            self.current = payload["operation_id"]
            if self.lost:
                raise TimeoutError("synthetic lost ACK after committed CAS")
            return 200, {"confirmed":True,"result":self.receipt()}
        return super().request(method,path,payload)


@pytest.fixture
def activation(tmp_path, monkeypatch):
    base = legacy_tests.MinifluxBridgeTests()
    base.setUp()
    binding = copy.deepcopy(base.binding)
    binding["scope"] = "instrumented_sender_only"
    client = ActivationClient(binding)
    window = {"not_before":int(time.time())-5,"expires_at":int(time.time())+300}
    control = bridge.MinifluxActivationControl(binding,client,lambda:copy.deepcopy(base.rows),tmp_path/"control",window)
    class SyntheticSession:
        def __init__(self):
            self.value = {"operation_id":"a"*64,"descriptor_sha256":"b"*64,"manifest_sha256":"c"*64,
                          "binding_sha256":"d"*64,"generation":2,"slots_sha256":"e"*64,"window":window}
            self.calls = []
            self.fail = False
        def check_sender_resume(self, *, active=True):
            self.calls.append(active)
            if self.fail:
                raise CutoverError("synthetic actual receiver lost readiness")
            return copy.deepcopy(self.value)
    # Strict type checks are exercised using a module-local synthetic stand-in;
    # native joint tests provide the real production class and live controls.
    monkeypatch.setitem(sys.modules,"services.production_activation_control",
                        types.SimpleNamespace(ReceiverActivationSession=SyntheticSession))
    return control,client,SyntheticSession(),base


def posts(client):
    return [row for row in client.calls if row[0] == "POST"]


def test_first_closed_then_proof_bound_resume_and_exact_recovery(activation):
    c,t,cap,_=activation
    op=c.binding["initial_operation_id"]
    closed=c.initialize_closed(op)
    assert closed["state"] == "closed" and closed["generation"] == 0
    assert c.query_initialize_closed(op) == closed
    value=c.production_resume(op,0,cap)
    intent=read_private(c.root/"production_resume.intent.json")
    assert intent["receiver_activation_proof"] == cap.value
    assert value["operation_id"] == intent["operation_id"]
    assert c.query_production_resume(value["operation_id"],cap) == value
    assert [p[1] for p in posts(t)] == ["/initialize-closed","/production-resume"]
    assert cap.calls[-1] is False
    assert value["historical_coverage"] == "unknown_before_T0_no_replay"


@pytest.mark.parametrize("action", ["initialize_closed", "production_resume"])
def test_lost_ack_queries_same_original_after_window_expiry(activation, action):
    c,t,cap,_=activation
    op=c.binding["initial_operation_id"]
    if action == "production_resume":
        c.initialize_closed(op)
    t.lost=True
    with pytest.raises(TimeoutError):
        c.initialize_closed(op) if action == "initialize_closed" else c.production_resume(op,0,cap)
    intent=read_private(c.root/(action+".intent.json"))
    c.deadline=0
    count=len(posts(t))
    result=(c.query_initialize_closed(op) if action == "initialize_closed"
            else c.query_production_resume(intent["operation_id"],cap))
    assert result and len(posts(t)) == count
    with pytest.raises(CutoverError):
        c.initialize_closed(op) if action == "initialize_closed" else c.production_resume(op,0,cap)
    assert len(posts(t)) == count


def test_old135_missing_capability_never_initializes(activation):
    c,t,_,_=activation
    t.schema=134
    with pytest.raises(CutoverError,match="schema135"):
        c.initialize_closed(c.binding["initial_operation_id"])
    assert not posts(t)


@pytest.mark.parametrize("bad", [True, {}, {"ready":True}, object()])
def test_plain_readiness_is_not_receiver_capability(activation,bad):
    c,t,_,_=activation
    op=c.binding["initial_operation_id"]
    c.initialize_closed(op)
    with pytest.raises(CutoverError,match="ReceiverActivationSession"):
        c.production_resume(op,0,bad)
    assert len(posts(t)) == 1


def test_proof_change_after_post_retains_outcome_and_blocks_confirmation(activation):
    c,t,cap,_=activation
    op=c.binding["initial_operation_id"]
    c.initialize_closed(op)
    original=t.request
    def change(method,path,payload=None):
        result=original(method,path,payload)
        if path == "/production-resume":
            cap.value["slots_sha256"]="f"*64
        return result
    t.request=change
    with pytest.raises(CutoverError,match="receiver changed"):
        c.production_resume(op,0,cap)
    assert (c.root/"production_resume-response.json").is_file()
    assert not (c.root/"production_resume.confirmed.json").exists()
    assert len(posts(t)) == 2


def test_resume_intent_write_failure_prevents_post(activation):
    c,t,cap,_=activation
    op=c.binding["initial_operation_id"]
    c.initialize_closed(op)
    write=bridge.durable_write
    def fail(path,value):
        if path.name == "production_resume.intent.json":
            raise OSError("synthetic fsync failure")
        return write(path,value)
    with patch.object(bridge,"durable_write",fail),pytest.raises(OSError):
        c.production_resume(op,0,cap)
    assert len(posts(t)) == 1


def test_query_budget_never_refills_or_posts(activation):
    c,t,_,_=activation
    op=c.binding["initial_operation_id"]
    c.initialize_closed(op)
    for _ in range(32):
        c.query_initialize_closed(op)
    with pytest.raises(CutoverError,match="budget"):
        c.query_initialize_closed(op)
    assert len(posts(t)) == 1


def test_container_restart_refuses_new_operation(activation):
    c,t,_,base=activation
    base.rows[0]["RestartCount"]=1
    with pytest.raises(CutoverError):
        c.initialize_closed(c.binding["initial_operation_id"])
    assert not posts(t)


def test_timeout_queries_charge_original_budget_before_io_and_restart(activation):
    c,t,_,_=activation
    op=c.binding["initial_operation_id"]
    c.initialize_closed(op)
    original=t.request
    attempted=[]
    def timeout(method,path,payload=None):
        if path.startswith("/operations/"):
            attempted.append(path)
            raise TimeoutError("synthetic unknown query")
        return original(method,path,payload)
    t.request=timeout
    for _ in range(32):
        with pytest.raises(TimeoutError):
            c.query_initialize_closed(op)
    recovered=bridge.MinifluxActivationControl(c.binding,t,c.inspect_current,c.root,c.window)
    with pytest.raises(CutoverError,match="budget"):
        recovered.query_initialize_closed(op)
    assert len(attempted) == 32 and len(list(c.root.glob("attempt-*.json"))) == 32
    assert len(posts(t)) == 1
