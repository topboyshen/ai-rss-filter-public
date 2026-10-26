"""Frozen HTTP checks of a locally built RSSHub compatibility image (explicit only).

This is not part of default pytest: it starts one isolated, network-none container
per case on a native Linux/amd64 Docker host. The image's default entrypoint/CMD
run unchanged; a hash-checked read-only overlay substitutes only upstream imports.
The HTTP client runs in the host cgroup, entering only the owned container's
network namespace. Resource limits are always 768 MiB / 256 PID. No images are
pulled and no production network, database, secret, or Docker socket is mounted.

--run requires a new evidence directory, an exact local image ID, and a JSON
module manifest. --check revalidates all saved evidence without Docker/network.
Failures retain response bodies, logs, fixtures and inspection evidence.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET


OWNER_LABEL = "ai-filter.rsshub-compat.run"
MEMORY_BYTES = 768 * 1024 * 1024
PID_LIMIT = 256
MAX_BODY_BYTES = 1024 * 1024
BASE_IMAGE = "diygod/rsshub@sha256:e7cd1bd8838b91f89c2ed56b0889f3754edc6aa1f1f5dac002e2e97e1079bd54"
T0 = 1792130400
IDENTITY_PINS = {
    "old": {
        "image": "diygod/rsshub@sha256:6ba8ade0743278daf37070e3253f1c8d26a74b8f1d367b6d4fa1fc785dbe3291",
        "image_id": "sha256:6e67569402f378f78f5ba43a06cc767fc387bbd57ce1ccde6f600a248a13cc4b",
        "modules": [{"path": "/app/dist/cls-telegraph-DU4oR7sr.mjs",
                     "sha256": "7c0c1ef5fc14e5a60df3ca4329204a972b4ea5c9490762b1081c08e1e816df03"}],
    },
    "candidate": {
        "image": BASE_IMAGE,
        "image_id": "sha256:a4c72dc4073603acf4942863c0e427b64792c8c26f5fcc4d95e5f1fa99ac4532",
        "modules": [{"path": "/app/dist/cls-telegraph-DFOzEo9d.mjs",
                     "sha256": "06d7f8eed8704604c0848084a1f595ce1dc0c2926c5444b6f1fc38eefcd604de"}],
    },
}

# Only import bindings to the real got/ofetch transport are substituted. The
# actual route, cache, date parser, renderer and HTTP middleware are untouched.
LOADER = r"""
import fs from 'node:fs';
import {createHash} from 'node:crypto';
const sha = s => createHash('sha256').update(s).digest('hex');
const raw = fs.readFileSync('/evidence/fixture.json');
const fixture = JSON.parse(raw);
const modules = JSON.parse(fs.readFileSync('/evidence/modules.json', 'utf8'));
const counts = new Map();
const request = async (kind, argument, options = {}) => {
  const url = kind === 'got' ? argument.url : argument;
  const query = kind === 'got' ? null : options.query;
  const matches = fixture.upstream.filter(x => x.kind === kind && x.url === url &&
    Object.entries(x.query ?? {}).every(([k,v]) => query?.[k] === v));
  if (matches.length !== 1) throw Error('unexpected_upstream_request:' + kind + ':' + url);
  const response = matches[0];
  const count = (counts.get(response.key) ?? 0) + 1;
  if (count > response.count) throw Error('unexpected_upstream_repeat:' + response.key);
  counts.set(response.key, count);
  console.error('COMPAT_UPSTREAM=' + JSON.stringify({key:response.key,kind,url,query}));
  console.error('COMPAT_FIXTURE_SHA256=' + sha(raw));
  return kind === 'got' ? {data:structuredClone(response.response)} : structuredClone(response.response);
};
export const got = argument => request('got', argument);
export const ofetch = (argument, options) => request('ofetch', argument, options);
export function verifyModule(path, expected) {
  const module = modules.find(x => x.path === path && x.sha256 === expected);
  if (!module) throw Error('unlisted_compat_module:' + path);
  const source = fs.readFileSync('/evidence/original-' + path.split('/').at(-1));
  if (sha(source) !== expected) throw Error('compat_module_identity_mismatch:' + path);
  console.error('COMPAT_MODULE_SHA256=' + JSON.stringify({path:module.path,sha256:sha(source)}));
}
"""

FETCH = r"""
import base64, json, sys, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None
request = urllib.request.Request(sys.argv[1], headers={'User-Agent':'AI-Filter isolated compatibility check'})
try:
    response = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(request, timeout=20)
