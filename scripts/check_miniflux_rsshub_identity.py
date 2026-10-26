"""Explicit, isolated Miniflux 2.3.2 identity migration check; no model calls.

Normal ``--check`` only reads a saved report. ``--run`` requires a disposable
native-amd64 Docker daemon and three already-pulled, digest-pinned images. It
creates an internal network, synthetic RSS server, ephemeral PostgreSQL and
Miniflux. No host ports, production volumes, credentials or upstream access.
The actual Miniflux parser/storage/API supplies entry hash/id/url; this script
does NOT reimplement Miniflux's hashing or persistence algorithms.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET


VERSION = "2.3.2"
LABEL = "ai-filter.miniflux-identity.run"
SECTION = "https://www.cls.cn/telegraph"
MODES = ("old", "compat", "candidate")
CASES = ("default", "duplicate", "watch")
PIN = re.compile(r"^[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}$")
SERVER = r'''
import hashlib,http.server,json,pathlib
mode="old"
requests=[]
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_POST(self):
  global mode
  new=self.path.removeprefix("/mode/")
  if new not in ("old","compat","candidate"):
   self.send_error(400);return
  mode=new;self.send_response(204);self.end_headers()
 def do_GET(self):
  case=self.path.strip("/")
  if case=="audit":
   body=json.dumps(requests).encode()
   self.send_response(200);self.end_headers();self.wfile.write(body);return
  if case not in ("default","duplicate","watch"):
   self.send_error(404);return
  body=pathlib.Path("/fixtures",case+"-"+mode+".xml").read_bytes()
  requests.append({"case":case,"mode":mode,"sha256":hashlib.sha256(body).hexdigest()})
  self.send_response(200);self.send_header("Content-Type","application/rss+xml")
  self.send_header("Cache-Control","no-store");self.end_headers();self.wfile.write(body)
http.server.ThreadingHTTPServer(("0.0.0.0",8080),Handler).serve_forever()
'''
CLIENT = r'''
import base64,json,sys,urllib.request,urllib.error
method,url,payload,auth=sys.argv[1:]
headers={"Content-Type":"application/json"}
if auth:headers["Authorization"]="Basic "+auth
request=urllib.request.Request(url,data=payload.encode() if payload else None,headers=headers,method=method)
try:
 with urllib.request.urlopen(request,timeout=20) as response:
  body=response.read(2097153);status=response.status
except urllib.error.HTTPError as error:body=error.read(2097153);status=error.code
if len(body)>2097152:raise ValueError("response too large")
print(json.dumps({"status":status,"body":body.decode()}))
'''


def synthetic_rss(case, mode):
    """Small invented articles; schema follows the saved old/candidate RSS."""
    if case not in CASES or mode not in MODES:
        raise ValueError("unknown synthetic case/mode")
    root = ET.Element("rss", version="2.0")
    channel = ET.SubElement(root, "channel")
    for name, value in (("title", "财联社隔离身份测试"), ("link", SECTION),
                        ("description", "合成测试，不是真实新闻")):
        ET.SubElement(channel, name).text = value
    titles = ("合成甲公司发布年度公告", "合成乙公司披露研发进展")
    for index, title in enumerate(titles, 1):
        if case == "duplicate":
            title = titles[0]
        link = f"https://www.cls.cn/detail/900000{index}"
        if case == "watch":
            link = f"https://api3.cls.cn/share/article/900000{index}?os=web&sv=8.7.9&app="
        real_link = case == "watch" or mode == "candidate"
        node = ET.SubElement(channel, "item")
        ET.SubElement(node, "title").text = title
        ET.SubElement(node, "link").text = link if real_link else ""
        ET.SubElement(node, "guid", isPermaLink="false").text = link if real_link else title
        ET.SubElement(node, "description").text = f"合成正文 {index}；模式 {mode}。"
        ET.SubElement(node, "pubDate").text = "Fri, 16 Oct 2026 10:00:00 GMT"
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def identity(entry):
    value = tuple(entry[key] for key in ("id", "hash", "url"))
    if not isinstance(value[0], int) or isinstance(value[0], bool) or value[0] <= 0:
        raise ValueError("invalid actual entry id")
    if not re.fullmatch(r"[0-9a-f]{64}", value[1]):
        raise ValueError("invalid actual entry hash")
    if not isinstance(value[2], str) or not value[2].startswith("https://"):
        raise ValueError("invalid actual entry URL")
    return value


def check_report(report):
    """Fail closed; positive migration and raw-candidate negative control."""
    if report.get("schema") != 1 or report.get("version") != VERSION:
        raise ValueError("wrong report/Miniflux version")
    if report.get("actual_version", {}).get("version") != VERSION:
        raise ValueError("actual Miniflux version not verified")
    if report.get("error") or report.get("cleanup") != {"containers": [], "network_removed": True}:
        raise ValueError("execution or cleanup incomplete")
    if set(report.get("snapshots", {})) != set(CASES):
        raise ValueError("missing actual Miniflux case")
    fixture_hashes = report.get("fixture_sha256", {})
    requests = report.get("fixture_requests", [])
    for case in CASES:
        for mode in MODES:
            expected_hash = fixture_hashes.get(f"{case}-{mode}.xml")
            if not expected_hash or {"case": case, "mode": mode, "sha256": expected_hash} not in requests:
                raise ValueError("actual Miniflux fixture fetch evidence missing")
    for case in CASES:
        stages = report["snapshots"][case]
        if set(stages) != set(MODES):
            raise ValueError("missing migration stage")
        old = {identity(row) for row in stages["old"]}
        compat = {identity(row) for row in stages["compat"]}
        candidate = {identity(row) for row in stages["candidate"]}
        if len(old) != 2 or compat != old:
            raise ValueError("compatible refresh changed identity")
        if case == "watch":
            if candidate != old or any(row[2] == SECTION or "ai_filter_item=" in row[2] for row in old):
                raise ValueError("watch route was altered")
        elif len(candidate - old) != 2 or not old <= candidate:
            raise ValueError("bare candidate identity change not detected")
        elif any(row[2] != SECTION for row in old):
            raise ValueError("legacy section URL fallback not reproduced")
    return {"compatible_cases": 3, "negative_controls": 2, "model_calls": 0}


class Sandbox:
    def __init__(self, output, images, rss_directory=None):
        self.output, self.images = output, images
        self.rss_directory = rss_directory
        self.fixture_hashes = {}
        self.runtime_profiles = {}
        self.log_errors = []
        self.owner = uuid.uuid4().hex
        self.network = "miniflux-identity-" + self.owner[:12]
        self.names = {key: self.network + "-" + key for key in images}
        self.containers = []
        self.network_created = False
        self.password = uuid.uuid4().hex
        self.auth = base64.b64encode(("fixture:" + self.password).encode()).decode()
        self.deadline = time.monotonic() + 300

    def command(self, *args, timeout=20):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("five-minute isolated check budget exhausted")
        result = subprocess.run(["docker", *args], check=True, capture_output=True, text=True,
                                timeout=min(timeout, remaining))
        return (result.stdout + (result.stderr if args and args[0] == "logs" else "")).strip()

    def inspect_images(self):
        if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
            raise ValueError("--run is restricted to disposable GitHub-hosted native runners")
        info = json.loads(self.command("info", "--format", "{{json .}}"))
        if info.get("OSType") != "linux" or info.get("Architecture") not in {"x86_64", "amd64"}:
            raise ValueError("requires a disposable native linux/amd64 daemon")
        result = {}
        for key, pin in self.images.items():
            if not PIN.fullmatch(pin):
                raise ValueError("all client/database/Miniflux images must use complete digests")
            try:
                inspected = self.command("image", "inspect", "--platform", "linux/amd64", pin)
            except subprocess.CalledProcessError as error:
                if "unknown flag: --platform" not in (error.stderr or "").lower():
                    raise
                inspected = self.command("image", "inspect", pin)
            image = json.loads(inspected)[0]
            if image.get("Architecture") != "amd64" or image.get("Os") != "linux":
                raise ValueError("wrong platform")
            if pin not in image.get("RepoDigests", []):
                raise ValueError("image digest resolution mismatch")
            result[key] = {"pin": pin, "id": image["Id"], "architecture": image["Architecture"]}
        return result

    def launch(self, key, memory, pids, extra, command=()):
        name = self.names[key]
        # Record intended ownership before launch, so timeout still permits cleanup.
        self.containers.append(name)
        self.command("run", "-d", "--pull", "never", "--platform", "linux/amd64",
                     "--name", name, "--label", LABEL + "=" + self.owner,
                     "--network", self.network, "--network-alias", key,
                     "--memory", memory, "--pids-limit", str(pids),
                     "--security-opt", "no-new-privileges", *extra, self.images[key], *command)
        inspected = json.loads(self.command("inspect", name))[0]
        host = inspected["HostConfig"]
        if (host["Memory"] != int(memory[:-1]) * 1024 * 1024 or host["PidsLimit"] != pids
                or host["PortBindings"] or inspected["Config"]["Labels"].get(LABEL) != self.owner):
            raise ValueError("actual private sandbox resource profile differs")
        self.runtime_profiles[key] = {"image_id": inspected["Image"], "memory": host["Memory"],
                                      "pids": host["PidsLimit"], "published_ports": host["PortBindings"]}

    def start(self):
        self.command("network", "create", "--internal", "--label", LABEL + "=" + self.owner, self.network)
        self.network_created = True
        network = json.loads(self.command("network", "inspect", self.network))[0]
        if not network["Internal"] or network["Labels"].get(LABEL) != self.owner:
            raise ValueError("isolated network profile mismatch")
        fixture_dir = self.output / "synthetic-rss"
        fixture_dir.mkdir()
        for case in CASES:
            for mode in MODES:
                filename = f"{case}-{mode}.xml"
                body = ((self.rss_directory / filename).read_bytes() if self.rss_directory
                        else synthetic_rss(case, mode))
                if len(body) > 1024 * 1024 or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
                    raise ValueError("fixture size or XML entity boundary")
                root = ET.fromstring(body)
                if root.tag != "rss" or len(root.findall("channel/item")) != 2:
                    raise ValueError("each synthetic actual-handler fixture must contain two RSS items")
                (fixture_dir / filename).write_bytes(body)
                self.fixture_hashes[filename] = hashlib.sha256(body).hexdigest()
        self.launch("python", "128m", 32, ["--read-only", "--cap-drop", "ALL",
                    "-v", f"{fixture_dir.resolve()}:/fixtures:ro", "--entrypoint", "python3",
                    ], ["-c", SERVER])
        self.launch("postgres", "512m", 128, [
            "--tmpfs", "/var/lib/postgresql/data:rw,size=536870912",
            "-e", "POSTGRES_DB=miniflux", "-e", "POSTGRES_USER=fixture",
            "-e", "POSTGRES_PASSWORD=" + self.password,
        ])
        self.wait_postgres()
        self.launch("miniflux", "256m", 64, [
            "--read-only", "--cap-drop", "ALL", "--tmpfs", "/tmp:rw,size=16777216",
            "-e", f"DATABASE_URL=postgres://fixture:{self.password}@postgres/miniflux?sslmode=disable",
            "-e", "RUN_MIGRATIONS=1", "-e", "CREATE_ADMIN=1", "-e", "ADMIN_USERNAME=fixture",
            "-e", "ADMIN_PASSWORD=" + self.password, "-e", "POLLING_FREQUENCY=1440",
            "-e", "BATCH_SIZE=1", "-e", "WORKER_POOL_SIZE=1",
            "-e", "FETCHER_ALLOW_PRIVATE_NETWORKS=1", "-e", "DISABLE_SCHEDULER_SERVICE=1",
            "-e", "HTTP_CLIENT_TIMEOUT=2",
        ])

    def wait_postgres(self):
        for _ in range(45):
            try:
                self.command("exec", self.names["postgres"], "pg_isready", "-U", "fixture", "-d", "miniflux")
                return
            except subprocess.SubprocessError:
                time.sleep(0.5)
        raise TimeoutError("isolated PostgreSQL failed to become ready")

    def request(self, method, path, data=None, service="miniflux"):
        base = "http://miniflux:8080" if service == "miniflux" else "http://python:8080"
        auth = self.auth if service == "miniflux" else ""
        raw = self.command("exec", self.names["python"], "python3", "-c", CLIENT,
                           method, base + path, json.dumps(data) if data is not None else "", auth, timeout=23)
        record = json.loads(raw)
        if not 200 <= record["status"] < 300:
            raise ValueError(f"isolated {service} API {method} {path} status={record['status']}")
        return json.loads(record["body"]) if record["body"] else None

    def wait_ready(self):
        for _ in range(45):
            try:
                return self.request("GET", "/v1/categories")
            except (ValueError, subprocess.SubprocessError):
                time.sleep(0.5)
        raise TimeoutError("isolated Miniflux failed to become ready")

    def entries(self, feed):
        rows = self.request("GET", f"/v1/feeds/{feed}/entries?limit=100&order=id&direction=asc")["entries"]
        return [{key: row[key] for key in ("id", "hash", "url", "title", "content")} for row in rows]

    def snapshots(self, result):
        categories = self.wait_ready()
        category = categories[0]["id"]
        feeds = {}
        result.update({case: {} for case in CASES})
        for case in CASES:
            created = self.request("POST", "/v1/feeds", {"feed_url": "http://python:8080/" + case,
                                                         "category_id": category})
            feeds[case] = created["feed_id"]
            result[case]["old"] = self.entries(feeds[case])
            self.save_snapshot(case, "old", result[case]["old"])
        for mode in ("compat", "candidate"):
            self.request("POST", "/mode/" + mode, service="python")
            for case, feed in feeds.items():
                self.request("PUT", f"/v1/feeds/{feed}/refresh")
                result[case][mode] = self.entries(feed)
                self.save_snapshot(case, mode, result[case][mode])
        return result

    def save_snapshot(self, case, mode, rows):
        path = self.output / f"{case}-{mode}-entries.json"
        if path.exists():
            raise ValueError("refuse to overwrite stage evidence")
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def cleanup(self):
        failures = []
        # Cleanup gets its own bounded budget, even after the workload deadline.
        self.deadline = time.monotonic() + 45
        for name in reversed(self.containers):
            try:
                inspected = json.loads(self.command("inspect", name))[0]
                if inspected["Config"]["Labels"].get(LABEL) != self.owner:
                    raise ValueError("cleanup ownership mismatch")
                try:
                    logs = self.command("logs", "--tail", "100", name)
                    (self.output / (name + ".log")).write_text(logs.replace(self.password, "[test-secret]"),
                                                               encoding="utf-8")
                except (OSError, subprocess.SubprocessError) as error:
                    self.log_errors.append({"name": name, "error": type(error).__name__})
                self.command("rm", "-f", "-v", name)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                failures.append({"name": name, "error": type(error).__name__})
        removed = not self.network_created
        if self.network_created:
            try:
                inspected = json.loads(self.command("network", "inspect", self.network))[0]
                if inspected["Labels"].get(LABEL) != self.owner:
                    raise ValueError("network cleanup ownership mismatch")
                self.command("network", "rm", self.network)
                removed = True
            except (OSError, ValueError, subprocess.SubprocessError):
                removed = False
        return {"containers": failures, "network_removed": removed}


def run(output, images, rss_directory=None):
    if output.exists():
        raise ValueError("evidence output must be a new directory")
    output.mkdir(parents=True, mode=0o700)
    sandbox = Sandbox(output, images, rss_directory)
    report = {"schema": 1, "version": VERSION, "snapshots": {}, "images": {}}
    try:
        report["images"] = sandbox.inspect_images()
        sandbox.start()
        report["fixture_sha256"] = sandbox.fixture_hashes
        report["fixture_source"] = "provided actual-handler RSS" if rss_directory else "generated contract fixtures"
        sandbox.wait_ready()
        report["actual_version"] = sandbox.request("GET", "/v1/version")
        if report["actual_version"].get("version") != VERSION:
            raise ValueError("actual running Miniflux version mismatch")
        sandbox.snapshots(report["snapshots"])
        report["fixture_requests"] = sandbox.request("GET", "/audit", service="python")
    except Exception as error:
        report["error"] = type(error).__name__ + ": " + str(error).replace(sandbox.password, "[test-secret]")
    finally:
        report["cleanup"] = sandbox.cleanup()
        report["runtime_profiles"] = sandbox.runtime_profiles
        report["fixture_sha256"] = sandbox.fixture_hashes
        report["log_errors"] = sandbox.log_errors
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return check_report(report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", type=Path)
    group.add_argument("--run", type=Path)
    parser.add_argument("--rss-directory", type=Path,
                        help="Nine actual-handler synthetic fixtures named CASE-MODE.xml; no production news")
    for key in ("miniflux", "postgres", "python"):
        parser.add_argument("--" + key + "-image")
    args = parser.parse_args()
    if args.check:
        result = check_report(json.loads(args.check.read_text()))
    else:
        images = {key: getattr(args, key + "_image") or "" for key in ("miniflux", "postgres", "python")}
        result = run(args.run, images, args.rss_directory)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
