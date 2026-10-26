"""Weekly report under concurrent HTTP reads, with a synthetic CPU-bound build.

The report is pure Python under the GIL.  Tests make that visible without a
large database: ``_classify_week`` burns a fixed amount of *thread CPU time*
per build, so N parallel builds stretch each other's wall clock exactly as the
production report does, and the 5 second cap is scaled down to match.
"""

import threading
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

BASE = "https://ai.example.invalid"
KEY = "weekly-cache-test-key"
TZ = ZoneInfo("Asia/Shanghai")
MONDAY = int(datetime(2026, 10, 12, 1, tzinfo=TZ).timestamp())  # Monday after the week
NOW = [MONDAY]
PAST = "/dashboard/events/weekly?week=2026-10-05"
OLDER = "/dashboard/events/weekly?week=2026-09-28"


def _at(day, hour=10, month=10):
    return int(datetime(2026, month, day, hour, tzinfo=TZ).timestamp())


@pytest.fixture
def env(monkeypatch):
    import app as app_module
    import db.database as database
    import routes.browser_auth as auth
    import routes.event_radar_routes as routes
    import routes.security as security
    import routes.shared as shared
    import services.event_weekly_report as weekly

    database.init_db()
    settings = {"api_key": KEY, "allow_query_api_key": True}
    monkeypatch.setenv("AI_FILTER_SESSION_SECRET", "weekly-cache-session-secret-0123456789")
    monkeypatch.delenv("AI_FILTER_SESSION_SECRET_FILE", raising=False)
    monkeypatch.setattr(auth, "_security_cfg", lambda: settings)
    monkeypatch.setattr(shared, "_security_cfg", lambda: settings)
    monkeypatch.setattr(routes, "_cfg", lambda: {"feeds": []})
    # The wall clock the report sees; the build deadline keeps the real monotonic clock.
    monkeypatch.setattr(weekly, "time", SimpleNamespace(
        time=lambda: NOW[0], monotonic=time.monotonic,
    ))
    NOW[0] = MONDAY
    weekly.clear_weekly_cache()
    with security._rate_limit_lock:
        security._rate_limit_store.clear()
    app_module.app.config["TESTING"] = True
    builds = []
    original = weekly._compute_weekly_report

    def counted(*args, **kwargs):
        builds.append(args[1:3])
        return original(*args, **kwargs)

    monkeypatch.setattr(weekly, "_compute_weekly_report", counted)
    yield SimpleNamespace(
        app=app_module.app, database=database, weekly=weekly, builds=builds,
        security=security,
    )
    weekly.clear_weekly_cache()


def _article(env, title, at, key=None):
    from db.event_core import resolve_or_create_event, upsert_event_evidence

    with env.database.get_connection() as conn:
        event_id = resolve_or_create_event(
            conn, {"weekly-cache": key or title}, title=title, event_type="policy",
            direction="中性", priority=8.0, occurred_at=at, now=at,
        )
        upsert_event_evidence(
            conn, event_id, source_uri=f"https://news.example/{event_id}",
            source_name="https://feed.example/rss", claim_key=event_id,
            claim_text=title, stance="supports", announcement_at=at - 60,
            observed_at=at, now=at,
        )
    return event_id


def _get(env, url, *, key=KEY):
    headers = {"X-API-Key": key} if key else {}
    with env.app.test_client() as client:
        return client.get(url, base_url=BASE, headers=headers)


def _burn_cpu_per_build(env, monkeypatch, seconds, cap):
    original = env.weekly._classify_week

    def slow(*args, **kwargs):
        start = time.thread_time()
        while time.thread_time() - start < seconds:
            pass
        return original(*args, **kwargs)

    monkeypatch.setattr(env.weekly, "_classify_week", slow)
    monkeypatch.setattr(env.weekly, "MAX_REPORT_SECONDS", cap)


def _concurrent(env, urls):
    results = [None] * len(urls)
    barrier = threading.Barrier(len(urls))

    def read(index):
        barrier.wait()
        with env.security._rate_limit_lock:
            env.security._rate_limit_store.clear()
        results[index] = _get(env, urls[index])

    threads = [threading.Thread(target=read, args=(i,)) for i in range(len(urls))]
    [thread.start() for thread in threads]
    [thread.join(timeout=60) for thread in threads]
    return results


def test_three_concurrent_identical_requests_do_not_all_time_out(env, monkeypatch):
    # Synthetic: one build costs 0.9s of CPU and the cap is 2.4s, so three
    # parallel builds (2.7s of GIL-shared CPU) overrun it while one does not.
    _article(env, "并发周报事件", _at(6))
    _burn_cpu_per_build(env, monkeypatch, 0.9, 2.4)
    responses = _concurrent(env, [PAST] * 3)
    assert [r.status_code for r in responses] == [200, 200, 200]
    assert len(env.builds) == 1
    pages = {r.get_data(as_text=True) for r in responses}
    assert len(pages) == 1 and "并发周报事件" in pages.pop()


def test_distinct_concurrent_requests_queue_instead_of_timing_each_other_out(env, monkeypatch):
    _article(env, "本周事件", _at(6))
    _article(env, "上周事件", _at(1))
    _burn_cpu_per_build(env, monkeypatch, 0.9, 2.4)
    responses = _concurrent(env, [PAST, OLDER, PAST + "&block=new&page=1"])
    assert [r.status_code for r in responses] == [200, 200, 200]
    assert len(env.builds) == 3  # different reports are never merged into one


