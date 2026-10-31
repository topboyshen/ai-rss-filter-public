"""Fixed bounded HTTP client inside one inspected sender network namespace.

No shell, sudo, published port, mount namespace or disposable client container.
Secrets travel only on stdin. A changed identity after HTTP is an unknown result
and never permission to repeat a mutation.
"""
from __future__ import annotations

import ctypes
import http.client
import json
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import stat
import subprocess
import sys
import time

HEX64 = re.compile(r"[0-9a-f]{64}\Z")
MAX_INPUT = 16384
MAX_HTTP = 65536
MAX_OUTPUT = 131072
TOTAL_SECONDS = 3
HTTP_SECONDS = 2
MAX_PROC_BYTES = 524288
MAX_PROC_LINES = 4096
MAX_PROC_TOTAL = 655360
MAX_OWNER_FDS = 256
PROC_SUPER_MAGIC = 0x9FA0


class NamespaceUncertain(RuntimeError):
    """Only fixed diagnostics may cross this boundary."""


def require(condition):
    if not condition:
        raise NamespaceUncertain("sender namespace request unconfirmed; query original operation only")


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _parsed(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result)
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs)
    _encoded(value)  # Reject non-finite JSON numbers.
    return value


def _capture(argv, *, deadline, input_bytes=None, max_bytes=MAX_OUTPUT, pass_fds=()):
    require(input_bytes is None or len(input_bytes) <= MAX_INPUT)
    require(time.monotonic() < deadline)
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, start_new_session=True,
            pass_fds=pass_fds, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, cwd="/")
    except OSError:
        raise NamespaceUncertain("sender namespace client unavailable") from None
    output, total, sent = bytearray(), 0, 0
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            if process.stdin is not None:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                require(remaining > 0)
                for key, _mask in selector.select(min(0.2, remaining)):
                    if key.fileobj is process.stdin:
                        try:
                            sent += os.write(key.fd, input_bytes[sent:])
                        except BlockingIOError:
                            continue
                        if sent == len(input_bytes):
                            selector.unregister(key.fileobj)
                            process.stdin.close()
                        continue
                    try:
                        chunk = os.read(key.fd, min(65536, max_bytes + 1 - total))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    require(total <= max_bytes)
                    if key.fileobj is process.stdout:
                        output.extend(chunk)
            require(process.wait(timeout=max(0.001, deadline - time.monotonic())) == 0
                    and time.monotonic() < deadline)
        return bytes(output)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise NamespaceUncertain("sender namespace request unconfirmed; query original operation only") from None
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)  # Only our local nsenter/client process group.
        process.wait(timeout=1)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


def _inspect(container_id, deadline):
    raw = _capture(["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", "inspect", container_id],
                   deadline=deadline, max_bytes=1024**2)
    rows = _parsed(raw)
    require(isinstance(rows, list) and len(rows) == 1 and rows[0]["Id"] == container_id)
    row, state = rows[0], rows[0]["State"]
    require(state["Running"] is True and not state.get("Paused") and not state.get("Restarting")
            and not state.get("Dead") and type(state["Pid"]) is int and state["Pid"] > 1
            and type(row["RestartCount"]) is int and row["RestartCount"] >= 0
            and isinstance(state["StartedAt"], str) and state["StartedAt"] and not state["StartedAt"].startswith("0001-")
            and isinstance(row["Image"], str) and row["Image"].startswith("sha256:") and HEX64.fullmatch(row["Image"][7:]))
    return {"container_id": container_id, "pid": state["Pid"], "started_at": state["StartedAt"],
            "restart_count": row["RestartCount"], "image_id": row["Image"]}


def _process_identity(pid):
    root = Path("/proc") / str(pid)
    with (root / "stat").open("rb") as handle:
        raw = handle.read(8193)
    require(len(raw) <= 8192)
    fields = raw[raw.rfind(b")") + 2:].split()
    require(len(fields) > 20)
    net = (root / "ns/net").stat()
    return {"pid": pid, "start_ticks": int(fields[19]), "network_device": net.st_dev, "network_inode": net.st_ino}


def _open_namespace(pid):
    return os.open(f"/proc/{pid}/ns/net", os.O_RDONLY)


def _filesystem_type(fd):
    library = ctypes.CDLL(None, use_errno=True)
    function = library.fstatfs
    function.argtypes = [ctypes.c_int, ctypes.c_void_p]
    function.restype = ctypes.c_int
    storage = ctypes.create_string_buffer(256)
    require(function(fd, ctypes.byref(storage)) == 0)
    return ctypes.cast(storage, ctypes.POINTER(ctypes.c_long)).contents.value


