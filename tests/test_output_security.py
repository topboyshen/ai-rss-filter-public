import time
import xml.etree.ElementTree as ET

from flask import Flask

from core.digest_engine import render_themes_html, render_themes_rss
from core.processor import build_html_feed, build_rss_xml
from services.backtest import render_backtest_compact_html, render_backtest_html
from utils.helpers import FeedEntry, escape_cdata, escape_html, safe_http_url


XSS = '<svg/onload=alert(1)>'


def test_output_helpers_escape_html_allow_only_http_urls_and_split_cdata():
    assert escape_html('" onmouseover="x') == '&quot; onmouseover=&quot;x'
    assert safe_http_url("https://example.com/a") == "https://example.com/a"
    assert safe_http_url("javascript:alert(1)") == "#"
    assert safe_http_url("data:text/html,x") == "#"
    assert safe_http_url("https://user:pass@example.com/private") == "#"
    assert escape_cdata("left]]>right") == "left]]]]><![CDATA[>right"


def test_processor_html_escapes_text_quotes_and_rejects_javascript_links():
    entry = FeedEntry(XSS, "javascript:alert(1)", "summary")
    entry.ai_summary = XSS
    html = build_html_feed([entry], XSS, "https://example.com", {})

    assert XSS not in html
    assert "&lt;svg/onload=alert(1)&gt;" in html
    assert 'href="#"' in html
    assert 'rel="noopener noreferrer"' in html

    quoted = FeedEntry("title", 'https://example.com/" onmouseover="alert(1)', "")
    quoted.ai_summary = ""
    quoted_html = build_html_feed([quoted], "title", "https://example.com", {})
    assert '" onmouseover="' not in quoted_html
    assert "&quot;" in quoted_html


def test_processor_rss_rejects_script_urls_and_splits_category_cdata(monkeypatch):
    entry = FeedEntry("title", "javascript:alert(1)", XSS)
    entry.ai_summary = XSS
    monkeypatch.setattr("core.processor.extract_tag_from_title", lambda _title: "tag]]>tail")

    xml = build_rss_xml(
        [entry],
        XSS,
        "javascript:alert(1)",
        'https://example.com/feed?x="quoted"',
        {},
    )
    root = ET.fromstring(xml)

    assert XSS not in xml
    assert "javascript:" not in xml
    assert "tag]]]]><![CDATA[>tail" in xml
    assert root.find("./channel/item/category").text == "tag]]>tail"
    assert "&quot;" in xml


def test_digest_html_escapes_all_llm_and_feed_fields():
    result = {
        "overall_assessment": XSS,
        "market_impact": XSS,
        "risk_alert": XSS,
        "generated_at": XSS,
        "themes": [{
            "direction": XSS,
            "theme": XSS,
            "importance": XSS,
            "insight": XSS,
            "market_impact": XSS,
            "background": XSS,
            "action": XSS,
            "articles": [{"title": XSS, "score": XSS, "link": "javascript:alert(1)"}],
        }],
    }

    html = render_themes_html(result, 48)

    assert XSS not in html
    assert html.count("&lt;svg/onload=alert(1)&gt;") >= 8
    assert "javascript:" not in html
    assert 'href="#"' in html
    assert 'rel="noopener noreferrer"' in html


def test_digest_rss_keeps_untrusted_html_inert_inside_cdata():
    result = {
        "overall_assessment": XSS,
        "generated_at": "now]]>later",
        "themes": [{
            "direction": f"看多{XSS}]]>tail",
            "theme": f"主题{XSS}]]>tail",
            "importance": 8,
            "insight": f"洞察{XSS}]]>tail",
            "market_impact": f"影响{XSS}]]>tail",
            "background": f"背景{XSS}]]>tail",
            "action": f"行动{XSS}]]>tail",
        }],
    }

    xml = render_themes_rss(result, 48)
    root = ET.fromstring(xml)

    assert XSS not in xml
    assert root.find("./channel/description").text == "&lt;svg/onload=alert(1)&gt;"
    assert "<svg" not in root.find("./channel/item/title").text
    assert "&lt;svg/onload=alert(1)&gt;]]&gt;tail" in root.find("./channel/item/title").text
    assert "<svg" not in root.find("./channel/item/description").text
    assert "&lt;svg/onload=alert(1)&gt;]]&gt;tail" in root.find("./channel/item/description").text


def test_root_page_escapes_dynamic_config_values(monkeypatch):
    import routes.dashboard_routes as dashboard_routes

    monkeypatch.setattr(
        dashboard_routes,
        "_cfg",
        lambda: {
            "feeds": [{"url": XSS}],
            "threshold": {"similarity": XSS},
        },
    )
    monkeypatch.setattr(
        dashboard_routes.DB,
        "get_feed_thresholds",
        lambda *_args, **_kwargs: {"notify": XSS, "pass": XSS},
    )
    app = Flask(__name__)
    app.register_blueprint(dashboard_routes.dash_bp)
    with app.test_client() as client:
        body = client.get("/").get_data(as_text=True)

    assert XSS not in body
    assert body.count("&lt;svg/onload=alert(1)&gt;") >= 4


def test_backtest_html_escapes_dynamic_result_fields():
    result = {
        "days": XSS,
        "deduped_samples": 1,
        "invalid_samples": 0,
        "noise_rate": 0,
        "raw_samples": 1,
        "dedupe_removed": 0,
        "note": XSS,
        "buckets": [{
            "bucket": XSS,
            "samples": 1,
            "avg_score": 8,
            "mapping_coverage": 100,
            "theme_follow_rate": 50,
            "top_themes": [{"theme": XSS, "count": 1}],
        }],
    }

    html = render_backtest_html(result)

    assert XSS not in html
    assert "&lt;svg/onload=alert(1)&gt;" in html


