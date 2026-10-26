import ast
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from services import quant_market_context as quant_context


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc)
TOKEN = "quant-market-context-token-32-bytes-minimum"


def _payload(**overrides):
    value = {
        "schema_version": quant_context.SCHEMA_VERSION,
        "generated_at": (NOW - timedelta(seconds=5)).isoformat().replace(
            "+00:00", "Z"
        ),
        "status": "ok",
        "market": {
            "available": True,
            "as_of": "2026-07-20",
            "expected_as_of": "2026-07-20",
            "age_days": 0,
            "is_stale": False,
            "source": "swing_macro",
            "breadth": {"limit_up": 34, "limit_down": 2},
            "margin_balance": 19000.5,
            "factors": [
                {
                    "factor": "total_amount",
                    "label": "全市场成交额",
                    "unit": "亿元",
                    "value": 12000.0,
                    "value_date": "2026-07-20",
                    "is_stale": False,
                    "percentile": 70.0,
                    "level": "正常",
                    "history_ready": True,
                    "collect_available": True,
                }
            ],
            "strategy": {
                "role": "core_trend_swing_eod",
                "decision_time": "15:10 Asia/Shanghai",
            },
        },
        "signals": {
            "current": [
                {
                    "decision_date": "2026-07-20",
                    "data_date": "2026-07-18",
                    "code": "510300",
                    "name": "沪深300ETF",
                    "signal_type": "趋势转强",
                    "period": "D",
                    "title": "沪深300ETF 趋势转强",
                    "confidence": 0.8,
                }
            ],
            "latest_decision_date": "2026-07-20",
            "recent": [],
            "effectiveness": _effectiveness(),
        },
        "risk": {
            "available": True,
            "level": "ok",
            "observed_at": (NOW - timedelta(minutes=5)).isoformat().replace(
                "+00:00", "Z"
            ),
            "age_seconds": 300,
            "is_stale": False,
            "is_future": False,
            "data_complete": True,
            "missing_fields": [],
            "cached_fields": [],
            "vix": 15.2,
            "us_10y": 4.1,
            "crude_oil": 80.0,
        },
        "quality": {"degraded_reasons": []},
    }
    value.update(overrides)
    return value


def _daily_audit(
    *,
    status="success",
    outcome="no_signal",
    signal_count=0,
    allowed_signal_count=None,
    blocked_signal_count=None,
    evaluated_count=10,
    failed_count=0,
    decision_date="2026-07-20",
    decision_eligible=True,
    eligibility_reason="scheduled_window",
    target_decisions=None,
):
    if allowed_signal_count is None:
        allowed_signal_count = 0 if outcome == "blocked" else signal_count
    if blocked_signal_count is None:
        blocked_signal_count = signal_count if outcome == "blocked" else 0
    return {
        "decision_date": decision_date,
        "data_date": decision_date,
        "run": {
            "run_id": "a" * 32,
            "status": status,
            "started_at": (NOW - timedelta(minutes=10)).isoformat().replace(
                "+00:00", "Z"
            ),
            "completed_at": (
                None
                if status in {"running", "not_run"}
                else (NOW - timedelta(minutes=9)).isoformat().replace("+00:00", "Z")
            ),
            "reason_codes": [],
            "decision_eligible": decision_eligible,
            "eligibility_reason": eligibility_reason,
        },
        "decision": {
            "outcome": outcome,
            "signal_count": signal_count,
            "allowed_signal_count": allowed_signal_count,
            "blocked_signal_count": blocked_signal_count,
        },
        "coverage": {
            "target_count": 10,
            "evaluated_count": evaluated_count,
            "failed_count": failed_count,
        },
        "target_decisions": target_decisions
        or (
            {
                "available": True,
                "no_trigger": 10 if outcome == "no_signal" else 9,
                "allowed": 1 if outcome == "emitted" else 0,
                "blocked": 1 if outcome == "blocked" else 0,
                "data_failed": 0,
            }
            if status == "success"
            else {
                "available": False,
                "no_trigger": 0,
                "allowed": 0,
                "blocked": 0,
                "data_failed": 0,
            }
        ),
    }


def _effectiveness(*, state="sample_insufficient", sample_size=0, minimum=30):
    return {
        "state": state,
        "sample_size": sample_size,
        "minimum_samples": minimum,
        "primary_horizon_sessions": 20,
        "method": "live_oos_or_walk_forward_oos",
        "metrics_available": state != "sample_insufficient",
        "horizon_samples": {"1": 0, "3": 0, "5": 0, "10": 0, "20": sample_size},
        "blocked_counterfactual_sample_size": 0,
    }


class _Response:
    def __init__(self, body, *, url=quant_context.FIXED_TUNNEL_URL, headers=None):
        self.status = 200
        self._body = body
        self._url = url
        self.headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
            **(headers or {}),
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self._url

    def read(self, limit):
        return self._body[:limit]


class _Opener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return self.response


@pytest.fixture(autouse=True)
def _reset_service(monkeypatch):
    quant_context._reset_quant_context_state_for_tests()
    monkeypatch.delenv("AI_FILTER_QUANT_CONTEXT_URL", raising=False)
    monkeypatch.delenv("AI_FILTER_QUANT_TOKEN_FILE", raising=False)
    monkeypatch.delenv("AI_FILTER_QUANT_CONTEXT_MAX_BYTES", raising=False)
    monkeypatch.delenv("AI_FILTER_QUANT_CONTEXT_MAX_AGE_SECONDS", raising=False)
    yield
    quant_context._reset_quant_context_state_for_tests()


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:18092/integration/v1/market-context",
        "http://host.docker.internal:18092/integration/v1/market-context?next=x",
        "http://user@host.docker.internal:18092/integration/v1/market-context",
        "http://host.docker.internal:18092/integration/v1/market-context/",
        "https://host.docker.internal:18092/integration/v1/market-context",
    ],
)
def test_url_allowlist_is_one_exact_internal_tunnel_endpoint(monkeypatch, url):
    monkeypatch.setenv("AI_FILTER_QUANT_CONTEXT_URL", url)
    with pytest.raises(quant_context.QuantContextError, match="allowlisted"):
        quant_context._validated_url()


