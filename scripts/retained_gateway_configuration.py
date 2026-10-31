"""Exact retained-container Nginx configuration, never an image-baked claim.

Only three regular files and one fixed directory are readable. Archive members
are examined before their payload is requested; no extraction or link following
is used. A static proof is not a live custody or mutation capability.
"""
from __future__ import annotations

import hashlib
import ctypes
import base64
from datetime import datetime
import os
import json
from pathlib import Path
import re
import select
import socket
import stat
import struct
import time
from urllib.parse import quote

from services import admission_fence as gate


KIND = "retained_container_config_archive"
FILE_PATHS = ("/etc/nginx/nginx.conf", "/etc/nginx/mime.types", "/etc/nginx/conf.d/default.conf")
DIRECTORY_PATH = "/etc/nginx/conf.d"
ARCHIVE_PATHS = (*FILE_PATHS, DIRECTORY_PATH)
MAX_FILE = 64 * 1024
MAX_ARCHIVE = 128 * 1024
IMAGE_ID = "sha256:0d3b80406a13a767339fbe2f41406d6c7da727ab89cf8fae399e81f780f814d1"
RECORD_FIELDS = {"path", "device", "inode", "uid", "mode", "size", "mtime_ns", "ctime_ns", "sha256"}
IDENTITY_FIELDS = {"id", "image_id", "name", "started_at", "restart_count", "config_sha256",
                   "host_sha256", "mounts_sha256", "networks"}
PROOF_FIELDS = {"schema", "kind", "source_sha256", "container_identity", "network_id", "archives", "closure_sha256"}
SOCKET_PATH = "/run/docker.sock"
PIDFILE = "/run/docker.pid"


def source_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def identity(row):
    """Same stable fields as GatewaySwitch.gateway_identity; natural stop is OK."""
    return {"id": row["Id"], "image_id": row["Image"], "name": row["Name"],
            "started_at": row["State"]["StartedAt"], "restart_count": row["RestartCount"],
            "config_sha256": gate._hash(row["Config"]), "host_sha256": gate._hash(row["HostConfig"]),
            "mounts_sha256": gate._hash(row["Mounts"]), "networks": sorted(row["NetworkSettings"]["Networks"])}


def _octal(raw):
    raw = raw.rstrip(b"\0 ").lstrip(b" ")
    gate._require(bool(re.fullmatch(rb"[0-7]+", raw)), "non-octal or extended gateway archive metadata")
    return int(raw, 8)


def _name(raw):
    value, _separator, padding = raw.partition(b"\0")
    gate._require(not padding.strip(b"\0"), "gateway archive name padding changed")
    text = value.decode("ascii", "strict")
    gate._require(text and not text.startswith("/") and not any(x in text.split("/") for x in (".", "..", "")),
                  "unsafe gateway archive member name")
    return text


