"""Synthetic fixtures ONLY. No production sample receives machine-filled labels."""

from copy import deepcopy
import csv
import json
from pathlib import Path

import pytest

from scripts.export_score_gold_candidates import (
    annotation_csv,
    build_pack,
    candidate_record,
    main as export_main,
    sample_candidates,
)
from scripts.import_score_gold_labels import import_phase, read_human_csv
from scripts.score_gold_metrics import evaluate, metrics_for
from services.score_gold import (
    LABEL_FIELDS,
    SOURCE_FIELDS,
    REASON_FIELDS,
    blank_labels,
    candidate_from_attempt,
    seal_pack,
    validate_candidates,
    validate_finalized,
)
from utils.score_artifacts import content_hash


# Only used in temporary unit-test data; never exported as a real annotation.
SYNTHETIC_LABELS = dict(
    relevance=1,
    direction="看多",
    score_band="high",
    should_be_selected=1,
    factual_claim_correct=1,
    theme_correct=1,
    reason_is_supported=1,
)


def evidence_fixture(
    index=0,
    *,
    score=8,
    strategy="contract-a",
    stage="stage1",
    model="kimi-fixture",
    neutral=False,
):
    source = {"raw_title": f"甲{index}公司发布新产品", "summary": ""}
    values = (
        [4.0, "其他", "来源未显示明确投资催化", "⚖️"]
        if neutral
        else [score, "公司公告", source["raw_title"], "📈"]
    )
    record = {
        "schema": "score-evidence-v1",
        "strategy_version": strategy,
        "event_id": f"synthetic-event-{index}",
        "event_group": content_hash(f"event-{index}"),
        "article_id": content_hash(f"article-{index}"),
        "captured_at": 1,
        "source": source,
        "input_hash": content_hash(source),
        "evidence_complete": True,
        "attempts": [
            {
                "stage": stage,
                "attempt": "initial",
                "attempted_at": 1,
                "request_hash": content_hash(source),
                "source_in_request": True,
                "model_name": model,
                "backend_name": "fixture-backend",
                "parsed": values,
                "raw_response": json.dumps(values),
                "accepted": True,
                "failure_reason": "",
                "failure_detail": "",
                "fact_failure_kind": "",
                "repair_trigger": "",
            }
        ],
    }
    record["evidence_id"] = content_hash(record)
    return record


def candidate_fixture(index=0, **kwargs):
    record = evidence_fixture(index, **kwargs)
    item = candidate_from_attempt(record, 0)
    item["_evidence"] = record
    return item


def pack_fixture(count=200, **kwargs):
    return build_pack(
        [candidate_fixture(index, **kwargs) for index in range(count)], count, 11
    )


def reviewed_fixture(count=200, **kwargs):
    pack = pack_fixture(count, **kwargs)
    for item in pack["items"]:
        item["labels"] = dict(SYNTHETIC_LABELS)
        item["human_review"] = {
            phase: [
                {
                    "annotator_id": reviewer,
                    "labels": {key: SYNTHETIC_LABELS[key] for key in fields},
                }
                for reviewer in ("synthetic_A", "synthetic_B")
            ]
            for phase, fields in (("source", SOURCE_FIELDS), ("reason", REASON_FIELDS))
        }
    pack["manifest"].update(
        status="finalized",
        unlabeled=False,
        ready_for_metrics=True,
        human_attestation="operator_attested_human_only",
    )
    return seal_pack(pack)


def fill_synthetic_csv(path: Path, items, phase: str, reviewer: str, **changes):
    path.write_bytes(annotation_csv(items, phase))
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        headers = reader.fieldnames
        rows = list(reader)
    assert headers is not None
    for row in rows:
        row["annotator_id"] = reviewer
        for field in LABEL_FIELDS:
            if field in row:
                row[field] = str(changes.get(field, SYNTHETIC_LABELS[field]))
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_joint_sampling_preserves_available_high_and_is_order_invariant():
    rows = [
        candidate_fixture(
            index,
            score=8 if index < 40 else 6 if index < 200 else 4,
            model="qwen-fixture" if index % 2 else "kimi-fixture",
            stage="stage2" if index % 3 else "stage1",
        )
        for index in range(600)
    ]
    selected, manifest = sample_candidates(rows, 300, 11)
    again, _ = sample_candidates(list(reversed(rows)), 300, 11)
    assert [row["gold_id"] for row in selected] == [row["gold_id"] for row in again]
    assert len(selected) == 300
    assert manifest["achieved"]["high"] == 40
    assert manifest["shortfall"]["high"] == 0
    assert manifest["achieved"]["kimi"] + manifest["achieved"]["qwen"] == 300


