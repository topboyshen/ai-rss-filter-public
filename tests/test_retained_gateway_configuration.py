"""Synthetic fixed archives only; no Docker, sockets, production or models."""
from copy import deepcopy
import base64
import io
import json
from pathlib import Path
import socket
import stat
import struct
import tarfile
import time
from types import SimpleNamespace

import pytest

from scripts import private_release_contract as private, retained_gateway_configuration as subject
from services import admission_fence as gate


MAIN = b"events {}\nhttp { include /etc/nginx/mime.types; include /etc/nginx/conf.d/*.conf; }\n"
MIME = b"types { text/plain txt; }\n"
DEFAULT = b"server { listen 5000; location / { proxy_pass http://ai-filter-api:5000; } }\n"


def tar(entries, *, format=tarfile.USTAR_FORMAT):
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode="w", format=format) as archive:
        for name, raw, kind in entries:
            info = tarfile.TarInfo(name)
            info.type, info.mode = kind, 0o755 if kind == tarfile.DIRTYPE else 0o644
            info.uid = info.gid = 0
            info.mtime = 123
            info.size = len(raw) if kind == tarfile.REGTYPE else 0
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info.linkname = "/not-to-be-read"
            archive.addfile(info, io.BytesIO(raw) if info.size else None)
    return target.getvalue()


def archive_map(main=MAIN, mime=MIME, default=DEFAULT):
    return {subject.FILE_PATHS[0]: tar([("nginx.conf", main, tarfile.REGTYPE)]),
            subject.FILE_PATHS[1]: tar([("mime.types", mime, tarfile.REGTYPE)]),
            subject.FILE_PATHS[2]: tar([("default.conf", default, tarfile.REGTYPE)]),
            subject.DIRECTORY_PATH: tar([("conf.d", b"", tarfile.DIRTYPE),
                                         ("conf.d/default.conf", default, tarfile.REGTYPE)])}


def row():
    return {"Id": "a" * 64, "Image": subject.IMAGE_ID, "Name": "/dedicated-web",
            "RestartCount": 0, "Config": {"Labels": {"com.docker.compose.project": "app"}},
            "HostConfig": {"Memory": 128 * 1024**2}, "Mounts": [],
            "State": {"Running": True, "StartedAt": "2026-10-22T01:00:13.214067128Z"},
            "NetworkSettings": {"Networks": {"retained-net": {"NetworkID": "b" * 64}}}}


@pytest.fixture
def proof_case(tmp_path):
    data = archive_map()
    files = {}
    for index, (path, raw) in enumerate(data.items()):
        target = tmp_path / f"archive-{index}.tar"
        target.write_bytes(raw)
        target.chmod(0o600)
        files[path] = target
    actual = row()
    proof = subject.describe(actual, "b" * 64, files)
    return proof, actual, data


def test_static_and_actual_full_closure(proof_case):
    proof, actual, data = proof_case
    calls = []

    def inspect(identifier):
        calls.append(("inspect", identifier))
        return deepcopy(actual)

    def archive(identifier, path, consumer):
        calls.append(("archive", identifier, path))
        return consumer(io.BytesIO(data[path]).read)

    result = subject.verify(proof, actual, inspect=inspect, archive=archive)
    assert result["files"] == dict(zip(subject.FILE_PATHS, (MAIN, MIME, DEFAULT), strict=True))
    assert [x[2] for x in calls if x[0] == "archive"] == list(subject.ARCHIVE_PATHS)
    assert calls[0][0] == calls[-1][0] == "inspect"
    assert subject.validate(proof) == result
    actual["State"].update(Running=False, Status="exited", FinishedAt="natural-stop", ExitCode=143)
    assert subject.verify(proof, actual, inspect=inspect, archive=archive) == result


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE,
                                  tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME])
def test_extension_or_special_header_rejected_without_payload_read(kind):
    header = bytearray(tar([("default.conf", b"secret", tarfile.REGTYPE)])[:512])
    header[156:157] = kind
    # Header type is rejected before even checking checksum/size.
    calls = []

    def read(size):
        calls.append(size)
        assert calls == [512], "must not read rejected member payload"
        return bytes(header)

    with pytest.raises(gate.AdmissionError, match="links or extensions"):
        subject.parse_archive(read, subject.FILE_PATHS[2])
    assert calls == [512]


