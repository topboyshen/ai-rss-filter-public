"""Offline tests of the evidence checker; no Docker, upstream, or model calls.

These synthetic evidence records test checker strictness, not actual RSSHub
behavior. Real-handler proof is produced only by the explicit native HTTP run.
"""

import base64
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from scripts import check_rsshub_compat_http as probe


MODULES = [{"path": "/app/dist/wallstreetcn-news-Fake.mjs", "sha256": "1" * 64},
           {"path": "/app/dist/cls-telegraph-Fake.mjs", "sha256": "2" * 64}]
IMAGE_ID = "sha256:" + "3" * 64
CONFIG = {"Entrypoint": ["dumb-init", "--"], "Cmd": ["npm", "run", "start"]}


def named(name):
    return next(case for case in probe.cases() if case["name"] == name)


def xml_body(items):
    root = ET.Element("rss", version="2.0")
    channel = ET.SubElement(root, "channel")
    for item in items:
        node = ET.SubElement(channel, "item")
        for key, value in item.items():
            child = ET.SubElement(node, key)
            child.text = value
            if key == "guid":
                child.set("isPermaLink", "false")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def record(case, modules=MODULES):
    fixture_hash = probe.sha256(json.dumps(case, ensure_ascii=False, sort_keys=True).encode())
    used = "wallstreetcn" if "wallstreetcn" in case["steps"][0]["path"] else "cls"
    module = next(module for module in modules if "/" + used + "-" in module["path"])
    logs = "COMPAT_MODULE_SHA256=" + json.dumps(module, separators=(",", ":")) + "\n"
    for source in case["upstream"]:
        for _ in range(source["count"]):
            logs += "COMPAT_UPSTREAM=" + json.dumps({key: source[key] for key in ("key", "kind", "url", "query")}) + "\n"
            logs += "COMPAT_FIXTURE_SHA256=" + fixture_hash + "\n"
    steps = []
    for step in case["steps"]:
        body = b"Unsupported Wallstreetcn news category" if "error" in step else xml_body(step["items"])
        steps.append({"path": step["path"], "status": 404 if "error" in step else 200,
                      "body": base64.b64encode(body).decode(), "body_sha256": probe.sha256(body),
                      "client_cgroup": "0::/host-test-client"})
    return {"case": case["name"], "fixture_sha256": fixture_hash, "logs": logs, "steps": steps, "cleanup": "removed"}


def report():
    return {"schema": 1, "modules": deepcopy(MODULES), "loader_sha256": probe.sha256(probe.LOADER.encode()),
            "rows": [record(case) for case in probe.cases()]}


def completed(stdout="", code=0):
    return subprocess.CompletedProcess([], code, stdout, "")


@pytest.mark.parametrize("case", probe.cases(), ids=lambda case: case["name"])
def test_checker_accepts_frozen_expected_http_results(case):
    probe.validate_row(case, record(case), MODULES)


@pytest.mark.parametrize("status", [200, 301, 400, 401, 403, 503])
def test_retired_category_requires_exact_not_found_not_an_auth_or_transport_error(status):
    case = named("unsupported_china")
    row = record(case)
    row["steps"][0]["status"] = status
    with pytest.raises(ValueError, match="404"):
        probe.validate_row(case, row, MODULES)


def test_frozen_cases_cover_data_ad_deleted_defaults_and_cache():
    cases = {case["name"]: case for case in probe.cases()}
    assert len(cases) == 9
    for name in ("global_default_mixed", "global_explicit_mixed"):
        case = cases[name]
        assert [item["guid"] for item in case["steps"][0]["items"]] == ["1002", "1001"]
        assert "channel=global-channel&accept=article&" in case["upstream"][0]["url"]
        assert {source["response"].get("code") for source in case["upstream"][1:]} == {20000, 404}
    cache = cases["detail_cache_keeps_listing_dates"]
    assert len(cache["steps"]) == 2
    assert cache["steps"][0]["items"][0]["pubDate"] != cache["steps"][1]["items"][0]["pubDate"]
    assert sum(source["key"] == "detail_1001" for source in cache["upstream"]) == 1
    assert "channel=shares&accept=article&" in cache["upstream"][1]["url"]


def test_cls_bare_identity_and_watch_are_deliberately_different():
    bare = named("cls_bare_identity")["steps"][0]["items"]
    watch = named("cls_watch_unchanged")["steps"][0]["items"]
    assert all(item["guid"] == item["title"] and item["link"] == "" for item in bare)
    assert all(item["guid"] == item["link"] and item["link"].startswith("https://www.cls.cn/detail/") for item in watch)
    for left, right in zip(bare, watch):
        assert {key: left[key] for key in ("title", "description", "pubDate")} == {
            key: right[key] for key in ("title", "description", "pubDate")}


