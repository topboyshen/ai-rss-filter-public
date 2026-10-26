// SPDX-License-Identifier: Apache-2.0
package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// Fixture-only DDL: not a production migration and not installed by the CLI.
const isolationDDL = `CREATE TABLE held_isolation_marker(singleton bool PRIMARY KEY CHECK(singleton),database_identity text,run_id text,manifest_sha256 text,adapter_identity text,observed_at timestamptz,producers_closed bool,sender_stopped bool,stopped_operation_id text NOT NULL DEFAULT '',receiver_identity text,endpoint_sha256 text);
CREATE TABLE held_isolation_audit(operation_id text,phase text CHECK(phase IN ('claimed','finished')),event jsonb CHECK(octet_length(event::text)<=3840),PRIMARY KEY(operation_id,phase));
CREATE FUNCTION immutable_review_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'append-only audit'; END $$;
CREATE TRIGGER immutable_review_audit BEFORE UPDATE OR DELETE ON held_isolation_audit FOR EACH ROW EXECUTE FUNCTION immutable_review_audit();`

func fixedMigration(t *testing.T) string {
	t.Helper()
	path := filepath.Join(os.Getenv("MINIFLUX_PATCHED_SOURCE"), "internal/database/webhook_outbox.go")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if digest(raw) != "42ee96b4a961887d5387f8a0261ec6f8e9132f78eb362760451c172341640dad" {
		t.Fatal("actual fixed sender migration hash mismatch")
	}
	file, err := parser.ParseFile(token.NewFileSet(), path, raw, 0)
	if err != nil {
		t.Fatal(err)
	}
	var statements []string
	ast.Inspect(file, func(node ast.Node) bool {
		literal, ok := node.(*ast.BasicLit)
		if ok && literal.Kind == token.STRING {
			value, err := strconv.Unquote(literal.Value)
			if err == nil && strings.Contains(value, "CREATE TABLE webhook_outbox") {
				statements = append(statements, value)
			}
		}
		return true
	})
	if len(statements) != 1 {
		t.Fatal("expected one real migration SQL literal")
	}
	return statements[0]
}

type fixture struct {
	db     *sql.DB
	dsn    string
	row    Snapshot
	proof  Proof
	schema string
}

func isolationServer(handler http.Handler) *httptest.Server {
	identity := digest([]byte(fmt.Sprintf("receiver-fixture-%d", time.Now().UnixNano())))
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet && r.URL.Path == "/__held_isolation_probe" {
			if r.Header.Get("X-Miniflux-Signature") != "" || r.ContentLength > 0 {
				panic("probe leaked original")
			}
			w.Header().Set("X-Held-Isolation-Identity", identity)
			w.WriteHeader(http.StatusNoContent)
			return
		}
		handler.ServeHTTP(w, r)
	}))
}

