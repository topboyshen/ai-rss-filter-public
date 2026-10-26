"""Offline checker/orchestration tests, not executions of the RSSHub parsers.

All Docker calls are mocked. Actual pinned-image HTTP regression is an explicit
run of scripts/repro_rsshub_36kr.py and is never hidden inside the default suite.
"""

import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from scripts import repro_rsshub_36kr as repro


SCRIPT = Path(repro.__file__).resolve()


def case_named(name):
    return next(case for case in repro.cases() if case["name"] == name)


def valid_empty_xml_case():
    """A structural-checker input, distinct from RSSHub's actual empty-route 503."""
    case = case_named("empty_list")
    case["status"] = {role: 200 for role in repro.PINS}
    return case


def xml_body(case):
    root = ET.Element("rss", version="2.0")
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "link").text = "https://www.36kr.com/newsflashes"
    for expected in case["items"]:
        node = ET.SubElement(channel, "item")
        for field, value in expected.items():
            ET.SubElement(node, field).text = value
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def replace_body(record, body):
    record["body"] = base64.b64encode(body).decode()
    record["body_sha256"] = repro.digest(body)


def synthetic_record(case, role):
    """Fabricated evidence solely for testing the checker, never route proof."""
    record = {
        "case": case["name"], "role": role, "pin": deepcopy(repro.PINS[role]),
        "input_sha256": repro.digest(case["html"].encode()), "cleanup": "removed",
        "status": case["status"][role],
    }
    record["logs"] = (
        f"REPRO_ROUTE_SHA256={repro.PINS[role]['route_sha256']}\n"
        f"REPRO_INPUT_SHA256={record['input_sha256']}\n"
    )
    if record["status"] == 503:
        record["logs"] += f"Error in /36kr/newsflashes: {case['errors'][role]}\n"
    replace_body(record, xml_body(case) if record["status"] == 200 else b"<html>route error</html>")
    return record


def synthetic_report():
    return {
        "schema": 1, "pins": deepcopy(repro.PINS), "loader_sha256": repro.digest(repro.LOADER.encode()),
        "rows": [synthetic_record(case, role) for case in repro.cases() for role in repro.PINS],
    }


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@pytest.mark.parametrize("case", repro.cases(), ids=lambda case: case["name"])
@pytest.mark.parametrize("role", ["old", "candidate"])
def test_checker_accepts_each_explicit_expected_outcome(case, role):
    repro.validate_response(case, role, synthetic_record(case, role))


def test_catalog_distinguishes_known_defects_from_common_success():
    catalog = {case["name"]: case for case in repro.cases()}
    assert len(catalog) == len(repro.cases()) == 18
    assert catalog["plain_duplicate_titles"]["status"] == {"old": 200, "candidate": 200}
    assert catalog["content_bracket"]["status"] == {"old": 503, "candidate": 200}
    assert catalog["malformed_outer"]["status"] == {"old": 503, "candidate": 503}
    assert catalog["malformed_outer"]["errors"] == {"old": "this route is empty", "candidate": "SyntaxError"}
    assert catalog["empty_list"]["errors"] == {"old": "this route is empty", "candidate": "this route is empty"}
    assert catalog["encrypted"]["status"] == {"old": 503, "candidate": 200}
    assert catalog["missing_state"]["status"] == {"old": 503, "candidate": 503}
    assert all("window.initialState" in case["html"] for case in repro.cases() if case["name"] != "missing_state")


def test_checker_allows_valid_empty_feed_and_distinct_items_with_same_title():
    empty = valid_empty_xml_case()
    record = synthetic_record(empty, "candidate")
    assert len(base64.b64decode(record["body"])) < 2000
    repro.validate_response(empty, "candidate", record)
    duplicate_titles = case_named("plain_duplicate_titles")
    assert duplicate_titles["items"][0]["title"] == duplicate_titles["items"][1]["title"]
    assert duplicate_titles["items"][0]["link"] != duplicate_titles["items"][1]["link"]
    repro.validate_response(duplicate_titles, "candidate", synthetic_record(duplicate_titles, "candidate"))


@pytest.mark.parametrize("field", ["title", "link", "description"])
def test_checker_rejects_any_wrong_item_field_even_with_valid_xml(field):
    case = case_named("plain_duplicate_titles")
    row = synthetic_record(case, "candidate")
    root = ET.fromstring(base64.b64decode(row["body"]))
    root.find(f"channel/item/{field}").text = "wrong-but-nonempty"
    replace_body(row, ET.tostring(root))
    with pytest.raises(ValueError, match="count/order/title/link/description"):
        repro.validate_response(case, "candidate", row)


