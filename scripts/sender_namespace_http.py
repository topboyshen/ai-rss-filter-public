"""Fixed bounded HTTP client inside one inspected sender network namespace.

No shell, sudo, published port, mount namespace or disposable client container.
Secrets travel only on stdin. A changed identity after HTTP is an unknown result
and never permission to repeat a mutation.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time

HEX64 = re.compile(r"[0-9a-f]{64}\Z")
MAX_INPUT = 16384
MAX_HTTP = 65536
MAX_OUTPUT = 131072
TOTAL_SECONDS = 3
HTTP_SECONDS = 2


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
    connection = http.client.HTTPConnection("127.0.0.1", value["port"], timeout=HTTP_SECONDS)
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
