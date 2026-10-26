"""Deterministic, rebuildable publisher/claim provenance projection.

This module is intentionally outside the production scoring and mapping write
paths.  It reads only stable base event evidence (``mapping_id IS NULL``) and
writes only the optional tables owned by :mod:`db.provenance_shadow`.
"""

from __future__ import annotations

import hashlib
import ipaddress
import itertools
import json
import re
import sqlite3
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from html import unescape as html_unescape
from urllib.parse import urlsplit

from db.event_core import canonical_event_group, canonical_event_id
from db.provenance_shadow import (
    ProvenanceShadowSchemaError,
    init_provenance_shadow_schema,
    verify_provenance_shadow_schema,
)


PROVENANCE_PROJECTION_ALGORITHM = "publisher-claim-lineage-shadow-v1"

_SOURCE_CLASSES = frozenset(
    {
        "official_record",
        "issuer_first_party",
        "wire",
        "newsroom",
        "analysis",
        "aggregator",
        "social",
        "unknown",
    }
)
_CLAIM_STATES = (
    "single_support",
    "multi_independent_support",
    "contested",
    "contradicted_only",
    "context_only",
)
_GROUP_RE = re.compile(r"[a-z0-9](?:[a-z0-9._:-]{0,127})")
_HOST_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_DUPLICATE_PREFIX_RE = re.compile(r"^[\[【（(]?\s*[⭐★]?[\d.]+\s*分?\s*[📈📉]?")
_NON_WORD_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
_COMMON_TWO_LABEL_PUBLIC_SUFFIXES = frozenset(
    {
        "ac.cn",
        "ac.uk",
        "co.in",
        "co.jp",
        "co.kr",
        "co.nz",
        "co.uk",
        "com.au",
        "com.cn",
        "com.hk",
        "com.my",
        "com.sg",
        "com.tw",
        "edu.cn",
        "gov.cn",
        "gov.uk",
        "mil.cn",
        "net.au",
        "net.cn",
        "org.au",
        "org.cn",
        "org.uk",
    }
)
_INTERNAL_SOURCE_HOSTS = frozenset({"localhost", "rsshub", "::1"})
_SAVEPOINTS = itertools.count()


class ProvenanceShadowConfigError(ValueError):
    """The deterministic publisher registry is ambiguous or malformed."""


class ProvenanceShadowBoundsError(RuntimeError):
    """A requested projection exceeds its explicit work bound."""


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _scope_json(scopes: Sequence[str]) -> str:
    return json.dumps(
        list(scopes),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _host(value: object) -> str:
    try:
        raw = str(value or "").strip().rstrip(".")
        if not raw:
            return ""
        try:
            return str(ipaddress.ip_address(raw))
        except ValueError:
            pass
        normalized = raw.encode("idna").decode("ascii").casefold()
        labels = normalized.split(".")
        if any(not _HOST_LABEL_RE.fullmatch(label) for label in labels):
            return ""
        return normalized
    except (UnicodeError, ValueError):
        return ""


def _uri_host(uri: object) -> str:
    try:
        parsed = urlsplit(str(uri or ""))
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"}:
        return ""
    if parsed.username is not None or parsed.password is not None:
        return ""
    return _host(parsed.hostname)


def _registrable_domain(host: str) -> str:
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return ""
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) < 2:
        return ""
    suffix = ".".join(labels[-2:])
    if suffix in _COMMON_TWO_LABEL_PUBLIC_SUFFIXES:
        return ".".join(labels[-3:]) if len(labels) >= 3 else ""
    return suffix


def _is_internal_host(host: str) -> bool:
    if not host:
        return False
    if host in _INTERNAL_SOURCE_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(host)
        return address.is_private or address.is_loopback
    except ValueError:
        return False


def _domain(value: object) -> str:
    raw = str(value or "").strip().casefold().lstrip(".").rstrip(".")
    normalized = _host(raw)
    if not normalized or _is_internal_host(normalized):
        raise ProvenanceShadowConfigError("publisher domains must be public DNS names")
    return normalized


def _route(value: object) -> str:
    route = str(value or "").strip()
    if not route.startswith("/") or "?" in route or "#" in route:
        raise ProvenanceShadowConfigError("RSS routes must be absolute paths")
    route = re.sub(r"/{2,}", "/", route)
    if len(route) > 512:
        raise ProvenanceShadowConfigError("RSS route is too long")
    return route.rstrip("/") or "/"


def _section(config: Mapping[str, object] | None) -> Mapping[str, object]:
    if config is None:
        return {}
    if not isinstance(config, Mapping):
        raise ProvenanceShadowConfigError("source_provenance config must be a mapping")
    if "source_provenance" in config:
        value = config["source_provenance"]
        if not isinstance(value, Mapping):
            raise ProvenanceShadowConfigError("source_provenance must be a mapping")
        return value
    # A compact section can be passed directly by tests and maintenance jobs.
    if set(config).issubset({"rules"}):
        return config
    return {}


