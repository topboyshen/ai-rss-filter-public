// SPDX-License-Identifier: Apache-2.0
package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type accessFixture struct {
	fixture
	s                  string
	reviewer, executor *sql.DB
}

func newAccessFixture(t *testing.T, target string, attempts int, age time.Duration) accessFixture {
	t.Helper()
	f := newFixture(t, target, attempts, age)
	suffix := fmt.Sprintf("%016x", time.Now().UnixNano())
	s := "held_access_isolation_" + suffix
	values := map[string]string{"SCHEMA": s, "SOURCE": f.schema, "OWNER": "ha_owner_" + suffix, "REVIEWER": "ha_review_" + suffix, "EXECUTOR": "ha_exec_" + suffix, "AUDITOR": "ha_audit_" + suffix}
	dir := os.Getenv("HELD_ACCESS_SQL_DIR")
	if dir == "" {
		dir = "../held-access"
	}
	for _, name := range []string{"install.sql", "execution.sql"} {
		raw, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			t.Fatal(err)
		}
		text := string(raw)
		for key, value := range values {
			text = strings.ReplaceAll(text, "__"+key+"__", value)
		}
		if _, err = f.db.Exec(text); err != nil {
			t.Fatal(name, err)
		}
	}
	_, err := f.db.Exec(`INSERT INTO `+s+`.control_marker(singleton,database_identity,run_id,manifest_sha256,adapter_identity,observed_at,producers_closed,sender_stopped,no_active_sender_leases,receiver_identity,endpoint_sha256) VALUES(true,$1,$2,$3,'held-access-synthetic-pg-v1',clock_timestamp(),true,true,true,$4,$5)`, f.proof.DatabaseIdentity, f.schema, f.proof.ManifestSHA256, f.proof.ReceiverIdentity, f.proof.EndpointSHA256)
	if err != nil {
		t.Fatal(err)
	}
	connect := func(kind, role string) *sql.DB {
		login := "ha_login_" + kind + "_" + suffix
		if _, err := f.db.Exec(`CREATE ROLE ` + login + ` LOGIN PASSWORD 'synthetic-ci-only' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; GRANT ` + values[role] + ` TO ` + login); err != nil {
			t.Fatal(err)
		}
		u, _ := url.Parse(f.dsn)
		u.User = url.UserPassword(login, "synthetic-ci-only")
		q := u.Query()
		q.Set("search_path", s)
		u.RawQuery = q.Encode()
		db, err := openAccessIsolation(u.String(), "isolation")
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { db.Close() })
		return db
	}
	return accessFixture{f, s, connect("review", "REVIEWER"), connect("execute", "EXECUTOR")}
}

func (f accessFixture) approve(t *testing.T, operation, action string) AccessApproval {
	t.Helper()
	var plan, proof json.RawMessage
	err := f.reviewer.QueryRow(`SELECT make_plan($1,$2,$3)`, f.row.DeliveryID, action, operation).Scan(&plan)
	if err != nil {
		t.Fatal(err)
	}
	err = f.db.QueryRow(`SELECT jsonb_build_object('schema',1,'mode','isolation','operation_id',$1::text,'database_identity',database_identity,'run_id',run_id,'manifest_sha256',manifest_sha256,'adapter_identity',adapter_identity,'observed_at',observed_at,'producers_closed',true,'sender_stopped',true,'no_active_sender_leases',true,'stopped_operation_id',stopped_operation_id,'receiver_identity',receiver_identity,'endpoint_sha256',endpoint_sha256) FROM `+f.s+`.control_marker`, operation).Scan(&proof)
	if err != nil {
		t.Fatal(err)
	}
	var raw []byte
	err = f.reviewer.QueryRow(`SELECT execution_approve($1,$2,$3)`, string(plan), string(proof), digest([]byte("reviewed original; unknown execution risk accepted"))).Scan(&raw)
	if err != nil {
		t.Fatal(err)
	}
	var approval AccessApproval
	if err = json.Unmarshal(raw, &approval); err != nil {
		t.Fatal(err)
	}
	return approval
}

func TestAccessProductionAndWrongSchemaDenied(t *testing.T) {
	for _, mode := range []string{"", "production"} {
		if db, err := openAccessIsolation("postgres://localhost/synthetic_outbox?search_path=held_access_isolation_0123456789abcdef", mode); err == nil {
			db.Close()
			t.Fatal("production reachable")
		}
	}
	if db, err := openAccessIsolation("postgres://remote/synthetic_outbox?search_path=held_access_isolation_0123456789abcdef", "isolation"); err == nil {
		db.Close()
		t.Fatal("remote reachable")
	}
}

