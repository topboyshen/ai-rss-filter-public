"""Summary facts share the bounded source-event guard without extra attempts."""

import json
import os
from pathlib import Path
import select
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.scorer import (
    LLMBackend,
    Scorer,
    _SUMMARY_REPAIR_INSTRUCTION,
    _parse_summary_result,
    _summary_event_text,
    capture_llm_calls,
)


@pytest.mark.parametrize(
    ("title", "content", "summary"),
    [
        ("甲公司未获得项目订单", "", "甲公司获得项目订单"),
        ("甲公司计划获得项目订单", "", "甲公司已获得项目订单"),
        ("甲公司预计获得项目订单", "", "甲公司获得项目订单"),
        ("甲公司建设工厂并计划获得订单", "", "甲公司获得订单"),
        ("甲公司否认获得项目订单", "", "甲公司获得项目订单"),
        ("甲公司获得订单的传闻已被否认", "", "甲公司获得订单"),
        ("甲公司获批收购乙公司", "", "甲公司收购乙公司"),
        ("甲公司获得政府补助", "", "获得大额订单"),
        ("甲公司获得订单；乙公司获得订单", "", "获得订单"),
        ("海天味业发布股东大会通知", "公司将召开会议并审议议案。", "已获得重大订单"),
        ("甲公司发布进展公告", "<p>甲公司未获得项目订单。</p>", "甲公司获得项目订单"),
        ("甲公司今日未获得项目订单", "", "甲公司获得项目订单"),
        ("甲公司计划今日获得项目订单", "", "甲公司已获得项目订单"),
        ("甲公司表示未获得项目订单", "", "甲公司获得项目订单"),
        ("甲公司表示拟获得项目订单", "", "甲公司获得项目订单"),
        ("甲公司表示已获得项目订单", "", "乙公司获得项目订单"),
        ("甲公司表示已获得政府补助", "", "甲公司获得项目订单"),
        ("甲公司未表示获得项目订单", "", "甲公司未获得项目订单"),
        ("甲公司发布公告", "公司已获得项目订单。", "甲公司获得项目订单"),
    ],
)
def test_summary_rejects_identified_source_event_conflicts(title, content, summary):
    assert _parse_summary_result(
        json.dumps({"summary": summary}, ensure_ascii=False), title, content
    ) == ("", "ungrounded_event_context")


@pytest.mark.parametrize(
    ("title", "content", "summary"),
    [
        ("甲公司未获得项目订单", "", "甲公司未获得项目订单"),
        ("甲公司计划获得项目订单", "", "甲公司拟获得项目订单"),
        ("甲公司预计获得项目订单", "", "甲公司预计获得项目订单"),
        ("甲公司计划建设工厂并计划获得补助", "", "甲公司计划获得补助"),
        ("甲公司建设工厂并获得订单", "", "甲公司获得订单"),
        ("甲公司签署项目合同", "", "甲公司签订项目合同"),
        ("甲公司并购乙公司", "", "甲公司收购乙公司"),
        ("甲公司获得项目订单", "", "获得项目订单"),
        ("甲公司发布公告", "<p>甲公司取得政府补助。</p>", "甲公司获得政府补助"),
        ("央行开展逆回购操作", "流动性安排保持稳定。", "央行开展逆回购操作"),
        ("央行开展逆回购", "中标利率为1.40%。", "中标利率为1.4%。"),
        ("甲公司获得1.40亿元项目订单", "", "甲公司获得1.4亿元项目订单"),
        ("甲公司今日获得项目订单", "", "甲公司获得项目订单"),
        ("甲公司发布公告", "甲公司表示已获得项目订单。", "甲公司获得项目订单"),
        ("甲公司表示今日已获得项目订单", "", "甲公司获得项目订单"),
        ("甲公司计划今日获得项目订单", "", "甲公司拟获得项目订单"),
        ("甲公司今天未获得项目订单", "", "甲公司未获得项目订单"),
        ("甲公司今日称计划获得项目订单", "", "甲公司计划获得项目订单"),
        ("甲公司今日获得项目订单", "", "甲公司今日获得项目订单"),
        ("甲公司表示已获得项目订单", "", "甲公司表示已获得项目订单"),
    ],
)
def test_summary_keeps_grounded_compression_and_supported_paraphrases(title, content, summary):
    assert _parse_summary_result(
        json.dumps({"summary": summary}, ensure_ascii=False), title, content
    ) == (summary, "")


