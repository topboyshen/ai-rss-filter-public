"""Offline blacklist replay is evidence, not a policy change or an importance score."""

import copy
import io
import json
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from feedparser import FeedParserDict

from core import processor
from ops import audit_blacklist_titles as audit


def document(title, rules=None, **fields):
    return {
        "schema": 1,
        "blacklist": ["业绩预告", "股东减持", "减持"] if rules is None else rules,
        "samples": [{"title": title, **fields}],
    }


@pytest.mark.parametrize("title,patterns", [
    ("业绩预告：预计净利润同比下降80%", ["业绩预告"]),
    ("澄清：不存在股东减持安排", ["股东减持", "减持"]),
    ("股东减持计划公告", ["股东减持", "减持"]),
])
def test_both_material_or_clarifying_and_routine_words_are_blocked_without_policy_relaxation(title, patterns):
    report = audit.audit_document(document(title))
    row = report["samples"][0]
    assert row["status"] == "blacklist"
    assert row["would_stop_before_scoring"] is True
    assert row["review_status"] == "manual_review_required"
    assert [match["pattern"] for match in row["matching_rules"]] == patterns
    assert "important" not in row and "recommended_action" not in row


@pytest.mark.parametrize("title,summary,rules,status", [
    ("&lt;b&gt;业绩预告&lt;/b&gt;：净利下降80%", "", ["业绩预告"], "blacklist"),
    ("<script>减持</script>央行政策更新", "", ["减持"], "not_blocked_by_blacklist"),
    ("央行政策更新", "业绩预告", ["业绩预告"], "not_blocked_by_blacklist"),
    ("某公司净利下降80%", "", [], "not_blocked_by_blacklist"),
    ("[⭐8.0 政策] 业绩预告", "", ["业绩预告"], "already_scored_title"),
    ("[★8.0 政策] 业绩预告", "", ["业绩预告"], "already_scored_title"),
    ("<b></b>", "", [""], "empty_title"),
    ("业绩预告\ufffd", "", ["业绩预告"], "invalid_source_text"),
    ("业绩预告", "来源\ufffd", ["业绩预告"], "invalid_source_text"),
    ("净利润同比下降80%", "", [r"净利润.*下降\d+%"], "blacklist"),
])
def test_offline_normalization_and_early_exit_match_the_actual_processor(title, summary, rules, status):
    sample = document(title, rules, summary=summary)
    audited = audit.audit_document(sample)["samples"][0]
    cfg = {"_blacklist_re": re.compile("|".join(rules)) if rules else re.compile("$^")}
    entry = FeedParserDict(title=title, summary=summary, link="https://example.com/title")
    normalized, _, reason = processor._normalize_entry_source(entry, cfg)
    assert audited["normalized_title"] == normalized
    assert audited["status"] == (reason or "not_blocked_by_blacklist") == status
    if status != "blacklist":
        assert audited["matching_rules"] == []


def test_actual_blacklist_exit_precedes_cache_similarity_model_and_persistence(monkeypatch):
    monkeypatch.setattr(processor.DB, "canonical_feed_url", lambda _cfg, url: url)
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append("forbidden")
        raise AssertionError("blacklisted title must stop before downstream processing")

    for name in ("get_cached_entry", "get_latest_signal_score_path", "set_cached_entry", "store_backtest_signal"):
        monkeypatch.setattr(processor.DB, name, forbidden)
    monkeypatch.setattr(processor, "find_similar_article", forbidden)
    monkeypatch.setattr(processor, "_score_stage_with_identity", forbidden)
    scorer = Mock()
    entry = FeedParserDict(title="澄清：不存在股东减持安排", link="https://example.com/denial", summary="")
    result, reason = processor.process_entry(entry, [], "", {"_blacklist_re": re.compile("股东减持|减持")}, scorer)
    assert result is None and reason == "blacklist"
    assert calls == [] and scorer.mock_calls == []


def test_exact_alternation_match_is_separate_from_overlapping_rule_explanations():
    row = audit.audit_document(document("澄清：不存在股东减持安排", ["减持", "股东减持"]))["samples"][0]
    assert row["decision_match"] == {"matched_text": "股东减持", "span": [6, 10]}
    assert [match["pattern"] for match in row["matching_rules"]] == ["减持", "股东减持"]


def test_sample_provenance_and_unknown_presence_are_preserved_without_causal_claims():
    value = document("业绩预告：净利下降80%", sample_id="operator-1", ingestion_scope="operator_example",
                     within_processing_limit=None, cache_present=False, signal_present=False)
    previous = copy.deepcopy(value)
    report = audit.audit_document(value)
    assert value == previous
    row = report["samples"][0]
    assert row["cache_present"] is False and row["within_processing_limit"] is None
    assert row["ingestion_scope"] == "operator_example" and row["summary_checked"] is False
    assert report["mode"] == "offline_policy_replay"
    assert any("not proof" in limitation for limitation in report["limitations"])


def test_stored_original_evidence_is_not_misrepresented_as_observed_feed_or_rejected_title():
    row = audit.audit_document(document(
        "飞龙股份：预计2026年前三季度净利润同比下降59.20%-66.87%",
        ingestion_scope="stored_original_evidence", within_processing_limit=None,
    ))["samples"][0]
    assert row["ingestion_scope"] == "stored_original_evidence"
    assert row["status"] == "not_blocked_by_blacklist"
    assert row["matching_rules"] == [] and row["review_status"] == "not_reviewed"


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(schema=True),
    lambda value: value.update(samples=[{"title": "x", "api_key": "not-for-report"}]),
    lambda value: value.update(samples=[{"title": "x", "cache_present": "false"}]),
    lambda value: value.update(samples=[{"title": "x", "ingestion_scope": "processed"}]),
    lambda value: value.update(blacklist=[1]),
    lambda value: value.update(blacklist=["x"] * 201),
    lambda value: value.update(samples=[{"title": "x"}] * 101),
    lambda value: value.update(samples=[{"title": "x" * 8193}]),
])
def test_invalid_or_unbounded_samples_are_refused(mutate):
    value = document("x")
    mutate(value)
    with pytest.raises(ValueError):
        audit.audit_document(value)


@pytest.mark.parametrize("payload", [b"{\"api_key\":\"secret-sentinel\"}", b"x" * (audit.MAX_INPUT_BYTES + 1)])
def test_cli_rejects_invalid_input_without_echoing_it(monkeypatch, capsys, payload):
    monkeypatch.setattr(audit.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload)))
    assert audit.main() == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "secret-sentinel" not in captured.err


def test_cli_emits_only_offline_replay_and_never_initializes_app_or_db(monkeypatch, capsys):
    payload = json.dumps(document("业绩预告：净利下降80%"), ensure_ascii=False).encode()
    monkeypatch.setattr(audit.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload)))
    monkeypatch.setattr(processor.DB, "get_cached_entry", Mock(side_effect=AssertionError("no database")))
    assert audit.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["blacklist_count"] == 1
    processor.DB.get_cached_entry.assert_not_called()


def test_fresh_offline_import_does_not_load_config_database_scorer_or_app():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; from ops import audit_blacklist_titles; "
         "assert not {'db.database', 'core.config', 'core.scorer', 'app'} & set(sys.modules)"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_cli_refuses_invalid_regex_without_showing_its_pattern(monkeypatch, capsys):
    payload = json.dumps(document("x", ["secret-sentinel["])).encode()
    monkeypatch.setattr(audit.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload)))
    assert audit.main() == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "secret-sentinel" not in captured.err