def test_token_is_file_only_bounded_and_not_group_writable(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv("AI_FILTER_QUANT_TOKEN_FILE", str(token_file))

    assert quant_context._read_service_token() == TOKEN

    token_file.chmod(0o622)
    with pytest.raises(quant_context.QuantContextError, match="group/other"):
        quant_context._read_service_token()

    token_file.chmod(0o600)
    token_file.write_text("short", encoding="utf-8")
    with pytest.raises(quant_context.QuantContextError, match="length or format"):
        quant_context._read_service_token()


def test_http_client_sends_bearer_without_proxy_redirect_or_compression(monkeypatch):
    body = json.dumps(_payload()).encode("utf-8")
    opener = _Opener(_Response(body))

    fetched = quant_context._fetch_payload(
        quant_context.FIXED_TUNNEL_URL,
        TOKEN,
        opener=opener,
    )

    assert fetched["schema_version"] == quant_context.SCHEMA_VERSION
    request, timeout = opener.requests[0]
    assert request.full_url == quant_context.FIXED_TUNNEL_URL
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert request.get_header("Accept-encoding") == "identity"
    assert timeout == 3.0

    captured = []
    sentinel = object()
    monkeypatch.setattr(
        quant_context,
        "build_opener",
        lambda *handlers: captured.extend(handlers) or sentinel,
    )
    assert quant_context._build_http_opener() is sentinel
    proxy_handlers = [
        handler for handler in captured if handler.__class__.__name__ == "ProxyHandler"
    ]
    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {}
    assert any(isinstance(handler, quant_context._RejectRedirects) for handler in captured)


def test_http_client_rejects_changed_final_url_oversize_and_duplicate_keys(monkeypatch):
    body = json.dumps(_payload()).encode("utf-8")
    redirected = _Opener(_Response(body, url="http://example.invalid/context"))
    with pytest.raises(quant_context.QuantContextError, match="URL changed"):
        quant_context._fetch_payload(
            quant_context.FIXED_TUNNEL_URL, TOKEN, opener=redirected
        )

    monkeypatch.setenv("AI_FILTER_QUANT_CONTEXT_MAX_BYTES", "4096")
    oversized = _Opener(_Response(b"{" + b"x" * 5000 + b"}"))
    with pytest.raises(quant_context.QuantContextError, match="size limit"):
        quant_context._fetch_payload(
            quant_context.FIXED_TUNNEL_URL, TOKEN, opener=oversized
        )

    duplicate = _Opener(
        _Response(b'{"schema_version":"a","schema_version":"b"}')
    )
    with pytest.raises(quant_context.QuantContextError, match="duplicate"):
        quant_context._fetch_payload(
            quant_context.FIXED_TUNNEL_URL, TOKEN, opener=duplicate
        )


def test_contract_version_and_generated_at_are_strict():
    wrong = _payload(schema_version="quant.market-context.v2")
    with pytest.raises(quant_context.QuantContextError, match="version mismatch"):
        quant_context.validate_market_context(wrong, now=NOW)

    stale = _payload(
        generated_at=(NOW - timedelta(minutes=10)).isoformat()
    )
    with pytest.raises(quant_context.QuantContextError, match="response is stale"):
        quant_context.validate_market_context(stale, now=NOW)

    naive = _payload(generated_at="2026-07-20T04:00:00")
    with pytest.raises(quant_context.QuantContextError, match="timezone-aware"):
        quant_context.validate_market_context(naive, now=NOW)


def test_stale_nested_sections_are_projected_as_neutral(monkeypatch):
    payload = _payload()
    payload["market"]["as_of"] = "2026-06-01"
    payload["risk"]["observed_at"] = "2026-06-01T00:00:00Z"
    payload["risk"]["age_seconds"] = 999999

    normalized = quant_context.validate_market_context(payload, now=NOW)

    assert normalized["market"]["available"] is False
    assert normalized["signals"]["available"] is False
    assert normalized["signals"]["current"] == []
    assert normalized["risk"]["available"] is False
    assert normalized["risk"]["level"] == ""
    assert "consumer_market_snapshot_stale" in normalized["quality"]["degraded_reasons"]
    assert "consumer_risk_snapshot_stale" in normalized["quality"]["degraded_reasons"]


def test_signal_contract_uses_decision_and_data_dates_and_drops_invalid_rows():
    payload = _payload()
    valid = payload["signals"]["current"][0]
    payload["signals"]["current"] = [
        valid,
        {**valid, "decision_date": "2026-07-19", "data_date": "2026-07-20"},
        {
            **valid,
            "date": "2026-07-20",
            "decision_date": None,
            "data_date": "2026-07-19",
        },
    ]

    normalized = quant_context.validate_market_context(payload, now=NOW)

    assert normalized["status"] == "degraded"
    assert normalized["signals"]["current"] == [
        {
            "decision_date": "2026-07-20",
            "data_date": "2026-07-18",
            "code": "510300",
            "name": "沪深300ETF",
            "signal_type": "趋势转强",
            "period": "D",
            "title": "沪深300ETF 趋势转强",
            "confidence": 0.8,
        }
    ]
    assert "date" not in normalized["signals"]["current"][0]
    assert "consumer_signal_contract_invalid" in normalized["quality"][
        "degraded_reasons"
    ]


def test_current_signal_must_match_market_decision_date():
    payload = _payload()
    payload["signals"]["current"][0]["decision_date"] = "2026-07-19"
    payload["signals"]["current"][0]["data_date"] = "2026-07-18"

    normalized = quant_context.validate_market_context(payload, now=NOW)

    assert normalized["signals"]["current"] == []
    assert "consumer_signal_decision_date_mismatch" in normalized["quality"][
        "degraded_reasons"
    ]


@pytest.mark.parametrize(
    ("section", "flag", "reason"),
    [
        ("market", "is_stale", "consumer_market_reported_stale"),
        ("risk", "is_stale", "consumer_risk_snapshot_stale"),
        ("risk", "is_future", "consumer_risk_snapshot_from_future"),
    ],
)
def test_producer_staleness_flags_fail_sections_neutral(section, flag, reason):
    payload = _payload()
    payload[section][flag] = True

    normalized = quant_context.validate_market_context(payload, now=NOW)

    assert normalized[section]["available"] is False
    assert normalized["status"] == "degraded"
    assert reason in normalized["quality"]["degraded_reasons"]
    if section == "market":
        assert normalized["signals"]["available"] is False
        assert normalized["signals"]["current"] == []
    else:
        assert normalized["risk"]["vix"] is None


def test_missing_producer_staleness_flags_fail_closed():
    payload = _payload()
    payload["market"].pop("is_stale")
    payload["risk"].pop("is_future")

    normalized = quant_context.validate_market_context(payload, now=NOW)

    assert normalized["market"]["available"] is False
    assert normalized["risk"]["available"] is False
    assert "consumer_market_staleness_invalid" in normalized["quality"][
        "degraded_reasons"
    ]
    assert "consumer_risk_staleness_invalid" in normalized["quality"][
        "degraded_reasons"
    ]


def test_trading_calendar_expected_date_allows_long_holiday_snapshot():
    payload = _payload()
    payload["market"].update(
        {
            "as_of": "2026-07-10",
            "expected_as_of": "2026-07-10",
            "age_days": 10,
            "is_stale": False,
        }
    )
    payload["signals"]["current"][0].update(
        {"decision_date": "2026-07-10", "data_date": "2026-07-09"}
    )
    payload["signals"]["latest_decision_date"] = "2026-07-10"

    normalized = quant_context.validate_market_context(payload, now=NOW)

    assert normalized["market"]["available"] is True
    assert normalized["market"]["expected_as_of"] == "2026-07-10"
    assert normalized["signals"]["current"][0]["decision_date"] == "2026-07-10"
    assert "consumer_market_snapshot_stale" not in normalized["quality"][
        "degraded_reasons"
    ]


def test_market_hard_age_cap_remains_fail_neutral():
    payload = _payload()
    payload["market"].update(
        {
            "as_of": "2026-06-01",
            "expected_as_of": "2026-06-01",
            "age_days": 49,
            "is_stale": False,
        }
    )
    payload["signals"]["current"] = []
    payload["signals"]["latest_decision_date"] = None

    normalized = quant_context.validate_market_context(payload, now=NOW)

    assert normalized["market"]["available"] is False
    assert normalized["signals"]["available"] is False
    assert "consumer_market_hard_age_exceeded" in normalized["quality"][
        "degraded_reasons"
    ]


def test_market_expected_slice_consistency_fails_neutral():
    payload = _payload()
    payload["market"].update(
        {
            "as_of": "2026-07-17",
            "expected_as_of": "2026-07-20",
            "age_days": 3,
            "is_stale": False,
        }
    )
    payload["signals"]["current"] = []

    normalized = quant_context.validate_market_context(payload, now=NOW)

    assert normalized["market"]["available"] is False
    assert "consumer_market_expected_slice_missing" in normalized["quality"][
        "degraded_reasons"
    ]


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("as_of", "2026-07-21", "consumer_market_as_of_invalid"),
        (
            "expected_as_of",
            "2026-07-21",
            "consumer_market_expected_as_of_invalid",
        ),
        ("expected_as_of", None, "consumer_market_expected_as_of_invalid"),
    ],
)
def test_market_calendar_dates_cannot_be_future_or_missing(field, value, reason):
    payload = _payload()
    payload["market"][field] = value
    payload["market"]["is_stale"] = False

    normalized = quant_context.validate_market_context(payload, now=NOW)

    assert normalized["market"]["available"] is False
    assert normalized["signals"]["current"] == []
    assert reason in normalized["quality"]["degraded_reasons"]


