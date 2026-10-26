"""Bounded raw storage recheck, not a release permit or a recovery command.

No HTTP receipt recovery, ReceiptStore construction, chmod, cleanup, model,
queue consumer or journal retry is used. The caller must first stop producers
and freeze application activity. ``locked_inventory`` keeps the *actual*
spool/journal locks until its caller leaves the context; a returned old sample
cannot authorize a later switch. Unknown files and incomplete writes fail shut.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

ROOT = Path(__file__).resolve().parents[1]
HEX = re.compile(r"[0-9a-f]{64}\Z")
MAX_FILES = 24000
MAX_BYTES = 272 * 1024**2
MAX_SINGLE = 8 * 1024**2
META_KEYS = ("dashboard_realtime_input_revision", "dashboard_realtime_published_revision",
             "dashboard_realtime_published_at")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


capacity = _load("independent_capacity", ROOT / "scripts/observe_handoff_capacity.py")
continuations = _load("independent_continuations", ROOT / "services/natural_score_continuations.py")


class InventoryError(Exception):
    """No zero proof is available. Original storage must be retained."""


def require(value, reason):
    if not value:
        raise InventoryError(reason)


def _identity(info):
    return {"device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid}


def directory_identity(path):
    info = Path(path).lstat()
    require(stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
            and info.st_uid == os.getuid(), "unsafe inventory directory")
    return _identity(info)


class Reader:
    def __init__(self, seconds):
        require(type(seconds) is int and 1 <= seconds <= 10, "invalid inventory deadline")
        self.deadline, self.files, self.bytes = time.monotonic() + seconds, 0, 0
        self.fingerprints = {}

    def tick(self):
        require(time.monotonic() <= self.deadline, "inventory deadline")
        self.files += 1
        require(self.files <= MAX_FILES, "inventory file budget")

    def entries(self, path):
        directory_identity(path)
        with os.scandir(path) as items:
            result = []
            for entry in items:
                self.tick()
                result.append(Path(entry.path))
        return result

    def read(self, path, limit=MAX_SINGLE):
        self.tick()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                    and before.st_uid == os.getuid() and stat.S_IMODE(before.st_mode) == 0o600
                    and before.st_size <= limit, "unsafe inventory file")
            raw = b""
            while len(raw) <= limit:
                part = os.read(fd, min(65536, limit + 1 - len(raw)))
                if not part:
                    break
                raw += part
                self.tick()
            after = os.fstat(fd)
            def signature(info):
                return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            require(signature(before) == signature(after) == signature(Path(path).lstat())
                    and len(raw) == before.st_size, "inventory file changed")
        finally:
            os.close(fd)
        self.bytes += len(raw)
        require(self.bytes <= MAX_BYTES, "inventory byte budget")
        self.fingerprints[str(path)] = hashlib.sha256(raw).hexdigest()
        return raw

    def json(self, path, limit=MAX_SINGLE):
        def pairs(items):
            value = {}
            for key, item in items:
                require(key not in value, "duplicate inventory JSON key")
                value[key] = item
            return value
        return json.loads(self.read(path, limit), object_pairs_hook=pairs,
                          parse_constant=lambda _: require(False, "nonfinite inventory JSON"))


@contextmanager
def existing_lock(path):
    """Never create a missing lock and never infer emptiness from lock absence."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size == 0,
                "unsafe inventory lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(_identity(info) == _identity(Path(path).lstat()), "inventory lock replaced")
        yield fd
        require(_identity(info) == _identity(Path(path).lstat()), "inventory lock replaced")
    finally:
        os.close(fd)


def _queue(reader, spool):
    counts = {"queue_pending": 0, "queue_inflight": 0, "terminal_pending": 0,
              "archive_pending": 0, "retained_dead_letters": 0}
    for path in reader.entries(spool):
        if path.name in {"inflight", "dead-letter", "receipts"}:
            directory_identity(path)
        elif path.name == ".lock":
            require(reader.read(path) == b"", "nonempty spool lock")
        elif path.name == ".completed-digests":
            # Legacy completion records do not prove a raw delivery receipt.
            raw = reader.json(path)
            require(isinstance(raw, (dict, list)), "invalid legacy completion evidence")
        elif path.suffix == ".json" and HEX.fullmatch(path.stem):
            value = _envelope(reader, path)
            counts["queue_pending"] += 1
            counts["archive_pending"] += int("archive_pending" in value)
        else:
            raise InventoryError("unknown or incomplete spool artifact")
    for path in reader.entries(spool / "inflight"):
        require(path.suffix == ".json" and HEX.fullmatch(path.stem), "unknown inflight artifact")
        value = _envelope(reader, path)
        counts["queue_inflight"] += 1
        counts["archive_pending"] += int("archive_pending" in value)
    for path in reader.entries(spool / "dead-letter"):
        require(re.fullmatch(r"[0-9a-f]{64}\.[0-9]+\.(terminal-pending|terminal)\.json", path.name)
                or re.fullmatch(r"[0-9a-f]{64}\.[0-9]+\.json", path.name), "unknown dead-letter artifact")
        value = _envelope(reader, path, digest=path.name.split(".")[0])
        pending = path.name.endswith(".terminal-pending.json")
        counts["terminal_pending"] += int(pending)
        counts["retained_dead_letters"] += int(not pending)
        counts["archive_pending"] += int(pending and "archive_pending" in value)
    return counts


