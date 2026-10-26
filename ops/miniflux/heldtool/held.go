// SPDX-License-Identifier: Apache-2.0
// This tool has NO production adapter. All mutations require a separately
// provisioned, locally stopped synthetic PostgreSQL fixture and exact approval.
package main

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"regexp"
	"strings"
	"time"

	_ "github.com/lib/pq"
)

const auditMaxRows = 10000
const auditMaxBytes = 16 * 1024 * 1024

var schemaPattern = regexp.MustCompile(`^held_isolation_[a-f0-9]{16,32}$`)
var identityPattern = regexp.MustCompile(`^[a-f0-9]{64}$`)
var operationPattern = regexp.MustCompile(`^[a-f0-9]{32}$`)

type Snapshot struct {
	DeliveryID string    `json:"delivery_id"`
	BodySHA256 string    `json:"body_sha256"`
	UserID     int64     `json:"user_id"`
	FeedID     int64     `json:"feed_id"`
	EntryID    int64     `json:"entry_id"`
	URL        string    `json:"-"`
	Signature  string    `json:"-"`
	Contract   string    `json:"contract"`
	Body       []byte    `json:"-"`
	State      string    `json:"state"`
	Attempts   int       `json:"attempts"`
	Created    time.Time `json:"created_at"`
	Lease      string    `json:"manual_lease"`
}

// Plans contain only hashes of sensitive bytes. Reading the original row is
// necessary; a body reconstructed from the current entry is never accepted.
type Plan struct {
	Schema           int       `json:"schema"`
	Mode             string    `json:"mode"`
	OperationID      string    `json:"operation_id"`
	Action           string    `json:"action"`
	DeliveryID       string    `json:"delivery_id"`
	SnapshotSHA256   string    `json:"snapshot_sha256"`
	BodySHA256       string    `json:"body_sha256"`
	EndpointSHA256   string    `json:"endpoint_sha256"`
	SignatureSHA256  string    `json:"signature_sha256"`
	Contract         string    `json:"contract"`
	Attempts         int       `json:"attempts"`
	Created          time.Time `json:"created_at"`
	DatabaseIdentity string    `json:"database_identity"`
}

type Proof struct {
	Schema               int       `json:"schema"`
	Mode                 string    `json:"mode"`
	RunID                string    `json:"run_id"`
	OperationID          string    `json:"operation_id"`
	ManifestSHA256       string    `json:"manifest_sha256"`
	ObservedAt           time.Time `json:"observed_at"`
	ProducersClosed      bool      `json:"producers_closed"`
	SenderStopped        bool      `json:"sender_stopped"`
	NoActiveSenderLeases bool      `json:"no_active_sender_leases"`
	DatabaseIdentity     string    `json:"database_identity"`
	AdapterIdentity      string    `json:"adapter_identity"`
	StoppedOperationID   string    `json:"stopped_operation_id"`
	ReceiverIdentity     string    `json:"receiver_identity"`
	EndpointSHA256       string    `json:"endpoint_sha256"`
}

type Approval struct {
	Mode                         string `json:"mode"`
	OperationID                  string `json:"operation_id"`
	PlanSHA256                   string `json:"plan_sha256"`
	ProofSHA256                  string `json:"proof_sha256"`
	OriginalIdentityReviewed     bool   `json:"original_identity_reviewed"`
	UnknownExecutionRiskAccepted bool   `json:"unknown_execution_risk_accepted"`
}

type Result struct {
	OperationID    string `json:"operation_id"`
	DeliveryID     string `json:"delivery_id"`
	State          string `json:"state"`
	Attempts       int    `json:"attempts"`
	Outcome        string `json:"outcome"`
	ResponseSHA256 string `json:"response_sha256"`
	HTTPRequests   int    `json:"original_http_attempts"`
	ProbeRequests  int    `json:"isolation_probe_http_attempts"`
}

func digest(raw []byte) string    { value := sha256.Sum256(raw); return hex.EncodeToString(value[:]) }
func objectHash(value any) string { raw, _ := json.Marshal(value); return digest(raw) }
func snapshotHash(row Snapshot) string {
	return objectHash(struct {
		Snapshot
		URL       string
		Signature string
		Body      []byte
	}{row, row.URL, row.Signature, row.Body})
}

