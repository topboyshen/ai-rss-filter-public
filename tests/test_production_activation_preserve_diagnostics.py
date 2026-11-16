"""Preservation failure reasons survive the public projection without raw text."""
import json

import pytest

from scripts import production_activation_host_native as native


@pytest.mark.parametrize("reason", ["AdmissionError", "TimeoutExpired", "pg:inspect",
                                   "sender:observation", "fixture:stop_unconfirmed", "redis:inspect"])
def test_fixed_preservation_failure_survives_without_scenario_failure(reason):
    summary = native.safe_activation({"failure": None, "evidence_errors": [reason],
                                      "status": "failed_or_evidence_insufficient"})
    assert summary["evidence_error_count"] == 1
    assert summary["evidence_error_codes"] == [reason]
    assert summary["evidence_error_codes_omitted"] == 0
    assert "failure_type" not in summary
    assert summary["status"] == "failed_or_evidence_insufficient"


@pytest.mark.parametrize("reason", ["password=synthetic-secret", "secret:inspect", "pg:secret",
                                   "AdmissionError\nsecret", "a" * 10000, {"secret": "value"}, None])
def test_unknown_preservation_text_is_counted_but_never_forwarded(reason):
    summary = native.safe_activation({"evidence_errors": [reason]})
    assert summary["evidence_error_count"] == 1
    assert summary["evidence_error_codes"] == [None]
    assert "secret" not in json.dumps(summary)


def test_preservation_projection_is_bounded_without_hiding_total():
    summary = native.safe_activation({"evidence_errors": ["RuntimeError"] * 17})
    assert summary["evidence_error_count"] == 17
    assert summary["evidence_error_codes"] == ["RuntimeError"] * 16
    assert summary["evidence_error_codes_omitted"] == 1


@pytest.mark.parametrize("errors", [None, "RuntimeError", {}, False])
def test_malformed_preservation_list_does_not_become_success(errors):
    summary = native.safe_activation({"evidence_errors": errors})
    assert summary["evidence_error_count"] is None
    assert summary["evidence_error_codes"] == []
    assert summary["evidence_error_codes_omitted"] is None


def test_no_preservation_error_has_empty_explicit_projection():
    summary = native.safe_activation({"evidence_errors": []})
    assert summary["evidence_error_count"] == 0
    assert summary["evidence_error_codes"] == []
    assert summary["evidence_error_codes_omitted"] == 0
