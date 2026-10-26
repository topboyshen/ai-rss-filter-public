-- ISOLATION ONLY: these roles can audit retained save_entry identities, never
-- authorize, retry, forward, update or delete them. No production installer.
BEGIN;
DO $guard$ BEGIN
 IF current_database()<>'synthetic_outbox' THEN
  RAISE EXCEPTION 'production installation prohibited';
 END IF;
 PERFORM '__SOURCE__.webhook_outbox'::regclass;
 PERFORM '__SOURCE__.webhook_legacy_originals'::regclass;
END $guard$;
CREATE ROLE __OWNER__ NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE __REVIEWER__ NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE __EXECUTOR__ NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE __AUDITOR__ NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE SCHEMA __SCHEMA__ AUTHORIZATION __OWNER__;
REVOKE ALL ON SCHEMA __SCHEMA__ FROM PUBLIC;
GRANT USAGE ON SCHEMA __SOURCE__ TO __OWNER__;
GRANT SELECT ON __SOURCE__.webhook_outbox, __SOURCE__.webhook_legacy_originals TO __OWNER__;
SET LOCAL ROLE __OWNER__;
ALTER DEFAULT PRIVILEGES IN SCHEMA __SCHEMA__ REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

CREATE FUNCTION __SCHEMA__.assert_legacy_reader() RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,__SCHEMA__,pg_temp AS $f$
BEGIN
 IF current_database()<>'synthetic_outbox'
 OR NOT (pg_has_role(session_user,'__REVIEWER__','member')
         OR pg_has_role(session_user,'__AUDITOR__','member'))
 OR pg_has_role(session_user,'__EXECUTOR__','member') THEN
  RAISE EXCEPTION 'isolated independent audit login required; no executor access';
 END IF;
END $f$;

CREATE FUNCTION __SCHEMA__.legacy_snapshot(delivery text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,__SCHEMA__,pg_temp AS $f$
DECLARE r __SOURCE__.webhook_legacy_originals%ROWTYPE;
BEGIN
 PERFORM __SCHEMA__.assert_legacy_reader();
 IF delivery IS NULL OR delivery!~'^[a-f0-9]{64}$' THEN
  RAISE EXCEPTION 'exact original identity required';
 END IF;
 SELECT * INTO STRICT r FROM __SOURCE__.webhook_legacy_originals WHERE delivery_id=delivery;
 IF r.state<>'held' OR r.event_type<>'save_entry'
 OR r.delivery_id<>encode(sha256(r.body),'hex') OR r.body_sha256<>r.delivery_id THEN
  RAISE EXCEPTION 'legacy original identity corrupt';
 END IF;
 RETURN jsonb_build_object('coverage','legacy_original_only_v1',
  'delivery_id',r.delivery_id,'body_sha256',r.body_sha256,'event_type',r.event_type,
  'state',r.state,'created_at',r.created_at,
  'endpoint_sha256',encode(sha256(convert_to(r.target_url,'UTF8')),'hex'),
  'signature_sha256',encode(sha256(convert_to(r.signature,'UTF8')),'hex'),
  'contract_sha256',encode(sha256(convert_to(r.contract,'UTF8')),'hex'),
  'snapshot_sha256',encode(sha256(convert_to(to_jsonb(r)::text,'UTF8')),'hex'),
  'charged_bytes',octet_length(r.body)+octet_length(r.target_url)+octet_length(r.contract)+1024,
  'forwarding_allowed',false,'automatic_compensation',false,'cleanup_credit',0);
END $f$;

CREATE FUNCTION __SCHEMA__.shared_inventory() RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,__SCHEMA__,pg_temp AS $f$
DECLARE n bigint; bytes bigint; legacy bigint; legacy_bytes bigint;
BEGIN
 PERFORM __SCHEMA__.assert_legacy_reader();
 SELECT count(*),coalesce(sum(octet_length(body)+octet_length(target_url)+octet_length(contract)+1024),0),
  count(*) FILTER (WHERE source='legacy'),
  coalesce(sum(octet_length(body)+octet_length(target_url)+octet_length(contract)+1024)
   FILTER (WHERE source='legacy'),0)
 INTO n,bytes,legacy,legacy_bytes
 FROM ((SELECT 'outbox' AS source,body,target_url,contract FROM __SOURCE__.webhook_outbox LIMIT 5001)
 UNION ALL (SELECT 'legacy',body,target_url,contract FROM __SOURCE__.webhook_legacy_originals LIMIT 5001)) bounded;
 IF n>5000 OR bytes>268435456 THEN RAISE EXCEPTION 'shared hard quota exceeded'; END IF;
 RETURN jsonb_build_object('coverage','outbox_and_legacy_v1','rows',n,'charged_bytes',bytes,
  'legacy_rows',legacy,'legacy_charged_bytes',legacy_bytes,
  'cleanup_credit',0,'forwarding_allowed',false,'switch_authorized',false);
END $f$;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA __SCHEMA__ FROM PUBLIC;
GRANT USAGE ON SCHEMA __SCHEMA__ TO __REVIEWER__,__EXECUTOR__,__AUDITOR__;
GRANT EXECUTE ON FUNCTION __SCHEMA__.legacy_snapshot(text),__SCHEMA__.shared_inventory()
 TO __REVIEWER__,__AUDITOR__;
COMMIT;