func newFixture(t *testing.T, target string, attempts int, age time.Duration) fixture {
	t.Helper()
	dsn := os.Getenv("MINIFLUX_OUTBOX_TEST_DSN")
	if dsn == "" {
		t.Skip("explicit isolated PostgreSQL DSN absent")
	}
	u, err := url.Parse(dsn)
	if err != nil || (u.Hostname() != "127.0.0.1" && u.Hostname() != "localhost") || u.Path != "/synthetic_outbox" {
		t.Fatal("test accepts only local synthetic_outbox")
	}
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		t.Fatal(err)
	}
	schema := "held_isolation_" + fmt.Sprintf("%016x", time.Now().UnixNano())
	if _, err = db.Exec(`CREATE SCHEMA ` + schema); err != nil {
		t.Fatal(err)
	}
	db.Close()
	q := u.Query()
	q.Set("search_path", schema)
	u.RawQuery = q.Encode()
	db, err = openIsolation(u.String())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if _, err = db.Exec(fixedMigration(t) + isolationDDL); err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC().Truncate(time.Microsecond)
	proof := Proof{Schema: 1, Mode: "isolation", RunID: schema, OperationID: strings.Repeat("a", 32), ManifestSHA256: digest([]byte("only local fixture producer and sender; both not started")), ObservedAt: now, ProducersClosed: true, SenderStopped: true, NoActiveSenderLeases: true, DatabaseIdentity: digest([]byte(schema)), AdapterIdentity: "synthetic-pg-stopped-fixture-v1"}
	proof.EndpointSHA256 = digest([]byte(target))
	proof.ReceiverIdentity = digest([]byte("no-HTTP-negative-fixture"))
	if target != "http://127.0.0.1:1/webhook" {
		res, err := http.Get(strings.TrimSuffix(target, "/webhook") + "/__held_isolation_probe")
		if err != nil {
			t.Fatal(err)
		}
		proof.ReceiverIdentity = res.Header.Get("X-Held-Isolation-Identity")
		res.Body.Close()
		if !identityPattern.MatchString(proof.ReceiverIdentity) {
			t.Fatal("actual synthetic receiver identity required")
		}
	}
	_, err = db.Exec(`INSERT INTO held_isolation_marker VALUES(true,$1,$2,$3,$4,$5,true,true,'',$6,$7)`, proof.DatabaseIdentity, proof.RunID, proof.ManifestSHA256, proof.AdapterIdentity, now, proof.ReceiverIdentity, proof.EndpointSHA256)
	if err != nil {
		t.Fatal(err)
	}
	body := []byte(`{"event_type":"new_entries","entries":[{"id":1,"title":"synthetic only"}]}`)
	mac := hmac.New(sha256.New, []byte("synthetic-secret"))
	mac.Write(body)
	row := Snapshot{DeliveryID: digest(body), BodySHA256: digest(body), UserID: 1, FeedID: 1, EntryID: 1, URL: target, Signature: hex.EncodeToString(mac.Sum(nil)), Contract: "synthetic-contract", Body: body, State: "held", Attempts: attempts, Created: now.Add(-age)}
	_, err = db.Exec(`INSERT INTO webhook_outbox(delivery_id,body_sha256,user_id,feed_id,entry_id,target_url,signature,contract,body,state,attempts,created_at) VALUES($1,$1,1,1,1,$2,$3,$4,$5,'held',$6,$7)`, row.DeliveryID, row.URL, row.Signature, row.Contract, row.Body, attempts, row.Created)
	if err != nil {
		t.Fatal(err)
	}
	t.Log("retained synthetic schema", schema)
	return fixture{db, u.String(), row, proof, schema}
}

func (f fixture) approved(t *testing.T, action string) (Plan, Proof, Approval) {
	t.Helper()
	row, err := readSnapshot(context.Background(), f.db, f.row.DeliveryID, false)
	if err != nil {
		t.Fatal(err)
	}
	plan, err := makePlan(row, f.proof.DatabaseIdentity, f.proof.OperationID, action)
	if err != nil {
		t.Fatal(err)
	}
	return plan, f.proof, Approval{"isolation", plan.OperationID, objectHash(plan), objectHash(f.proof), true, true}
}

func receipt(id string) string {
	return fmt.Sprintf(`{"schema":1,"durable":true,"body_sha256":%q,"delivery_id":%q,"accepted_strategy":"audit-not-a-claim"}`, id, id)
}

func TestIsolationAndApprovalBoundaries(t *testing.T) {
	for _, dsn := range []string{"postgres://localhost/production?search_path=held_isolation_0123456789abcdef", "postgres://remote/synthetic_outbox?search_path=held_isolation_0123456789abcdef", "postgres://localhost/synthetic_outbox?search_path=public", "postgres://localhost/synthetic_outbox?search_path=held_isolation_0123456789abcdef&options=unsafe"} {
		if db, err := openIsolation(dsn); err == nil {
			db.Close()
			t.Fatal("unsafe DSN accepted")
		}
	}
	p := Plan{Schema: 1, Mode: "isolation", OperationID: strings.Repeat("a", 32), DatabaseIdentity: strings.Repeat("b", 64), EndpointSHA256: strings.Repeat("e", 64)}
	proof := Proof{Schema: 1, Mode: "isolation", RunID: "fixture", OperationID: p.OperationID, ManifestSHA256: strings.Repeat("c", 64), DatabaseIdentity: p.DatabaseIdentity, ObservedAt: time.Now(), ProducersClosed: true, SenderStopped: true, NoActiveSenderLeases: true, AdapterIdentity: "synthetic-pg-stopped-fixture-v1", ReceiverIdentity: strings.Repeat("d", 64), EndpointSHA256: p.EndpointSHA256}
	approve := func(p Plan, pf Proof) Approval {
		return Approval{"isolation", p.OperationID, objectHash(p), objectHash(pf), true, true}
	}
	if err := validateApproval(p, proof, approve(p, proof), time.Now()); err != nil {
		t.Fatal(err)
	}
	for _, change := range []func(*Proof){func(p *Proof) { p.Mode = "production" }, func(p *Proof) { p.ProducersClosed = false }, func(p *Proof) { p.SenderStopped = false }, func(p *Proof) { p.NoActiveSenderLeases = false }, func(p *Proof) { p.AdapterIdentity = "http-fixture-no-pg" }, func(p *Proof) { p.ObservedAt = time.Now().Add(-61 * time.Second) }, func(p *Proof) { p.ObservedAt = time.Now().Add(2 * time.Second) }, func(p *Proof) { p.DatabaseIdentity = strings.Repeat("d", 64) }} {
		bad := proof
		change(&bad)
		if validateApproval(p, bad, approve(p, bad), time.Now()) == nil {
			t.Fatal("invalid proof accepted")
		}
	}
	a := approve(p, proof)
	a.OriginalIdentityReviewed = false
	if validateApproval(p, proof, a, time.Now()) == nil {
		t.Fatal("unreviewed identity accepted")
	}
}

