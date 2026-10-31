"""Local synthetic bytes/HTTP tests; not actual Docker pull or netns evidence."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import http.client
import io
import json
import os
import socket
from types import SimpleNamespace
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from scripts import production_offline_registry as registry


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


def descriptor(raw, media):
    return {"digest": digest(raw), "size": len(raw), "mediaType": media}


@pytest.fixture
def dataset(monkeypatch):
    refs, data, urls = [], {}, []
    for name in ("python", "postgres", "redis", "nginx"):
        config = encoded({"os": "linux", "architecture": "amd64", "name": name})
        layer = (name + "-fixed-layer").encode()
        manifest = encoded({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": descriptor(config, "application/vnd.oci.image.config.v1+json"),
            "layers": [descriptor(layer, "application/vnd.oci.image.layer.v1.tar+gzip")]})
        platform = descriptor(manifest, "application/vnd.oci.image.manifest.v1+json")
        platform["platform"] = {"os": "linux", "architecture": "amd64"}
        index = encoded({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [platform]})
        refs.append(name + "@" + digest(index))
        for kind, raw, media in [("manifests", index, "application/vnd.oci.image.index.v1+json"),
                                 ("manifests", manifest, "application/vnd.oci.image.manifest.v1+json"),
                                 ("blobs", config, "application/octet-stream"), ("blobs", layer, "application/octet-stream")]:
            data["https://registry-1.docker.io/v2/library/" + name + "/" + kind + "/" + digest(raw)] = (raw, media)
    monkeypatch.setattr(registry, "FIXED_REFERENCES", tuple(refs))
    monkeypatch.setattr(registry, "_require_root", lambda: None)
    @contextmanager
    def response(url, headers, deadline, *, token=False):
        registry.remaining(deadline)
        urls.append(url)
        assert headers.get("Accept-Encoding") == "identity"
        if token:
            assert url.startswith("https://auth.docker.io/token?service=registry.docker.io&scope=repository%3Alibrary%2F")
            raw, media = b'{"token":"public.synthetic.token"}', "application/json"
        else:
            assert headers["Authorization"] == "Bearer public.synthetic.token"
            raw, media = data[url]
        stream = io.BytesIO(raw)
        stream.getheader = lambda key, default=None: {"Content-Length": str(len(raw)), "Content-Type": media}.get(key, default)
        yield stream, SimpleNamespace(sock=SimpleNamespace(settimeout=lambda value: None))
    monkeypatch.setattr(registry, "_response", response)
    return tuple(refs), data, urls


@pytest.fixture
def bundle(tmp_path, dataset):
    root = tmp_path / "bundle"
    end = time.monotonic() + 30
    descriptor = registry.prepare_bundle(root, dataset[0], end)
    return root, descriptor, end


def test_fixed_public_allowlist_and_download_limits_are_exact():
    assert [r.split("@", 1)[0].split(":", 1)[0] for r in registry.FIXED_REFERENCES] == ["python", "postgres", "redis", "nginx"]
    assert len(set(registry.FIXED_REFERENCES)) == 4
    assert all(registry.SHA.fullmatch(r.split("@", 1)[1]) for r in registry.FIXED_REFERENCES)
    assert (registry.MAX_TOTAL, registry.MAX_BLOB, registry.MAX_OBJECTS) == (1024**3, 256 * 1024**2, 256)


def test_named_profiles_are_exact_and_do_not_replace_the_default_four():
    old = "miniflux/miniflux@sha256:cd353a6af9ce663291f405eddef26fa0d567f486b521c5d34e3fb50e41938b9a"
    assert registry.MINIFLUX131_REF == old
    assert registry.profile_references() == registry.FIXED_REFERENCES
    assert registry.profile_references("host-loopback131") == (*registry.FIXED_REFERENCES[:2], old)
    assert registry.profile_references("actual125-compatibility") == (*registry.FIXED_REFERENCES, old)
    assert registry._repository(old) == "miniflux/miniflux"
    assert all(registry._repository(ref).startswith("library/") for ref in registry.FIXED_REFERENCES)


@pytest.fixture
def named_dataset(dataset, monkeypatch):
    refs, data, urls = dataset
    config = encoded({"os": "linux", "architecture": "amd64", "name": "miniflux/miniflux"})
    layer = b"fixed-miniflux131-layer"
    manifest = encoded({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": descriptor(config, "application/vnd.oci.image.config.v1+json"),
        "layers": [descriptor(layer, "application/vnd.oci.image.layer.v1.tar+gzip")]})
    platform = descriptor(manifest, "application/vnd.oci.image.manifest.v1+json")
    platform["platform"] = {"os": "linux", "architecture": "amd64"}
    index = encoded({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [platform]})
    old = "miniflux/miniflux@" + digest(index)
    monkeypatch.setattr(registry, "MINIFLUX131_REF", old)
    for kind, raw, media in [("manifests", index, "application/vnd.oci.image.index.v1+json"),
                             ("manifests", manifest, "application/vnd.oci.image.manifest.v1+json"),
                             ("blobs", config, "application/octet-stream"), ("blobs", layer, "application/octet-stream")]:
        data["https://registry-1.docker.io/v2/miniflux/miniflux/" + kind + "/" + digest(raw)] = (raw, media)

    @contextmanager
    def response(url, headers, deadline, *, token=False):
        registry.remaining(deadline)
        urls.append(url)
        assert headers.get("Accept-Encoding") == "identity"
        if token:
            assert url.startswith("https://auth.docker.io/token?service=registry.docker.io&scope=repository%3A")
            raw, media = b'{"token":"public.synthetic.token"}', "application/json"
        else:
            assert headers["Authorization"] == "Bearer public.synthetic.token"
            raw, media = data[url]
        stream = io.BytesIO(raw)
        stream.getheader = lambda key, default=None: {"Content-Length": str(len(raw)), "Content-Type": media}.get(key, default)
        yield stream, SimpleNamespace(sock=SimpleNamespace(settimeout=lambda value: None))

    monkeypatch.setattr(registry, "_response", response)
    return refs, data, urls


@pytest.mark.parametrize("profile,count", [("host-loopback131", 3), ("actual125-compatibility", 5)])
def test_named_profile_roundtrip_binds_complete_set_and_repository(named_dataset, tmp_path, profile, count):
    _, _, urls = named_dataset
    refs = registry.profile_references(profile)
    root, end = tmp_path / "named", time.monotonic() + 30
    proof = registry.prepare_bundle(root, refs, end, profile=profile)
    value = json.loads((root / "BUNDLE.json").read_bytes())
    assert proof["profile"] == value["profile"] == profile and value["references"] == list(refs)
    assert json.loads((root / "PREPARE_STARTED.json").read_bytes())["deadline"] == end
    assert len(urls) == count * 5 and len(urls) == len(set(urls))
    assert any("scope=repository%3Aminiflux%2Fminiflux%3Apull" in url for url in urls)
    assert not any("library/miniflux" in url for url in urls)
    valid = registry.validate_bundle(root, proof, end)
    try:
        assert len(valid.routes) == count * 4
        assert any(path.startswith("/v2/miniflux/miniflux/") for path in valid.routes)
        if profile == "host-loopback131":
            assert not any("/redis/" in path or "/nginx/" in path for path in valid.routes)
    finally:
        valid.close()


@pytest.mark.parametrize("profile", ["default", "host-loopback131", "actual125-compatibility"])
@pytest.mark.parametrize("fault", ["subset", "reorder", "extra", "different_profile"])
def test_named_profile_never_accepts_generic_or_partial_sets(named_dataset, tmp_path, profile, fault):
    refs, urls = registry.profile_references(profile), named_dataset[2]
    if fault == "subset":
        refs = refs[:-1]
    elif fault == "reorder":
        refs = tuple(reversed(refs))
    elif fault == "extra":
        refs = (*refs, "arbitrary@sha256:" + "a" * 64)
    else:
        refs = registry.profile_references("actual125-compatibility" if profile != "actual125-compatibility" else "default")
    with pytest.raises(registry.RegistryError, match="complete frozen public dependency set"):
        registry.prepare_bundle(tmp_path / "invalid", refs, time.monotonic() + 30, profile=profile)
    assert urls == [] and not (tmp_path / "invalid").exists()


@pytest.mark.parametrize("profile", [None, {}, [], True, "all", "host-loopback131-extra"])
def test_unknown_registry_profile_fails_before_io(named_dataset, tmp_path, profile):
    with pytest.raises(registry.RegistryError, match="unknown frozen registry profile"):
        registry.prepare_bundle(tmp_path / "invalid", named_dataset[0], time.monotonic() + 30, profile=profile)
    assert named_dataset[2] == [] and not (tmp_path / "invalid").exists()


@pytest.mark.parametrize("fault", ["descriptor_profile", "missing_descriptor_profile", "bundle_profile", "default_profile_field"])
def test_profile_is_sealed_in_both_descriptor_and_original(named_dataset, tmp_path, fault):
    root, end = tmp_path / "named", time.monotonic() + 30
    profile = "default" if fault == "default_profile_field" else "host-loopback131"
    proof = registry.prepare_bundle(root, registry.profile_references(profile), end, profile=profile)
    if fault == "descriptor_profile":
        proof["profile"] = "actual125-compatibility"
    elif fault == "missing_descriptor_profile":
        proof.pop("profile")
    elif fault == "default_profile_field":
        proof["profile"] = "default"
    else:
        value = json.loads((root / "BUNDLE.json").read_bytes())
        value["profile"] = "actual125-compatibility"
        (root / "BUNDLE.json").write_bytes(registry._encoded(value))
        proof["sha256"] = hashlib.sha256((root / "BUNDLE.json").read_bytes()).hexdigest()
    with pytest.raises(registry.RegistryError):
        registry.validate_bundle(root, proof, end)


def test_prepare_keeps_all_original_bytes_and_same_deadline(bundle, dataset):
    root, descriptor, end = bundle
    record = json.loads((root / "PREPARE_STARTED.json").read_bytes())
    assert record["deadline"] == end
    assert len(dataset[2]) == 20 and len(set(dataset[2])) == 20
    assert not (root / "FAILURE.json").exists()
    assert descriptor["sha256"] == hashlib.sha256((root / "BUNDLE.json").read_bytes()).hexdigest()
    assert b"public.synthetic.token" not in b"".join(p.read_bytes() for p in root.iterdir() if p.is_file())
    valid = registry.validate_bundle(root, descriptor, end)
    try:
        assert len(valid.objects) == 16 and len(valid.routes) == 16
        for row in valid.objects.values():
            assert row["size"] == os.fstat(row["fd"]).st_size and row["path"].stat().st_mode & 0o777 == 0o600
    finally:
        valid.close()
    assert valid.objects == {}


@pytest.mark.parametrize("fault", ["expired", "nonfinite", "extended", "reference", "subset"])
def test_invalid_authority_or_deadline_does_not_download(tmp_path, dataset, fault):
    refs, _, urls = dataset
    end = time.monotonic() + 30
    if fault == "expired":
        end = time.monotonic() - 1
    elif fault == "nonfinite":
        end = float("nan")
    elif fault == "extended":
        end = time.monotonic() + 601
    elif fault == "reference":
        refs = (*refs[:-1], "unknown@sha256:" + "a" * 64)
    else:
        refs = refs[:-1]
    with pytest.raises(registry.RegistryError):
        registry.prepare_bundle(tmp_path / "invalid", refs, end)
    assert urls == []


@pytest.mark.parametrize("fault", ["wrong_bytes", "short_bytes", "layer_limit", "total_limit", "object_limit"])
def test_download_failure_retains_partial_original_and_stops_without_retry(tmp_path, dataset, monkeypatch, fault):
    refs, data, urls = dataset
    url = next(url for url in data if "/blobs/" in url and data[url][0].startswith(b"python-fixed"))
    if fault == "wrong_bytes":
        data[url] = (b"X" * len(data[url][0]), data[url][1])
    elif fault == "short_bytes":
        data[url] = (data[url][0][:-1], data[url][1])
    elif fault == "layer_limit":
        monkeypatch.setattr(registry, "MAX_BLOB", 5)
    elif fault == "total_limit":
        monkeypatch.setattr(registry, "MAX_TOTAL", 5)
    else:
        monkeypatch.setattr(registry, "MAX_OBJECTS", 1)
    root = tmp_path / "failure"
    with pytest.raises(registry.RegistryError):
        registry.prepare_bundle(root, refs, time.monotonic() + 30)
    assert not (root / "BUNDLE.json").exists() and (root / "FAILURE.json").exists()
    assert len(urls) == len(set(urls))
    failure = json.loads((root / "FAILURE.json").read_bytes())
    assert set(failure) == {"type", "message_sha256"}


@pytest.mark.parametrize("fault", ["changed_same_size", "symlink", "hardlink", "mode", "bundle_hash", "unused_object"])
def test_sealed_bundle_tampering_never_opens_registry(bundle, fault):
    root, desc, end = bundle
    target = next((root / "objects").iterdir())
    if fault == "changed_same_size":
        target.write_bytes(b"X" * target.stat().st_size)
    elif fault == "symlink":
        original = target.with_suffix(".original")
        target.rename(original)
        target.symlink_to(original)
    elif fault == "hardlink":
        os.link(target, root / "duplicate")
    elif fault == "mode":
        target.chmod(0o644)
    elif fault == "bundle_hash":
        desc["sha256"] = "a" * 64
    else:
        raw = b"unused sealed bytes"
        p = root / "objects" / digest(raw)[7:]
        p.write_bytes(raw)
        p.chmod(0o600)
        value = json.loads((root / "BUNDLE.json").read_bytes())
        value["objects"][digest(raw)] = {"size": len(raw), "media_type": "application/octet-stream", "kind": "blobs"}
        value["total_bytes"] += len(raw)
        (root / "BUNDLE.json").write_bytes(registry._encoded(value))
        desc["sha256"] = hashlib.sha256((root / "BUNDLE.json").read_bytes()).hexdigest()
    with pytest.raises((OSError, registry.RegistryError)):
        registry.validate_bundle(root, desc, end)
    assert not (root / "REGISTRY_OPEN.json").exists()


@pytest.mark.parametrize("url", ["http://registry-1.docker.io/v2/", "https://127.0.0.1/v2/",
    "https://registry-1.docker.io.evil.invalid/v2/", "https://user@registry-1.docker.io/v2/",
    "https://registry-1.docker.io:8443/v2/", "https://registry-1.docker.io/v2/#private"])
def test_https_endpoint_restrictions(url):
    with pytest.raises(registry.RegistryError):
        registry._url(url, {registry.REGISTRY, *registry.CDN_HOSTS})


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "::1", "fc00::1",
    "::ffff:8.8.8.8", "64:ff9b::808:808", "2002:0808:0808::1", "224.0.0.1", "ff0e::1", "0.0.0.0", "::",
    "240.0.0.1", "2001:db8::1", "fec0::1"])
def test_dns_rebinding_and_transition_addresses_never_connect(monkeypatch, address):
    monkeypatch.setattr(registry.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])
    monkeypatch.setattr(registry.socket, "socket", lambda *_: pytest.fail("nonpublic DNS cannot connect"))
    with pytest.raises(registry.RegistryError, match="nonpublic"):
        registry._https_connection("registry-1.docker.io", time.monotonic() + 30)


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "2001:4860:4860::8888", "2606:4700:4700::1111"])
def test_ordinary_public_unicast_passes_address_classification_only(address):
    assert registry._public_ip(address) is True  # No DNS lookup, socket, or public connection.


def test_redirect_strips_authorization_and_never_follows_unknown_domain(monkeypatch):
    calls, replies = [], [(307, {"Location": "https://production.cloudflare.docker.com/fixed?signature=private"}), (200, {})]
    class Connection:
        def request(self, method, path, headers):
            calls.append((method, path, headers))
        def getresponse(self):
            status, headers = replies.pop(0)
            return SimpleNamespace(status=status, getheader=lambda key: headers.get(key))
        def close(self):
            pass
    monkeypatch.setattr(registry, "_https_connection", lambda *_: Connection())
    with registry._response("https://registry-1.docker.io/v2/library/nginx/blobs/sha256:" + "a" * 64,
            {"Authorization": "Bearer synthetic-private"}, time.monotonic() + 30):
        pass
    assert len(calls) == 2 and "Authorization" not in calls[1][2]
    replies[:] = [(307, {"Location": "https://127.0.0.1/private"})]
    with pytest.raises(registry.RegistryError):
        with registry._response("https://registry-1.docker.io/v2/", {}, time.monotonic() + 30):
            pytest.fail("illegal redirect cannot yield")


@pytest.mark.parametrize("body,length", [(b"abc", 3), (b"abc", 9)])
def test_real_http_response_connection_close_eof_is_not_a_socket_error(body, length):
    class SocketFile(io.BytesIO):
        raw = property(lambda self: self)
        _sock = SimpleNamespace(settimeout=lambda _: None)
    class FakeSocket:
        def makefile(self, *_):
            return SocketFile(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: " + str(length).encode() + b"\r\n\r\n" + body)
    response = http.client.HTTPResponse(FakeSocket())
    response.begin()
    connection = SimpleNamespace(sock=None)
    assert registry._read(response, connection, 65536, time.monotonic() + 30) == body
    assert registry._read(response, connection, 65536, time.monotonic() + 30) == b""
    assert response.isclosed()


def test_download_streams_bounded_chunks_and_keeps_exact_bytes(tmp_path, dataset, monkeypatch):
    _, data, _ = dataset
    raw = b"synthetic-layer-" * 20000
    object_digest = digest(raw)
    data["https://registry-1.docker.io/v2/library/python/blobs/" + object_digest] = (raw, "application/octet-stream")
    root = tmp_path / "stream"
    (root / "objects").mkdir(parents=True)
    sizes, original_read = [], registry._read
    def read(response, connection, count, deadline):
        sizes.append(count)
        return original_read(response, connection, count, deadline)
    monkeypatch.setattr(registry, "_read", read)
    row = registry._download(root, "library/python", "blobs", object_digest, len(raw), None,
                             "public.synthetic.token", {}, time.monotonic() + 30)
    assert row["size"] == len(raw) and len(sizes) > 4 and max(sizes) == 65536
    assert (root / "objects" / object_digest[7:]).read_bytes() == raw


def test_partial_original_sync_failure_keeps_primary_error(monkeypatch):
    stream = SimpleNamespace(flush=lambda: None, fileno=lambda: 7)
    primary = TimeoutError("synthetic-primary-download-timeout")
    monkeypatch.setattr(registry.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("synthetic-sync-error")))
    with pytest.raises(TimeoutError, match="synthetic-primary") as found:
        registry._flush_original(stream, primary)
    assert isinstance(found.value.__cause__, OSError)
    assert "synchronization failure" in found.value.__notes__[0]


@pytest.mark.parametrize("fault", ["none", "multiple", "other_arch", "foreign_url"])
def test_fixed_platform_and_descriptor_reject_ambiguous_or_foreign_objects(fault):
    row = {"platform": {"os": "linux", "architecture": "amd64"}, "digest": "sha256:" + "a" * 64,
           "size": 10, "mediaType": "application/vnd.oci.image.manifest.v1+json"}
    if fault == "foreign_url":
        row["urls"] = ["https://127.0.0.1/private"]
        with pytest.raises(registry.RegistryError):
            registry._descriptor(row)
        return
    rows = [] if fault == "none" else [row, deepcopy(row)] if fault == "multiple" else [row]
    if fault == "other_arch":
        row["platform"]["architecture"] = "arm64"
    with pytest.raises(registry.RegistryError):
        registry._platform({"manifests": rows})


def test_namespace_check_requires_live_parent_and_new_namespace(monkeypatch):
    from scripts import production_host_ingress_native as host
    parent = {"pid": 1, "ppid": 0, "starttime": 10, "netns": 100, "mntns": 101, "cgroup": "/owned"}
    current = {**parent, "pid": os.getpid(), "netns": 200, "mntns": 201}
    monkeypatch.setattr(host, "proc_identity", lambda pid: deepcopy(parent if pid == 1 else current))
    registry._namespace(200, parent)
    current["netns"] = 100
    with pytest.raises(registry.RegistryError):
        registry._namespace(100, parent)
    current["netns"] = 200
    changed = {**parent, "starttime": 11}
    with pytest.raises(registry.RegistryError):
        registry._namespace(200, changed)


@pytest.mark.parametrize("value", ["bytes=-1", "bytes=0-1,4-5", "bytes=3-1", "bytes=0-99", "bytes=01-", "items=0-1"])
def test_ranges_are_single_bounded_bytes_only(value):
    with pytest.raises(registry.RegistryError):
        registry.request_range(value, 10)


def request(server, path, *, method="GET", headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=2)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_read_only_mirror_serves_exact_original_bytes_head_range_and_concurrent_clients(bundle, monkeypatch):
    root, desc, end = bundle
    monkeypatch.setattr(registry, "_namespace", lambda *_: None)
    monkeypatch.setattr(registry, "_https_connection", lambda *_: pytest.fail("inner registry cannot fetch"))
    with registry.serve_bundle(root, desc, expected_netns=200, parent_identity={}, deadline=end) as server:
        path, row = next((p, r) for p, r in server.bundle.routes.items() if r["kind"] == "blobs")
        expected = row["path"].read_bytes()
        assert request(server, "/v2/")[0] == 200
        status, headers, body = request(server, path + "?ns=docker.io")
        assert status == 200 and body == expected and headers["Docker-Content-Digest"] == digest(expected)
        assert request(server, path, method="HEAD")[2] == b""
        status, headers, body = request(server, path, headers={"Range": "bytes=1-3"})
        assert status == 206 and body == expected[1:4] and headers["Content-Range"] == f"bytes 1-3/{len(expected)}"
        with ThreadPoolExecutor(max_workers=3) as pool:
            assert all(result[2] == expected for result in pool.map(lambda _: request(server, path), range(3)))
    closed = json.loads((root / "REGISTRY_CLOSED.json").read_bytes())
    assert closed["status"] == "closed" and closed["outbound_network"] is False


@pytest.mark.parametrize("target,method,headers", [
    ("/v2/library/nginx/manifests/latest", "GET", {}), ("http://127.0.0.1/private", "GET", {}),
    ("/v2/../private", "GET", {}), ("/v2/?ns=internal", "GET", {}), ("/v2/", "POST", {}),
    ("/v2/", "TRACE", {}), ("/v2/", "GET", {"Authorization": "Bearer private"}),
    ("/v2/", "GET", {"Authorization": ""}), ("/v2/", "GET", {"Transfer-Encoding": ""}),
    ("/v2/", "GET", {"Host": "evil.invalid"})])
def test_mirror_rejects_unknown_targets_without_proxy_or_success(bundle, monkeypatch, target, method, headers):
    root, desc, end = bundle
    monkeypatch.setattr(registry, "_namespace", lambda *_: None)
    monkeypatch.setattr(registry, "_https_connection", lambda *_: pytest.fail("rejected target cannot fetch"))
    with pytest.raises(registry.RegistryError, match="unconfirmed"):
        with registry.serve_bundle(root, desc, expected_netns=200, parent_identity={}, deadline=end) as server:
            assert request(server, target, method=method, headers=headers)[0] in {404, 405}
    assert json.loads((root / "REGISTRY_CLOSED.json").read_bytes())["status"] == "unconfirmed"


def test_close_evidence_failure_retains_primary_exception(bundle, monkeypatch):
    root, desc, end = bundle
    monkeypatch.setattr(registry, "_namespace", lambda *_: None)
    save = registry._save
    def fail_close(path, value):
        if path.name == "REGISTRY_CLOSED.json":
            raise OSError("synthetic-evidence-sync-failure")
        return save(path, value)
    monkeypatch.setattr(registry, "_save", fail_close)
    with pytest.raises(RuntimeError, match="primary-body-error") as found:
        with registry.serve_bundle(root, desc, expected_netns=200, parent_identity={}, deadline=end):
            raise RuntimeError("primary-body-error")
    assert isinstance(found.value.__cause__, OSError)
    assert "close/evidence failure" in found.value.__notes__[0]
