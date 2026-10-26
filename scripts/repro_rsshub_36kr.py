"""Offline regression of two pinned RSSHub images' actual /36kr/newsflashes HTTP route.

Run explicitly on a LOCAL Docker daemon with both linux/amd64 images already pulled:
  python scripts/repro_rsshub_36kr.py --run /absolute/new-evidence-directory
Recheck saved evidence without Docker/network:
  python scripts/repro_rsshub_36kr.py --check /absolute/evidence-directory

Only transport is substituted with synthetic HTML; the pinned parser, HTTP error
handling and RSS serializer run unchanged. Every case gets a fresh, network-none
container. Nothing is deployed, no images are pulled, and evidence is never deleted.
Exit 0 means the complete EXPECTED matrix reproduced (including old-image defects),
not that both images pass every input or that a historical production 503 is explained.
"""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET


PINS = {
    "old": {
        "image": "diygod/rsshub@sha256:6ba8ade0743278daf37070e3253f1c8d26a74b8f1d367b6d4fa1fc785dbe3291",
        "id": "sha256:9397de86f3e25daa335c471d00a83b3b9e1caabdc2fb67ab90d505efab888ce1",
        "revision": "d51b0601258220e3114b76f09e826c6e2acf2f96",
        "route": "/app/dist/36kr-B7QjtH9X.mjs",
        "route_sha256": "a486ccf0b8c04705c6561ba0eb9997798500c7e048f789be28fbc7d3e667fb53",
    },
    "candidate": {
        "image": "diygod/rsshub@sha256:e7cd1bd8838b91f89c2ed56b0889f3754edc6aa1f1f5dac002e2e97e1079bd54",
        "id": "sha256:db4640df4f0bc15f1a28c576d15553249afb03e282d572893b43b6322407ddbe",
        "revision": "f23ada8a636fbbb5f92d8e91558cf4e8ffaca66a",
        "route": "/app/dist/36kr-CivCkmAY.mjs",
        "route_sha256": "a895634e34351d6074c6015619941d6c6398e098331fedbf44375deb9b83085e",
    },
}
OWNER_LABEL = "ai-filter.36kr-repro.run"
MAX_BODY_BYTES = 1024 * 1024
ENCRYPTED = (
    "VqtQgRz3gxvPRlPU22kQr3WtTGBBUPR8fhWyYvYYk7Kx3c9CZclOaK6vLX+DRQr18miru6GvcnpL90Do/nJEJn1R5OOrYfJ6F7g4OyKYn"
    "SwjOf1sxQTYpoZlhCfL/qRkMVSTbHqrKYUoTlWp8Fa1ZKkiiqrAGgedg6ERhHozpMsFwEh4BKufTthTftIV/nTNsFyEWv4JXy3Z5Hhdd"
    "cf8jqvCbD/8dP+In/7HlUkHy8hlYAldT8Hy+WCRinRvfyfFraFf2aUovzsS0eO4b2CTow=="
)

# Test-only Node loader, not an RSSHub feature flag. The source hash and unique
# import replacement prevent a bypass from masquerading as an actual-route test.
LOADER = r"""
import fs from 'node:fs';
import {createHash} from 'node:crypto';
import {registerHooks} from 'node:module';
import {fileURLToPath} from 'node:url';
const hash = s => createHash('sha256').update(s).digest('hex');
globalThis.__36krMockGot = async options => {
  if (options.method !== 'get' || options.url !== 'https://www.36kr.com/newsflashes')
    throw Error('unexpected_upstream_request');
  const body = fs.readFileSync('/evidence/input.html', 'utf8');
  console.error('REPRO_INPUT_SHA256=' + hash(body));
  return {data:body,statusCode:200,headers:{'content-type':'text/html; charset=utf-8'}};
};
registerHooks({load(url, context, nextLoad) {
  const result = nextLoad(url, context);
  if (url.startsWith('file:') && fileURLToPath(url) === process.env.REPRO_ROUTE) {
    const source = String(result.source);
    if (hash(source) !== process.env.REPRO_ROUTE_SHA256) throw Error('route_identity_mismatch');
    let count = 0;
    const changed = source.replace(/import\{t as ([a-zA-Z_$][\w$]*)\}from"\.\/got-[^"]+";/g,
      (_, name) => { count++; return `const ${name}=globalThis.__36krMockGot;`; });
    if (count !== 1) throw Error('transport_patch_count_' + count);
    console.error('REPRO_ROUTE_SHA256=' + hash(source));
    return {...result, source:changed};
  }
  return result;
}});
"""
FETCH = """
const r=await fetch('http://127.0.0.1:1200/36kr/newsflashes',{signal:AbortSignal.timeout(12000)});
const bytes=Buffer.from(await r.arrayBuffer());
if(bytes.length>1048576)throw Error('response_too_large');
console.log(JSON.stringify({status:r.status,body:bytes.toString('base64')}));
"""
HEALTH = """
fetch('http://127.0.0.1:1200/healthz',{signal:AbortSignal.timeout(1000)})
.then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(2));
"""