@pytest.mark.parametrize("mutation", ["missing", "extra", "reordered"])
def test_checker_rejects_count_or_order_changes(mutation):
    case = case_named("plain_duplicate_titles")
    row = synthetic_record(case, "candidate")
    root = ET.fromstring(base64.b64decode(row["body"]))
    channel = root.find("channel")
    first = channel.find("item")
    if mutation == "missing":
        channel.remove(first)
    elif mutation == "extra":
        channel.append(deepcopy(first))
    else:
        channel.remove(first)
        channel.append(first)
    replace_body(row, ET.tostring(root))
    with pytest.raises(ValueError, match="count/order/title/link/description"):
        repro.validate_response(case, "candidate", row)


@pytest.mark.parametrize("field", ["title", "link", "description"])
@pytest.mark.parametrize("operation", ["missing", "duplicate"])
def test_checker_rejects_missing_or_duplicate_item_fields(field, operation):
    case = case_named("plain_duplicate_titles")
    row = synthetic_record(case, "candidate")
    root = ET.fromstring(base64.b64decode(row["body"]))
    node = root.find("channel/item")
    target = node.find(field)
    if operation == "missing":
        node.remove(target)
    else:
        node.append(deepcopy(target))
    replace_body(row, ET.tostring(root))
    with pytest.raises(ValueError, match="missing or duplicate item field"):
        repro.validate_response(case, "candidate", row)


@pytest.mark.parametrize("body", [
    b"<rss><channel>", b"<html><channel/></html>", b"<rss/>",
    b"<rss><channel/><channel/></rss>",
    b"<rss><channel><link>https://example.com/</link></channel></rss>",
    b'<!DOCTYPE rss><rss><channel/></rss>',
    b'<!DOCTYPE rss [<!ENTITY x "injected">]><rss><channel/></rss>',
])
def test_checker_rejects_malformed_or_wrong_feed_structure(body):
    case = valid_empty_xml_case()
    row = synthetic_record(case, "candidate")
    replace_body(row, body)
    with pytest.raises((ValueError, ET.ParseError)):
        repro.validate_response(case, "candidate", row)


@pytest.mark.parametrize("field,value", [
    ("error", "timeout"), ("cleanup", "unconfirmed"), ("input_sha256", "wrong"),
    ("body_sha256", "wrong"), ("body", "invalid-base64!"), ("status", 201),
])
def test_checker_rejects_failed_or_tampered_execution(field, value):
    case = case_named("plain_duplicate_titles")
    row = synthetic_record(case, "candidate")
    row[field] = value
    with pytest.raises(ValueError):
        repro.validate_response(case, "candidate", row)


def test_checker_rejects_large_body_even_when_hash_matches():
    case = case_named("empty_list")
    row = synthetic_record(case, "candidate")
    replace_body(row, b"x" * (repro.MAX_BODY_BYTES + 1))
    with pytest.raises(ValueError, match="response integrity"):
        repro.validate_response(case, "candidate", row)


@pytest.mark.parametrize("pin_field", ["image", "id", "revision", "route", "route_sha256"])
def test_checker_rejects_image_identity_drift(pin_field):
    case = case_named("empty_list")
    row = synthetic_record(case, "candidate")
    row["pin"][pin_field] = "unexpected"
    with pytest.raises(ValueError, match="image/input identity"):
        repro.validate_response(case, "candidate", row)


@pytest.mark.parametrize("proof", ["REPRO_ROUTE_SHA256", "REPRO_INPUT_SHA256"])
@pytest.mark.parametrize("mutation", ["missing", "duplicated", "changed"])
def test_checker_requires_exactly_one_actual_route_and_transport_proof(proof, mutation):
    case = case_named("empty_list")
    row = synthetic_record(case, "candidate")
    marker = next(line for line in row["logs"].splitlines() if line.startswith(proof + "="))
    replacement = "" if mutation == "missing" else marker + "\n" + marker if mutation == "duplicated" else proof + "=wrong"
    row["logs"] = row["logs"].replace(marker, replacement)
    with pytest.raises(ValueError, match="handler/transport proof"):
        repro.validate_response(case, "candidate", row)


