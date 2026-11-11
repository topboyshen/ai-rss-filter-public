"""Anonymous /readiness must not republish raw refresh failures.

The persisted attempt contains the writer's failure text and may contain a
feed URL used as a fallback source name. Counters stay public so probes can
explain staleness; error text and private names require the API key.
"""
import json

import pytest

from services.ingestion_status import public_refresh_attempt


def _attempt():
    return {
        "version": 1,
        "refresh_id": "refresh-id-must-not-leak",
        "status": "partial",
        "started_at": 100,
        "finished_at": 200,
        "enabled_sources": 2,
        "succeeded_sources": 1,
        "failed_sources": 1,
        "partial_sources": 0,
        "source_unrecognized_entries": 0,
        "score_deferred_entries": 0,
        "score_terminal_unscored_entries": 0,
        "failures": [
            {
                "source": "http://rsshub:1200/private?token=abc",
                "error": "ConnectError: http://10.0.0.5:1200/feed refused",
            },
            {
                "source": "Example News",
                "error": "Timeout reading http://internal-host/feed",
            },
        ],
        "score_continuations": {
            "attempted": 3,
            "completed": 2,
            "deferred": 1,
            "terminal_unscored": 0,
            "errors": 1,
            "api_calls": 7,
            "remaining": {
                "total": 4,
                "active": 1,
                "due": 1,
                "terminal": 3,
                "completed": 2,
            },
        },
    }


def test_projection_keeps_counters_and_drops_private_detail():
    public = public_refresh_attempt(_attempt())

    assert public["status"] == "partial"
    assert public["enabled_sources"] == 2
    assert public["failed_sources"] == 1
    assert public["failures"] == {
        "count": 2,
        "sources": ["Example News"],
        "omitted_sources": 1,
    }
    assert "refresh_id" not in public
    assert public["score_continuations"] == {
        "attempted": 3,
        "completed": 2,
        "deferred": 1,
        "terminal_unscored": 0,
        "errors": 1,
        "remaining": {"total": 4, "active": 1, "due": 1, "terminal": 3},
    }
    encoded = json.dumps(public).lower()
    for marker in ("http", "rsshub", "internal-host", "token", "connecterror"):
        assert marker not in encoded


def test_projection_rejects_non_attempt_values():
    assert public_refresh_attempt(None) is None
    assert public_refresh_attempt("partial") is None


def test_projection_tolerates_malformed_entries():
    public = public_refresh_attempt({
        "status": "mystery",
        "enabled_sources": "2",
        "failures": [None, {"source": 5}, {"source": "ok"}],
        "score_continuations": {"remaining": {"active": "x"}, "errors": True},
    })

    assert public == {
        "failures": {"count": 3, "sources": ["ok"], "omitted_sources": 2},
        "score_continuations": {"remaining": {}},
    }


def _patch_readiness(monkeypatch, attempt, *, authorized):
    import routes.admin_routes as admin_routes

    monkeypatch.setattr(admin_routes, "check_api_key", lambda: authorized)
    monkeypatch.setattr(admin_routes, "_cfg", lambda: {})
    monkeypatch.setattr(admin_routes, "is_refresh_job_running", lambda: False)
    monkeypatch.setattr(
        admin_routes,
        "_staleness_info",
        lambda _cfg: {
            "last_full_refresh_at": None,
            "seconds_since_full_refresh": 60,
            "staleness_threshold_seconds": 14400,
            "stale": False,
            "degraded": True,
            "last_attempt": attempt,
            "last_ingestion_at": None,
        },
    )


def test_anonymous_readiness_redacts_failure_text(monkeypatch):
    from app import app

    _patch_readiness(monkeypatch, _attempt(), authorized=False)
    with app.test_client() as client:
        response = client.get("/readiness")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["degraded"] is True
    assert payload["last_attempt"]["failures"]["count"] == 2
    body = response.get_data(as_text=True).lower()
    for marker in ("http", "rsshub", "internal-host", "token", "connecterror",
                   "refresh-id-must-not-leak"):
        assert marker not in body


def test_anonymous_strict_readiness_stays_redacted(monkeypatch):
    from app import app

    _patch_readiness(monkeypatch, _attempt(), authorized=False)
    with app.test_client() as client:
        response = client.get("/readiness?strict=1")

    assert response.status_code == 503
    assert response.get_json()["reason"] == "degraded"
    assert "connecterror" not in response.get_data(as_text=True).lower()


def test_authenticated_readiness_keeps_full_diagnostics(monkeypatch):
    from app import app

    attempt = _attempt()
    _patch_readiness(monkeypatch, attempt, authorized=True)
    with app.test_client() as client:
        response = client.get("/readiness")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["last_attempt"] == attempt
    assert payload["last_attempt"]["failures"][1]["error"] == (
        "Timeout reading http://internal-host/feed"
    )


def test_readiness_does_not_open_database_when_attempt_is_patched(monkeypatch):
    import sqlite3

    from app import app

    _patch_readiness(monkeypatch, _attempt(), authorized=False)
    monkeypatch.setattr(
        sqlite3, "connect", lambda *_a, **_kw: pytest.fail("readiness opened SQLite")
    )
    with app.test_client() as client:
        response = client.get("/readiness")

    assert response.status_code == 200
