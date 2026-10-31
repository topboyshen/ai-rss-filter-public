"""Strict listener/PID-FD ownership; mocked kernel data is not native proof."""

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import socket
import stat
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sender_namespace_http as subject  # noqa: E402

OWNER = {"pid": 321, "start_ticks": 45, "network_device": 4, "network_inode": 12}
HEADER = b"sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"
LISTEN = b"0: 0100007F:22CE 00000000:0000 0A 00000000:00000000 00:00000000 00000000 65534 0 123 1\n"


@pytest.fixture
def case(monkeypatch):
    calls, closed = [], []
    row = SimpleNamespace(
        owner=deepcopy(OWNER),
        tables={"tcp": HEADER + LISTEN, "tcp6": HEADER},
        entries=["7"],
        links={"7": "socket:[123]"},
        calls=calls,
        closed=closed,
    )
    monkeypatch.setattr(subject, "_process_identity", lambda pid: deepcopy(row.owner))
    monkeypatch.setattr(
        subject.os,
        "pidfd_open",
        lambda pid, flags: calls.append(("pidfd", pid, flags)) or 702,
        raising=False,
    )
    monkeypatch.setattr(
        subject,
        "_duplicate_process_fd",
        lambda pidfd, fd: calls.append(("duplicate", pidfd, fd)) or 800,
    )
    monkeypatch.setattr(subject.os, "close", closed.append)
    monkeypatch.setattr(subject.os, "fstat", lambda fd: SimpleNamespace(st_ino=123))
    monkeypatch.setattr(subject.os, "readlink", lambda path: row.links[Path(path).name])

    @contextmanager
    def entries(path):
        assert path == "/proc/321/fd"
        yield [
            SimpleNamespace(name=name, path=path + "/" + name) for name in row.entries
        ]

    monkeypatch.setattr(subject.os, "scandir", entries)

    def table(guard, family):
        calls.append(("table", family))
        return row.tables[family]

    monkeypatch.setattr(subject._PinnedListener, "_read_table", table)

    class Listener:
        def __init__(self, fileno):
            assert fileno == 800
            row.listener = self
            self.closed = False

        def fileno(self):
            return 800

        def getsockname(self):
            return getattr(row, "endpoint", ("127.0.0.1", 8910))

        def getsockopt(self, level, option):
            assert level == socket.SOL_SOCKET
            return getattr(row, "options", {}).get(
                option,
                {
                    socket.SO_TYPE: socket.SOCK_STREAM,
                    socket.SO_ACCEPTCONN: 1,
                    socket.SO_REUSEPORT: 0,
                }[option],
            )

        def close(self):
            self.closed = True

    monkeypatch.setattr(subject.socket, "socket", Listener)
    row.guard = subject._listener_owner(deepcopy(OWNER), 8910, time.monotonic() + 2)
    return row


def test_listener_fd_is_held_across_operation_and_always_closed(case):
    p = case
    with p.guard as live:
        assert live is p.guard and not p.listener.closed and 702 not in p.closed
        live.confirm()
    assert p.listener.closed and p.closed == [702]
    assert [c for c in p.calls if c[0] == "duplicate"] == [("duplicate", 702, 7)]


def test_same_inode_dup_fds_are_not_multiple_listeners(case):
    case.entries += ["8"]
    case.links["8"] = "socket:[123]"
    with case.guard:
        pass
    assert case.listener.closed


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "two",
        "same_inode_twice",
        "wildcard",
        "other_address",
        "tcp6",
        "unknown_state",
        "zero_inode",
    ],
)
def test_ambiguous_or_unknown_listener_blocks_before_duplication(case, change):
    if change == "none":
        case.tables["tcp"] = HEADER
    elif change == "two":
        case.tables["tcp"] += LISTEN.replace(b"123", b"124")
    elif change == "same_inode_twice":
        case.tables["tcp"] += LISTEN
    elif change == "wildcard":
        case.tables["tcp"] = HEADER + LISTEN.replace(b"0100007F", b"00000000", 1)
    elif change == "other_address":
        case.tables["tcp"] = HEADER + LISTEN.replace(b"0100007F", b"0200007F", 1)
    elif change == "tcp6":
        case.tables["tcp6"] = HEADER + LISTEN.replace(b"0100007F", b"0" * 32).replace(
            b"00000000:0000", b"0" * 32 + b":0000"
        )
    elif change == "unknown_state":
        case.tables["tcp"] = HEADER + LISTEN.replace(b" 0A ", b" FF ")
    else:
        case.tables["tcp"] = HEADER + LISTEN.replace(b" 123 ", b" 0 ")
    with pytest.raises(subject.NamespaceUncertain):
        case.guard.__enter__()
    assert not [c for c in case.calls if c[0] == "duplicate"] and case.closed == [702]