@pytest.mark.parametrize("name", ["conf.d/unlisted.conf", "conf.d/../default.conf", "/etc/passwd",
                                  "conf.d/sub/default.conf", "conf.d/default.conf/", "unlisted"])
def test_unknown_directory_member_payload_is_never_read(name):
    raw = tar([("conf.d", b"", tarfile.DIRTYPE), (name, b"must not read", tarfile.REGTYPE)])
    source, reads = io.BytesIO(raw), []

    def read(size):
        reads.append(size)
        assert source.tell() < 1024, "must stop immediately at unknown header"
        return source.read(size)

    with pytest.raises((gate.AdmissionError, UnicodeError)):
        subject.parse_archive(read, subject.DIRECTORY_PATH)
    assert reads == [512, 512] and source.tell() == 1024


@pytest.mark.parametrize("entries", [[], [("conf.d", b"", tarfile.DIRTYPE)],
                                   [("conf.d/default.conf", DEFAULT, tarfile.REGTYPE)],
                                   [("conf.d", b"", tarfile.DIRTYPE), ("conf.d", b"", tarfile.DIRTYPE)]])
def test_directory_is_complete_exact_member_set(entries):
    with pytest.raises(gate.AdmissionError):
        subject.parse_archive(io.BytesIO(tar(entries)).read, subject.DIRECTORY_PATH)


@pytest.mark.parametrize("change", ["checksum", "truncated", "nonzero_padding", "trailing", "overread"])
def test_tar_framing_rejected(change):
    raw = archive_map()[subject.FILE_PATHS[0]]
    if change == "checksum":
        raw = b"x" + raw[1:]
    elif change == "truncated":
        raw = raw[:600]
    elif change == "nonzero_padding":
        index = 512 + len(MAIN)
        raw = raw[:index] + b"x" + raw[index + 1:]
    elif change == "trailing":
        raw += b"x" * 512
    read = io.BytesIO(raw).read
    if change == "overread":
        def read(size):
            return b"x" * (size + 1)
    with pytest.raises(gate.AdmissionError):
        subject.parse_archive(read, subject.FILE_PATHS[0])


@pytest.mark.parametrize("main,mime,default", [
    (MAIN.replace(b"/*.conf", b"/default.conf"), MIME, DEFAULT),
    (MAIN.replace(b"/etc/nginx/mime.types", b"$config"), MIME, DEFAULT),
    (MAIN.replace(b"include /etc/nginx/conf.d/*.conf;", b"include /other/*.conf;"), MIME, DEFAULT),
    (MAIN, MIME + b"include /other;", DEFAULT),
    (MAIN, MIME, DEFAULT + b"include /other;"),
    (MAIN + b"include /other;", MIME, DEFAULT),
    (MAIN + b"include \"unterminated", MIME, DEFAULT),
])
def test_include_closure_rejects_unknowns(main, mime, default):
    parsed = {path: subject.parse_archive(io.BytesIO(raw).read, path)
              for path, raw in archive_map(main, mime, default).items()}
    with pytest.raises(gate.AdmissionError):
        subject._closure(parsed)


def test_comments_and_literals_do_not_invent_include():
    main = b"# include /forbidden;\n" + MAIN
    default = b'server { set $literal "include /not-a-directive;"; # include /no;\n }\n'
    parsed = {path: subject.parse_archive(io.BytesIO(raw).read, path)
              for path, raw in archive_map(main, MIME, default).items()}
    assert subject._closure(parsed)[0][subject.FILE_PATHS[2]] == default


def test_directory_payload_must_match_singleton():
    data = archive_map()
    data[subject.DIRECTORY_PATH] = archive_map(default=DEFAULT + b"# changed\n")[subject.DIRECTORY_PATH]
    parsed = {path: subject.parse_archive(io.BytesIO(raw).read, path) for path, raw in data.items()}
    with pytest.raises(gate.AdmissionError, match="singleton and directory"):
        subject._closure(parsed)