def parse_archive(read, container_path):
    """Consume a *non-prefetching* read(n), rejecting unknown headers first.

    The caller owns the original bounded stream and must not buffer/prefetch it.
    The returned digest covers the complete accepted tar including zero padding.
    """
    gate._require(container_path in ARCHIVE_PATHS, "gateway archive path not permitted")
    count, digest, members, contents, metadata = 0, hashlib.sha256(), [], {}, {}

    def block(size, *, eof=False):
        nonlocal count
        gate._require(type(size) is int and 0 < size <= MAX_FILE and count + size <= MAX_ARCHIVE,
                      "gateway archive capacity exceeded")
        raw = read(size)
        gate._require(type(raw) is bytes and len(raw) <= size, "gateway archive reader over-read")
        if eof and not raw:
            return raw
        gate._require(len(raw) == size, "truncated gateway archive")
        count += len(raw)
        digest.update(raw)
        return raw

    directory = container_path == DIRECTORY_PATH
    names = {"conf.d": b"5", "conf.d/default.conf": b"0"} if directory else {Path(container_path).name: b"0"}
    while True:
        header = block(512)
        if header == b"\0" * 512:
            gate._require(block(512) == b"\0" * 512, "gateway archive end marker incomplete")
            while padding := block(512, eof=True):
                gate._require(padding == b"\0" * 512, "gateway archive trailing data")
            break
        # USTAR only: no PAX/GNU extension may rename, resize or redirect a member.
        kind = header[156:157]
        gate._require(kind in {b"0", b"\0", b"5"}, "gateway archive links or extensions forbidden")
        gate._require(header[257:263] == b"ustar\0" and header[263:265] == b"00"
                      and not header[345:500].strip(b"\0") and not header[157:257].strip(b"\0"),
                      "gateway archive extended metadata forbidden")
        gate._require(_octal(header[148:156]) == sum(header[:148]) + 8 * 32 + sum(header[156:]),
                      "gateway archive header checksum mismatch")
        raw_name = header[:100]
        if kind == b"5":
            raw_name = raw_name.rstrip(b"\0").removesuffix(b"/")
        name = _name(raw_name)
        normalized_kind = b"0" if kind == b"\0" else kind
        # This check must precede any request for member data.
        gate._require(name in names and name not in members and normalized_kind == names[name],
                      "unknown or duplicate gateway archive member")
        mode, uid, gid, size = (_octal(header[a:b]) for a, b in ((100, 108), (108, 116), (116, 124), (124, 136)))
        gate._require(uid == gid == 0 and mode in ({0o700, 0o755} if kind == b"5" else {0o400, 0o444, 0o600, 0o644})
                      and (size == 0 if kind == b"5" else 0 < size <= MAX_FILE), "unsafe gateway archive member")
        metadata[name] = {"kind": normalized_kind.decode(), "mode": mode, "uid": uid, "gid": gid,
                          "size": size, "mtime": _octal(header[136:148])}
        members.append(name)
        if size:
            raw = block(size)
            contents[name] = raw
            padding_size = (-size) % 512
            if padding_size:
                gate._require(block(padding_size) == b"\0" * padding_size, "gateway archive nonzero member padding")
    gate._require(set(members) == set(names), "gateway archive member closure incomplete")
    return {"sha256": digest.hexdigest(), "bytes": count, "members": metadata, "contents": contents}


def _tokens(raw):
    """Bounded Nginx lexer sufficient to find every directive's first token."""
    text = raw.decode("utf-8", "strict")
    gate._require(all(ord(c) >= 32 or c in "\t\r\n" for c in text) and "\x7f" not in text,
                  "gateway configuration contains control bytes")
    tokens, token, quoted, escaped, comment = [], [], None, False, False
    for char in text:
        if comment:
            comment = char != "\n"
            continue
        if escaped:
            token.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif quoted:
            if char == quoted:
                quoted = None
            else:
                token.append(char)
        elif char in "\"'":
            quoted = char
        elif char == "#":
            if token:
                tokens.append("".join(token))
                token = []
            comment = True
        elif char.isspace() or char in ";{}":
            if token:
                tokens.append("".join(token))
                token = []
            if char in ";{}":
                tokens.append(char)
        else:
            token.append(char)
    gate._require(not quoted and not escaped, "gateway configuration unterminated quoting")
    if token:
        tokens.append("".join(token))
    return tokens


def _includes(raw):
    includes, statement, depth = [], [], 0
    for token in _tokens(raw):
        if token in {";", "{", "}"}:
            if statement and statement[0] == "include":
                gate._require(token == ";" and len(statement) == 2, "invalid gateway include directive")
                includes.append(statement[1])
            if token == "{":
                depth += 1
            elif token == "}":
                depth -= 1
                gate._require(depth >= 0, "unbalanced gateway configuration")
            statement = []
        else:
            statement.append(token)
    gate._require(not statement and depth == 0, "incomplete gateway configuration")
    return includes


