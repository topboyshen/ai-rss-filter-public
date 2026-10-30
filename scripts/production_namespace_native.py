"""Root-only native proof for one already-stopped, owned synthetic Go sender.

The production transport contains no sudo. The CI caller may invoke this fixed
helper with sudo, feed only its synthetic token on stdin, and retain root-owned
originals. It sends one restrictive pause after observing the exact stop-new
operation; failures never repeat POST or claim queue drainage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from production_sender_driver import NativeSenderTransport, sources  # noqa: E402
import sender_namespace_http as namespace  # noqa: E402
from scripts.private_release_contract import safe_path  # noqa: E402
from services import admission_fence as gate  # noqa: E402

LABEL = "ai-filter.synthetic-miniflux-pause"
MEMORY = 768 * 1024**2
EXPORT_BYTES = 512 * 1024
EXPORT_INPUT_BYTES = 4 * 1024**2
EXPORT_FILE_BYTES = 1024**2
EVIDENCE_FILES = {"options.json", "token.json", "inspect.before.json", "network.json", "stop.query.json",
                  "pause.intent.json", "pause.response.json", "pause.query.json", "inspect.after.json", "RESULT.json", "STOP.json"}


def _host():
    gate._require(sys.platform == "linux" and os.geteuid() == 0, "native synthetic namespace helper requires Linux root")


def _docker(args, deadline):
    raw = namespace._capture(["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", *args],
                             deadline=min(deadline, time.monotonic() + 3), max_bytes=1024**2)
    return namespace._parsed(raw)


def _checked_options(options):
    keys = {"container_id", "owner", "image_id", "manifest", "network", "stop_operation", "evidence_root"}
    host_keys = {"private_host_context", "private_host_context_sha256"}
    gate._require(isinstance(options, dict) and set(options) in (keys, keys | host_keys),
                  "exact synthetic namespace options required")
    if host_keys <= options.keys():
        gate._require(isinstance(options["private_host_context_sha256"], str)
                      and namespace.HEX64.fullmatch(options["private_host_context_sha256"]),
                      "exact private host proof hash required")
        safe_path(options["private_host_context"])
    gate._require(all(isinstance(options[key], str) and namespace.HEX64.fullmatch(options[key])
                      for key in ("container_id", "manifest", "stop_operation"))
                  and re.fullmatch(r"[0-9a-f]{32}", options["owner"])
                  and re.fullmatch(r"sha256:[0-9a-f]{64}", options["image_id"])
                  and options["network"] == "miniflux-pause-" + options["owner"][:12] + "-network",
                  "owned synthetic identity required")
    return safe_path(options["evidence_root"])


def _owned(options, token, deadline):
    rows = _docker(["inspect", options["container_id"]], deadline)
    gate._require(isinstance(rows, list) and len(rows) == 1, "one synthetic sender required")
    row, host, state = rows[0], rows[0]["HostConfig"], rows[0]["State"]
    env = {}
    for item in row["Config"]["Env"]:
        key, value = item.split("=", 1)
        gate._require(key not in env, "duplicate synthetic environment")
        env[key] = value
    gate._require(row["Id"] == options["container_id"] and row["Image"] == options["image_id"]
                  and row["Config"]["Labels"].get(LABEL) == options["owner"]
                  and env.get("HANDOFF_ENABLED") == "1" and env.get("HANDOFF_MANIFEST") == options["manifest"]
                  and env.get("HANDOFF_CONTROL_ADDR") == "127.0.0.1:8787" and env.get("HANDOFF_CONTROL_TOKEN") == token
                  and env.get("ADMIN_USERNAME") == "synthetic" and env.get("DATABASE_URL", "").startswith("postgres://synthetic:")
                  and env["DATABASE_URL"].endswith("@pg:5432/synthetic_pause?sslmode=disable")
                  and host["NetworkMode"] == options["network"] and not host.get("PortBindings")
                  and set(row["NetworkSettings"]["Networks"]) == {options["network"]}
                  and host["Memory"] == host["MemorySwap"] == MEMORY and host["PidsLimit"] == 256
                  and host["NanoCpus"] == 1_000_000_000 and host["Privileged"] is False
                  and host["ReadonlyRootfs"] is True and not host.get("CapAdd") and "ALL" in host["CapDrop"]
                  and any(value in {"no-new-privileges", "no-new-privileges:true"} for value in host["SecurityOpt"])
                  and state["Running"] is True and state["OOMKilled"] is False
                  and not state["Paused"] and not state["Restarting"] and not state["Dead"],
                  "actual owned synthetic sender boundary differs")
    return row


def _receipt(body, options, operation, action, phases):
    gate._require(isinstance(body, dict) and isinstance(body.get("result"), dict), "actual sender receipt required")
    row = body["result"]
    gate._require(row.get("schema") == 1 and row.get("manifest") == options["manifest"]
                  and row.get("operation_id") == operation and row.get("action") == action
                  and row.get("state") in phases and row.get("durable") is True
                  and type(row.get("generation")) is int and type(row.get("previous_generation")) is int
                  and row["generation"] == row["previous_generation"] + 1
                  and type(body.get("confirmed")) is bool and row.get("confirmed") is body["confirmed"],
                  "actual namespace operation receipt differs")
    return row


def _exercise(options, root, token, deadline):
    before = _owned(options, token, deadline)
    gate._write(root / "inspect.before.json", before)
    network = _docker(["network", "inspect", options["network"]], deadline)
    isolated = isinstance(network, list) and len(network) == 1 and network[0]["Internal"] is True
    if not isolated and "private_host_context" in options and isinstance(network, list) and len(network) == 1:
        from scripts.production_activation_host_native import require_context_network
        require_context_network(Path(options["private_host_context"]), options["private_host_context_sha256"],
                                network[0], options["owner"])
        isolated = True
    gate._require(isinstance(network, list) and len(network) == 1 and isolated
                  and network[0]["Name"] == options["network"] and network[0]["Labels"].get(LABEL) == options["owner"],
                  "actual owned internal network required")
    gate._write(root / "network.json", network)
    transport = NativeSenderTransport("http://127.0.0.1:8787", root / "token.json", container_id=options["container_id"])
    status, original = transport.request("GET", "/operations/" + options["stop_operation"])
    gate._write(root / "stop.query.json", {"status": status, "body": original})
    stop = _receipt(original, options, options["stop_operation"], "stop_new", {"draining"})
    gate._require(status == 200 and stop["confirmed"] is True, "exact restrictive stop must already be confirmed")
    operation = gate._hash({"domain": "synthetic-namespace-pause", "container": options["container_id"],
                           "owner": options["owner"], "stop_operation": options["stop_operation"]})
    gate._write(root / "pause.intent.json", {"operation_id": operation, "parent": options["stop_operation"],
                "sources": sources(), "post_count_limit": 1, "production_access": False})
    status, actual = transport.request("POST", "/pause", {"operation_id": operation})
    gate._write(root / "pause.response.json", {"status": status, "body": actual})
    value = _receipt(actual, options, operation, "pause", {"closing", "closed"})
    gate._require(value["previous_generation"] == stop["generation"], "pause generation changed")
    status, query = transport.request("GET", "/operations/" + operation)
    gate._write(root / "pause.query.json", {"status": status, "body": query})
    checked = _receipt(query, options, operation, "pause", {"closing", "closed"})
    gate._require(checked["generation"] == value["generation"], "queried original operation generation differs")
    after = _owned(options, token, deadline)
    gate._write(root / "inspect.after.json", after)
    if "private_host_context" in options:
        from scripts.production_activation_host_native import require_context_network
        network_after = _docker(["network", "inspect", options["network"]], deadline)
        gate._require(isinstance(network_after, list) and len(network_after) == 1, "one private host network required after HTTP")
        require_context_network(Path(options["private_host_context"]), options["private_host_context_sha256"],
                                network_after[0], options["owner"])
    gate._require(all(before[key] == after[key] for key in ("Id", "Image", "RestartCount"))
                  and all(before["State"][key] == after["State"][key] for key in ("Pid", "StartedAt"))
                  and time.monotonic() < deadline, "synthetic owner changed or native deadline expired")
    return {"schema": 1, "status": "passed", "container_id": options["container_id"], "image_id": options["image_id"],
            "operation_id": operation, "namespace_transport": True, "get_count": 2, "post_count": 1,
            "observed_phase": checked["state"], "pause_confirmed": checked["confirmed"],
            "memory": MEMORY, "memory_swap": MEMORY, "pids": 256, "nano_cpus": 1_000_000_000,
            "published_control_port": False, "temporary_client_container": False,
            "queue_drained": False, "production_access": False, "sources": sources()}


def execute_owned(options, token):
    _host()
    root = _checked_options(options)
    gate._require(isinstance(token, str) and namespace.HEX64.fullmatch(token), "synthetic token required")
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    deadline = time.monotonic() + 25
    try:
        gate._write(root / "options.json", options)
        gate._write(root / "token.json", {"token": token})
        result = _exercise(options, root, token, deadline)
        gate._write(root / "RESULT.json", result)
        return result
    except BaseException as error:
        failure = {"status": "unconfirmed", "error_type": type(error).__name__,
                   "error_sha256": hashlib.sha256(str(error).encode()).hexdigest(),
                   "next_action": "preserve_originals_query_only_no_post_retry", "production_access": False}
        try:
            gate._write(root / "STOP.json", failure)
        except BaseException as evidence_error:
            error.add_note("native evidence failure: " + type(evidence_error).__name__)
        raise


def _evidence_root_fd(path):
    path = safe_path(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        gate._require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700, "private original root required")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _metadata(info):
    return {key: getattr(info, "st_" + key) for key in ("dev", "ino", "uid", "gid", "mode", "nlink", "size", "mtime_ns", "ctime_ns")}


def _export_file(fd, name, budget):
    source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        before = os.fstat(source)
        gate._require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_uid == os.geteuid()
                      and stat.S_IMODE(before.st_mode) == 0o600 and before.st_size <= EXPORT_FILE_BYTES,
                      "unsafe or oversized original")
        budget[0] += before.st_size
        gate._require(budget[0] <= EXPORT_INPUT_BYTES, "original byte budget exceeded")
        raw = bytearray()
        while len(raw) <= before.st_size:
            chunk = os.read(source, min(65536, before.st_size + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        gate._require(len(raw) == before.st_size and _metadata(os.fstat(source)) == _metadata(before)
                      and _metadata(os.stat(name, dir_fd=fd, follow_symlinks=False)) == _metadata(before), "original changed while exporting")
        return {"sha256": hashlib.sha256(raw).hexdigest(), "metadata": _metadata(before)}, bytes(raw)
    finally:
        os.close(source)


def _redacted(value, token, *, depth=0):
    gate._require(depth <= 32, "JSON nesting exceeds safe export bound")
    if isinstance(value, str):
        return value.replace(token, "<redacted-token>")
    if isinstance(value, list):
        return [_redacted(item, token, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        return {_redacted(key, token, depth=depth + 1): "<redacted-env>" if key == "Env" else _redacted(item, token, depth=depth + 1)
                for key, item in value.items()}
    return value


def safe_export(options, token):
    """Read-only derivative: retain hashes, omit token bytes, redact all Env."""
    gate._require(isinstance(token, str) and namespace.HEX64.fullmatch(token), "safe export token unavailable")
    root = _checked_options(options)
    packet = {"schema": 1, "complete": True, "originals_modified": False, "max_stdout_bytes": EXPORT_BYTES,
              "files": {}, "errors": [], "redaction": "token values and all Env; token files hash and metadata only"}
    budget, fd = [0], _evidence_root_fd(root)
    try:
        before = _metadata(os.fstat(fd))
        with os.scandir(fd) as children:
            names = []
            for child in children:
                gate._require(len(names) < 2 * len(EVIDENCE_FILES), "original file count bound")
                name = child.name
                gate._require(name.removesuffix(".pending") in EVIDENCE_FILES, "unexpected original filename")
                names.append(name)
        # A reused directory or wrong stdin token must never expose its content.
        for name in sorted(names):
            if name.removesuffix(".pending") in {"options.json", "token.json"}:
                _, raw = _export_file(fd, name, budget)
                expected = options if name.removesuffix(".pending") == "options.json" else {"token": token}
                gate._require(namespace._parsed(raw) == expected, "original export identity differs")
        for name in sorted(names):
            record, raw = _export_file(fd, name, budget)
            if name.removesuffix(".pending") != "token.json":
                try:
                    record["content"] = _redacted(namespace._parsed(raw), token)
                except (ValueError, gate.AdmissionError, namespace.NamespaceUncertain):
                    record["content_omitted"] = "invalid_or_unbounded_json"
                    packet["complete"] = False
            else:
                record["content_omitted"] = "secret_file"
            packet["files"][name] = record
            if len(namespace._encoded(packet)) > EXPORT_BYTES - 8192:
                record.pop("content", None)
                record["content_omitted"] = "output_budget"
                packet["complete"] = False
        gate._require(_metadata(os.fstat(fd)) == before, "original directory changed while exporting")
    finally:
        os.close(fd)
    return packet


def _literal_message(error):
    """Only a fixed string literal from this reviewed helper or its imports; runtime data never passes."""
    text = str(error)
    if not 0 < len(text) <= 200 or re.search(r"[0-9a-fA-F]{32,}", text) or not re.fullmatch(r"[ -~]+", text) or '"' in text:
        return None
    root = Path(__file__).resolve().parents[1]
    for folder in ("scripts", "services"):
        for source in sorted((root / folder).glob("*.py")):
            try:
                if ('"' + text + '"') in source.read_text(encoding="utf-8"):
                    return text
            except (OSError, UnicodeError):
                continue
    return None


def run_report(options, token):
    """Both success and failure produce a bounded safe stdout evidence packet."""
    try:
        result = execute_owned(options, token)
    except BaseException as error:
        result = {"status": "unconfirmed", "error_type": type(error).__name__,
                  "error_sha256": hashlib.sha256(str(error).encode()).hexdigest(),
                  "error_message_literal": _literal_message(error)}
    try:
        packet = safe_export(options, token)
    except BaseException as error:
        packet = {"schema": 1, "complete": False, "originals_modified": False, "files": {},
                  "errors": [{"type": type(error).__name__, "sha256": hashlib.sha256(str(error).encode()).hexdigest()}]}
    result["safe_evidence"] = packet
    if not packet["complete"]:
        result["execution_status"], result["status"] = result["status"], "unconfirmed"
    gate._require(len(namespace._encoded(result)) + 1 <= EXPORT_BYTES, "safe stdout export bound")
    return result


def main():
    parser = argparse.ArgumentParser()
    for name in ("container-id", "owner", "image-id", "manifest", "network", "stop-operation", "evidence-root"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--private-host-context")
    parser.add_argument("--private-host-context-sha256")
    options = {key: value for key, value in vars(parser.parse_args()).items() if value is not None}
    try:
        raw = sys.stdin.buffer.read(256)
        value = namespace._parsed(raw)
        gate._require(isinstance(value, dict) and set(value) == {"token"}, "exact synthetic stdin token required")
        result = run_report(options, value["token"])
        sys.stdout.buffer.write(namespace._encoded(result) + b"\n")
        sys.stdout.buffer.flush()
        return 0 if result["status"] == "passed" else 3
    except BaseException as error:
        print(json.dumps({"status": "unconfirmed", "error_type": type(error).__name__,
                          "error_sha256": hashlib.sha256(str(error).encode()).hexdigest(),
                          "safe_evidence": {"schema": 1, "complete": False, "files": {}, "originals_modified": False,
                                            "errors": ["input_or_export_unconfirmed"]}}), flush=True)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