@pytest.mark.parametrize("change", ["mount", "image", "restart", "config", "network", "oom", "archive"])
def test_actual_identity_and_archive_changes_block(proof_case, change):
    proof, actual, data = proof_case
    calls = []
    if change == "mount":
        actual["Mounts"] = [{"Type": "bind"}]
    elif change == "image":
        actual["Image"] = "sha256:" + "c" * 64
    elif change == "restart":
        actual["RestartCount"] = 1
    elif change == "config":
        actual["Config"]["changed"] = True
    elif change == "network":
        actual["NetworkSettings"]["Networks"]["retained-net"]["NetworkID"] = "c" * 64
    elif change == "oom":
        actual["State"]["OOMKilled"] = True
    else:
        data[subject.FILE_PATHS[0]] = archive_map(main=MAIN + b"# changed\n")[subject.FILE_PATHS[0]]

    def archive(identifier, path, consumer):
        calls.append(path)
        return consumer(io.BytesIO(data[path]).read)

    with pytest.raises(gate.AdmissionError):
        subject.verify(proof, actual, inspect=lambda _id: deepcopy(actual), archive=archive)
    assert bool(calls) == (change == "archive")


def test_post_archive_identity_change_is_not_hidden(proof_case):
    proof, actual, data = proof_case
    inspections = []

    def inspect(_identifier):
        inspections.append(1)
        changed = deepcopy(actual)
        if len(inspections) == 2:
            changed["State"]["StartedAt"] = "restarted"
        return changed

    with pytest.raises(gate.AdmissionError, match="identity changed"):
        subject.verify(proof, actual, inspect=inspect,
                       archive=lambda _id, path, consumer: consumer(io.BytesIO(data[path]).read))


@pytest.mark.parametrize("change", ["bytes", "inode", "mode", "symlink", "hardlink", "proof", "source"])
def test_private_originals_are_pinned(proof_case, change, tmp_path):
    proof, _actual, _data = proof_case
    path = Path(proof["archives"][subject.FILE_PATHS[0]]["path"])
    if change == "bytes":
        path.write_bytes(path.read_bytes().replace(b"events", b"EVENTS"))
    elif change == "inode":
        raw = path.read_bytes()
        path.rename(path.with_suffix(".retained"))
        path.write_bytes(raw)
        path.chmod(0o600)
    elif change == "mode":
        path.chmod(0o644)
    elif change == "symlink":
        path.rename(path.with_suffix(".retained"))
        path.symlink_to(path.with_suffix(".retained"))
    elif change == "hardlink":
        (tmp_path / "hardlink").hardlink_to(path)
    elif change == "proof":
        proof["closure_sha256"] = "0" * 64
    else:
        proof["source_sha256"] = "0" * 64
    with pytest.raises((gate.AdmissionError, private.ContractError, OSError)):
        subject.validate(proof)


class ResponseSocket:
    def __init__(self, raw, *, credentials=None, flags=0, fragment=None):
        self.raw, self.offset, self.requests = raw, 0, []
        self.credentials, self.flags, self.fragment = credentials, flags, fragment

    def settimeout(self, _seconds):
        pass

    def recvmsg(self, maximum, _ancillary):
        self.requests.append(maximum)
        size = min(maximum, self.fragment) if self.fragment else maximum
        raw = self.raw[self.offset:self.offset + size]
        self.offset += len(raw)
        credentials = self.credentials if self.credentials is not None else [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS,
                                                                              struct.pack("3i", 123, 0, 0))]
        return raw, credentials if raw else [], self.flags, None


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setattr(socket, "SCM_CREDENTIALS", 2, raising=False)


def http(body, *, length=True, extra=b"", status=b"HTTP/1.0 200 OK", path=subject.FILE_PATHS[0], path_stat=None):
    header = status + b"\r\nContent-Type: application/x-tar\r\n"
    directory = path == subject.DIRECTORY_PATH
    if path_stat is None:
        path_stat = {"name": Path(path).name, "size": 4096 if directory else int(body[124:136].strip(b"\0 "), 8),
                     "mode": (1 << 31) | 0o755 if directory else 0o644,
                     "mtime": "1970-01-01T00:02:03Z", "linkTarget": ""}
    header += b"X-Docker-Container-Path-Stat: " + base64.b64encode(json.dumps(path_stat).encode()) + b"\r\n"
    if length:
        header += b"Content-Length: " + str(len(body)).encode() + b"\r\n"
    return header + extra + b"\r\n" + body


