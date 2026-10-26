"""Pure, fail-closed title mentions over an already prepared security index.

These are literal publisher-title mentions, not investment recommendations.
The event writer shares the pure name/code/market conflict checks; card result
states remain separate. Neither read-only lookup rewrites stored mappings.
"""

from __future__ import annotations

import re
from typing import Any


_MARKETS = {
    "sh": "SSE", "ss": "SSE", "sse": "SSE", "沪": "SSE",
    "sz": "SZSE", "szse": "SZSE", "深": "SZSE",
    "bj": "BSE", "bse": "BSE", "京": "BSE",
    "hk": "HKEX", "hkex": "HKEX", "港": "HKEX",
    "us": "US", "nyse": "US", "nasdaq": "US", "amex": "US",
}
_MARKET_TOKEN = r"nasdaq|szse|hkex|nyse|amex|sse|bse|sh|ss|sz|bj|hk|us|沪|深|京|港"
_CODE_RE = re.compile(r"(?<![0-9])[0-9]{6}(?![0-9])")
_CODE_LABEL_RE = re.compile(r"(?:证券|股票|a股)代码[：:=（(]*$")
_ISSUER_CODE_LABEL = r"(?:证券|股票|a股)代码[：:=]?"
_NAME_CODE_RE = re.compile(
    rf"^(?:[（(](?:{_ISSUER_CODE_LABEL})?|[：:，,]?{_ISSUER_CODE_LABEL})"
    rf"(?:(?:{_MARKET_TOKEN})[.:：]?)?"
    rf"(?P<code>[0-9]{{6}})(?:[.:：]?(?:{_MARKET_TOKEN}))?(?![0-9a-z])[）)]?"
)
# Four/five-digit HK/HKEX codes only veto an A-share identity when explicitly
# marked and attached to that issuer. They never become association candidates.
# Keep the existing five-digit Chinese market notation unchanged.
# A bare suffix 港 needs a delimiter so 港元/港币 amounts are not market codes.
_NAME_HK_CODE_RE = re.compile(
    rf"^(?:[（(](?:{_ISSUER_CODE_LABEL})?|[：:，,]?{_ISSUER_CODE_LABEL})"
    r"(?:(?:hkex|hk)[.:：]?[0-9]{4,5}|港[.:：]?[0-9]{5}"
    r"|[0-9]{4,5}[.:：]?(?:hkex|hk)"
    r"|[0-9]{5}[.:：]?港(?=$|[)），,；;。:：]))"
    r"(?![0-9a-z])[）)]?"
)


def mention_result(raw_title: str, status: str, reason: str, mentions=None) -> dict:
    return {
        "raw_title": raw_title,
        "status": status,
        "reason": reason,
        "mentions": list(mentions or []),
    }


def _valid_a_share_row(row: tuple) -> bool:
    security_id, name, ticker, exchange, _normalized_name = row
    if not security_id or not name or not re.fullmatch(r"[0-9]{6}", ticker):
        return False
    expected = (
        "SSE" if ticker.startswith("6") else
        "SZSE" if ticker.startswith(("0", "3")) else
        "BSE" if ticker.startswith(("4", "8", "920")) else None
    )
    return expected is not None and exchange == expected


def _candidate_positions(compact: str, index: tuple) -> set[int]:
    _rows, names, tickers, lengths = index
    positions = set()
    for match in _CODE_RE.finditer(compact):
        positions.update(tickers.get(match.group(), ()))
    for start in range(len(compact)):
        for length in lengths:
            if start + length > len(compact):
                break
            positions.update(names.get(compact[start:start + length], ()))
    return positions


def _identity_count(rows: tuple, positions: tuple) -> int:
    # Use the complete index, before the output limit or market filtering.
    return len({rows[position][:4] for position in positions})


def _code_markets(compact: str, start: int, end: int) -> list[str]:
    left = compact[max(0, start - 8):start]
    right = compact[end:end + 8]
    prefix = re.search(rf"(?<![a-z])({_MARKET_TOKEN})[.:：]?$", left)
    suffix = re.match(rf"^[.:：]?({_MARKET_TOKEN})(?![a-z])", right)
    markets = [_MARKETS[match.group(1)] for match in (prefix, suffix) if match]
    if suffix is None and re.match(r"^[.:：][a-z]+", right):
        markets.append("UNKNOWN")
    return markets


