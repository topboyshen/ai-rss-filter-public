"""Explicit publisher omnibus titles must not consume concrete-news slots."""

import sqlite3
import time
from collections import Counter

import pytest

from sse_engine import (
    _is_realtime_card_noise,
    _load_from_db,
    _select_balanced_cards,
    _source_bucket,
)


@pytest.mark.parametrize("raw_title", [
    "华尔街见闻早餐FM-Radio | 2026年9月29日",
    "华尔街见闻早餐 FM Radio | 2026年9月29日",
    "华尔街见闻早餐 fm-radio | 2026年9月29日",
    "华尔街见闻早餐 fM - RaDiO | 2026年9月29日",
    "华尔街见闻早餐  FM   RADIO  | 2026年9月29日",
    "华尔街见闻早餐\tFM\tRadio | 2026年9月29日",
])
def test_explicit_original_breakfast_audio_digest_is_noise(raw_title):
    assert _is_realtime_card_noise({
        "raw_title": raw_title,
        "title": "全球市场与科技行业消息",
        "logic": "主要市场变化",
    })


@pytest.mark.parametrize("field", ["title", "logic"])
def test_generated_fields_do_not_turn_specific_original_into_breakfast_digest(field):
    entry = {
        "raw_title": "某半导体公司发布新一代存储芯片量产进展",
        "title": "存储芯片量产进展",
        "logic": "新增量产进展",
        field: "华尔街见闻早餐FM-Radio | 2026年9月29日",
    }
    assert not _is_realtime_card_noise(entry)


@pytest.mark.parametrize("raw_title", [None, "", " "])
def test_absent_original_title_is_not_evidence_for_new_digest_rule(raw_title):
    assert not _is_realtime_card_noise({
        "raw_title": raw_title,
        "title": "华尔街见闻早餐FM-Radio | 2026年9月29日",
        "logic": "主要市场变化",
    })


@pytest.mark.parametrize("title", [
    "早餐供应企业获得新订单",
    "早餐产业供应链投资项目落地",
    "消费政策新增早餐门店补贴",
    "某食品公司新增早餐生产线",
    "FM Radio设备制造企业发布量产进展",
])
def test_breakfast_business_and_specific_consumption_news_are_not_omnibus(title):
    assert not _is_realtime_card_noise({
        "raw_title": title, "title": title, "logic": "具体项目进展",
    })


@pytest.mark.parametrize("title", ["财经早报：主要市场消息", "债市晨报：市场消息"])
def test_existing_morning_report_filter_is_retained(title):
    assert _is_realtime_card_noise({"raw_title": title, "title": title, "logic": ""})


def test_existing_long_multisection_logic_filter_is_retained():
    assert _is_realtime_card_noise({
        "raw_title": "具体原标题", "title": "具体展示标题",
        "logic": "【央行公布新一轮货币政策利率安排】" + "甲" * 300
                 + "【半导体企业发布新的工厂建设计划】" + "乙" * 300
                 + "【创新药企业披露新的临床试验结果】" + "丙" * 300,
    })


def test_real_cache_digest_removal_fills_second_publisher_slot_without_relaxing_caps(
    tmp_path, monkeypatch,
):
    from db import database

    path = tmp_path / "news.sqlite"
    now = int(time.time())
    wallstreet = "http://rsshub:1200/wallstreetcn/news/global"
    peers = ("https://feeds.example/policy", "https://feeds.example/medicine")
    rows = [
        ("全球主要市场消息", "华尔街见闻早餐FM-Radio | 2026年9月29日",
         "https://news.example/digest", 9.9, wallstreet),
        ("芯片工厂量产进展", "某半导体公司投产新晶圆制造工厂",
         "https://news.example/semiconductor", 9.7, wallstreet),
        ("自动化项目订单", "某设备企业签订工业自动化项目订单",
         "https://news.example/automation", 9.5, wallstreet),
        ("食品供应新增产能", "某食品企业新增饮料生产线",
         "https://news.example/food", 9.3, wallstreet),
        ("央行公布利率决定", "央行公布利率决定",
         "https://news.example/policy", 9.2, peers[0]),
        ("创新药取得临床进展", "某药企发布创新药临床试验结果",
         "https://news.example/medicine", 9.1, peers[1]),
    ]
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE news_cache (title TEXT, raw_title TEXT, link TEXT, "
                     "score REAL, tag TEXT, logic TEXT, created_at INTEGER, feed_url TEXT)")
        conn.executemany("INSERT INTO news_cache VALUES (?, ?, ?, ?, '行业', '具体事件', ?, ?)",
                         [(title, raw_title, link, score, now - index, feed)
                          for index, (title, raw_title, link, score, feed) in enumerate(rows)])
    monkeypatch.setattr(database, "DB_PATH", str(path))
    cfg = {"threshold": {"pass": 7}, "feeds": [
        {"name": "华尔街见闻全球", "url": wallstreet},
        {"name": "政策发布方", "url": peers[0]},
        {"name": "医药发布方", "url": peers[1]},
    ]}

    candidates = _load_from_db(cfg=cfg, strict=True)
    assert "https://news.example/digest" not in {entry["link"] for entry in candidates}
    selected, fresh, fallback = _select_balanced_cards(
        candidates, limit=4, max_per_source=2, now_ts=now,
    )
    selected_links = {entry["link"] for entry in selected}
    assert selected_links == {
        "https://news.example/semiconductor", "https://news.example/automation",
        "https://news.example/policy", "https://news.example/medicine",
    }
    assert len(selected) == 4 and (fresh, fallback) == (4, 0)
    source_counts = Counter(_source_bucket(entry) for entry in selected)
    assert source_counts["华尔街见闻"] == 2
    assert max(source_counts.values()) == 2
    assert "https://news.example/food" not in selected_links
