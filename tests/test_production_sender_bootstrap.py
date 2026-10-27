"""Offline actual validators with bounded Docker/PG transport doubles."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tarfile
import time
from types import SimpleNamespace

import pytest

from scripts import production_sender_bootstrap as bootstrap
from services import admission_fence as gate
from scripts.production_legacy_takeover import LegacyTakeover


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def archive(raw=b"reviewed synthetic ELF", *, kind=tarfile.REGTYPE, name="miniflux"):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as stream:
        item = tarfile.TarInfo(name)
        item.type, item.size = kind, len(raw) if kind == tarfile.REGTYPE else 0
        stream.addfile(item, io.BytesIO(raw) if item.size else None)
    return output.getvalue()


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    sealed = tmp_path / "sealed"
    sealed.mkdir(mode=0o700)
    def freeze(name, raw):
        path = sealed / name
        path.write_bytes(raw)
        path.chmod(0o400)
        return {"path": str(path), "sha256": sha(raw)}
    operation, revision, image_id, identifier = "a"*64, "b"*40, "sha256:"+"c"*64, "d"*64
    network = {"Name": "reviewed-existing", "Id": "e"*64, "Driver": "bridge", "Scope": "local", "Internal": False, "Options": {}, "IPAM": {}}
    token, dsn = tmp_path / "token.json", tmp_path / "dsn.json"
    gate._write(token, {"token": "f"*64})
    gate._write(dsn, {"dsn": "postgres://synthetic:synthetic@127.0.0.1:5432/synthetic"})
    env = {"HANDOFF_ENABLED":"1", "WEBHOOK_OUTBOX":"1", "RUN_MIGRATIONS":"1", "HANDOFF_MANIFEST":"1"*64,
           "WEBHOOK_OUTBOX_CONTRACT":"synthetic-only", "HANDOFF_CONTROL_TOKEN":"f"*64, "HANDOFF_CONTROL_ADDR":"127.0.0.1:19002",
           "DATABASE_URL":gate._read(dsn)["dsn"]}
    service = {"image": image_id, "environment": env, "user":"65534:65534", "mem_limit":768*1024**2, "memswap_limit":768*1024**2,
               "pids_limit":256, "cpus":1, "cap_drop":["ALL"], "security_opt":["no-new-privileges:true"], "restart":"no",
               "read_only":True, "volumes":[{"type":"bind","source":str(sealed),"target":"/etc/miniflux-retained","read_only":True}], "ports":[], "networks":["sender"], "platform":"linux/amd64", "healthcheck":{"disable":True},
               "labels":{"org.opencontainers.image.revision":revision, "io.ai-filter.handoff.role":"miniflux", "io.ai-filter.sender.bootstrap":operation}}
    manifest = {"services":{"sender_"+operation[:16]:service}, "networks":{"sender":{"name":network["Name"],"external":True}}}
    image = {"Id": image_id, "Architecture":"amd64", "Os":"linux", "Config":{"Entrypoint":["/usr/bin/miniflux"],"Cmd":[],
             "User":"65534", "Env":[], "WorkingDir":"", "Labels":{"org.opencontainers.image.revision":revision}}}
    labels = {**service["labels"], "com.docker.compose.project":"reviewed-candidate", "com.docker.compose.service":next(iter(manifest["services"])),
              "com.docker.compose.project.working_dir":str(sealed)}
    row = {"Id":identifier,"Name":"/candidate","Image":image_id,"RestartCount":0,"Config":{**image["Config"],"Labels":labels,
           "User":"65534:65534","Env":[k+"="+v for k,v in env.items()],"Healthcheck":{"Test":["NONE"]}},
           "State":{"Running":False,"Status":"created","StartedAt":"0001-01-01T00:00:00Z","Restarting":False,"Dead":False,"Paused":False,"OOMKilled":False},
           "HostConfig":{"Memory":768*1024**2,"MemoryReservation":0,"MemorySwap":768*1024**2,"PidsLimit":256,"NanoCpus":10**9,
             "CpusetCpus":"","CpuShares":0,"Privileged":False,"CapAdd":[],"CapDrop":["ALL"],"SecurityOpt":["no-new-privileges:true"],
             "GroupAdd":[],"ReadonlyRootfs":True,"RestartPolicy":{"Name":"no"},"NetworkMode":network["Name"],"PortBindings":{}},
           "Mounts":[{"Type":"bind","Source":str(sealed),"Destination":"/etc/miniflux-retained","RW":False}],"NetworkSettings":{"Networks":{}}}
    old = deepcopy(row)
    old["Id"],old["State"]["Status"] = "7"*64,"exited"
    old["NetworkSettings"]["Networks"]={network["Name"]:{"NetworkID":network["Id"]}}
    legacy=LegacyTakeover.__new__(LegacyTakeover)
    legacy.plan={"docker":{"path":"/usr/bin/docker"},"containers":{"sender":{"id":old["Id"]}}}
    legacy.live=True
    legacy.check=lambda **kw: gate._require(legacy.live,"synthetic retained legacy lock lost")
    legacy.check_stopped=lambda: {"operation_id":"8"*64,"plan_sha256":"9"*64,"inventory_drained":False}
    build_log=freeze("build.log",b"synthetic externally reviewed offline build evidence; no real build in unit test\n")
    lock=json.loads((bootstrap.ROOT/"ops/miniflux/pause-source-lock.json").read_text())
    build={"schema":"reviewed-miniflux-offline-build-v1","revision":revision,"upstream_revision":lock["revision"],
           "pause_lock_sha256":sha((bootstrap.ROOT/"ops/miniflux/pause-source-lock.json").read_bytes()),
           "dockerfile_sha256":sha((bootstrap.ROOT/"ops/miniflux/Dockerfile").read_bytes()),"binary_sha256":sha(b"reviewed synthetic ELF"),
           "image_id":image_id,"go_version":"go1.26.4","build_evidence_sha256":build_log["sha256"]}
    before={"database":"synthetic","schema":"public","system_identifier":"123","postmaster_started_at":"synthetic-start",
            "current_user":"synthetic","session_user":"synthetic","schema_version":134}
    info=sealed.stat()
    mount_ids={str(sealed):{"device":info.st_dev,"inode":info.st_ino,"uid":info.st_uid,"mode":info.st_mode & 0o777,"kind":"directory"}}
    plan={"schema":bootstrap.SCHEMA,"operation_id":operation,"source_sha256":bootstrap.source_sha256(),"revision":revision,
          "project":"reviewed-candidate","service":next(iter(manifest["services"])),"compose":freeze("compose.json",gate._encoded(manifest)),
          "environment":freeze("environment",b"# no private aliases in unit fixture\n"),"build":freeze("build.json",gate._encoded(build)),
          "build_evidence":build_log,"mount_identities":mount_ids,"legacy_operation_id":"8"*64,"legacy_plan_sha256":"9"*64,"network":network,"postgres_before":before,
          "dsn_file":str(dsn),"token_file":str(token),"control_url":"http://127.0.0.1:19002","manifest":"1"*64,"contract":"synthetic-only",
          "window":{"not_before":int(time.time())-1,"expires_at":int(time.time())+300}}
    p=SimpleNamespace(root=root,plan=plan,legacy=legacy,row=row,old=old,manifest=manifest,image=image,service=service,build=build,
                      created=False,calls=[],raw=archive(),freeze=freeze,monkeypatch=monkeypatch)
    def docker(argv,**kwargs):
        p.calls.append(argv)
        if argv[:2]==["image","inspect"]:
            return gate._encoded([image])
        if argv[:2]==["network","inspect"]:
            return gate._encoded([network])
        if argv[0]=="inspect":
            return gate._encoded([old if argv[1]==old["Id"] else row])
        if argv[0]=="ps":
            return (identifier+"\n").encode() if p.created else b""
        if argv[0]=="compose":
            p.created=True
            return b"created"
        if argv[0]=="cp":
            return p.raw
        if argv[0]=="start":
            row["State"].update(Running=True,Status="running",StartedAt="2026-10-27T00:00:00Z")
            row["NetworkSettings"]["Networks"]={network["Name"]:{"NetworkID":network["Id"]}}
            return identifier.encode()
        raise AssertionError(argv)
    monkeypatch.setattr(bootstrap,"_docker",docker)
    monkeypatch.setattr(bootstrap.private,"source_identity",lambda root:{"source_sha":revision})
    monkeypatch.setattr(bootstrap,"_postgres",lambda plan,**kw:deepcopy(before))
    return p


def test_actual_validators_create_binary_verify_and_start_fixed_id(prepared):
    p=prepared
    value=bootstrap.create(p.legacy,p.plan,p.root)
    assert value["status"]=="created_unstarted" and value["binary"]["sha256"]==p.build["binary_sha256"]
    assert not p.row["State"]["Running"]
    result=bootstrap.start(p.legacy,p.plan,p.root)
    assert result["status"]=="started_requires_actual135_and_closed_initialization"
    assert [c for c in p.calls if c[0]=="start"]==[["start",p.row["Id"]]]
    assert not p.old["State"]["Running"] and not any(c[0] in {"rm","kill","stop","restart"} for c in p.calls)
    with pytest.raises((gate.AdmissionError,ValueError)):
        bootstrap.start(p.legacy,p.plan,p.root)


@pytest.mark.parametrize("change",["dependency","entrypoint","privileged","replicas","profiles","wrong_image","source","resources","token","outbox","handoff","dsn","binary_mount","dsn_file","create_admin"])
def test_unsafe_manifest_or_binding_refused_before_create(prepared,change):
    p=prepared
    if change in {"dependency","entrypoint","privileged","replicas","profiles"}:
        p.service[{"dependency":"depends_on","entrypoint":"entrypoint","privileged":"privileged","replicas":"deploy","profiles":"profiles"}[change]]=True
    elif change=="wrong_image":
        p.service["image"]="sha256:"+"0"*64
    elif change=="source":
        p.plan["source_sha256"]="0"*64
    elif change=="resources":
        p.service["pids_limit"]=257
    elif change=="token":
        p.service["environment"]["HANDOFF_CONTROL_TOKEN"]="0"*64
    elif change=="outbox":
        p.service["environment"]["WEBHOOK_OUTBOX"]="0"
    elif change=="handoff":
        p.service["environment"]["HANDOFF_ENABLED"]="0"
    elif change=="dsn":
        p.service["environment"]["DATABASE_URL"]="postgres://another-database"
    elif change=="dsn_file":
        p.service["environment"]["DATABASE_URL_FILE"]="/private/override"
    elif change=="create_admin":
        p.service["environment"]["CREATE_ADMIN"]="1"
    else:
        p.service["volumes"]=[{"type":"bind","source":str(p.root),"target":"/usr","read_only":True}]
    p.plan["compose"]=p.freeze("changed-compose.json",gate._encoded(p.manifest))
    with pytest.raises((gate.AdmissionError,ValueError)):
        bootstrap.create(p.legacy,p.plan,p.root)
    assert not any(c[0]=="compose" for c in p.calls)


@pytest.mark.parametrize("kind",[tarfile.SYMTYPE,tarfile.LNKTYPE,tarfile.DIRTYPE])
def test_created_binary_tar_never_follows_any_link(prepared,kind):
    p=prepared
    p.raw=archive(kind=kind)
    with pytest.raises(gate.AdmissionError,match="regular"):
        bootstrap.create(p.legacy,p.plan,p.root)
    assert p.created and not p.row["State"]["Running"]
    assert not (p.root/"created.json").exists()


def test_mismatched_actual_binary_keeps_created_stopped_and_never_recreates(prepared):
    p=prepared
    p.raw=archive(b"different binary")
    with pytest.raises(gate.AdmissionError,match="binary differs"):
        bootstrap.create(p.legacy,p.plan,p.root)
    assert bootstrap.query(p.legacy,p.plan,p.root)["status"]=="create_uncertain_no_start"
    with pytest.raises(gate.AdmissionError,match="attempted"):
        bootstrap.create(p.legacy,p.plan,p.root)
    assert len([c for c in p.calls if c[0]=="compose"])==1


def test_start_ack_unknown_is_inspected_without_repeat_start(prepared,monkeypatch):
    p=prepared
    bootstrap.create(p.legacy,p.plan,p.root)
    real=bootstrap._docker
    def lost(argv,**kwargs):
        value=real(argv,**kwargs)
        if argv[0]=="start":
            raise TimeoutError("synthetic lost actual start ACK")
        return value
    monkeypatch.setattr(bootstrap,"_docker",lost)
    with pytest.raises(TimeoutError):
        bootstrap.start(p.legacy,p.plan,p.root)
    value=bootstrap.query(p.legacy,p.plan,p.root)
    assert value["running"] and value["mutation_replayed"] is False
    assert len([c for c in p.calls if c[0]=="start"])==1


def test_changed_old_business_mount_or_port_cannot_be_replaced(prepared):
    p=prepared
    p.old["HostConfig"]["PortBindings"]={"8080/tcp":[{"HostIp":"127.0.0.1","HostPort":"8080"}]}
    with pytest.raises(gate.AdmissionError,match="retain original"):
        bootstrap.create(p.legacy,p.plan,p.root)
    assert not any(c[0]=="compose" for c in p.calls)


def test_caller_boolean_is_not_legacy_takeover_capability(prepared):
    p=prepared
    with pytest.raises(gate.AdmissionError,match="capability"):
        bootstrap.create({"stopped":True},p.plan,p.root)
    assert not p.calls


@pytest.fixture
def initialized(request,monkeypatch):
    from test_miniflux_activation_adapter import ActivationClient
    from sender_drain_inventory import PostgresReader
    p=request.getfixturevalue("prepared")
    if getattr(request,"param",False):
        p.service["volumes"],p.old["Mounts"],p.row["Mounts"]=[],[],[]
        p.plan["mount_identities"]={}
        dsn="postgres://synthetic:synthetic@192.0.2.44:5432/synthetic?sslmode=disable"
        gate._write(Path(p.plan["dsn_file"]),{"dsn":dsn})
        p.service["environment"]["DATABASE_URL"]=dsn
        p.row["Config"]["Env"]=[k+"="+v for k,v in p.service["environment"].items()]
        p.plan["compose"]=p.freeze("empty-compose.json",gate._encoded(p.manifest))
    bootstrap.create(p.legacy,p.plan,p.root)
    bootstrap.start(p.legacy,p.plan,p.root)
    identity={**p.plan["postgres_before"],"schema_version":135,"table_oid":100,"legacy_table_oid":101,
              "state_table_oid":102,"operations_table_oid":103,"leases_table_oid":104}
    monkeypatch.setattr(bootstrap,"_postgres",lambda plan,**kw:deepcopy(identity))
    clients=[]
    def transport(url,token,*,container_id):
        assert container_id==p.row["Id"]
        if not clients:
            binding=gate._read(p.root/"driver-plan.json")["binding"]
            client=ActivationClient(binding)
            client.inspect=lambda _:deepcopy([p.row])
            clients.append(client)
        return clients[0]
    class Reader(PostgresReader):
        def __init__(self,path,expected):
            self.expected=expected
        def initial_snapshot(self):
            t=clients[0]
            return {"database_identity":identity,"gate":None if t.state=="missing" else {
                "manifest":t.binding["manifest"],"generation":t.generation,"phase":t.state,"current_operation":t.current},
                "retained_operations":0 if t.state=="missing" else 1,"retained_leases":{"records":0,"active":0,"uncertain":0}}
    monkeypatch.setattr(bootstrap,"NativeSenderActivationTransport",transport)
    monkeypatch.setattr(bootstrap,"PostgresReader",Reader)
    p.clients=clients
    return p


def test_real_driver_first_closed_and_original_query_never_resume(initialized):
    p=initialized
    value=bootstrap.initialize(p.legacy,p.plan,p.root)
    assert value["status"]=="sender_initialized_closed" and value["pg_receipt"]["generation"]==0
    assert value["historical_raw_coverage"]=="unknown_before_T0_no_replay"
    assert bootstrap.query(p.legacy,p.plan,p.root)["pg_receipt"]==value["pg_receipt"]
    assert [row[1] for row in p.clients[0].calls if row[0]=="POST"]==["/initialize-closed"]


def test_real_driver_lost_closed_ack_only_original_query_after_window(initialized,monkeypatch):
    p=initialized
    original=bootstrap.NativeSenderActivationTransport
    def lost(*args,**kwargs):
        client=original(*args,**kwargs)
        client.lost=True
        return client
    monkeypatch.setattr(bootstrap,"NativeSenderActivationTransport",lost)
    with pytest.raises(TimeoutError):
        bootstrap.initialize(p.legacy,p.plan,p.root)
    assert not (p.root/"closed.json").exists()
    monkeypatch.setattr(bootstrap.time,"time",lambda:p.plan["window"]["expires_at"]+1)
    value=bootstrap.query(p.legacy,p.plan,p.root)
    assert value["status"]=="sender_initialized_closed" and value["query_only"]
    assert [row[1] for row in p.clients[0].calls if row[0]=="POST"]==["/initialize-closed"]


@pytest.mark.parametrize("kind",["old_schema","noncanonical_schema","existing_gate","wrong_database","wrong_role"])
def test_bounded_actual_pg_metadata_reader_refuses_unsafe_initialization(request,monkeypatch,kind):
    p=request.getfixturevalue("prepared")
    # Restore the real SQL reader; only the physical PG connection is doubled.
    import importlib.util
    spec=importlib.util.spec_from_file_location("bootstrap_sql_only",Path(bootstrap.__file__))
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    queries=[]
    base=deepcopy(p.plan["postgres_before"])
    if kind=="wrong_database":
        base["database"]="another"
    if kind=="wrong_role":
        base["current_user"]="another"
    class Result:
        def __init__(self,value):
            self.value=value
        def fetchone(self):
            return self.value
        def fetchall(self):
            return self.value
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self,*args):
            pass
        def execute(self,sql,params=None):
            queries.append((sql,params))
            if sql==module.PG_SQL:
                return Result(tuple(base[k] for k in module.PG_KEYS))
            if sql.startswith("SELECT version"):
                return Result([("131" if kind=="old_schema" else "0135" if kind=="noncanonical_schema" else "134",)])
            if sql.startswith("SELECT to_regclass"):
                return Result(("handoff_state" if kind=="existing_gate" else None,))
            if sql.startswith("SELECT 1 FROM"):
                return Result([(1,)])
            return Result(None)
    import sys
    monkeypatch.setitem(sys.modules,"psycopg",SimpleNamespace(connect=lambda *args,**kwargs:Connection()))
    with pytest.raises(gate.AdmissionError):
        module._postgres(p.plan)
    assert queries[-1][0]=="ROLLBACK"
    assert all("body" not in query.lower() and "count(*)" not in query.lower() for query,_ in queries)
    assert ("SET LOCAL statement_timeout='3000ms'",None) in queries


@pytest.mark.parametrize("initialized",[True],indirect=True)
def test_external_pg_sender_with_actual_empty_mounts_initializes_without_dummy_bind(initialized):
    p=initialized
    value=bootstrap.initialize(p.legacy,p.plan,p.root)
    assert value["status"]=="sender_initialized_closed"
    component=gate._read(p.root/"driver-plan.json")["binding"]["component"]
    assert component["mounts"]==[] and component["external_postgres"]["host"]=="192.0.2.44"
    assert component["external_postgres"]["read_only_rootfs"] is True


def test_existing_mount_identity_cannot_silently_move_to_new_inode(prepared):
    p=prepared
    key=next(iter(p.plan["mount_identities"]))
    p.plan["mount_identities"][key]["inode"]+=1
    with pytest.raises(gate.AdmissionError,match="inode"):
        bootstrap.create(p.legacy,p.plan,p.root)
    assert not any(c[0]=="compose" for c in p.calls)