def _closure(archives):
    files = {path: archives[path]["contents"][Path(path).name] for path in FILE_PATHS}
    directory = archives[DIRECTORY_PATH]
    gate._require(directory["contents"]["conf.d/default.conf"] == files[FILE_PATHS[2]]
                  and directory["members"]["conf.d/default.conf"] == archives[FILE_PATHS[2]]["members"]["default.conf"],
                  "gateway singleton and directory disagree")
    gate._require(_includes(files[FILE_PATHS[0]]) == [FILE_PATHS[1], DIRECTORY_PATH + "/*.conf"]
                  and not _includes(files[FILE_PATHS[1]]) and not _includes(files[FILE_PATHS[2]]),
                  "gateway include closure differs from fixed whitelist")
    closure = {"files": {path: hashlib.sha256(raw).hexdigest() for path, raw in files.items()},
               "archives": {path: {key: value[key] for key in ("sha256", "bytes", "members")}
                            for path, value in archives.items()}}
    return files, gate._hash(closure)


def _metadata(path, info):
    return {"path": str(path), "device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid,
            "mode": stat.S_IMODE(info.st_mode), "size": info.st_size, "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns}


def _local(record, container_path, *, describe=False):
    # The application image hashes/imports this module, but never receives the
    # host-only private-release controller or authority to load host originals.
    from scripts import private_release_contract as private
    gate._require(isinstance(record, dict) and set(record) == RECORD_FIELDS, "exact private gateway archive identity required")
    path = private.safe_path(record["path"])
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        metadata = _metadata(path, before)
        gate._require(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid() and before.st_nlink == 1
                      and metadata["mode"] in {0o400, 0o600} and 0 < before.st_size <= MAX_ARCHIVE,
                      "unsafe private gateway archive")
        gate._require(describe or all(type(record[key]) is type(value) and record[key] == value
                                     for key, value in metadata.items()), "private gateway archive metadata changed")
        parsed = parse_archive(lambda size: os.read(fd, size), container_path)
        gate._require(_metadata(path, os.fstat(fd)) == _metadata(path, path.lstat()) == metadata
                      and before.st_nlink == path.lstat().st_nlink and parsed["bytes"] == before.st_size,
                      "private gateway archive changed while reading")
        gate._require(describe or record["sha256"] == parsed["sha256"], "private gateway archive bytes changed")
        return parsed, {**metadata, "sha256": parsed["sha256"]}
    finally:
        os.close(fd)


def archive_identity(path, container_path):
    """Describe a private accepted archive without reading unknown payloads."""
    blank = dict.fromkeys(RECORD_FIELDS)
    blank["path"] = str(path)
    return _local(blank, container_path, describe=True)[1]


def _validate_identity(value):
    gate._require(isinstance(value, dict) and set(value) == IDENTITY_FIELDS
                  and gate.HEX64.fullmatch(str(value["id"])) and value["image_id"] == IMAGE_ID
                  and isinstance(value["name"], str) and bool(re.fullmatch(r"/[A-Za-z0-9_.-]{1,128}", value["name"]))
                  and isinstance(value["started_at"], str) and bool(value["started_at"])
                  and type(value["restart_count"]) is int and value["restart_count"] == 0
                  and all(gate.HEX64.fullmatch(str(value[key])) for key in
                          ("config_sha256", "host_sha256", "mounts_sha256"))
                  and isinstance(value["networks"], list) and len(value["networks"]) == 1
                  and bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value["networks"][0])),
                  "exact retained gateway archive container identity required")


def validate(proof):
    """Load only sealed local raw archives; never Docker, sockets or mutation."""
    gate._require(isinstance(proof, dict) and set(proof) == PROOF_FIELDS
                  and type(proof["schema"]) is int and proof["schema"] == 1 and proof["kind"] == KIND
                  and proof["source_sha256"] == source_sha256() and gate.HEX64.fullmatch(str(proof["network_id"]))
                  and isinstance(proof["archives"], dict) and set(proof["archives"]) == set(ARCHIVE_PATHS)
                  and gate.HEX64.fullmatch(str(proof["closure_sha256"])), "exact retained gateway archive proof required")
    _validate_identity(proof["container_identity"])
    gate._require(len({record.get("path") for record in proof["archives"].values()}) == 4,
                  "private gateway archives must be distinct")
    archives = {path: _local(proof["archives"][path], path)[0] for path in ARCHIVE_PATHS}
    files, closure = _closure(archives)
    gate._require(closure == proof["closure_sha256"], "retained gateway include proof changed")
    return {"files": files, "closure_sha256": closure, "container_identity": proof["container_identity"],
            "network_id": proof["network_id"]}


