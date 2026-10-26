"""Exact, reviewed exceptions for ``ops/correct_event_mapping.py``.

The correction tool normally refuses a mapping written under a legacy
(non title-only) contract, evidence outside the current source contract,
merged members, and more than ``MAX_EVIDENCE`` evidence rows.  An entry here
lifts those refusals for one exact row only.  Every field is compared with the
live database and any difference fails closed, so an entry cannot approve a
neighbouring mapping, a newer revision, changed evidence or a changed group.

Adding or changing an entry is a code review decision, never a CLI flag or a
manifest field.  Entries do not authorize execution: each row still needs its
own manifest, a read-only dry-run plan digest and a new audit file.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

# Defense in depth: no entry may ever describe a larger evidence group.
LEGACY_EVIDENCE_CEILING = 64


@dataclass(frozen=True)
class LegacyCorrection:
    event_id: str
    event_version: int
    mapping_id: str
    revision: int
    mapping_version: str
    security_id: str
    security_label: str  # "<EXCHANGE>:<ticker>" of the one issuer being retired
    # (event_id, version) of the canonical root followed by every merged member.
    group: tuple[tuple[str, int], ...]
    evidence_count: int
    evidence_sha256: str
    # (mapping_id, revision) of every OTHER current mapping on the root; these
    # are left exactly as they are.
    retained_mappings: tuple[tuple[str, int], ...] = ()

    @property
    def key(self) -> tuple[str, str, int, str]:
        return (self.event_id, self.mapping_id, self.revision, self.mapping_version)


def evidence_sha256(rows: list[dict]) -> str:
    """Digest every stored column of the group's evidence, order independent."""
    ordered = sorted(rows, key=lambda row: str(row["evidence_id"]))
    payload = json.dumps(ordered, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


# Public distribution deliberately ships no production correction approvals.
# Synthetic nine-row examples live under tests/fixtures and are installed only
# inside isolated tests. Adding a real entry still requires an explicit review.
LEGACY_CORRECTIONS: tuple[LegacyCorrection, ...] = ()


LEGACY_ALLOWLIST: dict[tuple[str, str, int, str], LegacyCorrection] = {
    entry.key: entry for entry in LEGACY_CORRECTIONS
}


def lookup(event_id: object, mapping_id: object, revision: object, mapping_version: object):
    """Return the exact entry, or ``None``; never a partial/approximate match."""
    return LEGACY_ALLOWLIST.get((event_id, mapping_id, revision, mapping_version))