func TestPostgresAccessRealRolesExactOneHTTPAndQuery(t *testing.T) {
	var hits atomic.Int32
	var original, signature string
	server := isolationServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		raw, _ := io.ReadAll(r.Body)
		original = string(raw)
		signature = r.Header.Get("X-Miniflux-Signature")
		w.WriteHeader(202)
		fmt.Fprint(w, receipt(digest(raw)))
	}))
	defer server.Close()
	f := newAccessFixture(t, server.URL+"/webhook", 19, time.Hour)
	a := f.approve(t, strings.Repeat("a", 32), "retry_once")
	for _, db := range []*sql.DB{f.reviewer, f.executor} {
		for _, sql := range []string{`UPDATE ` + f.schema + `.webhook_outbox SET attempts=0`, `DELETE FROM ` + f.s + `.execution_claims`, `SELECT body FROM ` + f.schema + `.webhook_outbox`, `UPDATE ` + f.schema + `.webhook_outbox SET target_url='changed'`, `ALTER TABLE ` + f.schema + `.webhook_outbox DISABLE TRIGGER ALL`} {
			if _, err := db.Exec(sql); err == nil {
				t.Fatal("direct privilege leak", sql)
			}
		}
	}
	if _, err := accessExecute(f.reviewer, a); err == nil || hits.Load() != 0 {
		t.Fatal("reviewer executed")
	}
	result, err := accessExecute(f.executor, a)
	if err != nil {
		t.Fatal(err)
	}
	var value Result
	json.Unmarshal(result, &value)
	if value.State != "acked" || value.Attempts != 20 || hits.Load() != 1 || original != string(f.row.Body) || signature != f.row.Signature {
		t.Fatal("not exact durable execution", string(result))
	}
	if _, err = accessExecute(f.executor, a); err == nil || hits.Load() != 1 {
		t.Fatal("duplicate resent")
	}
	query, err := accessQuery(f.executor, a.Operation)
	if err != nil || string(query) != string(result) || hits.Load() != 1 {
		t.Fatal("query is not exact no-HTTP", err)
	}
	row, _ := readSnapshot(context.Background(), f.db, f.row.DeliveryID, false)
	if !row.Created.Equal(f.row.Created) || row.URL != f.row.URL || row.Signature != f.row.Signature {
		t.Fatal("original mutated")
	}
}

func TestPostgresAccessConcurrencyDriftAndOriginalBudget(t *testing.T) {
	var hits atomic.Int32
	server := isolationServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { hits.Add(1); w.WriteHeader(503) }))
	defer server.Close()
	f := newAccessFixture(t, server.URL+"/webhook", 3, time.Hour)
	a := f.approve(t, strings.Repeat("a", 32), "retry_once")
	f.db.Exec(`UPDATE ` + f.schema + `.webhook_outbox SET attempts=4`)
	if _, err := accessExecute(f.executor, a); err == nil || hits.Load() != 0 {
		t.Fatal("stale approval sent")
	}
	f.db.Exec(`UPDATE ` + f.schema + `.webhook_outbox SET attempts=3`)
	var successes atomic.Int32
	var group sync.WaitGroup
	for i := 0; i < 5; i++ {
		group.Add(1)
		go func() {
			defer group.Done()
			if _, err := accessExecute(f.executor, a); err == nil {
				successes.Add(1)
			}
		}()
	}
	group.Wait()
	if successes.Load() != 1 || hits.Load() != 1 {
		t.Fatal("multiple execution", successes.Load(), hits.Load())
	}
	row, _ := readSnapshot(context.Background(), f.db, f.row.DeliveryID, false)
	if row.Attempts != 4 || row.State != "held" || row.Lease != "manual:"+a.Operation {
		t.Fatal("failed operation auto-released", row)
	}
	// Another approved retry must not turn held back into automatic pending.
	var plan string
	if err := f.reviewer.QueryRow(`SELECT make_plan($1,'retry_once',$2)`, f.row.DeliveryID, strings.Repeat("b", 32)).Scan(&plan); err != nil {
		t.Fatal(err)
	}
	if _, err := f.reviewer.Exec(`SELECT execution_approve($1,'{}',$2)`, plan, digest([]byte("note"))); err == nil {
		t.Fatal("manual lease bypassed")
	}
}

