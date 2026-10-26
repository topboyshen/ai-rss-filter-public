"""Homepage changes share the daily report's evidence semantics, not its size."""

from contextlib import contextmanager
import sqlite3

import pytest

from services import event_daily_report as daily
from services import event_home_digest as home
from services.event_radar_query import EventRadarUnavailableError
from test_event_daily_report import _block, _evidence, _event, _mapping, _report_event, _ts


@pytest.fixture
def database():
    import db.database as database_module

    database_module.init_db()
    return database_module


def _seed(database, key, title, *, day=5, hour=10, priority=7, stance="supports"):
    return _report_event(
        database, key, title, f"https://news.example/{key}", at=_ts(day, hour),
        priority=priority, stance=stance,
    )


def test_home_uses_daily_identity_classification_gates_and_sort(database):
    new = _seed(database, "new", "甲公司获得乙公司订单", priority=8)
    pending = _seed(database, "pending", "丙公司计划建设工厂", hour=11, priority=9)
    advanced = _seed(database, "advanced", "丁公司公布项目进展", day=4, priority=7)
    _evidence(database, advanced, "https://news.example/advanced2", "丁公司项目正式投产", at=_ts(5, 10))
    denial = _seed(database, "denial", "戊公司否认获得订单", priority=1)
    request = _seed(database, "request", "交易所要求己公司澄清", priority=6)
    _seed(database, "low-request", "交易所要求庚公司澄清", priority=1)
    _seed(database, "low-new", "辛公司公布例行会议", priority=5)
    now = _ts(5, 12)
    report = daily.load_daily_report("2026-10-05", now=now)
    digest = home.load_home_event_digest(now=now)
    assert [item["event_id"] for item in digest["highlights"]] == [pending, new, advanced]
    assert [item["event_id"] for item in digest["corrections"]] == [request, denial]
    report_items = {
        item["event_id"]: item for block in report["blocks"] for item in block["entries"]
    }
    for item in digest["highlights"] + digest["corrections"]:
        assert item["block"] == report_items[item["event_id"]]["block"]
        assert item["change"] == report_items[item["event_id"]]["change"]
    assert digest["highlights"][0]["block_label"] == "待验证"
    assert digest["corrections"][0]["correction_kind"] == "request"
    assert "不是当事方" in digest["corrections"][0]["change"]
    assert digest["corrections"][1]["correction_kind"] == "denial"


def test_home_materializes_only_five_selected_events(database, monkeypatch):
    for number in range(12):
        _seed(database, f"new-{number}", f"公司{number}发布新品", priority=7 + number / 20)
        _seed(database, f"deny-{number}", f"企业{number}否认投资传闻", priority=1 + number / 20)
    calls = []
    original = daily._item_view

    def counted(*args, **kwargs):
        calls.append(args[0]["root_ids"])
        return original(*args, **kwargs)

    monkeypatch.setattr(daily, "_item_view", counted)
    monkeypatch.setattr(daily, "load_daily_report", lambda **_: pytest.fail("do not build the full report"))
    digest = home.load_home_event_digest(now=_ts(5, 12))
    assert len(digest["highlights"]) == 3 and len(digest["corrections"]) == 2
    assert len(calls) == 5


@pytest.mark.parametrize(
    ("claim", "name", "expected"),
    [
        ("数字人民币试点扩围", "数字人", []),
        ("中国新能源车销量增加", "国新能源", []),
        ("中国汽车工业协会公布报告", "中国中车", []),
        ("宁德时代发布新电池", "宁德时代", ["宁德时代"]),
    ],
)
def test_companies_need_original_claim_with_entity_disambiguation(database, claim, name, expected):
    event = _seed(database, "company", claim)
    _mapping(database, event, "300750", name, "direct", at=_ts(5, 10))
    digest = home.load_home_event_digest(now=_ts(5, 12))
    assert digest["highlights"][0]["companies"] == expected


