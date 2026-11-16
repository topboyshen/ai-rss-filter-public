"""关键路径上的宽泛捕获必须留下证据，且只吞掉预期内的对象形状错误。

背景：A5 审计发现多处 ``except Exception`` 静默回退，使数据库故障与「本来就没
有数据」在日志里无法区分；``core/processor.py`` 的属性赋值兜底还会把编程错误
一起吞掉。本文件锁住收口后的契约：

* 回退路径的返回值与控制流保持不变；
* 回退发生时至少留下一条 warning（展示类兜底留下 debug）；
* processor 的属性/下标赋值只容忍 ``AttributeError``/``TypeError``/``KeyError``，
  其余异常必须暴露出来。
"""

import logging

import pytest
from flask import Flask

from core import processor
from routes import feed_routes
from routes import shared
from services import intelligence_query


def _raise(message):
    def boom(*args, **kwargs):
        raise RuntimeError(message)

    return boom


# ---------- routes/feed_routes.py: /cached_feed 主路径 ----------


def test_cached_feed_primary_query_failure_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(feed_routes, "validate_target_url", lambda url, cfg: (True, ""))
    monkeypatch.setattr(feed_routes, "_retired_rsshub_source_response", lambda urls: None)
    monkeypatch.setattr(feed_routes, "_cached_feed_source_name", lambda cfg, url: "feed")
    monkeypatch.setattr(
        feed_routes.DB,
        "get_entries_for_cached_rss",
        staticmethod(_raise("database is locked")),
    )
    monkeypatch.setattr(
        feed_routes.DB,
        "get_recent_entries_for_feed_preview",
        staticmethod(lambda *args, **kwargs: []),
    )

    app = Flask(__name__)
    with app.test_request_context("/cached_feed?url=http://example.invalid/feed"):
        with caplog.at_level(logging.WARNING, logger="routes.feed_routes"):
            response = feed_routes.cached_feed()

    # 控制流不变：仍然返回 200 的空 RSS，而不是 500。
    assert response.status_code == 200
    assert "暂无高分情报" in response.get_data(as_text=True)
    assert "[cached_feed]" in caplog.text
    assert "database is locked" in caplog.text


# ---------- services/intelligence_query.py: URL 规范化回退 ----------


def test_equivalent_feed_urls_falls_back_and_warns(monkeypatch, caplog):
    monkeypatch.setattr(
        intelligence_query.DB,
        "equivalent_feed_urls",
        staticmethod(_raise("no such table: feeds")),
    )

    with caplog.at_level(logging.WARNING, logger="services.intelligence_query"):
        result = intelligence_query.equivalent_feed_urls({}, "http://example.invalid/a")

    assert result == ["http://example.invalid/a"]
    assert "equivalent_feed_urls" in caplog.text
    assert "no such table" in caplog.text


def test_canonical_feed_url_falls_back_and_warns(monkeypatch, caplog):
    monkeypatch.setattr(
        intelligence_query.DB,
        "canonical_feed_url",
        staticmethod(_raise("database is locked")),
    )

    with caplog.at_level(logging.WARNING, logger="services.intelligence_query"):
        result = intelligence_query.canonical_feed_url({}, "http://example.invalid/a")

    assert result == "http://example.invalid/a"
    assert "canonical_feed_url" in caplog.text
    assert "database is locked" in caplog.text


# ---------- routes/shared.py: 共享刷新状态 ----------


def test_refresh_status_read_failure_is_logged(monkeypatch, caplog):
    import services.shared_cache as shared_cache

    monkeypatch.setattr(shared, "_refresh_running", False)
    monkeypatch.setattr(
        shared_cache, "get_shared_json", _raise("redis unavailable")
    )

    with caplog.at_level(logging.WARNING, logger="routes.shared"):
        assert shared.is_refresh_job_running() is False

    assert "refresh" in caplog.text.lower()
    assert "redis unavailable" in caplog.text


# ---------- core/processor.py: 属性赋值兜底 ----------


class _SlottedEntry:
    """只接受声明过属性的对象；写 scoring_meta 会抛 AttributeError。"""

    __slots__ = ()


def test_set_scoring_meta_tolerates_entry_shape_errors(caplog):
    with caplog.at_level(logging.DEBUG, logger="core.processor"):
        processor._set_scoring_meta(_SlottedEntry(), score=8.0)

    assert "scoring_meta" in caplog.text


def test_set_scoring_meta_does_not_swallow_programming_errors():
    class _ExplodingEntry:
        @property
        def scoring_meta(self):
            return {}

        @scoring_meta.setter
        def scoring_meta(self, value):
            raise RuntimeError("descriptor bug")

    with pytest.raises(RuntimeError, match="descriptor bug"):
        processor._set_scoring_meta(_ExplodingEntry(), score=8.0)


def test_assign_event_identity_uses_mapping_for_plain_dict():
    entry = {}
    processor._assign_event_identity(entry, "5f1a7e0e-6a1f-4c2b-9a3d-2f4b6c8d0e1f", 3)

    assert entry["event_id"] == "5f1a7e0e-6a1f-4c2b-9a3d-2f4b6c8d0e1f"
    assert entry["event_version"] == 3


def test_assign_event_identity_uses_scoring_meta_for_slotted_entry(caplog):
    from utils.helpers import FeedEntry

    entry = FeedEntry("标题", "http://example.invalid/a")

    with caplog.at_level(logging.DEBUG, logger="core.processor"):
        processor._assign_event_identity(
            entry, "5f1a7e0e-6a1f-4c2b-9a3d-2f4b6c8d0e1f", 2
        )

    assert entry.scoring_meta["event_id"] == "5f1a7e0e-6a1f-4c2b-9a3d-2f4b6c8d0e1f"
    assert entry.scoring_meta["event_version"] == 2
    # 身份存进 scoring_meta 属于正常路径，不应产生「未挂上」的噪音。
    assert "was not attached" not in caplog.text


def test_assign_event_identity_logs_when_entry_is_fully_immutable(caplog):
    from types import MappingProxyType

    with caplog.at_level(logging.DEBUG, logger="core.processor"):
        processor._assign_event_identity(
            MappingProxyType({}), "5f1a7e0e-6a1f-4c2b-9a3d-2f4b6c8d0e1f", 1
        )

    assert "was not attached" in caplog.text


def test_assign_event_identity_does_not_swallow_programming_errors():
    class _ExplodingMeta:
        @property
        def scoring_meta(self):
            return {}

        @scoring_meta.setter
        def scoring_meta(self, value):
            raise RuntimeError("descriptor bug")

    with pytest.raises(RuntimeError, match="descriptor bug"):
        processor._assign_event_identity(
            _ExplodingMeta(), "5f1a7e0e-6a1f-4c2b-9a3d-2f4b6c8d0e1f", 1
        )


def test_model_name_tolerates_unreadable_backends():
    class _OddScorer:
        backends = [object()]  # 没有 .model / .name

    assert processor._model_name(_OddScorer()) == ""


def test_model_name_does_not_swallow_programming_errors():
    class _ExplodingScorer:
        @property
        def backends(self):
            raise RuntimeError("backend registry bug")

    with pytest.raises(RuntimeError, match="backend registry bug"):
        processor._model_name(_ExplodingScorer())
