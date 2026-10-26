"""Budget/permissions tests use synthetic bytes in pytest's private sandbox only."""
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from core.config import _strategy_fingerprint
from services.score_evidence_capture import _CAPTURE, _publish_capture, capture_article_score
from services.score_gold import blank_labels
from utils.score_artifacts import canonical_json

ROOT = Path(__file__).resolve().parents[1]


def _options(directory, maximum=500):
    return {"directory": str(directory), "max_records": maximum, "max_record_bytes": 256 * 1024}


def test_limit_counts_actual_utf8_file_including_newline(tmp_path):
    directory = tmp_path / "evidence"
    options = _options(directory)
    limit = options["max_record_bytes"]
    capture = {"evidence_id": "0" * 64, "text": ""}
    space = limit - len(canonical_json(capture)) - 1
    capture["text"] = "源" * (space // 3) + "x" * (space % 3)
    assert len(canonical_json(capture)) + 1 == limit
    _publish_capture(capture, options)
    saved = directory / f"{capture['evidence_id']}.json"
    assert saved.stat().st_size == limit
    assert json.loads(saved.read_text()) == capture
    capture = {**capture, "evidence_id": "1" * 64, "text": capture["text"] + "x"}
    with pytest.raises(ValueError, match="oversized"):
        _publish_capture(capture, options)
    assert len(list(directory.glob("*.json"))) == 1


def test_exactly_500_records_and_durable_stop_after_restart(tmp_path):
    directory = tmp_path / "evidence"
    options = _options(directory)
    for index in range(500):
        _publish_capture({"evidence_id": f"{index:064x}", "synthetic": True}, options)
    records = list(directory.glob("*.json"))
    assert len(records) == 500
    assert all(path.stat().st_size <= 256 * 1024 and path.stat().st_mode & 0o777 == 0o600 for path in records)
    marker = directory / ".capture.complete"
    assert marker.is_file() and marker.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="complete"):
        _publish_capture({"evidence_id": "excess"}, options)
    # Simulate a crash after the 500th record but before publishing its stop marker.
    marker.unlink()
    with pytest.raises(ValueError, match="budget"):
        _publish_capture({"evidence_id": "crash-retry"}, options)
    assert marker.is_file()
    # Removing a fixture record simulates archival: it must NOT reopen this cohort.
    records[0].unlink()
    program = """
import json, sys
from services.score_evidence_capture import _publish_capture
try:
    _publish_capture({'evidence_id': 'restart'}, json.loads(sys.argv[1]))
except ValueError as error:
    assert 'complete' in str(error)
else:
    raise AssertionError('completed cohort reopened')
"""
    subprocess.run([sys.executable, "-c", program, json.dumps(options)], cwd=ROOT, check=True, timeout=15)
    assert len(list(directory.glob("*.json"))) == 499


def test_concurrent_publishers_share_one_budget(tmp_path):
    directory = tmp_path / "evidence"
    options = _options(directory, maximum=7)

    def publish(index):
        try:
            _publish_capture({"evidence_id": f"{index:064x}"}, options)
            return True
        except ValueError as error:
            assert "complete" in str(error) or "budget" in str(error)
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(publish, range(40)))
    assert sum(accepted) == 7
    assert len(list(directory.glob("*.json"))) == 7
    assert (directory / ".capture.complete").is_file()


def test_full_cohort_skips_collection_but_not_scoring(tmp_path):
    directory = tmp_path / "evidence"
    options = _options(directory, maximum=1)
    _publish_capture({"evidence_id": "fixture"}, options)
    ctx = SimpleNamespace(cfg={"score_evidence_capture": {**options, "enabled": True}})
    calls = []

    @capture_article_score
    def score(ctx, result, scorer, summary):
        assert _CAPTURE.get() is None
        calls.append(summary)
        return "normal-scoring-result"

    assert score(ctx, None, None, "unretained source") == "normal-scoring-result"
    assert calls == ["unretained source"]
    assert len(list(directory.glob("*.json"))) == 1


def test_enabled_config_is_bounded_and_does_not_change_score_contract(tmp_path):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    options = cfg.pop("score_evidence_capture")
    assert options == {"enabled": True, "directory": "/app/data/score_evidence/example-cohort",
                       "max_records": 500, "max_record_bytes": 262144}
    assert _strategy_fingerprint(cfg) == _strategy_fingerprint({**cfg, "score_evidence_capture": options})
    assert all(value is None for value in blank_labels().values())
    assert len(blank_labels()) == 7
    # The conftest I/O fixture redirects this production-configured path, not capture behavior.
    _publish_capture({"evidence_id": "sandbox-probe"}, options)
    assert (tmp_path / "score_evidence" / "example-cohort" / "sandbox-probe.json").is_file()