def _duplicate_process_fd(pidfd, target_fd):
    # Linux x86_64 and aarch64 both assign pidfd_getfd syscall number 438.
    # Unsupported architectures and EPERM remain unknown; no weaker fallback.
    require(os.uname().sysname == "Linux" and os.uname().machine in {"x86_64", "aarch64"})
    library = ctypes.CDLL(None, use_errno=True)
    function = library.syscall
    function.argtypes = [ctypes.c_long, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    function.restype = ctypes.c_int
    fd = function(438, pidfd, target_fd, 0)
    require(fd >= 0)
    return fd


class _PinnedListener:
    """Live sender FD capability, held until the HTTP result is classified."""
    def __init__(self, owner, port, deadline):
        self.owner, self.port, self.deadline = owner, port, deadline
        self.pidfd = self.listener = None
        self.inode = self.sender_fd = None
        self.read_bytes = 0

    def _window(self):
        require(time.monotonic() < self.deadline and _process_identity(self.owner["pid"]) == self.owner)

    def _read_table(self, family):
        require(family in ("tcp", "tcp6"))
        self._window()
        path = "/proc/self/net/" + family
        before = os.lstat(path)
        def fixed(value):
            return tuple(getattr(value, key) for key in
                ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink", "st_size"))
        require(before.st_mode == stat.S_IFREG | 0o444 and before.st_uid == before.st_gid == 0
                and before.st_nlink == 1 and before.st_size == 0)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            require(_filesystem_type(fd) == PROC_SUPER_MAGIC and fixed(os.fstat(fd)) == fixed(before))
            raw = bytearray()
            while len(raw) <= MAX_PROC_BYTES:
                self._window()
                block = os.read(fd, min(4096, MAX_PROC_BYTES + 1 - len(raw)))
                if not block:
                    break
                raw.extend(block)
                self.read_bytes += len(block)
                require(self.read_bytes <= MAX_PROC_TOTAL)
            require(len(raw) <= MAX_PROC_BYTES and fixed(os.fstat(fd)) == fixed(before)
                    and fixed(os.lstat(path)) == fixed(before))
            return bytes(raw)
        finally:
            os.close(fd)

    def _table_listener(self):
        found = []
        for family in ("tcp", "tcp6"):
            lines = self._read_table(family).decode("ascii", "strict").splitlines()
            require(0 < len(lines) <= MAX_PROC_LINES)
            header = lines[0].split()
            require(header[:2] == ["sl", "local_address"] and header[2] in {"rem_address", "remote_address"}
                    and header[3] == "st" and "inode" in header)
            digits = 8 if family == "tcp" else 32
            endpoint = re.compile(r"[0-9A-F]{" + str(digits) + r"}:[0-9A-F]{4}\Z")
            for line in lines[1:]:
                self._window()
                fields = line.split()
                require(len(fields) >= 10 and endpoint.fullmatch(fields[1])
                        and endpoint.fullmatch(fields[2]) and re.fullmatch(r"[0-9A-F]{2}", fields[3]))
                if int(fields[1].rsplit(":", 1)[1], 16) != self.port:
                    continue
                require(fields[3] in {"01", "02", "03", "04", "05", "06", "07", "08", "09", "0A", "0B", "0C"})
                if fields[3] != "0A":
                    continue
                # Unknown/v6-only or wildcard same-port listeners are not adopted.
                require(family == "tcp" and fields[1] == f"0100007F:{self.port:04X}"
                        and fields[2] == "00000000:0000" and re.fullmatch(r"[1-9][0-9]{0,19}", fields[9]))
                found.append(int(fields[9]))
        require(len(found) == 1)
        return found[0]

    def _owned_fds(self, inode):
        self._window()
        found = []
        with os.scandir(f"/proc/{self.owner['pid']}/fd") as entries:
            for index, entry in enumerate(entries):
                require(index < MAX_OWNER_FDS and re.fullmatch(r"0|[1-9][0-9]{0,9}", entry.name))
                self._window()
                if os.readlink(entry.path) == f"socket:[{inode}]":
                    found.append(int(entry.name))
        require(found)
        self._window()
        return sorted(found)

    def confirm(self):
        self._window()
        require(self._table_listener() == self.inode)
        require(self.sender_fd in self._owned_fds(self.inode))
        require(self._table_listener() == self.inode)
        require(os.readlink(f"/proc/{self.owner['pid']}/fd/{self.sender_fd}") == f"socket:[{self.inode}]")
        require(os.fstat(self.listener.fileno()).st_ino == self.inode
                and self.listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) == socket.SOCK_STREAM
                and self.listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1
                and self.listener.getsockname() == ("127.0.0.1", self.port)
                and self.listener.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT) == 0)
        self._window()

    def __enter__(self):
        try:
            self._window()
            require(hasattr(os, "pidfd_open"))
            self.pidfd = os.pidfd_open(self.owner["pid"], 0)
            self.inode = self._table_listener()
            self.sender_fd = self._owned_fds(self.inode)[0]
            fd = _duplicate_process_fd(self.pidfd, self.sender_fd)
            try:
                self.listener = socket.socket(fileno=fd)
            except BaseException:
                os.close(fd)
                raise
            self.confirm()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_args):
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        if self.pidfd is not None:
            os.close(self.pidfd)
            self.pidfd = None