def test_mutable_event_title_cannot_become_original_title_or_company_evidence(database):
    event = _event(database, "metadata", "国新能源获得订单", at=_ts(5, 10), priority=8)
    _evidence(database, event, "https://news.example/meta", "中国新能源车销量增加", at=_ts(5, 10))
    _mapping(database, event, "600617", "国新能源", "direct", at=_ts(5, 10))
    item = home.load_home_event_digest(now=_ts(5, 12))["highlights"][0]
    assert item["title"] == "中国新能源车销量增加"
    assert item["companies"] == []


def test_correction_source_is_newest_denial_not_older_support(database):
    event = _seed(database, "changing", "甲公司获得订单", hour=9)
    _evidence(database, event, "https://news.example/deny", "甲公司否认订单传闻", at=_ts(5, 11))
    item = home.load_home_event_digest(now=_ts(5, 12))["corrections"][0]
    assert item["title"] == "甲公司否认订单传闻"
    assert item["sources"][0]["url"] == "https://news.example/deny"
    assert "此前有" in item["change"]
    assert "最近入库" in item["observed_label"]


def test_hidden_older_source_cannot_supply_company_for_visible_headline(database):
    event = _seed(database, "older-named", "宁德时代发布新电池", hour=9)
    _mapping(database, event, "300750", "宁德时代", "direct", at=_ts(5, 9))
    _evidence(database, event, "https://news.example/newer", "电池行业发布报告", at=_ts(5, 11))
    item = home.load_home_event_digest(now=_ts(5, 12))["highlights"][0]
    assert item["title"] == "电池行业发布报告"
    assert item["companies"] == []


def test_latest_fresh_amount_binds_title_change_and_source(database):
    event = _seed(database, "amount-old", "甲公司获得项目订单", day=4)
    _evidence(database, event, "https://news.example/amount-3", "甲公司订单金额3亿元", at=_ts(5, 9))
    _evidence(database, event, "https://news.example/amount-8", "甲公司订单金额8亿元", at=_ts(5, 18))
    item = home.load_home_event_digest(now=_ts(5, 19))["highlights"][0]
    assert item["block"] == "advanced"
    assert item["title"] == "甲公司订单金额8亿元"
    assert "8亿元" in item["change"] and "3亿元" not in item["change"]
    assert item["sources"][0]["url"] == "https://news.example/amount-8"


def test_reposted_denial_is_background_not_primary_fresh_update(database):
    event = _seed(database, "denial-old", "甲公司否认订单传闻", day=4)
    _evidence(database, event, "https://news.example/denial-copy", "甲公司否认订单传闻", at=_ts(5, 9))
    _evidence(database, event, "https://news.example/new-update", "甲公司披露项目进展", at=_ts(5, 11))
    digest = home.load_home_event_digest(now=_ts(5, 12))
    assert digest["corrections"] == []
    item = digest["highlights"][0]
    assert item["block"] == "advanced"
    assert item["title"] == "甲公司披露项目进展"
    assert "甲公司披露项目进展" in item["change"]
    assert item["sources"][0]["url"] == "https://news.example/new-update"
    assert item["background_note"] == "此前已有否认／更正，新增报道不代表推翻"


def test_pending_primary_keeps_qualifier_even_when_newer_non_pending_source_exists(database):
    event = _seed(database, "pending-old", "甲公司披露投资项目", day=4)
    _evidence(database, event, "https://news.example/plan", "甲公司计划投资3亿元", at=_ts(5, 9))
    _evidence(database, event, "https://news.example/meeting", "甲公司召开项目讨论会", at=_ts(5, 11))
    item = home.load_home_event_digest(now=_ts(5, 12))["highlights"][0]
    assert item["block"] == "pending"
    assert item["title"] == "甲公司计划投资3亿元"
    assert "甲公司计划投资3亿元" in item["change"]
    assert item["sources"][0]["url"] == "https://news.example/plan"


