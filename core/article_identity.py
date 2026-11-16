"""Article identity: the one place that decides "is this the same article?".

Three rules used to live in three modules and drifted apart:

* link normalisation for the durable event identity key
  (formerly ``db.database._normalized_article_identity`` / ``_safe_evidence_url``);
* recognition of publisher *section* pages that upstream feeds hand out as
  the URL of every item (formerly private helpers in ``routes.webhook_routes``,
  added for the CLS telegraph in #204);
* the canonical text form (NFC + strip) that evidence hashes are computed on
  (``db.event_core.normalize_evidence_text``, added in #203).

``db/event_core.py`` must stay a stdlib-only, single-file module: the host
restore drill (``ops/backup/restore_drill.py``) copies it into a
content-addressed bundle and loads it by path.  It therefore keeps its own
two-line ``normalize_evidence_text``; ``normalize_identity_text`` below is the
same rule, and ``tests/test_article_identity.py`` fails if the two drift.

This module is stdlib-only and imports nothing from the project, so the
webhook route and the database layer can both use it.  It is deliberately NOT part of
the scoring strategy fingerprint (``core.config._strategy_code_fingerprint``):
nothing here decides a score.  The score cache key in ``core/processor.py``
still uses the raw ``link`` string; that is intentionally left untouched so
that this module can change without creating a new score cohort.

Compatibility contract: ``article_identity_key`` must return byte-identical
digests for every input it accepted before, because those digests are stored
in ``event_identities``.  ``tests/test_article_identity.py`` pins known values.
"""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Any, Mapping, NamedTuple
from urllib.parse import urlencode, urlsplit, urlunsplit

ARTICLE_IDENTITY_TYPE = "article_url_sha256"
_ARTICLE_IDENTITY_DOMAIN = b"ai-filter-article-observation-v1\0"

# Publisher section pages that some upstream feeds (e.g. RSSHub
# ``/cls/telegraph``, which emits no <link>) hand to Miniflux as the URL of
# every item.  An explicit list is used on purpose instead of a heuristic;
# extend it via ``webhook_shared_section_urls`` in the config.
DEFAULT_SHARED_SECTION_URLS = ("https://www.cls.cn/telegraph",)
SYNTHETIC_ITEM_PARAM = "ai_filter_item"
_ITEM_KEY_CHARS = 128


class ArticleIdentity(NamedTuple):
    """Result of :func:`article_identity`."""

    link: str
    identity_type: str
    identity_key: str


def normalize_identity_text(value: object) -> str:
    """The one canonical text form: Unicode NFC, then strip.

    Every producer that hashes text which is later verified (evidence
    snapshots) or compared as identity (title fallback keys) must use exactly
    this form; otherwise visually identical text such as the CJK compatibility
    ideograph U+F90A and U+91D1 hashes differently.
    """
    if value is None:
        value = ""
    return unicodedata.normalize("NFC", str(value)).strip()


def _is_default_port(scheme: str, port: int | None) -> bool:
    return (scheme == "http" and port == 80) or (scheme == "https" and port == 443)


# Presentation-only query parameters: transports append them, but they never
# identify a different article.  Item identity parameters (for example
# ``ai_filter_item``) are deliberately absent from this set.
_TRACKING_QUERY_KEYS = frozenset(
    {
        "from",
        "spm",
        "ref",
        "ref_src",
        "fbclid",
        "gclid",
        "yclid",
        "msclkid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "share_source",
        "share_token",
    }
)


def _strip_tracking_query(query: str) -> str:
    """Drop known tracking parameters, preserving order and every other key."""
    if not query:
        return ""
    kept = []
    for part in query.split("&"):
        if not part:
            continue
        key = part.split("=", 1)[0].strip().lower()
        if key.startswith("utm_") or key in _TRACKING_QUERY_KEYS:
            continue
        kept.append(part)
    return "&".join(kept)


def _canonical_article_path(path: str) -> str:
    """Fold trailing slashes only: HTTP path letter case is identity-bearing.

    Short links and base64-style ids can differ only in case, so lowercasing
    the path would merge two distinct articles into one event.  That merge is
    irreversible and overwrites the first article's title and priority, so a
    missed merge is preferred here.
    """
    normalized = path or "/"
    if normalized != "/":
        normalized = normalized.rstrip("/") or "/"
    return normalized


