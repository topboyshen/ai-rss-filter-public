"""Strict webhook receipts. Every call requires the existing spool flock.

There is deliberately no second lock and no automatic eviction. The caller
owns normalization, authentication and queue transitions. A visible JSON file
is not a successful receipt until its file and namespace barriers complete.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import stat
import time
from pathlib import Path

MAX_RECORDS = 4096
MAX_BYTES = 256 * 1024 * 1024
MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_COMPLETION_BYTES = 16 * 1024
_READ_VERIFY_CHUNK = 64 * 1024
_READ_IDENTITY = ("st_dev", "st_ino", "st_size", "st_mode", "st_uid", "st_nlink", "st_mtime_ns", "st_ctime_ns")
_INDEX_RESERVE = 2048
# A new alias must have this much admission headroom before its first intent
# write. A truncated intent cannot prove its actual size/identity, so charge a
# full bounded slot until a complete immutable context is available.
MAX_ALIAS_RESERVATION = 3 * (MAX_RECORD_BYTES + MAX_COMPLETION_BYTES + 2048)
_HEAD_KEYS = ("_reserved_bytes", "delivery_id", "task_digest", "primary_delivery_id")
_STATUSES = {"accepted", "completed", "terminal"}


class ReceiptError(Exception):
    """Fail closed; caller must not acknowledge a durable receipt."""


class ReceiptConflict(ReceiptError):
    pass


class ReceiptCapacityError(ReceiptError):
    pass


class ReceiptCorrupt(ReceiptError):
    pass


class ReceiptMalformedJSON(ReceiptCorrupt):
    """Only a parse failure can be considered for proven partial isolation."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _identifier(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ReceiptCorrupt("invalid receipt identifier")
    return value


def _private_directory(path):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.getuid()):
        raise ReceiptCorrupt("unsafe receipt directory")


def _open_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_size > MAX_RECORD_BYTES):
            raise ReceiptCorrupt("unsafe receipt file")
    except BaseException as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        if isinstance(exc, OSError):
            raise ReceiptCorrupt("receipt metadata unavailable after open") from exc
        raise
    return fd


def _verify_read(fd, data):
    # Equal timestamps do not imply equal content. Read the same descriptor
    # afresh: seeking a BufferedReader can reuse its stale cached bytes.
    expected = memoryview(data)
    for offset in range(0, len(data), _READ_VERIFY_CHUNK):
        size = min(_READ_VERIFY_CHUNK, len(data) - offset)
        if os.pread(fd, size, offset) != expected[offset:offset + size]:
            raise ReceiptCorrupt("receipt changed during bounded read")
    if os.pread(fd, 1, len(data)):
        raise ReceiptCorrupt("receipt changed during bounded read")


def _read(path):
    # Only failure of the initial open may mean "absent" to callers. Once a
    # descriptor exists, any I/O/metadata failure must fail closed, not become
    # a new admission or a proven partial-JSON hold.
    fd = _open_private(path)
    try:
        stream = os.fdopen(fd, "rb")
    except BaseException as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        if isinstance(exc, OSError):
            raise ReceiptCorrupt("receipt stream unavailable after open") from exc
        raise
    try:
        with stream:
            before = os.fstat(stream.fileno())
            if before.st_size > MAX_RECORD_BYTES:
                raise ReceiptCorrupt("receipt exceeds per-file safety limit")
            # The store budget is not a per-read allocation. The second pass
            # adds at most one fixed-size chunk, not another complete record.
            data = stream.read(before.st_size + 1)
            after = os.fstat(stream.fileno())
            if len(data) != before.st_size or any(
                getattr(before, key) != getattr(after, key) for key in _READ_IDENTITY
            ):
                raise ReceiptCorrupt("receipt changed during bounded read")
            _verify_read(stream.fileno(), data)
            final = os.fstat(stream.fileno())
            if any(getattr(before, key) != getattr(final, key) for key in _READ_IDENTITY):
                raise ReceiptCorrupt("receipt changed during bounded read")
    except OSError as exc:
        raise ReceiptCorrupt("receipt I/O failed during bounded read") from exc
    return data


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _decode(data):
    try:
        return json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise ReceiptMalformedJSON("invalid receipt JSON") from exc