def test_bare_explicit_shareurl_is_preserved_not_forced_empty():
    for item in named("cls_bare_existing_link")["steps"][0]["items"]:
        assert item["link"].startswith("https://www.cls.cn/detail/")
        assert item["guid"] == item["link"]
    for item in named("cls_bare_identity")["upstream"][0]["response"]["data"]["roll_data"]:
        assert "shareurl" not in item


def test_duplicate_title_items_are_preserved_with_separate_source_ids():
    case = named("cls_bare_duplicate_titles")
    items = case["steps"][0]["items"]
    assert len(items) == 2
    assert items[0]["title"] == items[1]["title"]
    assert items[0]["guid"] == items[1]["guid"]
    assert items[0]["description"] != items[1]["description"]
    assert case["upstream"][0]["response"]["data"]["roll_data"][0]["id"] == 920201
    assert case["upstream"][0]["response"]["data"]["roll_data"][1]["id"] == 920202


@pytest.mark.parametrize("field", ["title", "link", "guid", "description", "pubDate"])
def test_checker_rejects_each_wrong_item_field(field):
    case = named("global_default_mixed")
    row = record(case)
    wrong = deepcopy(case["steps"][0]["items"])
    wrong[0][field] = "not the source value"
    body = xml_body(wrong)
    row["steps"][0].update(body=base64.b64encode(body).decode(), body_sha256=probe.sha256(body))
    with pytest.raises(ValueError, match="RSS items"):
        probe.validate_row(case, row, MODULES)


@pytest.mark.parametrize("attribute", [None, "true", "False", "TRUE", "0", "", "not-a-boolean"])
def test_checker_rejects_guid_permalink_semantic_drift_even_when_guid_text_is_unchanged(attribute):
    case = named("global_default_mixed")
    row = record(case)
    root = ET.fromstring(base64.b64decode(row["steps"][0]["body"]))
    guid = root.find("channel/item/guid")
    original = guid.text
    if attribute is None:
        guid.attrib.pop("isPermaLink")
    else:
        guid.set("isPermaLink", attribute)
    assert guid.text == original
    body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    row["steps"][0].update(body=base64.b64encode(body).decode(), body_sha256=probe.sha256(body))
    with pytest.raises(ValueError, match="isPermaLink=false"):
        probe.validate_row(case, row, MODULES)


@pytest.mark.parametrize("operation", ["missing", "duplicate"])
def test_parser_requires_exactly_one_guid_with_explicit_false_permalink(operation):
    items = named("cls_bare_identity")["steps"][0]["items"]
    root = ET.fromstring(xml_body(items))
    node = root.find("channel/item")
    if operation == "missing":
        node.remove(node.find("guid"))
    else:
        node.append(deepcopy(node.find("guid")))
    with pytest.raises(ValueError, match="GUID"):
        probe.parse_items(ET.tostring(root))


def test_parser_accepts_explicit_false_for_both_title_and_url_guids():
    for name in ("cls_bare_identity", "cls_watch_unchanged", "global_default_mixed"):
        items = named(name)["steps"][0]["items"]
        assert probe.parse_items(xml_body(items)) == items


@pytest.mark.parametrize("operation", ["empty", "reverse", "extra", "missing"])
def test_checker_rejects_item_count_and_order_drift(operation):
    case = named("global_default_mixed")
    row = record(case)
    items = deepcopy(case["steps"][0]["items"])
    items = {"empty": [], "reverse": items[::-1], "extra": items + [items[0]], "missing": items[:-1]}[operation]
    body = xml_body(items)
    row["steps"][0].update(body=base64.b64encode(body).decode(), body_sha256=probe.sha256(body))
    with pytest.raises(ValueError, match="RSS items"):
        probe.validate_row(case, row, MODULES)


@pytest.mark.parametrize("status", [200, 301, 500, 503])
def test_unsupported_category_requires_explicit_4xx(status):
    case = named("unsupported_china")
    row = record(case)
    row["steps"][0]["status"] = status
    with pytest.raises(ValueError, match="404"):
        probe.validate_row(case, row, MODULES)


