-- ISOLATION ONLY. Render identifiers with verify.py; never install in production.
-- The owner can SELECT the original outbox, but cannot UPDATE or DELETE it.
BEGIN;
DO $guard$ BEGIN
 IF current_database()<>'synthetic_outbox' THEN
  RAISE EXCEPTION 'production installation prohibited';
 END IF;
END $guard$;
CREATE ROLE __OWNER__ NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE __REVIEWER__ NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE __EXECUTOR__ NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE __AUDITOR__ NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE SCHEMA __SCHEMA__ AUTHORIZATION __OWNER__;
REVOKE ALL ON SCHEMA __SCHEMA__ FROM PUBLIC;
GRANT USAGE ON SCHEMA __SOURCE__ TO __OWNER__;
GRANT SELECT ON __SOURCE__.webhook_outbox TO __OWNER__;
SET LOCAL ROLE __OWNER__;
ALTER DEFAULT PRIVILEGES IN SCHEMA __SCHEMA__ REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
CREATE TABLE __SCHEMA__.control_marker(
 singleton boolean PRIMARY KEY CHECK(singleton),
 database_identity text NOT NULL CHECK(database_identity~'^[a-f0-9]{64}$'),
 run_id text NOT NULL,
 manifest_sha256 text NOT NULL CHECK(manifest_sha256~'^[a-f0-9]{64}$'),
 adapter_identity text NOT NULL CHECK(adapter_identity='held-access-synthetic-pg-v1'),
 observed_at timestamptz NOT NULL,
 producers_closed boolean NOT NULL,
 sender_stopped boolean NOT NULL,
 no_active_sender_leases boolean NOT NULL,
 stopped_operation_id text NOT NULL DEFAULT ''
);
CREATE TABLE __SCHEMA__.approvals(
 operation_id text PRIMARY KEY CHECK(operation_id~'^[a-f0-9]{32}$'),
 delivery_id text NOT NULL CHECK(delivery_id~'^[a-f0-9]{64}$'),
 plan jsonb NOT NULL,
 proof jsonb NOT NULL,
 plan_sha256 text NOT NULL,
 proof_sha256 text NOT NULL,
 reviewer_name name NOT NULL,
 approval_note_sha256 text NOT NULL CHECK(approval_note_sha256~'^[a-f0-9]{64}$'),
 approved_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 charged_bytes integer NOT NULL DEFAULT 8192 CHECK(charged_bytes=8192),
 reserved_audit_rows integer NOT NULL DEFAULT 2 CHECK(reserved_audit_rows=2),
 CHECK(octet_length(plan::text)+octet_length(proof::text)+2048<=8192)
);
CREATE TABLE __SCHEMA__.reservations(
 operation_id text PRIMARY KEY REFERENCES __SCHEMA__.approvals(operation_id),
 executor_name name NOT NULL,
 reserved_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE __SCHEMA__.audit(
 operation_id text NOT NULL REFERENCES __SCHEMA__.approvals(operation_id),
 phase text NOT NULL CHECK(phase IN ('approved','reserved')),
 operator_name name NOT NULL,
 plan_sha256 text NOT NULL,
 proof_sha256 text NOT NULL,
 observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(operation_id,phase)
);
CREATE FUNCTION __SCHEMA__.immutable_evidence() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,__SCHEMA__,pg_temp AS $f$
BEGIN RAISE EXCEPTION 'append-only held access evidence'; END $f$;
CREATE TRIGGER approvals_immutable BEFORE UPDATE OR DELETE ON __SCHEMA__.approvals
 FOR EACH ROW EXECUTE FUNCTION __SCHEMA__.immutable_evidence();
CREATE TRIGGER reservations_immutable BEFORE UPDATE OR DELETE ON __SCHEMA__.reservations
 FOR EACH ROW EXECUTE FUNCTION __SCHEMA__.immutable_evidence();
CREATE TRIGGER audit_immutable BEFORE UPDATE OR DELETE ON __SCHEMA__.audit
 FOR EACH ROW EXECUTE FUNCTION __SCHEMA__.immutable_evidence();

CREATE FUNCTION __SCHEMA__.sha(value jsonb) RETURNS text LANGUAGE sql IMMUTABLE
 SET search_path=pg_catalog,__SCHEMA__,pg_temp AS $f$
 SELECT encode(sha256(convert_to(value::text,'UTF8')),'hex') $f$;

-- This internal helper is never granted to a login role. The JSON contains
-- only hashes of private body/URL/HMAC; actual body bytes remain in outbox.
CREATE FUNCTION __SCHEMA__.snapshot(delivery text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,__SCHEMA__,pg_temp AS $f$
DECLARE r __SOURCE__.webhook_outbox%ROWTYPE;
BEGIN
 SELECT * INTO STRICT r FROM __SOURCE__.webhook_outbox WHERE delivery_id=delivery;
 IF r.state<>'held' OR r.delivery_id<>encode(sha256(r.body),'hex') OR r.body_sha256<>r.delivery_id THEN
  RAISE EXCEPTION 'exact held original required';
 END IF;
 RETURN jsonb_build_object('delivery_id',r.delivery_id,'body_sha256',r.body_sha256,
  'endpoint_sha256',encode(sha256(convert_to(r.target_url,'UTF8')),'hex'),
  'signature_sha256',encode(sha256(convert_to(r.signature,'UTF8')),'hex'),
  'contract',r.contract,'attempts',r.attempts,'created_at',r.created_at,
  'snapshot_sha256',__SCHEMA__.sha(to_jsonb(r)),
  'manual_lease',coalesce(r.lease_token,''));
END $f$;

CREATE FUNCTION __SCHEMA__.assert_actor(expected_role text,forbidden_role text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,__SCHEMA__,pg_temp AS $f$
BEGIN
 IF current_database()<>'synthetic_outbox'
 OR NOT pg_has_role(session_user,expected_role,'member')
 OR pg_has_role(session_user,forbidden_role,'member') THEN
  RAISE EXCEPTION 'isolated separated database login required';
 END IF;
END $f$;

CREATE FUNCTION __SCHEMA__.make_plan(delivery text,action text,operation text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,__SCHEMA__,pg_temp AS $f$
DECLARE snapshot jsonb;
BEGIN
 PERFORM __SCHEMA__.assert_actor('__REVIEWER__','__EXECUTOR__');
 IF coalesce(operation,'')!~'^[a-f0-9]{32}$' OR coalesce(action,'') NOT IN ('retry_once','confirm_receipt') THEN
  RAISE EXCEPTION 'explicit operation and action required';
 END IF;
 snapshot:=__SCHEMA__.snapshot(delivery);
 RETURN snapshot||jsonb_build_object('schema',1,'mode','isolation','operation_id',operation,'action',action);
END $f$;

CREATE FUNCTION __SCHEMA__.validate_binding(plan jsonb,proof jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,__SCHEMA__,pg_temp AS $f$
DECLARE marker __SCHEMA__.control_marker%ROWTYPE; expected jsonb; now_utc timestamptz:=clock_timestamp();
BEGIN
 IF plan->>'mode' IS DISTINCT FROM 'isolation' OR plan->>'schema' IS DISTINCT FROM '1'
 OR coalesce(plan->>'operation_id','')!~'^[a-f0-9]{32}$'
 OR coalesce(plan->>'action','') NOT IN ('retry_once','confirm_receipt') THEN
  RAISE EXCEPTION 'invalid isolated plan';
 END IF;
 expected:=__SCHEMA__.snapshot(plan->>'delivery_id')||jsonb_build_object(
  'schema',1,'mode','isolation','operation_id',plan->>'operation_id','action',plan->>'action');
 IF plan IS DISTINCT FROM expected THEN RAISE EXCEPTION 'original snapshot CAS mismatch'; END IF;
 SELECT * INTO STRICT marker FROM __SCHEMA__.control_marker WHERE singleton=true;
 expected:=jsonb_build_object('schema',1,'mode','isolation','operation_id',plan->>'operation_id',
  'database_identity',marker.database_identity,'run_id',marker.run_id,
  'manifest_sha256',marker.manifest_sha256,'adapter_identity',marker.adapter_identity,
  'observed_at',marker.observed_at,'producers_closed',true,'sender_stopped',true,
  'no_active_sender_leases',true,'stopped_operation_id',marker.stopped_operation_id);
 IF proof IS DISTINCT FROM expected OR NOT marker.producers_closed OR NOT marker.sender_stopped
 OR NOT marker.no_active_sender_leases OR marker.observed_at<now_utc-interval '60 seconds'
 OR marker.observed_at>now_utc+interval '1 second' THEN
  RAISE EXCEPTION 'fresh actual isolated marker and exact proof required';
 END IF;
 IF EXISTS(SELECT 1 FROM __SOURCE__.webhook_outbox WHERE state='inflight') THEN
  RAISE EXCEPTION 'automatic inflight remains';
 END IF;
 IF plan->>'action'='retry_once' AND
 ((plan->>'attempts')::integer>=20 OR (plan->>'created_at')::timestamptz<=now_utc-interval '7 days'
 OR plan->>'manual_lease'<>'') THEN
  RAISE EXCEPTION 'original attempt/age/manual-claim budget prohibits retry';
 END IF;
 IF plan->>'manual_lease'<>'' AND
 (plan->>'manual_lease' NOT LIKE 'manual:%'
 OR substring(plan->>'manual_lease' from 8)<>marker.stopped_operation_id) THEN
  RAISE EXCEPTION 'unresolved manual operation not stopped';
 END IF;
END $f$;

CREATE FUNCTION __SCHEMA__.approve(plan jsonb,proof jsonb,note_sha256 text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,__SCHEMA__,pg_temp
 SET lock_timeout='3s' AS $f$
DECLARE plan_hash text;proof_hash text;
BEGIN
 PERFORM __SCHEMA__.assert_actor('__REVIEWER__','__EXECUTOR__');
 IF coalesce(note_sha256,'')!~'^[a-f0-9]{64}$' THEN RAISE EXCEPTION 'reviewed operator note hash required'; END IF;
 PERFORM __SCHEMA__.validate_binding(plan,proof);
 IF NOT pg_try_advisory_xact_lock(19500624) THEN RAISE EXCEPTION 'audit reservation busy'; END IF;
 IF (SELECT count(*)*2+2>10000 OR count(*)*8192+8192>16777216 FROM __SCHEMA__.approvals) THEN
  RAISE EXCEPTION 'audit reservation capacity exhausted';
 END IF;
 SET LOCAL synchronous_commit=on;
 IF current_setting('fsync')<>'on' OR current_setting('full_page_writes')<>'on' THEN
  RAISE EXCEPTION 'durable PostgreSQL settings required';
 END IF;
 plan_hash:=__SCHEMA__.sha(plan);proof_hash:=__SCHEMA__.sha(proof);
 INSERT INTO __SCHEMA__.approvals(operation_id,delivery_id,plan,proof,plan_sha256,proof_sha256,reviewer_name,approval_note_sha256)
 VALUES(plan->>'operation_id',plan->>'delivery_id',plan,proof,plan_hash,proof_hash,session_user,note_sha256);
 INSERT INTO __SCHEMA__.audit VALUES(plan->>'operation_id','approved',session_user,plan_hash,proof_hash,clock_timestamp());
 RETURN jsonb_build_object('operation_id',plan->>'operation_id','plan_sha256',plan_hash,'proof_sha256',proof_hash,
 'reviewer',session_user,'reserved_audit_rows',2,'charged_bytes',8192,'outbox_modified',false,'forwarding_allowed',false);
END $f$;

CREATE FUNCTION __SCHEMA__.reserve(operation text,plan_hash text,proof_hash text,note_sha256 text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,__SCHEMA__,pg_temp
 SET lock_timeout='3s' AS $f$
DECLARE approved __SCHEMA__.approvals%ROWTYPE;
BEGIN
 PERFORM __SCHEMA__.assert_actor('__EXECUTOR__','__REVIEWER__');
 SELECT * INTO STRICT approved FROM __SCHEMA__.approvals WHERE operation_id=operation FOR UPDATE;
 IF approved.reviewer_name=session_user OR approved.plan_sha256 IS DISTINCT FROM plan_hash OR approved.proof_sha256 IS DISTINCT FROM proof_hash
 OR approved.approval_note_sha256 IS DISTINCT FROM note_sha256 THEN RAISE EXCEPTION 'exact other-operator approval required'; END IF;
 PERFORM __SCHEMA__.validate_binding(approved.plan,approved.proof);
 SET LOCAL synchronous_commit=on;
 IF current_setting('fsync')<>'on' OR current_setting('full_page_writes')<>'on' THEN
  RAISE EXCEPTION 'durable PostgreSQL settings required';
 END IF;
 INSERT INTO __SCHEMA__.reservations VALUES(operation,session_user,clock_timestamp());
 INSERT INTO __SCHEMA__.audit VALUES(operation,'reserved',session_user,plan_hash,proof_hash,clock_timestamp());
 RETURN jsonb_build_object('operation_id',operation,'executor',session_user,
  'status','authorization_reserved_only','outbox_modified',false,'forwarding_allowed',false);
END $f$;

REVOKE ALL ON ALL TABLES IN SCHEMA __SCHEMA__ FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA __SCHEMA__ FROM PUBLIC;
GRANT USAGE ON SCHEMA __SCHEMA__ TO __REVIEWER__,__EXECUTOR__,__AUDITOR__;
GRANT EXECUTE ON FUNCTION __SCHEMA__.make_plan(text,text,text),__SCHEMA__.approve(jsonb,jsonb,text) TO __REVIEWER__;
GRANT EXECUTE ON FUNCTION __SCHEMA__.reserve(text,text,text,text) TO __EXECUTOR__;
GRANT SELECT ON __SCHEMA__.audit,__SCHEMA__.approvals,__SCHEMA__.reservations TO __AUDITOR__;
RESET ROLE;
COMMIT;