@pytest.mark.parametrize(
    ("source", "comparison"),
    [
        ("甲公司今日未获得项目订单", "甲公司未获得项目订单"),
        ("甲公司表示拟获得项目订单", "甲公司拟获得项目订单"),
        ("甲公司计划今日获得项目订单", "甲公司计划获得项目订单"),
        ("甲公司未表示获得项目订单", "甲公司未表示获得项目订单"),
        ("甲公司表示，乙公司未获得项目订单", "甲公司表示,乙公司未获得项目订单"),
        ("甲公司今日\n获得项目订单", "甲公司今日\n获得项目订单"),
        ("甲公司昨日获得项目订单", "甲公司昨日获得项目订单"),
    ],
)
def test_summary_comparison_does_not_erase_modals_or_cross_entity_boundaries(source, comparison):
    assert _summary_event_text(source) == comparison


def _run_bounded_summary_probe(setup, body, *, startup_timeout=10, timeout=3):
    """Separate cold imports from the unchanged post-startup hard deadline.

    A private READY pipe cannot be confused with import output. The child waits
    for GO before executing the measured body; both phases remain bounded.
    Linux CI/release gates and macOS local tests support pass_fds/select.
    """
    ready_read, ready_write = os.pipe()
    process = None
    phase = "startup"
    deadline = startup_timeout
    script = f"""import os, sys
{setup}
os.write(int(sys.argv[1]), b'R')
os.close(int(sys.argv[1]))
assert sys.stdin.read(1) == 'G', 'missing parent GO'
{body}
"""
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(ready_write)],
            cwd=Path(__file__).resolve().parents[1], pass_fds=(ready_write,),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        os.close(ready_write)
        ready_write = None
        if not select.select([ready_read], [], [], startup_timeout)[0]:
            raise subprocess.TimeoutExpired("summary probe startup", startup_timeout)
        if os.read(ready_read, 1) != b"R":
            stdout, stderr = process.communicate(timeout=timeout)
            raise AssertionError(f"summary probe exited before READY: {stdout}\n{stderr}")
        phase, deadline = "execution", timeout
        stdout, stderr = process.communicate(input="G", timeout=timeout)
        assert process.returncode == 0, f"summary probe execution failed: {stdout}\n{stderr}"
        return stdout
    except subprocess.TimeoutExpired as exc:
        process.kill()
        stdout, stderr = process.communicate()
        raise AssertionError(
            f"summary probe {phase} exceeded {deadline}s: {stdout}\n{stderr}"
        ) from exc
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()
        os.close(ready_read)
        if ready_write is not None:
            os.close(ready_write)


@pytest.mark.parametrize("count", [500, 2000])
@pytest.mark.parametrize("tail", ["公布公告", "获得项目订单"])
def test_long_source_bridge_is_bounded_and_stays_literal(count, tail):
    setup = """
import json
import time
from core.scorer import _parse_summary_result, _summary_event_text
"""
    body = f"""
source = "甲公司" + "今日" * {count} + {tail!r}
started = time.monotonic()
assert _summary_event_text(source) == source
assert _parse_summary_result(
    json.dumps({{"summary": "甲公司获得项目订单"}}, ensure_ascii=False), source, ""
) == ("", "ungrounded_event_context")
assert time.monotonic() - started < 0.5
"""
    _run_bounded_summary_probe(setup, body)


def test_summary_probe_startup_does_not_consume_execution_deadline():
    # Deliberately outlast the execution budget BEFORE READY. Import logging,
    # including partial lines, is not the control protocol.
    output = _run_bounded_summary_probe(
        "import time; print('import noise', end='', flush=True); time.sleep(3.2)",
        "print('core completed')",
    )
    assert output == "import noisecore completed\n"


