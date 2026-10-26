"""Public examples stay synthetic without broadening the clean-tree ignore scope."""

import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_public_config_does_not_distribute_personal_holdings_or_capture_identity():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    tier = next(row for row in cfg["fast_pass"]["tiers"] if row["name"] == "watchlist")
    assert tier["keywords"] == ["990001", "990002", "990003"]
    assert tier["score"] == 8.5 and tier["skip_llm"] is False
    assert cfg["score_evidence_capture"] == {
        "enabled": True, "directory": "/app/data/score_evidence/example-cohort",
        "max_records": 500, "max_record_bytes": 262144,
    }


@pytest.mark.parametrize("relative,ignored", [
    ("data/diagnostics/local-proof.json", True),
    ("data/diagnostics/private/nested-proof.json", True),
    ("data/diagnostics/STOP", True),
    ("diagnostics/tool.py", False),
    ("scripts/diagnostics.py", False),
    ("tests/diagnostics/test_example.py", False),
    ("nested/data/diagnostics/local-proof.json", False),
    ("data/diagnostics.py", False),
    ("data/diagnostics-other/evidence.json", False),
    ("data/new-runtime/unclassified.json", False),
])
def test_private_diagnostics_ignore_has_only_the_named_root_scope(tmp_path, relative, ignored):
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    subprocess.run(["git", "init", "-q", str(repository)], env=env, check=True)
    result = subprocess.run(
        ["git", "-C", str(repository), "check-ignore", "--no-index", "-q", relative],
        env=env, check=False,
    )
    assert result.returncode == (0 if ignored else 1)