def describe(row, network_id, archives):
    """Construct a static proof from reviewed private original archive files."""
    gate._require(isinstance(archives, dict) and set(archives) == set(ARCHIVE_PATHS), "fixed gateway archives required")
    records = {path: archive_identity(archives[path], path) for path in ARCHIVE_PATHS}
    parsed = {path: _local(records[path], path)[0] for path in ARCHIVE_PATHS}
    _files, closure = _closure(parsed)
    proof = {"schema": 1, "kind": KIND, "source_sha256": source_sha256(), "container_identity": identity(row),
             "network_id": network_id, "archives": records, "closure_sha256": closure}
    validate(proof)
    _row(proof, row)
    return proof


def _row(proof, row):
    gate._require(identity(row) == proof["container_identity"] and row["Mounts"] == []
                  and row["Image"] == IMAGE_ID and len(row["NetworkSettings"]["Networks"]) == 1
                  and next(iter(row["NetworkSettings"]["Networks"].values()))["NetworkID"] == proof["network_id"]
                  and not any(row["State"].get(key) for key in ("Paused", "Restarting", "Dead", "OOMKilled")),
                  "actual retained gateway archive identity changed")


def verify(proof, row, *, inspect, archive):
    """Compare actual fixed archives; archive(id,path,consumer) must not prefetch."""
    result = validate(proof)
    _row(proof, row)
    identifier = proof["container_identity"]["id"]
    _row(proof, inspect(identifier))
    actual = {}
    for path in ARCHIVE_PATHS:
        actual[path] = archive(identifier, path, lambda read, fixed=path: parse_archive(read, fixed))
        gate._require(actual[path]["sha256"] == proof["archives"][path]["sha256"],
                      "actual retained gateway archive bytes changed")
    files, closure = _closure(actual)
    _row(proof, inspect(identifier))
    gate._require(closure == proof["closure_sha256"] and files == result["files"],
                  "actual retained gateway include closure changed")
    return result