def test_rejected_category_cannot_contact_upstream():
    case = named("unsupported_china")
    row = record(case)
    row["logs"] += 'COMPAT_UPSTREAM={"key":"unexpected"}\n'
    with pytest.raises(ValueError, match="upstream calls"):
        probe.validate_row(case, row, MODULES)


@pytest.mark.parametrize("name", ["COMPAT_MODULE_SHA256=", "COMPAT_FIXTURE_SHA256=", "COMPAT_UPSTREAM="])
def test_checker_requires_actual_module_and_fixture_transport_proofs(name):
    case = named("global_default_mixed")
    row = record(case)
    row["logs"] = "\n".join(line for line in row["logs"].splitlines() if not line.startswith(name))
    with pytest.raises(ValueError):
        probe.validate_row(case, row, MODULES)


def test_checker_rejects_repeated_detail_fetch_in_cache_case():
    case = named("detail_cache_keeps_listing_dates")
    row = record(case)
    row["logs"] += 'COMPAT_UPSTREAM={"key":"detail_1001"}\n'
    with pytest.raises(ValueError, match="cache reuse"):
        probe.validate_row(case, row, MODULES)


@pytest.mark.parametrize("key,value", [("cleanup", "unconfirmed"), ("error", "timeout"), ("fixture_sha256", "bad")])
def test_checker_rejects_incomplete_execution(key, value):
    case = named("global_default_mixed")
    row = record(case)
    row[key] = value
    with pytest.raises(ValueError):
        probe.validate_row(case, row, MODULES)


@pytest.mark.parametrize("operation", ["missing", "duplicate", "unknown", "loader"])
def test_report_requires_complete_exact_case_and_loader_identity(operation):
    data = report()
    if operation == "missing":
        data["rows"].pop()
    elif operation == "duplicate":
        data["rows"][-1] = data["rows"][0]
    elif operation == "unknown":
        data["rows"][-1]["case"] = "not_a_case"
    else:
        data["loader_sha256"] = "bad"
    with pytest.raises(ValueError):
        probe.check_report(data)