func openIsolation(dsn string) (*sql.DB, error) {
	u, err := url.Parse(dsn)
	if err != nil || u.Scheme != "postgres" || (u.Hostname() != "127.0.0.1" && u.Hostname() != "localhost") || u.Path != "/synthetic_outbox" || !schemaPattern.MatchString(u.Query().Get("search_path")) {
		return nil, errors.New("isolation only: local synthetic_outbox and private held schema required")
	}
	q := u.Query()
	for key := range q {
		if key != "search_path" && key != "sslmode" {
			return nil, errors.New("unexpected database connection option")
		}
	}
	q.Set("connect_timeout", "3")
	q.Set("application_name", "held_isolation_review")
	u.RawQuery = q.Encode()
	db, err := sql.Open("postgres", u.String())
	if err == nil {
		db.SetMaxOpenConns(1)
	}
	return db, err
}

type rowReader interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}

func readSnapshot(ctx context.Context, db rowReader, id string, lock bool) (Snapshot, error) {
	var row Snapshot
	query := `SELECT delivery_id,body_sha256,user_id,feed_id,entry_id,target_url,signature,contract,body,state,attempts,created_at,coalesce(lease_token,'') FROM webhook_outbox WHERE delivery_id=$1`
	if lock {
		query += ` FOR UPDATE`
	}
	err := db.QueryRowContext(ctx, query, id).Scan(&row.DeliveryID, &row.BodySHA256, &row.UserID, &row.FeedID, &row.EntryID, &row.URL, &row.Signature, &row.Contract, &row.Body, &row.State, &row.Attempts, &row.Created, &row.Lease)
	return row, err
}

func makePlan(row Snapshot, database, operation, action string) (Plan, error) {
	if row.State != "held" || !identityPattern.MatchString(row.DeliveryID) || digest(row.Body) != row.DeliveryID || row.BodySHA256 != row.DeliveryID || !operationPattern.MatchString(operation) || !identityPattern.MatchString(database) || row.Contract == "" || (action != "retry_once" && action != "confirm_receipt") {
		return Plan{}, errors.New("exact held identity, operation and action required")
	}
	if row.Lease != "" && (action != "confirm_receipt" || !strings.HasPrefix(row.Lease, "manual:")) {
		return Plan{}, errors.New("unresolved claim: receipt-only explicit recovery required")
	}
	return Plan{1, "isolation", operation, action, row.DeliveryID, snapshotHash(row), row.BodySHA256, digest([]byte(row.URL)), digest([]byte(row.Signature)), row.Contract, row.Attempts, row.Created, database}, nil
}

func validateApproval(plan Plan, proof Proof, approval Approval, now time.Time) error {
	if plan.Schema != 1 || proof.Schema != 1 || plan.Mode != "isolation" || proof.Mode != "isolation" || approval.Mode != "isolation" {
		return errors.New("production execution is prohibited")
	}
	if !operationPattern.MatchString(plan.OperationID) || plan.OperationID != proof.OperationID || plan.OperationID != approval.OperationID || objectHash(plan) != approval.PlanSHA256 || objectHash(proof) != approval.ProofSHA256 || !approval.OriginalIdentityReviewed || !approval.UnknownExecutionRiskAccepted {
		return errors.New("exact reviewed plan and proof approval required")
	}
	if proof.RunID == "" || !identityPattern.MatchString(proof.ManifestSHA256) || !identityPattern.MatchString(proof.DatabaseIdentity) || plan.DatabaseIdentity != proof.DatabaseIdentity || proof.AdapterIdentity != "synthetic-pg-stopped-fixture-v1" || !proof.ProducersClosed || !proof.SenderStopped || !proof.NoActiveSenderLeases || now.Sub(proof.ObservedAt) > 60*time.Second || proof.ObservedAt.After(now.Add(time.Second)) {
		return errors.New("fresh stopped isolation proof required")
	}
	if !identityPattern.MatchString(proof.ReceiverIdentity) || !identityPattern.MatchString(proof.EndpointSHA256) || proof.EndpointSHA256 != plan.EndpointSHA256 {
		return errors.New("isolated receiver identity and endpoint required")
	}
	return nil
}