@pytest.mark.parametrize("length,fragment", [(True, None), (False, 17)])
def test_actual_writer_response_is_unbuffered_and_exact(credentials, length, fragment):
    body = archive_map()[subject.FILE_PATHS[0]]
    connection = ResponseSocket(http(body, length=length), fragment=fragment)
    response = subject._ArchiveResponse(connection, (123, 0, 0), time.monotonic() + 3, subject.FILE_PATHS[0])
    response.header()
    assert connection.offset == len(connection.raw) - len(body)
    parsed = subject.parse_archive(response.read, subject.FILE_PATHS[0])
    response.finish()
    assert parsed["contents"] == {"nginx.conf": MAIN} and connection.offset == len(connection.raw)
    assert connection.requests[:connection.offset - len(body)] == [1] * (connection.offset - len(body))


def test_http_stream_rejects_unknown_header_before_reading_payload(credentials):
    body = tar([("conf.d", b"", tarfile.DIRTYPE), ("conf.d/secret.conf", b"NEVER READ", tarfile.REGTYPE)])
    raw = http(body, path=subject.DIRECTORY_PATH)
    connection = ResponseSocket(raw)
    response = subject._ArchiveResponse(connection, (123, 0, 0), time.monotonic() + 3, subject.DIRECTORY_PATH)
    response.header()
    header_size = connection.offset
    with pytest.raises(gate.AdmissionError, match="unknown or duplicate"):
        subject.parse_archive(response.read, subject.DIRECTORY_PATH)
    assert connection.offset == header_size + 1024
    assert connection.raw[connection.offset:].startswith(b"NEVER READ")


@pytest.mark.parametrize("change", ["writer", "missing", "flags", "chunked", "duplicate", "bad_type", "503", "extra"])
def test_http_identity_framing_and_limits_remain_strict(credentials, change):
    body = archive_map()[subject.FILE_PATHS[0]]
    kwargs = {}
    extra = b""
    if change == "writer":
        kwargs["credentials"] = [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, struct.pack("3i", 999, 0, 0))]
    elif change == "missing":
        kwargs["credentials"] = []
    elif change == "flags":
        kwargs["flags"] = 8
    elif change == "chunked":
        extra = b"Transfer-Encoding: chunked\r\n"
    elif change == "duplicate":
        extra = b"Content-Length: 1\r\n"
    elif change == "bad_type":
        extra = b"Content-Type: text/plain\r\n"
    raw = http(body, extra=extra, status=b"HTTP/1.0 503 Error" if change == "503" else b"HTTP/1.0 200 OK")
    if change == "extra":
        raw += b"x"
    response = subject._ArchiveResponse(ResponseSocket(raw, **kwargs), (123, 0, 0), time.monotonic() + 3, subject.FILE_PATHS[0])
    with pytest.raises(gate.AdmissionError):
        response.header()
        subject.parse_archive(response.read, subject.FILE_PATHS[0])
        response.finish()


def unix_rows(*rows):
    return ("Num RefCount Protocol Flags Type St Inode Path\n" + "\n".join(rows) + "\n").encode()


LISTEN = "00001: 00000002 00000000 00010000 0001 01 123 /run/docker.sock"
ACCEPTED = "00002: 00000002 00000000 00000000 0001 03 124 /run/docker.sock"


def test_only_native_proven_connected_tuple_is_not_second_listener():
    assert subject._listener(unix_rows(LISTEN, ACCEPTED)) == subject._listener(unix_rows(LISTEN))


@pytest.mark.parametrize("row", [ACCEPTED.replace("0001 03", "0001 02"),
                                ACCEPTED.replace("0001 03", "0002 03"),
                                ACCEPTED.replace("00000000 00000000", "00000000 00010000"),
                                LISTEN.replace(" 123 ", " 125 "), ACCEPTED.replace(" 124 ", " 123 "),
                                ACCEPTED.replace("/run/docker.sock", "/var/run/docker.sock")])