except urllib.error.HTTPError as error:
    response = error
body = response.read(1048577)
if len(body) > 1048576:
    raise ValueError('response_too_large')
print(json.dumps({'status':response.status,'body':base64.b64encode(body).decode(),
                  'client_cgroup':open('/proc/self/cgroup').read()}))
"""


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def epoch(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def upstream(key, url, response, *, kind="got", query=None, count=1):
    return {"key": key, "kind": kind, "url": url, "query": query or {}, "response": response, "count": count}


def ws_listing(channel, items):
    url = f"https://api-one.wallstcn.com/apiv1/content/information-flow?channel={channel}&accept=article&limit=25"
    return upstream("listing_" + channel, url, {"data": {"items": items}})


def ws_resource(kind, identity, date=T0):
    prefix = "livenews" if kind == "live" else "articles"
    return {"resource_type": kind, "resource": {"id": identity, "uri": f"https://wallstreetcn.com/{prefix}/{identity}",
                                                "display_time": date}}


def ws_detail(kind, identity, title, *, deleted=False):
    suffix = f"lives/{identity}" if kind == "live" else f"articles/{identity}?extract=0"
    response = {"code": 20000, "data": {"title": title, "content": f"<p>{title}正文</p>", "source_name": "冻结来源"}}
    if deleted:
        response = {"code": 404, "data": None}
    return upstream("detail_" + str(identity), "https://api-one.wallstcn.com/apiv1/content/" + suffix, response)


def expected_ws(kind, identity, title, date=T0):
    prefix = "livenews" if kind == "live" else "articles"
    return {"title": title, "link": f"https://wallstreetcn.com/{prefix}/{identity}", "guid": str(identity),
            "description": f"<p>{title}正文</p>", "pubDate": epoch(date)}


def mixed_ws():
    items = [ws_resource("article", 1001), ws_resource("live", 1002, T0 + 60),
             {"resource_type": "ad", "resource": None}, ws_resource("article", 1003)]
    responses = [ws_listing("global-channel", items), ws_detail("article", 1001, "文章A"),
                 ws_detail("live", 1002, "快讯B"), ws_detail("article", 1003, "已删除", deleted=True)]
    # The real RSS middleware orders items by pubDate descending (not API order).
    expected = [expected_ws("live", 1002, "快讯B", T0 + 60), expected_ws("article", 1001, "文章A")]
    return responses, expected


def cls_payload(*, links=False, duplicate=False):
    payload = {"data": {"roll_data": [
        {"id": 920201, "title": "财联社同标题", "content": "第一条原始正文", "ctime": T0,
         "shareurl": "https://www.cls.cn/detail/920201"},
        {"id": 920202, "title": "", "content": "无标题快讯正文", "ctime": T0 + 60,
         "shareurl": "https://www.cls.cn/detail/920202"},
    ]}}
    if not links:
        for item in payload["data"]["roll_data"]:
            item.pop("shareurl")
    if duplicate:
        payload["data"]["roll_data"][1]["title"] = "财联社同标题"
    return payload


def cls_case(name, *, category=None, links=False, duplicate=False, role="compat"):
    payload = cls_payload(links=links, duplicate=duplicate)
    items = []
    for raw in payload["data"]["roll_data"]:
        title = raw["title"] or raw["content"]
        link = raw.get("shareurl", "")
        if not link and (role == "candidate" or (role == "compat" and category is not None)):
            link = f"https://www.cls.cn/detail/{raw['id']}"
        items.append({"title": title, "link": link, "guid": link or title,
                      "description": raw["content"], "pubDate": epoch(raw["ctime"])})
    url = "https://www.cls.cn/api/cache" if category is None else "https://www.cls.cn/v1/roll/get_roll_list"
    query = {"name": "telegraph"} if category is None else {"category": category}
    return {"name": name, "upstream": [upstream("cls_list", url, payload, kind="ofetch", query=query)],
            "steps": [{"path": "/cls/telegraph" + ("/" + category if category else ""), "items": items[::-1]}]}


def cases():
    result = []
    for name, path in (("global_default_mixed", "/wallstreetcn/news"),
                       ("global_explicit_mixed", "/wallstreetcn/news/global")):
        responses, expected = mixed_ws()
        result.append({"name": name, "upstream": responses, "steps": [{"path": path, "items": expected}]})
    for category in ("china", "not-a-real-category"):
        result.append({"name": "unsupported_" + category, "upstream": [],
                       "steps": [{"path": "/wallstreetcn/news/" + category, "error": "unsupported_category"}]})
    # A non-global category must retain its original API channel and article-only
    # selection. Sharing cached detail must not replace listing-specific dates.
    article = ws_resource("article", 1001)
    shares = ws_listing("shares", [ws_resource("article", 1001, T0 + 3600)])
    result.append({"name": "detail_cache_keeps_listing_dates", "upstream": [
        ws_listing("global-channel", [article]), shares, ws_detail("article", 1001, "共享详情")], "steps": [
            {"path": "/wallstreetcn/news/global", "items": [expected_ws("article", 1001, "共享详情")]},
            {"path": "/wallstreetcn/news/shares", "items": [expected_ws("article", 1001, "共享详情", T0 + 3600)]}]})
    result.extend([cls_case("cls_bare_identity"), cls_case("cls_bare_existing_link", links=True),
                   cls_case("cls_bare_duplicate_titles", duplicate=True),
                   cls_case("cls_watch_unchanged", category="watch", links=True)])
    return result


def identity_cases(role):
    return [cls_case("default-" + role, role=role), cls_case("duplicate-" + role, duplicate=True, role=role),
            cls_case("watch-" + role, category="watch", links=True, role=role)]


def command(args, *, timeout=20, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {result.stderr[-1200:]}")
    return result


def native_docker():
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise ValueError("native Linux/amd64 host required; emulation is not an acceptance environment")
    if any(os.environ.get(key) for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY")):
        raise ValueError("Docker environment overrides are not allowed")
    context = command(["docker", "context", "show"]).stdout.strip()
    address = command(["docker", "context", "inspect", context, "--format", "{{.Endpoints.docker.Host}}"])
    if not address.stdout.strip().startswith("unix:///"):
        raise ValueError("a local Unix-socket Docker daemon is required")
    docker = ["docker", "--context", context]
    info = json.loads(command([*docker, "info", "--format", "{{json .}}"] ).stdout)
    if info.get("OSType") != "linux" or info.get("Architecture") not in {"x86_64", "amd64"}:
        raise ValueError("native Linux/amd64 Docker daemon required")
    return docker


def validate_modules(modules, *, identity_only=False):
    if not isinstance(modules, list) or len(modules) != (1 if identity_only else 2):
        raise ValueError("exactly the wallstreetcn-news and cls-telegraph module identities are required")
    names = set()
    for module in modules:
        path, identity = module.get("path", ""), module.get("sha256", "")
        match = re.fullmatch(r"/app/dist/(wallstreetcn-news|cls-telegraph)-[\w-]+\.mjs", path)
        if (not match or not re.fullmatch(r"[0-9a-f]{64}", identity) or match[1] in names
                or (identity_only and match[1] != "cls-telegraph")):
            raise ValueError("invalid or duplicate module identity")
        names.add(match[1])
    return modules


def container_args(name, owner, directory, image, modules=()):
    args = ["create", "--pull=never", "--name", name, "--label", f"{OWNER_LABEL}={owner}",
            "--platform", "linux/amd64", "--network", "none", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--memory", str(MEMORY_BYTES), "--pids-limit", str(PID_LIMIT),
            "--mount", f"type=bind,src={directory},dst=/evidence,readonly", "-e", "NODE_ENV=production",
            "-e", "CACHE_TYPE=memory"]
    for module in modules:
        source = directory / ("injected-" + Path(module["path"]).name)
        args += ["--mount", f"type=bind,src={source},dst={module['path']},readonly"]
    return [*args, image]


def inspect_container(docker, name):
    return json.loads(command([*docker, "inspect", name]).stdout)[0]


def validate_container(info, image_id, image_config, owner, directory, modules=()):
    cfg, host, state = info["Config"], info["HostConfig"], info["State"]
    if info["Image"] != image_id or cfg.get("Labels", {}).get(OWNER_LABEL) != owner:
        raise ValueError("container image or ownership differs")
    if cfg.get("Entrypoint") != image_config.get("Entrypoint") or cfg.get("Cmd") != image_config.get("Cmd"):
        raise ValueError("default image entrypoint or command changed")
    if (host.get("Memory"), host.get("PidsLimit"), host.get("NetworkMode")) != (MEMORY_BYTES, PID_LIMIT, "none"):
        raise ValueError("resource or network isolation differs")
    if host.get("NanoCpus", 0) or host.get("CpuQuota", 0) or host.get("PortBindings"):
        raise ValueError("unexpected CPU restriction or published port")
    mounts = info.get("Mounts", [])
    expected = {"/evidence": str(directory), **{
        module["path"]: str(directory / ("injected-" + Path(module["path"]).name)) for module in modules}}
    if (len(mounts) != len(expected) or {m.get("Destination") for m in mounts} != set(expected)
            or any(m.get("RW") or m.get("Source") != expected.get(m.get("Destination")) for m in mounts)):
        raise ValueError("unexpected container mount")
    if state.get("OOMKilled") or info.get("RestartCount", 0):
        raise ValueError("container OOM or restart observed")
    if state.get("Running") is not True or type(state.get("ExitCode")) is not int or state["ExitCode"] != 0:
        raise ValueError("container is not running cleanly or exited with a nonzero status")


def host_request(pid, path, *, timeout=22):
    if not isinstance(pid, int) or pid <= 0 or not path.startswith("/"):
        raise ValueError("invalid owned container namespace or path")
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    result = command([*prefix, "nsenter", "--target", str(pid), "--net", "--", sys.executable,
                      "-c", FETCH, "http://127.0.0.1:1200" + path], timeout=timeout)
    return json.loads(result.stdout)


def wait_ready(pid):
    """Port-not-yet-listening is expected only inside the fixed startup budget."""
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        try:
            if host_request(pid, "/healthz", timeout=3)["status"] == 200:
                return
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            pass
        time.sleep(0.4)
    raise TimeoutError("isolated container did not start in 25 seconds")


def save_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def parse_items(body):
    if len(body) > MAX_BODY_BYTES or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise ValueError("unsafe or oversized XML")
    root = ET.fromstring(body)
    if root.tag != "rss" or len(root.findall("channel")) != 1:
        raise ValueError("not one RSS channel")
    rows = []
    for node in root.findall("channel/item"):
        guid = node.findall("guid")
        if len(guid) != 1 or guid[0].get("isPermaLink") != "false":
            raise ValueError("GUID must explicitly preserve isPermaLink=false")
        row = {}
        for key in ("title", "link", "guid", "description", "pubDate"):
            if len(node.findall(key)) > 1:
                raise ValueError("duplicate item field")
            row[key] = node.findtext(key) or ""
        rows.append(row)
    return rows


def validate_row(case, row, modules):
    import base64

    if row.get("error") or row.get("cleanup") != "removed":
        raise ValueError("execution or owned-container cleanup failed")
    if row.get("fixture_sha256") != sha256(json.dumps(case, ensure_ascii=False, sort_keys=True).encode()):
        raise ValueError("fixture identity differs")
    logs = row.get("logs", "")
    for module in modules:
        if module["path"].split("/")[-1].split("-")[0] not in case["steps"][0]["path"]:
            continue
        marker = "COMPAT_MODULE_SHA256=" + json.dumps(module, separators=(",", ":"))
        if logs.count(marker) != 1:
            raise ValueError("actual hash-checked handler proof missing or repeated")
    actual_requests = [json.loads(line.split("COMPAT_UPSTREAM=", 1)[1]) for line in logs.splitlines()
                       if "COMPAT_UPSTREAM=" in line]
    if Counter(x["key"] for x in actual_requests) != Counter({x["key"]: x["count"] for x in case["upstream"]}):
        raise ValueError("upstream calls, cache reuse, or zero-request rejection differ")
    if logs.count("COMPAT_FIXTURE_SHA256=" + row["fixture_sha256"]) != len(actual_requests):
        raise ValueError("frozen input proof missing")
    if len(row.get("steps", [])) != len(case["steps"]):
        raise ValueError("HTTP step coverage incomplete")
    for expected, actual in zip(case["steps"], row["steps"]):
        body = base64.b64decode(actual["body"], validate=True)
        if sha256(body) != actual.get("body_sha256") or actual.get("path") != expected["path"]:
            raise ValueError("HTTP body or path identity differs")
        if "error" in expected:
            if actual["status"] != 404 or b"Unsupported Wallstreetcn news category" not in body:
                raise ValueError("unsupported category was not explicitly rejected with 404")
        elif actual["status"] != 200 or parse_items(body) != expected["items"]:
            raise ValueError("RSS items, identity, content, ordering, or dates differ")


def cleanup_owned(docker, name, owner):
    info = inspect_container(docker, name)
    if info.get("Config", {}).get("Labels", {}).get(OWNER_LABEL) != owner:
        raise ValueError("refusing to clean a container not owned by this run")
    try:
        result = command([*docker, "logs", name], check=False)
        if result.returncode:
            raise RuntimeError("container logs could not be preserved")
        return result.stdout + result.stderr
    finally:
        command([*docker, "rm", "--force", info["Id"]])


def inject_transport(source, module):
    """Change exactly one transport import, leaving every other byte intact."""
    if sha256(source) != module["sha256"]:
        raise ValueError("actual image handler hash differs")
    pattern = rb'import\{t(?: as ([a-zA-Z_$][\w$]*))?\}from"\./(got|ofetch)-[^"]+";'
    matches = list(re.finditer(pattern, source))
    if len(matches) != 1:
        raise ValueError("exactly one upstream transport import required")
    match = matches[0]
    binding, kind = (match[1] or b"t").decode(), match[2].decode()
    replacement = (f'import{{{kind} as {binding},verifyModule as __compatVerify}}from"/evidence/transport.mjs";'
                   f'__compatVerify({json.dumps(module["path"])},{json.dumps(module["sha256"])});').encode()
    return source[:match.start()] + replacement + source[match.end():]


def prepare_overlays(docker, directory, image, image_id, owner, modules):
    """Read originals from an owned, never-started container, not a copied implementation."""
    name = "rsshub-compat-extract-" + uuid.uuid4().hex[:16]
    created = False
    try:
        command([*docker, "create", "--pull=never", "--name", name, "--label", f"{OWNER_LABEL}={owner}",
                 "--platform", "linux/amd64", "--network", "none", image])
        created = True
        info = inspect_container(docker, name)
        if info["Image"] != image_id or info["State"]["Running"] or info["State"]["StartedAt"][:4] != "0001":
            raise ValueError("source extraction container identity or never-started state differs")
        save_json(directory / "source-image-inspect.json", info)
        proof = []
        for module in modules:
            original = directory / ("original-" + Path(module["path"]).name)
            command([*docker, "cp", name + ":" + module["path"], str(original)])
            source = original.read_bytes()
            injected = inject_transport(source, module)
            (directory / ("injected-" + Path(module["path"]).name)).write_bytes(injected)
            proof.append({**module, "injected_sha256": sha256(injected), "transport_import_replacements": 1})
        save_json(directory / "transport-overlay-proof.json", proof)
    finally:
        if created:
            cleanup_owned(docker, name, owner)


def run_case(docker, output, owner, case, image, image_id, image_config, modules):
    import base64

    directory = output / case["name"]
    directory.mkdir(mode=0o755)
    raw = json.dumps(case, ensure_ascii=False, sort_keys=True).encode()
    (directory / "fixture.json").write_bytes(raw)
    (directory / "transport.mjs").write_text(LOADER, encoding="utf-8")
    save_json(directory / "modules.json", modules)
    name = "rsshub-compat-" + owner[:12] + "-" + case["name"].replace("_", "-")
    row = {"case": case["name"], "fixture_sha256": sha256(raw), "steps": [], "started_at": time.time()}
    created = False
    try:
        prepare_overlays(docker, directory, image, image_id, owner, modules)
        command([*docker, *container_args(name, owner, directory, image, modules)])
        created = True
        command([*docker, "start", name])
        info = inspect_container(docker, name)
        validate_container(info, image_id, image_config, owner, directory, modules)
        save_json(directory / "created-inspect.json", info)
        wait_ready(info["State"]["Pid"])
        for index, step in enumerate(case["steps"]):
            response = host_request(info["State"]["Pid"], step["path"])
            body = base64.b64decode(response["body"], validate=True)
            (directory / f"step-{index}.body").write_bytes(body)
            response.update(path=step["path"], body_sha256=sha256(body))
            row["steps"].append(response)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        row["error"] = f"{type(error).__name__}: {error}"
    finally:
        if created:
            try:
                final = inspect_container(docker, name)
                save_json(directory / "final-inspect.json", final)
                validate_container(final, image_id, image_config, owner, directory, modules)
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
                row["error"] = f"final inspection: {type(error).__name__}: {error}"
            try:
                row["logs"] = cleanup_owned(docker, name, owner)
                (directory / "container.log").write_text(row["logs"], encoding="utf-8")
                row["cleanup"] = "removed"
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
                row["cleanup"] = f"unconfirmed: {error}"
        row["finished_at"] = time.time()
        save_json(directory / "result.json", row)
    return row


def check_report(report):
    if report.get("mode") == "identity":
        return check_identity_report(report)
    modules = validate_modules(report["modules"])
    expected = {case["name"]: case for case in cases()}
    rows = report.get("rows", [])
    if report.get("schema") != 1 or report.get("loader_sha256") != sha256(LOADER.encode()):
        raise ValueError("report schema or loader identity differs")
    if len(rows) != len(expected) or {row.get("case") for row in rows} != set(expected):
        raise ValueError("frozen matrix coverage incomplete or repeated")
    for row in rows:
        validate_row(expected[row["case"]], row, modules)
    return len(rows)


def check_identity_report(report):
    expected = {case["name"]: case for role in ("old", "compat", "candidate") for case in identity_cases(role)}
    if report.get("schema") != 1 or report.get("loader_sha256") != sha256(LOADER.encode()):
        raise ValueError("identity report schema or loader differs")
    rows = report.get("rows", [])
    if len(rows) != len(expected) or {row.get("case") for row in rows} != set(expected):
        raise ValueError("three-version actual HTTP coverage incomplete or duplicated")
    for role, pin in IDENTITY_PINS.items():
        if report["images"].get(role) != pin:
            raise ValueError("fixed old/candidate identity differs")
    for row in rows:
        role = row["case"].rsplit("-", 1)[1]
        modules = validate_modules(report["images"][role]["modules"], identity_only=True)
        validate_row(expected[row["case"]], row, modules)
    return len(rows)


def image_inspect(docker, image, image_id):
    info = json.loads(command([*docker, "image", "inspect", image]).stdout)[0]
    if info["Id"] != image_id or (info.get("Os"), info.get("Architecture")) != ("linux", "amd64"):
        raise ValueError("local built image identity or platform differs")
    return info


def run_identity_matrix(output, image, image_id, modules):
    import base64

    if not output.is_absolute() or "," in str(output):
        raise ValueError("new absolute identity evidence directory required")
    modules = validate_modules(modules)
    images = {**IDENTITY_PINS, "compat": {"image": image, "image_id": image_id,
              "modules": [module for module in modules if "/cls-telegraph-" in module["path"]]}}
    docker = native_docker()
    inspections = {role: image_inspect(docker, pin["image"], pin["image_id"]) for role, pin in images.items()}
    output.mkdir(mode=0o755, parents=False, exist_ok=False)
    rss = output / "rss"
    rss.mkdir(mode=0o755)
    report = {"schema": 1, "mode": "identity", "images": images, "loader_sha256": sha256(LOADER.encode()),
              "started_at": time.time(), "rows": []}
    owner = uuid.uuid4().hex
    try:
        for role, pin in images.items():
            save_json(output / (role + "-image-inspect.json"), inspections[role])
            for case in identity_cases(role):
                row = run_case(docker, output, owner, case, pin["image"], pin["image_id"],
                               inspections[role]["Config"], pin["modules"])
                report["rows"].append(row)
                try:
                    validate_row(case, row, pin["modules"])
                    (rss / (case["name"] + ".xml")).write_bytes(base64.b64decode(row["steps"][0]["body"]))
                    print("PASS actual identity HTTP " + case["name"], flush=True)
                except (ValueError, KeyError, ET.ParseError) as error:
                    row["validation_error"] = str(error)
                    print("FAIL actual identity HTTP " + case["name"] + ": " + str(error), flush=True)
    finally:
        report["finished_at"] = time.time()
        save_json(output / "report.json", report)
    return check_identity_report(report)


def run_matrix(output, image, image_id, modules):
    if not output.is_absolute() or "," in str(output):
        raise ValueError("new absolute evidence directory without commas required")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("exact built image config ID required")
    validate_modules(modules)
    docker = native_docker()
    info = image_inspect(docker, image, image_id)
    output.mkdir(mode=0o755, parents=False, exist_ok=False)
    owner = uuid.uuid4().hex
    report = {"schema": 1, "image": image, "image_id": image_id, "image_config": info["Config"], "modules": modules,
              "loader_sha256": sha256(LOADER.encode()), "started_at": time.time(), "rows": []}
    save_json(output / "image-inspect.json", info)
    try:
        for case in cases():
            row = run_case(docker, output, owner, case, image, image_id, info["Config"], modules)
            report["rows"].append(row)
            try:
                validate_row(case, row, modules)
                print(f"PASS {case['name']}", flush=True)
            except (ValueError, KeyError, ET.ParseError) as error:
                row["validation_error"] = str(error)
                print(f"FAIL {case['name']}: {error}", flush=True)
    finally:
        report["finished_at"] = time.time()
        save_json(output / "report.json", report)
    return check_report(report)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", type=Path)
    mode.add_argument("--identity-run", type=Path, help="three fixed versions' actual CLS RSS for Miniflux migration")
    mode.add_argument("--check", type=Path)
    parser.add_argument("--image")
    parser.add_argument("--image-id")
    parser.add_argument("--modules", type=Path, help="JSON list of exact patched dist path/sha256 identities")
    args = parser.parse_args(argv)
    try:
        if args.run or args.identity_run:
            if not args.image or not args.image_id or not args.modules:
                parser.error("--run also requires --image, --image-id and --modules")
            runner = run_identity_matrix if args.identity_run else run_matrix
            count = runner(args.identity_run or args.run, args.image, args.image_id, json.loads(args.modules.read_text()))
        else:
            count = check_report(json.loads((args.check / "report.json").read_text()))
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, ET.ParseError, subprocess.SubprocessError) as error:
        print(f"FAIL: {type(error).__name__}: {error}")
        return 1
    print(f"PASS: {count} frozen HTTP cases; not a production or historical-503 proof")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