@pytest.mark.parametrize("mutation", ["status", "error", "route", "syntax"])
def test_checker_requires_expected_parser_failure_not_any_error(mutation):
    case = case_named("content_bracket")
    row = synthetic_record(case, "old")
    if mutation == "status":
        row["status"] = 500
    elif mutation == "error":
        row["logs"] = row["logs"].replace("Error", "Failure")
    elif mutation == "route":
        row["logs"] = row["logs"].replace("/36kr/newsflashes", "/unrelated")
    else:
        row["logs"] = row["logs"].replace("SyntaxError", "TransportError")
    with pytest.raises(ValueError):
        repro.validate_response(case, "old", row)


def test_checker_rejects_both_versions_agreeing_on_wrong_result():
    case = case_named("plain_duplicate_titles")
    for role in repro.PINS:
        row = synthetic_record(case, role)
        row["status"] = 503
        with pytest.raises(ValueError, match="HTTP status"):
            repro.validate_response(case, role, row)


def test_complete_expected_matrix_rechecks_offline():
    assert repro.check_report(synthetic_report()) == 36


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown", "schema", "pins", "loader"])
def test_report_rejects_incomplete_or_changed_evidence(mutation):
    report = synthetic_report()
    if mutation == "missing":
        report["rows"].pop()
    elif mutation == "duplicate":
        report["rows"].append(deepcopy(report["rows"][0]))
    elif mutation == "unknown":
        report["rows"][0]["case"] = "unrecorded-case"
    elif mutation == "schema":
        report["schema"] = 2
    elif mutation == "pins":
        report["pins"]["old"]["image"] = "diygod/rsshub:latest"
    else:
        report["loader_sha256"] = "changed-loader"
    with pytest.raises(ValueError):
        repro.check_report(report)


def test_offline_cli_succeeds_without_docker_or_network(tmp_path):
    repro.save_json(tmp_path / "report.json", synthetic_report())
    env = {**os.environ, "PATH": "", "DOCKER_HOST": "ssh://must-not-connect"}
    result = subprocess.run([sys.executable, str(SCRIPT), "--check", str(tmp_path)],
                            text=True, capture_output=True, timeout=15, env=env)
    assert result.returncode == 0
    assert "PASS: 36" in result.stdout
    assert "historical production cause remains unproven" in result.stdout


@pytest.mark.parametrize("content", [None, "{", "{}", '{"schema":1,"rows":[]}'])
def test_offline_cli_reports_missing_or_invalid_evidence(tmp_path, content):
    if content is not None:
        (tmp_path / "report.json").write_text(content, encoding="utf-8")
    result = subprocess.run([sys.executable, str(SCRIPT), "--check", str(tmp_path)],
                            text=True, capture_output=True, timeout=15, env={**os.environ, "PATH": ""})
    assert result.returncode == 1
    assert "FAIL:" in result.stdout
    assert "PASS:" not in result.stdout


def test_cli_does_not_allow_run_and_check_together(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), "--run", str(tmp_path), "--check", str(tmp_path)],
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 2
    assert "not allowed" in result.stderr


def test_existing_evidence_is_never_overwritten(tmp_path):
    target = tmp_path / "report.json"
    repro.save_json(target, {"original": True})
    with pytest.raises(FileExistsError):
        repro.save_json(target, {"replacement": True})
    assert json.loads(target.read_text()) == {"original": True}


@pytest.mark.parametrize("variable", ["DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY"])
def test_remote_docker_environment_is_rejected_before_command(monkeypatch, variable):
    monkeypatch.setenv(variable, "configured")
    monkeypatch.setattr(repro, "command", lambda *args, **kwargs: pytest.fail("must not invoke Docker"))
    with pytest.raises(ValueError, match="environment overrides"):
        repro.local_docker()


@pytest.mark.parametrize("endpoint", ["ssh://JD", "tcp://127.0.0.1:2375", "npipe:////./pipe/docker_engine", ""])
def test_non_unix_docker_context_is_rejected(monkeypatch, endpoint):
    for variable in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY"):
        monkeypatch.delenv(variable, raising=False)
    outputs = iter([completed("repro-context\n"), completed(endpoint)])
    monkeypatch.setattr(repro, "command", lambda *args, **kwargs: next(outputs))
    with pytest.raises(ValueError, match="local Unix-socket"):
        repro.local_docker()


def test_selected_local_context_is_explicit_in_all_later_commands(monkeypatch):
    for variable in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY"):
        monkeypatch.delenv(variable, raising=False)
    calls = []

    def fake_command(args, **kwargs):
        calls.append(args)
        return completed("colima\n" if len(calls) == 1 else "unix:///private/tmp/docker.sock\n")

    monkeypatch.setattr(repro, "command", fake_command)
    assert repro.local_docker() == ["docker", "--context", "colima"]
    assert calls[1][3] == "colima"