def _encode_record(record):
    # Bounded first line allows capacity/index recovery without reading bodies.
    head = {key: record[key] for key in _HEAD_KEYS}
    tail = {key: value for key, value in record.items() if key not in _HEAD_KEYS}
    return _json(head)[:-1] + b",\n" + _json(tail)[1:]


def _reservation(record):
    accepted = {key: value for key, value in record.items() if key != "completion"}
    accepted.update(status="accepted", _reserved_bytes=MAX_BYTES)
    slot = len(_encode_record(accepted)) + MAX_COMPLETION_BYTES + 2048
    copies = 3 if record.get("alias_intent_schema") == 1 else 2
    return copies * slot + (_INDEX_RESERVE if record["primary_delivery_id"] == record["delivery_id"] else 0)


def _head(path):
    with os.fdopen(_open_private(path), "rb") as stream:
        line = stream.readline(1024)
        size = os.fstat(stream.fileno()).st_size
    if not line.endswith(b",\n"):
        raise ReceiptCorrupt("invalid receipt reservation header")
    head = _decode(line[:-2] + b"}")
    if not isinstance(head, dict) or set(head) != set(_HEAD_KEYS):
        raise ReceiptCorrupt("invalid receipt reservation header")
    for key in _HEAD_KEYS[1:]:
        _identifier(head[key])
    reserved = head["_reserved_bytes"]
    index = _INDEX_RESERVE if head["primary_delivery_id"] == head["delivery_id"] else 0
    if type(reserved) is not int or not 0 < reserved <= MAX_BYTES or size * 2 + index > reserved:
        raise ReceiptCorrupt("invalid receipt reservation")
    return head


def _completion(value):
    if value is not None and not isinstance(value, dict):
        raise ReceiptCorrupt("completion must be an object")
    try:
        if len(_json(value)) > MAX_COMPLETION_BYTES:
            raise ReceiptCapacityError("completion exceeds reserved limit")
    except (TypeError, ValueError) as exc:
        raise ReceiptCorrupt("invalid completion") from exc
    def reject_credentials(item):
        if isinstance(item, dict):
            if any(str(key).lower() in {"secret", "signature", "api_key", "authorization"} for key in item):
                raise ReceiptCorrupt("credentials are not receipt metadata")
            for nested in item.values():
                reject_credentials(nested)
        elif isinstance(item, list):
            for nested in item:
                reject_credentials(nested)
    reject_credentials(value)


