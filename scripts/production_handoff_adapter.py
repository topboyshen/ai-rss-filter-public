"""Fail-closed production readiness adapter; executable file-backed isolation gate.

Production has no shared admission capability yet. This module never runs SSH,
Docker, shell commands, signals, a release, or a model. Isolation has real flock
admission/active-request exclusion, not caller-supplied 'closed' booleans.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from durable_handoff_cutover import durable_write, encoded, private_directory, read_private, require

SOURCE_REVISION = "a5ffd64fa987f3a64676478c05a212aa8146418a"
SOURCE_TREE = "9a9be370acbcf59a82ffbe13075f0529f4faafa1"
SOURCE_FILES = ("app.py", "docker-compose.yml", "routes/feed_routes.py", "routes/admin_routes.py",
                "routes/dashboard_routes.py", "routes/webhook_routes.py", "services/refresh.py",
                "worker_main.py", "scripts/release.sh", "scripts/cron_refresh.sh", "scripts/cron_event_enrichment.sh")
ENTRANCES = ("miniflux_scheduler", "miniflux_manual", "legacy_async_sender", "webhook", "webhook_bypass",
             "natural_cron", "manual_refresh", "filter", "filter_multi", "briefing_llm", "feedback",
             "internal_queue", "cron_maintenance", "outside_compose")
MAX_RECORDS = 512
MAX_BODY = 65536
MAX_LOCK_SECONDS = 2.0
MAX_GENERATION = 2**63 - 1
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class AdapterBlocked(RuntimeError):
    """Required real authority/evidence is absent; no durable stop was confirmed."""


class AdapterBusy(AdapterBlocked):
    """An existing request/lock owner must finish naturally; never killed."""


def sha(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def present(path):
    """Broken symlinks and other unconfirmed material also close admission."""
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def validate_generation(value):
    require(type(value) is int and 0 <= value <= MAX_GENERATION, "invalid generation")
    return value


def validate_state(value):
    require(isinstance(value, dict) and set(value) == {"generation", "entrances"}, "invalid gate state")
    validate_generation(value["generation"])
    entries = value["entrances"]
    require(isinstance(entries, dict) and set(entries) == set(ENTRANCES)
            and all(type(status) is str and status in {"open", "closed"} for status in entries.values())
            and len(set(entries.values())) == 1, "invalid or partial entrance state")
    return value


def bounded_paths(directory, limit):
    private_directory(directory)
    paths = []
    with os.scandir(directory) as entries:
        for entry in entries:
            require(len(paths) < limit, "raw inventory cap exceeded")
            paths.append(Path(entry.path))
    return paths


def audit_source(root):
    """Read static files only; never import the application or open its DB."""
    root = Path(root)
    facts = {
        "app.py": ("@app.before_request", "def _gate_webhook", "def _handle_options"),
        "routes/feed_routes.py": ('@feed_bp.route("/filter")', 'process_feed('),
        "routes/admin_routes.py": ('@admin_bp.route("/refresh"', '_submit_background_task'),
        "routes/dashboard_routes.py": ('def _queue_briefing_llm_refresh', '@dash_bp.route("/feedback"'),
        "routes/webhook_routes.py": ('def _locked_spool', 'def _submit_background_task'),
        "services/refresh.py": ('def refresh_execution_lock', 'def run_refresh_command'),
        "worker_main.py": ('_retry_pending_dashboard_publication()', 'start_webhook_dispatcher'),
        "scripts/cron_refresh.sh": ('MAINTENANCE_FILE=', 'flock -n 9'),
        "scripts/cron_event_enrichment.sh": ('MAINTENANCE_FILE=', 'REFRESH_EXECUTION_LOCK_FILE='),
        "scripts/release.sh": ('acquire_release_lock()', 'RELEASE_LOCK_FD'),
        "docker-compose.yml": ('location / {', 'expose:', 'AI_FILTER_REFRESH_LOCK:'),
    }
    files = {}
    for relative in SOURCE_FILES:
        raw = (root / relative).read_bytes()
        lines = raw.decode().splitlines()
        files[relative] = {"sha256": hashlib.sha256(raw).hexdigest(), "anchors": {
            needle: [n for n, line in enumerate(lines, 1) if needle in line] for needle in facts[relative]}}
    return {"status": "blocked_missing_shared_admission", "scope": "static_source_only",
            "expected_upstream_revision": SOURCE_REVISION, "expected_tree": SOURCE_TREE,
            "files": files, "production_adapter_bound": False,
            "blockers": ["No common API/worker cross-process admission fence or active-request registry",
                         "Refresh/spool locks cover only their own paths, not every producer",
                         "Compose does not bind Miniflux legacy async state or network bypass closure",
                         "No runtime capacity observer can assert a production stop through this adapter"]}


def validate_inspect(rows, expected):
    """Validate raw docker-inspect shaped data; collection is NOT implemented here."""
    require(isinstance(rows, list) and len(rows) == len(expected), "component inventory differs")
    by_name = {row["Name"].lstrip("/"): row for row in rows}
    require(set(by_name) == set(expected), "unknown/missing/duplicate component")
    for name, binding in expected.items():
        row = by_name[name]
        observed = {"id": row["Id"], "image": row["Image"], "started_at": row["State"]["StartedAt"],
                    "restart_count": row["RestartCount"],
                    "revision": row["Config"]["Labels"].get("org.opencontainers.image.revision")}
        require(observed == binding and type(observed["restart_count"]) is int, "component identity changed")
        require(row["State"]["Running"] is True and row["State"].get("OOMKilled") is False,
                "component is not running safely")
    return {name: dict(binding) for name, binding in expected.items()}


@contextmanager
def held_lock(path, exclusive, seconds):
    require(type(seconds) in (int, float) and 0 <= seconds <= MAX_LOCK_SECONDS, "lock budget invalid")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600, "unsafe lock inode")
        deadline = time.monotonic() + seconds
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise AdapterBusy("existing lock/request did not drain within the bound") from error
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        after = path.lstat()
        require(after.st_ino == info.st_ino and after.st_dev == info.st_dev, "lock path replaced")
        yield {"device": info.st_dev, "inode": info.st_ino}
    finally:
        os.close(fd)


def init_isolation(root, manifest):
    """Create only a fresh, private, explicitly synthetic gate; never reuse a live path."""
    root = Path(root)
    require(not root.exists(), "isolation root must be new")
    require(manifest.get("mode") == "isolation" and manifest.get("entrances") == list(ENTRANCES), "isolation inventory differs")
    require(re.fullmatch(r"[0-9a-f]{32}", manifest.get("run_id", "")), "invalid run identity")
    private_directory(root, create=True)
    for name in ("locks", "operations", "admissions", "work"):
        private_directory(root / name, create=True)
    durable_write(root / "marker.json", {"schema": 1, "mode": "isolation", "manifest_sha256": sha(manifest)})
    durable_write(root / "state.json", {"generation": 0, "entrances": dict.fromkeys(ENTRANCES, "open")})


class ProductionHandoffAdapter:
    def __init__(self, root, manifest, mode="production", lock_seconds=0.2):
        self.root, self.manifest, self.mode = Path(root), manifest, mode
        self.lock_seconds = lock_seconds
        self.manifest_sha256 = sha(manifest)
        self.identity = manifest.get("identity")

    def _capability(self):
        # There is deliberately no operator boolean that can enable live mode.
        if self.mode != "isolation" or self.manifest.get("mode") != "isolation":
            raise AdapterBlocked("production shared admission capability is absent; no stop confirmed")
        private_directory(self.root)
        marker = read_private(self.root / "marker.json")
        require(marker == {"schema": 1, "mode": "isolation", "manifest_sha256": self.manifest_sha256}, "isolation marker differs")
        require(self.manifest.get("entrances") == list(ENTRANCES), "entrypoint inventory differs")
        require(isinstance(self.identity, dict) and self.identity.get("scope") == "isolation"
                and self.identity.get("adapter_sha256") == hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "adapter source/identity changed")

    @contextmanager
    def _critical(self):
        self._capability()
        with held_lock(self.root / "locks/release.lock", True, self.lock_seconds) as release:
            with held_lock(self.root / "locks/refresh.lock", True, self.lock_seconds) as refresh:
                with held_lock(self.root / "locks/admission.lock", True, self.lock_seconds) as admission:
                    yield {"release": release, "refresh": refresh, "admission": admission}

    def _inventory(self):
        admissions = bounded_paths(self.root / "admissions", MAX_RECORDS)
        work = bounded_paths(self.root / "work", MAX_RECORDS - len(admissions))
        admitted = []
        for path in admissions:
            require(path.suffix == ".json", "unconfirmed admission material exists")
            row = read_private(path)
            require(isinstance(row.get("body_b64"), str) and 0 < len(row["body_b64"]) <= ((MAX_BODY + 2) // 3) * 4,
                    "encoded admission body exceeds bound")
            raw = base64.b64decode(row["body_b64"], validate=True)
            require(0 < len(raw) <= MAX_BODY and hashlib.sha256(raw).hexdigest() == row["body_sha256"]
                    and row["entrance"] in ENTRANCES and sha({"body_sha256": row["body_sha256"], "entrance": row["entrance"]}) == path.stem,
                    "admission identity mismatch")
            admitted.append(path.stem)
        rows = []
        allowed = {"queue_pending", "queue_inflight", "journal_pending", "journal_running", "journal_commit_pending",
                   "archive_pending", "terminal_pending", "active_requests", "old_route_inflight", "old_route_unknown",
                   "rejected_unresolved", "missing_originals", "buffer_inflight", "buffer_uncertain", "legacy_inflight",
                   "legacy_unpreserved", "unknown_producers"}
        counters = dict.fromkeys(sorted(allowed), 0)
        for path in work:
            require(path.suffix == ".json", "unrecognized work material")
            row = read_private(path)
            require(set(row) == {"kind", "identity"} and row["kind"] in allowed and HEX64.fullmatch(row["identity"]),
                    "unknown raw work state")
            counters[row["kind"]] += 1
            rows.append({"file": path.name, "sha256": sha(row), **row})
        return {"admission_total": len(admitted), "admission_ids": sorted(admitted), "counters": counters, "raw_work": rows}

    def snapshot(self, expected_identity, generation):
        validate_generation(generation)
        with self._critical() as locks:
            state = validate_state(read_private(self.root / "state.json"))
            pending = self.root / "transition.json.pending"
            require(not present(pending), "stop transition pending; reconcile exact operation")
            if present(self.root / "transition.json"):
                transition = read_private(self.root / "transition.json")
                self._receipt_identity(transition["receipt"], transition["receipt"]["operation_id"])
                require(state == validate_state(transition["state"]) == self._closed(transition["receipt"]["generation"]),
                        "stop transition not yet reconciled")
            require(self.identity == expected_identity and type(generation) is int and state["generation"] == generation,
                    "identity/generation changed")
            return {"identity": self.identity, **state, **self._inventory(), "locks": locks,
                    "manifest_sha256": self.manifest_sha256, "scope": "isolation", "production_adapter_bound": False}

    def verify_switch_gate(self, expected_identity, generation):
        snapshot = self.snapshot(expected_identity, generation)
        require(all(value == "closed" for value in snapshot["entrances"].values()), "admission not closed")
        require(all(value == 0 for value in snapshot["counters"].values()), "original zero gate not met")
        # No deploy action exists; a released lock snapshot is not a permit.
        return {**snapshot, "switch_authorized": False, "status": "isolated_zero_observed_only"}

    def _confirmed(self, operation_id):
        path = self.root / "operations" / (operation_id + ".json")
        receipt = read_private(path)
        self._receipt_identity(receipt, operation_id)
        require(validate_state(read_private(self.root / "state.json")) == self._closed(receipt["generation"]),
                "confirmed operation does not match closed gate")
        durable_write(path, receipt)
        return receipt

    def _receipt_identity(self, receipt, operation_id):
        validate_generation(receipt["previous_generation"])
        validate_generation(receipt["generation"])
        require(receipt["operation_id"] == operation_id and receipt["manifest_sha256"] == self.manifest_sha256
                and receipt["identity"] == self.identity and receipt["durable"] is True
                and receipt["state"] == "closed" and receipt["scope"] == "isolation"
                and receipt["production_adapter_bound"] is False
                and type(receipt["previous_generation"]) is int and type(receipt["generation"]) is int
                and receipt["previous_generation"] >= 0 and receipt["generation"] == receipt["previous_generation"] + 1,
                "operation identity conflict")

    @staticmethod
    def _closed(generation):
        validate_generation(generation)
        return {"generation": generation, "entrances": dict.fromkeys(ENTRANCES, "closed")}

    def stop(self, operation_id, reason, expected_generation):
        validate_generation(expected_generation)
        require(HEX64.fullmatch(operation_id) and isinstance(reason, str) and 0 < len(reason) <= 256,
                "bad stop identity/reason")
        with self._critical():
            path = self.root / "operations" / (operation_id + ".json")
            if path.exists():
                receipt = self._confirmed(operation_id)
                require(receipt["reason"] == reason and receipt["previous_generation"] == expected_generation,
                        "stop operation reused with other arguments")
                return receipt
            require(not present(self.root / "transition.json") and not present(self.root / "transition.json.pending"),
                    "uncertain stop intent must be queried, not reissued")
            state = validate_state(read_private(self.root / "state.json"))
            require(type(expected_generation) is int and state["generation"] == expected_generation, "stop CAS conflict")
            require(expected_generation < MAX_GENERATION, "generation exhausted")
            require(all(value == "open" for value in state["entrances"].values()), "unknown/previous closure must be reconciled")
            receipt = {"operation_id": operation_id, "reason": reason, "previous_generation": expected_generation,
                       "generation": expected_generation + 1, "identity": self.identity, "manifest_sha256": self.manifest_sha256,
                       "durable": True, "state": "closed", "scope": "isolation", "production_adapter_bound": False}
            closed = self._closed(expected_generation + 1)
            durable_write(self.root / "transition.json", {"state": closed, "receipt": receipt})
            durable_write(self.root / "state.json", closed)
            durable_write(path, receipt)
            return receipt

    def query_operation(self, operation_id):
        require(HEX64.fullmatch(operation_id), "bad operation id")
        with self._critical():
            transition = self.root / "transition.json"
            pending = transition.with_name(transition.name + ".pending")
            if present(pending):
                value = read_private(pending)
                self._receipt_identity(value["receipt"], operation_id)
                require(validate_state(value["state"]) == self._closed(value["receipt"]["generation"]), "pending transition is not closed")
                if present(transition):
                    require(read_private(transition) == value, "transition pending conflicts")
                durable_write(transition, value)
            if present(transition):
                value = read_private(transition)
                receipt = value["receipt"]
                self._receipt_identity(receipt, operation_id)
                require(validate_state(value["state"]) == self._closed(receipt["generation"]), "transition is not closed")
                state = validate_state(read_private(self.root / "state.json"))
                original = {"generation": receipt["previous_generation"], "entrances": dict.fromkeys(ENTRANCES, "open")}
                require(state in (original, value["state"]), "transition generation/state differs")
                durable_write(transition, value)
                durable_write(self.root / "state.json", value["state"])
                durable_write(self.root / "operations" / (operation_id + ".json"), receipt)
            return self._confirmed(operation_id)

    @contextmanager
    def admission(self, entrance):
        """Real isolated producer gate: SH retained throughout in-flight work."""
        self._capability()
        require(entrance in ENTRANCES, "unknown producer")
        with held_lock(self.root / "locks/admission.lock", False, self.lock_seconds):
            if present(self.root / "transition.json") or present(self.root / "transition.json.pending"):
                raise AdapterBlocked("durable/unconfirmed stop transition prevents new admission")
            state = validate_state(read_private(self.root / "state.json"))
            if state["entrances"].get(entrance) != "open":
                raise AdapterBlocked("new admission is closed")
            yield

    def accept_isolated(self, entrance, raw):
        require(type(raw) is bytes and 0 < len(raw) <= MAX_BODY, "synthetic body limit")
        with self.admission(entrance):
            with held_lock(self.root / "locks/counter.lock", True, self.lock_seconds):
                inventory = self._inventory()
                body_sha = hashlib.sha256(raw).hexdigest()
                delivery = sha({"body_sha256": body_sha, "entrance": entrance})
                path = self.root / "admissions" / (delivery + ".json")
                record = {"body_sha256": body_sha, "entrance": entrance, "body_b64": base64.b64encode(raw).decode()}
                if path.exists():
                    require(read_private(path) == record, "duplicate original differs")
                else:
                    require(inventory["admission_total"] + len(inventory["raw_work"]) < MAX_RECORDS, "inventory full")
                durable_write(path, record)
                return {"delivery_id": delivery, "durable": True, "scope": "isolation"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    report = audit_source(args.source_root)
    if args.execute:
        report["execution"] = "refused_missing_production_admission_capability"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