def digest(value):
    return hashlib.sha256(value).hexdigest()


def wrap(state):
    return '<html><title>快讯_36氪</title><script>window.initialState=' + json.dumps(
        state, ensure_ascii=False, separators=(",", ":")
    ) + '</script></html>'


def item(**changes):
    return {"itemType": 1, "itemId": "repro-001", "widgetTitle": "隔离测试快讯",
            "widgetContent": "完整合法内容", "publishTime": 1792130400000, "author": "fixture", **changes}


def rss_items(items):
    return [{"title": i["widgetTitle"], "link": "https://www.36kr.com/newsflashes/" + i["itemId"],
             "description": i["widgetContent"]} for i in items]


def cases():
    """Frozen synthetic expectations, not a reimplementation of either parser."""
    result = []

    def add(name, items, old_ok=True, candidate_ok=True, html=None):
        result.append({"name": name, "html": html if html is not None else wrap({"newsflash": {"itemList": items}}),
                       "items": rss_items(items), "status": {"old": 200 if old_ok else 503,
                                                             "candidate": 200 if candidate_ok else 503}})

    # Same title is legal: identity is not title uniqueness. Preserve input order.
    add("plain_duplicate_titles", [item(), item(itemId="repro-002", widgetContent="第二条不同内容")])
    # Parser accepts []; the actual RSSHub HTTP middleware rejects empty routes.
    add("empty_list", [], False, False)
    add("title_bracket", [item(widgetTitle="发布[更正]说明")], False)
    add("content_bracket", [item(widgetContent="指数[测试]上涨，原文完整")], False)
    add("closing_bracket", [item(widgetContent="正文含单独 ] 字符")], False)
    add("nested_array", [item(tags=["快讯", "市场"])], False)
    add("nested_empty_array", [item(tags=[])], False)
    add("escaped_quotes", [item(widgetContent='引号 "quote" 和路径 C:\\temp\\file')])
    add("escaped_bracket", [item(widgetContent='他说 "[更正]"，路径 C:\\temp\\file')], False)
    add("multiple_items", [item(), item(itemId="repro-002", widgetTitle="第二条[更正]")], False)
    add("escaped_newline", [item(widgetContent="第一行\n第二行")])
    add("missing_state", [], False, False, "<html>Challenge required</html>")
    add("missing_list", [], False, False, wrap({"notice": "no list"}))
    add("wrong_list_type", [], False, False, wrap({"itemList": None}))
    add("malformed_item", [], False, False, '<script>window.initialState={"itemList":[{"widgetTitle":"bad]"}</script>')
    # Old parser accepts [], then HTTP rejects an empty route; candidate rejects JSON.
    add("malformed_outer", [], False, False, '<script>window.initialState={"itemList":[],"outer":}</script>')
    add("encrypted", [item(widgetTitle="加密状态[更正]", widgetContent="完整保留加密快讯。")], False,
        html=wrap({"state": ENCRYPTED, "isEncrypt": True}))
    add("invalid_encrypted", [], False, False, wrap({"state": "not-valid-aes-data", "isEncrypt": True}))
    for case in result:
        case["errors"] = {"old": "SyntaxError", "candidate": "SyntaxError"}
        if case["name"] in {"missing_state", "missing_list", "wrong_list_type", "encrypted", "invalid_encrypted"}:
            case["errors"]["old"] = "TypeError"
        if case["name"] == "missing_state":
            case["errors"]["candidate"] = "does not contain initial state"
        if case["name"] in {"missing_list", "wrong_list_type"}:
            case["errors"]["candidate"] = "does not contain an article list"
        if case["name"] == "empty_list":
            case["errors"] = {role: "this route is empty" for role in PINS}
        if case["name"] == "malformed_outer":
            case["errors"]["old"] = "this route is empty"
    return result


