"""The CI synthetic candidate API must be browser-auth ready exactly like production."""
import os
import stat

import pytest

if not os.environ.get("AI_FILTER_DB_PATH"):
    raise RuntimeError("tests require conftest to set an isolated AI_FILTER_DB_PATH")
os.environ.setdefault("AI_FILTER_CONFIG_PATH", os.path.join(os.path.dirname(__file__), "..", "config.yaml"))

import app as app_module  # noqa: E402
from scripts import private_release_contract as private  # noqa: E402
from scripts import production_activation_fixture as fixture  # noqa: E402

API_KEY = "synthetic-never-a-production-secret-" + "0" * 32


@pytest.fixture
def web_client(monkeypatch):
    app_module.initialize_app()
    app_module.app.config["TESTING"] = True
    security = app_module.config.cfg.setdefault("security", {})
    original = security.get("api_key", "")
    security["api_key"] = API_KEY
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    monkeypatch.delenv("AI_FILTER_SESSION_SECRET", raising=False)
    monkeypatch.delenv("AI_FILTER_SESSION_SECRET_FILE", raising=False)
    try:
        with app_module.app.test_client() as client:
            yield client
    finally:
        security["api_key"] = original


def _manifest(tmp_path, secret):
    paths = {name: tmp_path / name for name in ("data", "fence", "spool", "journal", "grants", "slots")}
    return fixture.manifest_input(paths, "synthetic", "a" * 64, 2, {}, "owned-internal", secret)


def test_without_session_secret_candidate_web_reports_not_ready(web_client):
    # Reproduces CI run 40828784318: api_key present, no session secret -> 503 NOT READY.
    response = web_client.get("/health")
    assert response.status_code == 503 and response.data == b"NOT READY"


def test_fixture_session_secret_makes_candidate_web_ready(tmp_path, web_client, monkeypatch, capsys):
    secret_path = fixture.session_secret_file(tmp_path, API_KEY)
    web = _manifest(tmp_path, secret_path)["services"]["ai-filter-api"]
    monkeypatch.setenv("AI_FILTER_SESSION_SECRET", web["environment"]["AI_FILTER_SESSION_SECRET"])
    # In the container this is the /run/secrets bind of the same host file.
    monkeypatch.setenv("AI_FILTER_SESSION_SECRET_FILE", str(secret_path))
    response = web_client.get("/health")
    assert response.status_code == 200 and response.data == b"OK"
    secret = secret_path.read_text().strip()
    assert secret != API_KEY and 32 <= len(secret) <= 4096
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err


def test_session_secret_file_is_private_random_and_distinct(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = fixture.session_secret_file(tmp_path / "a", API_KEY)
    second = fixture.session_secret_file(tmp_path / "b", API_KEY)
    assert stat.S_IMODE(first.stat().st_mode) == 0o400
    assert stat.S_IMODE(first.parent.stat().st_mode) == 0o700
    assert first.read_bytes() != second.read_bytes()
    data, _ = private.private_file(first)
    payload = data.rstrip(b"\n")
    assert 32 <= len(payload) <= 4096 and all(0x21 <= value <= 0x7E for value in payload)
    with pytest.raises(FileExistsError):
        fixture.session_secret_file(tmp_path / "a", API_KEY)


def test_manifest_injects_secret_by_named_file_reference_only(tmp_path):
    secret_path = fixture.session_secret_file(tmp_path, API_KEY)
    manifest = _manifest(tmp_path, secret_path)
    secret = secret_path.read_text().strip()
    assert secret not in repr(manifest)
    assert manifest["secrets"] == {"dashboard_session_secret": {"file": str(secret_path)}}
    web, worker = manifest["services"]["ai-filter-api"], manifest["services"]["ai-filter-worker"]
    assert web["secrets"] == ["dashboard_session_secret"] and "secrets" not in worker
    assert web["environment"]["AI_FILTER_SESSION_SECRET"] == ""
    assert web["environment"]["AI_FILTER_SESSION_SECRET_FILE"] == "/run/secrets/dashboard_session_secret"
    private.validate_attachments(web, manifest, True)
    # Default (no secret) shape is unchanged for existing callers.
    assert "secrets" not in _manifest(tmp_path, None)