func TestStrictReceiptAndRedirect(t *testing.T) {
	id := strings.Repeat("a", 64)
	good := receipt(id)
	if !validReceipt([]byte(good), id) {
		t.Fatal("valid audit field rejected")
	}
	for _, key := range []string{"schema", "durable", "body_sha256", "delivery_id"} {
		for _, bad := range []string{strings.Title(key), strings.ToUpper(key)} {
			if validReceipt([]byte(strings.Replace(good, `"`+key+`":`, `"`+bad+`":`, 1)), id) {
				t.Fatal("case alias accepted")
			}
		}
	}
	for _, bad := range []string{good + good, `{"schema":1,"durable":false,"Durable":true}`, strings.Replace(good, `"schema":1`, `"schema":1,"schema":1`, 1), strings.Replace(good, `"durable":true`, `"durable":"true"`, 1), strings.Repeat("x", 4097)} {
		if validReceipt([]byte(bad), id) {
			t.Fatal("invalid receipt accepted")
		}
	}
	var hits atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { hits.Add(1) }))
	defer target.Close()
	source := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, target.URL, 307) }))
	defer source.Close()
	body := []byte("synthetic")
	row := Snapshot{Body: body, DeliveryID: digest(body), BodySHA256: digest(body), URL: source.URL + "/webhook", Signature: strings.Repeat("a", 64)}
	if ok, _, _, attempts := exchange(row, "retry_once"); ok || hits.Load() != 0 || attempts != 1 {
		t.Fatal("redirect forwarded original")
	}
	row.URL = "https://public.invalid/webhook"
	if ok, _, _, attempts := exchange(row, "retry_once"); ok || attempts != 0 {
		t.Fatal("locally rejected target counted as HTTP")
	}
}

func TestPostgresHeldRetryExactlyOnce(t *testing.T) {
	var hits atomic.Int32
	var body, signature string
	server := isolationServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		raw, _ := io.ReadAll(r.Body)
		body = string(raw)
		signature = r.Header.Get("X-Miniflux-Signature")
		w.WriteHeader(503)
	}))
	defer server.Close()
	f := newFixture(t, server.URL+"/webhook", 2, time.Hour)
	p, pf, a := f.approved(t, "retry_once")
	result, err := execute(f.db, p, pf, a)
	if err != nil {
		t.Fatal(err)
	}
	if result.State != "held" || result.Attempts != 3 || hits.Load() != 1 || body != string(f.row.Body) || signature != f.row.Signature {
		t.Fatal("not one exact frozen transmission", result, hits.Load())
	}
	if _, err = execute(f.db, p, pf, a); err == nil || hits.Load() != 1 {
		t.Fatal("same approval repeated network")
	}
	row, _ := readSnapshot(context.Background(), f.db, f.row.DeliveryID, false)
	if !row.Created.Equal(f.row.Created) || row.URL != f.row.URL || row.Signature != f.row.Signature || row.Contract != f.row.Contract || row.Lease != "" {
		t.Fatal("immutable or manual lease changed incorrectly")
	}
	var normal int
	f.db.QueryRow(`SELECT count(*) FROM webhook_outbox WHERE state IN ('pending','inflight')`).Scan(&normal)
	if normal != 0 {
		t.Fatal("manual failure entered automatic retries")
	}
}

