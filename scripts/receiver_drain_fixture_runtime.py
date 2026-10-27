"""Synthetic-only real Flask/ReceiptStore runtime for the native joint fixture.

The scorer is fake. No application DB initialization, real model, external feed
or notification is called. Never use this entry point for a real deployment.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import os
from pathlib import Path
import sqlite3
import threading
import time

from services import admission_fence as gate
from services import receiver_drain_storage as disk


def prepare_storage(root):
    """Create only a new fixture-owned SQLite watermark store and empty journal."""
    journal = disk.directory(root / "journal", create=True)
    disk.directory(journal / ".locks", create=True)
    disk.create_lock(journal / ".lock")
    path = root / "fixture.sqlite"
    gate._require(not path.exists(), "synthetic database already exists; preserve previous evidence")
    connection = sqlite3.connect(path)
    try:
        path.chmod(0o600)
        connection.execute("CREATE TABLE app_meta(k TEXT PRIMARY KEY,v TEXT NOT NULL)")
        connection.executemany("INSERT INTO app_meta VALUES(?,?)", [(key, "0") for key in (
            "dashboard_realtime_input_revision", "dashboard_realtime_published_revision", "dashboard_realtime_published_at")])
        connection.commit()
    finally:
        connection.close()
    gate._confirm_file(path)
    gate._sync_directory(root)


def synthetic_application(root, token, max_consume):
    from flask import jsonify, request
    from app import app
    from routes import webhook_routes as webhook
    from services import admission_http
    config = disk.confirm(root / "config.json")
    gate._require(config == {"scoring_strategy_version": "native-joint-synthetic-v1",
                            "feeds": [{"url": "http://fixture:8080/feed", "name": "Synthetic joint feed"}]},
                  "fixed synthetic fixture configuration required")
    webhook.stop_webhook_dispatcher(timeout=2)
    webhook._cfg = lambda: config
    webhook.resolve_scoring_contract = lambda value, **_kw: value
    webhook.Scorer = lambda: object()
    webhook.resolve_prompt = lambda *_a: ("default", "Synthetic; no model call")
    webhook._find_notify_threshold = lambda *_a: 99.0
    webhook._queue_committed_dashboard_inputs = lambda: None
    webhook.DB.set_app_meta = lambda *_a: None
    webhook.DB.update_feed_health = lambda **_kw: None
    calls, mutex = [], threading.Lock()

    def fake_score(entry, **kwargs):
        gate._require(kwargs["cfg"] == config and len(calls) < max_consume, "synthetic score budget exceeded")
        calls.append({"link_sha256": hashlib.sha256(entry.link.encode()).hexdigest(), "strategy": config["scoring_strategy_version"]})
        disk.write(root / "synthetic-scores.json", {"schema": 1, "calls": calls, "real_model_calls": 0})
        return None, "filtered"
    webhook.process_entry = fake_score
    webhook._ensure_spool_dir()
    webhook._receipt_store(webhook._webhook_spool_dir)

    @app.post("/__synthetic__/consume")
    def consume():
        gate._require(hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + token)
                      and request.get_data() == b"" and request.content_length in (None, 0), "synthetic consume authorization required")
        with mutex:
            gate._require(len(calls) < max_consume, "synthetic consume budget exhausted")
            with gate.admission("synthetic_native_consumer", kind="drain"):
                claimed = webhook._claim_next_spooled_task(now=time.time() + 1)
                if claimed:
                    path, envelope = claimed
                    gate._require(webhook._execute_claimed_spooled_task(path, envelope), "synthetic consumer failed")
                    gate._require(webhook._complete_spooled_task(path, envelope["digest"], envelope["owner"]), "completion barrier failed")
        return jsonify({"schema": 1, "consumed": bool(claimed), "calls": len(calls), "real_model_calls": 0})
    # Override only this process's synthetic route classification. The shipped
    # admission module has no fixture marker, exemption or caller boolean.
    actual_kind = admission_http.request_kind
    def fixture_kind():
        if request.method == "POST" and request.endpoint == "consume" and request.url_rule.rule == "/__synthetic__/consume":
            return "drain"
        return actual_kind()
    admission_http.request_kind = fixture_kind
    return app


def run(args):
    root = disk.directory(args.root)
    marker = disk.confirm(root / "synthetic-owner.json")
    gate._require(isinstance(marker, dict) and set(marker) == {"schema", "synthetic", "nonce"}
                  and marker["schema"] == 1 and marker["synthetic"] is True
                  and isinstance(marker["nonce"], str) and gate.HEX64.fullmatch(marker["nonce"]), "synthetic ownership marker required")
    gate._require(1 <= args.seconds <= 600 and 1 <= args.max_consume <= 128
                  and 1024 <= args.port <= 65535 and 1024 <= args.control_port <= 65535, "finite synthetic runtime required")
    gate._require(args.token_file == root / "token.json", "bound fixture token path required")
    token = disk.confirm(args.token_file)
    gate._require(set(token) == {"token"} and gate.HEX64.fullmatch(token["token"]), "private synthetic token required")
    gate._require(os.environ.get("WEBHOOK_SPOOL_DIR") == str(root / "spool")
                  and os.environ.get("AI_FILTER_ADMISSION_ROOT") == str(root / "fence"), "fixture storage paths differ")
    gate._require(os.environ.get("AI_FILTER_ROLE") == ("worker" if args.worker else "web"), "synthetic role differs")
    if args.worker:
        disk.once(root / "worker-ready.json", {"schema": 1, "synthetic": True, "role": "worker", "behavior": "bounded identity hold"})
        threading.Event().wait(args.seconds)
        return
    prepare_storage(root)
    app = synthetic_application(root, token["token"], args.max_consume)
    from services.receiver_drain_control import DrainServer, ReceiverDrainControl
    from services.receiver_drain_transfer import body_limit, config_sha256, sources
    from werkzeug.serving import make_server
    fence = gate._configured()
    gate._require(fence is not None, "synthetic enforced admission required")
    control = ReceiverDrainControl(fence)
    with DrainServer(control, token["token"], args.control_port) as server:
        http = make_server("0.0.0.0", args.port, app, threaded=True)
        threads = [threading.Thread(target=item.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
                   for item in (server, http)]
        for thread in threads:
            thread.start()
        disk.once(root / "ready.json", {"schema": 1, "synthetic": True, "registration": control.registration(),
            "body_limit": body_limit(), "config_sha256": config_sha256(), "source_sha256": sources(),
            "model": "fake", "application_http_port": args.port, "control_loopback_port": args.control_port})
        try:
            threading.Event().wait(args.seconds)
        finally:
            http.shutdown()
            http.server_close()
            server.shutdown()
            for thread in threads:
                thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--control-port", type=int, default=8766)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--max-consume", type=int, default=16)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