def test_actual_new_denial_precedes_newer_request_and_old_denial_reprint(database):
    event = _seed(database, "prior-denial", "甲公司否认并购传闻", day=4)
    _evidence(database, event, "https://news.example/current-denial", "甲公司否认获得大额订单", at=_ts(5, 9))
    _evidence(database, event, "https://news.example/request", "交易所要求甲公司澄清", at=_ts(5, 10))
    _evidence(database, event, "https://news.example/old-reprint", "甲公司否认并购传闻", at=_ts(5, 11))
    item = home.load_home_event_digest(now=_ts(5, 12))["corrections"][0]
    assert item["correction_kind"] == "denial"
    assert item["title"] == "甲公司否认获得大额订单"
    assert "甲公司否认获得大额订单" in item["change"]
    assert item["sources"][0]["url"] == "https://news.example/current-denial"


def test_repeat_denial_does_not_reappear_as_today_change(database):
    _seed(database, "old-denial", "甲公司否认订单传闻", day=4, priority=1)
    _seed(database, "reprint", "甲公司否认订单传闻", day=5, priority=1)
    digest = home.load_home_event_digest(now=_ts(5, 12))
    assert digest["corrections"] == []
    assert digest["highlights"] == []
    assert digest["empty_label"] == "暂无达到首页展示门槛的事件变化，更多内容见日报"


def test_date_week_and_urls_use_beijing_build_day(database):
    digest = home.load_home_event_digest(now=_ts(5, 0))
    assert digest["date"] == "2026-10-05"
    assert digest["week"] == "2026-10-05"
    assert digest["daily_url"] == "/dashboard/events/daily?date=2026-10-05"
    assert digest["weekly_url"] == "/dashboard/events/weekly?week=2026-10-05"
    sunday = home.load_home_event_digest(now=_ts(4, 23, 59))
    assert sunday["week"] == "2026-09-28"
    assert "2026-10-04 23:59" in sunday["cutoff_label"]


def test_empty_data_is_insufficient_not_claim_no_events_happened(database):
    digest = home.load_home_event_digest(now=_ts(5, 12))
    assert digest["data_state"] == "before_coverage"
    assert "证据不足" in digest["data_state_label"]
    assert "不代表" in digest["data_state_label"]
    assert digest["freshness"]["stale"] is True
    assert not digest["highlights"] and not digest["corrections"]


def test_previous_day_only_is_no_ingestion_not_successful_empty(database):
    _seed(database, "yesterday", "甲公司获得订单", day=4)
    digest = home.load_home_event_digest(now=_ts(5, 12))
    assert digest["data_state"] == "no_ingestion"
    assert "不代表没有发生" in digest["data_state_label"]


def test_stale_and_truncated_report_remain_explicit(database, monkeypatch):
    _seed(database, "first", "甲公司获得订单", hour=1)
    _seed(database, "second", "乙公司获得订单", hour=2)
    stale = home.load_home_event_digest(now=_ts(5, 12))
    assert stale["data_state"] == "stale" and stale["freshness"]["stale"]
    monkeypatch.setattr(daily, "MAX_DAY_EVIDENCE_ROWS", 1)
    truncated = home.load_home_event_digest(now=_ts(5, 12))
    assert truncated["data_state"] == "truncated"
    assert "报告不完整" in truncated["data_state_label"]


def test_public_projection_omits_private_feed_and_internal_fields(database):
    event = _event(database, "private", "甲公司获得订单", at=_ts(5, 10))
    _evidence(
        database, event, "https://news.example/article", "甲公司获得订单", at=_ts(5, 10),
        publisher="https://private.example/feed?api_key=secret",
    )
    digest = home.load_home_event_digest(now=_ts(5, 12))
    item = digest["highlights"][0]
    assert "private.example" not in str(digest) and "secret" not in str(digest)
    assert item["sources"][0]["publisher"] == "news.example"
    assert not {"previous", "earliest", "uncertainties", "rule_association_count"} & item.keys()
    unsafe = home._public_source({"url": "javascript:alert(1)", "source_name": "private"})
    assert unsafe["url"] == "" and "source_name" not in unsafe


