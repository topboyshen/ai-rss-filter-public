"""Fixed public-image bytes for an offline private-netns Docker Hub mirror.

Outer HTTPS downloads are one-shot and digest checked. The inner server never
fetches, proxies, resolves, or rewrites a manifest. Docker must pull the original
Docker Hub digest reference and prove its actual RepoDigests independently.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import http.client
import http.server
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import socketserver
import ssl
import stat
import threading
import time
from urllib.parse import urlencode, urlsplit

FIXED_REFERENCES = (
    "python:3.11.17-slim-trixie@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce",
    "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea",
    "redis:7-alpine@sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99",
    "nginx:stable-alpine@sha256:0d3b80406a13a767339fbe2f41406d6c7da727ab89cf8fae399e81f780f814d1",
)
MINIFLUX131_REF = "miniflux/miniflux@sha256:cd353a6af9ce663291f405eddef26fa0d567f486b521c5d34e3fb50e41938b9a"
NAMED_PROFILES = frozenset({"host-loopback131", "actual125-compatibility"})
INDEX_TYPES = {"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"}
MANIFEST_TYPES = {"application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"}
MEDIA_TYPES = INDEX_TYPES | MANIFEST_TYPES
MAX_BLOB, MAX_TOTAL, MAX_OBJECTS, MAX_MANIFEST = 256 * 1024**2, 1024**3, 256, 2 * 1024**2
REGISTRY = "registry-1.docker.io"
CDN_HOSTS = {"production.cloudflare.docker.com", "production.cloudfront.docker.com",
             "docker-images-prod.6aa30f8b08e16409b46e0173d6de2f56.r2.cloudflarestorage.com"}
SHA = re.compile(r"sha256:[0-9a-f]{64}")


class RegistryError(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise RegistryError(message)


def profile_references(profile="default"):
    require(type(profile) is str and profile in {"default", *NAMED_PROFILES}, "unknown frozen registry profile")
    if profile == "host-loopback131":
        return (*FIXED_REFERENCES[:2], MINIFLUX131_REF)
    if profile == "actual125-compatibility":
        return (*FIXED_REFERENCES, MINIFLUX131_REF)
    return FIXED_REFERENCES


def _repository(reference):
    require(reference in (*FIXED_REFERENCES, MINIFLUX131_REF), "unknown frozen registry reference")
    if reference == MINIFLUX131_REF:
        return "miniflux/miniflux"
    return "library/" + reference.split("@", 1)[0].split(":", 1)[0]


def remaining(deadline):
    value = deadline - time.monotonic() if type(deadline) in {int, float} and math.isfinite(deadline) else -1
    require(0 < value <= 600, "original shared registry deadline expired or invalid")
    return value


def _require_root():
    require(os.geteuid() == 0, "root-owned isolated registry required")


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate registry JSON key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: require(False, "nonfinite registry JSON"))


def _encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _save(path, value):
    raw = _encoded(value)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return hashlib.sha256(raw).hexdigest()


def _private_root(root, *, fresh=False):
    _require_root()
    require(isinstance(root, Path) and root.is_absolute() and root == root.resolve(), "direct private registry root required")
    for parent in root.parents:
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid in {0, os.geteuid()}
                and (not info.st_mode & 0o022 or info.st_mode & stat.S_ISVTX), "unsafe registry ancestor")
    if fresh:
        root.mkdir(mode=0o700)
    info = root.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
            "private registry directory ownership/mode differs")


def _identity(info):
    return [info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _file_fd(path, size, digest, deadline):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid() and before.st_nlink == 1
                and stat.S_IMODE(before.st_mode) == 0o600 and before.st_size == size <= MAX_BLOB,
                "sealed registry object identity differs")
        sha, count = hashlib.sha256(), 0
        while True:
            remaining(deadline)
            raw = os.read(fd, 65536)
            if not raw:
                break
            count += len(raw)
            require(count <= size, "registry object grew")
            sha.update(raw)
        require(count == size and sha.hexdigest() == digest.removeprefix("sha256:")
                and _identity(before) == _identity(os.fstat(fd)) == _identity(path.lstat()), "registry object hash/identity differs")
        return fd, _identity(before)
    except BaseException:
        os.close(fd)
        raise


def _url(url, hosts):
    parsed = urlsplit(url)
    require(parsed.scheme == "https" and parsed.hostname in hosts and parsed.port in {None, 443}
            and parsed.username is None and parsed.password is None and not parsed.fragment, "illegal registry HTTPS endpoint")
    return parsed


def _https_connection(host, deadline):
    remaining(deadline)
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    require(addresses and len(addresses) <= 32 and all(_public_ip(row[4][0]) for row in addresses),
            "registry DNS resolved a nonpublic target")
    family, kind, proto, _, address = addresses[0]
    raw = socket.socket(family, kind, proto)
    try:
        raw.settimeout(min(10, remaining(deadline)))
        raw.connect(address)
        context = ssl.create_default_context()
        connection = http.client.HTTPSConnection(host, timeout=min(10, remaining(deadline)), context=context)
        connection.sock = context.wrap_socket(raw, server_hostname=host)
        return connection
    except BaseException:
        raw.close()
        raise


def _public_ip(value):
    address = ipaddress.ip_address(value)
    ordinary_unicast = address.is_global and not (
        address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified
        or address.is_multicast or address.is_reserved or getattr(address, "is_site_local", False))
    return ordinary_unicast and (address.version == 4 or not (
        address.ipv4_mapped or address.sixtofour or address.teredo
        or address in ipaddress.ip_network("64:ff9b::/96") or address in ipaddress.ip_network("64:ff9b:1::/48")))


@contextmanager
def _response(url, headers, deadline, *, token=False):
    allowed = {"auth.docker.io"} if token else {REGISTRY, *CDN_HOSTS}
    for hop in range(3):
        parsed = _url(url, allowed)
        connection = _https_connection(parsed.hostname, deadline)
        try:
            connection.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""), headers=headers)
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                require(not token and hop < 2, "registry redirect bound")
                url = response.getheader("Location")
                require(isinstance(url, str), "registry redirect location absent")
                _url(url, {REGISTRY, *CDN_HOSTS})
                headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
                continue
            require(response.status == 200 and response.getheader("Content-Encoding") in {None, "identity"},
                    "registry HTTP response failed")
            yield response, connection
            return
        finally:
            connection.close()
    raise RegistryError("registry redirect exhausted")


def _read(response, connection, count, deadline):
    remaining(deadline)
    if isinstance(response, http.client.HTTPResponse) and response.isclosed():
        return b""
    sock = connection.sock or response.fp.raw._sock
    sock.settimeout(min(3, remaining(deadline)))
    return response.read(count)


def _token(repository, deadline):
    query = urlencode({"service": "registry.docker.io", "scope": "repository:" + repository + ":pull"})
    with _response("https://auth.docker.io/token?" + query, {"Accept-Encoding": "identity"}, deadline, token=True) as (response, connection):
        raw = _read(response, connection, 65537, deadline)
        require(len(raw) <= 65536, "public registry token response bound")
        value = _json(raw).get("token")
        require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,16384}", value), "public pull token invalid")
        return value  # Never persist the public bearer token or signed redirect URLs.


def _download(root, repository, kind, digest, size, media_type, token, objects, deadline):
    require(SHA.fullmatch(digest) and kind in {"manifests", "blobs"}, "fixed registry object descriptor required")
    limit = MAX_MANIFEST if kind == "manifests" else MAX_BLOB
    require(size is None or type(size) is int and 0 < size <= limit, "registry descriptor size bound")
    if digest in objects:
        require(size is None or objects[digest]["size"] == size, "duplicate registry object size differs")
        return objects[digest]
    require(len(objects) < MAX_OBJECTS, "registry object count bound")
    path = root / "objects" / digest[7:]
    headers = {"Authorization": "Bearer " + token, "Accept": ", ".join(sorted(MEDIA_TYPES)), "Accept-Encoding": "identity"}
    count, sha, content_type, primary = 0, hashlib.sha256(), None, None
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as stream:
        try:
            with _response("https://" + REGISTRY + "/v2/" + repository + "/" + kind + "/" + digest, headers, deadline) as (response, connection):
                declared = response.getheader("Content-Length")
                require(declared is None or declared.isdecimal() and int(declared) <= limit
                        and (size is None or int(declared) == size), "registry HTTP length differs")
                require(response.getheader("Docker-Content-Digest") in {None, digest}, "registry header digest differs")
                content_type = response.getheader("Content-Type", "application/octet-stream").split(";", 1)[0]
                require(kind == "manifests" or content_type == "application/octet-stream", "registry blob media type differs")
                while chunk := _read(response, connection, min(65536, limit + 1 - count), deadline):
                    count += len(chunk)
                    require(count <= limit and (size is None or count <= size)
                            and sum(x["size"] for x in objects.values()) + count <= MAX_TOTAL, "registry download byte bound")
                    stream.write(chunk)
                    sha.update(chunk)
        except BaseException as error:
            primary = error
            raise
        finally:
            _flush_original(stream, primary)
    require(sha.hexdigest() == digest[7:] and count > 0 and (size is None or count == size), "registry downloaded object digest/size differs")
    if kind == "manifests":
        require(content_type in MEDIA_TYPES and (media_type is None or content_type == media_type), "registry manifest media type differs")
    objects[digest] = {"size": count, "media_type": content_type, "kind": kind}
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return objects[digest]


def _flush_original(stream, primary):
    try:
        stream.flush()
        os.fsync(stream.fileno())
    except BaseException as evidence_error:
        if primary:
            primary.add_note("registry partial-original synchronization failure: " + type(evidence_error).__name__)
            raise primary from evidence_error
        raise


def _manifest(root, digest):
    path = root / "objects" / digest[7:]
    require(path.stat().st_size <= MAX_MANIFEST, "registry manifest bound")
    value = _json(path.read_bytes())
    require(isinstance(value, dict) and value.get("schemaVersion") == 2, "registry manifest schema differs")
    return value


def _platform(index):
    values = index.get("manifests")
    require(isinstance(values, list) and 0 < len(values) <= 64, "registry index descriptor bound")
    selected = [row for row in values if row.get("platform", {}).get("os") == "linux"
                and row.get("platform", {}).get("architecture") == "amd64"]
    require(len(selected) == 1, "exact one linux/amd64 manifest required")
    return selected[0]


def _descriptor(row, *, config=False):
    require(isinstance(row, dict) and SHA.fullmatch(str(row.get("digest"))) and type(row.get("size")) is int
            and 0 < row["size"] <= (MAX_MANIFEST if config else MAX_BLOB) and not row.get("urls"), "invalid fixed image descriptor")
    return row["digest"], row["size"], row.get("mediaType")


def _image(root, reference, token, objects, deadline):
    _, digest = reference.split("@")
    repository = _repository(reference)
    _download(root, repository, "manifests", digest, None, None, token, objects, deadline)
    top = _manifest(root, digest)
    manifest_digest = digest
    if "manifests" in top:
        manifest_digest, size, media = _descriptor(_platform(top), config=True)
        _download(root, repository, "manifests", manifest_digest, size, media, token, objects, deadline)
    manifest = _manifest(root, manifest_digest)
    config_digest, size, media = _descriptor(manifest.get("config"), config=True)
    _download(root, repository, "blobs", config_digest, size, media, token, objects, deadline)
    config = _json((root / "objects" / config_digest[7:]).read_bytes())
    require(config.get("os") == "linux" and config.get("architecture") == "amd64", "fixed config platform differs")
    layers = manifest.get("layers")
    require(isinstance(layers, list) and 0 < len(layers) <= 64, "fixed layer count bound")
    for layer in layers:
        digest, size, media = _descriptor(layer)
        _download(root, repository, "blobs", digest, size, media, token, objects, deadline)


def prepare_bundle(root, references, deadline, *, profile="default"):
    remaining(deadline)
    expected = profile_references(profile)
    require(tuple(references) == expected, "only complete frozen public dependency set permitted")
    _private_root(root, fresh=True)
    (root / "objects").mkdir(mode=0o700)
    selected = {"profile": profile} if profile != "default" else {}
    _save(root / "PREPARE_STARTED.json", {"schema": 1, "references": list(references), "deadline": deadline, **selected})
    objects = {}
    try:
        for reference in references:
            repository = _repository(reference)
            _image(root, reference, _token(repository, deadline), objects, deadline)
        value = {"schema": 1, "references": list(references), "objects": objects, **selected,
                 "total_bytes": sum(row["size"] for row in objects.values())}
        sha = _save(root / "BUNDLE.json", value)
        return {"schema": 1, "path": str(root / "BUNDLE.json"), "sha256": sha, **selected}
    except BaseException as error:
        try:
            _save(root / "FAILURE.json", {"type": type(error).__name__, "message_sha256": hashlib.sha256(str(error).encode()).hexdigest()})
        except BaseException as evidence_error:
            error.add_note("registry failure preservation failed: " + type(evidence_error).__name__)
            raise error from evidence_error
        raise


def _json_fd(value):
    require(value["size"] <= MAX_MANIFEST, "registry JSON size bound")
    return _json(os.pread(value["fd"], value["size"] + 1, 0))


def _route(routes, repository, kind, digest, objects, *, size=None, media=None):
    require(digest in objects, "referenced registry object absent")
    row = objects[digest]
    require(row["kind"] == kind and (size is None or row["size"] == size), "registry descriptor object differs")
    if kind == "manifests":
        require(row["media_type"] in MEDIA_TYPES and (media is None or row["media_type"] == media), "sealed manifest media differs")
    routes["/v2/" + repository + "/" + kind + "/" + digest] = {**row, "digest": digest}
    return row


def _image_routes(reference, objects, routes):
    repository = _repository(reference)
    digest = reference.split("@", 1)[1]
    used = {digest}
    top = _json_fd(_route(routes, repository, "manifests", digest, objects))
    require(isinstance(top, dict) and top.get("schemaVersion") == 2, "sealed index schema differs")
    if "manifests" in top:
        digest, size, media = _descriptor(_platform(top), config=True)
        manifest = _json_fd(_route(routes, repository, "manifests", digest, objects, size=size, media=media))
        used.add(digest)
    else:
        manifest = top
    require(isinstance(manifest, dict) and manifest.get("schemaVersion") == 2, "sealed manifest schema differs")
    digest, size, _ = _descriptor(manifest.get("config"), config=True)
    config = _json_fd(_route(routes, repository, "blobs", digest, objects, size=size))
    require(config.get("os") == "linux" and config.get("architecture") == "amd64", "sealed config platform differs")
    used.add(digest)
    layers = manifest.get("layers")
    require(isinstance(layers, list) and 0 < len(layers) <= 64, "sealed layer count bound")
    for layer in layers:
        digest, size, _ = _descriptor(layer)
        _route(routes, repository, "blobs", digest, objects, size=size)
        used.add(digest)
    return used


class ValidatedBundle:
    def __init__(self, root, objects, routes):
        self.root, self.objects, self.routes = root, objects, routes

    def close(self):
        for row in self.objects.values():
            os.close(row["fd"])
        self.objects.clear()


def validate_bundle(root, descriptor, deadline):
    remaining(deadline)
    _private_root(root)
    _private_root(root / "objects")
    require(isinstance(descriptor, dict), "sealed bundle descriptor differs")
    profile = descriptor.get("profile", "default")
    references = profile_references(profile)
    selected = {"profile"} if profile != "default" else set()
    require(set(descriptor) == {"schema", "path", "sha256"} | selected
            and descriptor["schema"] == 1 and descriptor["path"] == str(root / "BUNDLE.json")
            and re.fullmatch(r"[0-9a-f]{64}", str(descriptor["sha256"])), "sealed bundle descriptor differs")
    size = (root / "BUNDLE.json").lstat().st_size
    require(size <= MAX_MANIFEST, "bundle manifest size bound")
    fd, _ = _file_fd(root / "BUNDLE.json", size, "sha256:" + descriptor["sha256"], deadline)
    try:
        value = _json(os.pread(fd, size + 1, 0))
    finally:
        os.close(fd)
    require(isinstance(value, dict) and set(value) == {"schema", "references", "objects", "total_bytes"} | selected
            and value.get("profile", "default") == profile
            and value["schema"] == 1 and value["references"] == list(references)
            and isinstance(value["objects"], dict) and 0 < len(value["objects"]) <= MAX_OBJECTS, "bundle identity differs")
    objects, routes, total = {}, {}, 0
    try:
        for digest, row in value["objects"].items():
            require(SHA.fullmatch(digest) and isinstance(row, dict) and set(row) == {"size", "media_type", "kind"}
                    and type(row["size"]) is int and 0 < row["size"] <= MAX_BLOB
                    and row["kind"] in {"manifests", "blobs"} and isinstance(row["media_type"], str), "sealed object descriptor invalid")
            total += row["size"]
            require(total <= MAX_TOTAL, "sealed bundle total byte bound")
            path = root / "objects" / digest[7:]
            fd, identity = _file_fd(path, row["size"], digest, deadline)
            objects[digest] = {**row, "fd": fd, "identity": identity, "path": path}
        require(type(value["total_bytes"]) is int and value["total_bytes"] == total, "bundle total bytes differ")
        used = set()
        for reference in references:
            used |= _image_routes(reference, objects, routes)
        require(used == objects.keys(), "bundle contains unreferenced image objects")
        return ValidatedBundle(root, objects, routes)
    except BaseException:
        for row in objects.values():
            os.close(row["fd"])
        raise


def _namespace(expected, parent):
    from scripts.production_host_ingress_native import proc_identity
    require(type(expected) is int and isinstance(parent, dict)
            and set(parent) == {"pid", "ppid", "starttime", "netns", "mntns", "cgroup"}, "registry namespace binding absent")
    current = proc_identity(os.getpid())
    require(current["netns"] == expected != parent["netns"] and current["mntns"] != parent["mntns"]
            and current["cgroup"] == parent["cgroup"] and proc_identity(parent["pid"]) == parent,
            "registry is not in the live parent's new private namespace")


def request_range(value, size):
    if value is None:
        return 0, size - 1, 200
    match = re.fullmatch(r"bytes=(0|[1-9][0-9]*)-(0|[1-9][0-9]*)?", value)
    require(match is not None, "unsupported registry HTTP range")
    start, end = int(match[1]), int(match[2]) if match[2] is not None else size - 1
    require(0 <= start <= end < size, "registry HTTP range outside object")
    return start, end, 206


class _Registry(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads, block_on_close, allow_reuse_address, request_queue_size = True, False, False, 16

    def __init__(self, bundle, expected_netns, parent, deadline):
        self.bundle, self.expected_netns, self.parent, self.deadline = bundle, expected_netns, parent, deadline
        self.lock, self.slots = threading.Lock(), threading.BoundedSemaphore(4)
        self.stopping, self.active, self.requests, self.sent, self.failed = threading.Event(), 0, 0, 0, False
        super().__init__(("127.0.0.1", 0), _Handler)
        self.timeout = 0.05

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.failed = True
            self.shutdown_request(request)
            return
        with self.lock:
            self.active += 1
        try:
            super().process_request(request, address)
        except BaseException:
            with self.lock:
                self.active -= 1
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            with self.lock:
                self.active -= 1
            self.slots.release()

    def handle_error(self, request, address):
        self.failed = True  # No traceback, request path, bearer token, or environment is exported.

    def run(self):
        try:
            while not self.stopping.is_set():
                remaining(self.deadline)
                _namespace(self.expected_netns, self.parent)
                self.handle_request()
        except BaseException:
            self.failed = True


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_):
        pass

    def setup(self):
        self.request.settimeout(min(3, remaining(self.server.deadline)))
        super().setup()

    def parse_request(self):
        if not super().parse_request():
            self.server.failed = True
            return False
        if self.command not in {"GET", "HEAD"}:
            self.server.failed = True
            self.send_error(405, "sealed registry is GET/HEAD only")
            return False
        return True

    def _route(self):
        require(self.client_address[0] == "127.0.0.1" and len(self.headers) <= 32
                and sum(len(k) + len(v) for k, v in self.headers.items()) <= 8192, "registry client/header bound")
        hosts = self.headers.get_all("Host", [])
        require(hosts == ["127.0.0.1:" + str(self.server.server_port)] and "Authorization" not in self.headers
                and "Transfer-Encoding" not in self.headers and self.headers.get_all("Content-Length", []) in [[], ["0"]]
                and len(self.headers.get_all("Range", [])) <= 1, "registry HTTP authority/body rejected")
        value = urlsplit(self.path)
        require(not value.scheme and not value.netloc and not value.fragment and value.query in {"", "ns=docker.io"},
                "registry HTTP target rejected")
        if value.path == "/v2/":
            require(not self.headers.get("Range"), "registry API ping cannot be ranged")
            return None
        require(value.path in self.server.bundle.routes, "registry path is not a sealed object")
        row = self.server.bundle.routes[value.path]
        require(_identity(os.fstat(row["fd"])) == row["identity"] == _identity(row["path"].lstat()), "served registry object identity changed")
        return row

    def _serve(self, *, head=False):
        server = self.server
        try:
            remaining(server.deadline)
            _namespace(server.expected_netns, server.parent)
            with server.lock:
                server.requests += 1
                require(server.requests <= 2048, "registry request count bound")
            row = self._route()
            size = row["size"] if row else 2
            start, end, status = request_range(self.headers.get("Range"), size)
            self.send_response(status)
            self.send_header("Docker-Distribution-Api-Version", "registry/2.0")
            self.send_header("Content-Type", row["media_type"] if row else "application/json")
            self.send_header("Content-Length", str(end - start + 1))
            if row:
                self.send_header("Docker-Content-Digest", row["digest"])
                self.send_header("ETag", '"' + row["digest"] + '"')
                self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if not head:
                self._body(row, start, end)
            if row:
                require(_identity(os.fstat(row["fd"])) == row["identity"] == _identity(row["path"].lstat()), "served object changed during response")
        except BaseException:
            server.failed = True
            self.close_connection = True
            if not self.wfile.closed:
                try:
                    self.send_error(404, "sealed registry request rejected")
                except (OSError, ValueError):
                    pass

    def _body(self, row, start, end):
        while start <= end:
            self.request.settimeout(min(3, remaining(self.server.deadline)))
            chunk = os.pread(row["fd"], min(65536, end - start + 1), start) if row else b"{}"
            require(chunk, "served registry object truncated")
            with self.server.lock:
                self.server.sent += len(chunk)
                require(self.server.sent <= 3 * MAX_TOTAL, "registry served byte bound")
            self.wfile.write(chunk)
            start += len(chunk)

    def do_GET(self):
        self._serve()

    def do_HEAD(self):
        self._serve(head=True)

    def do_POST(self):
        self.server.failed = True
        self.send_error(405, "sealed registry is GET/HEAD only")

    do_PUT = do_DELETE = do_PATCH = do_CONNECT = do_OPTIONS = do_POST


@contextmanager
def serve_bundle(root, descriptor, *, expected_netns, parent_identity, deadline):
    remaining(deadline)
    _namespace(expected_netns, parent_identity)
    bundle = validate_bundle(root, descriptor, deadline)
    server, thread, primary = None, None, None
    try:
        server = _Registry(bundle, expected_netns, parent_identity, deadline)
        _namespace(expected_netns, parent_identity)
        server.url = "http://127.0.0.1:" + str(server.server_port)
        server.port = server.server_port
        _save(root / "REGISTRY_OPEN.json", {"schema": 1, "netns": expected_netns, "port": server.port,
              "bundle_sha256": descriptor["sha256"], "deadline": deadline, "outbound_network": False})
        thread = threading.Thread(target=server.run, daemon=True, name="sealed-offline-registry")
        thread.start()
        yield server
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            if server:
                server.stopping.set()
                if thread:
                    thread.join(timeout=max(0, min(0.2, deadline - time.monotonic())))
                while server.active and time.monotonic() < deadline:
                    time.sleep(min(0.01, max(0, deadline - time.monotonic())))
                server.server_close()
                complete = (not thread or not thread.is_alive()) and not server.active and not server.failed
                _save(root / "REGISTRY_CLOSED.json", {"schema": 1, "status": "closed" if complete else "unconfirmed",
                      "requests": server.requests, "bytes_sent": server.sent, "outbound_network": False,
                      "bundle_sha256": descriptor["sha256"]})
                require(complete, "private registry closing/requests unconfirmed")
            bundle.close()
        except BaseException as evidence_error:
            if primary:
                primary.add_note("private registry close/evidence failure: " + type(evidence_error).__name__)
                raise primary from evidence_error
            raise
