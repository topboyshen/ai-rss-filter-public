"""Final-outcome tests for the deterministic Event Radar daily report."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

TZ = ZoneInfo("Asia/Shanghai")


def _ts(day, hour, minute=0, second=0):
    return int(datetime(2026, 10, day, hour, minute, second, tzinfo=TZ).timestamp())


@pytest.fixture
def database():
    import db.database as database_module

    database_module.init_db()
    return database_module


def _event(database, key, title, *, at, priority=7.0, event_type="policy"):
    from db.event_core import resolve_or_create_event

    with database.get_connection() as conn:
        return resolve_or_create_event(
            conn, {"test": key}, title=title, event_type=event_type,
            direction="中性", priority=priority, occurred_at=at, now=at,
        )


def _evidence(database, event_id, url, claim, *, at, stance="supports",
              publisher="https://feed-a.example/rss", announced=True):
    from db.event_core import upsert_event_evidence

    with database.get_connection() as conn:
        return upsert_event_evidence(
            conn, event_id, source_uri=url, source_name=publisher,
            claim_key=f"claim:{url}:{claim}", claim_text=claim, stance=stance,
            announcement_at=(at - 60) if announced else None,
            observed_at=at, now=at,
        )


def _report_event(database, key, title, url, *, at, priority=7.0,
                  publisher="https://feed-a.example/rss", stance="supports"):
    event_id = _event(database, key, title, at=at, priority=priority)
    _evidence(database, event_id, url, title, at=at, publisher=publisher, stance=stance)
    return event_id


def _merge(database, source, target, *, at):
    from db.event_core import merge_events

    with database.get_connection() as conn:
        return merge_events(conn, source, target, reason="test", now=at)


def _mapping(database, event_id, ticker, name, relation, *, at):
    from db.event_core import upsert_event_security, upsert_security

    with database.get_connection() as conn:
        security_id = upsert_security(
            conn, exchange="SZSE", ticker=ticker, name=name, now=at,
        )
        upsert_event_security(
            conn, event_id, security_id, relation_type=relation,
            confidence=0.8, relevance=8.0, mapping_version="mapping-test-v1",
            now=at,
        )


def _report(day, now):
    from services.event_daily_report import load_daily_report

    return load_daily_report(f"2026-10-{day:02d}", now=now)


def _block(report, key):
    return next(block for block in report["blocks"] if block["key"] == key)


def _titles(report, key):
    return [item["title"] for item in _block(report, key)["entries"]]


@pytest.fixture
def client(database, monkeypatch):
    import app as app_module
    import routes.event_radar_routes as radar_routes
    import routes.security as security
    from routes.browser_auth import DashboardAuth

    monkeypatch.setattr(radar_routes, "get_dashboard_auth", lambda: DashboardAuth(kind="header"))
    with security._rate_limit_lock:
        security._rate_limit_store.clear()
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as test_client:
        yield test_client


def _page(client, monkeypatch, day, now):
    import services.event_daily_report as daily

    monkeypatch.setattr(daily.time, "time", lambda: now)
    response = client.get(f"/dashboard/events/daily?date=2026-10-{day:02d}")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _section(html, key):
    match = re.search(rf'data-block="{key}".*?</section>', html, re.S)
    assert match, key
    return match.group(0)


def test_historical_date_is_reconstructed_as_of_its_cutoff(database, client, monkeypatch):
    older = _report_event(database, "old", "央行宣布定向降准", "https://n.example/old",
                          at=_ts(4, 10))
    today = _report_event(database, "today", "工信部发布算力新规", "https://n.example/today",
                          at=_ts(5, 10))
    _report_event(database, "future", "次日才出现的事件", "https://n.example/future",
                  at=_ts(6, 9))
    # A merge recorded after the 5th must not rewrite the 5th's history.
    _merge(database, today, older, at=_ts(6, 12))

    report = _report(5, now=_ts(7, 8))
    assert _titles(report, "new") == ["工信部发布算力新规"]
    assert all("次日" not in title for block in report["blocks"] for title in
               [item["title"] for item in block["entries"]])
    assert report["cutoff_label"] == "截止 2026-10-05 23:59:59（北京时间）"
    assert "事件标题与事件评分为当前值" in report["historical_note"]
    assert report["next_date"] == "2026-10-06" and report["prev_date"] == "2026-10-04"

    html = _page(client, monkeypatch, 5, _ts(7, 8))
    assert "事件日报 · 2026-10-05" in html
    assert 'data-testid="historical-badge">按历史入库记录重建的视图，非当日封存日报<' in html
    assert report["historical_note"].startswith("按历史入库记录重建的视图，非当日封存日报：")
    assert 'data-testid="historical-badge"' not in _page(client, monkeypatch, 5, _ts(5, 23))
    assert "工信部发布算力新规" in _section(html, "new")
    assert "次日才出现的事件" not in html


def test_no_change_day_says_no_substantive_update_without_padding(database, client, monkeypatch):
    first = _report_event(database, "base", "国庆假期高速充电量同比增长60%",
                          "https://n.example/base", at=_ts(4, 9))
    repeat = _report_event(database, "repeat", "国庆假期高速充电量同比增长60%",
                           "https://other.example/repeat", at=_ts(5, 20),
                           publisher="https://feed-b.example/rss")
    _merge(database, repeat, first, at=_ts(5, 20, 5))

    report = _report(5, now=_ts(6, 1))
    assert report["data_state"] == "ok"
    assert report["repeat_event_count"] == 1
    for block in report["blocks"]:
        assert block["entries"] == []
        if block["key"] == "advanced":
            assert block["empty_label"].startswith("未识别到已关联事件的新进展（")
            assert "跨标题的后续报道无法自动识别" in block["empty_label"]
        else:
            assert block["empty_label"] == "无实质更新"

    html = _page(client, monkeypatch, 5, _ts(6, 1))
    for key in ("new", "correction", "pending"):
        section = _section(html, key)
        assert "无实质更新" in section and "event-card" not in section
    advanced = _section(html, "advanced")
    assert "未识别到已关联事件的新进展" in advanced and "无实质更新" not in advanced


def test_duplicate_reports_merge_and_report_count_is_not_importance(database, client, monkeypatch):
    first = _report_event(database, "dup-a", "横琴口岸单日客流突破15万人次，创新高",
                          "https://n.example/dup-a", at=_ts(5, 9), priority=6.5)
    second = _report_event(database, "dup-b", "横琴口岸单日客流突破15万人次 创新高",
                           "https://n.example/dup-b", at=_ts(5, 9, 30), priority=6.5,
                           publisher="https://feed-b.example/rss")
    for index in range(3):
        extra = _report_event(database, f"dup-x{index}", "横琴口岸单日客流突破15万人次，创新高",
                              f"https://n.example/dup-x{index}", at=_ts(5, 10, index),
                              priority=6.5, publisher=f"https://feed-{index}.example/rss")
        _merge(database, extra, first, at=_ts(5, 10, index + 5))
    _report_event(database, "single", "华为与高通达成专利交叉许可协议",
                  "https://n.example/single", at=_ts(5, 11), priority=8.0)

    report = _report(5, now=_ts(6, 1))
    items = _block(report, "new")["entries"]
    assert [item["title"] for item in items] == [
        "华为与高通达成专利交叉许可协议", "横琴口岸单日客流突破15万人次，创新高",
    ]
    merged = items[1]
    assert merged["report_count"] == 5 and merged["merged_group_count"] == 2
    assert {item["event_id"] for item in items} & {first, second}
    assert report["duplicate_groups_folded"] == 1

    section = _section(_page(client, monkeypatch, 5, _ts(6, 1)), "new")
    assert section.count('class="event-card"') == 2
    assert "当日 5 条报道已合并（数量不代表重要程度）" in section


def test_denial_is_a_correction_clue_and_older_support_never_overrides_it(database, client, monkeypatch):
    event_id = _event(database, "order", "某科技公司获得百亿算力订单", at=_ts(3, 9))
    for index in range(3):
        _evidence(database, event_id, f"https://n.example/order-{index}",
                  "某科技公司获得百亿算力订单", at=_ts(4, 9 + index),
                  publisher=f"https://feed-{index}.example/rss")
    _evidence(database, event_id, "https://n.example/order-denial",
              "某科技公司否认获得百亿算力订单", at=_ts(5, 15), stance="contradicts")
    # A lexicon-only denial whose original report is not linked, with a low score.
    _report_event(database, "rumor-denial", "洁雅股份：市场有关公司转型的传闻不实",
                  "https://n.example/jieya", at=_ts(5, 16), priority=3.0)

    report = _report(5, now=_ts(6, 1))
    reversal = {item["title"]: item for item in _block(report, "correction")["entries"]}
    assert set(reversal) == {"某科技公司获得百亿算力订单", "洁雅股份：市场有关公司转型的传闻不实"}
    for key in ("new", "advanced", "pending"):
        assert "某科技公司获得百亿算力订单" not in _titles(report, key)
    order = reversal["某科技公司获得百亿算力订单"]
    assert order["change"].startswith("此前有 3 条支持记录")
    assert "旧支持记录不覆盖此变化" in order["change"]
    assert "支持与否认/更正并存，旧支持记录不覆盖否认" in order["uncertainties"]
    jieya = reversal["洁雅股份：市场有关公司转型的传闻不实"]
    assert "被否认/更正的原始报道未能自动关联：证据不足" in jieya["uncertainties"]
    assert "否认/更正由标题措辞规则识别，未经人工或模型核验" in jieya["uncertainties"]
    # The denied rumor's wording ("传闻") must not turn the denial into a pending item.
    assert all("计划/传闻/预期" not in note for note in jieya["uncertainties"])

    html = _page(client, monkeypatch, 5, _ts(6, 1))
    assert "澄清／更正线索" in html and "反转" not in html
    section = _section(html, "correction")
    assert "某科技公司否认获得百亿算力订单" in section
    assert 'class="denial"' in section


@pytest.mark.parametrize("cross_root", [False, True])
@pytest.mark.parametrize(("original", "correction", "stance"), [
    ("某公司项目状态说明", "某公司项目状态说明", "contradicts"),
    ("某公司澄清 未参与该项目", "某公司澄清：未参与该项目", "supports"),
])
def test_new_denial_semantics_survive_an_identical_normalized_headline(
    database, cross_root, original, correction, stance,
):
    from services import event_weekly_report as weekly

    original_event = _report_event(
        database, "original-support", original, "https://n.example/original-support",
        at=_ts(4, 9),
    )
    target = _event(database, "new-denial", correction, at=_ts(5, 10)) if cross_root else original_event
    _evidence(
        database, target, "https://n.example/new-denial", correction,
        at=_ts(5, 10), stance=stance,
    )
    day = _report(5, now=_ts(12, 1))
    week = weekly.load_weekly_report("2026-10-05", now=_ts(12, 1))
    for report, label in ((day, "今日"), (week, "本周")):
        block = _block(report, "correction")
        assert block["total"] == len(block["entries"]) == 1
        item = block["entries"][0]
        assert item["correction_kind"] == "denial"
        assert f"{label}出现否认/更正：{correction}" in item["change"]
        assert "此前有 1 条支持记录" in item["change"]
        assert item["sources"][0]["url"] == "https://n.example/new-denial"
        assert item["previous"] == {"claim_text": original, "time_label": "2026-10-04 09:00"}
        assert report["repeat_event_count"] == 0


@pytest.mark.parametrize("cross_root", [False, True])
def test_identical_denial_repost_stays_repeat_between_support_endpoints(database, cross_root):
    title = "某公司项目状态说明"
    first = _report_event(database, "support-first", title, "https://n.example/support-first",
                          at=_ts(2, 9))
    denial = _event(database, "prior-denial", title, at=_ts(3, 9)) if cross_root else first
    _evidence(database, denial, "https://n.example/prior-denial", title,
              at=_ts(3, 9), stance="contradicts")
    last = _event(database, "support-last", title, at=_ts(4, 9)) if cross_root else first
    _evidence(database, last, "https://n.example/support-last", title, at=_ts(4, 9))
    current = _event(database, "denial-repost", title, at=_ts(5, 9)) if cross_root else first
    _evidence(database, current, "https://n.example/denial-repost", title,
              at=_ts(5, 9), stance="contradicts")

    report = _report(5, now=_ts(6, 1))
    assert report["repeat_event_count"] == 1
    assert _block(report, "correction")["total"] == 0
    assert all(block["entries"] == [] for block in report["blocks"])


@pytest.mark.parametrize("event_count", [1, 2])
def test_current_titles_do_not_change_original_groups_or_history(database, event_count):
    from services import event_weekly_report as weekly

    old = "日前独立历史标题"
    _report_event(database, "unrelated-prior", old, "https://n.example/unrelated-prior",
                  at=_ts(4, 9))
    left = _report_event(database, "independent-left", "当日独立事件甲",
                         "https://n.example/independent-left", at=_ts(5, 9), priority=6.5)
    event_ids = [left]
    if event_count == 2:
        event_ids.append(_report_event(
            database, "independent-right", "当日独立事件乙",
            "https://n.example/independent-right", at=_ts(5, 10), priority=8.0,
        ))

    def state(report):
        return {
            item["event_id"]: (block["key"], item["priority"], item["previous"], item["earliest"],
                                tuple(source["url"] for source in item["sources"]))
            for block in report["blocks"] for item in block["entries"]
        }

    before = _report(5, now=_ts(12, 1))
    with database.get_connection() as conn:
        placeholders = ",".join("?" for _ in event_ids)
        conn.execute(f"UPDATE events SET title=? WHERE event_id IN ({placeholders})", (old, *event_ids))
        conn.commit()
    after = _report(5, now=_ts(12, 1))
    week = weekly.load_weekly_report("2026-10-05", now=_ts(12, 1))
    for report in (after, week):
        assert report["event_group_count"] == event_count
        assert report["duplicate_groups_folded"] == 0
        assert report["repeat_event_count"] == 0
        assert _block(report, "new")["total"] == event_count
        assert state(report) == state(before)
        assert {item["title"] for item in _block(report, "new")["entries"]} == {old}
        assert "事件标题与事件评分为当前值" in report["historical_note"]


def test_later_support_does_not_overturn_an_earlier_denial(database):
    event_id = _event(database, "denied-first", "某药企新药获批上市", at=_ts(4, 9))
    _evidence(database, event_id, "https://n.example/drug-denial", "某药企澄清新药获批报道不实",
              at=_ts(4, 10))
    _evidence(database, event_id, "https://n.example/drug-new", "某药企新药获批上市细节披露",
              at=_ts(5, 10), publisher="https://feed-b.example/rss")

    item = _block(_report(5, now=_ts(6, 1)), "advanced")["entries"][0]
    assert any(note.startswith("此前已有否认/更正记录（2026-10-04 10:00，仅作背景）")
               and "新的报道不推翻该否认" in note for note in item["uncertainties"])


def test_missing_data_is_insufficient_evidence_never_absence(database, client, monkeypatch):
    _report_event(database, "d3", "十月三日的事件", "https://n.example/d3", at=_ts(3, 10))
    _report_event(database, "d5", "十月五日的事件", "https://n.example/d5", at=_ts(5, 10))
    event_id = _event(database, "no-time", "缺发布时间的事件", at=_ts(5, 11))
    _evidence(database, event_id, "https://n.example/no-time", "缺发布时间的事件",
              at=_ts(5, 11), announced=False)

    before = _report(2, now=_ts(5, 23))
    gap = _report(4, now=_ts(5, 23))
    assert before["data_state"] == "before_coverage"
    assert gap["data_state"] == "no_ingestion"
    for report in (before, gap):
        for block in report["blocks"]:
            assert block["empty_label"].startswith("证据不足")
            assert "无实质更新" not in block["empty_label"]
    assert "不代表没有发生" in gap["data_state_label"]

    html = _page(client, monkeypatch, 4, _ts(5, 23))
    assert "无实质更新" not in html
    assert html.count("证据不足") >= 4
    assert re.search(r"(?<!不代表)没有发生", html) is None

    item = next(item for item in _block(_report(5, now=_ts(5, 23)), "new")["entries"]
                if item["title"] == "缺发布时间的事件")
    assert item["sources"][0]["published_label"] == "发布时间：证据不足（仅有入库时间）"


def test_cutoff_and_freshness_labels_mark_partial_and_stale_days(database, client, monkeypatch):
    _report_event(database, "fresh", "上午入库的事件", "https://n.example/fresh",
                  at=_ts(5, 9, 20))
    fresh = _report(5, now=_ts(5, 9, 30))
    assert fresh["cutoff_label"] == "截止 2026-10-05 09:30:00（北京时间，当日未结束）"
    assert fresh["freshness"]["latest_label"] == "2026-10-05 09:20（北京时间）"
    assert fresh["freshness"]["age_label"] == "距截止 10 分钟"
    assert fresh["freshness"]["stale"] is False and fresh["is_today"] is True
    assert fresh["next_date"] == ""

    stale = _report(5, now=_ts(5, 18))
    assert stale["data_state"] == "stale"
    assert stale["freshness"]["age_label"] == "距截止 8 小时 40 分钟"
    empty = [block for block in stale["blocks"] if not block["entries"]]
    assert empty and all(
        block["empty_label"].startswith("证据不足：最近一次入库为 2026-10-05 09:20")
        for block in empty
    )

    html = _page(client, monkeypatch, 5, _ts(5, 18))
    scope = re.search(r'data-testid="report-scope".*?</section>', html, re.S).group(0)
    assert "截止 2026-10-05 18:00:00（北京时间，当日未结束）" in scope
    assert "最近一次入库 2026-10-05 09:20（北京时间），距截止 8 小时 40 分钟" in scope
    assert "覆盖范围：" in scope


def test_named_companies_stay_separate_from_rule_pool(database, client, monkeypatch):
    event_id = _report_event(database, "catl", "宁德时代获得欧洲储能大单",
                             "https://n.example/catl", at=_ts(5, 10), priority=7.5)
    _mapping(database, event_id, "300750", "宁德时代", "direct", at=_ts(5, 10, 1))
    _mapping(database, event_id, "002594", "比亚迪", "direct", at=_ts(5, 10, 1))
    _mapping(database, event_id, "300014", "亿纬锂能", "concept", at=_ts(5, 10, 1))
    _mapping(database, event_id, "002460", "赣锋锂业", "upstream", at=_ts(5, 10, 1))
    _mapping(database, event_id, "000001", "未来映射", "concept", at=_ts(5, 23, 59, 59) + 10)

    item = _block(_report(5, now=_ts(6, 1)), "new")["entries"][0]
    assert item["companies"] == ["宁德时代"]
    # Unnamed "direct" rule hits are association, not a company mention.
    assert item["rule_association_count"] == 3

    associations = re.search(
        r'data-testid="associations">(.*?)</dd>', _page(client, monkeypatch, 5, _ts(6, 1)), re.S
    ).group(1)
    assert "原文点名公司：宁德时代" in associations
    assert "主题规则池关联 3 只证券（规则池，不列出，不代表受益）" in associations
    for name in ("比亚迪", "亿纬锂能", "赣锋锂业", "未来映射"):
        assert name not in associations


def test_report_never_scores_win_rate_or_price_performance(database, client, monkeypatch):
    _report_event(database, "plan", "某公司拟收购海外锂矿", "https://n.example/plan",
                  at=_ts(5, 10))
    report = _report(5, now=_ts(6, 1))
    pending = _block(report, "pending")["entries"][0]
    assert pending["next_check"] == "等待当事方公告、官方文件或实际落地记录"
    assert "首次出现，表述为计划/传闻/预期" in pending["change"]
    forbidden = {"direction_hit", "gross_return", "excess_return", "outcomes", "win_rate"}
    assert not forbidden & set(report) and not forbidden & set(pending)

    html = _page(client, monkeypatch, 5, _ts(6, 1))
    assert html.count("胜率") == 1 and "本报告不评价投资胜率" in html
    assert "事件进展不等于股价表现" in html
    for word in ("收益率", "超额收益", "命中率", "推荐买入"):
        assert word not in html


def test_daily_report_reads_without_writes_or_model_calls(database):
    _report_event(database, "ro", "只读检查事件", "https://n.example/ro", at=_ts(5, 10))
    with database.get_connection() as conn:
        before = conn.execute("SELECT total_changes()").fetchone()[0]
        counts = [conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("events", "event_evidence", "llm_call_logs")]
    _report(5, now=_ts(6, 1))
    with database.get_connection() as conn:
        assert [conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("events", "event_evidence", "llm_call_logs")] == counts
        assert conn.execute("SELECT total_changes()").fetchone()[0] == before


@pytest.mark.parametrize("query", [
    "date=2026-13-01", "date=20261005", "date=2099-01-01", "date=2026-10-05&date=2026-10-04",
    "date=2026-10-05&tab=overview", "block=new&page=2", "date=2026-10-05&block=new",
    "date=2026-10-05&page=2", "date=2026-10-05&block=new&page=01",
    "date=&block=new&page=2", "date=%20&block=new&page=2",
    "date=%202026-10-05&block=new&page=2", "date=2026-10-05%20&block=new&page=2",
])
def test_daily_report_route_rejects_invalid_queries(client, monkeypatch, query):
    import services.event_daily_report as daily

    monkeypatch.setattr(daily.time, "time", lambda: _ts(6, 1))
    assert client.get(f"/dashboard/events/daily?{query}").status_code == 400


@pytest.mark.parametrize("title", [
    "深交所要求甲公司澄清不实传闻",
    "上交所要求公司就财报失实传闻作出澄清",
    "交易所要求甲公司说明是否曾否认订单传闻",
    "交易所要求甲公司回应问询函中的失实传闻",
    "收到问询函后公司被要求澄清不实传闻",
])
def test_denial_words_inside_clarification_requests_are_not_denials(
    database, client, monkeypatch, title
):
    _report_event(database, "request", title, "https://n.example/request",
                  at=_ts(5, 10), priority=6.5)
    item = _block(_report(5, now=_ts(6, 1)), "correction")["entries"][0]
    assert item["correction_kind"] == "request"
    assert item["change"].startswith("今日出现要求澄清/问询：")
    assert "今日出现否认/更正：" not in item["change"]
    assert "今日出现否认/更正：" not in _section(
        _page(client, monkeypatch, 5, _ts(6, 1)), "correction"
    )


@pytest.mark.parametrize("title", [
    "证监会要求提高上市公司质量",
    "证监会要求持续推进资本市场改革",
])
def test_regulatory_policy_demands_are_not_clarification_clues(database, title):
    _report_event(database, "policy", title, "https://n.example/policy",
                  at=_ts(5, 10), priority=6.5)
    report = _report(5, now=_ts(6, 1))
    assert _titles(report, "new") == [title]
    assert _block(report, "correction")["total"] == 0


@pytest.mark.parametrize("title", [
    "收到问询函后，公司否认重组传闻",
    "深交所要求澄清重组传闻；甲公司否认接触交易对手",
    "甲公司澄清重组传闻不实，回应交易所要求说明",
    "甲公司否认重组传闻并回应问询函",
    "收到问询函后公司否认重组传闻",
    "公司否认重组传闻并回应交易所要求澄清",
    "甲公司回复深交所问询函称重组传闻不实",
    "甲公司回应深交所问询函并否认重组传闻",
])
def test_explicit_company_denials_survive_an_adjacent_request(database, title):
    _report_event(database, "answer", title, "https://n.example/answer",
                  at=_ts(5, 10), priority=3.0)
    item = _block(_report(5, now=_ts(6, 1)), "correction")["entries"][0]
    assert item["correction_kind"] == "denial"
    assert item["change"].startswith("今日出现否认/更正：")


def test_daily_report_login_return_path_is_allowed():
    from routes.browser_auth import _safe_next

    assert _safe_next("/dashboard/events/daily") == "/dashboard/events/daily"


def test_changed_numbers_are_not_hidden_as_duplicates(database, client, monkeypatch):
    _report_event(database, "pct-old", "某指数日内跌1.5%", "https://n.example/pct-old",
                  at=_ts(4, 10))
    _report_event(database, "pct-new", "某指数日内跌15%", "https://n.example/pct-new",
                  at=_ts(5, 10))
    _report_event(database, "pct-repeat", "某指数日内跌 1.5%。", "https://n.example/pct-rep",
                  at=_ts(5, 10, 30), publisher="https://feed-b.example/rss")
    _report_event(database, "minus", "美元指数-5%", "https://n.example/minus", at=_ts(5, 11))
    _report_event(database, "plus", "美元指数+5%", "https://n.example/plus", at=_ts(5, 11, 5))
    _report_event(database, "minus-fw", "美元指数－5％", "https://n.example/minus-fw",
                  at=_ts(5, 11, 10), publisher="https://feed-b.example/rss")

    report = _report(5, now=_ts(6, 1))
    new = {item["title"]: item for item in _block(report, "new")["entries"]}
    # Next-day figure change is a new item; a punctuation-only repost is not.
    assert set(new) == {"某指数日内跌15%", "美元指数-5%", "美元指数+5%"}
    assert report["repeat_event_count"] == 1
    assert new["美元指数-5%"]["merged_group_count"] == 2
    assert new["美元指数+5%"]["merged_group_count"] == 1
    section = _section(_page(client, monkeypatch, 5, _ts(6, 1)), "new")
    assert "某指数日内跌15%" in section and "美元指数+5%" in section


def test_currency_term_is_not_reported_as_named_company(database, client, monkeypatch):
    event_id = _report_event(database, "dcep", "平安银行试点数字人民币钱包",
                             "https://n.example/dcep", at=_ts(5, 10), priority=7.0)
    _mapping(database, event_id, "920670", "数字人", "direct", at=_ts(5, 10, 1))
    _mapping(database, event_id, "000001", "平安银行", "direct", at=_ts(5, 10, 1))
    _mapping(database, event_id, "900001", "平安", "direct", at=_ts(5, 10, 1))

    item = _block(_report(5, now=_ts(6, 1)), "new")["entries"][0]
    assert item["companies"] == ["平安银行"]
    assert item["rule_association_count"] == 2
    associations = re.search(
        r'data-testid="associations">(.*?)</dd>', _page(client, monkeypatch, 5, _ts(6, 1)), re.S
    ).group(1)
    assert "原文点名公司：平安银行 ·" in associations
    assert "数字人" not in associations


def test_reposted_denial_is_background_and_only_new_denials_count(database, client, monkeypatch):
    _report_event(database, "deny-1", "洁雅股份：市场有关公司转型的传闻不实",
                  "https://n.example/deny-1", at=_ts(4, 9), priority=3.0)
    _report_event(database, "deny-2", "洁雅股份：市场有关公司转型的传闻不实",
                  "https://other.example/deny-2", at=_ts(5, 20), priority=3.0,
                  publisher="https://feed-b.example/rss")

    first, repost = _report(4, now=_ts(6, 1)), _report(5, now=_ts(6, 1))
    assert len(_block(first, "correction")["entries"]) == 1
    assert _block(repost, "correction")["entries"] == []
    assert _block(repost, "correction")["empty_label"] == "无实质更新"
    assert repost["repeat_event_count"] == 1

    _report_event(database, "deny-3", "洁雅股份再次澄清：未涉足宠物医疗器械",
                  "https://n.example/deny-3", at=_ts(5, 21), priority=3.0)
    later = _report(5, now=_ts(6, 1))
    assert [item["title"] for item in _block(later, "correction")["entries"]] == [
        "洁雅股份再次澄清：未涉足宠物医疗器械"
    ]
    section = _section(_page(client, monkeypatch, 5, _ts(6, 1)), "correction")
    assert "未涉足宠物医疗器械" in section
    assert "市场有关公司转型的传闻不实" not in section


def test_correction_links_are_shown_first_even_with_many_sources(database, client, monkeypatch):
    event_id = _event(database, "many", "某车企宣布全系降价两成", at=_ts(5, 8), priority=7.0)
    for index in range(6):
        _evidence(database, event_id, f"https://n.example/price-{index}",
                  "某车企宣布全系降价两成", at=_ts(5, 9, index * 5),
                  publisher=f"https://feed-{index}.example/rss")
    _evidence(database, event_id, "https://n.example/price-denial",
              "某车企否认全系降价两成", at=_ts(5, 15), publisher="https://feed-x.example/rss")
    _evidence(database, event_id, "https://n.example/price-0",
              "更正：某车企仅部分车型降价", at=_ts(5, 16))

    item = _block(_report(5, now=_ts(6, 1)), "correction")["entries"][0]
    urls = [source["url"] for source in item["sources"]]
    assert urls[:2] == ["https://n.example/price-0", "https://n.example/price-denial"]
    assert item["sources"][0]["claim_text"] == "更正：某车企仅部分车型降价"
    assert item["sources"][0]["same_link_previous"] == "某车企宣布全系降价两成"
    assert len(item["sources"]) == 5 and item["sources_more"] == 2
    # Remaining slots go to the newest supporting reports.
    assert urls[2:] == [f"https://n.example/price-{i}" for i in (5, 4, 3)]

    section = _section(_page(client, monkeypatch, 5, _ts(6, 1)), "correction")
    assert 'href="https://n.example/price-denial"' in section
    assert "同一链接此前记录为：某车企宣布全系降价两成" in section


def test_clarification_requests_are_not_written_as_denials(database, client, monkeypatch):
    _report_event(database, "ask", "深交所要求某科技公司澄清重组传闻",
                  "https://n.example/ask", at=_ts(5, 10), priority=6.5)
    _report_event(database, "letter", "某药企收到上交所问询函", "https://n.example/letter",
                  at=_ts(5, 11), priority=6.0)
    _report_event(database, "low-letter", "某小公司收到深交所关注函",
                  "https://n.example/low-letter", at=_ts(5, 11, 30), priority=2.0)
    _report_event(database, "reply", "关于某公司向特定对象发行股票申请文件的审核问询函之回复报告",
                  "https://n.example/reply", at=_ts(5, 11, 40), priority=6.5)
    _report_event(database, "answer", "某科技公司发布澄清公告：未与任何方接触重组",
                  "https://n.example/answer", at=_ts(5, 12), priority=5.0)
    # A non-regulatory demand for an explanation is not a clarification clue.
    _report_event(database, "geo", "伊朗要求美国解释为何拒绝7日停火",
                  "https://n.example/geo", at=_ts(5, 12, 30), priority=6.5)

    report = _report(5, now=_ts(6, 1))
    assert "伊朗要求美国解释为何拒绝7日停火" in {
        item["title"] for item in _block(report, "new")["entries"]
    }
    block = _block(report, "correction")
    items = {item["title"]: item for item in block["entries"]}
    # A low-score request follows the score gate; a routine inquiry reply is
    # not a clarification clue at all.
    assert block["total"] == 4 and block["hidden_low_priority"] == 1
    assert set(items) == {
        "深交所要求某科技公司澄清重组传闻", "某药企收到上交所问询函",
        "某科技公司发布澄清公告：未与任何方接触重组",
    }
    for title in ("深交所要求某科技公司澄清重组传闻", "某药企收到上交所问询函"):
        request = items[title]
        assert request["correction_kind"] == "request"
        assert request["change"].startswith("今日出现要求澄清/问询：")
        assert "不是当事方的否认或更正" in request["change"]
        assert "否认/更正：" not in request["change"]
        assert request["next_check"] == "等待当事方的澄清或回复公告"
    answer = items["某科技公司发布澄清公告：未与任何方接触重组"]
    assert answer["correction_kind"] == "denial"
    assert answer["change"].startswith("今日出现否认/更正：")

    html = _page(client, monkeypatch, 5, _ts(6, 1))
    card = re.search(r'<article class="event-card"[^>]*>\s*<h3 class="event-title">'
                     r'深交所要求某科技公司澄清重组传闻</h3>.*?</article>', html, re.S).group(0)
    assert "今日出现否认" not in card and "（要求澄清/问询，非否认）" in card


@pytest.mark.parametrize(("next_path", "expected"), [
    ("/dashboard/events/daily?date=2026-10-05", "/dashboard/events/daily?date=2026-10-05"),
    ("/dashboard/events/daily?date=2026-10-05&block=new&page=2",
     "/dashboard/events/daily?date=2026-10-05&block=new&page=2"),
    ("/dashboard/events/daily?page=2&block=new&date=2026-10-05",
     "/dashboard/events/daily?date=2026-10-05&block=new&page=2"),
    ("/dashboard/events/daily?date=2026-10-05&block=new", "/dashboard"),
    ("/dashboard/events/daily?date=2026-10-05&page=2", "/dashboard"),
    ("/dashboard/events/daily?date=&block=new&page=2", "/dashboard"),
    ("/dashboard/events/daily?date=%20&block=new&page=2", "/dashboard"),
    ("/dashboard/events/daily?date=%202026-10-05&block=new&page=2", "/dashboard"),
    ("/dashboard/events/daily?date=2026-10-05%20&block=new&page=2", "/dashboard"),
    ("/dashboard/events/daily?date=2026-02-30", "/dashboard"),
    ("/dashboard/events/daily?date=2026-10-5", "/dashboard"),
    ("/dashboard/events/daily?date=2026-10-05&tab=x", "/dashboard"),
    ("/dashboard/events/daily?date=2026-10-05#x", "/dashboard"),
    ("/dashboard/events/daily?date=//evil.example", "/dashboard"),
    ("https://evil.example/dashboard/events/daily?date=2026-10-05", "/dashboard"),
    ("/dashboard/events?date=2026-10-05", "/dashboard"),
])
def test_daily_return_path_keeps_only_a_strict_date(next_path, expected):
    from routes.browser_auth import _safe_next

    assert _safe_next(next_path) == expected


def test_login_round_trip_returns_to_the_selected_date(database, monkeypatch):
    import app as app_module
    import routes.browser_auth as browser_auth
    import services.event_daily_report as daily

    sessions: set[str] = set()
    monkeypatch.setenv("AI_FILTER_SESSION_SECRET", "daily-session-secret-for-tests-0123456789")
    monkeypatch.delenv("AI_FILTER_SESSION_SECRET_FILE", raising=False)
    monkeypatch.setattr(browser_auth, "_security_cfg", lambda: {"api_key": "daily-key"})
    monkeypatch.setattr(browser_auth, "_consume_login_attempt", lambda _secret: (True, 1))
    monkeypatch.setattr(browser_auth, "_clear_login_attempts", lambda _secret: None)
    monkeypatch.setattr(browser_auth, "_store_session",
                        lambda _secret, session_id: sessions.add(session_id))
    monkeypatch.setattr(browser_auth, "_session_record_valid",
                        lambda _secret, session_id: session_id in sessions)
    monkeypatch.setattr(daily.time, "time", lambda: _ts(6, 1))
    _report_event(database, "login", "登录回跳检查事件", "https://n.example/login", at=_ts(5, 10))

    base = "https://ai.example.invalid"
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        start = client.get("/dashboard/events/daily?date=2026-10-05", base_url=base,
                           headers={"Accept": "text/html"})
        assert start.status_code == 302
        login_url = start.headers["Location"]
        assert login_url == "/dashboard/login?next=/dashboard/events/daily%3Fdate%3D2026-10-05"
        page = client.get(login_url, base_url=base)
        token = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.get_data(as_text=True))
        next_field = re.search(r'name="next"\s+value="([^"]+)"', page.get_data(as_text=True))
        assert token and next_field
        done = client.post("/dashboard/login", base_url=base, data={
            "csrf_token": token.group(1), "api_key": "daily-key",
            "next": next_field.group(1).replace("&amp;", "&"),
        })
        assert done.status_code == 303
        assert done.headers["Location"] == "/dashboard/events/daily?date=2026-10-05"
        final = client.get(done.headers["Location"], base_url=base)
        bad = client.get("/dashboard/events/daily?date=2026-02-30", base_url=base,
                         headers={"Accept": "text/html", "Cookie": ""})
    assert final.status_code == 200
    body = final.get_data(as_text=True)
    assert "事件日报 · 2026-10-05" in body and "登录回跳检查事件" in body
    assert bad.status_code in {200, 400}


def test_login_round_trip_returns_to_the_selected_block_page(database, monkeypatch):
    import app as app_module
    import routes.browser_auth as browser_auth
    import services.event_daily_report as daily

    sessions: set[str] = set()
    monkeypatch.setenv("AI_FILTER_SESSION_SECRET", "daily-paging-session-secret-0123456789")
    monkeypatch.delenv("AI_FILTER_SESSION_SECRET_FILE", raising=False)
    monkeypatch.setattr(browser_auth, "_security_cfg", lambda: {"api_key": "daily-key"})
    monkeypatch.setattr(browser_auth, "_consume_login_attempt", lambda _secret: (True, 1))
    monkeypatch.setattr(browser_auth, "_clear_login_attempts", lambda _secret: None)
    monkeypatch.setattr(browser_auth, "_store_session",
                        lambda _secret, session_id: sessions.add(session_id))
    monkeypatch.setattr(browser_auth, "_session_record_valid",
                        lambda _secret, session_id: session_id in sessions)
    monkeypatch.setattr(daily.time, "time", lambda: _ts(6, 1))
    for index in range(21):
        title = f"分页登录检查事件{index:02d}"
        _report_event(database, f"login-{index}", title, f"https://n.example/login-{index}",
                      at=_ts(5, 10))
    target = "/dashboard/events/daily?date=2026-10-05&block=new&page=2"
    base = "https://ai.example.invalid"
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        start = client.get(target, base_url=base, headers={"Accept": "text/html"})
        assert start.status_code == 302
        login_url = start.headers["Location"]
        assert login_url == (
            "/dashboard/login?next=/dashboard/events/daily%3Fdate%3D2026-10-05"
            "%26block%3Dnew%26page%3D2"
        )
        page = client.get(login_url, base_url=base)
        token = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.get_data(as_text=True))
        next_field = re.search(r'name="next"\s+value="([^"]+)"', page.get_data(as_text=True))
        done = client.post("/dashboard/login", base_url=base, data={
            "csrf_token": token.group(1), "api_key": "daily-key",
            "next": next_field.group(1).replace("&amp;", "&"),
        })
        assert done.status_code == 303
        assert done.headers["Location"] == target
        final = client.get(done.headers["Location"], base_url=base)
    assert final.status_code == 200
    body = final.get_data(as_text=True)
    assert body.count('class="event-card"') == 1
    assert "分页登录检查事件" in body and "返回当日概览" in body


def test_daily_overflow_has_continue_reading_and_page_two_shows_item_21(
    database, client, monkeypatch,
):
    titles = [
        f"公司披露项目进展{index:02d}，金额{index + 10}亿元" for index in range(22)
    ]
    for index, title in enumerate(titles):
        _report_event(
            database, f"page-{index}", title, f"https://n.example/page-{index}",
            at=_ts(5, 10), priority=7.0,
        )
    html = _page(client, monkeypatch, 5, _ts(6, 1))
    overview_shown = [title for title in titles if title in html]
    assert "查看更多新增 →" in html
    assert "/dashboard/events/daily?date=2026-10-05&amp;block=new&amp;page=2" in html
    assert len(overview_shown) == 20
    page_two = client.get("/dashboard/events/daily?date=2026-10-05&block=new&page=2")
    assert page_two.status_code == 200
    body = page_two.get_data(as_text=True)
    page_shown = [title for title in titles if title in body]
    assert len(page_shown) == 2
    assert not set(overview_shown) & set(page_shown)
    assert set(overview_shown) | set(page_shown) == set(titles)
    assert "展示第 21–22 条" in body


def _card(html, title):
    match = re.search(
        r'<article class="event-card"[^>]*>\s*<h3 class="event-title">'
        + re.escape(title) + r"</h3>.*?</article>", html, re.S,
    )
    assert match, title
    return match.group(0)


def test_publishers_are_counted_by_outlet_not_feed(database, client, monkeypatch):
    def multi(key, title, records, minute):
        event_id = _event(database, key, title, at=_ts(5, 9, minute))
        for offset, (url, feed) in enumerate(records):
            _evidence(database, event_id, url, title, at=_ts(5, 10, minute + offset),
                      publisher=feed)

    multi("same", "国常会研究稳定房地产市场政策", [
        ("https://wallstreetcn.com/livenews/1", "http://rsshub:1200/wallstreetcn/live/a-stock"),
        ("https://wallstreetcn.com/articles/2", "http://rsshub:1200/wallstreetcn/news/global"),
        ("https://wallstreetcn.com/livenews/3", "http://127.0.0.1:1200/wallstreetcn/hot"),
    ], 0)
    multi("two", "某公司科创板上市申请获受理", [
        ("https://static.sse.com.cn/disclosure/a.pdf", "http://rsshub:1200/sse/disclosure"),
        ("https://kcb.sse.com.cn/renewal/b", "http://rsshub:1200/sse/renewal"),
        ("https://www.cls.cn/detail/1", "http://127.0.0.1:1200/cls/depth"),
    ], 10)
    multi("unknown", "某频道称新品下周发布", [
        ("http://rsshub:1200/telegram/channel/abc", "http://rsshub:1200/telegram/channel/abc"),
        ("http://rsshub:1200/telegram/channel/def", "http://rsshub:1200/telegram/channel/def"),
    ], 20)
    multi("mixed", "某省发布算力基础设施方案", [
        ("https://www.cls.cn/detail/2", "http://127.0.0.1:1200/cls/telegraph"),
        ("http://rsshub:1200/telegram/channel/ghi", "http://rsshub:1200/telegram/channel/ghi"),
    ], 30)

    report = _report(5, now=_ts(6, 1))
    items = {
        item["title"]: item
        for block in report["blocks"] for item in block["entries"]
    }
    same = items["国常会研究稳定房地产市场政策"]
    assert same["uncertainties"][0] == "仅 1 个来源发布方，未见独立来源"
    assert same["next_check"] == "等待第二个独立发布方或一手公告"
    assert items["某公司科创板上市申请获受理"]["uncertainties"][0].startswith(
        "2 个来源发布方（按媒体识别，同一媒体的多个订阅源只算 1 个；"
    )
    assert items["某频道称新品下周发布"]["uncertainties"][0] == (
        "来源媒体未能识别，发布方数量未知（2 个链接）；不能据此判断独立来源"
    )
    assert items["某频道称新品下周发布"]["next_check"] == "核实来源媒体或查找一手公告"
    assert items["某省发布算力基础设施方案"]["uncertainties"][0] == (
        "仅 1 个来源发布方，未见独立来源；另有 1 个链接来源媒体未能识别，未计入独立来源"
    )

    html = _page(client, monkeypatch, 5, _ts(6, 1))
    same_card = _card(html, "国常会研究稳定房地产市场政策")
    assert "仅 1 个来源发布方，未见独立来源" in same_card
    assert "3 个来源发布方" not in same_card and same_card.count("华尔街见闻") == 3
    assert "（来源媒体未能识别）" not in same_card
    assert "2 个来源发布方" in _card(html, "某公司科创板上市申请获受理")
    unknown_card = _card(html, "某频道称新品下周发布")
    assert "发布方数量未知（2 个链接）" in unknown_card
    assert "按 1 个来源发布方计" not in unknown_card
    assert unknown_card.count("（来源媒体未能识别）") == 2
    assert "2 个来源发布方" not in unknown_card
    assert _card(html, "某省发布算力基础设施方案").count("（来源媒体未能识别）") == 1


@pytest.mark.parametrize("feed", [
    "https://rsshub.evil.example/wallstreetcn/live",
    "http://rsshub:1200/other/wallstreetcn/live",
    "http://reader@rsshub:1200/wallstreetcn/live",
    "ftp://rsshub:1200/wallstreetcn/live",
    "http://rsshub:1200/wallstreetcn/../cls/telegraph",
    "http://rsshub:1200/wallstreetcn/%2e%2e/cls/telegraph",
    "http://rss\nhub:1200/wallstreetcn/live",
])
def test_daily_publishers_do_not_trust_fake_or_invalid_rss_routes(database, feed):
    from services.event_daily_report import _outlet_key

    item = {"source_uri": "http://rsshub:1200/unknown/item", "source_name": feed}
    assert _outlet_key(item) == ""


def test_daily_publisher_identity_reuses_domains_and_trusted_routes(database, client, monkeypatch):
    event_id = _event(database, "media", "多个媒体报道同一政策", at=_ts(5, 9))
    records = [
        ("https://wallstreetcn.com/articles/1", "http://rsshub:1200/wallstreetcn/news/global"),
        ("https://api.wallstreetcn.com/articles/2", "http://127.0.0.1:1200/wallstreetcn/live"),
        ("http://localhost:1200/wallstreetcn/news/china", "http://rsshub:1200/wallstreetcn/news/china"),
        ("https://www.cls.cn/detail/1", "http://rsshub:1200/cls/telegraph"),
        ("https://www.cls.cn/detail/2", "http://rsshub:1200/wallstreetcn/live"),
    ]
    for offset, (url, feed) in enumerate(records):
        _evidence(database, event_id, url, "多个媒体报道同一政策", at=_ts(5, 10, offset), publisher=feed)
    item = _block(_report(5, now=_ts(6, 1)), "new")["entries"][0]
    assert item["uncertainties"][0].startswith("2 个来源发布方")
    assert "转载关系未核验" in item["uncertainties"][0]
    assert "2 个来源发布方" in _card(_page(client, monkeypatch, 5, _ts(6, 1)), "多个媒体报道同一政策")


def test_comparison_operators_keep_changed_thresholds_apart(database, client, monkeypatch):
    # Cross-day: yesterday "≤", today "≥" is a change, not a repeat.
    _report_event(database, "le", "某公司预告净利润 ≤5亿元", "https://n.example/le",
                  at=_ts(4, 10))
    _report_event(database, "ge", "某公司预告净利润 ≥5亿元", "https://n.example/ge",
                  at=_ts(5, 10))
    # Same day: full-width "＜" and "≥" headlines must not be folded together.
    _report_event(database, "lt", "某指数成交额＜1万亿元", "https://n.example/lt",
                  at=_ts(5, 11))
    _report_event(database, "gte", "某指数成交额≥1万亿元", "https://n.example/gte",
                  at=_ts(5, 11, 5))

    report = _report(5, now=_ts(6, 1))
    assert report["repeat_event_count"] == 0
    assert report["duplicate_groups_folded"] == 0
    shown = {item["title"] for block in report["blocks"] for item in block["entries"]}
    assert {"某公司预告净利润 ≥5亿元", "某指数成交额＜1万亿元", "某指数成交额≥1万亿元"} <= shown

    html = _page(client, monkeypatch, 5, _ts(6, 1))
    new_section = _section(html, "new")
    assert "无实质更新" not in new_section
    assert "某公司预告净利润 ≥5亿元" in new_section
    assert "某指数成交额＜1万亿元" in new_section and "某指数成交额≥1万亿元" in new_section


def test_folded_groups_cite_the_latest_correction(database, client, monkeypatch):
    first = _event(database, "fold-a", "某公司宣布投资计划", at=_ts(5, 9))
    _evidence(database, first, "https://n.example/plan", "某公司宣布投资计划", at=_ts(5, 10))
    _evidence(database, first, "https://n.example/fix-80", "更正：某公司投资额为八十亿",
              at=_ts(5, 18))
    second = _event(database, "fold-b", "某公司宣布投资计划！", at=_ts(5, 10, 30))
    _evidence(database, second, "https://m.example/plan", "某公司宣布投资计划！",
              at=_ts(5, 10, 30))
    _evidence(database, second, "https://m.example/fix-90", "更正：某公司投资额为九十亿",
              at=_ts(5, 11))

    report = _report(5, now=_ts(6, 1))
    assert report["duplicate_groups_folded"] == 1
    item = _block(report, "correction")["entries"][0]
    assert "更正：某公司投资额为八十亿" in item["change"]
    assert "九十亿" not in item["change"]
    assert item["sources"][0]["claim_text"] == "更正：某公司投资额为八十亿"

    card = _section(_page(client, monkeypatch, 5, _ts(6, 1)), "correction")
    assert "今日出现否认/更正：更正：某公司投资额为八十亿" in card


def test_previous_record_is_the_most_recent_prior_record(database, client, monkeypatch):
    event_id = _event(database, "prev", "某公司收购乙公司", at=_ts(3, 9))
    _evidence(database, event_id, "https://n.example/p3", "某公司收购乙公司", at=_ts(3, 10))
    _evidence(database, event_id, "https://n.example/p4", "某公司否认收购乙公司",
              at=_ts(4, 10))
    _evidence(database, event_id, "https://n.example/p5", "某公司收购乙公司细节披露",
              at=_ts(5, 10))

    item = next(
        item for block in _report(5, now=_ts(6, 1))["blocks"] for item in block["entries"]
        if item["event_id"] == event_id
    )
    assert item["previous"]["claim_text"] == "某公司否认收购乙公司"
    assert item["previous"]["time_label"] == "2026-10-04 10:00"
    assert item["earliest"]["claim_text"] == "某公司收购乙公司"

    html = _page(client, monkeypatch, 5, _ts(6, 1))
    assert "上次记录：某公司否认收购乙公司 · 2026-10-04 10:00" in html
    assert "最早记录：某公司收购乙公司 · 2026-10-03 10:00" in html


def test_same_day_original_counts_as_linked_before_a_denial(database, client, monkeypatch):
    event_id = _event(database, "intraday", "某公司获大额订单", at=_ts(5, 8))
    _evidence(database, event_id, "https://n.example/order", "某公司获大额订单", at=_ts(5, 9))
    _evidence(database, event_id, "https://m.example/deny", "某公司否认获大额订单",
              at=_ts(5, 18))

    item = _block(_report(5, now=_ts(6, 1)), "correction")["entries"][0]
    assert item["change"].startswith("此前有 1 条支持记录（最早 2026-10-05 09:00）；今日出现否认/更正：")
    assert "支持与否认/更正并存，旧支持记录不覆盖否认" in item["uncertainties"]
    assert not any("未能自动关联" in note for note in item["uncertainties"])

    card = _section(_page(client, monkeypatch, 5, _ts(6, 1)), "correction")
    assert "原始报道未能自动关联" not in card
    assert "https://n.example/order" in card


@pytest.mark.parametrize(("title", "block", "kind"), [
    ("意大利财政部长：如果欧洲央行行长拉加德能就其职务（去留）问题作出澄清，将是合适的。",
     "new", ""),
    ("分析人士呼吁央行尽快就政策方向作出澄清", "new", ""),
    ("某公司发布澄清公告：未与任何方接触重组", "correction", "denial"),
    ("某公司澄清称未参与该项目", "correction", "denial"),
    ("某公司对市场传闻予以澄清", "correction", "denial"),
])
def test_bare_clarification_word_is_not_a_denial(database, client, monkeypatch, title, block, kind):
    _report_event(database, "clarify", title, "https://n.example/clarify",
                  at=_ts(5, 10), priority=6.5)
    report = _report(5, now=_ts(6, 1))
    item = _block(report, block)["entries"][0]
    assert item["title"] == title and item["correction_kind"] == kind
    html = _page(client, monkeypatch, 5, _ts(6, 1))
    if kind:
        assert "今日出现否认/更正：" in _section(html, "correction")
    else:
        assert _block(report, "correction")["total"] == 0
        assert "今日出现否认/更正" not in html


@pytest.mark.parametrize("existing_root_history", [False, True])
def test_cross_root_repost_keeps_first_and_latest_observations(
    database, client, monkeypatch, existing_root_history,
):
    first = "甲公司投资金额为100亿元"
    middle = "甲公司投资金额为80亿元"
    current = "甲公司投资金额为50亿元"
    original = _report_event(database, "first", first, "https://n.example/first",
                             at=_ts(3, 9))
    _report_event(database, "middle", middle, "https://n.example/middle", at=_ts(3, 12))
    _report_event(database, "latest", first, "https://n.example/latest", at=_ts(4, 18))
    target = original if existing_root_history else _event(
        database, "today", first, at=_ts(5, 9),
    )
    if not existing_root_history:
        _evidence(database, target, "https://n.example/reposted-first", first, at=_ts(5, 8))
    _evidence(database, target, "https://n.example/today", middle, at=_ts(5, 9))
    _evidence(database, target, "https://n.example/today", current, at=_ts(5, 18))

    item = _block(_report(5, now=_ts(6, 1)), "advanced")["entries"][0]
    assert item["previous"] == {
        "claim_text": first, "time_label": "2026-10-04 18:00",
    }
    assert item["earliest"] == {
        "claim_text": first, "time_label": "2026-10-03 09:00",
    }
    assert current in item["change"]
    html = _section(_page(client, monkeypatch, 5, _ts(6, 1)), "advanced")
    assert f"上次记录：{first} · 2026-10-04 18:00" in html
    assert f"最早记录：{first} · 2026-10-03 09:00" in html


def test_lookback_endpoints_do_not_duplicate_own_history(database, client, monkeypatch):
    original = "乙公司获得项目订单"
    event = _report_event(database, "original", original, "https://n.example/original",
                         at=_ts(4, 9))
    _evidence(database, event, "https://n.example/denial", "乙公司否认获得项目订单",
              at=_ts(5, 18))
    item = _block(_report(5, now=_ts(6, 1)), "correction")["entries"][0]
    assert item["change"].startswith("此前有 1 条支持记录")
    assert item["earliest"] is None
    html = _section(_page(client, monkeypatch, 5, _ts(6, 1)), "correction")
    assert "此前有 1 条支持记录" in html
    assert "最早记录：" not in html