func TestPostgresHeldReceiptsAndBudgets(t *testing.T) {
	for _, test := range []struct {
		name, action     string
		status, attempts int
		age              time.Duration
		valid, claimable bool
	}{{"retry_ack", "retry_once", 202, 19, time.Hour, true, true}, {"ordinary202", "retry_once", 202, 1, time.Hour, false, true}, {"exhausted", "retry_once", 202, 20, time.Hour, true, false}, {"aged", "retry_once", 202, 1, 8 * 24 * time.Hour, true, false}, {"query_exhausted", "confirm_receipt", 200, 20, 8 * 24 * time.Hour, true, true}, {"query404", "confirm_receipt", 404, 20, time.Hour, false, true}} {
		t.Run(test.name, func(t *testing.T) {
			var hits atomic.Int32
			var path string
			server := isolationServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				hits.Add(1)
				path = r.URL.Path
				raw, _ := io.ReadAll(r.Body)
				w.WriteHeader(test.status)
				if test.valid {
					fmt.Fprint(w, receipt(digest(raw)))
				}
			}))
			defer server.Close()
			f := newFixture(t, server.URL+"/webhook", test.attempts, test.age)
			p, pf, a := f.approved(t, test.action)
			result, err := execute(f.db, p, pf, a)
			if !test.claimable {
				if err == nil || hits.Load() != 0 {
					t.Fatal("exhausted row transmitted")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			expect := "held"
			if test.valid {
				expect = "acked"
			}
			if result.State != expect || hits.Load() != 1 {
				t.Fatal(result)
			}
			attempts := test.attempts
			if test.action == "retry_once" {
				attempts++
			}
			if result.Attempts != attempts {
				t.Fatal("attempt budget reset")
			}
			if test.action == "confirm_receipt" && path != "/webhook/receipt" {
				t.Fatal("query resent scoring webhook")
			}
		})
	}
}

func TestPostgresHeldCrashAndExplicitReceiptRecovery(t *testing.T) {
	var hits atomic.Int32
	server := isolationServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		if r.URL.Path != "/webhook/receipt" {
			t.Error("crash recovery retried scoring")
		}
		raw, _ := io.ReadAll(r.Body)
		fmt.Fprint(w, receipt(digest(raw)))
	}))
	defer server.Close()
	f := newFixture(t, server.URL+"/webhook", 5, time.Hour)
	p, pf, a := f.approved(t, "retry_once")
	if _, err := claim(f.db, p, pf, a); err != nil {
		t.Fatal(err)
	}
	f.db.Close()
	db, err := openIsolation(f.dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	f.db = db
	row, err := readSnapshot(context.Background(), db, f.row.DeliveryID, false)
	if err != nil || row.Attempts != 6 || row.State != "held" || row.Lease != "manual:"+p.OperationID {
		t.Fatal("crash reservation not durable", err)
	}
	if _, err = makePlan(row, p.DatabaseIdentity, strings.Repeat("b", 32), "retry_once"); err == nil {
		t.Fatal("crashed claim automatically retried")
	}
	f.proof.OperationID = strings.Repeat("b", 32)
	p2, pf2, a2 := f.approved(t, "confirm_receipt")
	if _, err = execute(db, p2, pf2, a2); err == nil || hits.Load() != 0 {
		t.Fatal("missing stopped prior operation ignored")
	}
	f.proof.StoppedOperationID = p.OperationID
	if _, err = db.Exec(`UPDATE held_isolation_marker SET stopped_operation_id=$1`, p.OperationID); err != nil {
		t.Fatal(err)
	}
	p2, pf2, a2 = f.approved(t, "confirm_receipt")
	result, err := execute(db, p2, pf2, a2)
	if err != nil || result.State != "acked" || result.Attempts != 6 || hits.Load() != 1 {
		t.Fatal(result, err)
	}
	var records int
	db.QueryRow(`SELECT count(*) FROM held_isolation_audit`).Scan(&records)
	if records != 3 {
		t.Fatal("crash audit overwritten", records)
	}
}