def validate_response(case, role, record):
    if record.get("error") or record.get("cleanup") != "removed":
        raise ValueError("execution or owned-container cleanup failed")
    if record.get("pin") != PINS[role] or record.get("input_sha256") != digest(case["html"].encode()):
        raise ValueError("image/input identity mismatch")
    logs = record.get("logs", "")
    for marker in ("REPRO_ROUTE_SHA256=" + PINS[role]["route_sha256"],
                   "REPRO_INPUT_SHA256=" + record["input_sha256"]):
        if logs.count(marker) != 1:
            raise ValueError("actual handler/transport proof missing or repeated")
    body = base64.b64decode(record["body"], validate=True)
    if len(body) > MAX_BODY_BYTES or digest(body) != record.get("body_sha256"):
        raise ValueError("response integrity mismatch")
    if record["status"] != case["status"][role]:
        raise ValueError("unexpected HTTP status")
    if record["status"] == 503:
        if case["errors"][role] not in logs or "Error in /36kr/newsflashes:" not in logs:
            raise ValueError("missing expected actual route error")
        return
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise ValueError("DTD/entity not allowed")
    root = ET.fromstring(body)
    if root.tag != "rss" or len(root.findall("channel")) != 1:
        raise ValueError("not one RSS channel")
    channel = root.find("channel")
    if channel.findtext("link") != "https://www.36kr.com/newsflashes":
        raise ValueError("wrong channel identity")
    actual = []
    for node in channel.findall("item"):
        if any(len(node.findall(field)) != 1 for field in ("title", "link", "description")):
            raise ValueError("missing or duplicate item field")
        actual.append({field: node.findtext(field) or "" for field in ("title", "link", "description")})
    if actual != case["items"]:
        raise ValueError("RSS count/order/title/link/description mismatch")


def check_report(report):
    expected = {(c["name"], role): c for c in cases() for role in PINS}
    if report.get("schema") != 1 or report.get("pins") != PINS or report.get("loader_sha256") != digest(LOADER.encode()):
        raise ValueError("report version/identity mismatch")
    rows = report.get("rows", [])
    seen = set()
    for row in rows:
        key = (row.get("case"), row.get("role"))
        if key not in expected or key in seen:
            raise ValueError("unexpected or duplicate result")
        seen.add(key)
        validate_response(expected[key], key[1], row)
    if seen != set(expected):
        raise ValueError("incomplete matrix")
    return len(rows)


