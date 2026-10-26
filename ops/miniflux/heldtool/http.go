// SPDX-License-Identifier: Apache-2.0
package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// This unauthenticated control probe sends neither original body nor HMAC.
// Loopback alone is NOT proof that a server is a synthetic fixture. The exact
// fixture identity must also match the approved plan and actual PG marker.
func probeReceiver(row Snapshot, proof Proof) (bool, int) {
	u, err := url.Parse(row.URL)
	if err != nil || u.Scheme != "http" || (u.Hostname() != "localhost" && u.Hostname() != "127.0.0.1") || u.User != nil || u.Path != "/webhook" || u.RawQuery != "" || u.Fragment != "" || digest([]byte(row.URL)) != proof.EndpointSHA256 {
		return false, 0
	}
	u.Path = "/__held_isolation_probe"
	client := &http.Client{Timeout: time.Second, Transport: &http.Transport{Proxy: nil, DisableKeepAlives: true, MaxResponseHeaderBytes: 8192}, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	defer client.CloseIdleConnections()
	res, err := client.Get(u.String())
	if err != nil {
		return false, 1
	}
	defer res.Body.Close()
	return res.StatusCode == http.StatusNoContent && res.Header.Get("X-Held-Isolation-Identity") == proof.ReceiverIdentity, 1
}

// Fixed 10 seconds, no transport retry, no redirect and no public target.
// A response hash is audit metadata; this tool does NOT retain the raw response.
func exchange(row Snapshot, action string) (bool, string, string, int) {
	u, err := url.Parse(row.URL)
	if err != nil || u.Scheme != "http" || (u.Hostname() != "localhost" && u.Hostname() != "127.0.0.1") || u.User != nil || u.Path != "/webhook" || u.RawQuery != "" || u.Fragment != "" || digest(row.Body) != row.DeliveryID || row.BodySHA256 != row.DeliveryID || len(row.Body) == 0 || len(row.Body) > 1048576 || !identityPattern.MatchString(row.Signature) {
		return false, "isolation_target_or_identity_rejected", "", 0
	}
	expected := http.StatusAccepted
	if action == "confirm_receipt" {
		u.Path = "/webhook/receipt"
		expected = http.StatusOK
	} else if action != "retry_once" {
		return false, "action_rejected", "", 0
	}
	req, err := http.NewRequest(http.MethodPost, u.String(), bytes.NewReader(row.Body))
	if err != nil {
		return false, "request_rejected", "", 0
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Miniflux-Signature", row.Signature)
	req.Header.Set("X-Miniflux-Event-Type", "new_entries")
	req.Header.Set("X-AI-Filter-Delivery-ID", row.DeliveryID)
	transport := &http.Transport{Proxy: nil, DisableKeepAlives: true}
	defer transport.CloseIdleConnections()
	client := &http.Client{Timeout: 10 * time.Second, Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	res, err := client.Do(req)
	if err != nil {
		return false, "transport_outcome_unknown", "", 1
	}
	defer res.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(res.Body, 4097))
	if err != nil {
		return false, "incomplete_receipt_unknown", digest(raw), 1
	}
	if res.StatusCode != expected || !validReceipt(raw, row.DeliveryID) {
		return false, "receipt_unconfirmed", digest(raw), 1
	}
	return true, "exact_durable_receipt", digest(raw), 1
}

func validReceipt(raw []byte, id string) bool {
	if len(raw) > 4096 {
		return false
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	start, err := decoder.Token()
	if err != nil || start != json.Delim('{') {
		return false
	}
	seen := map[string]bool{}
	values := map[string]json.RawMessage{}
	for decoder.More() {
		key, err := decoder.Token()
		name, ok := key.(string)
		if err != nil || !ok || seen[name] {
			return false
		}
		seen[name] = true
		for _, reserved := range []string{"schema", "durable", "body_sha256", "delivery_id"} {
			if name != reserved && strings.EqualFold(name, reserved) {
				return false
			}
		}
		var value json.RawMessage
		if decoder.Decode(&value) != nil {
			return false
		}
		values[name] = value
	}
	if end, err := decoder.Token(); err != nil || end != json.Delim('}') {
		return false
	}
	var trailing any
	if decoder.Decode(&trailing) != io.EOF {
		return false
	}
	var schema int
	var durable bool
	var body, delivery string
	return json.Unmarshal(values["schema"], &schema) == nil && schema == 1 && json.Unmarshal(values["durable"], &durable) == nil && durable && json.Unmarshal(values["body_sha256"], &body) == nil && body == id && json.Unmarshal(values["delivery_id"], &delivery) == nil && delivery == id
}
