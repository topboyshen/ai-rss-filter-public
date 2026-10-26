"""Bounded, insert-only A-share security-master synchronization.

CNINFO is used only as a candidate universe and remains authoritative for the
canonical name. Tencent's hardened quote adapter confirms a fresh symbol and
exchange prefix before a row may
be inserted; name differences are reported rather than silently adopted.
Existing security rows are never updated: ``upsert_security`` has
full-replacement semantics, so a partial external snapshot must not erase
manually curated aliases, industry, concepts, or lifecycle state.

This module deliberately does not create ``event_securities``.  Security master
data is entity resolution input, not evidence that a particular event affects
an equity.
"""

from __future__ import annotations

import itertools
import json
import re
import sqlite3
import time
import unicodedata
import urllib.request
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from modules.a_stock_data import MAX_CODES_PER_REQUEST, UA, tencent_quote


CNINFO_STOCK_URL = "https://www.cninfo.com.cn/new/data/szse_stock.json"
MAX_CNINFO_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CNINFO_RECORDS = 10_000
MIN_CNINFO_A_SHARE_RECORDS = 4_000
MIN_CNINFO_EXCHANGE_RECORDS = {"SSE": 1_800, "SZSE": 2_300, "BSE": 300}
MIN_TENCENT_CONFIRMATION_RATIO = 0.70
MIN_TENCENT_EXCHANGE_RATIO = 0.50
MAX_TENCENT_QUOTE_AGE_SECONDS = 14 * 24 * 60 * 60
MAX_TENCENT_FUTURE_SKEW_SECONDS = 5 * 60

_ASCII_TICKER_RE = re.compile(r"[0-9]{6}\Z")
_PINYIN_RE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_ORG_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_EXCHANGE_PREFIX = {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}
_SYNC_SAVEPOINTS = itertools.count()


class SecurityMasterError(RuntimeError):
    """Base error for fail-closed security-master synchronization."""


class SecurityMasterFetchError(SecurityMasterError):
    """A fixed upstream endpoint failed its transport contract."""


