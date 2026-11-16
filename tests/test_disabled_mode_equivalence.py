"""Disabled admission must stay behaviorally inert in the sealed deployment.

The public line carries optional enforced-admission and controlled-startup
code. Production seals ``AI_FILTER_ADMISSION_MODE=disabled`` and omits every
controlled-startup operation variable, so these tests pin the legacy path the
running containers actually execute: ordinary request handling and one
database initialization, with no fence directory, lease, or operation claim.
"""

import pytest
from flask import Flask, Response

from services import admission_http, admission_fence, controlled_startup
from services import production_startup


def _forbidden(*_args, **_kwargs):
    pytest.fail("disabled admission must not inspect the request or reserve a lease")


def test_disabled_http_admission_is_inert_for_webhook_and_receipt(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_FILTER_ADMISSION_MODE", "disabled")
    monkeypatch.setenv("AI_FILTER_ADMISSION_ROOT", str(tmp_path / "never-created"))
    monkeypatch.setattr(admission_http, "request_kind", _forbidden)
    monkeypatch.setattr(admission_fence, "reserve_lease", _forbidden)
    application = Flask("disabled-admission-equivalence")
    application.add_url_rule(
        "/webhook", "webhook.webhook_receiver", lambda: Response(b"webhook"), methods=["POST"]
    )
    application.add_url_rule(
        "/webhook/receipt", "webhook.webhook_receipt", lambda: Response(b"receipt"), methods=["POST"]
    )
    admission_http.install_http_admission(application)

    client = application.test_client()
    assert client.post("/webhook", data=b"body").data == b"webhook"
    assert client.post("/webhook/receipt", data=b"body").data == b"receipt"
    assert not (tmp_path / "never-created").exists()


def test_disabled_runtime_initialization_keeps_legacy_single_call(monkeypatch, tmp_path):
    for variable in (
        "AI_FILTER_ADMISSION_MODE",
        controlled_startup.ENV_OPERATION,
        production_startup.ENV_OPERATION,
        production_startup.ENV_ROOT,
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("AI_FILTER_ADMISSION_ROOT", str(tmp_path / "never-created"))
    calls = []

    result = controlled_startup.initialize_runtime_database(
        "web", lambda: calls.append("init"), "http_database_initialize"
    )

    assert result is None
    assert calls == ["init"]
    assert not (tmp_path / "never-created").exists()
