"""Fixed bounded HTTP client inside one inspected sender network namespace.

No shell, sudo, published port, mount namespace or disposable client container.
Secrets travel only on stdin. A changed identity after HTTP is an unknown result
and never permission to repeat a mutation.
"""
from __future__ import annotations

import ctypes
import http.client
import json
import math
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


_CHILD_KINDS = frozenset({"NamespaceUncertain", "OSError", "TimeoutError", "ConnectionError",
                         "JSONDecodeError", "ValueError", "KeyError", "TypeError", "other"})
_CAPTURE_STAGES = frozenset({"spawn", "pipe_select", "stdin_write", "stdout_read", "stderr_read",
                           "output_bound", "wait_exit", "exit_and_deadline"})


def _diagnostic_codes():
    """Only positions in this exact loaded source can appear in a child report."""
    result = {name: globals()[name].__code__ for name in
              ("main", "_http", "_validate_request", "_parsed", "_encoded", "_process_identity",
               "_filesystem_type", "_duplicate_process_fd")}
    result.update({"_PinnedListener." + name: getattr(_PinnedListener, name).__code__ for name in
                   ("_window", "_read_table", "_table_listener", "_owned_fds", "confirm", "__enter__", "__exit__")})
    return result


def _child_diagnostic(error):
    codes = _diagnostic_codes()
    location = None
    current = error.__traceback__
    while current is not None:
        for name, code in codes.items():
            if current.tb_frame.f_code is code:
                location = {"function": name, "line": current.tb_lineno}
        current = current.tb_next
    kind = ("NamespaceUncertain" if isinstance(error, NamespaceUncertain) else
            "TimeoutError" if isinstance(error, TimeoutError) else
            "ConnectionError" if isinstance(error, ConnectionError) else
            "OSError" if isinstance(error, OSError) else
            "JSONDecodeError" if isinstance(error, json.JSONDecodeError) else
            type(error).__name__ if type(error) in (ValueError, KeyError, TypeError) else "other")
    number = error.errno if isinstance(error, OSError) else None
    number = number if type(number) is int and 0 < number <= 4095 else None
    return {"schema": 1, "stage": "unclassified" if location is None else location["function"],
            "kind": kind, "errno": number, "location": location}


def _safe_child_diagnostic(value):
    if not isinstance(value, dict) or set(value) != {"schema", "stage", "kind", "errno", "location"}:
        return None
    if (type(value["schema"]) is not int or value["schema"] != 1
            or not isinstance(value["kind"], str) or value["kind"] not in _CHILD_KINDS):
        return None
    number, location = value["errno"], value["location"]
    if number is not None and (type(number) is not int or not 0 < number <= 4095):
        return None
    if number is not None and value["kind"] not in {"OSError", "TimeoutError", "ConnectionError"}:
        return None
    if location is not None:
        if not isinstance(location, dict) or set(location) != {"function", "line"}:
            return None
        code = _diagnostic_codes().get(location["function"]) if isinstance(location["function"], str) else None
        if code is None or type(location["line"]) is not int or location["line"] not in {row[2] for row in code.co_lines()}:
            return None
    if value["stage"] != ("unclassified" if location is None else location["function"]):
        return None
    return {"schema": 1, "stage": value["stage"], "kind": value["kind"], "errno": number,
            "location": None if location is None else dict(location)}


def safe_namespace_diagnostic(value):
    """Strict allowlist: never serialize stderr, argv, request, headers or exception text."""
    keys = {"schema", "operation", "stage", "capture_elapsed_seconds", "capture_budget_seconds",
            "remaining_seconds", "deadline_expired", "returncode", "stdout_bytes", "stderr_bytes",
            "stdout_eof", "stderr_eof", "stdin_complete", "child_status", "child"}
    if not isinstance(value, dict) or set(value) != keys:
        return None
    if (type(value["schema"]) is not int or value["schema"] != 1
            or not isinstance(value["operation"], str) or value["operation"] not in {"docker_inspect", "namespace_http", "other"}
            or not isinstance(value["stage"], str) or value["stage"] not in _CAPTURE_STAGES):
        return None
    for key in ("capture_elapsed_seconds", "capture_budget_seconds", "remaining_seconds"):
        # Elapsed may record an overrun; budget/remaining can never grant more
        # than the unchanged TOTAL_SECONDS=3 (inner HTTP_SECONDS remains 2).
        ceiling = 60 if key == "capture_elapsed_seconds" else TOTAL_SECONDS
        if type(value[key]) not in (int, float) or not math.isfinite(value[key]) or not 0 <= value[key] <= ceiling:
            return None
    if value["returncode"] is not None and (type(value["returncode"]) is not int or not -64 <= value["returncode"] <= 255):
        return None
    if any(type(value[key]) is not int or not 0 <= value[key] <= 1024**2 + 1 for key in ("stdout_bytes", "stderr_bytes")):
        return None
    if any(type(value[key]) is not bool for key in ("deadline_expired", "stdout_eof", "stderr_eof", "stdin_complete")):
        return None
    if not isinstance(value["child_status"], str) or value["child_status"] not in {"absent", "valid", "invalid", "truncated"}:
        return None
    child = _safe_child_diagnostic(value["child"]) if value["child"] is not None else None
    if (value["child_status"] == "valid") != (child is not None) or (value["child"] is not None and child is None):
        return None
    if child is not None and (value["operation"] != "namespace_http" or not value["stderr_eof"] or value["returncode"] != 3):
        return None
    if (value["stdout_bytes"] + value["stderr_bytes"] > 1024**2 + 1
            or (value["child_status"] == "absent") != (value["stderr_bytes"] == 0)
            or (value["child_status"] == "truncated") != (value["stderr_bytes"] > 4096)
            or value["remaining_seconds"] > value["capture_budget_seconds"]
            or value["deadline_expired"] != (value["remaining_seconds"] == 0)):
        return None
    return {**value, "child": child}


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


