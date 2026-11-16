#!/usr/bin/env python3
"""Read-only audit of article identity rows that need a one-off digest backfill.

``d1e2ba22`` folded trailing slashes and tracking query parameters into the
article identity.  Rows written before it keep their old digest, so the same
article re-entering with the old spelling would create a second event.  This
tool never writes: it opens the database with ``mode=ro`` plus
``PRAGMA query_only``, then classifies every stored ``article_url_sha256``
identity as:

- ``current``      -- the stored digest already matches the current code
- ``backfillable`` -- a new alias for the same event could be inserted
- ``conflict``     -- the new digest already belongs to another event
- ``no_source``    -- no evidence/cache row can prove which URL wrote it

A backfill that would insert the ``backfillable`` aliases is a separate,
explicitly authorised production write; this script only reports counts and
bounded samples (identity prefixes and event ids, never source URLs).

WAL databases may need a quiescent copy; if ``mode=ro`` cannot open the file,
copy the database (including ``-wal``/``-shm``) and audit the copy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from db.database import _normalized_article_identity

_IDENTITY_TYPE = "article_url_sha256"
_DIGEST_PREFIX = b"ai-filter-article-observation-v1\0"
_REQUIRED_TABLES = frozenset(
    {"events", "event_identities", "event_evidence", "news_cache"}
)
_SAMPLE_LIMIT = 5
_MERGE_HOPS = 16


def legacy_identity(link: str) -> str:
    """Frozen pre-2026-11-08 digest; changing this invalidates the backfill."""
    raw = str(link or "").strip()
    if not raw:
        raise ValueError("article link is required")
    normalized = raw
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
            hostname = parsed.hostname.encode("idna").decode("ascii").lower()
            port = parsed.port
            default_port = (parsed.scheme.lower() == "http" and port == 80) or (
                parsed.scheme.lower() == "https" and port == 443
            )
            netloc = hostname if not port or default_port else f"{hostname}:{port}"
            normalized = urlunsplit(
                (
                    parsed.scheme.lower(),
                    netloc,
                    parsed.path or "/",
                    parsed.query,
                    "",
                )
            )
    except (UnicodeError, ValueError):
        normalized = raw
    return hashlib.sha256(
        _DIGEST_PREFIX + normalized.encode("utf-8", errors="replace")
    ).hexdigest()


def open_readonly(path) -> sqlite3.Connection:
    """Open ``path`` read-only; any write statement fails instead of mutating."""
    uri = f"file:{quote(str(Path(path).expanduser().resolve()))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _missing_tables(conn: sqlite3.Connection) -> list[str]:
    present = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    return sorted(_REQUIRED_TABLES - present)


def _canonical_event(conn: sqlite3.Connection, event_id: str) -> str:
    current = str(event_id or "").strip()
    seen = set()
    for _ in range(_MERGE_HOPS):
        if not current or current in seen:
            return current
        seen.add(current)
        row = conn.execute(
            "SELECT merged_into FROM events WHERE event_id = ?", (current,)
        ).fetchone()
        target = str(row[0] or "").strip() if row else ""
        if not target:
            return current
        current = target
    return current


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _source_urls(conn: sqlite3.Connection) -> dict[str, set[str]]:
    urls: dict[str, set[str]] = defaultdict(set)
    for event_id, source_uri in conn.execute(
        "SELECT event_id, source_uri FROM event_evidence"
    ):
        text = str(source_uri or "").strip()
        if event_id and text:
            urls[str(event_id)].add(text)
    if "event_id" in _columns(conn, "news_cache"):
        for event_id, link in conn.execute("SELECT event_id, link FROM news_cache"):
            text = str(link or "").strip()
            if event_id and text:
                urls[str(event_id)].add(text)
    return urls


def _current_digest(link: str) -> str:
    return _normalized_article_identity(link)[1]


def _identity_rows(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (str(key), str(event_id))
        for key, event_id in conn.execute(
            "SELECT identity_key, event_id FROM event_identities "
            "WHERE identity_type = ?",
            (_IDENTITY_TYPE,),
        )
    ]


def _classify(key, event_id, canonical, urls, owners) -> str:
    """Return ``current`` / ``backfillable`` / ``conflict`` / ``no_source``."""
    candidates = urls.get(str(event_id), set()) | urls.get(canonical, set())
    proven = []
    for url in candidates:
        try:
            if legacy_identity(url) == key:
                proven.append(url)
        except ValueError:
            continue
    if not proven:
        return "no_source"
    digests = {_current_digest(url) for url in proven}
    if len(digests) > 1:
        return "conflict"
    digest = digests.pop()
    if digest == key:
        return "current"
    owner = owners.get(digest)
    if owner is None or owner == canonical:
        return "backfillable"
    return "conflict"


def scan(conn: sqlite3.Connection) -> dict:
    """Classify every stored identity row without writing anything."""
    raw_rows = _identity_rows(conn)
    owners = {
        key: _canonical_event(conn, event_id) for key, event_id in raw_rows
    }
    urls = _source_urls(conn)
    counts = {
        "identity_rows": 0,
        "current": 0,
        "backfillable": 0,
        "conflict": 0,
        "no_source": 0,
    }
    samples: dict[str, list[dict]] = {
        "backfillable": [],
        "conflict": [],
        "no_source": [],
    }
    for key, event_id in raw_rows:
        canonical = _canonical_event(conn, event_id)
        outcome = _classify(key, event_id, canonical, urls, owners)
        counts["identity_rows"] += 1
        counts[outcome] += 1
        if outcome in samples and len(samples[outcome]) < _SAMPLE_LIMIT:
            samples[outcome].append({"identity": key[:12], "event_id": canonical})
    return {"counts": counts, "samples": samples}


def _format_text(result: dict) -> str:
    counts = result["counts"]
    lines = [f"article identity rows: {counts['identity_rows']}"]
    for name in ("current", "backfillable", "conflict", "no_source"):
        lines.append(f"{name}: {counts[name]}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only audit of article identity backfill needs"
    )
    parser.add_argument("--db", required=True, help="path to the SQLite database")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    try:
        conn = open_readonly(args.db)
    except sqlite3.Error as exc:
        print(f"cannot open database read-only: {exc}", file=sys.stderr)
        return 1
    try:
        missing = _missing_tables(conn)
        if missing:
            print(f"missing tables: {', '.join(missing)}", file=sys.stderr)
            return 2
        result = scan(conn)
    except sqlite3.Error as exc:
        print(f"audit failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(_format_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