def test_backtest_html_hides_empty_buckets_and_withholds_immature_rates():
    result = {
        "requested_days": 90,
        "actual_start_date": "2026-07-25",
        "actual_end_date": "2026-07-26",
        "actual_window_days": 2,
        "deduped_samples": 1,
        "unique_signal_days": 1,
        "history_status": "accumulating",
        "min_statistical_samples": 30,
        "min_independent_result_days": 30,
        "raw_samples": 1,
        "dedupe_removed": 0,
        "invalid_samples": 0,
        "note": "still accumulating",
        "buckets": [
            {"bucket": "9.0+", "samples": 0},
            {
                "bucket": "8.0-8.9",
                "samples": 1,
                "unique_signal_days": 1,
                "avg_score": 8.5,
                "mapping_coverage": 100,
                "theme_follow_rate": 0,
                "asset_direction_samples": 1,
                "asset_direction_unique_days": 1,
                "asset_direction_win_rate": 100.0,
                "asset_direction_sample_sufficient": False,
                "top_themes": [{"theme": "AI算力", "count": 1}],
            },
        ],
    }

    html = render_backtest_html(result)

    assert "9.0+" not in html
    assert "8.0-8.9" in html
    assert "当前策略样本积累中" in html
    assert "待积累" in html
    assert "100.0%" not in html
    assert "口径与限制" in html


def test_backtest_compact_html_uses_production_maturity_and_escapes_fields():
    result = {
        "requested_days": 90,
        "actual_start_date": XSS,
        "actual_end_date": "2026-07-26",
        "actual_window_days": 2,
        "deduped_samples": 4,
        "unique_signal_days": 2,
        "proxy_coverage": 25.0,
        "history_status": "accumulating",
        "selection_policy": "stored_aggregation_threshold",
        "min_statistical_samples": 30,
        "min_independent_result_days": 30,
        "note": XSS,
        "buckets": [{
            "bucket": "8.0-8.9",
            "samples": 4,
            "asset_t1_direction_win_rate": 100.0,
            "asset_t1_direction_sample_sufficient": False,
        }],
    }

    html = render_backtest_compact_html(result)

    assert XSS not in html
    assert "&lt;svg/onload=alert(1)&gt;" in html
    assert "AI评分有效性" in html
    assert "生产入选口径" in html
    assert "请求90天，实际覆盖2天" in html
    assert "当前策略样本积累中" in html
    assert "25.0%" in html
    assert "100.0%" not in html
    assert "8.0-8.9" not in html
    assert "T+1" not in html
    assert XSS not in html
    assert 'href="/quality"' in html
    assert "历史关联，非交易回测" in html


def test_backtest_compact_html_labels_legacy_and_empty_history():
    legacy = render_backtest_compact_html({
        "requested_days": 90,
        "history_status": "legacy",
        "selection_policy": "legacy_current_threshold",
        "deduped_samples": 0,
        "buckets": [],
    })
    assert "短期缓存" in legacy
    assert "不代表请求的完整历史窗口" in legacy
    assert "—" in legacy
    assert "—%" not in legacy

    empty = render_backtest_compact_html({
        "requested_days": 90,
        "history_status": "accumulating",
        "selection_policy": "stored_aggregation_threshold",
        "deduped_samples": 0,
        "buckets": [],
    })
    assert "尚无有效信号快照" in empty


def test_sse_cards_escape_content_reject_script_links_and_add_rel():
    from sse_engine import _render_cards

    html = _render_cards([{
        "title": XSS,
        "score": 8.8,
        "tag": XSS,
        "logic": XSS,
        "link": "javascript:alert(1)",
        "time": int(time.time()),
        "assets": XSS,
        "sectors": XSS,
        "stocks": XSS,
        "score_reason": XSS,
        "watch": XSS,
    }])

    assert XSS not in html
    assert "javascript:" not in html
    assert 'href="#"' in html
    assert 'rel="noopener noreferrer"' in html
    assert "&lt;svg/onload=alert(1)&gt;" in html


def test_sse_cards_replace_conflicting_model_logic_and_hide_fastpass_hint():
    from sse_engine import _render_cards

    html = _render_cards([{
        "title": "央行公开市场操作",
        "score": 8.8,
        "tag": "宏观",
        "logic": "流动性边际改善；FastPass候选:宏观",
        "link": "https://example.com/liquidity",
        "time": int(time.time()),
    }])

    assert "流动性边际改善" not in html
    assert "政策或信用条件变化会影响估值修复和顺周期板块轮动" in html
    assert "FastPass候选" not in html


def test_main_web_keeps_polling_widget_without_stream_route():
    from app import app

    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert "/stream" not in rules
    assert "/glance-sse" in rules


def test_glance_widget_rejects_javascript_href(monkeypatch):
    from app import app
    import routes.dashboard_routes as dashboard_routes

    monkeypatch.setattr(
        dashboard_routes,
        "_get_briefing_articles",
        lambda **_kwargs: [(
            "javascript:alert(1)", "safe", "safe", 8.0, "tag", "logic", "summary", int(time.time()), "feed"
        )],
    )
    with app.test_client() as client:
        body = client.get("/glance").get_data(as_text=True)
    assert "javascript:" not in body
    assert 'href="#"' in body