@pytest.mark.parametrize("url", [
    "https://user:secret@news.example/article",
    "https://user@news.example/article",
    "https://news.example/article?api_key=secret",
    "https://news.example/article?API-KEY=secret",
    "https://news.example/article?Api.Key=secret",
    "https://news.example/article?%61pi%5fkey=secret",
    "https://news.example/article?api%255fkey=secret",
    "https://news.example/article?token=secret",
    "https://news.example/article?Access_Token=secret",
    "https://news.example/article?t%6fken=secret",
    "https://news.example/article?article=123;token=secret",
    "https://news.example/article?X-Amz-Signature=secret",
    "https://news.example/article?SIGNATURE=secret",
    "https://news.example/article?sig=secret",
    "https://news.example/article#access_token=secret",
    "http://localhost/article", "http://localhost./article", "http://foo.localhost/article",
    "http://intranet/article", "http://news.internal/article", "http://news.local/article",
    "http://127.0.0.1/article", "http://192.168.1.2/article", "http://172.16.0.5/article",
    "http://10.0.0.5/article", "http://169.254.169.254/article", "http://100.64.0.1/article",
    "http://[::1]/article", "http://[fd00::1]/article", "http://[fe80::1]/article",
    "http://2130706433/article", "http://127.1/article", "http://0177.0.0.1/article",
    "http://0x7f.0.0.1/article", "http://news%2elocalhost/article",
])
def test_public_source_rejects_private_or_credential_urls_without_reflecting_value(url):
    result = home._public_source({"url": url, "source_name": url, "claim_text": "原文标题"})
    assert result["url"] == ""
    assert result["publisher"] == "来源链接不可用"
    assert url not in str(result) and "secret" not in str(result)


@pytest.mark.parametrize("url", [
    "https://www.cls.cn/telegraph?ai_filter_item=h_0123456789abcdef",
    "https://news.example/article?id=123&a=1&b=2",
    "https://news.example/article?articleid=123&page=2",
    "https://news.example/article?q=%E6%96%B0%E9%97%BB&category=market#details",
    "https://8.8.8.8/article?id=123",
    "https://[2001:4860:4860::8888]/article?id=123",
])
def test_public_source_preserves_safe_query_identities_exactly(url):
    assert home._public_source({"url": url})["url"] == url


def test_read_failure_raises_unavailable_instead_of_false_empty(database, monkeypatch):
    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("sensitive SQL/path")

    monkeypatch.setattr(daily, "prepare_daily_report", fail)
    with pytest.raises(EventRadarUnavailableError, match="home event digest is unavailable"):
        home.load_home_event_digest(now=_ts(5, 12))


def test_deadline_cleanup_rolls_back_clears_handler_and_keeps_read_only(database, monkeypatch):
    recorded = {}
    original = home.get_read_only_connection

    class TrackedConnection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def set_progress_handler(self, handler, steps):
            recorded.setdefault("handlers", []).append((handler is None, steps))
            self.conn.set_progress_handler(handler, steps)

    @contextmanager
    def tracked():
        with original() as conn:
            try:
                yield TrackedConnection(conn)
            finally:
                recorded["transaction"] = conn.in_transaction
                recorded["busy"] = conn.execute("PRAGMA busy_timeout").fetchone()[0]
                with pytest.raises(sqlite3.OperationalError):
                    conn.execute("CREATE TABLE forbidden_write(x)")

    monkeypatch.setattr(home, "get_read_only_connection", tracked)
    monkeypatch.setattr(home, "MAX_HOME_READ_SECONDS", -1)
    with pytest.raises(EventRadarUnavailableError):
        home.load_home_event_digest(now=_ts(5, 12))
    assert recorded["handlers"][-1] == (True, 0)
    assert recorded["transaction"] is False and recorded["busy"] == 100


def test_home_and_daily_preserve_numeric_identity_differences(database):
    _seed(database, "less", "投资金额≤5亿元", day=4)
    greater = _seed(database, "greater", "投资金额≥5亿元", day=5)
    digest = home.load_home_event_digest(now=_ts(5, 12))
    report = daily.load_daily_report("2026-10-05", now=_ts(5, 12))
    assert [item["event_id"] for item in digest["highlights"]] == [greater]
    assert _block(report, "new")["entries"][0]["event_id"] == greater