@pytest.mark.parametrize(
    ("setup", "body", "startup_timeout", "timeout", "message"),
    [
        ("import time; time.sleep(30)", "pass", 0.1, 3, "startup exceeded"),
        ("import time", "time.sleep(30)", 10, 0.1, "execution exceeded"),
        ("raise RuntimeError('import failure')", "pass", 10, 3, "import failure"),
        ("", "assert False, 'core failure'", 10, 3, "core failure"),
        ("import sys; sys.exit(0)", "pass", 10, 3, "exited before READY"),
    ],
)
def test_summary_probe_failures_reap_process_and_close_pipe(
    monkeypatch, setup, body, startup_timeout, timeout, message,
):
    processes, pipes = [], []
    real_popen, real_pipe = subprocess.Popen, os.pipe

    def record_process(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    def record_pipe():
        pair = real_pipe()
        pipes.extend(pair)
        return pair

    monkeypatch.setattr(subprocess, "Popen", record_process)
    monkeypatch.setattr(os, "pipe", record_pipe)
    with pytest.raises(AssertionError, match=message):
        _run_bounded_summary_probe(
            setup, body, startup_timeout=startup_timeout, timeout=timeout,
        )
    assert len(processes) == 1 and processes[0].poll() is not None
    for descriptor in pipes:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def _summary_backend(name):
    backend = LLMBackend(
        name=name, api_key="", base_url="", model=name, rate_limit=0,
    )
    backend.client = object()
    return backend


@pytest.mark.parametrize(("repair", "attempts"), [(False, 1), (True, 2)])
def test_event_rejection_keeps_existing_attempt_budget_and_empty_fallback(
    monkeypatch, repair, attempts,
):
    backend = _summary_backend("summary-event")
    scorer = Scorer()
    monkeypatch.setattr(scorer, "backends", [backend])
    monkeypatch.setattr(scorer, "_log_llm_call", lambda *_a, **_k: None)
    create = Mock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"summary":"甲公司获得项目订单"}'))],
        usage=None,
        model=backend.model,
    ))
    backend.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with capture_llm_calls() as captured:
        result = scorer.summarize(
            "甲公司未获得项目订单",
            "甲公司未获得项目订单。公司披露本次进展并提醒读者以正式公告为准。" * 3,
            {
                "generate_summary": True,
                "llm_semantic_repair": {"enabled": repair},
                "llm_routing": {"summary": ["summary-event"]},
            },
        )
    assert result == ""
    assert create.call_count == captured.total == attempts
    assert captured.by_purpose == {"summary": attempts}
    assert backend.fail_count == 0


def test_event_rejection_uses_fixed_repair_and_accepts_grounded_result(monkeypatch):
    backend = _summary_backend("summary-repair")
    scorer = Scorer()
    monkeypatch.setattr(scorer, "backends", [backend])
    monkeypatch.setattr(scorer, "_log_llm_call", lambda *_a, **_k: None)
    rejected = '{"summary":"甲公司获得项目订单"}'
    responses = [rejected, '{"summary":"甲公司未获得项目订单"}']
    requests = []

    def respond(_backend, messages, *_args, **_kwargs):
        requests.append(messages)
        return responses.pop(0)

    monkeypatch.setattr(scorer, "_call_llm", respond)
    result = scorer.summarize(
        "甲公司未获得项目订单",
        "甲公司未获得项目订单。公司披露本次进展并提醒读者以正式公告为准。" * 3,
        {
            "generate_summary": True,
            "llm_semantic_repair": {"enabled": True},
            "llm_routing": {"summary": ["summary-repair"]},
        },
    )
    assert result == "甲公司未获得项目订单"
    assert len(requests) == 2
    assert _SUMMARY_REPAIR_INSTRUCTION in requests[1][0]["content"]
    assert rejected not in json.dumps(requests[1], ensure_ascii=False)


def test_event_rejection_falls_back_to_next_backend_without_new_retries(monkeypatch):
    first = _summary_backend("first")
    second = _summary_backend("second")
    scorer = Scorer()
    monkeypatch.setattr(scorer, "backends", [first, second])
    monkeypatch.setattr(scorer, "_log_llm_call", lambda *_a, **_k: None)
    calls = []

    def respond(backend, *_args, **_kwargs):
        calls.append(backend.name)
        return json.dumps(
            {"summary": "甲公司获得项目订单" if backend is first else "甲公司未获得项目订单"},
            ensure_ascii=False,
        )

    monkeypatch.setattr(scorer, "_call_llm", respond)
    assert scorer.summarize(
        "甲公司未获得项目订单",
        "甲公司未获得项目订单。公司披露本次进展并提醒读者以正式公告为准。" * 3,
        {
            "generate_summary": True,
            "llm_routing": {"summary": ["first", "second"]},
        },
    ) == "甲公司未获得项目订单"
    assert calls == ["first", "second"]
    assert first.fail_count == second.fail_count == 0