def _validate(record, delivery_id):
    if not isinstance(record, dict):
        raise ReceiptCorrupt("receipt must be an object")
    if type(record.get("schema")) is not int or record["schema"] != 1 or record.get("durable") is not True:
        raise ReceiptCorrupt("invalid receipt schema")
    if record.get("delivery_id") != delivery_id or record.get("body_sha256") != delivery_id:
        raise ReceiptConflict("receipt body identity mismatch")
    _identifier(record.get("task_digest"))
    _identifier(record.get("primary_delivery_id"))
    if "alias_intent_schema" in record:
        if (type(record["alias_intent_schema"]) is not int or record["alias_intent_schema"] != 1
                or record["primary_delivery_id"] == delivery_id):
            raise ReceiptCorrupt("invalid alias intent schema")
    try:
        body = base64.b64decode(record["raw_body_b64"], validate=True)
        task_bytes = _json(record["task"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReceiptCorrupt("invalid stored receipt body or task") from exc
    if not body or not isinstance(record["task"], dict):
        raise ReceiptCorrupt("invalid stored receipt body or task")
    if hashlib.sha256(body).hexdigest() != delivery_id or hashlib.sha256(task_bytes).hexdigest() != record["task_digest"]:
        raise ReceiptConflict("stored receipt identity mismatch")
    for key in ("accepted_strategy", "accepted_revision"):
        if not isinstance(record.get(key), str) or not 0 < len(record[key]) <= 256:
            raise ReceiptCorrupt("invalid acceptance identity")
    moment = record.get("accepted_at")
    if type(moment) not in (int, float) or not math.isfinite(moment) or moment < 0:
        raise ReceiptCorrupt("invalid acceptance timestamp")
    if not isinstance(record.get("status"), str) or record["status"] not in _STATUSES:
        raise ReceiptCorrupt("invalid receipt status")
    _completion(record.get("completion"))
    if "_reserved_bytes" in record and record["_reserved_bytes"] != _reservation(record):
        raise ReceiptCorrupt("receipt reservation was changed")
    return record


def public_record(record):
    """Serialize a record already validated/barrier-confirmed by this store."""
    return {key: value for key, value in record.items()
            if key not in {"raw_body_b64", "task", "_reserved_bytes", "alias_intent_schema"}}


def accepted_record(raw_body, task, *, strategy, revision, accepted_at, primary=None):
    """Build the original identity from a separately verified queue context."""
    delivery_id = hashlib.sha256(raw_body).hexdigest()
    record = {"schema": 1, "durable": True, "delivery_id": delivery_id, "body_sha256": delivery_id,
              "task_digest": hashlib.sha256(_json(task)).hexdigest(),
              "primary_delivery_id": primary["delivery_id"] if primary else delivery_id,
              "accepted_strategy": primary["accepted_strategy"] if primary else strategy,
              "accepted_revision": primary["accepted_revision"] if primary else revision,
              "accepted_at": accepted_at, "status": "accepted",
              "raw_body_b64": base64.b64encode(raw_body).decode(), "task": task}
    if primary:
        record["alias_intent_schema"] = 1
    _validate(record, delivery_id)
    record["_reserved_bytes"] = _reservation(record)
    if len(_encode_record(record)) + MAX_COMPLETION_BYTES + 16 > MAX_RECORD_BYTES:
        raise ReceiptCapacityError("receipt exceeds per-file safety limit")
    return record


def _json_value_prefix(text):
    """Whether text can be completed to exactly one JSON value (no evaluation)."""
    class Incomplete(Exception):
        pass

    def needed(pos):
        if pos == len(text):
            raise Incomplete
        return text[pos]

    def space(pos):
        while pos < len(text) and text[pos] in " \t\r\n":
            pos += 1
        return pos

    def string(pos):
        start = pos
        pos += 1
        while True:
            char = needed(pos)
            pos += 1
            if char == '"':
                json.loads(text[start:pos])
                return pos
            if ord(char) < 32:
                raise ValueError
            if char == "\\":
                escaped = needed(pos)
                pos += 1
                if escaped == "u":
                    for _ in range(4):
                        if needed(pos) not in "0123456789abcdefABCDEF":
                            raise ValueError
                        pos += 1
                elif escaped not in '"\\/bfnrt':
                    raise ValueError

    def value(pos, depth=0):
        if depth > 64:
            raise ValueError
        pos = space(pos)
        char = needed(pos)
        if char == '"':
            return string(pos)
        if char in "[{":
            closing = "]" if char == "[" else "}"
            pos = space(pos + 1)
            if needed(pos) == closing:
                return pos + 1
            while True:
                if char == "{":
                    if needed(pos) != '"':
                        raise ValueError
                    pos = space(string(pos))
                    if needed(pos) != ":":
                        raise ValueError
                    pos += 1
                pos = space(value(pos, depth + 1))
                if needed(pos) == closing:
                    return pos + 1
                if needed(pos) != ",":
                    raise ValueError
                pos = space(pos + 1)
        for literal in ("true", "false", "null"):
            if char == literal[0]:
                for expected in literal:
                    if needed(pos) != expected:
                        raise ValueError
                    pos += 1
                return pos
        if char == "-":
            pos += 1
            char = needed(pos)
        if char not in "0123456789":
            raise ValueError
        pos += 1
        if char != "0":
            while pos < len(text) and text[pos] in "0123456789":
                pos += 1
        for marker in (".", "eE"):
            if pos < len(text) and text[pos] in marker:
                pos += 1
                if marker == "eE" and needed(pos) in "+-":
                    pos += 1
                if needed(pos) not in "0123456789":
                    raise ValueError
                while pos < len(text) and text[pos] in "0123456789":
                    pos += 1
        return pos

    try:
        return space(value(0)) == len(text)
    except Incomplete:
        return True
    except (ValueError, RecursionError):
        return False


def _partial_outcome_identity(data, original):
    """Reject observable identity changes without interpreting a partial outcome.

    The final record proves the held task, not the partial body's outcome. A
    partially written completion can only keep that task held, never expose the
    old accepted state or supply a successful acknowledgement.
    """
    if original["status"] != "accepted":
        raise ReceiptConflict("partial write would change an immutable outcome")
    head = _encode_record(original).split(b"\n", 1)[0] + b"\n"
    if len(data) < len(head):
        if not head.startswith(data):
            raise ReceiptConflict("partial receipt header conflicts with original")
        return
    if not data.startswith(head):
        raise ReceiptConflict("partial receipt header conflicts with original")
    # UTF-8 can be interrupted inside a character; only an incomplete trailing
    # codepoint is permissible. Its bytes remain untouched on disk.
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        if error.end != len(data) or error.reason != "unexpected end of data":
            raise ReceiptCorrupt("partial receipt is not a UTF-8 prefix") from error
        text = data[:error.start].decode("utf-8")
    decoder = json.JSONDecoder()
    offset, seen = 1, set()
    while offset < len(text):
        while offset < len(text) and text[offset] in " \n\r\t":
            offset += 1
        if offset == len(text):
            return
        try:
            key, end = decoder.raw_decode(text, offset)
        except json.JSONDecodeError as error:
            if any(_json(key).decode().startswith(text[offset:]) for key in (set(original) | {"completion"}) - seen):
                return
            raise ReceiptCorrupt("partial receipt key is not a JSON prefix") from error
        if not isinstance(key, str) or key in seen or key not in set(original) | {"completion"}:
            raise ReceiptConflict("partial receipt has an unknown or repeated field")
        seen.add(key)
        if end == len(text):
            return
        if text[end] != ":":
            raise ReceiptCorrupt("partial receipt has an invalid field separator")
        offset = end + 1
        fragment = text[offset:]
        if key not in {"completion", "status"} and _json(original[key]).decode().startswith(fragment):
            return
        if key == "status" and any(_json(status).decode().startswith(fragment) for status in ("completed", "terminal")):
            return
        if key == "completion" and len(fragment.encode()) <= MAX_COMPLETION_BYTES and _json_value_prefix(fragment):
            return
        try:
            value, end = decoder.raw_decode(text, offset)
        except json.JSONDecodeError as error:
            fragment = text[offset:]
            if key not in {"completion", "status"}:
                if not _json(original[key]).decode().startswith(fragment):
                    raise ReceiptConflict("partial immutable receipt field changed") from error
            elif key == "status":
                if not any(_json(status).decode().startswith(fragment) for status in ("completed", "terminal")):
                    raise ReceiptConflict("partial receipt outcome changed") from error
            elif not _json_value_prefix(fragment):
                raise ReceiptCorrupt("completion is not a JSON prefix") from error
            return
        if end == len(text) and key not in {"completion", "status"}:
            # JSONDecoder considers 179 a complete number even if the write was
            # interrupted inside the actual accepted_at value 1792630824.1423.
            if _json(original[key]).decode().startswith(text[offset:]):
                return
            raise ReceiptConflict("partial immutable receipt field changed")
        if key == "completion":
            _completion(value)
        elif key == "status":
            if value not in {"completed", "terminal"}:
                raise ReceiptConflict("partial receipt has an invalid outcome")
        elif value != original[key]:
            raise ReceiptConflict("partial immutable receipt field changed")
        if end == len(text):
            return
        if text[end] != ",":
            raise ReceiptCorrupt("partial receipt has an invalid separator")
        offset = end + 1


class ReceiptStore:
    def __init__(self, spool, max_records=MAX_RECORDS, max_bytes=MAX_BYTES, *, partial_context=None):
        if type(max_records) is not int or not 1 <= max_records <= MAX_RECORDS:
            raise ValueError("receipt record capacity exceeds hard limit")
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES:
            raise ValueError("receipt byte capacity exceeds hard limit")
        self.spool = Path(spool)
        self.directory = self.spool / "receipts"
        self.tasks = self.directory / "tasks"
        self.intents = self.directory / "alias-intents"
        self.max_records = max_records
        self.max_bytes = max_bytes
        self.partial_context = partial_context

    def _directories(self, create=False):
        _private_directory(self.spool)
        for path in (self.directory, self.tasks):
            try:
                _private_directory(path)
            except FileNotFoundError:
                if not create:
                    return False
                path.mkdir(mode=0o700)
                _fsync_directory(path.parent)
        return True

    def _barrier(self, path):
        fd = _open_private(path)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        current = path.parent
        while True:
            _fsync_directory(current)
            if current == self.spool.parent:
                break
            current = current.parent

    def _write(self, path, payload):
        if len(payload) > MAX_RECORD_BYTES:
            raise ReceiptCapacityError("receipt exceeds per-file safety limit")
        temporary = path.with_name(path.name + ".pending")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            if _read(temporary) != payload:
                raise ReceiptConflict("unconfirmed receipt write requires recovery")
            fd = _open_private(temporary)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(temporary, path)
        self._barrier(path)

    def _read_record(self, path, delivery_id):
        try:
            record = _validate(_decode(_read(path)), delivery_id)
        except FileNotFoundError:
            return None
        if _head(path) != {key: record[key] for key in _HEAD_KEYS}:
            raise ReceiptCorrupt("receipt header disagrees with body")
        return record

    @staticmethod
    def _check_transition(record, pending):
        if record is None:
            if pending["status"] != "accepted":
                raise ReceiptConflict("terminal pending write has no accepted original")
        elif pending != record:
            pending_identity = {key: value for key, value in pending.items() if key not in {"status", "completion"}}
            record_identity = {key: value for key, value in record.items() if key not in {"status", "completion"}}
            if (pending_identity != record_identity or record["status"] != "accepted"
                    or pending["status"] not in {"completed", "terminal"}
                    or pending["primary_delivery_id"] != pending["delivery_id"]):
                raise ReceiptConflict("pending receipt transition conflicts with original")

    def _record(self, delivery_id):
        path = self.directory / (_identifier(delivery_id) + ".json")
        record = self._read_record(path, delivery_id)
        pending_path = path.with_name(path.name + ".pending")
        pending = self._read_record(pending_path, delivery_id)
        if pending is not None:
            self._check_transition(record, pending)
            if pending.get("alias_intent_schema") == 1 and self._alias_intent(delivery_id) != pending:
                raise ReceiptConflict("pending alias receipt differs from immutable intent")
            # A full pending outcome is the original intent, not a fresh write:
            # preserve its finish time and recover before exposing accepted.
            self._write(path, _encode_record(pending))
            record = pending
        if record is not None:
            if record.get("alias_intent_schema") == 1:
                intent = self._alias_intent(delivery_id)
                if intent != record:
                    raise ReceiptConflict("alias receipt disagrees with immutable intent")
            self._barrier(path)
        return record

    def load(self, delivery_id):
        _identifier(delivery_id)
        if not self._directories():
            return None
        record = self._record(delivery_id)
        if record is None and self._alias_intent(delivery_id) is not None:
            raise ReceiptConflict("alias acceptance intent has no confirmed receipt")
        if record is not None:
            primary = self.find_task(record["task_digest"])
            if primary is None or primary["delivery_id"] != record["primary_delivery_id"]:
                raise ReceiptConflict("receipt primary identity mismatch")
        return record

    def _held_header(self, path, delivery_id, data):
        if not path.name.endswith(".json.pending"):
            raise ReceiptCorrupt("corrupt final receipt cannot be isolated as a partial write")
        original = self._read_record(self.directory / (delivery_id + ".json"), delivery_id)
        if original is not None:
            if original["primary_delivery_id"] != delivery_id:
                if not _encode_record(original).startswith(data):
                    raise ReceiptConflict("immutable alias receipt changed")
            else:
                _partial_outcome_identity(data, original)
        else:
            original = self._alias_intent(delivery_id)
            if original is None and self.partial_context is not None:
                try:
                    hint = _head(path)
                except ReceiptCorrupt:
                    hint = None
                original = self.partial_context(delivery_id, hint)
            if original is None:
                raise ReceiptCorrupt("partial receipt has no trusted acceptance context")
            _validate(original, delivery_id)
            if original["status"] != "accepted" or not _encode_record(original).startswith(data):
                raise ReceiptConflict("partial receipt conflicts with frozen queue acceptance")
        # This is a durable hold, not recovery or acceptance. Do not alter the
        # bytes. A failed hold barrier must still stop admission.
        self._barrier(path)
        return {key: original[key] for key in _HEAD_KEYS}

    def _headers(self):
        headers = self._intent_headers()
        for path in self.directory.iterdir():
            if path.name in {"tasks", "alias-intents"}:
                continue
            name = path.name.removesuffix(".pending")
            if not name.endswith(".json"):
                raise ReceiptCorrupt("unknown receipt entry")
            delivery_id = _identifier(name[:-5])
            data = _read(path)
            try:
                stored = _validate(_decode(data), delivery_id)
            except ReceiptMalformedJSON:
                head = self._held_header(path, delivery_id, data)
                stored = None
            else:
                head = _head(path)
                if stored.get("alias_intent_schema") == 1 and self._alias_intent(delivery_id) != stored:
                    raise ReceiptConflict("alias receipt differs from immutable intent")
                if path.name.endswith(".pending"):
                    original = self._read_record(self.directory / (delivery_id + ".json"), delivery_id)
                    self._check_transition(original, stored)
            if stored is None:
                old = headers.get(delivery_id)
                if old is not None and old != head:
                    raise ReceiptConflict("partial receipt identity mismatch")
                headers[delivery_id] = head
                continue
            if head != {key: stored[key] for key in _HEAD_KEYS}:
                raise ReceiptCorrupt("reservation header disagrees with receipt")
            if head["delivery_id"] != delivery_id:
                raise ReceiptConflict("receipt filename mismatch")
            old = headers.get(delivery_id)
            if old is not None and old != head:
                raise ReceiptConflict("pending receipt identity mismatch")
            headers[delivery_id] = head
        return headers

    def _intent_directory(self, create=False):
        try:
            _private_directory(self.intents)
        except FileNotFoundError:
            if not create:
                return False
            self.intents.mkdir(mode=0o700)
            _fsync_directory(self.directory)
        return True

    def _validate_intent(self, record, delivery_id):
        _validate(record, delivery_id)
        if record.get("alias_intent_schema") != 1 or record["status"] != "accepted" or "completion" in record:
            raise ReceiptConflict("alias intent is not an immutable acceptance")
        primary = self._read_record(self.directory / (record["primary_delivery_id"] + ".json"),
                                    record["primary_delivery_id"])
        if (primary is None or primary["delivery_id"] != primary["primary_delivery_id"]
                or any(record[key] != primary[key] for key in
                       ("task", "task_digest", "accepted_strategy", "accepted_revision"))):
            raise ReceiptConflict("alias intent has no matching primary")
        index = self.tasks / (record["task_digest"] + ".json")
        expected = {"schema": 1, "durable": True, "task_digest": record["task_digest"],
                    "primary_delivery_id": record["primary_delivery_id"]}
        if _read(index) != _json(expected):
            raise ReceiptConflict("alias intent primary index mismatch")
        self._barrier(self.directory / (primary["delivery_id"] + ".json"))
        self._barrier(index)
        return record

    def _alias_intent(self, delivery_id):
        if not self._intent_directory():
            return None
        path = self.intents / (_identifier(delivery_id) + ".json")
        record = self._read_record(path, delivery_id)
        pending = self._read_record(path.with_name(path.name + ".pending"), delivery_id)
        if pending is not None:
            self._validate_intent(pending, delivery_id)
            if record is not None and record != pending:
                raise ReceiptConflict("alias intent is immutable")
            self._write(path, _encode_record(pending))
            record = pending
        if record is not None:
            self._validate_intent(record, delivery_id)
            self._barrier(path)
        return record

    def _intent_headers(self):
        if not self._intent_directory():
            return {}
        delivery_ids = set()
        for path in self.intents.iterdir():
            name = path.name.removesuffix(".pending")
            if not name.endswith(".json"):
                raise ReceiptCorrupt("unknown alias intent entry")
            delivery_ids.add(_identifier(name[:-5]))
        headers = {}
        for delivery_id in delivery_ids:
            name = delivery_id + ".json"
            path = self.intents / (name + ".pending")
            try:
                # Compare full immutable records, not merely equal headers.
                # A complete matching pending copy is synchronized/recovered
                # before lowering the conservative slot to actual reservation.
                record = self._alias_intent(delivery_id)
            except ReceiptMalformedJSON:
                # This namespace is only written BEFORE any alias receipt.
                # Do not infer a task/acceptance from damaged bytes or headers.
                # Its hard maximum was reserved before opening this file.
                related = [self.intents / name, self.directory / name,
                           self.directory / (name + ".pending")]
                if any(p.exists() or p.is_symlink() for p in related):
                    raise ReceiptCorrupt("partial alias intent is not a pre-receipt hold")
                self._barrier(path)
                head = {"delivery_id": delivery_id, "task_digest": None,
                        "primary_delivery_id": None, "_reserved_bytes": MAX_ALIAS_RESERVATION}
            else:
                if record is None:
                    raise ReceiptCorrupt("alias intent disappeared during admission")
                head = {key: record[key] for key in _HEAD_KEYS}
            headers[delivery_id] = head
        return headers

    def _prepare_alias(self, record):
        self._intent_directory(create=True)
        self._reserve(record, reserve_bytes=MAX_ALIAS_RESERVATION)
        path = self.intents / (record["delivery_id"] + ".json")
        self._write(path, _encode_record(record))
        # An immutable, independently synced acceptance is available before
        # the ordinary receipt can become partially visible.
        return self._alias_intent(record["delivery_id"])

    def _index(self, primary):
        payload = _json({"schema": 1, "durable": True, "task_digest": primary["task_digest"],
                         "primary_delivery_id": primary["delivery_id"]})
        path = self.tasks / (primary["task_digest"] + ".json")
        if path.exists() or path.is_symlink():
            if _read(path) != payload:
                raise ReceiptConflict("task index identity mismatch")
            self._barrier(path)
        else:
            self._write(path, payload)

    def find_task(self, task_digest):
        _identifier(task_digest)
        if not self._directories():
            return None
        path = self.tasks / (task_digest + ".json")
        try:
            index = _decode(_read(path))
        except FileNotFoundError:
            matches = [head["delivery_id"] for head in self._headers().values()
                       if head["task_digest"] == task_digest and head["primary_delivery_id"] == head["delivery_id"]]
            if len(matches) > 1:
                raise ReceiptConflict("multiple primary receipts")
            if not matches:
                return None
            primary = self._record(matches[0])
            if primary is None:
                raise ReceiptConflict("unconfirmed primary write requires recovery")
            self._index(primary)
            return primary
        if (not isinstance(index, dict) or type(index.get("schema")) is not int
                or index["schema"] != 1 or index.get("durable") is not True or index.get("task_digest") != task_digest):
            raise ReceiptCorrupt("invalid task index")
        primary = self._record(_identifier(index.get("primary_delivery_id")))
        if primary is None or primary["delivery_id"] != primary["primary_delivery_id"] or primary["task_digest"] != task_digest:
            raise ReceiptConflict("task index has no matching primary receipt")
        self._index(primary)
        return primary

    def _reserve(self, record, *, reserve_bytes=None):
        record["_reserved_bytes"] = _reservation(record)
        headers = self._headers()
        requested = record["_reserved_bytes"] if reserve_bytes is None else reserve_bytes
        if len(headers) >= min(self.max_records, MAX_RECORDS) or sum(item["_reserved_bytes"] for item in headers.values()) + requested > min(self.max_bytes, MAX_BYTES):
            raise ReceiptCapacityError("receipt capacity exhausted; existing evidence retained")

    def accept(self, raw_body, task, *, strategy, revision, accepted_at=None):
        if not isinstance(raw_body, bytes) or not raw_body or not isinstance(task, dict):
            raise ReceiptCorrupt("invalid request body or normalized task")
        if len(raw_body) > MAX_RECORD_BYTES:
            raise ReceiptCapacityError("receipt body exceeds per-file safety limit")
        delivery_id = hashlib.sha256(raw_body).hexdigest()
        try:
            task_digest = hashlib.sha256(_json(task)).hexdigest()
        except (TypeError, ValueError) as exc:
            raise ReceiptCorrupt("invalid normalized task") from exc
        self._directories(create=True)
        previous = self._record(delivery_id)
        pending = self.directory / (delivery_id + ".json.pending")
        if previous is None and (pending.exists() or pending.is_symlink()):
            previous = _validate(_decode(_read(pending)), delivery_id)
            _head(pending)
            if previous["raw_body_b64"] != base64.b64encode(raw_body).decode() or previous["task"] != task:
                raise ReceiptConflict("pending duplicate delivery identity mismatch")
            self._write(self.directory / (delivery_id + ".json"), _encode_record(previous))
        if previous is not None:
            if previous["raw_body_b64"] != base64.b64encode(raw_body).decode() or previous["task"] != task:
                raise ReceiptConflict("duplicate delivery identity mismatch")
            primary = self.find_task(task_digest)
            if primary is None or primary["delivery_id"] != previous["primary_delivery_id"]:
                raise ReceiptConflict("duplicate primary identity mismatch")
            return previous, False
        primary = self.find_task(task_digest)
        record = self._alias_intent(delivery_id)
        if record is not None:
            if (record["raw_body_b64"] != base64.b64encode(raw_body).decode() or record["task"] != task
                    or primary is None or primary["delivery_id"] != record["primary_delivery_id"]):
                raise ReceiptConflict("alias acceptance intent conflicts with delivery")
        else:
            record = accepted_record(raw_body, task, strategy=strategy, revision=revision,
                                     accepted_at=time.time() if accepted_at is None else accepted_at,
                                     primary=primary)
            if primary:
                record = self._prepare_alias(record)
            else:
                self._reserve(record)
        self._write(self.directory / (delivery_id + ".json"), _encode_record(record))
        self._index(primary or record)
        return record, True

    def update_status(self, delivery_id, *, status, completion=None):
        _completion(completion)
        record = self.load(delivery_id)
        if record is None:
            raise ReceiptConflict("receipt missing")
        if record["primary_delivery_id"] != delivery_id:
            raise ReceiptConflict("update the primary receipt, not an alias")
        if not isinstance(status, str) or status not in {"completed", "terminal"}:
            raise ReceiptConflict("unsupported receipt transition")
        if record["status"] != "accepted":
            if record["status"] == status and record.get("completion") == completion:
                return record
            raise ReceiptConflict("terminal receipt outcome is immutable")
        updated = dict(record, status=status, completion=completion)
        payload = _encode_record(updated)
        if len(payload) * 2 + _INDEX_RESERVE > record["_reserved_bytes"]:
            raise ReceiptCapacityError("completion exceeds its reserved storage")
        self._write(self.directory / (delivery_id + ".json"), payload)
        return updated