def _normalize_registry(config: Mapping[str, object] | None) -> dict[str, object]:
    section = _section(config)
    unknown = set(section).difference({"rules"})
    if unknown:
        raise ProvenanceShadowConfigError(
            "unknown source_provenance keys: " + ", ".join(sorted(map(str, unknown)))
        )
    raw_rules = section.get("rules", [])
    if raw_rules is None:
        raw_rules = []
    if not isinstance(raw_rules, Sequence) or isinstance(raw_rules, (str, bytes)):
        raise ProvenanceShadowConfigError("source_provenance.rules must be a list")
    if len(raw_rules) > 500:
        raise ProvenanceShadowConfigError("source_provenance.rules exceeds 500 entries")

    domains: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    routes: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, Mapping):
            raise ProvenanceShadowConfigError("each provenance rule must be a mapping")
        extra = set(raw_rule).difference(
            {"publisher_group", "domains", "rss_routes", "source_class", "first_party_scopes"}
        )
        if extra:
            raise ProvenanceShadowConfigError(
                "unknown provenance rule keys: " + ", ".join(sorted(map(str, extra)))
            )
        group = str(raw_rule.get("publisher_group") or "").strip().casefold()
        if not _GROUP_RE.fullmatch(group):
            raise ProvenanceShadowConfigError("publisher_group must be a stable ASCII slug")
        source_class = str(raw_rule.get("source_class") or "unknown").strip().casefold()
        if source_class not in _SOURCE_CLASSES:
            raise ProvenanceShadowConfigError("unsupported source_class")
        raw_scopes = raw_rule.get("first_party_scopes", [])
        if not isinstance(raw_scopes, Sequence) or isinstance(raw_scopes, (str, bytes)):
            raise ProvenanceShadowConfigError("first_party_scopes must be a list")
        scopes = tuple(
            sorted(
                {
                    str(item).strip().casefold()
                    for item in raw_scopes
                    if str(item).strip()
                }
            )
        )
        if len(scopes) > 50 or any(len(scope) > 128 for scope in scopes):
            raise ProvenanceShadowConfigError("first_party_scopes exceeds its bound")
        if len(_scope_json(scopes)) > 4_000:
            raise ProvenanceShadowConfigError(
                "first_party_scopes serialized form exceeds 4000 characters"
            )
        metadata = (group, source_class, scopes)
        raw_domains = raw_rule.get("domains", [])
        raw_routes = raw_rule.get("rss_routes", [])
        for values, label in ((raw_domains, "domains"), (raw_routes, "rss_routes")):
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                raise ProvenanceShadowConfigError(f"{label} must be a list")
        if not raw_domains and not raw_routes:
            raise ProvenanceShadowConfigError("a provenance rule needs a domain or RSS route")
        for item in raw_domains:
            key = _domain(item)
            if key in domains and domains[key] != metadata:
                raise ProvenanceShadowConfigError(f"conflicting publisher domain: {key}")
            domains[key] = metadata
        for item in raw_routes:
            key = _route(item)
            if key in routes and routes[key] != metadata:
                raise ProvenanceShadowConfigError(f"conflicting RSS route: {key}")
            routes[key] = metadata

    canonical = {
        "domains": [
            {"match": key, "group": val[0], "class": val[1], "scopes": list(val[2])}
            for key, val in sorted(domains.items())
        ],
        "routes": [
            {"match": key, "group": val[0], "class": val[1], "scopes": list(val[2])}
            for key, val in sorted(routes.items())
        ],
    }
    return {"domains": domains, "routes": routes, "canonical": canonical}


def provenance_projection_version(config: Mapping[str, object] | None = None) -> str:
    """Return an order-independent version for only the sidecar contract."""
    registry = _normalize_registry(config)
    digest = _fingerprint(
        {"algorithm": PROVENANCE_PROJECTION_ALGORITHM, "registry": registry["canonical"]}
    )
    return f"{PROVENANCE_PROJECTION_ALGORITHM}:{digest}"


def _trusted_route_candidates(source_uri: object, source_name: object) -> tuple[str, ...]:
    """Return only routes whose transport identity makes them trustworthy."""
    paths: set[str] = set()
    try:
        parsed_uri = urlsplit(str(source_uri or ""))
        path = parsed_uri.path
        if path and _is_internal_host(_host(parsed_uri.hostname)):
            paths.add(re.sub(r"/{2,}", "/", path))
    except ValueError:
        pass
    raw_name = str(source_name or "").strip()
    if raw_name:
        try:
            parsed = urlsplit(raw_name)
        except ValueError:
            parsed = None
        if (
            parsed is not None
            and parsed.scheme.casefold() in {"http", "https"}
            and _is_internal_host(_host(parsed.hostname))
            and parsed.path
        ):
            paths.add(re.sub(r"/{2,}", "/", parsed.path))
    return tuple(sorted(paths))


def _path_matches(path: str, route: str) -> bool:
    return path == route or path.startswith(route + "/")