def image_metadata(pin):
    return {"Id": pin["id"], "Os": "linux", "Architecture": "amd64", "RepoDigests": [pin["image"]],
            "Config": {"Labels": {"org.opencontainers.image.revision": pin["revision"]}}}


@pytest.mark.parametrize("mutation", [None, "id", "revision", "os", "arch", "digest"])
def test_installed_image_identity_checks_every_required_field(monkeypatch, mutation):
    calls = []

    def fake_command(args, **kwargs):
        calls.append(args)
        pin = next(value for value in repro.PINS.values() if value["image"] == args[-1])
        data = image_metadata(pin)
        if mutation == "id":
            data["Id"] = "sha256:other"
        elif mutation == "revision":
            data["Config"]["Labels"]["org.opencontainers.image.revision"] = "other"
        elif mutation == "os":
            data["Os"] = "windows"
        elif mutation == "arch":
            data["Architecture"] = "arm64"
        elif mutation == "digest":
            data["RepoDigests"] = []
        return completed(json.dumps(data))

    monkeypatch.setattr(repro, "command", fake_command)
    if mutation is None:
        repro.verify_images(["docker"])
        assert len(calls) == 2
        assert all(call[call.index("--platform") + 1] == "linux/amd64" for call in calls)
    else:
        with pytest.raises(ValueError, match="image identity"):
            repro.verify_images(["docker"])


def test_container_configuration_cannot_reach_production_or_pull_images(tmp_path):
    args = repro.container_args("owned-name", "run123", tmp_path, repro.PINS["old"])
    assert args[0] == "create"
    assert "--pull=never" in args
    for flag, value in (("--network", "none"), ("--cap-drop", "ALL"), ("--memory", "1g"),
                        ("--cpus", "1"), ("--pids-limit", "128"), ("--security-opt", "no-new-privileges")):
        assert args[args.index(flag) + 1] == value
    assert "--read-only" in args
    assert f"{repro.OWNER_LABEL}=run123" in args
    assert f"type=bind,src={tmp_path},dst=/evidence,readonly" in args
    assert "--publish" not in args and "-p" not in args and "--privileged" not in args
    assert "docker.sock" not in " ".join(args)
    assert "--import" in args and repro.PINS["old"]["image"] in args


@pytest.mark.parametrize("case", ["missing", "foreign"])
def test_cleanup_never_removes_unconfirmed_or_foreign_container(monkeypatch, case):
    calls = []

    def fake_command(args, **kwargs):
        calls.append(args)
        if case == "missing":
            return completed(returncode=1)
        return completed(json.dumps([{"Id": "foreign-id", "Config": {"Labels": {repro.OWNER_LABEL: "other"}}}]))

    monkeypatch.setattr(repro, "command", fake_command)
    with pytest.raises(RuntimeError):
        repro.remove_owned(["docker"], "container-name", "expected-run")
    assert len(calls) == 1
    assert "rm" not in calls[0]


@pytest.mark.parametrize("logs_fail", [False, True])
def test_cleanup_removes_exact_inspected_id_and_preserves_log_failure(monkeypatch, logs_fail):
    calls = []

    def fake_command(args, **kwargs):
        calls.append(args)
        if "inspect" in args:
            return completed(json.dumps([{"Id": "owned-id", "Config": {"Labels": {repro.OWNER_LABEL: "run"}}}]))
        if "logs" in args:
            return completed("stdout", int(logs_fail), "stderr")
        return completed()

    monkeypatch.setattr(repro, "command", fake_command)
    if logs_fail:
        with pytest.raises(RuntimeError, match="preserve route logs"):
            repro.remove_owned(["docker"], "owned-name", "run")
    else:
        assert repro.remove_owned(["docker"], "owned-name", "run") == "stdoutstderr"
    assert calls[-1] == ["docker", "rm", "-f", "owned-id"]