def test_unknown_multiple_and_alias_socket_rows_block(row):
    with pytest.raises((gate.AdmissionError, gate.AdmissionUncertain)):
        subject._listener(unix_rows(LISTEN, row))


def test_typed_listener_must_agree_across_both_complete_reads(monkeypatch):
    metadata = tuple(range(7))
    values = iter([(unix_rows(LISTEN, ACCEPTED), metadata), (unix_rows(LISTEN), metadata)])
    monkeypatch.setattr(subject, "_unix_table", lambda: next(values))
    assert subject._current_listener()["inode"] == 123
    values = iter([(unix_rows(LISTEN), metadata), (unix_rows(LISTEN.replace(" 123 ", " 125 ")), metadata)])
    with pytest.raises(gate.AdmissionError, match="between complete reads"):
        subject._current_listener()


@pytest.mark.parametrize("identifier,path,seconds", [("wrong", subject.FILE_PATHS[0], 1),
                                                   ("a" * 64, "/etc/passwd", 1),
                                                   ("a" * 64, subject.FILE_PATHS[0], 4),
                                                   ("a" * 64, subject.FILE_PATHS[0], -1)])
def test_native_api_rejects_unapproved_request_before_socket(identifier, path, seconds, monkeypatch):
    monkeypatch.setattr(subject.socket, "socket", lambda *_args: pytest.fail("must not connect"))
    with pytest.raises(gate.AdmissionError):
        subject.read_native_archive(identifier, path, lambda _read: None, deadline=time.monotonic() + seconds)


@pytest.mark.parametrize("field,value", [("name", "different"), ("mode", (1 << 27) | 0o777),
                                        ("linkTarget", "/elsewhere"), ("size", 0), ("mode", True),
                                        ("mtime", "1970-01-01T00:02:03")])
def test_actual_docker_path_stat_is_mandatory_and_ordinary(credentials, field, value):
    body = archive_map()[subject.FILE_PATHS[0]]
    path_stat = {"name": "nginx.conf", "size": len(MAIN), "mode": 0o644,
                 "mtime": "1970-01-01T00:02:03Z", "linkTarget": ""}
    path_stat[field] = value
    connection = ResponseSocket(http(body, path_stat=path_stat))
    response = subject._ArchiveResponse(connection, (123, 0, 0), time.monotonic() + 3, subject.FILE_PATHS[0])
    with pytest.raises((gate.AdmissionError, ValueError)):
        response.header()
    assert connection.offset == len(connection.raw) - len(body)


def test_missing_actual_path_stat_blocks_before_any_tar_payload(credentials):
    body = archive_map()[subject.FILE_PATHS[0]]
    prefix = b"HTTP/1.0 200 OK\r\nContent-Type: application/x-tar\r\n\r\n"
    connection = ResponseSocket(prefix + body)
    response = subject._ArchiveResponse(connection, (123, 0, 0), time.monotonic() + 3, subject.FILE_PATHS[0])
    with pytest.raises(gate.AdmissionError, match="stat missing"):
        response.header()
    assert connection.offset == len(prefix)


def test_native_reader_uses_only_fixed_get_and_current_authority(credentials, monkeypatch):
    body = archive_map()[subject.FILE_PATHS[0]]
    events = []

    class Authority:
        pid = 123

        def __init__(self, deadline):
            assert time.monotonic() < deadline <= time.monotonic() + 3

        def __enter__(self):
            events.append("authority-enter")
            return self

        def __exit__(self, *_args):
            events.append("authority-exit")

        def peer(self, value):
            assert value == (1, 0, 0)
            events.append("peer")

        def confirm(self):
            events.append("confirm")

    class Connection(ResponseSocket):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            events.append("close")

        def setsockopt(self, *_args):
            pass

        def connect(self, path):
            assert path == subject.SOCKET_PATH

        def getsockopt(self, *_args):
            return struct.pack("3i", 1, 0, 0)

        def sendall(self, raw):
            events.append(raw)

    connection = Connection(http(body))
    monkeypatch.setattr(socket, "SO_PASSCRED", 16, raising=False)
    monkeypatch.setattr(socket, "SO_PEERCRED", 17, raising=False)
    monkeypatch.setattr(subject, "_ArchiveAuthority", Authority)
    monkeypatch.setattr(socket, "socket", lambda *_args: connection)
    result = subject.read_native_archive("a" * 64, subject.FILE_PATHS[0],
                                        lambda read: subject.parse_archive(read, subject.FILE_PATHS[0]),
                                        deadline=time.monotonic() + 3)
    expected = ("GET /containers/" + "a" * 64 + "/archive?path=%2Fetc%2Fnginx%2Fnginx.conf HTTP/1.0\r\n"
                "Host: localhost\r\nConnection: close\r\n\r\n").encode()
    assert events == ["authority-enter", "peer", expected, "confirm", "close", "authority-exit"]
    assert result["contents"] == {"nginx.conf": MAIN}


