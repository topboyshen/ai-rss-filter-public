"""Behavior checks for import cleanup, not merely a clean lint count."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_startup_logging_precedes_core_and_routes(tmp_path):
    program = """
import builtins
import logging
import sys

assert not logging.getLogger().handlers
original_import = builtins.__import__

def checked_import(name, *args, **kwargs):
    if name == 'core' or name.startswith(('core.', 'routes.')):
        root = logging.getLogger()
        assert root.level == logging.INFO, name
        assert root.handlers[0].stream is sys.stderr, name
    return original_import(name, *args, **kwargs)

builtins.__import__ = checked_import
import app
import routes.shared as shared
assert app.init_db is app.initialize_app
assert app._init_db is shared.init_db
assert app.app.url_map
"""
    db_path = tmp_path / "must-not-create.db"
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=ROOT,
        env={**os.environ, "AI_FILTER_DB_PATH": str(db_path)},
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not db_path.exists()
    assert "COPY logging_bootstrap.py ." in (ROOT / "Dockerfile").read_text()


def test_shared_compatibility_exports_remain_identical():
    import db
    from core.prompts import get_prompt_for_url
    from db import database
    from db.event_mapping_gate import EventMappingMigrationGateError
    import routes.shared as shared
    import utils.helpers as helpers

    assert shared.init_db is database.init_db
    assert shared.get_prompt_for_url is get_prompt_for_url
    assert database.EventMappingMigrationGateError is EventMappingMigrationGateError
    assert db.cleanup_test_records is database.cleanup_test_records
    assert "cleanup_test_records" in db.__all__
    for name in (
        "get_cached",
        "set_cached",
        "flush_stats",
        "extract_score_from_title",
        "extract_tag_from_title",
        "FeedEntry",
        "resolve_feed_url_to_cache_key",
        "strategy_cache_key",
        "clean_title_for_compare",
    ):
        assert getattr(shared, name) is getattr(helpers, name)


@pytest.mark.parametrize(
    "script",
    [
        "scripts/run_event_enrichment.py",
        "scripts/run_provenance_shadow.py",
        "workers/market_close_collector.py",
        "workers/security_master_collector.py",
    ],
)
def test_cli_import_bootstrap_works_outside_repository(tmp_path, script):
    db_path = tmp_path / "must-not-create.db"
    env = {**os.environ, "AI_FILTER_DB_PATH": str(db_path)}
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(ROOT / script), "--help"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "usage:" in result.stdout.lower()
    assert not db_path.exists()


@pytest.mark.parametrize("source", ["import os\n", "not valid Python !", "\xff"])
def test_circular_import_reader_tolerates_unreadable_source(tmp_path, source):
    from scripts.check_circular_imports import get_imports

    path = tmp_path / "example.py"
    path.write_bytes(source.encode("latin-1"))
    assert get_imports(path) == (["os"] if source.startswith("import") else [])
    assert get_imports(tmp_path / "missing.py") == []


def test_circular_import_reader_does_not_swallow_interrupts(monkeypatch, tmp_path):
    import scripts.check_circular_imports as checker

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(checker, "open", interrupted, raising=False)
    with pytest.raises(KeyboardInterrupt):
        checker.get_imports(tmp_path / "anything.py")


def test_lint_debt_allowances_are_empty():
    from scripts.check_lint import baseline_counts, pinned_ruff_version

    payload = json.loads((ROOT / ".ci" / "ruff-baseline.json").read_text())
    assert payload["findings"] == []
    assert baseline_counts(payload, pinned_ruff_version()) == {}


def test_ruff_version_has_a_single_source_of_truth():
    from scripts.check_lint import pinned_ruff_version

    version = pinned_ruff_version()
    assert "required-version" not in (ROOT / "ruff.toml").read_text()
    for relative in (
        "ruff.toml",
        "scripts/check_lint.py",
        "docs/lint-baseline.md",
        ".github/workflows/ci.yml",
    ):
        assert version not in (ROOT / relative).read_text(encoding="utf-8"), relative