func beginDurable(ctx context.Context, db *sql.DB) (*sql.Tx, error) {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	if _, err = tx.ExecContext(ctx, `SET LOCAL synchronous_commit=on; SET LOCAL statement_timeout='3s'; SET LOCAL lock_timeout='3s'`); err != nil {
		tx.Rollback()
		return nil, err
	}
	var durable bool
	err = tx.QueryRowContext(ctx, `SELECT current_setting('fsync')='on' AND current_setting('full_page_writes')='on'`).Scan(&durable)
	if err != nil || !durable {
		tx.Rollback()
		return nil, errors.New("PostgreSQL durability settings required")
	}
	return tx, nil
}

func claim(db *sql.DB, plan Plan, proof Proof, approval Approval) (Snapshot, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := validateApproval(plan, proof, approval, time.Now().UTC()); err != nil {
		return Snapshot{}, err
	}
	tx, err := beginDurable(ctx, db)
	if err != nil {
		return Snapshot{}, err
	}
	defer tx.Rollback()
	var lock bool
	if err = tx.QueryRowContext(ctx, `SELECT pg_try_advisory_xact_lock(19500623)`).Scan(&lock); err != nil || !lock {
		return Snapshot{}, errors.New("maintenance capacity lock busy")
	}
	var identity, runID, manifest, adapter, stoppedOperation, receiverIdentity, endpointHash string
	var stopped bool
	var observed time.Time
	err = tx.QueryRowContext(ctx, `SELECT database_identity,run_id,manifest_sha256,adapter_identity,observed_at,producers_closed AND sender_stopped,stopped_operation_id,receiver_identity,endpoint_sha256 FROM held_isolation_marker WHERE singleton=true`).Scan(&identity, &runID, &manifest, &adapter, &observed, &stopped, &stoppedOperation, &receiverIdentity, &endpointHash)
	if err != nil || identity != proof.DatabaseIdentity || runID != proof.RunID || manifest != proof.ManifestSHA256 || adapter != proof.AdapterIdentity || !observed.Equal(proof.ObservedAt) || !stopped || stoppedOperation != proof.StoppedOperationID || receiverIdentity != proof.ReceiverIdentity || endpointHash != proof.EndpointSHA256 {
		return Snapshot{}, errors.New("actual isolated database marker does not match proof")
	}
	var active int
	err = tx.QueryRowContext(ctx, `SELECT count(*) FROM webhook_outbox WHERE state='inflight' OR (state='pending' AND lease_until>now())`).Scan(&active)
	if err != nil || active != 0 {
		return Snapshot{}, errors.New("automatic claims remain")
	}
	// A supporting check only: the fixture proof, not pg_stat_activity alone,
	// must attest that the enumerated external producers and sender are stopped.
	err = tx.QueryRowContext(ctx, `SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND backend_type='client backend' AND application_name<>'held_isolation_review'`).Scan(&active)
	if err != nil || active != 0 {
		return Snapshot{}, errors.New("other database clients remain")
	}
	row, err := readSnapshot(ctx, tx, plan.DeliveryID, true)
	if err != nil || row.State != "held" || snapshotHash(row) != plan.SnapshotSHA256 {
		return Snapshot{}, errors.New("held snapshot CAS failed")
	}
	if row.Lease != "" && proof.StoppedOperationID != strings.TrimPrefix(row.Lease, "manual:") {
		return Snapshot{}, errors.New("prior manual operation must be stopped before receipt-only recovery")
	}
	checked, err := makePlan(row, plan.DatabaseIdentity, plan.OperationID, plan.Action)
	if err != nil || checked != plan {
		return Snapshot{}, errors.New("plan identity changed")
	}
	if plan.Action == "retry_once" && (row.Attempts >= 20 || !row.Created.Add(7*24*time.Hour).After(time.Now())) {
		return Snapshot{}, errors.New("existing attempt or age budget exhausted")
	}
	var rows, bytes int64
	err = tx.QueryRowContext(ctx, `SELECT count(*) FILTER (WHERE phase='claimed')*2,count(*) FILTER (WHERE phase='claimed')*8192 FROM held_isolation_audit`).Scan(&rows, &bytes)
	if err != nil || rows+2 > auditMaxRows || bytes+8192 > auditMaxBytes {
		return Snapshot{}, errors.New("audit capacity unavailable")
	}
	// Reserve both records before any network. The finish event is appended only
	// after the response; unfinished operations keep their reservation forever.
	err = appendAudit(ctx, tx, plan, proof, "claimed", row.Attempts, "", "", 0, 0)
	if err != nil {
		return Snapshot{}, err
	}
	increment := 0
	if plan.Action == "retry_once" {
		increment = 1
	}
	_, err = tx.ExecContext(ctx, `UPDATE webhook_outbox SET attempts=attempts+$2,lease_token=$3,lease_until=NULL,last_error='manual_review_claimed' WHERE delivery_id=$1 AND state='held'`, row.DeliveryID, increment, "manual:"+plan.OperationID)
	if err != nil {
		return Snapshot{}, err
	}
	if err = tx.Commit(); err != nil {
		return Snapshot{}, err
	}
	row.Attempts += increment
	row.Lease = "manual:" + plan.OperationID
	return row, nil
}