def test_response_eof_is_short_read_not_retry_loop(credentials):
    connection = ResponseSocket(b"short")
    response = subject._ArchiveResponse(connection, (123, 0, 0), time.monotonic() + 3, subject.FILE_PATHS[0])
    # Explicitly exercise body EOF without building a fake valid tar header.
    response.remaining = None
    assert response.read(512) == b"short"
    before = len(connection.requests)
    assert response.read(512) == b"" and len(connection.requests) == before


def test_proc_time_is_not_fd_directory_or_table_identity():
    class Info:
        st_dev, st_ino, st_mode, st_uid, st_gid, st_nlink, st_size = 1, 2, 0o40500, 0, 0, 1, 0
        st_mtime_ns, st_ctime_ns = 123, 456

    first, later = Info(), Info()
    later.st_mtime_ns, later.st_ctime_ns = 999, 1000
    assert subject._proc_identity(first) == subject._proc_identity(later)
    assert subject._stat_identity(first) != subject._stat_identity(later)  # Ordinary file guard is unchanged.


def process_metadata(monkeypatch, *, systemd=False, fixed_change=None):
    pid = 1 if systemd else 123
    executable = "/usr/lib/systemd/systemd" if systemd else "/usr/bin/dockerd"
    parts = [b"S"] * 20
    parts[19] = b"5678"
    inputs = {f"/proc/{pid}/stat": str(pid).encode() + b" (daemon) " + b" ".join(parts),
              f"/proc/{pid}/status": (f"Uid:\t0 0 0 0\nGid:\t0 0 0 0\nTracerPid:\t0\n"
                                      f"Pid:\t{pid}\nTgid:\t{pid}\nPPid:\t1\n").encode()}
    actual = SimpleNamespace(st_dev=1, st_ino=2, st_mode=stat.S_IFREG | 0o755, st_uid=0, st_gid=0,
                             st_nlink=1, st_size=100, st_mtime_ns=123, st_ctime_ns=456)
    fixed = deepcopy(actual)
    if fixed_change:
        setattr(fixed, fixed_change[0], fixed_change[1])
    monkeypatch.setattr(subject, "_small", lambda path, _maximum: (inputs[path], ()))
    monkeypatch.setattr(subject, "_link_identity", lambda path: ("synthetic_link", path))
    def readlink(path):
        return executable if path.endswith("/exe") else "net:[789]"
    def readstat(path, **_kwargs):
        if path == f"/proc/{pid}/exe":
            return actual
        if path == executable:
            return fixed
        assert path == f"/proc/{pid}/ns/net"
        return SimpleNamespace(st_dev=3, st_ino=789)
    monkeypatch.setattr(subject.os, "readlink", readlink)
    monkeypatch.setattr(subject.os, "stat", readstat)
    return pid, actual


@pytest.mark.parametrize("systemd", [False, True])
def test_authority_process_pins_actual_executable_file_and_netns(monkeypatch, systemd):
    pid, actual = process_metadata(monkeypatch, systemd=systemd)
    value = subject._process(pid, systemd=systemd)
    assert value["exe_identity"] == subject._stat_identity(actual)
    assert value["namespace_identity"] == (3, 789) and value["start_ticks"] == 5678


