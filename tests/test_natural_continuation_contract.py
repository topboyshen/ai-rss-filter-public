"""Continuation policy changes must participate in the scoring contract."""
import builtins
import io
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.config import _strategy_code_fingerprint
from core import processor
from services import natural_score_continuations as store


@pytest.mark.parametrize("contract,context,message", [
    ({}, "", "was not resolved"),
    ({"_scoring_contract_resolved": True}, "", "was not registered"),
    ({
        "_scoring_contract_resolved": True, "_scoring_contract_registered": True,
        "_few_shot_context": "registered",
    }, "different", "does not match"),
])
def test_invalid_feedback_contract_precedes_journal_access(monkeypatch, contract, context, message):
    canonicalize = Mock(side_effect=AssertionError("must validate contract first"))
    journal = Mock(side_effect=AssertionError("must validate contract first"))
    monkeypatch.setattr(processor.DB, "canonical_feed_url", canonicalize)
    monkeypatch.setattr(store, "locked_record", journal)
    entry = SimpleNamespace(title="unchanged", link="https://example.test/invalid-contract")
    cfg = {"few_shot_feedback": {"enabled": True}, **contract}
    with pytest.raises(RuntimeError, match=message):
        processor.process_entry(
            entry, [], "prompt", cfg, few_shot_context=context, persist_deferred=True,
        )
    canonicalize.assert_not_called()
    journal.assert_not_called()
    assert entry.title == "unchanged"


@pytest.fixture
def ignore_repository(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    subprocess.run(
        ["git", "init", "--quiet", "--template=", str(repository)],
        env=env, text=True, capture_output=True, check=True, timeout=10,
    )
    (repository / ".gitignore").write_bytes(
        (Path(__file__).resolve().parents[1] / ".gitignore").read_bytes(),
    )
    return repository, env


@pytest.mark.parametrize("relative", [
    "data/natural_score_continuations/" + "a" * 64 + ".json",
    "data/natural_score_continuations/.locks/" + "a" * 64 + ".lock",
    "data/natural_score_continuations/.lock",
    "data/natural_score_continuations/.tmp-synthetic",
    "data/natural_score_continuations/unknown-private-runtime-file",
])
def test_private_continuation_runtime_directory_is_ignored(ignore_repository, relative):
    repository, env = ignore_repository
    result = subprocess.run(
        ["git", "-C", str(repository), "check-ignore", "-q", relative],
        env=env, text=True, capture_output=True, check=False, timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("relative", [
    "data/natural_score_continuations-other/record.json",
    "data/natural_score_continuations_config.yaml",
    "data/unrecognized-runtime/record.json",
    "data/unrecognized-runtime/.lock",
    "data/unrecognized-runtime/.tmp-synthetic",
    "nested/data/natural_score_continuations/record.json",
    "nested/data/natural_score_continuations/.lock",
    "services/untracked_continuation.py",
    "core/untracked_source.py",
    "config-untracked.yaml",
])
def test_continuation_ignore_does_not_hide_other_source_or_data(ignore_repository, relative):
    repository, env = ignore_repository
    result = subprocess.run(
        ["git", "-C", str(repository), "check-ignore", "-q", relative],
        env=env, text=True, capture_output=True, check=False, timeout=10,
    )
    assert result.returncode == 1, result.stderr


@pytest.mark.parametrize("relative", [
    "services/natural_scoring.py",
    "services/natural_score_continuations.py",
])
def test_continuation_policy_is_part_of_scoring_fingerprint(monkeypatch, relative):
    previous = _strategy_code_fingerprint()
    real_open = builtins.open

    def changed(path, mode="r", *args, **kwargs):
        if str(path).endswith(relative) and mode == "rb":
            with real_open(path, mode, *args, **kwargs) as source:
                return io.BytesIO(source.read() + b"\n# policy change\n")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", changed)
    assert _strategy_code_fingerprint() != previous