func TestPostgresAccessClaimCrashAndFailedFinalAuditNeverResend(t *testing.T) {
	var hits atomic.Int32
	server := isolationServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		raw, _ := io.ReadAll(r.Body)
		w.WriteHeader(202)
		fmt.Fprint(w, receipt(digest(raw)))
	}))
	defer server.Close()
	f := newAccessFixture(t, server.URL+"/webhook", 3, time.Hour)
	a := f.approve(t, strings.Repeat("a", 32), "retry_once")
	conn, _ := f.executor.Conn(context.Background())
	if err := accessClaim(conn, a); err != nil {
		t.Fatal(err)
	}
	conn.Close()
	if _, err := accessExecute(f.executor, a); err == nil || hits.Load() != 0 {
		t.Fatal("claimed crash resent")
	}
	q, err := accessQuery(f.executor, a.Operation)
	if err != nil || !strings.Contains(string(q), "claimed_outcome_unknown") {
		t.Fatal(string(q), err)
	}
	f2 := newAccessFixture(t, server.URL+"/webhook", 3, time.Hour)
	a2 := f2.approve(t, strings.Repeat("b", 32), "retry_once")
	_, err = f2.db.Exec(`CREATE FUNCTION ` + f2.s + `.reject_finish() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic EIO';END $$;CREATE TRIGGER reject_finish BEFORE INSERT ON ` + f2.s + `.execution_results FOR EACH ROW EXECUTE FUNCTION ` + f2.s + `.reject_finish()`)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = accessExecute(f2.executor, a2); err == nil || hits.Load() != 1 {
		t.Fatal("uncommitted ACK claimed")
	}
	if _, err = accessExecute(f2.executor, a2); err == nil || hits.Load() != 1 {
		t.Fatal("ACK loss resent")
	}
	row, _ := readSnapshot(context.Background(), f2.db, f2.row.DeliveryID, false)
	if row.State != "held" || row.Attempts != 4 || row.Lease != "manual:"+a2.Operation {
		t.Fatal("claim rolled back with final transaction")
	}
}

func TestPostgresAccessNetworkHoldsOriginalAndMarkerLocks(t *testing.T) {
	entered, release := make(chan struct{}), make(chan struct{})
	server := isolationServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		close(entered)
		<-release
		w.WriteHeader(202)
	}))
	defer server.Close()
	f := newAccessFixture(t, server.URL+"/webhook", 3, time.Hour)
	a := f.approve(t, strings.Repeat("a", 32), "retry_once")
	result := make(chan error, 1)
	go func() { _, err := accessExecute(f.executor, a); result <- err }()
	select {
	case <-entered:
	case <-time.After(3 * time.Second):
		close(release)
		t.Fatal("HTTP did not start")
	}
	for _, query := range []string{`UPDATE ` + f.schema + `.webhook_outbox SET target_url='changed'`, `UPDATE ` + f.s + `.control_marker SET sender_stopped=false`} {
		_, err := f.db.Exec(`SET lock_timeout='50ms';` + query)
		if err == nil {
			close(release)
			t.Fatal("HTTP ran without lock")
		}
	}
	close(release)
	if err := <-result; err != nil {
		t.Fatal(err)
	}
	row, _ := readSnapshot(context.Background(), f.db, f.row.DeliveryID, false)
	if row.State != "held" || row.Lease != "manual:"+a.Operation {
		t.Fatal("ordinary202 acknowledged")
	}
}

func TestPostgresAccessExhaustedInterruptedClaimOnlyExplicitReceiptRecovery(t *testing.T) {
	var hits atomic.Int32
	server := isolationServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		if r.URL.Path != "/webhook/receipt" {
			t.Error("receipt recovery resent original webhook")
		}
		body, _ := io.ReadAll(r.Body)
		fmt.Fprint(w, receipt(digest(body)))
	}))
	defer server.Close()
	f := newAccessFixture(t, server.URL+"/webhook", 19, time.Hour)
	a := f.approve(t, strings.Repeat("a", 32), "retry_once")
	conn, _ := f.executor.Conn(context.Background())
	if err := accessClaim(conn, a); err != nil {
		t.Fatal(err)
	}
	conn.Close()
	// Simulate the explicitly stopped previous operation, not timeout implying
	// it stopped. The fixture has no real sender/producer process.
	if _, err := f.db.Exec(`UPDATE `+f.s+`.control_marker SET stopped_operation_id=$1`, a.Operation); err != nil {
		t.Fatal(err)
	}
	b := f.approve(t, strings.Repeat("b", 32), "confirm_receipt")
	raw, err := accessExecute(f.executor, b)
	if err != nil {
		t.Fatal(err)
	}
	var result Result
	json.Unmarshal(raw, &result)
	if result.State != "acked" || result.Attempts != 20 || hits.Load() != 1 {
		t.Fatal("receipt changed budget", string(raw))
	}
	query, err := accessQuery(f.executor, a.Operation)
	if err != nil || !strings.Contains(string(query), "claimed_outcome_unknown") {
		t.Fatal("old uncertainty rewritten", string(query), err)
	}
}

