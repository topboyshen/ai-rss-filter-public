"""Real-socket regression tests for the OpenAI SDK transport contract.

The scorer depends on the SDK only through ``OpenAI(api_key, base_url,
max_retries=0)``, ``chat.completions.create`` and the ``APITimeoutError`` /
``AuthenticationError`` / ``APIError`` hierarchy.  These tests drive the real
SDK client (no client stubs) against a local HTTP server so that transport
changes, such as openai 3.x moving from httpx to httpx2, are exercised
end to end: timeouts, 429s with and without Retry-After, 5xx, connection
refused and reset, malformed bodies, failover and the conservative local
fallback.  No external network is used.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import openai
import pytest

from core.scorer import LLMBackend, Scorer


TITLE = "中报窗口期机构密集调研，创新药步入业绩与国际化兑现期"
SUMMARY = "机构调研集中在创新药企业，关注中报业绩和国际化进展。"
VALID_SCORE = json.dumps(
    {
        "score": 8.5,
        "tag": "创新药",
        "logic": "业绩与国际化进入兑现期",
        "sentiment": "看多",
    },
    ensure_ascii=False,
)
CFG = {
    "scoring_strategy_version": "sdk-transport-regression",
    "llm_score_cache": {"enabled": True, "ttl_seconds": 3600},
    "local_scoring": {"enabled": True, "default_score": 4.2, "default_tag": "本地"},
}
BACKEND_TIMEOUT = 1


def _completion(content):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "mock-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    }


class _MockLLM:
    """Scriptable OpenAI-compatible server; one scenario per URL prefix."""

    def __init__(self):
        self.scenarios: dict[str, str] = {}
        self.hits: dict[str, int] = {}
        self.lock = threading.Lock()
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                return

            def _send(self, status, body, headers=None, content_type="application/json"):
                raw = body.encode() if isinstance(body, str) else body
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                prefix = self.path.split("/")[1]
                with mock.lock:
                    mock.hits[prefix] = mock.hits.get(prefix, 0) + 1
                scenario = mock.scenarios.get(prefix, "ok")
                error = json.dumps({"error": {"message": scenario, "type": "mock"}})
                if scenario == "ok":
                    self._send(200, json.dumps(_completion(VALID_SCORE), ensure_ascii=False))
                elif scenario == "timeout":
                    time.sleep(BACKEND_TIMEOUT + 1.5)
                    self._send(200, json.dumps(_completion(VALID_SCORE), ensure_ascii=False))
                elif scenario == "429":
                    self._send(429, error)
                elif scenario == "429_retry_after":
                    self._send(429, error, {"Retry-After": "30"})
                elif scenario in {"500", "502", "503", "401"}:
                    self._send(int(scenario), error)
                elif scenario == "reset":
                    self.connection.setsockopt(
                        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                    )
                    self.connection.close()
                    self.close_connection = True
                elif scenario == "malformed_json":
                    self._send(200, "{not json")
                elif scenario == "html_200":
                    self._send(200, "<html>gateway</html>", content_type="text/html")
                elif scenario == "missing_choices":
                    body = _completion(VALID_SCORE)
                    body.pop("choices")
                    self._send(200, json.dumps(body))
                elif scenario == "empty_choices":
                    body = _completion(VALID_SCORE)
                    body["choices"] = []
                    self._send(200, json.dumps(body))
                elif scenario == "null_content":
                    self._send(200, json.dumps(_completion(None)))
                else:  # pragma: no cover - scenario typo guard
                    self._send(599, error)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, prefix):
        return f"http://127.0.0.1:{self.port}/{prefix}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def mock_llm(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    server = _MockLLM()
    yield server
    server.close()


def _backend(name, base_url, **overrides):
    params = dict(
        name=name,
        api_key="test-key",
        base_url=base_url,
        model=f"{name}-model",
        rate_limit=0,
        max_wait_seconds=0,
        timeout=BACKEND_TIMEOUT,
        max_fail_count=3,
    )
    params.update(overrides)
    backend = LLMBackend(**params)
    assert isinstance(backend.client, openai.OpenAI)
    return backend


@pytest.fixture
def scorer_env(monkeypatch, mock_llm):
    scorer = Scorer()
    scorer.clear_runtime_cache()
    logged = []
    backoffs = []

    def log_call(purpose, backend, result=None, latency_ms=0, success=True, error_type="", cfg=None):
        logged.append((backend.name, success, error_type))

    monkeypatch.setattr(scorer, "_log_llm_call", log_call)
    monkeypatch.setattr(
        LLMBackend, "rate_limit_backoff", lambda self: backoffs.append(self.name)
    )

    def make(primary_url, secondary_url=None):
        backends = [_backend("kimi", primary_url, thinking="disabled")]
        if secondary_url is not None:
            backends.append(
                _backend("siliconflow", secondary_url, structured_output_mode="json_object")
            )
        monkeypatch.setattr(scorer, "backends", backends)
        return backends

    yield scorer, make, logged, backoffs
    scorer.clear_runtime_cache()


# scenario -> (SDK exception class name, primary counted as backend failure)
FAILURES = {
    "timeout": ("APITimeoutError", True),
    "429": ("RateLimitError", False),
    "429_retry_after": ("RateLimitError", False),
    "500": ("InternalServerError", True),
    "502": ("InternalServerError", True),
    "503": ("InternalServerError", True),
    "reset": ("APIConnectionError", True),
    "malformed_json": ("JSONDecodeError", True),
    "html_200": ("AttributeError", True),
    "missing_choices": ("TypeError", True),
    "empty_choices": ("IndexError", True),
    "null_content": ("AttributeError", True),
}


def _exception_name(scorer, backend):
    try:
        scorer._call_llm(backend, [{"role": "user", "content": "x"}], max_tokens=8, purpose="digest", cfg=CFG)
    except Exception as exc:  # noqa: BLE001 - classification is the assertion
        return type(exc).__name__, exc
    return None, None


@pytest.mark.parametrize("scenario", sorted(FAILURES))
def test_sdk_error_classes_and_single_attempt(scenario, scorer_env, mock_llm):
    scorer, make, logged, _ = scorer_env
    mock_llm.scenarios["kimi"] = scenario
    (backend,) = make(mock_llm.url("kimi"))

    started = time.monotonic()
    name, exc = _exception_name(scorer, backend)
    elapsed = time.monotonic() - started

    expected_name, _ = FAILURES[scenario]
    assert name == expected_name
    if isinstance(exc, openai.APIConnectionError):
        # The transport cause is httpx (openai 2.x) or httpx2 (openai 3.x).
        assert type(exc.__cause__).__module__.split(".")[0] in {"httpx", "httpx2"}
    if isinstance(exc, openai.APIStatusError):
        assert exc.status_code == int(scenario[:3])
    # max_retries=0: exactly one outbound request, no hidden Retry-After sleep.
    assert mock_llm.hits.get("kimi") == 1
    assert elapsed < BACKEND_TIMEOUT + 1.0
    assert logged == [("kimi", False, name)]


def test_connection_refused_is_api_connection_error(scorer_env):
    scorer, make, logged, _ = scorer_env
    (backend,) = make(f"http://127.0.0.1:{_closed_port()}/v1")
    name, exc = _exception_name(scorer, backend)
    assert name == "APIConnectionError"
    assert isinstance(exc, openai.APIError)
    assert logged == [("kimi", False, "APIConnectionError")]


def test_rate_limit_classifier_sees_sdk_status(scorer_env, mock_llm):
    scorer, make, _, _ = scorer_env
    mock_llm.scenarios["kimi"] = "429_retry_after"
    (backend,) = make(mock_llm.url("kimi"))
    _, exc = _exception_name(scorer, backend)
    assert isinstance(exc, openai.APIError)
    assert LLMBackend.is_rate_limit_error(exc)


@pytest.mark.parametrize("scenario", sorted(FAILURES) + ["refused", "401"])
def test_score_fails_over_to_secondary_backend(scenario, scorer_env, mock_llm):
    scorer, make, logged, backoffs = scorer_env
    if scenario == "refused":
        primary_url = f"http://127.0.0.1:{_closed_port()}/v1"
    else:
        mock_llm.scenarios["kimi"] = scenario
        primary_url = mock_llm.url("kimi")
    kimi, silicon = make(primary_url, mock_llm.url("sf"))

    result = scorer.score_stage1_with_identity(TITLE, SUMMARY, "跨行业 A 股产业提示词", CFG)

    assert result.values == (8.5, "创新药", "业绩与国际化进入兑现期", "📈")
    assert result.model_name == "siliconflow-model"
    assert mock_llm.hits.get("sf") == 1
    if scenario != "refused":
        assert mock_llm.hits.get("kimi") == 1
    assert logged[0][:2] == ("kimi", False)
    assert logged[-1] == ("siliconflow", True, "")
    if scenario.startswith("429"):
        assert backoffs == ["kimi"]
        assert kimi.fail_count == 0
    elif scenario == "401":
        assert logged[0][2] == "AuthenticationError"
        assert kimi.fail_count == kimi.max_fail_count
        assert not kimi.is_healthy()
    else:
        assert backoffs == []
        assert kimi.fail_count == 1
    assert silicon.fail_count == 0


@pytest.mark.parametrize("scenario", ["timeout", "503", "reset", "malformed_json", "empty_choices", "429"])
def test_all_backends_failing_uses_uncached_local_fallback(scenario, scorer_env, mock_llm):
    scorer, make, logged, _ = scorer_env
    mock_llm.scenarios["kimi"] = scenario
    mock_llm.scenarios["sf"] = scenario
    make(mock_llm.url("kimi"), mock_llm.url("sf"))

    result = scorer.score_stage1_with_identity(TITLE, SUMMARY, "跨行业 A 股产业提示词", CFG)

    assert result.model_name == "fallback_local"
    assert result.values[0] != 8.5
    assert [entry[:2] for entry in logged] == [("kimi", False), ("siliconflow", False)]
    assert scorer._score_cache == {}
    assert mock_llm.hits == {"kimi": 1, "sf": 1}


def test_environment_proxy_and_no_proxy_are_honoured(monkeypatch, mock_llm, scorer_env):
    scorer, make, _, _ = scorer_env
    dead_proxy = f"http://127.0.0.1:{_closed_port()}"
    monkeypatch.setenv("HTTP_PROXY", dead_proxy)
    monkeypatch.setenv("http_proxy", dead_proxy)

    # NO_PROXY covers the mock host: requests go direct and succeed.
    (direct,) = make(mock_llm.url("kimi"))
    name, _ = _exception_name(scorer, direct)
    assert name is None
    assert mock_llm.hits.get("kimi") == 1

    # Without NO_PROXY the client must route through the (dead) env proxy.
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    (proxied,) = make(mock_llm.url("kimi"))
    name, _ = _exception_name(scorer, proxied)
    assert name == "APIConnectionError"
    assert mock_llm.hits.get("kimi") == 1