func TestPostgresHeldCASConcurrencyAndAuditFailure(t *testing.T) {
	var hits atomic.Int32
	server := isolationServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { hits.Add(1); w.WriteHeader(503) }))
	defer server.Close()
	f := newFixture(t, server.URL+"/webhook", 1, time.Hour)
	p, pf, a := f.approved(t, "retry_once")
	var group sync.WaitGroup
	var success atomic.Int32
	for i := 0; i < 8; i++ {
		group.Add(1)
		go func() {
			defer group.Done()
			if _, err := execute(f.db, p, pf, a); err == nil {
				success.Add(1)
			}
		}()
	}
	group.Wait()
	if hits.Load() != 1 || success.Load() != 1 {
		t.Fatal("concurrent approval sent more than once", hits.Load(), success.Load())
	}
	if _, err := f.db.Exec(`UPDATE held_isolation_audit SET event='{}'`); err == nil {
		t.Fatal("audit mutable")
	}
	if _, err := f.db.Exec(`DELETE FROM held_isolation_audit`); err == nil {
		t.Fatal("audit deletable")
	}
	f2 := newFixture(t, server.URL+"/webhook", 1, time.Hour)
	f.db.Close()
	if _, err := f2.db.Exec(`CREATE FUNCTION reject_review() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic ENOSPC'; END $$; CREATE TRIGGER reject_review BEFORE INSERT ON held_isolation_audit FOR EACH ROW EXECUTE FUNCTION reject_review()`); err != nil {
		t.Fatal(err)
	}
	p, pf, a = f2.approved(t, "retry_once")
	before := hits.Load()
	if _, err := execute(f2.db, p, pf, a); err == nil || hits.Load() != before {
		t.Fatal("audit failure sent network")
	}
	row, _ := readSnapshot(context.Background(), f2.db, f2.row.DeliveryID, false)
	if row.Attempts != 1 || row.Lease != "" {
		t.Fatal("audit failure charged claim")
	}
}

func TestPostgresHeldCapacityAndMarkerCAS(t *testing.T) {
	f := newFixture(t, "http://127.0.0.1:1/webhook", 1, time.Hour)
	if _, err := f.db.Exec(`INSERT INTO held_isolation_audit SELECT md5(i::text),'claimed','{}'::jsonb FROM generate_series(1,2048)i`); err != nil {
		t.Fatal(err)
	}
	p, pf, a := f.approved(t, "retry_once")
	if _, err := claim(f.db, p, pf, a); err == nil {
		t.Fatal("audit byte reservation cap ignored")
	}
	f.db.Close()
	f2 := newFixture(t, "http://127.0.0.1:1/webhook", 1, time.Hour)
	p, pf, a = f2.approved(t, "retry_once")
	if _, err := f2.db.Exec(`UPDATE held_isolation_marker SET producers_closed=false`); err != nil {
		t.Fatal(err)
	}
	if _, err := claim(f2.db, p, pf, a); err == nil {
		t.Fatal("actual stopped marker mismatch accepted")
	}
	if _, err := f2.db.Exec(`UPDATE held_isolation_marker SET producers_closed=true; UPDATE webhook_outbox SET attempts=2`); err != nil {
		t.Fatal(err)
	}
	if _, err := claim(f2.db, p, pf, a); err == nil {
		t.Fatal("changed attempts accepted")
	}
}

func TestPostgresHeldUnknownTransportAndClaimDurability(t *testing.T) {
	var hits atomic.Int32
	server := isolationServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		h := w.(http.Hijacker)
		conn, _, _ := h.Hijack()
		conn.Close()
	}))
	defer server.Close()
	f := newFixture(t, server.URL+"/webhook", 1, time.Hour)
	if _, err := f.db.Exec(`SET synchronous_commit=off; CREATE FUNCTION require_sync_review() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF current_setting('synchronous_commit')<>'on' THEN RAISE EXCEPTION 'not synchronous'; END IF; RETURN NEW; END $$; CREATE TRIGGER require_sync_review BEFORE INSERT ON held_isolation_audit FOR EACH ROW EXECUTE FUNCTION require_sync_review()`); err != nil {
		t.Fatal(err)
	}
	p, pf, a := f.approved(t, "retry_once")
	result, err := execute(f.db, p, pf, a)
	if err != nil || result.State != "held" || result.Attempts != 2 || result.Outcome != "transport_outcome_unknown" || hits.Load() != 1 {
		t.Fatal(result, err)
	}
}

func TestPlanDoesNotExportSensitiveMessage(t *testing.T) {
	body := []byte("private-body")
	row := Snapshot{DeliveryID: digest(body), BodySHA256: digest(body), Body: body, URL: "http://localhost/webhook", Signature: strings.Repeat("f", 64), State: "held", Contract: "audit"}
	p, err := makePlan(row, strings.Repeat("a", 64), strings.Repeat("b", 32), "retry_once")
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(p)
	for _, secret := range []string{string(body), row.URL, row.Signature} {
		if strings.Contains(string(raw), secret) {
			t.Fatal("sensitive plan output")
		}
	}
}