def _listener_owner(owner, port, deadline):
    return _PinnedListener(owner, port, deadline)


def _validate_request(port, method, path, token, payload):
    require(isinstance(path, str) and isinstance(method, str))
    allowed = ((method == "GET" and (path in {"/registration", "/activation-registration"}
                or (path.startswith("/operations/") and HEX64.fullmatch(path[12:]))))
               or (method == "POST" and path in {"/stop-new", "/pause", "/initialize-closed", "/production-resume"}))
    require(type(port) is int and 1 <= port <= 65535 and isinstance(path, str) and allowed
            and isinstance(token, str) and HEX64.fullmatch(token)
            and ((method == "GET" and payload is None) or (method == "POST" and isinstance(payload, dict)))
            and len(_encoded(payload)) <= 8192)


def request(container_id, port, method, path, token, payload=None):
    require(isinstance(container_id, str) and HEX64.fullmatch(container_id))
    _validate_request(port, method, path, token, payload)
    deadline = time.monotonic() + TOTAL_SECONDS
    fd = None
    try:
        before = _inspect(container_id, deadline)
        owner = _process_identity(before["pid"])
        fd = _open_namespace(before["pid"])
        info = os.fstat(fd)
        require((info.st_dev, info.st_ino) == (owner["network_device"], owner["network_inode"])
                and _process_identity(before["pid"]) == owner)
        value = {"port": port, "method": method, "path": path, "token": token, "payload": payload, "owner": owner}
        argv = ["/usr/bin/nsenter", "--target", str(before["pid"]), f"--net=/proc/self/fd/{fd}", "--",
                str(Path(sys.executable).resolve()), "-I", "-B", str(Path(__file__).resolve()), "--client"]
        raw = _capture(argv, deadline=deadline, input_bytes=_encoded(value), pass_fds=(fd,))
        require(_inspect(container_id, deadline) == before and _process_identity(before["pid"]) == owner)
        result = _parsed(raw)
        require(isinstance(result, dict) and set(result) == {"status", "body"}
                and type(result["status"]) is int and result["status"] in (200, 409)
                and isinstance(result["body"], dict) and time.monotonic() < deadline)
        return result["status"], result["body"]
    except (OSError, ValueError, KeyError, TypeError):
        raise NamespaceUncertain("sender namespace identity or response unconfirmed; query original operation only") from None
    finally:
        if fd is not None:
            os.close(fd)


def _http(value):
    _validate_request(value["port"], value["method"], value["path"], value["token"], value["payload"])
    owner = value["owner"]
    require(isinstance(owner, dict) and set(owner) == {"pid", "start_ticks", "network_device", "network_inode"}
            and all(type(number) is int and number >= 0 for number in owner.values())
            and owner["pid"] > 1 and owner["start_ticks"] > 0 and owner["network_inode"] > 0)
    own = Path("/proc/self/ns/net").stat()
    require((own.st_dev, own.st_ino) == (owner["network_device"], owner["network_inode"])
            and _process_identity(owner["pid"]) == owner)
    deadline = time.monotonic() + HTTP_SECONDS
    with _listener_owner(owner, value["port"], deadline) as guard:
        remaining = deadline - time.monotonic()
        require(remaining > 0)
        connection = http.client.HTTPConnection("127.0.0.1", value["port"], timeout=remaining)
        try:
            body = None if value["payload"] is None else _encoded(value["payload"])
            connection.request(value["method"], value["path"], body=body,
                               headers={"Authorization": "Bearer " + value["token"], "Content-Type": "application/json"})
            response, raw = connection.getresponse(), bytearray()
            while len(raw) <= MAX_HTTP:
                require(time.monotonic() < deadline)
                if connection.sock:
                    connection.sock.settimeout(max(0.001, deadline - time.monotonic()))
                part = response.read1(min(4096, MAX_HTTP + 1 - len(raw)))
                if not part:
                    break
                raw.extend(part)
            require(len(raw) <= MAX_HTTP and response.status in (200, 409) and time.monotonic() < deadline)
            result = {"status": response.status, "body": _parsed(raw)}
            require(isinstance(result["body"], dict) and len(_encoded(result)) <= MAX_OUTPUT)
            guard.confirm()
            return result
        finally:
            connection.close()


def main():
    try:
        require(sys.argv[1:] == ["--client"])
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        require(len(raw) <= MAX_INPUT)
        value = _parsed(raw)
        require(isinstance(value, dict) and set(value) == {"port", "method", "path", "token", "payload", "owner"})
        sys.stdout.buffer.write(_encoded(_http(value)))
        return 0
    except Exception:
        # A traceback could include private request data. Keep diagnostics fixed.
        sys.stderr.write("sender namespace client unconfirmed\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