def _small(path, maximum):
    """Fixed procfs/pid inputs, without following a caller-selected path."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        gate._require(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
                      and not stat.S_IMODE(before.st_mode) & 0o022,
                      "unsafe gateway archive authority input")
        value = os.read(fd, maximum + 1)
        metadata = _stat_identity(before)
        gate._require(len(value) <= maximum and _stat_identity(os.fstat(fd)) == metadata
                      and _stat_identity(os.stat(path, follow_symlinks=False)) == metadata,
                      "gateway archive authority input changed")
        return value, metadata
    finally:
        os.close(fd)


def _process(pid, *, systemd=False):
    raw, _metadata = _small(f"/proc/{pid}/stat", 8192)
    parts = raw[raw.rfind(b")") + 2:].split()
    gate._require(raw.startswith(str(pid).encode() + b" (") and len(parts) >= 20,
                  "gateway archive authority process stat invalid")
    status, _metadata = _small(f"/proc/{pid}/status", 16384)
    fields = dict(line.split(b":", 1) for line in status.splitlines() if b":" in line)
    gate._require(fields.get(b"Uid", b"").split() == fields.get(b"Gid", b"").split() == [b"0"] * 4
                  and fields.get(b"TracerPid", b"").strip() == b"0"
                  and fields.get(b"Pid", b"").strip() == fields.get(b"Tgid", b"").strip() == str(pid).encode(),
                  "gateway archive authority process is not untraced root")
    executable = os.readlink(f"/proc/{pid}/exe")
    wanted = {"/lib/systemd/systemd", "/usr/lib/systemd/systemd"} if systemd else {"/usr/bin/dockerd"}
    gate._require(executable in wanted and (pid == 1 if systemd else pid > 1),
                  "gateway archive authority executable differs")
    actual_exe = os.stat(f"/proc/{pid}/exe")
    fixed_exe = os.stat(executable, follow_symlinks=False)
    gate._require(stat.S_ISREG(fixed_exe.st_mode) and fixed_exe.st_uid == fixed_exe.st_gid == 0
                  and not stat.S_IMODE(fixed_exe.st_mode) & 0o022
                  and _stat_identity(actual_exe) == _stat_identity(fixed_exe),
                  "gateway archive authority executable file differs")
    namespace = os.stat(f"/proc/{pid}/ns/net")
    return {"pid": pid, "start_ticks": int(parts[19]), "exe": executable,
            "exe_link": _link_identity(f"/proc/{pid}/exe"),
            "exe_identity": _stat_identity(actual_exe),
            "parent": fields.get(b"PPid", b"").strip(), "namespace": os.readlink(f"/proc/{pid}/ns/net"),
            "namespace_identity": (namespace.st_dev, namespace.st_ino)}


def _link_identity(path):
    info = os.lstat(path)
    gate._require(stat.S_ISLNK(info.st_mode), "gateway archive authority link required")
    value = os.readlink(path)
    gate._require(len(value) <= 4096 and os.lstat(path) == info, "gateway archive authority link changed")
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_ctime_ns, value)


def _stat_identity(info):
    # Reading may update atime; file identity and mutation timestamps may not change.
    return tuple(getattr(info, "st_" + key) for key in
                 ("dev", "ino", "mode", "uid", "gid", "nlink", "size", "mtime_ns", "ctime_ns"))


def _proc_identity(info):
    return tuple(getattr(info, "st_" + key) for key in ("dev", "ino", "mode", "uid", "gid", "nlink", "size"))


def _procfs(fd):
    library = ctypes.CDLL(None, use_errno=True)
    function = library.fstatfs
    function.argtypes, function.restype = [ctypes.c_int, ctypes.c_void_p], ctypes.c_int
    storage = ctypes.create_string_buffer(256)
    gate._require(function(fd, ctypes.byref(storage)) == 0
                  and ctypes.cast(storage, ctypes.POINTER(ctypes.c_long)).contents.value == 0x9FA0,
                  "actual Docker archive procfs required")


def _unix_table():
    """Previously proven typed procfs fields; proc timestamps are not identity."""
    path = "/proc/net/unix"
    before = os.lstat(path)
    gate._require(before.st_mode == stat.S_IFREG | 0o444 and before.st_uid == before.st_gid == 0
                  and before.st_nlink == 1 and before.st_size == 0, "unsafe Docker archive procfs table")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        _procfs(fd)
        fixed = _proc_identity(before)
        gate._require(_proc_identity(os.fstat(fd)) == fixed, "Docker archive procfs table open changed")
        raw = bytearray()
        while len(raw) <= 512 * 1024:
            chunk = os.read(fd, min(4096, 512 * 1024 + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        gate._require(len(raw) <= 512 * 1024 and _proc_identity(os.fstat(fd)) == fixed
                      and _proc_identity(os.lstat(path)) == fixed, "Docker archive procfs table changed/capacity")
        return bytes(raw), fixed
    finally:
        os.close(fd)


def _listener(raw):
    lines = raw.decode("ascii", "strict").splitlines()
    gate._require(0 < len(lines) <= 4096 and lines[0].split() ==
                  ["Num", "RefCount", "Protocol", "Flags", "Type", "St", "Inode", "Path"],
                  "Docker archive listener table capacity/header")
    listeners, accepted, inodes = [], [], set()
    for line in lines[1:]:
        row = line.split()
        if len(row) < 8 or row[7] not in {SOCKET_PATH, "/var/run/docker.sock"}:
            continue
        gate._require(len(row) == 8 and re.fullmatch(r"[1-9][0-9]{0,19}", row[6])
                      and row[6] not in inodes, "Docker archive socket table identity ambiguous")
        inodes.add(row[6])
        shape = tuple(row[2:6])
        if shape == ("00000000", "00010000", "0001", "01"):
            listeners.append(row)
        elif shape == ("00000000", "00000000", "0001", "03"):
            accepted.append(row)
        else:
            raise gate.AdmissionUncertain("Docker archive target socket tuple unknown")
    gate._require(len(listeners) == 1 and all(row[7] == listeners[0][7] for row in accepted),
                  "Docker archive listener is not unique")
    return {"inode": int(listeners[0][6]), "path": listeners[0][7], "shape": tuple(listeners[0][2:6])}


def _current_listener():
    first, fixed = _unix_table()
    second, second_fixed = _unix_table()
    listener, other = _listener(first), _listener(second)
    gate._require(listener == other and fixed == second_fixed,
                  "Docker archive listener changed between complete reads")
    return listener


def _alive(fd):
    poll = select.poll()
    poll.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    gate._require(not poll.poll(0), "gateway archive authority process exited")


class _ArchiveAuthority:
    """Current socket path, listener FD, pidfd and actual response-writer seal."""
    def __init__(self, deadline):
        self.deadline, self.pidfds, self.processes, self.held = deadline, {}, {}, {}
        self.namespace_fd = None

    def __enter__(self):
        gate._require(os.geteuid() == 0 and os.uname().sysname == "Linux"
                      and hasattr(os, "pidfd_open") and all(hasattr(socket, name) for name in
                      ("SO_PEERCRED", "SO_PASSCRED", "SCM_CREDENTIALS")), "native root archive authority required")
        try:
            self.socket = os.lstat(SOCKET_PATH)
            gate._require(stat.S_ISSOCK(self.socket.st_mode) and self.socket.st_uid == 0
                          and stat.S_IMODE(self.socket.st_mode) == 0o660 and self.socket.st_nlink == 1,
                          "unsafe fixed Docker archive socket")
            self.pid_raw, self.pid_metadata = _small(PIDFILE, 32)
            gate._require(re.fullmatch(rb"[1-9][0-9]{0,9}\n?", self.pid_raw) is not None
                          and 1 < int(self.pid_raw) < 2**31, "fixed Docker archive daemon PID required")
            self.pid = int(self.pid_raw)
            self.namespace_fd = os.open("/proc/self/ns/net", os.O_RDONLY | os.O_CLOEXEC)
            namespace = os.fstat(self.namespace_fd)
            self.namespace = namespace.st_dev, namespace.st_ino
            self.listener_row = _current_listener()
            self.listener = self.listener_row["inode"]
            self._anchor(self.pid)
            self.confirm()
            return self
        except BaseException:
            self.close()
            raise

    def _anchor(self, pid):
        self.pidfds[pid] = os.pidfd_open(pid, 0)
        _alive(self.pidfds[pid])
        self.processes[pid] = _process(pid, systemd=pid == 1)
        gate._require(self.processes[pid]["namespace_identity"] == self.namespace,
                      "Docker archive authority process uses another network namespace")
        fdroot = f"/proc/{pid}/fd"
        before, held = os.lstat(fdroot), {}
        gate._require(stat.S_ISDIR(before.st_mode) and before.st_uid == before.st_gid == 0,
                      "unsafe Docker archive FD directory")
        directory = os.open(fdroot, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            _procfs(directory)
            fixed = _proc_identity(before)
            gate._require(_proc_identity(os.fstat(directory)) == fixed, "Docker archive FD directory open changed")
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    gate._require(index < 256 and time.monotonic() < self.deadline,
                                  "Docker archive FD scan capacity/deadline")
                    gate._require(entry.name.isdigit(), "unexpected Docker archive FD name")
                    path = fdroot + "/" + entry.name
                    try:
                        value = _link_identity(path)
                    except FileNotFoundError:
                        raise gate.AdmissionUncertain("Docker archive FD inventory changed") from None
                    if value[-1] == f"socket:[{self.listener}]":
                        held[path] = value
            gate._require(held and _proc_identity(os.fstat(directory)) == _proc_identity(os.lstat(fdroot)) == fixed,
                          "Docker archive listener FD ownership unconfirmed")
        finally:
            os.close(directory)
        self.held[pid] = held

    def peer(self, value):
        gate._require(value in {(self.pid, 0, 0), (1, 0, 0)}, "Docker archive socket peer not recognized")
        if value[0] == 1 and 1 not in self.pidfds:
            self._anchor(1)
        self.confirm()

    def confirm(self):
        gate._require(time.monotonic() < self.deadline
                      and _stat_identity(os.lstat(SOCKET_PATH)) == _stat_identity(self.socket)
                      and _small(PIDFILE, 32) == (self.pid_raw, self.pid_metadata),
                      "Docker archive socket/PID authority changed")
        namespace = os.stat("/proc/self/ns/net")
        held_namespace = os.fstat(self.namespace_fd)
        gate._require((namespace.st_dev, namespace.st_ino) == self.namespace ==
                      (held_namespace.st_dev, held_namespace.st_ino)
                      and _current_listener() == self.listener_row, "Docker archive listener/namespace changed")
        for pid, fd in self.pidfds.items():
            _alive(fd)
            gate._require(_process(pid, systemd=pid == 1) == self.processes[pid],
                          "Docker archive daemon lifecycle changed")
            for path, value in self.held[pid].items():
                gate._require(_link_identity(path) == value, "Docker archive listener FD changed")
            _alive(fd)

    def close(self):
        for fd in self.pidfds.values():
            os.close(fd)
        self.pidfds = {}
        if self.namespace_fd is not None:
            os.close(self.namespace_fd)
            self.namespace_fd = None

    def __exit__(self, *_args):
        self.close()


def _close_rights(controls):
    import array
    for level, kind, raw in controls:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            values = array.array("i")
            values.frombytes(raw[:len(raw) - len(raw) % values.itemsize])
            for fd in values:
                os.close(fd)


class _ArchiveResponse:
    """No makefile/read-ahead: recvmsg never exceeds parser's immediate need."""
    def __init__(self, connection, expected, deadline, container_path):
        gate._require(container_path in ARCHIVE_PATHS, "fixed Docker archive response path required")
        self.connection, self.expected, self.deadline, self.path = connection, expected, deadline, container_path
        self.total, self.segments, self.remaining, self.eof = 0, 0, None, False

    def _recv(self, maximum):
        gate._require(0 < maximum <= MAX_FILE and time.monotonic() < self.deadline,
                      "Docker archive response deadline")
        self.connection.settimeout(self.deadline - time.monotonic())
        raw, controls, flags, _address = self.connection.recvmsg(maximum, socket.CMSG_SPACE(12))
        _close_rights(controls)
        gate._require(time.monotonic() < self.deadline and flags == 0, "Docker archive response deadline/flags")
        if not raw:
            sentinel = [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, b"\0" * 12)]
            gate._require(not controls or controls == sentinel, "Docker archive EOF credentials invalid")
            self.eof = True
            return raw
        gate._require(controls == [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, struct.pack("3i", *self.expected))],
                      "Docker archive actual writer identity differs")
        self.total += len(raw)
        self.segments += 1
        gate._require(len(raw) <= maximum and self.total <= MAX_ARCHIVE + 4096 and self.segments <= 8192,
                      "Docker archive response capacity")
        return raw

    def header(self):
        raw = bytearray()
        while not raw.endswith(b"\r\n\r\n"):
            gate._require(len(raw) < 4096, "Docker archive HTTP header capacity")
            value = self._recv(1)
            gate._require(value, "Docker archive HTTP header incomplete")
            raw.extend(value)
        lines = raw[:-4].decode("ascii", "strict").split("\r\n")
        gate._require(lines[0] in {"HTTP/1.0 200 OK", "HTTP/1.1 200 OK"}, "Docker archive HTTP status not success")
        fields = {}
        for line in lines[1:]:
            key, separator, value = line.partition(":")
            key = key.lower()
            gate._require(separator and re.fullmatch(r"[a-z0-9!#$%&'*+.^_`|~-]+", key)
                          and key not in fields and all(32 <= ord(c) < 127 for c in value),
                          "Docker archive HTTP header invalid")
            fields[key] = value.strip()
        gate._require("transfer-encoding" not in fields and fields.get("content-type") == "application/x-tar",
                      "Docker archive HTTP framing/type unsupported")
        raw_stat = base64.b64decode(fields.get("x-docker-container-path-stat", ""), validate=True)
        gate._require(0 < len(raw_stat) <= 2048, "Docker archive path stat missing/capacity")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                gate._require(key not in result, "Docker archive path stat duplicate key")
                result[key] = value
            return result
        actual = json.loads(raw_stat, object_pairs_hook=unique)
        directory = self.path == DIRECTORY_PATH
        gate._require(isinstance(actual, dict) and set(actual) == {"name", "size", "mode", "mtime", "linkTarget"}
                      and actual["name"] == Path(self.path).name and actual["linkTarget"] == ""
                      and type(actual["size"]) is int and (0 <= actual["size"] <= MAX_FILE if directory
                                                           else 0 < actual["size"] <= MAX_FILE)
                      and type(actual["mode"]) is int and actual["mode"] in
                      ({(1 << 31) | 0o700, (1 << 31) | 0o755} if directory else {0o400, 0o444, 0o600, 0o644})
                      and isinstance(actual["mtime"], str) and 0 < len(actual["mtime"]) <= 64,
                      "Docker archive actual path is not fixed ordinary file/directory")
        modified = datetime.fromisoformat(actual["mtime"].replace("Z", "+00:00"))
        gate._require(modified.tzinfo is not None, "Docker archive path timestamp lacks timezone")
        self.path_stat = {**actual, "mtime_epoch": int(modified.timestamp())}
        if "content-length" in fields:
            gate._require(re.fullmatch(r"[0-9]{1,6}", fields["content-length"])
                          and 0 < int(fields["content-length"]) <= MAX_ARCHIVE, "Docker archive content length invalid")
            self.remaining = int(fields["content-length"])
        else:
            gate._require(lines[0].startswith("HTTP/1.0 ") or fields.get("connection", "").lower() == "close",
                          "Docker archive EOF framing not explicit")

    def read(self, size):
        gate._require(type(size) is int and 0 < size <= MAX_FILE, "Docker archive bounded read required")
        if self.remaining == 0 or self.eof:
            return b""
        requested = size if self.remaining is None else min(size, self.remaining)
        value = bytearray()
        while len(value) < requested and not self.eof:
            value.extend(self._recv(requested - len(value)))
        if self.remaining is not None:
            self.remaining -= len(value)
        return bytes(value)

    def finish(self):
        gate._require(self.remaining in (None, 0), "Docker archive HTTP body incomplete")
        gate._require(self.eof or not self._recv(1), "Docker archive HTTP extra body")