func TestPrivateInputsRejectDuplicateAndAliases(t *testing.T) {
	path := filepath.Join(t.TempDir(), "approval.json")
	for _, raw := range []string{`{"mode":"isolation","mode":"production"}`, `{"Mode":"isolation"}`, `{"mode":"isolation"}{}`, `{"mode":"isolation","extra":true}`} {
		if err := os.WriteFile(path, []byte(raw), 0600); err != nil {
			t.Fatal(err)
		}
		var approval Approval
		if readPrivate(path, &approval) == nil {
			t.Fatal("ambiguous input accepted", raw)
		}
	}
	if err := os.WriteFile(path, []byte(`{"mode":"isolation"}`), 0600); err != nil {
		t.Fatal(err)
	}
	var approval Approval
	if err := readPrivate(path, &approval); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, 0644); err != nil {
		t.Fatal(err)
	}
	if readPrivate(path, &approval) == nil {
		t.Fatal("public approval accepted")
	}
}

func TestPostgresHeldActualTimeoutStaysHeld(t *testing.T) {
	var hits atomic.Int32
	release := make(chan struct{})
	server := isolationServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		_, _ = io.Copy(io.Discard, r.Body)
		<-release
	}))
	defer func() { close(release); server.Close() }()
	f := newFixture(t, server.URL+"/webhook", 3, time.Hour)
	p, pf, a := f.approved(t, "retry_once")
	started := time.Now()
	result, err := execute(f.db, p, pf, a)
	elapsed := time.Since(started)
	if err != nil || result.State != "held" || result.Attempts != 4 || result.Outcome != "transport_outcome_unknown" || hits.Load() != 1 || elapsed < 9*time.Second || elapsed > 13*time.Second {
		t.Fatal("not one fixed-budget timeout", result, err, elapsed)
	}
}

func TestPostgresHeldACKBeforeFinalAuditFailure(t *testing.T) {
	var hits atomic.Int32
	server := isolationServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		body, _ := io.ReadAll(r.Body)
		w.WriteHeader(202)
		fmt.Fprint(w, receipt(digest(body)))
	}))
	defer server.Close()
	f := newFixture(t, server.URL+"/webhook", 3, time.Hour)
	_, err := f.db.Exec(`CREATE FUNCTION reject_finish_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.phase='finished' THEN RAISE EXCEPTION 'synthetic finish storage failure'; END IF; RETURN NEW; END $$; CREATE TRIGGER reject_finish_audit BEFORE INSERT ON held_isolation_audit FOR EACH ROW EXECUTE FUNCTION reject_finish_audit()`)
	if err != nil {
		t.Fatal(err)
	}
	p, pf, a := f.approved(t, "retry_once")
	if _, err = execute(f.db, p, pf, a); err == nil || hits.Load() != 1 {
		t.Fatal("uncommitted ACK reported successful")
	}
	row, _ := readSnapshot(context.Background(), f.db, f.row.DeliveryID, false)
	if row.State != "held" || row.Attempts != 4 || row.Lease != "manual:"+p.OperationID {
		t.Fatal("failed finish erased quarantine")
	}
	if _, err = execute(f.db, p, pf, a); err == nil || hits.Load() != 1 {
		t.Fatal("failed finalization blindly retried")
	}
	var count int
	f.db.QueryRow(`SELECT count(*) FROM held_isolation_audit`).Scan(&count)
	if count != 1 {
		t.Fatal("claim audit lost")
	}
}

func TestPostgresHeldReceiverIdentityGate(t *testing.T) {
	var postHits atomic.Int32
	var drift atomic.Bool
	server := isolationServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { postHits.Add(1); w.WriteHeader(503) }))
	defer server.Close()
	f := newFixture(t, server.URL+"/webhook", 1, time.Hour)
	// Approved receiver identity changes in the proof AND marker. This does not
	// make the actual HTTP fixture another receiver; no original bytes may leave.
	f.proof.ReceiverIdentity = digest([]byte("different-fixture"))
	drift.Store(true)
	if _, err := f.db.Exec(`UPDATE held_isolation_marker SET receiver_identity=$1`, f.proof.ReceiverIdentity); err != nil {
		t.Fatal(err)
	}
	p, pf, a := f.approved(t, "retry_once")
	result, err := execute(f.db, p, pf, a)
	if err != nil || result.State != "held" || result.HTTPRequests != 0 || result.ProbeRequests != 1 || postHits.Load() != 0 || !drift.Load() {
		t.Fatal("unbound receiver got raw message", result, err)
	}
}
