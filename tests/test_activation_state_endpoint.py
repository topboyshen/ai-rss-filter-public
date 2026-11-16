"""只读 activation 状态端点：把「按意图关闭」与「状态未知」分开。

关闭期唯一可读的 ``/health`` 对 activation 全盲，而能读 activation 的
``/readiness`` 会先取准入租约、在关闭期被 503 挡在业务逻辑之前。发布于是可以
显示 healthy 而业务持续 503，外部也无从区分「按意图关闭」「正在恢复」与
「状态读不出来」。``GET /activation/state`` 只报告状态，不参与准入判定。
"""

import json

import pytest

import routes.admin_routes as admin_routes
import services.production_activation as activation
import services.production_startup as startup
from services import admission_http

API_KEY = "activation-state-test-key"


@pytest.fixture
def client(monkeypatch):
    import app as app_module

    monkeypatch.setattr(
        admin_routes,
        "check_api_key",
        lambda: __import__("flask").request.headers.get("X-API-Key") == API_KEY,
    )
    with app_module.app.test_client() as test_client:
        yield test_client


def _headers():
    return {"X-API-Key": API_KEY}


def _body(response):
    return json.loads(response.get_data(as_text=True))


def _arm(tmp_path):
    (tmp_path / "activation.arm.json").write_text("{}", encoding="utf-8")
    return tmp_path, {"state_path": str(tmp_path)}


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(startup, "enabled", lambda: True)


def test_disabled_startup_reports_not_applicable(client, monkeypatch):
    monkeypatch.setattr(startup, "enabled", lambda: False)

    response = client.get("/activation/state", headers=_headers())

    assert response.status_code == 200
    assert _body(response) == {"state": "not_applicable"}


def test_missing_arm_reports_closed_by_intent(client, enabled, monkeypatch, tmp_path):
    monkeypatch.setattr(startup, "load", lambda **_kwargs: (tmp_path, {}))

    response = client.get("/activation/state", headers=_headers())

    assert response.status_code == 200
    assert _body(response) == {"state": "closed-by-intent", "phase": "pre-activation"}


def test_confirmed_readiness_reports_open(client, enabled, monkeypatch, tmp_path):
    root, descriptor = _arm(tmp_path)
    monkeypatch.setattr(startup, "load", lambda **_kwargs: (root, descriptor))
    monkeypatch.setattr(activation, "readiness", lambda *_args, **_kwargs: {})

    response = client.get("/activation/state", headers=_headers())

    assert response.status_code == 200
    assert _body(response) == {"state": "open", "phase": "open"}


@pytest.mark.parametrize(
    "phase,expected",
    [("resuming", "resuming"), ("draining", "closing"), ("frozen", "closing")],
)
def test_in_flight_activation_reports_its_phase(
    client, enabled, monkeypatch, tmp_path, phase, expected
):
    root, descriptor = _arm(tmp_path)
    monkeypatch.setattr(startup, "load", lambda **_kwargs: (root, descriptor))

    def not_ready(*_args, **_kwargs):
        raise RuntimeError("activation readiness expired")

    monkeypatch.setattr(activation, "readiness", not_ready)
    monkeypatch.setattr(activation, "resume_phase", lambda *_args, **_kwargs: phase)

    response = client.get("/activation/state", headers=_headers())

    assert response.status_code == 200
    assert _body(response) == {"state": expected, "phase": phase}


def test_resume_open_without_ready_pulses_is_unknown(
    client, enabled, monkeypatch, tmp_path
):
    root, descriptor = _arm(tmp_path)
    monkeypatch.setattr(startup, "load", lambda **_kwargs: (root, descriptor))

    def not_ready(*_args, **_kwargs):
        raise RuntimeError("activation readiness expired")

    monkeypatch.setattr(activation, "readiness", not_ready)
    monkeypatch.setattr(activation, "resume_phase", lambda *_args, **_kwargs: "open")

    response = client.get("/activation/state", headers=_headers())

    assert response.status_code == 503
    assert _body(response) == {"state": "unknown", "reason": "RuntimeError"}


def test_unreadable_startup_state_is_unknown(client, enabled, monkeypatch):
    def broken(**_kwargs):
        raise startup.gate.AdmissionError("startup descriptor/source differs")

    monkeypatch.setattr(startup, "load", broken)

    response = client.get("/activation/state", headers=_headers())

    assert response.status_code == 503
    assert _body(response) == {"state": "unknown", "reason": "AdmissionError"}


def test_state_requires_the_api_key(client, enabled, monkeypatch, tmp_path):
    monkeypatch.setattr(startup, "load", lambda **_kwargs: (tmp_path, {}))

    response = client.get("/activation/state")

    assert response.status_code in (401, 403)


def test_state_endpoint_is_an_exact_get_only_exemption():
    with __import__("app").app.test_request_context("/activation/state", method="GET"):
        assert admission_http.request_kind() is None
    with __import__("app").app.test_request_context("/activation/state", method="POST"):
        assert admission_http.request_kind() == "intake"
    with __import__("app").app.test_request_context("/activation/state?llm=true", method="GET"):
        assert admission_http.request_kind() == "intake"