def read_native_archive(identifier, container_path, consumer, *, deadline):
    """One fixed GET under current daemon authority and the caller's old budget."""
    gate._require(isinstance(identifier, str) and gate.HEX64.fullmatch(identifier)
                  and container_path in ARCHIVE_PATHS and callable(consumer)
                  and type(deadline) in (int, float) and time.monotonic() < deadline <= time.monotonic() + 3,
                  "fixed bounded native gateway archive request required")
    with _ArchiveAuthority(deadline) as authority:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            connection.settimeout(max(0.001, deadline - time.monotonic()))
            connection.connect(SOCKET_PATH)
            raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            gate._require(len(raw) == 12, "Docker archive peer credentials incomplete")
            authority.peer(struct.unpack("3i", raw))
            route = "/containers/" + identifier + "/archive?path=" + quote(container_path, safe="")
            connection.settimeout(max(0.001, deadline - time.monotonic()))
            connection.sendall(("GET " + route + " HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n").encode("ascii"))
            response = _ArchiveResponse(connection, (authority.pid, 0, 0), deadline, container_path)
            response.header()
            result = consumer(response.read)
            response.finish()
            name = "conf.d" if container_path == DIRECTORY_PATH else Path(container_path).name
            member = result["members"][name]
            gate._require(member["mode"] == response.path_stat["mode"] & 0o777
                          and member["mtime"] == response.path_stat["mtime_epoch"]
                          and (container_path == DIRECTORY_PATH or member["size"] == response.path_stat["size"]),
                          "Docker archive header and actual path stat disagree")
            authority.confirm()
            return result
