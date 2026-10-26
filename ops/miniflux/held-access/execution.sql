-- ISOLATION ONLY; installed after install.sql. Not a sender schema migration.
BEGIN;
DO $g$ BEGIN IF current_database()<>'synthetic_outbox' THEN
 RAISE EXCEPTION 'production installation prohibited'; END IF; END $g$;
GRANT UPDATE(attempts,lease_token,lease_until,state,acked_at,last_error)
 ON __SOURCE__.webhook_outbox TO __OWNER__;
-- A narrowly scoped installer-owned accessor avoids granting pg_read_all_stats
-- (which would expose other sessions' queries) to the NOLOGIN function owner.
CREATE FUNCTION __SCHEMA__.execution_backend_start() RETURNS timestamptz LANGUAGE sql SECURITY DEFINER
 SET search_path=pg_catalog,__SCHEMA__,pg_temp AS $f$
 SELECT backend_start FROM pg_stat_activity WHERE pid=pg_backend_pid() $f$;
REVOKE ALL ON FUNCTION __SCHEMA__.execution_backend_start() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION __SCHEMA__.execution_backend_start() TO __OWNER__;
SET LOCAL ROLE __OWNER__;
ALTER TABLE __SCHEMA__.control_marker ADD COLUMN receiver_identity text NOT NULL DEFAULT '';
ALTER TABLE __SCHEMA__.control_marker ADD COLUMN endpoint_sha256 text NOT NULL DEFAULT '';
CREATE TABLE __SCHEMA__.execution_approvals(
 operation_id text PRIMARY KEY CHECK(operation_id~'^[a-f0-9]{32}$'),
 plan jsonb NOT NULL, proof jsonb NOT NULL, note_sha256 text NOT NULL,
 plan_sha256 text NOT NULL, proof_sha256 text NOT NULL, reviewer_name name NOT NULL,
 approved_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 reserved_bytes integer NOT NULL DEFAULT 12288 CHECK(reserved_bytes=12288),
 CHECK(octet_length(plan::text)+octet_length(proof::text)<6144)
);
CREATE TABLE __SCHEMA__.execution_claims(
 operation_id text PRIMARY KEY REFERENCES __SCHEMA__.execution_approvals,
 executor_name name NOT NULL, backend_pid integer NOT NULL, backend_start timestamptz NOT NULL,
 claimed_snapshot_sha256 text NOT NULL, attempts integer NOT NULL,
 claimed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE __SCHEMA__.execution_results(
 operation_id text PRIMARY KEY REFERENCES __SCHEMA__.execution_claims,
 executor_name name NOT NULL, result jsonb NOT NULL CHECK(octet_length(result::text)<4096),
 finished_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER execution_approvals_immutable BEFORE UPDATE OR DELETE ON __SCHEMA__.execution_approvals
 FOR EACH ROW EXECUTE FUNCTION __SCHEMA__.immutable_evidence();
CREATE TRIGGER execution_claims_immutable BEFORE UPDATE OR DELETE ON __SCHEMA__.execution_claims
 FOR EACH ROW EXECUTE FUNCTION __SCHEMA__.immutable_evidence();
CREATE TRIGGER execution_results_immutable BEFORE UPDATE OR DELETE ON __SCHEMA__.execution_results
 FOR EACH ROW EXECUTE FUNCTION __SCHEMA__.immutable_evidence();

CREATE FUNCTION __SCHEMA__.execution_durable() RETURNS void LANGUAGE plpgsql
 SET search_path=pg_catalog,__SCHEMA__,pg_temp AS $f$
BEGIN
 PERFORM set_config('synchronous_commit','on',true);
 IF current_database()<>'synthetic_outbox' OR current_setting('fsync')<>'on'
 OR current_setting('full_page_writes')<>'on' THEN RAISE EXCEPTION 'durable isolation required'; END IF;
END $f$;

-- Lock order for every execution transaction: advisory -> marker -> outbox -> approval.
-- Keeping marker FOR SHARE until HTTP/final commit prevents changing this stop
-- assertion concurrently. It is a synthetic adapter assertion, not production proof.
CREATE FUNCTION __SCHEMA__.execution_stop(proof jsonb,operation text,endpoint text) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,__SCHEMA__,pg_temp AS $f$
DECLARE m __SCHEMA__.control_marker%ROWTYPE; expected jsonb;
BEGIN
 IF NOT pg_try_advisory_xact_lock(19500625) THEN RAISE EXCEPTION 'execution busy'; END IF;
 SELECT * INTO STRICT m FROM __SCHEMA__.control_marker WHERE singleton FOR SHARE;
 expected:=jsonb_build_object('schema',1,'mode','isolation','operation_id',operation,
 'database_identity',m.database_identity,'run_id',m.run_id,'manifest_sha256',m.manifest_sha256,
 'adapter_identity',m.adapter_identity,'observed_at',m.observed_at,'producers_closed',true,
 'sender_stopped',true,'no_active_sender_leases',true,'stopped_operation_id',m.stopped_operation_id,
 'receiver_identity',m.receiver_identity,'endpoint_sha256',m.endpoint_sha256);
 IF proof IS DISTINCT FROM expected OR NOT m.producers_closed OR NOT m.sender_stopped
 OR NOT m.no_active_sender_leases OR m.observed_at<clock_timestamp()-interval '60 seconds'
 OR m.observed_at>clock_timestamp()+interval '1 second' OR m.receiver_identity!~'^[a-f0-9]{64}$'
 OR m.endpoint_sha256 IS DISTINCT FROM endpoint THEN RAISE EXCEPTION 'stopped receiver binding changed'; END IF;
 IF EXISTS(SELECT 1 FROM __SOURCE__.webhook_outbox WHERE state='inflight'
 OR (state='pending' AND lease_until>clock_timestamp())) THEN RAISE EXCEPTION 'automatic lease remains'; END IF;
END $f$;

CREATE FUNCTION __SCHEMA__.execution_approve(plan jsonb,proof jsonb,note text) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,__SCHEMA__,pg_temp
 SET lock_timeout='3s' AS $f$
DECLARE p text; q text;
BEGIN
 PERFORM __SCHEMA__.assert_actor('__REVIEWER__','__EXECUTOR__');
 PERFORM __SCHEMA__.execution_stop(proof,plan->>'operation_id',plan->>'endpoint_sha256');
 PERFORM 1 FROM __SOURCE__.webhook_outbox WHERE delivery_id=plan->>'delivery_id' FOR UPDATE;
 PERFORM __SCHEMA__.validate_binding(plan,proof-'receiver_identity'-'endpoint_sha256');
 IF coalesce(note,'')!~'^[a-f0-9]{64}$' THEN RAISE EXCEPTION 'operator approval hash required'; END IF;
 IF (SELECT count(*)*3+3>10000 OR count(*)*12288+12288>16777216 FROM __SCHEMA__.execution_approvals)
 THEN RAISE EXCEPTION 'execution audit capacity exhausted'; END IF;
 PERFORM __SCHEMA__.execution_durable(); p:=__SCHEMA__.sha(plan);q:=__SCHEMA__.sha(proof);
 INSERT INTO __SCHEMA__.execution_approvals(operation_id,plan,proof,note_sha256,plan_sha256,proof_sha256,reviewer_name)
 VALUES(plan->>'operation_id',plan,proof,note,p,q,session_user);
 RETURN jsonb_build_object('operation_id',plan->>'operation_id','plan_sha256',p,'proof_sha256',q,'note_sha256',note);
END $f$;

CREATE FUNCTION __SCHEMA__.execution_claim(operation text,p text,q text,note text) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,__SCHEMA__,pg_temp
 SET lock_timeout='3s' AS $f$
DECLARE a __SCHEMA__.execution_approvals%ROWTYPE; r __SOURCE__.webhook_outbox%ROWTYPE; increment integer;
BEGIN
 PERFORM __SCHEMA__.assert_actor('__EXECUTOR__','__REVIEWER__');
 SELECT * INTO STRICT a FROM __SCHEMA__.execution_approvals WHERE operation_id=operation;
 PERFORM __SCHEMA__.execution_stop(a.proof,operation,a.plan->>'endpoint_sha256');
 SELECT * INTO STRICT r FROM __SOURCE__.webhook_outbox WHERE delivery_id=a.plan->>'delivery_id' FOR UPDATE;
 PERFORM 1 FROM __SCHEMA__.execution_approvals WHERE operation_id=operation FOR UPDATE;
 IF a.reviewer_name=session_user OR a.plan_sha256 IS DISTINCT FROM p OR a.proof_sha256 IS DISTINCT FROM q
 OR a.note_sha256 IS DISTINCT FROM note THEN RAISE EXCEPTION 'exact separated approval required'; END IF;
 PERFORM __SCHEMA__.validate_binding(a.plan,a.proof-'receiver_identity'-'endpoint_sha256');
 IF EXISTS(SELECT 1 FROM __SCHEMA__.execution_claims WHERE operation_id=operation) THEN
  RAISE EXCEPTION 'already claimed; query original operation only'; END IF;
 PERFORM __SCHEMA__.execution_durable();
 increment:=CASE WHEN a.plan->>'action'='retry_once' THEN 1 ELSE 0 END;
 UPDATE __SOURCE__.webhook_outbox SET attempts=attempts+increment,lease_token='manual:'||operation,
 lease_until=NULL,last_error='manual_execution_claimed' WHERE delivery_id=r.delivery_id RETURNING * INTO r;
 INSERT INTO __SCHEMA__.execution_claims(operation_id,executor_name,backend_pid,backend_start,claimed_snapshot_sha256,attempts)
 VALUES(operation,session_user,pg_backend_pid(),__SCHEMA__.execution_backend_start(),
 __SCHEMA__.sha(to_jsonb(r)),r.attempts);
 RETURN jsonb_build_object('operation_id',operation,'status','claimed_not_sent','attempts',r.attempts);
END $f$;

CREATE FUNCTION __SCHEMA__.execution_begin(operation text) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,__SCHEMA__,pg_temp
 SET lock_timeout='3s' AS $f$
DECLARE a __SCHEMA__.execution_approvals%ROWTYPE; c __SCHEMA__.execution_claims%ROWTYPE;
 r __SOURCE__.webhook_outbox%ROWTYPE;
BEGIN
 PERFORM __SCHEMA__.assert_actor('__EXECUTOR__','__REVIEWER__');
 SELECT * INTO STRICT a FROM __SCHEMA__.execution_approvals WHERE operation_id=operation;
 PERFORM __SCHEMA__.execution_stop(a.proof,operation,a.plan->>'endpoint_sha256');
 SELECT * INTO STRICT r FROM __SOURCE__.webhook_outbox WHERE delivery_id=a.plan->>'delivery_id' FOR UPDATE;
 SELECT * INTO STRICT c FROM __SCHEMA__.execution_claims WHERE operation_id=operation;
 IF c.executor_name<>session_user OR c.backend_pid<>pg_backend_pid()
 OR c.backend_start IS DISTINCT FROM __SCHEMA__.execution_backend_start()
 OR c.claimed_snapshot_sha256<>__SCHEMA__.sha(to_jsonb(r)) OR r.state<>'held'
 OR r.lease_token<>'manual:'||operation OR EXISTS(SELECT 1 FROM __SCHEMA__.execution_results WHERE operation_id=operation)
 THEN RAISE EXCEPTION 'exact same-session claim required; query only after interruption'; END IF;
 IF a.plan->>'action'='retry_once' AND (r.attempts>20 OR r.created_at<=clock_timestamp()-interval '7 days')
 THEN RAISE EXCEPTION 'charged original budget expired'; END IF;
 PERFORM __SCHEMA__.execution_durable();
 RETURN jsonb_build_object('operation_id',operation,'delivery_id',r.delivery_id,'body_sha256',r.body_sha256,
 'body_hex',encode(r.body,'hex'),'endpoint',r.target_url,'signature',r.signature,'action',a.plan->>'action',
 'receiver_identity',a.proof->>'receiver_identity','endpoint_sha256',a.proof->>'endpoint_sha256',
 'attempts',r.attempts,'lease',r.lease_token,'snapshot_sha256',c.claimed_snapshot_sha256);
END $f$;

CREATE FUNCTION __SCHEMA__.execution_finish(operation text,snapshot_hash text,outcome text,response_hash text,acked boolean,requests integer,probes integer) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,__SCHEMA__,pg_temp
 SET lock_timeout='3s' AS $f$
DECLARE data jsonb; r __SOURCE__.webhook_outbox%ROWTYPE; result jsonb;
BEGIN
 -- Revalidates the same locked row, stop marker and exact claim within the HTTP transaction.
 data:=__SCHEMA__.execution_begin(operation);
 IF data->>'snapshot_sha256' IS DISTINCT FROM snapshot_hash OR acked IS NULL
 OR requests IS NULL OR probes IS NULL OR requests NOT IN (0,1) OR probes NOT IN (0,1) OR coalesce(outcome,'') NOT IN
 ('isolation_receiver_unconfirmed','isolation_target_or_identity_rejected','transport_outcome_unknown',
 'incomplete_receipt_unknown','receipt_unconfirmed','exact_durable_receipt','action_rejected','request_rejected')
 OR (acked AND (outcome<>'exact_durable_receipt' OR requests<>1 OR probes<>1 OR coalesce(response_hash,'')!~'^[a-f0-9]{64}$'))
 OR (NOT acked AND outcome='exact_durable_receipt') THEN RAISE EXCEPTION 'invalid exact result'; END IF;
 PERFORM __SCHEMA__.execution_durable();
 UPDATE __SOURCE__.webhook_outbox SET state=CASE WHEN acked THEN 'acked' ELSE 'held' END,
 acked_at=CASE WHEN acked THEN clock_timestamp() ELSE NULL END,
 lease_token=CASE WHEN acked THEN NULL ELSE lease_token END,last_error=outcome
 WHERE delivery_id=data->>'delivery_id' RETURNING * INTO STRICT r;
 result:=jsonb_build_object('operation_id',operation,'delivery_id',r.delivery_id,'state',r.state,'attempts',r.attempts,
 'outcome',outcome,'response_sha256',response_hash,'original_http_attempts',requests,'isolation_probe_http_attempts',probes);
 INSERT INTO __SCHEMA__.execution_results(operation_id,executor_name,result) VALUES(operation,session_user,result);
 RETURN result;
END $f$;

CREATE FUNCTION __SCHEMA__.execution_query(operation text) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,__SCHEMA__,pg_temp AS $f$
DECLARE c __SCHEMA__.execution_claims%ROWTYPE; result jsonb;
BEGIN
 PERFORM __SCHEMA__.assert_actor('__EXECUTOR__','__REVIEWER__');
 SELECT * INTO STRICT c FROM __SCHEMA__.execution_claims WHERE operation_id=operation;
 IF c.executor_name<>session_user THEN RAISE EXCEPTION 'original executor required'; END IF;
 SELECT r.result INTO result FROM __SCHEMA__.execution_results r WHERE operation_id=operation;
 RETURN coalesce(result,jsonb_build_object('operation_id',operation,'status','claimed_outcome_unknown',
 'attempts',c.attempts,'resend_allowed',false));
END $f$;

REVOKE ALL ON ALL TABLES IN SCHEMA __SCHEMA__ FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA __SCHEMA__ FROM PUBLIC;
GRANT EXECUTE ON FUNCTION __SCHEMA__.execution_approve(jsonb,jsonb,text) TO __REVIEWER__;
GRANT EXECUTE ON FUNCTION __SCHEMA__.execution_claim(text,text,text,text),__SCHEMA__.execution_begin(text),
 __SCHEMA__.execution_finish(text,text,text,text,boolean,integer,integer),__SCHEMA__.execution_query(text) TO __EXECUTOR__;
GRANT SELECT ON __SCHEMA__.execution_approvals,__SCHEMA__.execution_claims,__SCHEMA__.execution_results TO __AUDITOR__;
RESET ROLE;
COMMIT;