func appendAudit(ctx context.Context, tx *sql.Tx, plan Plan, proof Proof, phase string, attempts int, outcome, responseHash string, requests, probes int) error {
	event := map[string]any{"operation_id": plan.OperationID, "delivery_id": plan.DeliveryID, "action": plan.Action, "plan_sha256": objectHash(plan), "proof_sha256": objectHash(proof), "attempts": attempts, "phase": phase, "outcome": outcome, "response_sha256": responseHash, "original_http_attempts": requests, "isolation_probe_http_attempts": probes}
	raw, _ := json.Marshal(event)
	_, err := tx.ExecContext(ctx, `INSERT INTO held_isolation_audit(operation_id,phase,event) VALUES($1,$2,$3::jsonb)`, plan.OperationID, phase, string(raw))
	return err
}

func finish(db *sql.DB, plan Plan, proof Proof, row Snapshot, outcome, responseHash string, acked bool, requests, probes int) (Result, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	tx, err := beginDurable(ctx, db)
	if err != nil {
		return Result{}, err
	}
	defer tx.Rollback()
	state := "held"
	if acked {
		state = "acked"
	}
	result, err := tx.ExecContext(ctx, `UPDATE webhook_outbox SET state=$2,lease_token=NULL,lease_until=NULL,acked_at=CASE WHEN $2='acked' THEN now() ELSE NULL END,last_error=$4 WHERE delivery_id=$1 AND state='held' AND lease_token=$3 AND attempts=$5`, row.DeliveryID, state, row.Lease, outcome, row.Attempts)
	if err != nil {
		return Result{}, err
	}
	count, err := result.RowsAffected()
	if err != nil || count != 1 {
		return Result{}, errors.New("manual finish CAS failed; retained for receipt-only recovery")
	}
	if err = appendAudit(ctx, tx, plan, proof, "finished", row.Attempts, outcome, responseHash, requests, probes); err != nil {
		return Result{}, err
	}
	if err = tx.Commit(); err != nil {
		return Result{}, err
	}
	return Result{plan.OperationID, row.DeliveryID, state, row.Attempts, outcome, responseHash, requests, probes}, nil
}

func execute(db *sql.DB, plan Plan, proof Proof, approval Approval) (Result, error) {
	row, err := claim(db, plan, proof, approval)
	if err != nil {
		return Result{}, err
	}
	verified, probes := probeReceiver(row, proof)
	if !verified {
		return finish(db, plan, proof, row, "isolation_receiver_unconfirmed", "", false, 0, probes)
	}
	acked, outcome, responseHash, requests := exchange(row, plan.Action)
	return finish(db, plan, proof, row, outcome, responseHash, acked, requests, probes)
}

func safeError(err error) string {
	if err == nil {
		return ""
	}
	return fmt.Sprintf("held review stopped (%T); no automatic retry", err)
}
