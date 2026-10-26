// SPDX-License-Identifier: Apache-2.0
package main

import (
	"context"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/url"
	"os"
	"regexp"
	"time"
)

var accessSchema = regexp.MustCompile(`^held_access_isolation_[a-f0-9]{16,32}$`)

type AccessApproval struct {
	Operation string `json:"operation_id"`
	PlanHash  string `json:"plan_sha256"`
	ProofHash string `json:"proof_sha256"`
	NoteHash  string `json:"note_sha256"`
}

type accessPayload struct {
	Operation    string `json:"operation_id"`
	Delivery     string `json:"delivery_id"`
	BodyHash     string `json:"body_sha256"`
	BodyHex      string `json:"body_hex"`
	Endpoint     string `json:"endpoint"`
	Signature    string `json:"signature"`
	Action       string `json:"action"`
	Receiver     string `json:"receiver_identity"`
	EndpointHash string `json:"endpoint_sha256"`
	Attempts     int    `json:"attempts"`
	Lease        string `json:"lease"`
	SnapshotHash string `json:"snapshot_sha256"`
}

func openAccessIsolation(dsn, mode string) (*sql.DB, error) {
	u, err := url.Parse(dsn)
	if err != nil || mode != "isolation" || u.Scheme != "postgres" || (u.Hostname() != "127.0.0.1" && u.Hostname() != "localhost") || u.Path != "/synthetic_outbox" || !accessSchema.MatchString(u.Query().Get("search_path")) {
		return nil, errors.New("held access production adapter is not installed")
	}
	q := u.Query()
	for key := range q {
		if key != "search_path" && key != "sslmode" {
			return nil, errors.New("unexpected access DSN option")
		}
	}
	q.Set("connect_timeout", "3")
	q.Set("application_name", "held_access_isolation_executor")
	u.RawQuery = q.Encode()
	db, err := sql.Open("postgres", u.String())
	if err == nil {
		db.SetMaxOpenConns(1)
	}
	return db, err
}

func accessTx(ctx context.Context, conn *sql.Conn) (*sql.Tx, error) {
	tx, err := conn.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	_, err = tx.ExecContext(ctx, `SET LOCAL synchronous_commit=on; SET LOCAL statement_timeout='3s'; SET LOCAL lock_timeout='3s'; SET LOCAL idle_in_transaction_session_timeout='15s'`)
	if err != nil {
		tx.Rollback()
		return nil, err
	}
	return tx, nil
}

func accessClaim(conn *sql.Conn, approval AccessApproval) error {
	if !operationPattern.MatchString(approval.Operation) || !identityPattern.MatchString(approval.PlanHash) || !identityPattern.MatchString(approval.ProofHash) || !identityPattern.MatchString(approval.NoteHash) {
		return errors.New("exact access approval hashes required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	tx, err := accessTx(ctx, conn)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var safe json.RawMessage
	err = tx.QueryRowContext(ctx, `SELECT execution_claim($1,$2,$3,$4)`, approval.Operation, approval.PlanHash, approval.ProofHash, approval.NoteHash).Scan(&safe)
	if err != nil {
		return err
	}
	// The original budget claim must be committed before any network request.
	// An ambiguous commit is NOT retried: only accessQuery on this operation.
	return tx.Commit()
}

func accessExchangeLocked(conn *sql.Conn, operation string) (json.RawMessage, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	tx, err := accessTx(ctx, conn)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	var raw []byte
	if err = tx.QueryRowContext(ctx, `SELECT execution_begin($1)`, operation).Scan(&raw); err != nil {
		return nil, err
	}
	var payload accessPayload
	if err = json.Unmarshal(raw, &payload); err != nil {
		return nil, err
	}
	body, err := hex.DecodeString(payload.BodyHex)
	if err != nil || payload.Operation != operation || payload.Delivery != digest(body) || payload.BodyHash != payload.Delivery || payload.Lease != "manual:"+operation || !identityPattern.MatchString(payload.SnapshotHash) {
		return nil, errors.New("locked original identity invalid")
	}
	row := Snapshot{DeliveryID: payload.Delivery, BodySHA256: payload.BodyHash, Body: body, URL: payload.Endpoint, Signature: payload.Signature}
	// Both original row and actual stopped marker remain locked through HTTP
	// and final CAS. This function is called only after THIS invocation claimed.
	verified, probes := probeReceiver(row, Proof{ReceiverIdentity: payload.Receiver, EndpointSHA256: payload.EndpointHash})
	acked, outcome, responseHash, requests := false, "isolation_receiver_unconfirmed", "", 0
	if verified {
		acked, outcome, responseHash, requests = exchange(row, payload.Action)
	}
	var result json.RawMessage
	err = tx.QueryRowContext(ctx, `SELECT execution_finish($1,$2,$3,$4,$5,$6,$7)`, operation, payload.SnapshotHash, outcome, responseHash, acked, requests, probes).Scan(&result)
	if err != nil {
		return nil, err
	}
	if err = tx.Commit(); err != nil {
		return nil, err
	}
	return result, nil
}

func accessExecute(db *sql.DB, approval AccessApproval) (json.RawMessage, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	conn, err := db.Conn(ctx)
	if err != nil {
		return nil, err
	}
	defer conn.Close()
	if err = accessClaim(conn, approval); err != nil {
		return nil, err
	}
	return accessExchangeLocked(conn, approval.Operation)
}

func accessQuery(db *sql.DB, operation string) (json.RawMessage, error) {
	if !operationPattern.MatchString(operation) {
		return nil, errors.New("exact original operation required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	var result json.RawMessage
	err := db.QueryRowContext(ctx, `SELECT execution_query($1)`, operation).Scan(&result)
	return result, err
}

func runAccess(mode, approvalPath, query string) error {
	if (approvalPath == "") == (query == "") {
		return errors.New("exactly one access execute or query required")
	}
	db, err := openAccessIsolation(os.Getenv("HELD_ACCESS_ISOLATION_DSN"), mode)
	if err != nil {
		return err
	}
	defer db.Close()
	var result json.RawMessage
	if approvalPath != "" {
		var approval AccessApproval
		if err = readPrivate(approvalPath, &approval); err != nil {
			return err
		}
		result, err = accessExecute(db, approval)
	} else {
		result, err = accessQuery(db, query)
	}
	if err != nil {
		return err
	}
	return json.NewEncoder(os.Stdout).Encode(result)
}
