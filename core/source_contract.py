"""Explicit identities for the forward-only publisher-title write contract.

The event-store generation remains independent: adopting these identities does
not authorize rematerializing old evidence or changing its frozen payload.
"""

from __future__ import annotations

import re
from collections.abc import Iterable


ARTICLE_SOURCE_CONTRACT = "publisher-title-v2"
ARTICLE_CLAIM_PREFIX = f"article_snapshot:{ARTICLE_SOURCE_CONTRACT}:"
MAPPING_CLAIM_PREFIX = f"mapping:{ARTICLE_SOURCE_CONTRACT}:"
MAPPING_WRITER_MARKER = "title-only-v2"
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_UUID_TOKEN = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_MAPPING_CLAIM_RE = re.compile(rf"{_UUID_TOKEN}:{_UUID_TOKEN}\Z")


def is_current_article_claim(claim_key: object) -> bool:
    """Require a stored, versioned title identity; never infer it from dates."""
    key = str(claim_key or "")
    return key.startswith(ARTICLE_CLAIM_PREFIX) and bool(
        _DIGEST_RE.fullmatch(key[len(ARTICLE_CLAIM_PREFIX):])
    )


def is_current_source_claim(claim_key: object) -> bool:
    """Recognize original-title and bound-mapping identities, not legacy keys."""
    key = str(claim_key or "")
    return is_current_article_claim(key) or (
        key.startswith(MAPPING_CLAIM_PREFIX)
        and bool(_MAPPING_CLAIM_RE.fullmatch(key[len(MAPPING_CLAIM_PREFIX):]))
    )


def is_current_mapping_writer(mapping_version: object) -> bool:
    """Identify the writer contract, separately from active rules/master state."""
    version = str(mapping_version or "")
    return version.startswith(f"asset-map:{MAPPING_WRITER_MARKER}:") or (
        version.startswith("asset-map:")
        and f":writer-{MAPPING_WRITER_MARKER}:" in version
    )


def mapping_contract_status(
    mapping_version: object, *, current_versions: Iterable[str] = (),
) -> str:
    """Keep writer provenance distinct from exact active contract equality."""
    version = str(mapping_version or "")
    if not is_current_mapping_writer(version):
        return "legacy_unknown"
    return "current" if version in current_versions else "historical"
