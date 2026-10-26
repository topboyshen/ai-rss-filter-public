"""The real few-shot contract resolver must remain read-only on browser GETs."""

import sqlite3
import time

import pytest

from core.user_feedback import resolve_scoring_contract
from db import database
from tests.test_dashboard_browser_auth import (
    BASE_URL,
    _login,
    dashboard_browser as _dashboard_browser_fixture,
)


dashboard_browser = _dashboard_browser_fixture
PAGE_PATH = "/dashboard/neutral-records"
BASE_CONTRACT = "neutral-readonly:base-contract"


@pytest.fixture
def few_shot_browser(dashboard_browser, monkeypatch):
    import routes.neutral_records_routes as routes

    cfg = {
        "scoring_strategy_version": BASE_CONTRACT,
        "few_shot_feedback": {
            "enabled": True,
            "positive_examples": 2,
            "negative_examples": 2,
            "stage2_only": True,
        },
        "feeds": [],
    }
    monkeypatch.setattr(routes, "_cfg", lambda: cfg)
    client, _auth, _security = dashboard_browser
    assert _login(client, next_path=PAGE_PATH).status_code == 303
    return client, cfg


def _forbid_writers(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("a browser read invoked a database writer or initializer")

    monkeypatch.setattr(database, "get_connection", forbidden)
    monkeypatch.setattr(database, "init_db", forbidden)
    monkeypatch.setattr(database.DB, "register_scoring_contract_snapshot", forbidden)


@pytest.mark.parametrize("example_limit", [0, 2])
def test_few_shot_get_does_not_create_missing_database(
    few_shot_browser, monkeypatch, tmp_path, example_limit,
):
    client, cfg = few_shot_browser
    cfg["few_shot_feedback"]["positive_examples"] = example_limit
    cfg["few_shot_feedback"]["negative_examples"] = example_limit
    missing = tmp_path / "missing-few-shot.db"
    monkeypatch.setattr(database, "DB_PATH", str(missing))
    _forbid_writers(monkeypatch)

    response = client.get(PAGE_PATH, base_url=BASE_URL)

    assert response.status_code == 503
    assert "记录暂不可读取" in response.get_data(as_text=True)
    assert response.headers["Cache-Control"].startswith("no-store")
    assert not missing.exists()
    assert list(tmp_path.glob("missing-few-shot.db*")) == []


def test_few_shot_get_preserves_delete_journal_and_contract(
    few_shot_browser, isolate_ai_filter_db, monkeypatch,
):
    client, cfg = few_shot_browser
    now = int(time.time()) - 30
    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO news_cache (link, title, raw_title, score, tag, logic, "
            "created_at, score_path, strategy_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("https://example.test/feedback", "反馈样例", "反馈样例", 8.0,
             "测试", "冻结样例", now, "llm", BASE_CONTRACT),
        )
        conn.commit()
    database.add_article_feedback("https://example.test/feedback", "good", note="已冻结")

    # Compare with the previous connection behavior before freezing the file.
    with monkeypatch.context() as legacy:
        legacy.setattr(database, "get_read_only_connection", database.get_connection)
        expected = resolve_scoring_contract(cfg, register_snapshot=False)
    assert expected["_few_shot_sample_ids"]
    assert expected["scoring_strategy_version"].startswith(BASE_CONTRACT + ":fb-")

    with sqlite3.connect(isolate_ai_filter_db) as conn:
        conn.execute(
            "INSERT INTO news_cache (link, title, raw_title, created_at, score_path, "
            "strategy_version, safe_neutral_reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("https://example.test/neutral", "只读契约记录", "只读契约记录", now,
             "llm_safe_neutral_grounding", expected["scoring_strategy_version"], "facts"),
        )
        conn.commit()
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    before = isolate_ai_filter_db.read_bytes()
    before_files = sorted(path.name for path in isolate_ai_filter_db.parent.glob("cache.db*"))
    _forbid_writers(monkeypatch)

    response = client.get(PAGE_PATH, base_url=BASE_URL)

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "只读契约记录" in body
    assert "事实表述未通过现有来源校验" in body
    assert expected["scoring_strategy_version"] in body
    assert resolve_scoring_contract(cfg, register_snapshot=False) == expected
    assert response.headers["Cache-Control"].startswith("no-store")
    assert isolate_ai_filter_db.read_bytes() == before
    assert sorted(path.name for path in isolate_ai_filter_db.parent.glob("cache.db*")) == before_files
    with database.get_read_only_connection() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert conn.execute("SELECT COUNT(*) FROM scoring_contract_snapshots").fetchone()[0] == 0
