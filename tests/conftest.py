import os
import shutil
import tempfile
from pathlib import Path

import pytest

# pytest imports conftest before collecting test modules. Force a unique,
# process-local database path here so even module-level ``import app`` calls
# cannot bind to a developer or production database. Each xdist worker gets a
# distinct directory because it imports this module in its own process.
_TEST_SESSION_DB_DIR = Path(tempfile.mkdtemp(prefix="ai-filter-pytest-"))
_TEST_SESSION_DB_PATH = _TEST_SESSION_DB_DIR / "collection-cache.db"
os.environ["AI_FILTER_DB_PATH"] = str(_TEST_SESSION_DB_PATH)


@pytest.fixture(autouse=True)
def isolate_ai_filter_db(tmp_path, monkeypatch):
    """Keep tests from writing to the production cache database."""
    db_path = tmp_path / "cache.db"
    monkeypatch.setenv("AI_FILTER_DB_PATH", str(db_path))

    import db.database as database

    monkeypatch.setattr(database, "DB_PATH", str(db_path))

    production_db = Path(__file__).resolve().parents[1] / "data" / "cache.db"
    assert Path(database.DB_PATH).resolve() != production_db.resolve()
    yield db_path


@pytest.fixture(autouse=True)
def isolate_score_evidence_output(tmp_path, monkeypatch):
    """Exercise enabled capture, but redirect production-configured output to the test sandbox."""
    import services.score_evidence_capture as capture
    from utils.score_artifacts import private_directory

    production = Path("/app/data/score_evidence")

    def private_test_directory(path):
        if path.is_relative_to(production):
            path = tmp_path / "score_evidence" / path.relative_to(production)
        return private_directory(path)

    monkeypatch.setattr(capture, "private_directory", private_test_directory)


def pytest_unconfigure(config):
    """Remove the collection-safe database only after all fixture teardown."""
    shutil.rmtree(_TEST_SESSION_DB_DIR, ignore_errors=True)