def test_event_deduplication_and_shortfall_are_explicit():
    first, second = candidate_fixture(1), candidate_fixture(2)
    second["event_group"] = first["event_group"]
    selected, manifest = sample_candidates([first, second], 300, 11)
    assert len(selected) == 1
    assert manifest["inventory_events"]["high"] == 1
    assert manifest["shortfall"]["high"] == 39


def test_legacy_never_falls_back_to_display_title_or_generated_summary():
    item = candidate_record(
        {
            "link": "fixture",
            "title": "[⭐9]新闻💡模型理由",
            "summary": "模型生成摘要",
            "score": 9,
        }
    )
    assert item["source"] == {"raw_title": "", "summary": None}
    assert not item["evidence_complete"]
    assert all(value is None for value in item["labels"].values())


def test_full_source_and_rejected_reason_are_preserved():
    record = evidence_fixture()
    record.pop("evidence_id")
    record["source"]["raw_title"] = "原始标题" * 100
    record["source"]["summary"] = "实际评分摘要" * 400
    record["input_hash"] = content_hash(record["source"])
    record["attempts"][0].update(
        accepted=False,
        failure_reason="ungrounded_score_facts",
        fact_failure_kind="numeric_fact_failure",
    )
    record["attempts"][0]["parsed"][2] = "被拒绝理由" * 100
    record["evidence_id"] = content_hash(record)
    item = candidate_from_attempt(record, 0)
    assert item["source"] == record["source"]
    assert item["prediction"]["logic"] == "被拒绝理由" * 100
    assert item["prediction"]["score_path"] == "rejected"
    assert item["labels"] == blank_labels()