def _envelope(reader, path, digest=None):
    value = reader.json(path)
    task = value.get("task") if isinstance(value, dict) else None
    require(isinstance(task, dict) and value.get("version") == 1, "invalid queue envelope")
    raw = json.dumps(task, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    actual = hashlib.sha256(raw).hexdigest()
    require(value.get("digest") == actual == (digest or path.stem), "queue task identity mismatch")
    require(type(value.get("attempts")) is int and 0 <= value["attempts"] <= 1000,
            "invalid queue budget")
    return value


def _journal(reader, path):
    states = {key: 0 for key in continuations.ACTIVE_STATES | continuations.TERMINAL_STATES}
    locks = []
    for item in reader.entries(path):
        if item.name == ".lock":
            require(reader.read(item) == b"", "nonempty journal lock")
        elif item.name == ".locks":
            for lock in reader.entries(item):
                require(lock.suffix == ".lock" and HEX.fullmatch(lock.stem), "unknown journal identity lock")
                locks.append(lock)
        else:
            require(item.suffix == ".json" and HEX.fullmatch(item.stem), "unknown journal artifact")
            value = continuations._validate(reader.json(item, continuations.MAX_RECORD_BYTES), key=item.stem)
            states[value["state"]] += 1
    require(sum(states.values()) <= continuations.MAX_RECORDS and len(locks) <= continuations.MAX_RECORDS,
            "journal hard capacity")
    return states, locks


def _publication(reader, path, expected):
    info = Path(path).lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and info.st_nlink == 1 and _identity(info) == expected, "database identity mismatch")
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", timeout=0, uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        sql_deadline = min(reader.deadline, time.monotonic() + 3)
        connection.set_progress_handler(lambda: int(time.monotonic() > sql_deadline), 1000)
        connection.execute("BEGIN")
        rows = connection.execute("SELECT k,v FROM app_meta WHERE k IN (?,?,?)", META_KEYS).fetchmany(4)
        require(len(rows) == 3 and {r[0] for r in rows} == set(META_KEYS), "publication metadata missing")
        values = {}
        for key, value in rows:
            require(isinstance(value, str) and value.isascii() and value.isdecimal(), "invalid publication counter")
            values[key] = int(value)
        require(_identity(Path(path).lstat()) == expected, "database changed identity")
        return {"input": values[META_KEYS[0]], "published": values[META_KEYS[1]],
                "published_at": values[META_KEYS[2]], "read_only": True}
    finally:
        connection.close()


def sender_blockers(sender):
    """Shared-capacity evidence is necessary; retained held is not drained."""
    if sender is None:
        return ["postgres_not_observed"]
    capacity.validate_sender_sample(sender)
    blockers = []
    if sender["states"]["pending"] or sender["states"]["inflight"]:
        blockers.append("sender_unfinished")
    if sender["states"]["held"]:
        blockers.append("sender_held_retained")
    if sender["legacy"]["rows"]:
        blockers.append("sender_legacy_held")
    return blockers


@contextmanager
def locked_inventory(spec, *, postgres=None, seconds=5):
    """Acquire real storage locks, independently read, and retain locks in yield.

    ``postgres`` must be an actual bound DB sampler in the combined driver;
    absent PG proof blocks switching rather than substituting synthetic zeros.
    This API alone can NEVER authorize switching or opening admission.
    """
    reader = Reader(seconds)
    require(isinstance(spec, dict) and set(spec) == {"spool", "journal", "database", "identities"},
            "invalid inventory specification")
    spool, journal, database = (Path(spec[k]) for k in ("spool", "journal", "database"))
    identities = spec["identities"]
    require(set(identities) == {"spool", "journal", "database"}, "missing inventory identities")
    require(directory_identity(spool) == identities["spool"]
            and directory_identity(journal) == identities["journal"], "storage identity changed")
    with ExitStack() as stack:
        lock_identities = {}
        def retain(path):
            fd = stack.enter_context(existing_lock(path))
            lock_identities[str(path)] = _identity(os.fstat(fd))
        retain(spool / ".lock")
        retain(journal / ".lock")
        # First discover identity locks, then reread every record while holding
        # them. A previous terminal/empty view is not a post-lock zero proof.
        _, locks = _journal(reader, journal)
        for path in sorted(locks):
            retain(path)
        states, again = _journal(reader, journal)
        require(sorted(again) == sorted(locks), "journal lock inventory changed")
        counts = _queue(reader, spool)
        receiver = capacity._receiver_locked(spool, identities["spool"], reader.deadline, [0], [0])
        publication = _publication(reader, database, identities["database"])
        sender = postgres() if postgres is not None else None
        blockers = [key for key, count in counts.items() if count and key != "retained_dead_letters"]
        blockers += ["journal_" + key for key in continuations.ACTIVE_STATES if states[key]]
        if receiver["states"]["accepted"]:
            blockers.append("uncompleted_receipts")
        if publication["input"] != publication["published"]:
            blockers.append("publication_not_caught_up")
        blockers += sender_blockers(sender)
        reader.tick()
        result = {"schema": 1, "counts": counts, "journal": states, "receiver": receiver,
                  "publication": publication, "sender": sender, "blockers": sorted(blockers),
                  "storage_drained": not blockers, "locks_held": True, "switch_authorized": False,
                  "lock_identities": lock_identities,
                  "files_inspected": reader.files, "bytes_read": reader.bytes,
                  "evidence_sha256": hashlib.sha256(json.dumps(reader.fingerprints, sort_keys=True).encode()).hexdigest()}
        yield result
        require(directory_identity(spool) == identities["spool"]
                and directory_identity(journal) == identities["journal"], "storage identity changed")