def test_successful_poll_caches_only_validated_envelope(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv(
        "AI_FILTER_QUANT_CONTEXT_URL", quant_context.FIXED_TUNNEL_URL
    )
    monkeypatch.setenv("AI_FILTER_QUANT_TOKEN_FILE", str(token_file))
    writes = []
    monkeypatch.setattr(
        quant_context,
        "set_shared_json",
        lambda name, value, ttl: writes.append((name, value, ttl)) or True,
    )
    body = json.dumps(_payload()).encode("utf-8")

    assert quant_context.poll_quant_market_context_once(
        opener=_Opener(_Response(body)), now=NOW
    )
    name, envelope, ttl = writes[0]
    assert name == quant_context.CACHE_NAME
    assert ttl == 150
    assert envelope["cache_version"] == 1
    assert envelope["payload"]["schema_version"] == quant_context.SCHEMA_VERSION
    assert envelope["payload"]["signals"]["current"][0]["code"] == "510300"


def test_refresh_failure_does_not_delete_or_extend_previous_snapshot(monkeypatch):
    monkeypatch.setenv(
        "AI_FILTER_QUANT_CONTEXT_URL", quant_context.FIXED_TUNNEL_URL
    )
    monkeypatch.setattr(quant_context, "_read_service_token", lambda: TOKEN)
    writes = []
    monkeypatch.setattr(
        quant_context,
        "set_shared_json",
        lambda *_args: writes.append("unexpected") or True,
    )

    class BrokenOpener:
        def open(self, *_args, **_kwargs):
            raise OSError("tunnel unavailable")

    assert not quant_context.poll_quant_market_context_once(
        opener=BrokenOpener(), now=NOW
    )
    assert writes == []


def test_cached_snapshot_is_display_only_and_xss_safe(monkeypatch):
    payload = _payload()
    payload["risk"]["level"] = '<img src=x onerror="alert(1)">'
    payload["signals"]["current"][0]["title"] = "<script>alert(1)</script>"
    normalized = quant_context.validate_market_context(payload, now=NOW)
    envelope = {
        "cache_version": 1,
        "fetched_at": (NOW - timedelta(seconds=10)).timestamp(),
        "payload": normalized,
    }
    monkeypatch.setattr(quant_context, "get_shared_json", lambda _name: envelope)

    public = quant_context.get_quant_market_context(now=NOW)
    html = quant_context.render_quant_market_context_html(public)
    original = {"themes": []}
    attached = quant_context.attach_quant_market_context(original)

    assert public["decision_effect"] == "none"
    assert public["age_seconds"] == 10
    assert public["risk"]["vix"] == 15.2
    assert "<script>" not in html
    assert "<img" not in html
    assert "&lt;script&gt;" in html
    assert attached is not original
    assert "quant_context" not in original


def test_stale_or_unreadable_redis_snapshot_is_neutral(monkeypatch):
    normalized = quant_context.validate_market_context(_payload(), now=NOW)
    stale = {
        "cache_version": 1,
        "fetched_at": (NOW - timedelta(minutes=10)).timestamp(),
        "payload": normalized,
    }
    monkeypatch.setattr(quant_context, "get_shared_json", lambda _name: stale)
    context = quant_context.get_quant_market_context(now=NOW)
    assert context["available"] is False
    assert context["decision_effect"] == "none"
    assert context["reason"] == "cache_stale"

    monkeypatch.setattr(
        quant_context,
        "get_shared_json",
        lambda _name: (_ for _ in ()).throw(OSError("redis down")),
    )
    assert quant_context.get_quant_market_context(now=NOW)["available"] is False


def test_poller_lifecycle_is_singleton_and_stoppable(monkeypatch):
    monkeypatch.setenv(
        "AI_FILTER_QUANT_CONTEXT_URL", quant_context.FIXED_TUNNEL_URL
    )
    monkeypatch.setattr(quant_context, "_poll_interval_seconds", lambda: 1)
    called = threading.Event()
    monkeypatch.setattr(
        quant_context,
        "poll_quant_market_context_once",
        lambda: called.set() or True,
    )

    first = quant_context.start_quant_market_context_poller()
    second = quant_context.start_quant_market_context_poller()

    assert first is second
    assert first is not None and first.is_alive()
    assert called.wait(1.0)
    quant_context.stop_quant_market_context_poller(timeout=1.0)
    assert not first.is_alive()


def test_worker_wires_optional_poller_start_health_and_stop():
    source = (ROOT / "worker_main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "services.quant_market_context"
        for alias in node.names
    }
    assert {
        "quant_context_enabled",
        "start_quant_market_context_poller",
        "stop_quant_market_context_poller",
    } <= imported
    assert 'subsystems["quant_context"]' in source


def test_compose_scopes_quant_secret_to_worker_and_env_has_no_token_value():
    compose = yaml.safe_load(
        (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    services = compose["services"]
    assert compose["secrets"]["quant_market_context_token"]["file"] == (
        "./secrets/quant_market_context_token"
    )
    assert services["ai-filter-worker"]["secrets"] == [
        "quant_market_context_token"
    ]
    for name, service in services.items():
        if name != "ai-filter-worker":
            assert "quant_market_context_token" not in (service.get("secrets") or [])
    worker_env = services["ai-filter-worker"]["environment"]
    assert worker_env["AI_FILTER_QUANT_CONTEXT_URL"] == quant_context.FIXED_TUNNEL_URL
    assert worker_env["AI_FILTER_QUANT_TOKEN_FILE"] == (
        "/run/secrets/quant_market_context_token"
    )
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "AI_FILTER_QUANT_TOKEN=" not in env_example


def test_scoring_pipeline_has_no_quant_context_dependency():
    for relative in (
        "core/processor.py",
        "core/scorer.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "quant_market_context" not in source, relative


def test_data_cutoff_shows_market_slice_not_swing_decision_date(monkeypatch):
    """An idle swing strategy must not make the whole strip look stale."""
    payload = _payload()
    payload["signals"]["current"] = []
    payload["signals"]["latest_decision_date"] = "2026-07-17"
    normalized = quant_context.validate_market_context(payload, now=NOW)
    envelope = {
        "cache_version": 1,
        "fetched_at": (NOW - timedelta(seconds=10)).timestamp(),
        "payload": normalized,
    }
    monkeypatch.setattr(quant_context, "get_shared_json", lambda _name: envelope)

    public = quant_context.get_quant_market_context(now=NOW)
    html = quant_context.render_quant_market_context_html(public)
    text = quant_context.quant_market_context_text(public)

    # 收盘切片是 2026-07-20；07-17 只是最近一次已知信号日。
    assert "最近交易日收盘切片（2026-07-20）" in html
    assert "运行状态未知（旧版接口未提供审计）" in html
    assert "最近信号 2026-07-17" in html
    assert "行情截至" not in html
    assert "数据截至" not in html
    assert "最近交易日收盘切片（2026-07-20）" in text
    assert "最近信号 2026-07-17" in text
    assert "非当日涨跌判断" in html


def test_swing_annotation_is_omitted_when_decision_matches_market_slice(
    monkeypatch,
):
    payload = _payload()
    payload["signals"]["current"] = []
    normalized = quant_context.validate_market_context(payload, now=NOW)
    envelope = {
        "cache_version": 1,
        "fetched_at": (NOW - timedelta(seconds=10)).timestamp(),
        "payload": normalized,
    }
    monkeypatch.setattr(quant_context, "get_shared_json", lambda _name: envelope)

    html = quant_context.render_quant_market_context_html(
        quant_context.get_quant_market_context(now=NOW)
    )
    assert "量化日决策：运行状态未知（旧版接口未提供审计）" in html
    assert "无新信号" not in html
    assert "最近交易日收盘切片（2026-07-20）" in html


def test_successful_daily_audit_distinguishes_completed_no_signal(monkeypatch):
    payload = _payload()
    payload["signals"]["current"] = []
    payload["signals"]["daily_audit"] = _daily_audit()
    normalized = quant_context.validate_market_context(payload, now=NOW)
    envelope = {
        "cache_version": 1,
        "fetched_at": (NOW - timedelta(seconds=10)).timestamp(),
        "payload": normalized,
    }
    monkeypatch.setattr(quant_context, "get_shared_json", lambda _name: envelope)

    public = quant_context.get_quant_market_context(now=NOW)
    html = quant_context.render_quant_market_context_html(public)
    text = quant_context.quant_market_context_text(public)

    assert public["swing"]["audit"]["state"] == "valid"
    assert public["swing"]["audit"]["run_status"] == "success"
    assert "量化日决策（2026-07-20）：已完成 · 无新增可发布技术信号 · 已检查 10/10" in html
    assert "标的状态 未触发 10 / 放行 0 / 风控拦截 0 / 数据失败 0" in html
    assert "量化日决策（2026-07-20）：已完成 · 无新增可发布技术信号 · 已检查 10/10" in text
    assert "运行状态未知" not in html


def test_successful_emitted_audit_can_confirm_direction(monkeypatch):
    payload = _payload()
    payload["signals"]["daily_audit"] = _daily_audit(
        outcome="emitted", signal_count=1
    )
    normalized = quant_context.validate_market_context(payload, now=NOW)
    envelope = {
        "cache_version": 1,
        "fetched_at": (NOW - timedelta(seconds=10)).timestamp(),
        "payload": normalized,
    }
    monkeypatch.setattr(quant_context, "get_shared_json", lambda _name: envelope)
    public = quant_context.get_quant_market_context(now=NOW)
    public["comparison"] = quant_context._build_comparison(
        {"themes": [{"theme": "宽基", "direction": "看多"}]}, public
    )

    html = quant_context.render_quant_market_context_html(public)

    assert public["comparison"]["direction_axis"] == "support"
    assert "量化日决策（2026-07-20）：已完成 · 形成 1 个新技术信号 · 放行 1" in html


def test_all_signals_blocked_is_visible_but_cannot_confirm_direction(monkeypatch):
    payload = _payload()
    payload["signals"]["daily_audit"] = _daily_audit(
        outcome="blocked", signal_count=1
    )
    normalized = quant_context.validate_market_context(payload, now=NOW)
    envelope = {
        "cache_version": 1,
        "fetched_at": (NOW - timedelta(seconds=10)).timestamp(),
        "payload": normalized,
    }
    monkeypatch.setattr(quant_context, "get_shared_json", lambda _name: envelope)
    public = quant_context.get_quant_market_context(now=NOW)
    public["comparison"] = quant_context._build_comparison(
        {"themes": [{"theme": "宽基", "direction": "看多"}]}, public
    )

    html = quant_context.render_quant_market_context_html(public)

    assert public["comparison"]["direction_axis"] == "support"
    assert public["comparison"]["relation"] == "aligned_but_blocked"
    assert "形成 1 个新技术信号 · 全部被风控拦截" in html
    assert "无新信号" not in html


def test_failed_daily_audit_is_neutral_and_never_rendered_as_no_signal(monkeypatch):
    payload = _payload()
    payload["signals"]["current"] = []
    payload["signals"]["daily_audit"] = _daily_audit(
        status="failed",
        outcome="data_unavailable",
        evaluated_count=0,
        failed_count=10,
    )
    payload["risk"].update({"level": "critical", "vix": 18.7})
    normalized = quant_context.validate_market_context(payload, now=NOW)
    envelope = {
        "cache_version": 1,
        "fetched_at": (NOW - timedelta(seconds=10)).timestamp(),
        "payload": normalized,
    }
    monkeypatch.setattr(quant_context, "get_shared_json", lambda _name: envelope)
    public = quant_context.get_quant_market_context(now=NOW)
    public["comparison"] = quant_context._build_comparison(
        {"themes": [{"theme": "宽基", "direction": "看多"}]}, public
    )

    html = quant_context.render_quant_market_context_html(public)

    assert public["comparison"]["direction_axis"] == "unconfirmed"
    assert public["comparison"]["risk_axis"] == "critical"
    assert "运行失败 · 已检查 0/10 · 决策不可用" in html
    assert "无新信号" not in html
    assert "量化日决策运行失败" in html


def test_inconsistent_audit_and_current_signals_fail_neutral(monkeypatch):
    payload = _payload()
    payload["signals"]["daily_audit"] = _daily_audit()
    normalized = quant_context.validate_market_context(payload, now=NOW)
    envelope = {
        "cache_version": 1,
        "fetched_at": (NOW - timedelta(seconds=10)).timestamp(),
        "payload": normalized,
    }
    monkeypatch.setattr(quant_context, "get_shared_json", lambda _name: envelope)
    public = quant_context.get_quant_market_context(now=NOW)
    public["comparison"] = quant_context._build_comparison(
        {"themes": [{"theme": "宽基", "direction": "看多"}]}, public
    )

    html = quant_context.render_quant_market_context_html(public)

    assert public["swing"]["audit"]["consistent"] is False
    assert public["comparison"]["direction_axis"] == "unconfirmed"
    assert "运行与信号记录不一致 · 决策不可用" in html
    assert "量化运行审计不可用" in html


def test_running_daily_audit_is_not_rendered_as_no_signal(monkeypatch):
    payload = _payload()
    payload["signals"]["current"] = []
    payload["signals"]["daily_audit"] = _daily_audit(
        status="running",
        outcome="not_evaluated",
        evaluated_count=0,
    )
    normalized = quant_context.validate_market_context(payload, now=NOW)
    envelope = {
        "cache_version": 1,
        "fetched_at": (NOW - timedelta(seconds=10)).timestamp(),
        "payload": normalized,
    }
    monkeypatch.setattr(quant_context, "get_shared_json", lambda _name: envelope)

    html = quant_context.render_quant_market_context_html(
        quant_context.get_quant_market_context(now=NOW)
    )

    assert "运行中 · 尚未形成决策" in html
    assert "无新信号" not in html


def test_pre_window_partial_is_kept_as_audit_but_never_confirms_direction(
    monkeypatch,
):
    payload = _payload(status="degraded")
    payload["signals"]["current"] = []
    payload["signals"]["daily_audit"] = _daily_audit(
        status="partial",
        outcome="data_unavailable",
        evaluated_count=10,
        failed_count=0,
        decision_eligible=False,
        eligibility_reason="before_decision_window",
    )
    payload["quality"]["degraded_reasons"] = [
        "swing_audit_outside_decision_window"
    ]
    normalized = quant_context.validate_market_context(payload, now=NOW)
    envelope = {
        "cache_version": 1,
        "fetched_at": (NOW - timedelta(seconds=10)).timestamp(),
        "payload": normalized,
    }
    monkeypatch.setattr(quant_context, "get_shared_json", lambda _name: envelope)
    public = quant_context.get_quant_market_context(now=NOW)
    public["comparison"] = quant_context._build_comparison(
        {"themes": [{"theme": "宽基", "direction": "看多"}]}, public
    )

    html = quant_context.render_quant_market_context_html(public)

    assert public["swing"]["audit"]["decision_eligible"] is False
    assert public["comparison"]["direction_axis"] == "unconfirmed"
    assert public["comparison"]["relation"] == "data_limited"
    assert "非正式时点补跑" in html
    assert "partial 记录保留" in html
    assert "不构成 15:10 Asia/Shanghai 方向确认" in html
    assert "非正式时点补跑已隔离" in html


def test_no_trigger_means_ai_leads_not_direction_conflict(monkeypatch):
    payload = _payload()
    payload["signals"]["current"] = []
    payload["signals"]["daily_audit"] = _daily_audit()
    normalized = quant_context.validate_market_context(payload, now=NOW)
    envelope = {
        "cache_version": 1,
        "fetched_at": (NOW - timedelta(seconds=10)).timestamp(),
        "payload": normalized,
    }
    monkeypatch.setattr(quant_context, "get_shared_json", lambda _name: envelope)
    public = quant_context.get_quant_market_context(now=NOW)
    public["comparison"] = quant_context._build_comparison(
        {"themes": [{"theme": "宽基", "direction": "看多"}]}, public
    )

    assert public["comparison"]["relation"] == "ai_leads"
    assert public["comparison"]["relation_label"] == "AI领先 · Quant未确认"


def test_effectiveness_sample_gate_is_visible_without_exact_win_rate(monkeypatch):
    payload = _payload()
    payload["signals"]["effectiveness"] = _effectiveness(
        state="sample_insufficient", sample_size=0, minimum=30
    )
    normalized = quant_context.validate_market_context(payload, now=NOW)
    envelope = {
        "cache_version": 1,
        "fetched_at": (NOW - timedelta(seconds=10)).timestamp(),
        "payload": normalized,
    }
    monkeypatch.setattr(quant_context, "get_shared_json", lambda _name: envelope)

    public = quant_context.get_quant_market_context(now=NOW)
    html = quant_context.render_quant_market_context_html(public)
    text = quant_context.quant_market_context_text(public)

    assert public["swing"]["effectiveness"]["state"] == "sample_insufficient"
    assert public["swing"]["effectiveness"]["metrics_available"] is False
    assert "历史有效性：<b>样本不足 · 20个交易日成熟样本 0/30 · 精确胜率隐藏</b>" in html
    assert "历史有效性 样本不足 · 20个交易日成熟样本 0/30 · 精确胜率隐藏" in text
    assert "胜率 0" not in html


@pytest.mark.parametrize(
    ("sample_size", "state", "metrics_available", "accepted"),
    [
        (29, "sample_insufficient", False, True),
        (29, "sample_insufficient", True, False),
        (30, "uncertain", True, True),
        (30, "uncertain", False, False),
    ],
)
def test_effectiveness_contract_is_consistent_at_sample_boundary(
    sample_size, state, metrics_available, accepted
):
    value = _effectiveness(state=state, sample_size=sample_size, minimum=30)
    value["metrics_available"] = metrics_available

    result = quant_context._sanitize_effectiveness(value)

    assert (result is not None) is accepted


def test_degraded_snapshot_is_visibly_marked(monkeypatch):
    payload = _payload(status="degraded")
    payload["quality"]["degraded_reasons"] = ["factor_history_insufficient"]
    normalized = quant_context.validate_market_context(payload, now=NOW)
    envelope = {
        "cache_version": 1,
        "fetched_at": (NOW - timedelta(seconds=10)).timestamp(),
        "payload": normalized,
    }
    monkeypatch.setattr(quant_context, "get_shared_json", lambda _name: envelope)

    public = quant_context.get_quant_market_context(now=NOW)
    html = quant_context.render_quant_market_context_html(public)
    assert public["status"] == "degraded"
    assert "AI × Quant 双轴旁证" in html
    assert "只读 · 部分降级" in html
    assert "因子历史积累中" in html
    assert "#16a34a" in html


def test_bullish_briefing_and_critical_risk_keeps_direction_unconfirmed(monkeypatch):
    payload = _payload(status="degraded")
    payload["market"]["breadth"]["limit_up"] = 40
    payload["signals"]["current"] = []
    payload["signals"]["latest_decision_date"] = "2026-07-17"
    payload["risk"]["level"] = "critical"
    payload["risk"]["vix"] = 18.58
    payload["quality"]["degraded_reasons"] = [
        "consumer_market_factor_degraded",
        "factor_history_insufficient",
    ]
    normalized = quant_context.validate_market_context(payload, now=NOW)
    envelope = {
        "cache_version": 1,
        "fetched_at": (NOW - timedelta(seconds=10)).timestamp(),
        "payload": normalized,
    }
    monkeypatch.setattr(quant_context, "get_shared_json", lambda _name: envelope)
    public = quant_context.get_quant_market_context(now=NOW)
    monkeypatch.setattr(quant_context, "get_quant_market_context", lambda: public)

    briefing = quant_context.attach_quant_market_context({
        "themes": [
            {"theme": "AI算力", "direction": "看多"},
            {"theme": "芯片国产化", "direction": "看多"},
            {"theme": "存储", "direction": "中性"},
        ]
    })
    context = briefing["quant_context"]
    html = quant_context.render_quant_market_context_html(context)
    text = quant_context.quant_market_context_text(context)

    # Legacy payload has no formal no_trigger audit, so it remains independent
    # rather than pretending that Quant completed a decision.
    assert context["comparison"]["relation"] == "independent"
    assert context["comparison"]["relation_label"] == "双轴独立观察"
    assert context["comparison"]["direction_axis"] == "unconfirmed"
    assert context["comparison"]["risk_axis"] == "critical"
    assert context["risk"]["normalized_state"] == "critical"
    assert context["risk"]["scope"] == "global"
    assert "方向关系 双轴独立观察" in html
    assert "风险约束 强风险约束" in html
    assert "风险约束：<b>宏观风险为强约束</b>" in html
    assert "最近交易日收盘切片（2026-07-20）：收盘涨停 40 / 跌停 2" in html
    assert "VIX 18.58" in html
    assert "宏观风险为强约束" in html
    assert "非当日涨跌判断" in html
    assert "风险冲突" not in html
    assert "风险约束 宏观风险为强约束" in text
    assert "非当日涨跌判断" in text


def test_matching_quant_signal_and_briefing_render_as_direction_support(monkeypatch):
    normalized = quant_context.validate_market_context(_payload(), now=NOW)
    envelope = {
        "cache_version": 1,
        "fetched_at": (NOW - timedelta(seconds=10)).timestamp(),
        "payload": normalized,
    }
    monkeypatch.setattr(quant_context, "get_shared_json", lambda _name: envelope)
    public = quant_context.get_quant_market_context(now=NOW)
    monkeypatch.setattr(quant_context, "get_quant_market_context", lambda: public)

    briefing = quant_context.attach_quant_market_context({
        "themes": [{"theme": "宽基", "direction": "看多"}]
    })

    assert briefing["quant_context"]["comparison"]["relation"] == "dual_support"
    assert briefing["quant_context"]["comparison"]["relation_label"] == "双轴同向强化"
    assert briefing["quant_context"]["comparison"]["direction_axis"] == "support"
    assert briefing["quant_context"]["comparison"]["risk_axis"] == "stable"


@pytest.mark.parametrize(
    ("signal_type", "title"),
    [
        ("volume_breakout_ma60", "放量突破 MA60"),
        ("weekly_macd_golden_cross", "周线 MACD 金叉"),
    ],
)
def test_real_quant_signal_types_are_directional_bullish(
    monkeypatch, signal_type, title
):
    payload = _payload()
    payload["signals"]["current"][0].update(
        {"signal_type": signal_type, "title": title}
    )
    payload["signals"]["daily_audit"] = _daily_audit(
        outcome="emitted", signal_count=1
    )
    normalized = quant_context.validate_market_context(payload, now=NOW)
    envelope = {
        "cache_version": 1,
        "fetched_at": (NOW - timedelta(seconds=10)).timestamp(),
        "payload": normalized,
    }
    monkeypatch.setattr(quant_context, "get_shared_json", lambda _name: envelope)
    public = quant_context.get_quant_market_context(now=NOW)

    comparison = quant_context._build_comparison(
        {"themes": [{"theme": "宽基", "direction": "看多"}]}, public
    )

    assert quant_context._swing_bias(public) == "bullish"
    assert comparison["relation"] == "dual_support"
    assert comparison["direction_axis"] == "support"


@pytest.mark.parametrize(
    ("level", "state", "scope", "label", "color"),
    [
        ("ok", "stable", "global", "宏观平稳", "#16a34a"),
        ("normal", "stable", "global", "宏观平稳", "#16a34a"),
        ("low", "stable", "global", "低风险", "#16a34a"),
        ("medium", "caution", "global", "风险关注", "#d97706"),
        ("elevated", "caution", "global", "风险抬升", "#d97706"),
        ("high", "critical", "global", "高风险", "#dc2626"),
        ("critical", "critical", "global", "强风险约束", "#dc2626"),
        ("warning", "data_limited", "data", "数据告警", "#d97706"),
        (
            "growth_blocked",
            "growth_constrained",
            "growth",
            "成长风格受限",
            "#d97706",
        ),
        ("future_level", "unknown", "unknown", "状态未知", "#71717a"),
    ],
)
def test_risk_vocabulary_keeps_severity_quality_and_scope_separate(
    level, state, scope, label, color
):
    normalized = quant_context._normalize_risk_level(level, available=True)

    assert normalized == {
        "state": state,
        "scope": scope,
        "label": label,
        "color": color,
    }


@pytest.mark.parametrize(
    ("level", "risk_axis", "scope", "label", "summary"),
    [
        ("warning", "data_limited", "data", "数据告警", "风险数据存在缺口"),
        (
            "growth_blocked",
            "growth_constrained",
            "growth",
            "成长风格受限",
            "成长风格买入受限",
        ),
    ],
)
def test_non_global_producer_states_do_not_become_global_risk_conflicts(
    monkeypatch, level, risk_axis, scope, label, summary
):
    payload = _payload(status="degraded")
    payload["signals"]["current"] = []
    payload["risk"]["level"] = level
    normalized = quant_context.validate_market_context(payload, now=NOW)
    envelope = {
        "cache_version": 1,
        "fetched_at": (NOW - timedelta(seconds=10)).timestamp(),
        "payload": normalized,
    }
    monkeypatch.setattr(quant_context, "get_shared_json", lambda _name: envelope)
    public = quant_context.get_quant_market_context(now=NOW)
    comparison = quant_context._build_comparison(
        {"themes": [{"theme": "AI算力", "direction": "看多"}]}, public
    )
    public["comparison"] = comparison
    html = quant_context.render_quant_market_context_html(public)

    assert comparison["risk_axis"] == risk_axis
    assert comparison["risk_scope"] == scope
    assert comparison["relation"] == "independent"
    assert label in html
    assert summary in html
    assert "宏观风险制约" not in html
    assert "border-left:3px solid #d97706" in html


def test_direction_support_does_not_hide_critical_risk_axis(monkeypatch):
    payload = _payload()
    payload["risk"].update({"level": "critical", "vix": 18.58})
    normalized = quant_context.validate_market_context(payload, now=NOW)
    envelope = {
        "cache_version": 1,
        "fetched_at": (NOW - timedelta(seconds=10)).timestamp(),
        "payload": normalized,
    }
    monkeypatch.setattr(quant_context, "get_shared_json", lambda _name: envelope)
    public = quant_context.get_quant_market_context(now=NOW)
    public["comparison"] = quant_context._build_comparison(
        {"themes": [{"theme": "宽基", "direction": "看多"}]}, public
    )
    html = quant_context.render_quant_market_context_html(public)

    assert public["comparison"]["relation"] == "dual_support"
    assert public["comparison"]["direction_axis"] == "support"
    assert public["comparison"]["risk_axis"] == "critical"
    assert "方向关系 双轴同向强化" in html
    assert "风险约束 强风险约束" in html
    assert "方向关系：<b>双轴同向强化</b>" in html
    assert "风险约束：<b>宏观风险为强约束</b>" in html
    assert "border-left:3px solid #dc2626" in html


def test_bearish_theme_and_critical_risk_is_not_rendered_as_green_safety(monkeypatch):
    payload = _payload()
    payload["signals"]["current"] = []
    payload["risk"].update({"level": "critical", "vix": 18.58})
    normalized = quant_context.validate_market_context(payload, now=NOW)
    envelope = {
        "cache_version": 1,
        "fetched_at": (NOW - timedelta(seconds=10)).timestamp(),
        "payload": normalized,
    }
    monkeypatch.setattr(quant_context, "get_shared_json", lambda _name: envelope)
    public = quant_context.get_quant_market_context(now=NOW)
    public["comparison"] = quant_context._build_comparison(
        {"themes": [{"theme": "风险事件", "direction": "看空"}]}, public
    )
    html = quant_context.render_quant_market_context_html(public)

    assert public["comparison"]["relation"] == "independent"
    assert public["comparison"]["risk_axis"] == "critical"
    assert "宏观风险为强约束" in html
    assert "border-left:3px solid #dc2626" in html
    assert "#16a34a" not in html


def test_market_risk_and_cache_times_are_visibly_decoupled(monkeypatch):
    current = datetime(2026, 7, 27, 4, 0, tzinfo=timezone.utc)
    payload = _payload()
    payload["generated_at"] = (current - timedelta(seconds=5)).isoformat().replace(
        "+00:00", "Z"
    )
    payload["market"].update(
        {"as_of": "2026-07-24", "expected_as_of": "2026-07-24", "age_days": 3}
    )
    payload["signals"]["current"] = []
    payload["signals"]["latest_decision_date"] = "2026-07-21"
    payload["risk"]["observed_at"] = (
        current - timedelta(minutes=3)
    ).isoformat().replace("+00:00", "Z")
    payload["risk"]["age_seconds"] = 180
    normalized = quant_context.validate_market_context(payload, now=current)
    envelope = {
        "cache_version": 1,
        "fetched_at": (current - timedelta(seconds=18)).timestamp(),
        "payload": normalized,
    }
    monkeypatch.setattr(quant_context, "get_shared_json", lambda _name: envelope)

    public = quant_context.get_quant_market_context(now=current)
    html = quant_context.render_quant_market_context_html(public)

    assert public["risk"]["age_seconds"] == 180
    assert "最近交易日收盘切片（2026-07-24）" in html
    assert "量化日决策：运行状态未知（旧版接口未提供审计）" in html
    assert "最近信号 2026-07-21" in html
    assert "风控观测 3 分钟前" in html
    assert "快照同步 18 秒前" in html
    assert "非当日涨跌判断" in html
    assert "行情截至" not in html
    assert "今日盘面" not in html


# 10-22 生产实测：Quant-Server 附带的 research_pool 有 40 条，整包 107137 字节。
_PROD_MEASURED_BYTES = 107_137


def _payload_with_research_pool(target_bytes=_PROD_MEASURED_BYTES):
    payload = _payload()
    items = []
    payload["research_pool"] = {"schema_version": "x", "items": items}
    filler = 0
    while len(json.dumps(payload).encode("utf-8")) < target_bytes:
        items.append({"code": f"{510000 + filler}", "note": "研" * 800})
        filler += 1
    return payload


def _compose_quant_max_bytes():
    from pathlib import Path

    compose = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    for line in compose.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("AI_FILTER_QUANT_CONTEXT_MAX_BYTES:"):
            return int(line.split(":", 1)[1].strip())
    raise AssertionError("compose lacks AI_FILTER_QUANT_CONTEXT_MAX_BYTES")


def test_prod_sized_payload_with_research_pool_is_cached(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv(
        "AI_FILTER_QUANT_CONTEXT_URL", quant_context.FIXED_TUNNEL_URL
    )
    monkeypatch.setenv("AI_FILTER_QUANT_TOKEN_FILE", str(token_file))
    writes = []
    monkeypatch.setattr(
        quant_context,
        "set_shared_json",
        lambda name, value, ttl: writes.append(value) or True,
    )
    body = json.dumps(_payload_with_research_pool()).encode("utf-8")
    assert len(body) >= _PROD_MEASURED_BYTES

    # Both the code default and the value production actually runs with.
    for limit in (None, _compose_quant_max_bytes()):
        writes.clear()
        if limit is None:
            monkeypatch.delenv("AI_FILTER_QUANT_CONTEXT_MAX_BYTES", raising=False)
        else:
            monkeypatch.setenv("AI_FILTER_QUANT_CONTEXT_MAX_BYTES", str(limit))
        assert quant_context.poll_quant_market_context_once(
            opener=_Opener(_Response(body)), now=NOW
        ), limit
        assert "research_pool" not in writes[0]["payload"]


def test_compose_limit_keeps_headroom_over_measured_size():
    assert _compose_quant_max_bytes() >= 2 * _PROD_MEASURED_BYTES


def test_oversize_still_fails_neutral_with_sizes_in_log(monkeypatch, caplog):
    monkeypatch.setenv(
        "AI_FILTER_QUANT_CONTEXT_URL", quant_context.FIXED_TUNNEL_URL
    )
    monkeypatch.setattr(quant_context, "_read_service_token", lambda: TOKEN)
    writes = []
    monkeypatch.setattr(
        quant_context, "set_shared_json", lambda *_a: writes.append(1) or True
    )
    body = json.dumps(_payload_with_research_pool(300_000)).encode("utf-8")

    with caplog.at_level("WARNING"):
        assert not quant_context.poll_quant_market_context_once(
            opener=_Opener(_Response(body)), now=NOW
        )
    assert writes == []
    assert f"declared={len(body)} limit={256 * 1024}" in caplog.text

    # Without Content-Length the bounded read must still stop at the limit.
    no_length = _Response(body)
    no_length.headers.pop("Content-Length")
    with pytest.raises(quant_context.QuantContextError, match=r"limit=262144"):
        quant_context._fetch_payload(
            quant_context.FIXED_TUNNEL_URL, TOKEN, opener=_Opener(no_length)
        )