def _capture(argv, *, deadline, input_bytes=None, max_bytes=MAX_OUTPUT, pass_fds=(), operation="other"):
    require(input_bytes is None or len(input_bytes) <= MAX_INPUT)
    require(time.monotonic() < deadline)
    started, stage = time.monotonic(), "spawn"
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, start_new_session=True,
            pass_fds=pass_fds, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, cwd="/")
    except OSError:
        raise NamespaceUncertain("sender namespace client unavailable") from None
    output, total, sent = bytearray(), 0, 0
    # A diagnostic prefix is retained inside, not in addition to, MAX_OUTPUT.
    # Counts describe pipe reads; a rejected output is never a successful result.
    diagnostic_stderr, counts, eof = bytearray(), {"stdout": 0, "stderr": 0}, {"stdout": False, "stderr": False}

    def observation():
        now = time.monotonic()
        child, child_status = None, "absent"
        if counts["stderr"] > 4096:
            child_status = "truncated"
        elif diagnostic_stderr:
            child_status = "invalid"
            try:
                if operation == "namespace_http" and eof["stderr"] and process.poll() == 3:
                    child = _safe_child_diagnostic(_parsed(bytes(diagnostic_stderr)))
            except (ValueError, TypeError, NamespaceUncertain):
                pass
            if child is not None:
                child_status = "valid"
        value = {"schema": 1, "operation": operation, "stage": stage,
                 "capture_elapsed_seconds": max(0.0, now - started), "capture_budget_seconds": max(0.0, deadline - started),
                 "remaining_seconds": max(0.0, deadline - now), "deadline_expired": now >= deadline,
                 "returncode": process.poll(), "stdout_bytes": counts["stdout"], "stderr_bytes": counts["stderr"],
                 "stdout_eof": eof["stdout"], "stderr_eof": eof["stderr"],
                 "stdin_complete": input_bytes is None or sent == len(input_bytes), "child_status": child_status, "child": child}
        return safe_namespace_diagnostic(value)

    def annotate(error):
        try:
            error.namespace_diagnostic = observation()
        except Exception:
            # Observation failure never hides or changes the primary refusal.
            error.namespace_diagnostic = None

    try:
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            if process.stdin is not None:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE)
            while selector.get_map():
                stage = "pipe_select"
                remaining = deadline - time.monotonic()
                require(remaining > 0)
                for key, _mask in selector.select(min(0.2, remaining)):
                    if key.fileobj is process.stdin:
                        stage = "stdin_write"
                        try:
                            sent += os.write(key.fd, input_bytes[sent:])
                        except BlockingIOError:
                            continue
                        if sent == len(input_bytes):
                            selector.unregister(key.fileobj)
                            process.stdin.close()
                        continue
                    try:
                        stream_name = "stdout" if key.fileobj is process.stdout else "stderr"
                        stage = stream_name + "_read"
                        chunk = os.read(key.fd, min(65536, max_bytes + 1 - total))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        eof[stream_name] = True
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    counts[stream_name] += len(chunk)
                    if stream_name == "stderr":
                        diagnostic_stderr.extend(chunk[:max(0, 4096 - len(diagnostic_stderr))])
                    stage = "output_bound"
                    require(total <= max_bytes)
                    if key.fileobj is process.stdout:
                        output.extend(chunk)
            stage = "wait_exit"
            returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            stage = "exit_and_deadline"
            require(returncode == 0 and time.monotonic() < deadline)
        return bytes(output)
    except NamespaceUncertain as error:
        annotate(error)
        raise
    except (OSError, ValueError, subprocess.TimeoutExpired):
        error = NamespaceUncertain("sender namespace request unconfirmed; query original operation only")
        annotate(error)
        raise error from None
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)  # Only our local nsenter/client process group.
        process.wait(timeout=1)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


def _inspect(container_id, deadline):
    raw = _capture(["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", "inspect", container_id],
                   deadline=deadline, max_bytes=1024**2, operation="docker_inspect")
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
        raw = _capture(argv, deadline=deadline, input_bytes=_encoded(value), pass_fds=(fd,), operation="namespace_http")
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
    except Exception as error:
        # A traceback could include private request data. Keep diagnostics fixed.
        try:
            message = _encoded(_child_diagnostic(error)).decode("ascii")
        except Exception:
            message = "sender namespace client unconfirmed"
        try:
            sys.stderr.write(message + "\n")
        except Exception:
            pass  # Even an unavailable diagnostic stream cannot change rc=3.
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
