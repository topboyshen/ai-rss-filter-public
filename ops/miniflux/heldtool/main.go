// SPDX-License-Identifier: Apache-2.0
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"reflect"
	"time"
)

// Input files must be private, regular, bounded and contain one strict object.
// No credentials in command arguments; database DSN comes from the environment.
func readPrivate(path string, target any) error {
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 || info.Size() > 65536 {
		return errors.New("private regular bounded JSON file required")
	}
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()
	actual, err := file.Stat()
	if err != nil || !os.SameFile(info, actual) {
		return errors.New("input file identity changed")
	}
	raw, err := io.ReadAll(io.LimitReader(file, 65537))
	if err != nil {
		return err
	}
	if len(raw) > 65536 {
		return errors.New("input grew beyond bound")
	}
	allowed := map[string]bool{}
	typ := reflect.TypeOf(target).Elem()
	for i := 0; i < typ.NumField(); i++ {
		allowed[typ.Field(i).Tag.Get("json")] = true
	}
	keys := json.NewDecoder(bytes.NewReader(raw))
	first, err := keys.Token()
	if err != nil || first != json.Delim('{') {
		return errors.New("object required")
	}
	seen := map[string]bool{}
	for keys.More() {
		key, err := keys.Token()
		name, ok := key.(string)
		if err != nil || !ok || seen[name] || !allowed[name] {
			return errors.New("duplicate or noncanonical input key")
		}
		seen[name] = true
		var value json.RawMessage
		if keys.Decode(&value) != nil {
			return errors.New("invalid input value")
		}
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err = dec.Decode(target); err != nil {
		return err
	}
	var tail any
	if dec.Decode(&tail) != io.EOF {
		return errors.New("one JSON object required")
	}
	return nil
}

func run() error {
	accessMode := flag.String("access-mode", "", "isolation only; no production access adapter")
	accessApproval := flag.String("access-approval", "", "private exact separated SQL approval receipt; one execution")
	accessQuery := flag.String("access-query", "", "query an original SQL execution operation; no HTTP or resend")
	action := flag.String("action", "", "plan action: retry_once or confirm_receipt")
	id := flag.String("delivery-id", "", "exact original delivery SHA256")
	op := flag.String("operation-id", "", "reviewer-selected unique 32 lowercase hex ID")
	database := flag.String("database-identity", "", "isolated database marker identity")
	planFile := flag.String("plan", "", "private approved plan JSON")
	proofFile := flag.String("proof", "", "private stopped isolation proof JSON")
	approvalFile := flag.String("approval", "", "private explicit approval JSON")
	execFlag := flag.Bool("execute", false, "isolation-only CAS and one bounded HTTP operation; never production")
	reviewFlag := flag.Bool("review", false, "offline: show exact plan/proof approval hashes; no database or HTTP")
	flag.Parse()
	if *accessMode != "" || *accessApproval != "" || *accessQuery != "" {
		if *execFlag || *reviewFlag || *action != "" || *id != "" || *op != "" || *database != "" || *planFile != "" || *proofFile != "" || *approvalFile != "" {
			return errors.New("access execution cannot mix legacy commands")
		}
		return runAccess(*accessMode, *accessApproval, *accessQuery)
	}
	if *reviewFlag {
		if *execFlag {
			return errors.New("review and execute are mutually exclusive")
		}
		var plan Plan
		var proof Proof
		if err := readPrivate(*planFile, &plan); err != nil {
			return err
		}
		if err := readPrivate(*proofFile, &proof); err != nil {
			return err
		}
		if plan.Mode != "isolation" || proof.Mode != "isolation" || plan.OperationID != proof.OperationID {
			return errors.New("isolation identities required")
		}
		return json.NewEncoder(os.Stdout).Encode(map[string]any{"execution": false, "approval_template": Approval{"isolation", plan.OperationID, objectHash(plan), objectHash(proof), false, false}})
	}
	db, err := openIsolation(os.Getenv("HELD_ISOLATION_DSN"))
	if err != nil {
		return err
	}
	defer db.Close()
	if !*execFlag {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		row, err := readSnapshot(ctx, db, *id, false)
		if err != nil {
			return err
		}
		plan, err := makePlan(row, *database, *op, *action)
		if err != nil {
			return err
		}
		return json.NewEncoder(os.Stdout).Encode(map[string]any{"plan": plan, "plan_sha256": objectHash(plan), "execution": false})
	}
	var plan Plan
	var proof Proof
	var approval Approval
	if err = readPrivate(*planFile, &plan); err != nil {
		return err
	}
	if err = readPrivate(*proofFile, &proof); err != nil {
		return err
	}
	if err = readPrivate(*approvalFile, &approval); err != nil {
		return err
	}
	result, err := execute(db, plan, proof, approval)
	if err != nil {
		return err
	}
	return json.NewEncoder(os.Stdout).Encode(result)
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, safeError(err))
		os.Exit(2)
	}
}