func TestPostgresAccessAgeBudgetStopDriftAndAuditCapacity(t *testing.T) {
	server := isolationServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { t.Error("rejected plan sent HTTP") }))
	defer server.Close()
	f := newAccessFixture(t, server.URL+"/webhook", 20, 8*24*time.Hour)
	var plan, proof json.RawMessage
	if err := f.reviewer.QueryRow(`SELECT make_plan($1,'retry_once',$2)`, f.row.DeliveryID, strings.Repeat("a", 32)).Scan(&plan); err != nil {
		t.Fatal(err)
	}
	// Use complete marker proof, so rejection actually exercises original budget.
	if err := f.db.QueryRow(`SELECT jsonb_build_object('schema',1,'mode','isolation','operation_id',$1::text,'database_identity',database_identity,'run_id',run_id,'manifest_sha256',manifest_sha256,'adapter_identity',adapter_identity,'observed_at',observed_at,'producers_closed',true,'sender_stopped',true,'no_active_sender_leases',true,'stopped_operation_id',stopped_operation_id,'receiver_identity',receiver_identity,'endpoint_sha256',endpoint_sha256) FROM `+f.s+`.control_marker`, strings.Repeat("a", 32)).Scan(&proof); err != nil {
		t.Fatal(err)
	}
	if _, err := f.reviewer.Exec(`SELECT execution_approve($1,$2,$3)`, string(plan), string(proof), digest([]byte("note"))); err == nil {
		t.Fatal("20/7day budget ignored")
	}
	f2 := newAccessFixture(t, server.URL+"/webhook", 3, time.Hour)
	a := f2.approve(t, strings.Repeat("b", 32), "retry_once")
	conn, _ := f2.executor.Conn(context.Background())
	defer conn.Close()
	if err := accessClaim(conn, a); err != nil {
		t.Fatal(err)
	}
	if _, err := f2.db.Exec(`UPDATE ` + f2.s + `.control_marker SET sender_stopped=false`); err != nil {
		t.Fatal(err)
	}
	if _, err := accessExchangeLocked(conn, a.Operation); err == nil {
		t.Fatal("earlier stop proof used outside execution lock")
	}
	if _, err := f.db.Exec(`INSERT INTO ` + f.s + `.execution_approvals(operation_id,plan,proof,note_sha256,plan_sha256,proof_sha256,reviewer_name) SELECT md5(i::text),'{}','{}','x','x','x',session_user FROM generate_series(1,1365)i`); err != nil {
		t.Fatal(err)
	}
	// confirm_receipt is not blocked by 20/7d, but still needs audit capacity.
	if err := f.reviewer.QueryRow(`SELECT make_plan($1,'confirm_receipt',$2)`, f.row.DeliveryID, strings.Repeat("a", 32)).Scan(&plan); err != nil {
		t.Fatal(err)
	}
	if _, err := f.reviewer.Exec(`SELECT execution_approve($1,$2,$3)`, string(plan), string(proof), digest([]byte("note"))); err == nil {
		t.Fatal("audit full allowed")
	}
}

func TestPostgresAccessActualTimeoutAndUnknownExecutionRemainQuarantined(t *testing.T) {
	var hits atomic.Int32
	release := make(chan struct{})
	server := isolationServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { hits.Add(1); io.Copy(io.Discard, r.Body); <-release }))
	defer func() { close(release); server.Close() }()
	f := newAccessFixture(t, server.URL+"/webhook", 2, time.Hour)
	a := f.approve(t, strings.Repeat("a", 32), "retry_once")
	started := time.Now()
	raw, err := accessExecute(f.executor, a)
	elapsed := time.Since(started)
	if err != nil {
		t.Fatal(err)
	}
	var result Result
	json.Unmarshal(raw, &result)
	if result.Outcome != "transport_outcome_unknown" || result.State != "held" || result.Attempts != 3 || hits.Load() != 1 || elapsed < 9*time.Second || elapsed > 15*time.Second {
		t.Fatal(string(raw), elapsed)
	}
	if _, err = accessExecute(f.executor, a); err == nil || hits.Load() != 1 {
		t.Fatal("unknown execution resent")
	}
}