def test_established_connection_with_same_local_port_is_not_a_second_listener(case):
    case.tables["tcp"] += LISTEN.replace(b" 0A ", b" 01 ").replace(b" 123 ", b" 124 ")
    with case.guard:
        pass


@pytest.mark.parametrize(
    "kind", ["wrong_pid", "fd_cap", "invalid_name", "missing_owner"]
)
def test_process_and_fd_inventory_fail_closed(case, kind):
    if kind == "wrong_pid":
        case.owner["start_ticks"] += 1
    elif kind == "fd_cap":
        case.entries = [str(n) for n in range(257)]
        case.links = {name: "socket:[123]" for name in case.entries}
    elif kind == "invalid_name":
        case.entries = ["../7"]
    else:
        case.links["7"] = "socket:[999]"
    with pytest.raises(subject.NamespaceUncertain):
        case.guard.__enter__()
    assert not [c for c in case.calls if c[0] == "duplicate"]


@pytest.mark.parametrize(
    "kind",
    ["start", "netns", "fd", "listener", "endpoint", "acceptconn", "type", "reuseport"],
)
def test_post_operation_identity_and_fd_drift_are_unknown(case, kind):
    with pytest.raises(subject.NamespaceUncertain):
        with case.guard as guard:
            if kind == "start":
                case.owner["start_ticks"] += 1
            elif kind == "netns":
                case.owner["network_inode"] += 1
            elif kind == "fd":
                case.links["7"] = "socket:[999]"
            elif kind == "listener":
                case.tables["tcp"] = HEADER + LISTEN.replace(b"123", b"124")
            elif kind == "endpoint":
                case.endpoint = ("0.0.0.0", 8910)
            else:
                option = {
                    "acceptconn": socket.SO_ACCEPTCONN,
                    "type": socket.SO_TYPE,
                    "reuseport": socket.SO_REUSEPORT,
                }[kind]
                case.options = {
                    option: {
                        "acceptconn": 0,
                        "type": socket.SOCK_DGRAM,
                        "reuseport": 1,
                    }[kind]
                }
            guard.confirm()
    assert case.listener.closed and case.closed == [702]


def test_pidfd_getfd_permission_failure_is_not_a_fallback(case, monkeypatch):
    monkeypatch.setattr(
        subject,
        "_duplicate_process_fd",
        lambda *args: (_ for _ in ()).throw(PermissionError("synthetic")),
    )
    with pytest.raises(PermissionError):
        case.guard.__enter__()
    assert case.closed == [702] and not hasattr(case, "listener")


def test_guard_never_resets_deadline(case):
    case.guard.deadline = time.monotonic() - 1
    with pytest.raises(subject.NamespaceUncertain):
        case.guard.__enter__()
    assert case.calls == []


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
def test_pidfd_getfd_has_explicit_known_linux_syscall(machine, monkeypatch):
    function = Mock(return_value=800)
    monkeypatch.setattr(
        subject.os, "uname", lambda: SimpleNamespace(sysname="Linux", machine=machine)
    )
    monkeypatch.setattr(
        subject.ctypes,
        "CDLL",
        lambda *args, **kwargs: SimpleNamespace(syscall=function),
    )
    assert subject._duplicate_process_fd(702, 7) == 800
    function.assert_called_once_with(438, 702, 7, 0)