def canonical_article_url(link: object) -> str:
    """Return the normalised form that the article identity digest covers.

    HTTP(S) URLs get a lower-case scheme, an IDNA lower-case host, no default
    port, ``/`` for an empty path and no fragment.  Tracking query parameters
    are removed while item identity parameters (e.g. ``?ai_filter_item=...``)
    and every other key are kept; trailing slashes are folded.  Anything
    that is not a parseable HTTP(S) URL is returned stripped but otherwise
    unchanged, which keeps legacy/non-URL inputs deterministic.
    """
    raw = str(link or "").strip()
    if not raw:
        raise ValueError("article link is required for stable event identity")
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        if scheme in {"http", "https"} and parsed.hostname:
            hostname = parsed.hostname.encode("idna").decode("ascii").lower()
            port = parsed.port
            netloc = hostname if not port or _is_default_port(scheme, port) else f"{hostname}:{port}"
            return urlunsplit(
                (
                    scheme,
                    netloc,
                    _canonical_article_path(parsed.path),
                    _strip_tracking_query(parsed.query),
                    "",
                )
            )
    except (UnicodeError, ValueError):
        # Non-URL fixture or malformed legacy input: exact normalized text is
        # still deterministic and is never fetched by the event core.
        pass
    return raw


def article_identity_key(link: object) -> tuple[str, str]:
    """Durable observation key for ``event_identities``; hides the source URL.

    The digest is an external identity lookup key only.  The event's public,
    long-lived identity is the UUID stored in ``events.event_id``.
    """
    normalized = canonical_article_url(link)
    digest = hashlib.sha256(
        _ARTICLE_IDENTITY_DOMAIN + normalized.encode("utf-8", errors="replace")
    ).hexdigest()
    return ARTICLE_IDENTITY_TYPE, digest


def is_safe_article_url(link: object) -> bool:
    """True for a credential-free absolute HTTP(S) URL with a host."""
    try:
        parsed = urlsplit(str(link or "").strip())
    except ValueError:
        return False
    return bool(
        parsed.scheme.lower() in {"http", "https"}
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
    )


def section_url_key(url: object) -> str | None:
    """``host[:port]/path`` for a bare section URL, else ``None``.

    URLs that already carry a query, fragment or credentials are never treated
    as a section page, so an existing per-item link is not rewritten again.
    """
    try:
        parts = urlsplit(str(url or "").strip())
        host = (parts.hostname or "").lower()
        port = parts.port
    except (TypeError, ValueError):
        return None
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not host:
        return None
    if parts.query or parts.fragment or parts.username or parts.password:
        return None
    if ":" in host:
        host = f"[{host}]"
    if port is not None and not _is_default_port(scheme, port):
        host = f"{host}:{port}"
    return f"{host}{parts.path.rstrip('/') or '/'}"


def shared_section_urls(cfg: Any) -> dict[str, str]:
    """Map equivalent section spellings to the first configured canonical URL."""
    configured = (cfg or {}).get("webhook_shared_section_urls") if isinstance(cfg, dict) else None
    urls = configured if isinstance(configured, (list, tuple)) else DEFAULT_SHARED_SECTION_URLS
    shared: dict[str, str] = {}
    for url in urls:
        key = section_url_key(url)
        if key:
            scheme = urlsplit(str(url).strip()).scheme.lower()
            shared.setdefault(key, f"{scheme}://{key}")
    return shared


def upstream_item_key(entry: Mapping[str, Any], title: object) -> str:
    """Stable per-item key: Miniflux entry hash, else entry id, else title digest.

    The title fallback hashes the NFC form, so the same headline delivered in
    a different Unicode normalisation form keeps the same identity.
    """
    for field in ("hash", "id"):
        value = entry.get(field)
        if isinstance(value, bool) or value is None:
            continue
        text = str(value).strip()
        if text:
            return f"{field[0]}-{text[:_ITEM_KEY_CHARS]}"
    digest = hashlib.sha256(normalize_identity_text(title).encode("utf-8")).hexdigest()
    return f"t-{digest[:32]}"


def per_item_link(
    entry: Mapping[str, Any], url: str, title: object, shared: Mapping[str, str]
) -> str:
    """Return ``url`` or, for a shared section page, a stable per-item link.

    A query parameter is used rather than a ``#fragment`` because
    :func:`canonical_article_url` drops fragments, which would collapse
    distinct items into one event again.
    """
    key = section_url_key(url)
    if not key or key not in shared:
        return url
    item = upstream_item_key(entry, title)
    return f"{shared[key]}?{urlencode({SYNTHETIC_ITEM_PARAM: item})}"


def article_identity(
    link: str,
    *,
    entry: Mapping[str, Any] | None = None,
    title: object = "",
    shared: Mapping[str, str] | None = None,
) -> ArticleIdentity:
    """All three rules in one call: section page -> per-item link -> digest.

    Production applies the two halves at different times (the webhook rewrites
    the link when it accepts a delivery; the worker derives the digest when it
    persists the event), so the runtime calls :func:`per_item_link` and
    :func:`article_identity_key` directly.  This entry point exists so callers
    and tests can ask "what identity would this upstream item get?" without
    re-composing the rules by hand.
    """
    resolved = link
    if entry is not None:
        resolved = per_item_link(entry, link, title, shared if shared is not None else shared_section_urls(None))
    identity_type, identity_key = article_identity_key(resolved)
    return ArticleIdentity(resolved, identity_type, identity_key)