def _name_identity_conflict(compact: str, row: tuple) -> bool:
    _security_id, _name, ticker, exchange, name = row
    market_words = {
        "沪股": "SSE", "深股": "SZSE", "京股": "BSE", "港股": "HKEX",
        # Compact titles are casefolded, so the Latin H-share qualifier is "h股".
        "h股": "HKEX", "美股": "US",
    }
    market_word = "|".join(market_words)
    for occurrence in re.finditer(re.escape(name), compact):
        left = compact[max(0, occurrence.start() - 2):occurrence.start()]
        if left in market_words and market_words[left] != exchange:
            return True
        tail = compact[occurrence.end():]
        # Only a market qualifier adjacent to this issuer applies to it;
        # another issuer or clause mentioning Hong Kong is not a conflict.
        suffix = re.match(rf"^(?:({market_word})|[（(]({market_word})[）)])", tail)
        if suffix and market_words[suffix.group(1) or suffix.group(2)] != exchange:
            return True
        if exchange != "HKEX" and _NAME_HK_CODE_RE.match(tail):
            return True
        code = _NAME_CODE_RE.match(tail)
        if code is not None and code.group("code") != ticker:
            return True
        market = re.match(rf"^[（(]({_MARKET_TOKEN})[）)]", tail)
        if market is not None and _MARKETS[market.group(1)] != exchange:
            return True
    return False


def _code_owner_names(compact: str, ticker: str, index: tuple) -> set[str]:
    """Find master names explicitly paired with a code, including code labels."""
    owners = set()
    for position in _candidate_positions(compact, index):
        other_name = index[0][position][4]
        for occurrence in re.finditer(re.escape(other_name), compact):
            qualifier = _NAME_CODE_RE.match(compact[occurrence.end():])
            if qualifier is not None and qualifier.group("code") == ticker:
                owners.add(other_name)
    return owners


def _ticker_evidence(compact: str, row: tuple, named: bool) -> tuple[bool, bool]:
    _security_id, _name, ticker, exchange, name = row
    anchored = False
    conflict = _name_identity_conflict(compact, row)
    for match in _CODE_RE.finditer(compact):
        if match.group() != ticker:
            continue
        markets = _code_markets(compact, match.start(), match.end())
        if any(market != exchange for market in markets):
            conflict = True
            continue
        left = compact[max(0, match.start() - len(name) - 12):match.start()]
        name_anchor = named and bool(re.search(re.escape(name) + r"[（(]$", left))
        anchored |= bool(markets or _CODE_LABEL_RE.search(left) or name_anchor)
    return anchored, conflict


def _resolve_candidate(raw_title: str, compact: str, row: tuple, index: tuple):
    # Lazy import avoids core's processor -> database initialization cycle.
    from core.asset_mapping import (
        is_direct_security_mention,
        is_direct_security_ticker_mention,
    )

    rows, names, tickers, _lengths = index
    security_id, name, ticker, exchange, normalized_name = row
    # Reuse existing attribution grammar for labelled as well as bare codes;
    # this normalization does not change the exact title returned to callers.
    attribution_text = re.sub(
        rf"([（(]){_ISSUER_CODE_LABEL}", r"\1", compact,
    )
    if _ticker_evidence(compact, row, False)[1]:
        return None, True
    named = normalized_name in compact and is_direct_security_mention(
        attribution_text, name, allow_qualified_ticker=True,
    )
    anchored, conflict = _ticker_evidence(compact, row, named)
    coded = anchored and is_direct_security_ticker_mention(
        attribution_text, name, ticker, exchange=exchange,
    )
    owners = _code_owner_names(compact, ticker, index)
    if conflict or owners - {normalized_name}:
        return None, True
    if owners and not named:
        # A labelled ticker cannot rescue a broker/data-provider attribution.
        coded = False
    if not named and not coded:
        return None, False
    ambiguous = (
        conflict or not _valid_a_share_row(row)
        or _identity_count(rows, names.get(normalized_name, ())) != 1
        or _identity_count(rows, tickers.get(ticker, ())) != 1
    )
    if ambiguous:
        return None, True
    return {
        "security_id": security_id, "name": name,
        "ticker": ticker, "exchange": exchange,
    }, False


def resolve_title_mentions(raw_title: str, index: tuple, *, limit: int = 5) -> dict[str, Any]:
    """Resolve a single title without I/O, confidence scores, or theme rules."""
    if not raw_title.strip():
        return mention_result(raw_title, "unavailable", "missing_title")
    if not index[0]:
        return mention_result(raw_title, "unavailable", "master_empty")
    compact = re.sub(r"\s+", "", raw_title).casefold()
    mentions = []
    ambiguous = False
    for position in sorted(_candidate_positions(compact, index)):
        mention, rejected = _resolve_candidate(raw_title, compact, index[0][position], index)
        ambiguous |= rejected
        if mention is not None and mention not in mentions:
            mentions.append(mention)
    reason = "ambiguous" if ambiguous else "matched" if mentions else "no_match"
    return mention_result(raw_title, "ok", reason, mentions[:max(1, min(20, int(limit)))])