class SecurityMasterValidationError(SecurityMasterError):
    """An upstream snapshot failed schema or completeness validation."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _open_cninfo(request: urllib.request.Request, *, timeout: int):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RejectRedirects(),
    )
    return opener.open(request, timeout=timeout)


@dataclass(frozen=True)
class SecurityCandidate:
    exchange: str
    ticker: str
    name: str
    aliases: tuple[str, ...] = ()
    source_org_id: str = ""

    @property
    def quote_symbol(self) -> str:
        return _EXCHANGE_PREFIX[self.exchange] + self.ticker


def _bounded_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise SecurityMasterValidationError(f"invalid {field}")
    text = unicodedata.normalize("NFC", value).strip()
    if not text or len(text) > maximum or "\x00" in text:
        raise SecurityMasterValidationError(f"invalid {field}")
    return text


def _nonnegative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _exchange_for_ticker(ticker: str) -> str:
    if not _ASCII_TICKER_RE.fullmatch(ticker):
        raise SecurityMasterValidationError("ticker must be six ASCII digits")
    if ticker.startswith(("0", "3")):
        return "SZSE"
    if ticker.startswith("6"):
        return "SSE"
    if ticker.startswith(("4", "8")) or re.fullmatch(r"920[0-9]{3}", ticker):
        return "BSE"
    raise SecurityMasterValidationError(f"unsupported A-share ticker segment: {ticker}")


def _candidate_from_cninfo(record: object) -> SecurityCandidate | None:
    if not isinstance(record, dict):
        raise SecurityMasterValidationError("CNINFO stockList row must be an object")
    if record.get("category") != "A股":
        return None

    ticker = _bounded_text(record.get("code"), "ticker", 6)
    exchange = _exchange_for_ticker(ticker)
    name = _bounded_text(record.get("zwjc"), "security name", 255)
    org_id = _bounded_text(record.get("orgId"), "CNINFO orgId", 64)
    if not _ORG_ID_RE.fullmatch(org_id):
        raise SecurityMasterValidationError("invalid CNINFO orgId")

    aliases: set[str] = {ticker, f"{_EXCHANGE_PREFIX[exchange].upper()}{ticker}"}
    suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}[exchange]
    aliases.add(f"{ticker}.{suffix}")
    raw_pinyin = record.get("pinyin")
    if raw_pinyin is None:
        pinyin = ""
    elif isinstance(raw_pinyin, str):
        pinyin = unicodedata.normalize("NFC", raw_pinyin).strip()
    else:
        raise SecurityMasterValidationError("invalid CNINFO pinyin")
    if pinyin:
        if not _PINYIN_RE.fullmatch(pinyin):
            raise SecurityMasterValidationError("invalid CNINFO pinyin")
    return SecurityCandidate(
        exchange,
        ticker,
        name,
        tuple(sorted(aliases)),
        org_id,
    )


def _collapse_bse_legacy_codes(
    by_identity: dict[tuple[str, str], SecurityCandidate],
) -> None:
    """Prefer the 920 code while preserving legacy BSE codes as aliases."""
    by_org: dict[str, list[SecurityCandidate]] = {}
    for candidate in by_identity.values():
        if candidate.exchange == "BSE" and candidate.source_org_id:
            by_org.setdefault(candidate.source_org_id, []).append(candidate)
    for group in by_org.values():
        current = [
            candidate
            for candidate in group
            if re.fullmatch(r"920[0-9]{3}", candidate.ticker)
        ]
        legacy = [
            candidate
            for candidate in group
            if candidate.ticker.startswith(("4", "8"))
        ]
        if len(current) != 1 or not legacy or len(current) + len(legacy) != len(group):
            continue
        preferred = current[0]
        aliases = set(preferred.aliases)
        for old in legacy:
            aliases.update(old.aliases)
            by_identity.pop((old.exchange, old.ticker), None)
        by_identity[(preferred.exchange, preferred.ticker)] = SecurityCandidate(
            preferred.exchange,
            preferred.ticker,
            preferred.name,
            tuple(sorted(aliases)),
            preferred.source_org_id,
        )


def parse_cninfo_snapshot(
    payload: bytes,
    *,
    minimum_a_shares: int = MIN_CNINFO_A_SHARE_RECORDS,
) -> list[SecurityCandidate]:
    minimum_count = _nonnegative_integer(minimum_a_shares, "minimum_a_shares")
    if minimum_count > MAX_CNINFO_RECORDS:
        raise ValueError("minimum_a_shares exceeds record limit")
    if len(payload) > MAX_CNINFO_RESPONSE_BYTES:
        raise SecurityMasterValidationError("CNINFO response exceeds size limit")
    try:
        document = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecurityMasterValidationError("CNINFO response is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("stockList"), list):
        raise SecurityMasterValidationError("CNINFO response lacks stockList")
    rows = document["stockList"]
    if len(rows) > MAX_CNINFO_RECORDS:
        raise SecurityMasterValidationError("CNINFO stockList exceeds record limit")

    by_identity: dict[tuple[str, str], SecurityCandidate] = {}
    for raw in rows:
        candidate = _candidate_from_cninfo(raw)
        if candidate is None:
            continue
        key = (candidate.exchange, candidate.ticker)
        previous = by_identity.get(key)
        if previous is not None and previous != candidate:
            raise SecurityMasterValidationError(
                f"conflicting CNINFO identity: {candidate.exchange}/{candidate.ticker}"
            )
        by_identity[key] = candidate

    _collapse_bse_legacy_codes(by_identity)
    if len(by_identity) < minimum_count:
        raise SecurityMasterValidationError(
            f"CNINFO A-share count below completeness floor: {len(by_identity)}"
        )
    exchanges = Counter(candidate.exchange for candidate in by_identity.values())
    if minimum_count >= MIN_CNINFO_A_SHARE_RECORDS:
        for exchange, floor in MIN_CNINFO_EXCHANGE_RECORDS.items():
            if exchanges[exchange] < floor:
                raise SecurityMasterValidationError(
                    f"CNINFO {exchange} count below completeness floor: "
                    f"{exchanges[exchange]}"
                )
    return sorted(by_identity.values(), key=lambda item: (item.exchange, item.ticker))


def fetch_cninfo_candidates(
    *,
    minimum_a_shares: int = MIN_CNINFO_A_SHARE_RECORDS,
    opener: Callable[..., Any] = _open_cninfo,
) -> list[SecurityCandidate]:
    """Fetch the one fixed HTTPS snapshot; callers cannot supply an URL."""
    request = urllib.request.Request(
        CNINFO_STOCK_URL,
        headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        },
    )
    try:
        with opener(request, timeout=15) as response:
            if response.geturl() != CNINFO_STOCK_URL:
                raise SecurityMasterFetchError("CNINFO endpoint redirected")
            payload = response.read(MAX_CNINFO_RESPONSE_BYTES + 1)
    except SecurityMasterError:
        raise
    except Exception as exc:
        raise SecurityMasterFetchError("CNINFO request failed") from exc
    if len(payload) > MAX_CNINFO_RESPONSE_BYTES:
        raise SecurityMasterFetchError("CNINFO response exceeds size limit")
    return parse_cninfo_snapshot(payload, minimum_a_shares=minimum_a_shares)


def _chunks(
    values: Sequence[SecurityCandidate],
    size: int,
) -> Iterable[Sequence[SecurityCandidate]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def confirm_candidates_with_tencent(
    candidates: Sequence[SecurityCandidate],
    *,
    quote_fetcher: Callable[[list[str]], dict[str, dict[str, Any]]] = tencent_quote,
    minimum_ratio: float = MIN_TENCENT_CONFIRMATION_RATIO,
    minimum_exchange_ratio: float = MIN_TENCENT_EXCHANGE_RATIO,
    max_quote_age_seconds: int = MAX_TENCENT_QUOTE_AGE_SECONDS,
    now: int | None = None,
) -> tuple[list[SecurityCandidate], dict[str, int]]:
    """Confirm current symbol/name without treating one missing quote as delisting."""
    if not 0 <= minimum_ratio <= 1 or not 0 <= minimum_exchange_ratio <= 1:
        raise ValueError("confirmation ratios must be between zero and one")
    maximum_age = _nonnegative_integer(
        max_quote_age_seconds, "max_quote_age_seconds"
    )
    fixed_checked_at = None if now is None else _nonnegative_integer(now, "now")
    if len(candidates) > MAX_CNINFO_RECORDS:
        raise SecurityMasterValidationError("too many Tencent confirmation candidates")
    canonical_candidates = tuple(_validated_candidate(item) for item in candidates)
    identities = {
        (candidate.exchange, candidate.ticker)
        for candidate in canonical_candidates
    }
    if len(identities) != len(canonical_candidates):
        raise SecurityMasterValidationError(
            "Tencent confirmation candidates contain duplicate identities"
        )
    confirmed: list[SecurityCandidate] = []
    requested_by_exchange = Counter(
        candidate.exchange for candidate in canonical_candidates
    )
    confirmed_by_exchange: Counter[str] = Counter()
    name_mismatches = 0

    for batch in _chunks(canonical_candidates, MAX_CODES_PER_REQUEST):
        symbols = [candidate.quote_symbol for candidate in batch]
        try:
            quotes = quote_fetcher(symbols)
        except Exception as exc:
            raise SecurityMasterFetchError("Tencent quote confirmation failed") from exc
        if not isinstance(quotes, dict):
            raise SecurityMasterValidationError("Tencent quote result must be an object")
        checked_at = (
            int(time.time()) if fixed_checked_at is None else fixed_checked_at
        )
        for candidate in batch:
            quote = quotes.get(candidate.ticker)
            if not isinstance(quote, dict):
                continue
            symbol = str(quote.get("symbol") or "").strip().casefold()
            if symbol != candidate.quote_symbol:
                raise SecurityMasterValidationError(
                    f"Tencent symbol mismatch for {candidate.exchange}/{candidate.ticker}"
                )
            quote_ts = quote.get("quote_ts")
            if (
                isinstance(quote_ts, bool)
                or not isinstance(quote_ts, int)
                or quote_ts <= 0
                or quote_ts < checked_at - maximum_age
                or quote_ts > checked_at + MAX_TENCENT_FUTURE_SKEW_SECONDS
            ):
                continue
            name = _bounded_text(quote.get("name"), "Tencent security name", 255)
            confirmed.append(
                SecurityCandidate(
                    candidate.exchange,
                    candidate.ticker,
                    candidate.name,
                    candidate.aliases,
                    candidate.source_org_id,
                )
            )
            confirmed_by_exchange[candidate.exchange] += 1
            if name != candidate.name:
                name_mismatches += 1

    total = len(canonical_candidates)
    ratio = (len(confirmed) / total) if total else 1.0
    if total and ratio < minimum_ratio:
        raise SecurityMasterValidationError(
            f"Tencent confirmation ratio below floor: {ratio:.3f}"
        )
    for exchange, requested in requested_by_exchange.items():
        if (
            requested >= 10
            and confirmed_by_exchange[exchange] / requested
            < minimum_exchange_ratio
        ):
            raise SecurityMasterValidationError(
                f"Tencent {exchange} confirmation ratio below floor"
            )
    stats = {
        "candidate_count": total,
        "confirmed_count": len(confirmed),
        "unconfirmed_count": total - len(confirmed),
        "name_mismatch_count": name_mismatches,
    }
    for exchange in _EXCHANGE_PREFIX:
        stats[f"confirmed_{exchange.lower()}"] = confirmed_by_exchange[exchange]
    return confirmed, stats


def _existing_security(
    conn: sqlite3.Connection,
    candidate: SecurityCandidate,
) -> tuple[Any, ...] | None:
    return conn.execute(
        "SELECT security_id, name, asset_type, currency, aliases, industry, concepts, "
        "is_active, version, updated_at FROM securities WHERE exchange = ? AND ticker = ?",
        (candidate.exchange, candidate.ticker),
    ).fetchone()


def _existing_bse_legacy_count(
    conn: sqlite3.Connection,
    candidate: SecurityCandidate,
) -> int:
    if candidate.exchange != "BSE" or not candidate.ticker.startswith("920"):
        return 0
    legacy = sorted(
        {
            alias
            for alias in candidate.aliases
            if _ASCII_TICKER_RE.fullmatch(alias)
            and alias.startswith(("4", "8"))
        }
    )
    if not legacy:
        return 0
    placeholders = ",".join("?" for _item in legacy)
    return int(
        conn.execute(
            "SELECT count(*) FROM securities "
            f"WHERE exchange = 'BSE' AND ticker IN ({placeholders})",
            legacy,
        ).fetchone()[0]
    )


def _validated_candidate(candidate: object) -> SecurityCandidate:
    if not isinstance(candidate, SecurityCandidate):
        raise SecurityMasterValidationError("confirmed candidate has invalid type")
    exchange = _bounded_text(candidate.exchange, "exchange", 32).upper()
    if exchange not in _EXCHANGE_PREFIX:
        raise SecurityMasterValidationError("unsupported exchange")
    ticker = _bounded_text(candidate.ticker, "ticker", 6)
    name = _bounded_text(candidate.name, "security name", 255)
    source_org_id = ""
    if candidate.source_org_id:
        source_org_id = _bounded_text(
            candidate.source_org_id,
            "source org id",
            64,
        )
        if (
            candidate.source_org_id != source_org_id
            or not _ORG_ID_RE.fullmatch(source_org_id)
        ):
            raise SecurityMasterValidationError("invalid source org id")
    if (
        candidate.exchange != exchange
        or candidate.ticker != ticker
        or candidate.name != name
    ):
        raise SecurityMasterValidationError("candidate is not canonical")
    if _exchange_for_ticker(ticker) != exchange:
        raise SecurityMasterValidationError("ticker and exchange disagree")
    if isinstance(candidate.aliases, (str, bytes)):
        raise SecurityMasterValidationError("aliases must be a collection of strings")
    try:
        aliases = {
            _bounded_text(alias, "security alias", 255)
            for alias in candidate.aliases
        }
    except TypeError as exc:
        raise SecurityMasterValidationError("aliases must be iterable") from exc
    if len(aliases) > 128:
        raise SecurityMasterValidationError("too many security aliases")
    encoded_aliases = json.dumps(
        sorted(aliases), ensure_ascii=False, separators=(",", ":")
    )
    if len(encoded_aliases) > 16_000:
        raise SecurityMasterValidationError("security aliases are too large")
    return SecurityCandidate(
        exchange,
        ticker,
        name,
        tuple(sorted(aliases)),
        source_org_id,
    )


def _insert_security(
    conn: sqlite3.Connection,
    candidate: SecurityCandidate,
    *,
    now: int,
) -> None:
    """Insert one row without any conflict path that can update existing data."""
    aliases_json = json.dumps(
        list(candidate.aliases), ensure_ascii=False, separators=(",", ":")
    )
    conn.execute(
        "INSERT INTO securities "
        "(security_id, exchange, ticker, name, asset_type, currency, aliases, "
        "industry, concepts, is_active, version, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'equity', 'CNY', ?, '', '[]', 1, 1, ?, ?)",
        (
            str(uuid.uuid4()),
            candidate.exchange,
            candidate.ticker,
            candidate.name,
            aliases_json,
            now,
            now,
        ),
    )


@contextmanager
def _atomic_write(conn: sqlite3.Connection):
    """Own the write atomically without committing a caller-owned transaction."""
    if conn.in_transaction:
        name = f"security_master_sync_{next(_SYNC_SAVEPOINTS)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            conn.execute(f"RELEASE SAVEPOINT {name}")
            raise
        else:
            conn.execute(f"RELEASE SAVEPOINT {name}")
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def sync_confirmed_candidates(
    conn: sqlite3.Connection,
    candidates: Sequence[SecurityCandidate],
    *,
    dry_run: bool = False,
    now: int | None = None,
) -> dict[str, int]:
    """Insert confirmed equities atomically while preserving every existing row."""
    current_time = int(time.time()) if now is None else _nonnegative_integer(now, "now")
    stats = {
        "inserted": 0,
        "unchanged": 0,
        "manual_conflicts": 0,
        "legacy_conflicts": 0,
        "preserved": 0,
    }
    ordered = sorted(
        (_validated_candidate(item) for item in candidates),
        key=lambda item: (item.exchange, item.ticker),
    )
    if len({(item.exchange, item.ticker) for item in ordered}) != len(ordered):
        raise SecurityMasterValidationError("confirmed snapshot contains duplicate identities")

    def apply() -> None:
        for candidate in ordered:
            existing = _existing_security(conn, candidate)
            if existing is not None:
                same_name = str(existing[1]) == candidate.name
                compatible = (
                    str(existing[2]).casefold() == "equity"
                    and str(existing[3]).upper() == "CNY"
                    and int(existing[7]) == 1
                )
                stats["preserved"] += 1
                if same_name and compatible:
                    stats["unchanged"] += 1
                else:
                    stats["manual_conflicts"] += 1
                continue
            legacy_count = _existing_bse_legacy_count(conn, candidate)
            if legacy_count:
                stats["preserved"] += legacy_count
                stats["manual_conflicts"] += 1
                stats["legacy_conflicts"] += 1
                continue
            stats["inserted"] += 1
            if dry_run:
                continue
            _insert_security(conn, candidate, now=current_time)

    if dry_run:
        apply()
    else:
        with _atomic_write(conn):
            apply()
    return stats


def run_security_master_sync(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
    minimum_a_shares: int = MIN_CNINFO_A_SHARE_RECORDS,
    opener: Callable[..., Any] = _open_cninfo,
    quote_fetcher: Callable[[list[str]], dict[str, dict[str, Any]]] = tencent_quote,
) -> dict[str, int]:
    if (
        isinstance(minimum_a_shares, bool)
        or not isinstance(minimum_a_shares, int)
        or minimum_a_shares < MIN_CNINFO_A_SHARE_RECORDS
    ):
        raise ValueError(
            f"production minimum_a_shares must be >= {MIN_CNINFO_A_SHARE_RECORDS}"
        )
    candidates = fetch_cninfo_candidates(
        minimum_a_shares=minimum_a_shares,
        opener=opener,
    )
    confirmed, confirmation_stats = confirm_candidates_with_tencent(
        candidates,
        quote_fetcher=quote_fetcher,
    )
    return {
        **confirmation_stats,
        **sync_confirmed_candidates(conn, confirmed, dry_run=dry_run),
    }


__all__ = [
    "CNINFO_STOCK_URL",
    "SecurityCandidate",
    "SecurityMasterError",
    "SecurityMasterFetchError",
    "SecurityMasterValidationError",
    "confirm_candidates_with_tencent",
    "fetch_cninfo_candidates",
    "parse_cninfo_snapshot",
    "run_security_master_sync",
    "sync_confirmed_candidates",
]