@pytest.mark.parametrize(
    "system,machine,rc",
    [("Darwin", "aarch64", 800), ("Linux", "unknown", 800), ("Linux", "aarch64", -1)],
)
def test_unsupported_or_refused_pidfd_getfd_is_unknown(
    system, machine, rc, monkeypatch
):
    function = Mock(return_value=rc)
    monkeypatch.setattr(
        subject.os, "uname", lambda: SimpleNamespace(sysname=system, machine=machine)
    )
    monkeypatch.setattr(
        subject.ctypes,
        "CDLL",
        lambda *args, **kwargs: SimpleNamespace(syscall=function),
    )
    with pytest.raises(subject.NamespaceUncertain):
        subject._duplicate_process_fd(702, 7)


def metadata(**changes):
    fields = {
        "st_dev": 4,
        "st_ino": 5,
        "st_mode": stat.S_IFREG | 0o444,
        "st_uid": 0,
        "st_gid": 0,
        "st_nlink": 1,
        "st_size": 0,
        "st_mtime_ns": 100,
        "st_ctime_ns": 100,
    }
    fields.update(changes)
    return SimpleNamespace(**fields)


@pytest.fixture
def proc_case(monkeypatch):
    p = SimpleNamespace(
        raw=HEADER + LISTEN,
        before=metadata(),
        opened=metadata(st_mtime_ns=101),
        after=metadata(st_mtime_ns=102),
        fs=subject.PROC_SUPER_MAGIC,
        closes=[],
        opens=[],
    )
    monkeypatch.setattr(subject, "_process_identity", lambda pid: deepcopy(OWNER))
    monkeypatch.setattr(subject.os, "lstat", lambda path: p.before)
    fstats = iter([p.opened, p.after])
    monkeypatch.setattr(subject.os, "fstat", lambda fd: next(fstats))
    monkeypatch.setattr(
        subject.os, "open", lambda path, flags: p.opens.append((path, flags)) or 7
    )
    monkeypatch.setattr(subject.os, "close", p.closes.append)
    monkeypatch.setattr(subject, "_filesystem_type", lambda fd: p.fs)

    def read(fd, size):
        assert fd == 7 and 0 < size <= 4096
        part, p.raw = p.raw[:size], p.raw[size:]
        return part

    monkeypatch.setattr(subject.os, "read", read)
    p.guard = subject._listener_owner(deepcopy(OWNER), 8910, time.monotonic() + 2)
    return p


def test_typed_procfs_timestamps_are_not_used_as_identity(proc_case):
    assert proc_case.guard._read_table("tcp") == HEADER + LISTEN
    assert proc_case.closes == [7]
    assert proc_case.opens[0][0] == "/proc/self/net/tcp"


@pytest.mark.parametrize(
    "kind", ["fs", "uid", "mode", "nlink", "size", "inode", "limit"]
)
def test_typed_procfs_keeps_fixed_identity_and_original_read_budgets(
    proc_case, monkeypatch, kind
):
    p = proc_case
    if kind == "fs":
        p.fs = 0xEF53
    elif kind == "uid":
        p.before = metadata(st_uid=1)
    elif kind == "mode":
        p.before = metadata(st_mode=stat.S_IFREG | 0o644)
    elif kind == "nlink":
        p.before = metadata(st_nlink=2)
    elif kind == "size":
        p.before = metadata(st_size=1)
    elif kind == "inode":
        p.opened = metadata(
            st_ino=6
        )  # Replace the iterator's referenced object fields.
    else:
        p.guard.read_bytes = subject.MAX_PROC_TOTAL
    if kind == "inode":
        monkeypatch.setattr(subject.os, "fstat", lambda fd: p.opened)
    with pytest.raises(subject.NamespaceUncertain):
        p.guard._read_table("tcp")


def test_reading_unknown_procfs_family_does_not_open(proc_case):
    with pytest.raises(subject.NamespaceUncertain):
        proc_case.guard._read_table("udp")
    assert proc_case.opens == []