def test_complete_saved_report_rechecks_without_docker_or_network(tmp_path):
    assert probe.check_report(report()) == 9
    (tmp_path / "report.json").write_text(json.dumps(report()))
    result = subprocess.run([sys.executable, str(Path(probe.__file__)), "--check", str(tmp_path)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert "9 frozen HTTP cases" in result.stdout


def identity_report():
    images = deepcopy(probe.IDENTITY_PINS)
    images["compat"] = {"image": "local-compat", "image_id": IMAGE_ID, "modules": [MODULES[1]]}
    return {"schema": 1, "mode": "identity", "images": images,
            "loader_sha256": probe.sha256(probe.LOADER.encode()),
            "rows": [record(case, images[role]["modules"])
                     for role in images for case in probe.identity_cases(role)]}


def test_identity_matrix_checks_actual_fixed_old_candidate_and_compat_results():
    assert probe.check_report(identity_report()) == 9
    assert {case["name"] for case in probe.identity_cases("old")} == {
        "default-old", "duplicate-old", "watch-old"}
    old, candidate = probe.identity_cases("old")[0], probe.identity_cases("candidate")[0]
    assert old["upstream"] == candidate["upstream"]
    assert old["steps"][0]["items"][0]["link"] == ""
    assert candidate["steps"][0]["items"][0]["link"] == "https://www.cls.cn/detail/920202"


@pytest.mark.parametrize("operation", ["pin", "coverage", "module", "loader"])
def test_identity_matrix_rejects_drift_or_incomplete_proof(operation):
    data = identity_report()
    if operation == "pin":
        data["images"]["old"]["image_id"] = IMAGE_ID
    elif operation == "coverage":
        data["rows"].pop()
    elif operation == "module":
        data["images"]["compat"]["modules"][0]["sha256"] = "bad"
    else:
        data["loader_sha256"] = "bad"
    with pytest.raises(ValueError):
        probe.check_report(data)


@pytest.mark.parametrize("mutation", ["path", "hash", "duplicate", "count"])
def test_module_identities_are_exact_and_unique(mutation):
    modules = deepcopy(MODULES)
    if mutation == "path":
        modules[0]["path"] = "/app/dist/index.mjs"
    elif mutation == "hash":
        modules[0]["sha256"] = "not a hash"
    elif mutation == "duplicate":
        modules[1] = modules[0]
    else:
        modules.pop()
    with pytest.raises(ValueError):
        probe.validate_modules(modules)


def container_info(directory):
    return {"Image": IMAGE_ID, "Config": {**CONFIG, "Labels": {probe.OWNER_LABEL: "owner"}},
            "HostConfig": {"Memory": probe.MEMORY_BYTES, "PidsLimit": 256, "NetworkMode": "none"},
            "State": {"OOMKilled": False, "Running": True, "ExitCode": 0}, "RestartCount": 0,
            "Mounts": [{"Destination": "/evidence", "Source": str(directory), "RW": False}]}


@pytest.mark.parametrize("mutation", ["memory", "pid", "network", "oom", "restart", "entrypoint", "cmd",
                                     "image", "owner", "mount", "port", "cpu"])
def test_container_limits_identity_default_command_and_isolation_are_verified(tmp_path, mutation):
    info = container_info(tmp_path)
    probe.validate_container(info, IMAGE_ID, CONFIG, "owner", tmp_path)
    if mutation in {"memory", "pid", "network", "port", "cpu"}:
        key, value = {"memory": ("Memory", 1024 ** 3), "pid": ("PidsLimit", 512),
                      "network": ("NetworkMode", "bridge"), "port": ("PortBindings", {"1200/tcp": []}),
                      "cpu": ("NanoCpus", 1000)}[mutation]
        info["HostConfig"][key] = value
    elif mutation == "oom":
        info["State"]["OOMKilled"] = True
    elif mutation == "restart":
        info["RestartCount"] = 1
    elif mutation in {"entrypoint", "cmd"}:
        info["Config"]["Entrypoint" if mutation == "entrypoint" else "Cmd"] = ["node"]
    elif mutation == "image":
        info["Image"] = "different"
    elif mutation == "owner":
        info["Config"]["Labels"][probe.OWNER_LABEL] = "not owned"
    else:
        info["Mounts"][0]["RW"] = True
    with pytest.raises(ValueError):
        probe.validate_container(info, IMAGE_ID, CONFIG, "owner", tmp_path)


@pytest.mark.parametrize("state", [
    {"Running": False, "ExitCode": 0}, {"Running": False, "ExitCode": 137},
    {"Running": True, "ExitCode": 1}, {"Running": True, "ExitCode": 137},
    {"ExitCode": 0}, {"Running": True}, {"Running": 1, "ExitCode": 0},
    {"Running": True, "ExitCode": False}, {"Running": True, "ExitCode": "0"},
])
def test_container_cannot_pass_after_stopping_or_with_missing_or_nonzero_exit_status(tmp_path, state):
    info = container_info(tmp_path)
    info["State"] = {"OOMKilled": False, **state}
    with pytest.raises(ValueError, match="not running cleanly"):
        probe.validate_container(info, IMAGE_ID, CONFIG, "owner", tmp_path)


def test_container_accepts_running_process_with_explicit_zero_exit_status(tmp_path):
    probe.validate_container(container_info(tmp_path), IMAGE_ID, CONFIG, "owner", tmp_path)


def test_container_command_keeps_image_defaults_and_no_cpu_relaxation(tmp_path):
    args = probe.container_args("name", "owner", tmp_path, "exact-local-image")
    assert args[args.index("--memory") + 1] == str(768 * 1024 * 1024)
    assert args[args.index("--pids-limit") + 1] == "256"
    assert args[args.index("--network") + 1] == "none"
    assert args[-1] == "exact-local-image"
    assert "--entrypoint" not in args and "--cpus" not in args and "--publish" not in args


@pytest.mark.parametrize("system,machine", [("Darwin", "arm64"), ("Linux", "aarch64"), ("Darwin", "x86_64")])
def test_native_gate_rejects_emulation_before_any_docker_call(monkeypatch, system, machine):
    monkeypatch.setattr(probe.platform, "system", lambda: system)
    monkeypatch.setattr(probe.platform, "machine", lambda: machine)
    monkeypatch.setattr(probe, "command", lambda *a, **k: pytest.fail("must stop before Docker"))
    with pytest.raises(ValueError, match="native Linux/amd64"):
        probe.native_docker()


def test_http_client_enters_only_network_namespace_not_container_cgroup(monkeypatch):
    calls = []
    monkeypatch.setattr(probe.os, "geteuid", lambda: 1000)

    def fake(args, **kwargs):
        calls.append((args, kwargs))
        return completed('{"status":200,"body":"","client_cgroup":"host"}')

    monkeypatch.setattr(probe, "command", fake)
    assert probe.host_request(123, "/healthz")["client_cgroup"] == "host"
    args = calls[0][0]
    assert args[:8] == ["sudo", "-n", "nsenter", "--target", "123", "--net", "--", sys.executable]
    assert "--cgroup" not in args and "docker" not in args
    assert calls[0][1]["timeout"] == 22


def test_cleanup_never_removes_unowned_container(monkeypatch):
    monkeypatch.setattr(probe, "inspect_container", lambda *a: {"Config": {"Labels": {probe.OWNER_LABEL: "other"}}})
    monkeypatch.setattr(probe, "command", lambda *a, **k: pytest.fail("must not remove unowned container"))
    with pytest.raises(ValueError, match="not owned"):
        probe.cleanup_owned(["docker"], "name", "owner")


def test_readiness_accepts_initial_connection_refusal_within_original_startup_budget(monkeypatch):
    requests = []

    def request(pid, path, **kwargs):
        requests.append((pid, path, kwargs))
        if len(requests) == 1:
            raise RuntimeError("connection refused while server is starting")
        return {"status": 200}

    monkeypatch.setattr(probe, "host_request", request)
    monkeypatch.setattr(probe.time, "sleep", lambda *_: None)
    probe.wait_ready(123)
    assert requests == [(123, "/healthz", {"timeout": 3})] * 2


def test_transport_injection_changes_only_unique_import_not_handler_body():
    prefix = b'import{t}from"./cache-X.mjs";'
    transport = b'import{t as request}from"./got-ABC.mjs";'
    body = b'const handler=async()=>request({url:"frozen"});export{handler};'
    source = prefix + transport + body
    module = {"path": "/app/dist/wallstreetcn-news-Fake.mjs", "sha256": probe.sha256(source)}
    result = probe.inject_transport(source, module)
    assert result.startswith(prefix) and result.endswith(body)
    assert b'got as request' in result and b'/evidence/transport.mjs' in result
    assert b'__compatVerify(' in result and module["sha256"].encode() in result
    assert b'NODE_OPTIONS' not in probe.LOADER.encode()


@pytest.mark.parametrize("source", [b'export{};', b'import{t}from"./got-A.mjs";import{t as q}from"./ofetch-B.mjs";'])
def test_injection_rejects_absent_or_ambiguous_transport(source):
    with pytest.raises(ValueError, match="exactly one"):
        probe.inject_transport(source, {"path": "/app/dist/cls-telegraph-X.mjs", "sha256": probe.sha256(source)})


def test_injection_rejects_changed_original_handler_hash():
    with pytest.raises(ValueError, match="hash differs"):
        probe.inject_transport(b'import{t}from"./got-A.mjs";', MODULES[0])


def test_overlay_mounts_are_readonly_and_cannot_hide_other_application_files(tmp_path):
    info = container_info(tmp_path)
    for module in MODULES:
        info["Mounts"].append({"Destination": module["path"],
                               "Source": str(tmp_path / ("injected-" + Path(module["path"]).name)), "RW": False})
    probe.validate_container(info, IMAGE_ID, CONFIG, "owner", tmp_path, MODULES)
    info["Mounts"][-1]["Destination"] = "/app/package.json"
    with pytest.raises(ValueError, match="mount"):
        probe.validate_container(info, IMAGE_ID, CONFIG, "owner", tmp_path, MODULES)
    args = probe.container_args("owned", "owner", tmp_path, "exact-image", MODULES)
    assert not any("NODE_OPTIONS" in arg for arg in args)
    assert args[-1] == "exact-image" and args.count("--mount") == 3


def test_readiness_does_not_extend_fixed_startup_budget(monkeypatch):
    ticks = iter([0, 0, 26])
    monkeypatch.setattr(probe.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(probe.time, "sleep", lambda *_: None)
    monkeypatch.setattr(probe, "host_request", lambda *_a, **_k: {"status": 503})
    with pytest.raises(TimeoutError, match="25 seconds"):
        probe.wait_ready(123)


def test_cleanup_removes_owned_even_when_log_capture_fails(monkeypatch):
    monkeypatch.setattr(probe, "inspect_container", lambda *a: {
        "Id": "owned-id", "Config": {"Labels": {probe.OWNER_LABEL: "owner"}}})
    calls = []

    def fake(args, **kwargs):
        calls.append(args)
        return completed(code=1 if "logs" in args else 0)

    monkeypatch.setattr(probe, "command", fake)
    with pytest.raises(RuntimeError, match="logs"):
        probe.cleanup_owned(["docker"], "name", "owner")
    assert calls[-1] == ["docker", "rm", "--force", "owned-id"]
