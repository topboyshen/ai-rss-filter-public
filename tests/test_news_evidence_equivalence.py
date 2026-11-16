"""Exact presentation equivalence against the locked pre-PR master.

BASELINE_SHA was rendered once, in an isolated checkout, using synthetic_cases
below and a frozen clock. The expected digests cover the *complete* HTML, not
selected text. CI needs neither git history nor a copied old renderer. Only
this PR's evidence details are excluded; card order, links, score/direction,
metadata, title mentions, rule pools, escaping and styling must stay exact.
"""

from copy import deepcopy
import hashlib
import re

import pytest

import sse_engine


BASELINE_SHA = "516a57a29acb438e48de58150a9c5625a4c6a98e"
NOW = 2_000_000_000
BASELINE_HTML_SHA256 = {
    ("empty", False): "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ("empty", True): "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ("facts_and_theme", False): "2b0374482630a8f5e46652160294e4468fd63b2b8029d4bc6be51b793578e57c",
    ("facts_and_theme", True): "84d1be156fdf19ea1dac9d47ea6bc2adfc05c4c6af2311416a7b62a4d8b60e40",
    ("ownership_and_legacy", False): "5aa2fbadbae109f4942b8b97d5b64d190ef82ec8782c36a800c5b27c4f5f4c2d",
    ("ownership_and_legacy", True): "c04c716b208760288392ff82f61a17baf57d48bf4049bb04c54fa438e2715998",
    ("escaping_and_long_title", False): "52c2e1dca3dbff2b34c18d1da38b7bba3f1831a8a9d53cad2b0c7a6b53626c86",
    ("escaping_and_long_title", True): "7a82df1c9b88a651a32e21bfd7761f55d92b83f81a264dc33f8a4d8e544a06e1",
}


def synthetic_cases():
    def entry(title, **fields):
        return {
            "title": title, "raw_title": title, "score": 8.8, "importance_score": 8.8,
            "direction": "看多", "score_path": "llm", "safe_neutral_reason": "",
            "link": "https://news.example/article", "source": "合成发布方", "time": NOW - 60,
            "_selection_threshold": 7.2, "_realtime_section": "fresh_strong", **fields,
        }

    title = "示例药业披露创新药研发进展"
    direct = {"raw_title": title, "status": "ok", "reason": "matched", "mentions": [{
        "security_id": "SSE:688001", "name": "示例药业", "ticker": "688001", "exchange": "SSE",
    }]}
    source_versions = {field: {"version": "synthetic:old", "value": value} for field, value in (
        ("score", 7.5), ("direction", "看空"), ("logic", "缓存理由"),
    )}
    legacy = entry("只有展示标题的旧记录", score_path="unknown")
    del legacy["raw_title"]
    return {
        "empty": [],
        "facts_and_theme": [
            entry(title, _title_equity_mentions=direct, duplicate_count=2),
            entry("创新药行业研发进展", direction="中性", score=7.2, importance_score=7.2,
                  link="https://news.example/watch", time=NOW - 13 * 3600,
                  _realtime_section="fallback", _title_equity_mentions={
                      "raw_title": "创新药行业研发进展", "status": "ok", "reason": "no_match", "mentions": [],
                  }),
        ],
        "ownership_and_legacy": [
            entry("事件方向与显示优先级分别保留", score=9.0, importance_score=7.5, direction="看空",
                  logic="缓存理由", _score_sources=source_versions,
                  _score_contract_current_version="synthetic:current"),
            entry("安全中性缓存记录", score=4.0, importance_score=4.0, direction="中性",
                  score_path="stage2_safe_neutral_grounding", safe_neutral_reason="facts",
                  link="https://news.example/neutral"),
            legacy,
            entry("空原始标题但有展示标题", raw_title="", link="https://news.example/empty-title"),
        ],
        "escaping_and_long_title": [
            entry('<script>attack()</script><em>完整新闻</em> 研发投入<5% & "计划" ' + "长标题，" * 50,
                  link="javascript:alert(1)", source='<svg/onload=attack()>',
                  tag="伪造公司", logic="伪造公司受益", summary="private-body"),
            entry("没有可用来源链接", link="", direction="看空", score_path="fast_pass"),
            entry("", raw_title="<br>"),
        ],
    }


def _without_evidence_context(html, expected_count):
    # The new details contains escaped fixed strings and no nested details.
    pattern = r'<details class="home-news-evidence"[^>]*>.*?</details>'
    clean, count = re.subn(pattern, "", html)
    assert count == expected_count
    assert "home-news-evidence" not in clean
    return clean


@pytest.mark.parametrize("case_name", list(synthetic_cases()))
@pytest.mark.parametrize("reading", [False, True])
def test_complete_html_matches_locked_master_outside_new_context(monkeypatch, case_name, reading):
    monkeypatch.setattr(sse_engine.time, "time", lambda: NOW)
    entries = synthetic_cases()[case_name]
    before = deepcopy(entries)
    html = sse_engine._render_cards(entries, reading=reading)
    original_html = _without_evidence_context(html, html.count("<article"))
    assert entries == before
    assert hashlib.sha256(original_html.encode()).hexdigest() == BASELINE_HTML_SHA256[(case_name, reading)]