@pytest.mark.parametrize("change", [("st_ino", 999), ("st_dev", 99), ("st_uid", 1000),
                                    ("st_mode", stat.S_IFLNK | 0o777), ("st_mode", stat.S_IFREG | 0o777),
                                    ("st_ctime_ns", 999), ("st_size", 101)])
def test_textual_executable_path_cannot_replace_real_file_identity(monkeypatch, change):
    pid, _actual = process_metadata(monkeypatch, fixed_change=change)
    with pytest.raises(gate.AdmissionError, match="executable file differs"):
        subject._process(pid)


@pytest.mark.parametrize("pid", [1, 123])
def test_inherited_listener_fd_does_not_authorize_different_process_netns(monkeypatch, pid):
    authority = subject._ArchiveAuthority(time.monotonic() + 3)
    authority.namespace = (3, 789)
    monkeypatch.setattr(subject.os, "pidfd_open", lambda *_args: 99, raising=False)
    monkeypatch.setattr(subject, "_alive", lambda _fd: None)
    monkeypatch.setattr(subject, "_process", lambda *_args, **_kw: {"namespace_identity": (3, 999)})
    monkeypatch.setattr(subject.os, "lstat", lambda _path: pytest.fail("FD scan must not begin for other netns"))
    with pytest.raises(gate.AdmissionError, match="another network namespace"):
        authority._anchor(pid)


def fixed_image(identifier=None):
    return {"Id": identifier or subject.IMAGE_ID, "Architecture": "amd64", "Os": "linux",
            "RepoDigests": ["nginx@" + subject.IMAGE_ID],
            "RootFS": {"Type": "layers", "Layers": list(subject.IMAGE_DIFF_IDS)}}


@pytest.mark.parametrize("identifier", ["index", "manifest", "config"])
def test_fixed_content_chain_accepts_backend_representation_but_preserves_exact_id(identifier):
    identifier = {"index": subject.IMAGE_ID, "manifest": subject.IMAGE_MANIFEST_ID,
                  "config": subject.IMAGE_CONFIG_ID}[identifier]
    assert subject.validate_image(fixed_image(identifier), expected_id=identifier) == identifier
    assert len(subject.IMAGE_IDS) == 3 and subject.IMAGE_ID != subject.IMAGE_CONFIG_ID


@pytest.mark.parametrize("fault", ["unreviewed_id", "sealed_identity", "platform", "os", "missing_rootfs",
                                   "wrong_layer", "layer_order", "extra_layer", "missing_digest", "other_repo",
                                   "other_digest", "missing_id"])
def test_fixed_image_content_or_sealed_predecessor_drift_is_blocked(fault):
    image = fixed_image(subject.IMAGE_CONFIG_ID)
    expected = subject.IMAGE_CONFIG_ID
    if fault == "unreviewed_id":
        image["Id"] = "sha256:" + "f" * 64
    elif fault == "sealed_identity":
        expected = subject.IMAGE_ID
    elif fault == "platform":
        image["Architecture"] = "arm64"
    elif fault == "os":
        image["Os"] = "windows"
    elif fault == "missing_rootfs":
        image.pop("RootFS")
    elif fault == "wrong_layer":
        image["RootFS"]["Layers"][0] = "sha256:" + "f" * 64
    elif fault == "layer_order":
        image["RootFS"]["Layers"].reverse()
    elif fault == "extra_layer":
        image["RootFS"]["Layers"].append("sha256:" + "f" * 64)
    elif fault == "missing_digest":
        image["RepoDigests"] = []
    elif fault == "other_repo":
        image["RepoDigests"] = ["unreviewed/nginx@" + subject.IMAGE_ID]
    elif fault == "other_digest":
        image["RepoDigests"] = ["nginx@" + subject.IMAGE_CONFIG_ID]
    else:
        image.pop("Id")
    with pytest.raises(gate.AdmissionError, match="image content or exact identity"):
        subject.validate_image(image, expected_id=expected)


def test_chain_member_cannot_replace_an_already_sealed_retained_gateway(proof_case):
    proof, actual, _data = proof_case
    assert actual["Image"] == subject.IMAGE_ID
    actual["Image"] = subject.IMAGE_CONFIG_ID
    with pytest.raises(gate.AdmissionError, match="identity changed"):
        subject._row(proof, actual)