def resolve_publisher(
    source_uri: object,
    source_name: object = "",
    *,
    config: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Resolve one publisher without DNS, HTTP, a model, or mutable state."""
    registry = _normalize_registry(config)
    return _resolve_publisher_with_registry(source_uri, source_name, registry=registry)


def _resolve_publisher_with_registry(
    source_uri: object,
    source_name: object = "",
    *,
    registry: Mapping[str, object],
) -> dict[str, object]:
    """Resolve against a caller-owned normalized registry, without mutating it."""
    host = _uri_host(source_uri)
    domain_identity = _publisher_domain_identity(host, registry=registry)
    if domain_identity is not None:
        return domain_identity
    return _publisher_route_identity(
        host, _trusted_route_candidates(source_uri, source_name), registry=registry,
    )


def _publisher_domain_identity(
    host: str, *, registry: Mapping[str, object],
) -> dict[str, object] | None:
    """Resolve a normalized host before considering any transport routes."""
    domain_matches = [
        (domain, metadata)
        for domain, metadata in registry["domains"].items()  # type: ignore[union-attr]
        if host == domain or host.endswith("." + domain)
    ]
    if domain_matches:
        _match, metadata = max(domain_matches, key=lambda item: (len(item[0]), item[0]))
        group, source_class, scopes = metadata
        return {
            "publisher_group": group,
            "publisher_basis": "registry_domain",
            "source_class": source_class,
            "first_party_scopes": list(scopes),
        }
    return None


def _resolve_publisher_identity(
    host: str, trusted_routes: tuple[str, ...], *, registry: Mapping[str, object],
) -> dict[str, object]:
    """Resolve already parsed identity inputs owned by a read-only caller.

    The caller must normalize the host and supply only trusted internal routes.
    Article paths, ports and query strings are deliberately not identity keys.
    """
    domain_identity = _publisher_domain_identity(host, registry=registry)
    if domain_identity is not None:
        return domain_identity
    return _publisher_route_identity(host, trusted_routes, registry=registry)


def _publisher_route_identity(
    host: str, trusted_routes: tuple[str, ...], *, registry: Mapping[str, object],
) -> dict[str, object]:

    # Route declarations are trusted only behind a local/RSSHub transport.
    # A public site cannot masquerade as another publisher by choosing /cls/.
    route_matches: list[tuple[str, tuple[str, str, tuple[str, ...]]]] = []
    for path in trusted_routes:
        route_matches.extend(
            (route, metadata)
            for route, metadata in registry["routes"].items()  # type: ignore[union-attr]
            if _path_matches(path, route)
        )
    if route_matches:
        _match, metadata = max(route_matches, key=lambda item: (len(item[0]), item[0]))
        group, source_class, scopes = metadata
        return {
            "publisher_group": group,
            "publisher_basis": "registry_route",
            "source_class": source_class,
            "first_party_scopes": list(scopes),
        }

    publisher_domain = _registrable_domain(host)
    if publisher_domain:
        domain_group = f"domain:{publisher_domain}"
        if len(domain_group) > 128:
            domain_group = "domainhash:" + _fingerprint({"domain": publisher_domain})
        return {
            "publisher_group": domain_group,
            "publisher_basis": "registrable_domain",
            "source_class": "unknown",
            "first_party_scopes": [],
        }
    return {
        "publisher_group": "unknown",
        "publisher_basis": "unknown",
        "source_class": "unknown",
        "first_party_scopes": [],
    }


def _row_dict(row: sqlite3.Row | Sequence[object]) -> dict[str, object]:
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    raise TypeError("provenance projection requires sqlite3.Row connections")


def _claim_identity(row: Mapping[str, object]) -> tuple[str, str]:
    text = html_unescape(str(row.get("claim_text") or ""))
    text = re.sub(
        r"</?(?:em|strong|b|i|mark)\b[^>]*>",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = _DUPLICATE_PREFIX_RE.sub("", text)
    text = re.sub(
        r"^(财联社|格隆汇|华尔街见闻|金十数据)?\s*\d{1,2}月\d{1,2}日电[，,]?",
        "",
        text,
    )
    text = re.sub(r"^(财联社|格隆汇|华尔街见闻|金十数据)[：:，,\s]*", "", text)
    normalized = _NON_WORD_RE.sub("", text).lower()
    if len(normalized) >= 12:
        return _fingerprint({"claim": normalized}), "exact_normalized_title"
    # Short claims are too collision-prone to manufacture corroboration.
    return (
        _fingerprint({"claim": normalized, "evidence_id": str(row["evidence_id"])}),
        "isolated_short_claim",
    )


def _evidence_input_fingerprint(
    row: Mapping[str, object], projection_version: str
) -> str:
    return _fingerprint(
        {
            "projection_version": projection_version,
            "evidence_id": str(row["evidence_id"]),
            "version": int(row["version"]),
            "event_id": str(row["event_id"]),
            "source_uri": str(row["source_uri"]),
            "source_type": str(row["source_type"]),
            "source_name": str(row["source_name"]),
            "claim_key": str(row["claim_key"]),
            "claim_text": str(row["claim_text"]),
            "snapshot_hash": str(row["snapshot_hash"]),
            "stance": str(row["stance"]),
            "announcement_at": row["announcement_at"],
            "observed_at": int(row["observed_at"]),
        }
    )


def _project_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    root_event_id: str,
    root_event_version: int,
    projection_version: str,
    registry_config: Mapping[str, object] | None,
    computed_at: int,
) -> list[dict[str, object]]:
    projected: list[dict[str, object]] = []
    for row in rows:
        claim_fingerprint, claim_basis = _claim_identity(row)
        publisher = resolve_publisher(
            row["source_uri"], row["source_name"], config=registry_config
        )
        snapshot_hash = str(row["snapshot_hash"] or "").casefold()
        if _HASH_RE.fullmatch(snapshot_hash):
            lineage_cluster = "snapshot:" + snapshot_hash
            lineage_basis = "exact_snapshot"
        else:
            lineage_cluster = "unique:" + _fingerprint(
                {"evidence_id": str(row["evidence_id"])}
            )
            lineage_basis = "unique_evidence"
        projected.append(
            {
                "evidence_id": str(row["evidence_id"]),
                "evidence_version": int(row["version"]),
                "event_id": str(row["event_id"]),
                "root_event_id": root_event_id,
                "root_event_version": root_event_version,
                "projection_version": projection_version,
                "claim_fingerprint": claim_fingerprint,
                "claim_basis": claim_basis,
                **publisher,
                "first_party_scope_json": _scope_json(
                    publisher["first_party_scopes"]  # type: ignore[arg-type]
                ),
                "lineage_cluster": lineage_cluster,
                "lineage_basis": lineage_basis,
                "input_fingerprint": _evidence_input_fingerprint(row, projection_version),
                "stance": str(row["stance"]),
                "announcement_at": row["announcement_at"],
                "observed_at": int(row["observed_at"]),
                "computed_at": computed_at,
            }
        )
    representatives: dict[str, str] = {}
    for lineage, members in _groups(projected, "lineage_cluster").items():
        representative = min(
            members,
            key=lambda item: (
                int(item["announcement_at"])
                if item["announcement_at"] is not None
                else int(item["observed_at"]),
                int(item["observed_at"]),
                str(item["evidence_id"]),
            ),
        )
        representatives[lineage] = str(representative["evidence_id"])
    for row in projected:
        row["lineage_representative_evidence_id"] = representatives[
            str(row["lineage_cluster"])
        ]
    return projected


def _groups(
    rows: Sequence[dict[str, object]], key: str
) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[str(row[key])].append(row)
    return dict(grouped)


def _independent_units(rows: Sequence[dict[str, object]]) -> int:
    if not rows:
        return 0
    parents = list(range(len(rows)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    publisher_owner: dict[str, int] = {}
    lineage_owner: dict[str, int] = {}
    for index, row in enumerate(rows):
        publisher = str(row["publisher_group"])
        if publisher != "unknown":
            if publisher in publisher_owner:
                union(index, publisher_owner[publisher])
            else:
                publisher_owner[publisher] = index
        lineage = str(row["lineage_cluster"])
        if str(row["lineage_basis"]) == "exact_snapshot":
            if lineage in lineage_owner:
                union(index, lineage_owner[lineage])
            else:
                lineage_owner[lineage] = index

    # Unknown publishers never create an independent unit.  They can collapse
    # into a known unit through an identical snapshot, but cannot unlock multi.
    known_roots = {
        find(index)
        for index, row in enumerate(rows)
        if str(row["publisher_group"]) != "unknown"
    }
    return len(known_roots)


def _claim_rows(
    projected: Sequence[dict[str, object]],
    *,
    root_event_id: str,
    root_event_version: int,
    projection_version: str,
    computed_at: int,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for claim_fingerprint, rows in sorted(_groups(projected, "claim_fingerprint").items()):
        support = [row for row in rows if row["stance"] == "supports"]
        contradict = [row for row in rows if row["stance"] == "contradicts"]
        context = [row for row in rows if row["stance"] == "context"]
        support_units = _independent_units(support)
        contradict_units = _independent_units(contradict)
        if support and contradict:
            state = "contested"
        elif contradict:
            state = "contradicted_only"
        elif support_units >= 2:
            state = "multi_independent_support"
        elif support:
            state = "single_support"
        else:
            state = "context_only"
        evidence_times = [
            int(row["announcement_at"])
            if row["announcement_at"] is not None
            else int(row["observed_at"])
            for row in rows
        ]
        result.append(
            {
                "root_event_id": root_event_id,
                "root_event_version": root_event_version,
                "claim_fingerprint": claim_fingerprint,
                "projection_version": projection_version,
                "input_fingerprint": _fingerprint(
                    {
                        "root_event_id": root_event_id,
                        "root_event_version": root_event_version,
                        "projection_version": projection_version,
                        "evidence_inputs": sorted(
                            str(row["input_fingerprint"]) for row in rows
                        ),
                    }
                ),
                "state": state,
                "support_evidence_count": len(support),
                "support_publisher_count": len(
                    {str(row["publisher_group"]) for row in support}
                    - {"unknown"}
                ),
                "support_independent_units": support_units,
                "contradict_evidence_count": len(contradict),
                "contradict_publisher_count": len(
                    {str(row["publisher_group"]) for row in contradict}
                    - {"unknown"}
                ),
                "contradict_independent_units": contradict_units,
                "context_evidence_count": len(context),
                "official_record_support_count": sum(
                    row["source_class"] == "official_record" for row in support
                ),
                # V1 has no audited event-subject identity.  Publisher class
                # and declared scopes are frozen metadata only, never proof
                # that this source is first-party for this particular event.
                "first_party_support_count": 0,
                "unclassified_evidence_count": sum(
                    row["publisher_group"] == "unknown" or row["source_class"] == "unknown"
                    for row in rows
                ),
                "first_evidence_at": min(evidence_times),
                "latest_evidence_at": max(evidence_times),
                "computed_at": computed_at,
            }
        )
    return result


@contextmanager
def _atomic(conn: sqlite3.Connection):
    if conn.in_transaction:
        name = f"provenance_project_{next(_SAVEPOINTS)}"
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


_EVIDENCE_SELECT = (
    "SELECT evidence_id, event_id, source_uri, source_type, source_name, "
    "claim_key, claim_text, snapshot_hash, stance, announcement_at, "
    "observed_at, version FROM event_evidence"
)


def _base_evidence_for_group(
    conn: sqlite3.Connection,
    members: Sequence[str],
    *,
    row_limit: int,
) -> list[dict[str, object]]:
    if isinstance(row_limit, bool) or not isinstance(row_limit, int) or row_limit < 1:
        raise ProvenanceShadowBoundsError("base evidence row_limit must be positive")
    placeholders = ",".join("?" for _ in members)
    rows = conn.execute(
        _EVIDENCE_SELECT
        + f" WHERE mapping_id IS NULL AND event_id IN ({placeholders}) "
        "ORDER BY observed_at, evidence_id LIMIT ?",
        (*members, row_limit),
    ).fetchall()
    return [_row_dict(row) for row in rows]


def _root_candidates(
    conn: sqlite3.Connection,
    projection_version: str,
    *,
    evidence_scan_limit: int,
) -> tuple[list[str], bool]:
    # Rank canonical roots, not evidence rows or merged members.  Otherwise a
    # single oversized root can fill every bounded candidate slot forever and
    # prevent a later healthy root from making progress.
    rows = conn.execute(
        "WITH RECURSIVE canonical_members(root_event_id, event_id, depth) AS ("
        " SELECT event_id, event_id, 0 FROM events "
        " WHERE merge_state = 'active' AND merged_into IS NULL "
        " UNION ALL "
        " SELECT parent.root_event_id, child.event_id, parent.depth + 1 "
        " FROM canonical_members AS parent "
        " JOIN events AS child ON child.merged_into = parent.event_id "
        " WHERE parent.depth < 64"
        "), candidate_lanes(root_event_id, lane, ordering_at) AS ("
        " SELECT members.root_event_id, 0, MIN(e.observed_at) "
        " FROM canonical_members AS members "
        " JOIN event_evidence AS e ON e.event_id = members.event_id "
        " LEFT JOIN event_evidence_provenance_shadow AS p "
        " ON p.evidence_id = e.evidence_id AND p.projection_version = ? "
        " WHERE e.mapping_id IS NULL AND (p.evidence_id IS NULL "
        " OR p.evidence_version <> e.version OR p.event_id <> e.event_id) "
        " GROUP BY members.root_event_id "
        " UNION ALL "
        " SELECT members.root_event_id, 1, MIN(p.computed_at) "
        " FROM canonical_members AS members "
        " JOIN event_evidence_provenance_shadow AS p "
        " ON p.event_id = members.event_id "
        " LEFT JOIN event_evidence AS current_base "
        " ON current_base.evidence_id = p.evidence_id "
        " AND current_base.mapping_id IS NULL "
        " WHERE p.projection_version = ? AND "
        " (current_base.evidence_id IS NULL "
        " OR current_base.event_id <> p.event_id) "
        " GROUP BY members.root_event_id "
        " UNION ALL "
        " SELECT members.root_event_id, 2, MIN(p.computed_at) "
        " FROM canonical_members AS members "
        " JOIN event_evidence_provenance_shadow AS p "
        " ON p.event_id = members.event_id "
        " JOIN events AS prior_root ON prior_root.event_id = p.root_event_id "
        " WHERE p.projection_version = ? AND "
        " (prior_root.merge_state <> 'active' "
        " OR prior_root.version <> p.root_event_version) "
        " GROUP BY members.root_event_id "
        " UNION ALL "
        " SELECT members.root_event_id, 3, MIN(p.computed_at) "
        " FROM canonical_members AS members "
        " JOIN event_evidence_provenance_shadow AS p "
        " ON p.event_id = members.event_id "
        " LEFT JOIN event_claim_state_shadow AS c "
        " ON c.root_event_id = p.root_event_id "
        " AND c.claim_fingerprint = p.claim_fingerprint "
        " AND c.projection_version = p.projection_version "
        " WHERE p.projection_version = ? AND c.root_event_id IS NULL "
        " GROUP BY members.root_event_id"
        "), ranked_roots AS ("
        " SELECT root_event_id, MIN(lane) AS first_lane, "
        " MIN(ordering_at) AS first_ordering_at FROM candidate_lanes "
        " GROUP BY root_event_id"
        ") SELECT root_event_id FROM ranked_roots "
        "ORDER BY first_lane, first_ordering_at, root_event_id LIMIT ?",
        (
            projection_version,
            projection_version,
            projection_version,
            projection_version,
            evidence_scan_limit + 1,
        ),
    ).fetchall()
    return (
        [str(row[0]) for row in rows[:evidence_scan_limit]],
        len(rows) > evidence_scan_limit,
    )


def _write_root_projection(
    conn: sqlite3.Connection,
    *,
    root_event_id: str,
    members: Sequence[str],
    projected: Sequence[dict[str, object]],
    claims: Sequence[dict[str, object]],
    projection_version: str,
) -> None:
    for row in projected:
        conn.execute(
            "INSERT INTO event_evidence_provenance_shadow ("
            "evidence_id, evidence_version, event_id, root_event_id, root_event_version, "
            "projection_version, claim_fingerprint, claim_basis, publisher_group, "
            "publisher_basis, source_class, first_party_scope_json, lineage_cluster, "
            "lineage_representative_evidence_id, lineage_basis, input_fingerprint, computed_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(evidence_id, projection_version) DO UPDATE SET "
            "evidence_version=excluded.evidence_version, event_id=excluded.event_id, "
            "root_event_id=excluded.root_event_id, root_event_version=excluded.root_event_version, "
            "claim_fingerprint=excluded.claim_fingerprint, claim_basis=excluded.claim_basis, "
            "publisher_group=excluded.publisher_group, publisher_basis=excluded.publisher_basis, "
            "source_class=excluded.source_class, first_party_scope_json=excluded.first_party_scope_json, "
            "lineage_cluster=excluded.lineage_cluster, "
            "lineage_representative_evidence_id=excluded.lineage_representative_evidence_id, "
            "lineage_basis=excluded.lineage_basis, input_fingerprint=excluded.input_fingerprint, "
            "computed_at=excluded.computed_at",
            tuple(
                row[key]
                for key in (
                    "evidence_id", "evidence_version", "event_id", "root_event_id",
                    "root_event_version", "projection_version", "claim_fingerprint",
                    "claim_basis", "publisher_group", "publisher_basis", "source_class",
                    "first_party_scope_json", "lineage_cluster",
                    "lineage_representative_evidence_id", "lineage_basis",
                    "input_fingerprint", "computed_at",
                )
            ),
        )
    member_placeholders = ",".join("?" for _ in members)
    expected_evidence_ids = tuple(str(row["evidence_id"]) for row in projected)
    if expected_evidence_ids:
        expected_placeholders = ",".join("?" for _ in expected_evidence_ids)
        conn.execute(
            "DELETE FROM event_evidence_provenance_shadow "
            "WHERE projection_version = ? "
            f"AND event_id IN ({member_placeholders}) "
            f"AND evidence_id NOT IN ({expected_placeholders})",
            (projection_version, *members, *expected_evidence_ids),
        )
    else:
        conn.execute(
            "DELETE FROM event_evidence_provenance_shadow "
            "WHERE projection_version = ? "
            f"AND event_id IN ({member_placeholders})",
            (projection_version, *members),
        )
    conn.execute(
        "DELETE FROM event_claim_state_shadow WHERE projection_version = ? "
        f"AND root_event_id IN ({member_placeholders})",
        (projection_version, *members),
    )
    for row in claims:
        conn.execute(
            "INSERT INTO event_claim_state_shadow ("
            "root_event_id, root_event_version, claim_fingerprint, projection_version, "
            "input_fingerprint, state, support_evidence_count, support_publisher_count, "
            "support_independent_units, contradict_evidence_count, "
            "contradict_publisher_count, contradict_independent_units, context_evidence_count, "
            "official_record_support_count, first_party_support_count, "
            "unclassified_evidence_count, first_evidence_at, latest_evidence_at, computed_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(
                row[key]
                for key in (
                    "root_event_id", "root_event_version", "claim_fingerprint",
                    "projection_version", "input_fingerprint", "state",
                    "support_evidence_count", "support_publisher_count",
                    "support_independent_units", "contradict_evidence_count",
                    "contradict_publisher_count", "contradict_independent_units",
                    "context_evidence_count", "official_record_support_count",
                    "first_party_support_count", "unclassified_evidence_count",
                    "first_evidence_at", "latest_evidence_at", "computed_at",
                )
            ),
        )


def project_provenance_shadow(
    conn: sqlite3.Connection,
    *,
    config: Mapping[str, object] | None = None,
    limit: int | None = None,
    evidence_scan_limit: int = 200,
    max_roots: int = 25,
    max_group_evidence: int = 2_000,
    max_total_evidence: int = 2_000,
    now: int | None = None,
) -> dict[str, object]:
    """Project a bounded batch; exceptions are intentionally caller-visible."""
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100_000:
            raise ProvenanceShadowBoundsError("limit is outside its safe bound")
        # Backward-compatible maintenance alias.  It never expands the dirty
        # scan above its own small bound and cannot inherit mapping's 100k.
        max_total_evidence = limit
        evidence_scan_limit = min(evidence_scan_limit, limit)
    for value, name, maximum in (
        (evidence_scan_limit, "evidence_scan_limit", 10_000),
        (max_roots, "max_roots", 1_000),
        (max_group_evidence, "max_group_evidence", 100_000),
        (max_total_evidence, "max_total_evidence", 100_000),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ProvenanceShadowBoundsError(f"{name} is outside its safe bound")
    registry = _normalize_registry(config)  # validate before creating any table
    projection_version = provenance_projection_version(config)
    computed_at = int(time.time()) if now is None else int(now)
    if computed_at <= 0:
        raise ValueError("now must be a positive Unix timestamp")
    init_provenance_shadow_schema(conn)

    processed_roots = 0
    processed_evidence = 0
    deferred_oversized_roots = 0
    with _atomic(conn):
        candidate_ids, has_more = _root_candidates(
            conn, projection_version, evidence_scan_limit=evidence_scan_limit
        )
        roots: list[str] = []
        seen_roots: set[str] = set()
        for event_id in candidate_ids:
            root = canonical_event_id(conn, event_id)
            if root not in seen_roots:
                roots.append(root)
                seen_roots.add(root)
        if len(roots) > max_roots:
            roots = roots[:max_roots]
            has_more = True
        for root in roots:
            members = canonical_event_group(conn, root)
            root_row = conn.execute(
                "SELECT version FROM events WHERE event_id = ? AND merge_state = 'active'",
                (root,),
            ).fetchone()
            if root_row is None:
                continue
            remaining_total = max(0, max_total_evidence - processed_evidence)
            root_fetch_bound = min(max_group_evidence, remaining_total)
            evidence = _base_evidence_for_group(
                conn,
                members,
                row_limit=root_fetch_bound + 1,
            )
            if len(evidence) > root_fetch_bound:
                deferred_oversized_roots += 1
                has_more = True
                continue
            projected = _project_rows(
                evidence,
                root_event_id=root,
                root_event_version=int(root_row[0]),
                projection_version=projection_version,
                registry_config=config,
                computed_at=computed_at,
            )
            claims = _claim_rows(
                projected,
                root_event_id=root,
                root_event_version=int(root_row[0]),
                projection_version=projection_version,
                computed_at=computed_at,
            )
            _write_root_projection(
                conn,
                root_event_id=root,
                members=members,
                projected=projected,
                claims=claims,
                projection_version=projection_version,
            )
            processed_roots += 1
            processed_evidence += len(evidence)

    complete = not has_more and deferred_oversized_roots == 0
    return {
        "status": "ok",
        "complete": complete,
        "projection_version": projection_version,
        "processed_roots": processed_roots,
        "processed_evidence": processed_evidence,
        "deferred_oversized_roots": deferred_oversized_roots,
        "has_more": has_more,
        "registry_domain_count": len(registry["domains"]),  # type: ignore[arg-type]
        "registry_route_count": len(registry["routes"]),  # type: ignore[arg-type]
    }


def _pending(reason: str, projection_version: str = "") -> dict[str, object]:
    return {
        "status": "pending",
        "reason": reason,
        "projection_version": projection_version,
        "root_event_version": 0,
        "raw_evidence_count": 0,
        "projected_evidence_count": 0,
        "publisher_group_count": 0,
        "lineage_unit_count": 0,
        "first_party_evidence_count": 0,
        "official_record_evidence_count": 0,
        "claim_count": 0,
        "claim_state_counts": {state: 0 for state in _CLAIM_STATES},
        "latest_evidence_at": 0,
        "computed_at": 0,
    }


def load_event_provenance_shadow(
    conn: sqlite3.Connection,
    root_event_id: str,
    *,
    config: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Load one strict aggregate, returning pending instead of stale evidence."""
    try:
        projection_version = provenance_projection_version(config)
    except ProvenanceShadowConfigError:
        return _pending("invalid_config")
    try:
        verify_provenance_shadow_schema(conn)
    except (ProvenanceShadowSchemaError, sqlite3.DatabaseError):
        return _pending("schema_unavailable", projection_version)
    try:
        root = canonical_event_id(conn, root_event_id)
        members = canonical_event_group(conn, root)
    except (KeyError, ValueError, sqlite3.DatabaseError):
        return _pending("root_missing", projection_version)
    root_row = conn.execute(
        "SELECT version FROM events WHERE event_id = ? AND merge_state = 'active'",
        (root,),
    ).fetchone()
    if root_row is None:
        return _pending("root_missing", projection_version)
    root_version = int(root_row[0])
    base_rows = _base_evidence_for_group(conn, members, row_limit=2_001)
    if not base_rows:
        result = _pending("no_base_evidence", projection_version)
        result["root_event_version"] = root_version
        return result
    if len(base_rows) > 2_000:
        result = _pending("input_bound_exceeded", projection_version)
        result["root_event_version"] = root_version
        result["raw_evidence_count"] = len(base_rows)
        return result

    expected_projected = _project_rows(
        base_rows,
        root_event_id=root,
        root_event_version=root_version,
        projection_version=projection_version,
        registry_config=config,
        computed_at=1,
    )
    expected_claims = _claim_rows(
        expected_projected,
        root_event_id=root,
        root_event_version=root_version,
        projection_version=projection_version,
        computed_at=1,
    )
    ids = [str(row["evidence_id"]) for row in base_rows]
    placeholders = ",".join("?" for _ in ids)
    stored_projected = conn.execute(
        "SELECT * FROM event_evidence_provenance_shadow WHERE projection_version = ? "
        f"AND evidence_id IN ({placeholders})",
        (projection_version, *ids),
    ).fetchall()
    projected_by_id = {str(row["evidence_id"]): row for row in stored_projected}
    projected_fields = (
        "evidence_version", "event_id", "root_event_id", "root_event_version",
        "claim_fingerprint", "claim_basis", "publisher_group", "publisher_basis",
        "source_class", "first_party_scope_json", "lineage_cluster",
        "lineage_representative_evidence_id", "lineage_basis", "input_fingerprint",
    )
    complete = len(projected_by_id) == len(expected_projected)
    for expected in expected_projected:
        stored = projected_by_id.get(str(expected["evidence_id"]))
        if stored is None or any(stored[field] != expected[field] for field in projected_fields):
            complete = False
            break
    if not complete:
        result = _pending("input_incomplete", projection_version)
        result.update(
            root_event_version=root_version,
            raw_evidence_count=len(base_rows),
            projected_evidence_count=len(projected_by_id),
        )
        return result

    stored_claims = conn.execute(
        "SELECT * FROM event_claim_state_shadow WHERE root_event_id = ? "
        "AND projection_version = ? ORDER BY claim_fingerprint",
        (root, projection_version),
    ).fetchall()
    claim_by_id = {str(row["claim_fingerprint"]): row for row in stored_claims}
    claim_fields = (
        "root_event_version", "input_fingerprint", "state", "support_evidence_count",
        "support_publisher_count", "support_independent_units",
        "contradict_evidence_count", "contradict_publisher_count",
        "contradict_independent_units", "context_evidence_count",
        "official_record_support_count", "first_party_support_count",
        "unclassified_evidence_count", "first_evidence_at", "latest_evidence_at",
    )
    claims_complete = len(claim_by_id) == len(expected_claims)
    for expected in expected_claims:
        stored = claim_by_id.get(str(expected["claim_fingerprint"]))
        if stored is None or any(stored[field] != expected[field] for field in claim_fields):
            claims_complete = False
            break
    if not claims_complete:
        result = _pending("claim_aggregate_incomplete", projection_version)
        result.update(
            root_event_version=root_version,
            raw_evidence_count=len(base_rows),
            projected_evidence_count=len(projected_by_id),
        )
        return result

    state_counts = Counter(str(row["state"]) for row in stored_claims)
    stale_count = conn.execute(
        "SELECT COUNT(*) FROM event_claim_state_shadow WHERE projection_version = ? "
        "AND root_event_id IN ("
        + ",".join("?" for _ in members)
        + ") AND root_event_id <> ?",
        (projection_version, *members, root),
    ).fetchone()[0]
    return {
        "status": "degraded" if int(stale_count) else "ready",
        "reason": "stale_merged_aggregate" if int(stale_count) else "",
        "projection_version": projection_version,
        "root_event_version": root_version,
        "raw_evidence_count": len(base_rows),
        "projected_evidence_count": len(stored_projected),
        "publisher_group_count": len(
            {str(row["publisher_group"]) for row in stored_projected} - {"unknown"}
        ),
        "lineage_unit_count": len(
            {str(row["lineage_cluster"]) for row in stored_projected}
        ),
        "first_party_evidence_count": 0,
        "official_record_evidence_count": sum(
            str(row["source_class"]) == "official_record" for row in stored_projected
        ),
        "claim_count": len(stored_claims),
        "claim_state_counts": {state: int(state_counts[state]) for state in _CLAIM_STATES},
        "latest_evidence_at": max(int(row["latest_evidence_at"]) for row in stored_claims),
        "computed_at": min(
            min(int(row["computed_at"]) for row in stored_projected),
            min(int(row["computed_at"]) for row in stored_claims),
        ),
    }


def run_provenance_shadow(
    *,
    config: Mapping[str, object] | None = None,
    limit: int | None = None,
    evidence_scan_limit: int = 200,
    max_roots: int = 25,
    max_group_evidence: int = 2_000,
    max_total_evidence: int = 2_000,
    now: int | None = None,
) -> dict[str, object]:
    """Maintenance wrapper; callers retain responsibility for error handling."""
    from db import database

    with database.db_lock:
        with database.get_connection() as conn:
            return project_provenance_shadow(
                conn,
                config=config,
                limit=limit,
                evidence_scan_limit=evidence_scan_limit,
                max_roots=max_roots,
                max_group_evidence=max_group_evidence,
                max_total_evidence=max_total_evidence,
                now=now,
            )


__all__ = [
    "PROVENANCE_PROJECTION_ALGORITHM",
    "ProvenanceShadowBoundsError",
    "ProvenanceShadowConfigError",
    "load_event_provenance_shadow",
    "project_provenance_shadow",
    "provenance_projection_version",
    "resolve_publisher",
    "run_provenance_shadow",
]
