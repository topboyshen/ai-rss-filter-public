"""Replay an explicit blacklist against bounded source samples, offline.

Usage: python -m ops.audit_blacklist_titles < samples.json
Only stdin is read and stdout is written. No configuration, database, model,
network, or application initialization is involved; policy is never changed.
"""

from __future__ import annotations

import json
import re
import sys

from utils.helpers import plain_text_from_markup


MAX_INPUT_BYTES = 256 * 1024
MAX_SAMPLES = 100
MAX_RULES = 200
MAX_TEXT_CHARS = 8192
SAMPLE_FIELDS = frozenset({
    "sample_id", "title", "summary", "link", "feed_name", "observed_at",
    "ingestion_scope", "within_processing_limit", "cache_present", "signal_present",
})


def _validated_input(value: dict) -> tuple[list[str], list[dict]]:
    if not isinstance(value, dict) or set(value) != {"schema", "blacklist", "samples"}:
        raise ValueError("explicit schema, blacklist and samples are required")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise ValueError("unsupported input schema")
    rules, samples = value["blacklist"], value["samples"]
    if not isinstance(rules, list) or len(rules) > MAX_RULES:
        raise ValueError("blacklist must contain at most 200 patterns")
    if any(not isinstance(rule, str) or len(rule) > MAX_TEXT_CHARS for rule in rules):
        raise ValueError("blacklist patterns must be bounded strings")
    if not isinstance(samples, list) or len(samples) > MAX_SAMPLES:
        raise ValueError("samples must contain at most 100 rows")
    for sample in samples:
        if not isinstance(sample, dict) or not set(sample) <= SAMPLE_FIELDS or "title" not in sample:
            raise ValueError("sample fields differ from the explicit source schema")
        for key, item in sample.items():
            if key in {"within_processing_limit", "cache_present", "signal_present"}:
                if item is not None and type(item) is not bool:
                    raise ValueError("presence and processing-limit fields must be boolean or null")
            elif not isinstance(item, str) or len(item) > MAX_TEXT_CHARS:
                raise ValueError("sample text fields must be bounded strings")
        if sample.get("ingestion_scope", "operator_example") not in {
            "observed_feed", "stored_original_evidence", "operator_example",
        }:
            raise ValueError("unsupported ingestion scope")
    return rules, samples


def _audit_sample(sample: dict, combined, indexed_rules: list) -> dict:
    title = plain_text_from_markup(sample["title"])
    summary = plain_text_from_markup(sample.get("summary", ""))
    status = "not_blocked_by_blacklist"
    match = None
    if not title:
        status = "empty_title"
    elif title.startswith(("[⭐", "[★")):
        status = "already_scored_title"
    elif "\ufffd" in title or "\ufffd" in summary:
        status = "invalid_source_text"
    else:
        match = combined.search(title)
        if match:
            status = "blacklist"
    matching_rules = []
    if match:
        for index, pattern, regex in indexed_rules:
            individual = regex.search(title)
            if individual:
                matching_rules.append({
                    "index": index, "pattern": pattern, "matched_text": individual.group(),
                    "span": list(individual.span()),
                })
    return {
        **{key: item for key, item in sample.items() if key not in {"title", "summary"}},
        "source_title": sample["title"], "normalized_title": title,
        "summary_checked": "summary" in sample, "status": status,
        "would_stop_before_scoring": status != "not_blocked_by_blacklist",
        "decision_match": {"matched_text": match.group(), "span": list(match.span())} if match else None,
        "matching_rules": matching_rules,
        "review_status": "manual_review_required" if match else "not_reviewed",
    }


def audit_document(value: dict) -> dict:
    rules, samples = _validated_input(value)
    # Match the application's alternation exactly; these are regexes, not
    # escaped literals. Individual matches explain overlap, not precedence.
    combined = re.compile("|".join(rules)) if rules else re.compile("$^")
    indexed = [(index, rule, re.compile(rule)) for index, rule in enumerate(rules)]
    rows = [_audit_sample(sample, combined, indexed) for sample in samples]
    return {
        "schema": "ai-filter-blacklist-audit-v1", "mode": "offline_policy_replay",
        "rule_count": len(rules), "sample_count": len(rows),
        "blacklist_count": sum(row["status"] == "blacklist" for row in rows),
        "samples": rows,
        "limitations": [
            "Replay is not proof a historical refresh processed this item.",
            "A feed item outside the per-feed processing limit may never reach this check.",
            "Blacklist exits precede scoring/cache writes; missing database rows do not prove the cause.",
            "Not blocked by blacklist does not mean selected by scoring, filters or a page.",
            "Routine notices, material changes and clarifications require human review; no policy is relaxed.",
        ],
    }


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("input exceeds the offline audit bound")
        report = audit_document(json.loads(raw))
    except (ValueError, re.error):
        # Do not echo invalid input; callers could accidentally pass secrets.
        print(json.dumps({"error": "invalid or oversized offline audit input"}), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