def command(args, timeout=15, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {result.stderr[-1500:]}")
    return result


def local_docker():
    # Never honor a remote daemon override or run against a production context.
    if any(os.environ.get(k) for k in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY")):
        raise ValueError("clear Docker environment overrides; use a local Unix-socket context")
    context = command(["docker", "context", "show"]).stdout.strip()
    endpoint = command(["docker", "context", "inspect", context,
                        "--format", "{{.Endpoints.docker.Host}}"]).stdout.strip()
    if not endpoint.startswith("unix:///"):
        raise ValueError("only a local Unix-socket Docker context is allowed")
    return ["docker", "--context", context]


def verify_images(docker):
    for pin in PINS.values():
        data = json.loads(command([*docker, "image", "inspect", "--platform", "linux/amd64",
                                   "--format", "{{json .}}", pin["image"]]).stdout)
        revision = data.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision")
        if (data["Id"] != pin["id"] or revision != pin["revision"]
                or (data.get("Os"), data.get("Architecture")) != ("linux", "amd64")
                or pin["image"] not in data.get("RepoDigests", [])):
            raise ValueError("installed pinned image identity mismatch")


def save_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def container_args(name, run_id, directory, pin):
    return ["create", "--pull=never", "--name", name, "--label", f"{OWNER_LABEL}={run_id}",
            "--platform", "linux/amd64", "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--memory", "1g", "--cpus", "1", "--pids-limit", "128",
            "--tmpfs", "/tmp:rw,nosuid,size=32m", "--tmpfs", "/app/logs:rw,nosuid,size=16m",
            "--mount", f"type=bind,src={directory},dst=/evidence,readonly",
            "-e", "NODE_ENV=production", "-e", "CACHE_TYPE=memory",
            "-e", "REPRO_ROUTE=" + pin["route"], "-e", "REPRO_ROUTE_SHA256=" + pin["route_sha256"],
            "--entrypoint", "node", pin["image"], "--import", "/evidence/transport.mjs", "/app/dist/index.mjs"]


def remove_owned(docker, name, run_id):
    info = command([*docker, "container", "inspect", name], check=False)
    if info.returncode:
        # An inspect failure cannot prove the container absent; surface cleanup uncertainty.
        raise RuntimeError("cannot confirm owned container for cleanup")
    details = json.loads(info.stdout)[0]
    if details.get("Config", {}).get("Labels", {}).get(OWNER_LABEL) != run_id:
        raise RuntimeError("refusing to remove a container not owned by this run")
    try:
        logs = command([*docker, "logs", "--tail", "160", details["Id"]], check=False)
    finally:
        # A log timeout must not leave an otherwise confirmed owned container running.
        command([*docker, "rm", "-f", details["Id"]])
    if logs.returncode:
        raise RuntimeError("could not preserve route logs")
    return logs.stdout + logs.stderr


def run_case(docker, directory, run_id, case, role, deadline):
    name = f"36kr-repro-{run_id}-{role}-{case['name']}"
    row = {"case": case["name"], "role": role, "pin": PINS[role],
           "input_sha256": digest(case["html"].encode()), "started_at": time.time()}
    attempted = False
    try:
        if time.monotonic() >= deadline:
            raise TimeoutError("matrix time budget exhausted")
        attempted = True
        command([*docker, *container_args(name, run_id, directory, PINS[role])])
        command([*docker, "start", name])
        health_deadline = min(deadline, time.monotonic() + 45)
        while time.monotonic() < health_deadline:
            if command([*docker, "exec", name, "node", "-e", HEALTH], timeout=4, check=False).returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise TimeoutError("local container startup timeout")
        payload = json.loads(command([*docker, "exec", name, "node", "--input-type=module", "-e", FETCH]).stdout)
        body = base64.b64decode(payload["body"], validate=True)
        row.update(payload)
        row["body_sha256"] = digest(body)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        row["error"] = f"{type(error).__name__}: {error}"
    finally:
        if attempted:
            try:
                row["logs"] = remove_owned(docker, name, run_id)
                row["cleanup"] = "removed"
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                row["cleanup"] = f"unconfirmed: {error}"
        row["finished_at"] = time.time()
        save_json(directory / f"{role}.json", row)
    return row


def run_matrix(output):
    if not output.is_absolute() or "," in str(output):
        raise ValueError("output must be a new absolute path without commas")
    docker = local_docker()
    verify_images(docker)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    run_id = uuid.uuid4().hex
    report = {"schema": 1, "pins": PINS, "loader_sha256": digest(LOADER.encode()), "run_id": run_id,
              "started_at": time.time(), "scope": "synthetic input; local Docker; no upstream/production", "rows": []}
    deadline = time.monotonic() + 900
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            for case in cases():
                directory = output / case["name"]
                directory.mkdir(mode=0o700)
                (directory / "input.html").write_text(case["html"], encoding="utf-8")
                (directory / "transport.mjs").write_text(LOADER, encoding="utf-8")
                futures = [pool.submit(run_case, docker, directory, run_id, case, role, deadline) for role in PINS]
                rows = [future.result() for future in futures]
                report["rows"].extend(rows)
                # Stop on execution/expectation failure, preserving all completed evidence.
                for row in rows:
                    validate_response(case, row["role"], row)
                print(f"{case['name']}: expected A/B outcomes verified", flush=True)
    finally:
        report["finished_at"] = time.time()
        save_json(output / "report.json", report)
    return check_report(report)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--run", type=Path, help="explicit local Docker integration; new evidence directory")
    modes.add_argument("--check", type=Path, help="offline validation of an existing report.json")
    args = parser.parse_args(argv)
    try:
        count = run_matrix(args.run) if args.run else check_report(
            json.loads((args.check / "report.json").read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, ET.ParseError, subprocess.SubprocessError) as error:
        print(f"FAIL: {type(error).__name__}: {error}")
        return 1
    print(f"PASS: {count} pinned-image/case outcomes; historical production cause remains unproven")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
