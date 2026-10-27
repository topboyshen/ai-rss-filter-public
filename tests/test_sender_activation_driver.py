"""Synthetic fixed-reader orchestration; native execution is independently required."""
import copy

import pytest

from test_miniflux_activation_adapter import activation  # noqa: F401
from test_production_sender_driver import setup as sender_setup  # noqa: F401
import production_sender_driver as sender
from sender_drain_inventory import PostgresReader
from durable_handoff_cutover import CutoverError


class SyntheticReader(PostgresReader):
    def __init__(self, expected, sample, client):
        self.expected,self.sample,self.client=expected,sample,client
        self.leases,self.operations=0,0
    def initial_snapshot(self):
        value=copy.deepcopy(self.sample)
        t=self.client
        value.update(database_identity=self.expected,
                     gate=None if t.state == "missing" else {"manifest":t.binding["manifest"],"generation":t.generation,
                         "phase":t.state,"current_operation":t.current},
                     retained_leases={"records":self.leases,"active":t.active,"uncertain":t.uncertain,
                                      "active_producers":0,"uncertain_producers":0},retained_operations=self.operations)
        return value


@pytest.fixture
def driver(request,tmp_path):
    control,client,cap,base=request.getfixturevalue("activation")
    existing,_,_=request.getfixturevalue("sender_setup")
    plan=copy.deepcopy(existing.plan)
    plan["binding"]=control.binding
    plan["binding"]["application_source_sha256"]=existing.plan["binding"]["application_source_sha256"]
    plan["postgres_identity"]["schema_version"]=135
    rows=copy.deepcopy(base.rows)
    rows[0]["Config"]["Env"]=["HANDOFF_ENABLED=1","HANDOFF_MANIFEST="+control.binding["manifest"]]
    client.inspect=lambda _:copy.deepcopy(rows)
    sample=copy.deepcopy(existing.postgres.sample)
    sample["states"]={"pending":0,"inflight":0,"held":0,"acked":0}
    sample["legacy"]["rows"]=0
    sample["expired_unacknowledged"]=0
    reader=SyntheticReader(plan["postgres_identity"],sample,client)
    d=sender.SenderActivationDriver(plan,client,reader,tmp_path/"activation-driver")
    return d,client,cap,reader


def test_driver_first_closed_and_resume_real_reader_boundary(driver):
    d,t,cap,reader=driver
    op=d.plan["binding"]["initial_operation_id"]
    receipt=d.initialize_closed(op)
    assert receipt["state"] == "closed"
    reader.operations=1
    value=d.production_resume(op,0,cap)
    assert value["pg_receipt"]["state"] == "open"
    assert d.query_production_resume(value["operation_id"],cap) == value


@pytest.mark.parametrize("cause",["held","legacy","pending","unknown","operation_limit","lease_limit"])
def test_driver_resume_blockers_preserve_closed_without_post(driver,cause):
    d,t,cap,reader=driver
    op=d.plan["binding"]["initial_operation_id"]
    d.initialize_closed(op)
    if cause in {"held","pending"}:
        reader.sample["states"][cause]=1
    elif cause == "legacy":
        reader.sample["legacy"]["rows"]=1
    elif cause == "unknown":
        t.uncertain=1
    elif cause == "operation_limit":
        reader.operations=9999
    else:
        reader.leases=9998
    with pytest.raises(CutoverError):
        d.production_resume(op,0,cap)
    assert t.state == "closed"
    assert [c[1] for c in t.calls if c[0] == "POST"] == ["/initialize-closed"]


def test_initialclosed_does_not_claim_held_rows_drained(driver):
    d,_,_,reader=driver
    reader.sample["states"]["held"]=1
    value=d.initialize_closed(d.plan["binding"]["initial_operation_id"])
    assert value["state"] == "closed" and "queue_checked" not in value
    assert reader.sample["states"]["held"] == 1


def test_default_transport_never_accepts_activation_mutations():
    plain=object.__new__(sender.NativeSenderTransport)
    new=object.__new__(sender.NativeSenderActivationTransport)
    assert not plain._allowed("POST","/initialize-closed")
    assert not plain._allowed("POST","/production-resume")
    assert new._allowed("POST","/initialize-closed")
    assert not new._allowed("POST","/initialize")
    assert not new._allowed("POST","/resume")
    assert not new._allowed("POST","/pause")