def test_cache_keys_never_mix_weeks_blocks_or_pages(env):
    _article(env, "本周独有事件", _at(6))
    _article(env, "上周独有事件", _at(1))
    first = _get(env, PAST).get_data(as_text=True)
    older = _get(env, OLDER).get_data(as_text=True)
    assert "本周独有事件" in first and "上周独有事件" not in first
    assert "上周独有事件" in older and "本周独有事件" not in older
    again = _get(env, PAST).get_data(as_text=True)
    assert again == first
    paged = _get(env, PAST + "&block=new&page=1")
    assert paged.status_code == 200 and paged.get_data(as_text=True) != first
    assert len(env.builds) == 3  # overview twice and older week:
    assert len(env.weekly._REPORT_CACHE) == 3


def test_cached_page_keeps_its_original_generated_at_and_never_looks_fresher(env):
    _article(env, "新鲜度事件", _at(6))
    first = _get(env, PAST).get_data(as_text=True)
    NOW[0] += 100  # still the same Monday, inside the 120s past-week TTL
    second = _get(env, PAST).get_data(as_text=True)
    assert second == first and len(env.builds) == 1
    assert "生成于 2026-10-12 01:00:00（北京时间）" in second
    assert "01:01:40" not in second


def test_past_week_expires_by_ttl_and_regenerates_with_a_new_generated_at(env, monkeypatch):
    _article(env, "过期事件", _at(6))
    clock = [0.0]
    monkeypatch.setattr(env.weekly._REPORT_CACHE, "_clock", lambda: clock[0])
    _get(env, PAST)
    clock[0] += env.weekly.PAST_WEEK_CACHE_SECONDS - 1
    _get(env, PAST)
    assert len(env.builds) == 1
    clock[0] += 2
    NOW[0] += 125
    page = _get(env, PAST).get_data(as_text=True)
    assert len(env.builds) == 2 and "生成于 2026-10-12 01:02:05（北京时间）" in page


def test_current_week_is_invalidated_by_new_evidence_not_only_by_ttl(env):
    NOW[0] = _at(7, 9)
    _article(env, "周三早报", _at(7, 8))
    url = "/dashboard/events/weekly?week=2026-10-05"
    first = _get(env, url).get_data(as_text=True)
    assert "周三早报" in first and "当前晚报" not in first
    _get(env, url)
    assert len(env.builds) == 1
    NOW[0] += 600
    _article(env, "当前晚报", NOW[0] - 30)
    second = _get(env, url).get_data(as_text=True)
    assert len(env.builds) == 2 and "当前晚报" in second and "本周未结束" in second


def test_past_week_ignores_later_evidence_but_follows_new_mapping_rows(env):
    event_id = _article(env, "已结束周事件", _at(6))
    _get(env, PAST)
    NOW[0] += 5
    _article(env, "之后才入库的事件", NOW[0] - 1)  # observed after the week's cutoff
    _get(env, PAST)
    assert len(env.builds) == 1  # a completed week cannot change because of it

    from db.event_core import upsert_event_security, upsert_security

    with env.database.get_connection() as conn:
        security_id = upsert_security(conn, exchange="SZSE", ticker="000001", name="样例股份", now=_at(6))
        upsert_event_security(
            conn, event_id, security_id, relation_type="direct", confidence=0.8,
            relevance=8.0, mapping_version="mapping-test-v1", now=_at(6),
        )
    _get(env, PAST)
    assert len(env.builds) == 2  # a new mapping row is a new data marker


def test_authentication_is_enforced_before_any_cache_or_database_read(env, monkeypatch):
    _article(env, "受保护事件", _at(6))
    assert _get(env, PAST).status_code == 200  # warm the cache for this key
    reads = []
    monkeypatch.setattr(env.weekly, "_data_marker", lambda *_: reads.append(1) or (0,))
    for headers_key in (None, "wrong-key"):
        response = _get(env, PAST, key=headers_key)
        assert response.status_code in (302, 401)
        assert "受保护事件" not in response.get_data(as_text=True)
    assert reads == [] and len(env.builds) == 1


def test_failure_is_503_with_retry_after_and_is_not_remembered(env, monkeypatch):
    from services.event_radar_query import EventRadarUnavailableError

    _article(env, "失败后恢复", _at(6))
    original = env.weekly._compute_weekly_report
    outcomes = [EventRadarUnavailableError("busy")]

    def flaky(*args, **kwargs):
        if outcomes:
            raise outcomes.pop()
        return original(*args, **kwargs)

    monkeypatch.setattr(env.weekly, "_compute_weekly_report", flaky)
    failed = _get(env, PAST)
    assert failed.status_code == 503 and failed.headers["Retry-After"] == "5"
    assert len(env.weekly._REPORT_CACHE) == 0
    recovered = _get(env, PAST)
    assert recovered.status_code == 200 and "失败后恢复" in recovered.get_data(as_text=True)


def test_invalid_requests_create_no_entries_and_valid_ones_stay_bounded(env):
    _article(env, "有界事件", _at(6))
    for bad in ("?week=2026-10-06", "?week=2026-10-05&block=nope&page=1", "?week=2026-10-05&page=1"):
        assert _get(env, "/dashboard/events/weekly" + bad).status_code == 400
    assert len(env.weekly._REPORT_CACHE) == 0
    for page in range(1, 60):
        _get(env, PAST + f"&block=new&page={page}")
    assert len(env.weekly._REPORT_CACHE) <= 24


def test_fixed_clock_callers_are_never_cached(env):
    _article(env, "固定时钟事件", _at(6))
    for _ in range(2):
        report = env.weekly.load_weekly_report("2026-10-05", now=NOW[0])
        assert report["generated_at"] == NOW[0]
    assert len(env.builds) == 2 and len(env.weekly._REPORT_CACHE) == 0