def test_export_run_is_write_once_and_source_blind(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "one.json").write_text(json.dumps(evidence_fixture()), encoding="utf-8")
    (evidence / "two.json").write_text(
        json.dumps(evidence_fixture(1, strategy="contract-b")), encoding="utf-8"
    )
    args = [
        "--evidence-dir",
        str(evidence),
        "--out-dir",
        str(tmp_path / "out"),
        "--run-id",
        "test-only",
        "--target",
        "1",
    ]
    assert export_main(args) == 0
    run = tmp_path / "out" / "test-only"
    index = json.loads((run / "index.json").read_text())
    assert len(index["contracts"]) == 2
    source = run / index["contracts"][0]["directory"] / "source.csv"
    text = source.read_text()
    assert "model_name" not in text and "score_path" not in text and "logic" not in text
    assert not source.with_name("reason.csv").exists()
    source.write_text("human edit sentinel")
    assert export_main(args) == 2
    assert source.read_text() == "human edit sentinel"
    assert (source.stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize(
    "mutation",
    ["label", "source", "prediction", "duplicate", "contract", "evidence", "flags"],
)
def test_metric_gate_rejects_tampering_or_invalid_labels(mutation):
    pack = reviewed_fixture()
    item = pack["items"][0]
    if mutation == "label":
        item["labels"] = dict.fromkeys(LABEL_FIELDS, "garbage")
    elif mutation == "source":
        item["source"]["summary"] = "模型摘要替代来源"
    elif mutation == "prediction":
        item["prediction"]["logic"] = "replacement"
    elif mutation == "duplicate":
        pack["items"][1]["gold_id"] = item["gold_id"]
    elif mutation == "contract":
        item["strategy_version"] = "different-contract"
    elif mutation == "evidence":
        item["evidence_complete"] = False
    else:
        pack = pack_fixture()
        pack["manifest"].update(
            status="finalized",
            unlabeled=False,
            ready_for_metrics=True,
            human_attestation="operator_attested_human_only",
        )
    # Even an intentionally re-hashed artifact must satisfy semantic validation.
    seal_pack(pack)
    with pytest.raises(ValueError):
        evaluate(pack)


def test_no_accuracy_for_candidates_or_small_set():
    with pytest.raises(ValueError, match="unlabeled"):
        evaluate(pack_fixture())
    with pytest.raises(ValueError, match="200"):
        evaluate(reviewed_fixture(2))


def test_precision_definitions_and_na_denominators():
    pack = reviewed_fixture(neutral=True)
    result = evaluate(pack)["metrics"]
    assert (
        result["safe_neutral_precision"]["estimate"] == 0
    )  # low prediction is NOT a correct abstention
    assert result["high_selection_precision"]["estimate"] is None
    assert result["sample_safe_neutral_share"]["estimate"] == 1
    rows = deepcopy(pack["items"])
    for item in rows:
        item["prediction"]["score"] = 8
        item["labels"].update(should_be_selected=0, factual_claim_correct="na")
    metrics = metrics_for(rows)
    assert metrics["high_band_agreement"]["estimate"] == 1
    assert metrics["high_selection_precision"]["estimate"] == 0
    assert metrics["factual_claim_correct"]["denominator"] == 0
    assert metrics["factual_claim_correct"]["wilson95"] is None


def test_two_phase_import_requires_humans_and_resolves_disputes(tmp_path):
    pack = pack_fixture()
    source = [
        fill_synthetic_csv(
            tmp_path / f"source-{name}.csv", pack["items"], "source", name
        )
        for name in ("synthetic_A", "synthetic_B")
    ]
    with pytest.raises(ValueError, match="attestation"):
        import_phase(pack, "source", source, human_attestation=False)
    source_reviewed = import_phase(pack, "source", source, human_attestation=True)
    assert all(item["labels"] == blank_labels() for item in source_reviewed["items"])
    reason = [
        fill_synthetic_csv(
            tmp_path / f"reason-{name}.csv",
            pack["items"],
            "reason",
            name,
            theme_correct=0 if name == "synthetic_B" else 1,
        )
        for name in ("synthetic_A", "synthetic_B")
    ]
    with pytest.raises(ValueError, match="phase/order"):
        import_phase(pack, "reason", reason, human_attestation=True)
    with pytest.raises(ValueError, match="unresolved dispute"):
        import_phase(source_reviewed, "reason", reason, human_attestation=True)
    adjudication = fill_synthetic_csv(
        tmp_path / "adjudication.csv", pack["items"], "adjudication", "synthetic_C"
    )
    final = import_phase(
        source_reviewed,
        "reason",
        reason,
        human_attestation=True,
        adjudication=adjudication,
    )
    validate_finalized(final)
    assert evaluate(final)["metrics"]["n"] == 200
    assert pack["manifest"]["unlabeled"] is True


def test_csv_rejects_duplicate_ids_bad_labels_and_source_edits(tmp_path):
    pack = pack_fixture(2)
    path = fill_synthetic_csv(
        tmp_path / "invalid.csv",
        pack["items"],
        "source",
        "synthetic_A",
        relevance="garbage",
    )
    with pytest.raises(ValueError, match="invalid human label"):
        read_human_csv(path, pack["items"], "source")
    fill_synthetic_csv(path, pack["items"], "source", "synthetic_A")
    text = path.read_text()
    path.write_text(text.replace(pack["items"][0]["input_hash"], "modified"))
    with pytest.raises(ValueError, match="identity/source"):
        read_human_csv(path, pack["items"], "source")
    fill_synthetic_csv(path, pack["items"], "source", "synthetic_A")
    with path.open("a") as stream:
        stream.write(path.read_text().splitlines()[1] + "\n")
    with pytest.raises(ValueError, match="duplicate"):
        read_human_csv(path, pack["items"], "source")


def test_import_rejects_incomplete_legacy_evidence():
    pack = build_pack(
        [candidate_record({"link": "fixture", "strategy_version": "old", "score": 8})],
        1,
        1,
    )
    with pytest.raises(ValueError, match="evidence incomplete"):
        validate_candidates(pack)