@pytest.mark.parametrize("failure", [None, "start", "fetch", "cleanup"])
def test_case_execution_always_attempts_owned_cleanup_and_saves_result(monkeypatch, tmp_path, failure):
    case = case_named("empty_list")
    expected = synthetic_record(case, "old")
    calls, cleanups = [], []

    def fake_command(args, **kwargs):
        calls.append(args)
        if (failure == "start" and "start" in args) or (failure == "fetch" and repro.FETCH in args):
            raise subprocess.TimeoutExpired(args, 15)
        if repro.FETCH in args:
            return completed(json.dumps({"status": expected["status"], "body": expected["body"]}))
        return completed()

    def fake_cleanup(docker, name, run_id):
        cleanups.append((docker, name, run_id))
        if failure == "cleanup":
            raise RuntimeError("ownership unavailable")
        return expected["logs"]

    monkeypatch.setattr(repro, "command", fake_command)
    monkeypatch.setattr(repro, "remove_owned", fake_cleanup)
    row = repro.run_case(["docker"], tmp_path, "run", case, "old", repro.time.monotonic() + 60)
    assert len(cleanups) == 1
    assert cleanups[0][1] == "36kr-repro-run-old-empty_list"
    assert json.loads((tmp_path / "old.json").read_text()) == row
    if failure:
        with pytest.raises(ValueError):
            repro.validate_response(case, "old", row)
    else:
        repro.validate_response(case, "old", row)
    if failure == "cleanup":
        assert row["cleanup"].startswith("unconfirmed:")
    else:
        assert row["cleanup"] == "removed"


def test_log_timeout_still_removes_only_confirmed_owned_container(monkeypatch):
    calls = []

    def fake_command(args, **kwargs):
        calls.append(args)
        if "inspect" in args:
            return completed(json.dumps([{"Id": "owned-id", "Config": {"Labels": {repro.OWNER_LABEL: "run"}}}]))
        if "logs" in args:
            raise subprocess.TimeoutExpired(args, 15)
        return completed()

    monkeypatch.setattr(repro, "command", fake_command)
    with pytest.raises(subprocess.TimeoutExpired):
        repro.remove_owned(["docker"], "owned-name", "run")
    assert calls[-1] == ["docker", "rm", "-f", "owned-id"]


def test_expired_matrix_budget_never_starts_or_removes_container(monkeypatch, tmp_path):
    monkeypatch.setattr(repro, "command", lambda *args, **kwargs: pytest.fail("must not start Docker"))
    monkeypatch.setattr(repro, "remove_owned", lambda *args, **kwargs: pytest.fail("nothing was created"))
    row = repro.run_case(["docker"], tmp_path, "run", case_named("empty_list"), "old", 0)
    assert "matrix time budget exhausted" in row["error"]
    assert "cleanup" not in row
    assert json.loads((tmp_path / "old.json").read_text()) == row


@pytest.mark.parametrize("path", [Path("relative"), Path("/tmp/comma,path")])
def test_invalid_output_is_rejected_before_docker(monkeypatch, path):
    monkeypatch.setattr(repro, "local_docker", lambda: pytest.fail("must reject path before Docker"))
    with pytest.raises(ValueError, match="absolute path without commas"):
        repro.run_matrix(path)


def test_matrix_failure_preserves_completed_evidence_and_does_not_continue(monkeypatch, tmp_path):
    output = tmp_path / "new-run"
    calls = []
    monkeypatch.setattr(repro, "local_docker", lambda: ["docker"])
    monkeypatch.setattr(repro, "verify_images", lambda docker: None)

    def fake_run(docker, directory, run_id, case, role, deadline):
        calls.append((case["name"], role))
        row = synthetic_record(case, role)
        row["error"] = "injected route execution failure"
        repro.save_json(directory / f"{role}.json", row)
        return row

    monkeypatch.setattr(repro, "run_case", fake_run)
    with pytest.raises(ValueError, match="execution"):
        repro.run_matrix(output)
    report = json.loads((output / "report.json").read_text())
    assert len(report["rows"]) == 2
    assert set(calls) == {("plain_duplicate_titles", "old"), ("plain_duplicate_titles", "candidate")}
    assert (output / "plain_duplicate_titles/input.html").exists()
    assert (output / "plain_duplicate_titles/transport.mjs").exists()
    assert not (output / "empty_list").exists()
    assert report["finished_at"] >= report["started_at"]


def test_new_run_refuses_existing_evidence_directory(monkeypatch, tmp_path):
    (tmp_path / "keep.txt").write_text("prior evidence")
    monkeypatch.setattr(repro, "local_docker", lambda: ["docker"])
    monkeypatch.setattr(repro, "verify_images", lambda docker: None)
    monkeypatch.setattr(repro, "run_case", lambda *args: pytest.fail("must preserve directory"))
    with pytest.raises(FileExistsError):
        repro.run_matrix(tmp_path)
    assert (tmp_path / "keep.txt").read_text() == "prior evidence"
