import hashlib
import json
import sqlite3
import time

import pytest

from core.scorer import LLMBackend, Scorer
from core.processor import process_entry
from core.user_feedback import build_few_shot_profile, resolve_scoring_contract
from db import database


def _insert_article(link: str, raw_title: str, score: float) -> None:
    now = int(time.time())
    with database.get_connection() as conn:
        event_id, event_version = database._resolve_article_event(
            conn,
            link=link,
            title=raw_title,
            event_type="test-news",
            direction="neutral",
            priority=score,
            occurred_at=now,
            strategy_version="base-v1",
            observed_at=now,
        )
        conn.execute(
            "INSERT INTO news_cache "
            "(link, title, raw_title, score, tag, logic, created_at, summary, "
            "feed_url, strategy_version, score_path, event_id, event_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                link,
                raw_title,
                raw_title,
                score,
                "测试标签",
                "测试逻辑",
                now,
                "",
                "https://feed.test/rss",
                "base-v1",
                "llm",
                event_id,
                event_version,
            ),
        )
        conn.commit()


def _cfg() -> dict:
    return {
        "scoring_strategy_version": "manual-v1:static-fingerprint",
        "few_shot_feedback": {
            "enabled": True,
            "positive_examples": 2,
            "negative_examples": 2,
            "stage2_only": True,
        },
    }


def test_feedback_profile_changes_when_selected_samples_change():
    database.init_db()
    _insert_article("https://news.test/1", "样例一", 8.0)
    database.add_article_feedback(
        "https://news.test/1", "good", target_score=9.0, note="有效"
    )
    first = build_few_shot_profile(_cfg())

    _insert_article("https://news.test/2", "样例二", 3.0)
    database.add_article_feedback(
        "https://news.test/2", "bad", target_score=2.0, note="噪音"
    )
    second = build_few_shot_profile(_cfg())

    assert first["profile_hash"] != second["profile_hash"]
    assert first["sample_ids"] != second["sample_ids"]
    assert "样例一" in second["context"]
    assert "样例二" in second["context"]
    assert "不可信数据" in second["context"]


def test_feedback_profile_does_not_drift_when_live_cache_is_rescored():
    database.init_db()
    _insert_article("https://news.test/1", "稳定样例", 8.0)
    database.add_article_feedback("https://news.test/1", "good", note="冻结")
    first = build_few_shot_profile(_cfg())

    with database.get_connection() as conn:
        conn.execute(
            "UPDATE news_cache SET score = ?, tag = ?, logic = ?, "
            "strategy_version = ? WHERE link = ?",
            (2.0, "后来标签", "后来逻辑", "later-v2", "https://news.test/1"),
        )
        conn.commit()
    second = build_few_shot_profile(_cfg())

    assert second["profile_hash"] == first["profile_hash"]
    assert second["snapshot"] == first["snapshot"]
    assert second["snapshot"][0]["score"] == 8.0
    assert second["snapshot"][0]["snapshot_origin"] == "feedback_write"
    quality = database.get_quality_metrics(hours=24, cfg={})
    assert quality["precision_at_7"] == 100.0
    assert quality["precision_n_at_7"] == 1
    with database.get_connection() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE feedback_example_snapshots SET score = 10 "
                "WHERE feedback_id = ?",
                (second["sample_ids"][0],),
            )


def test_legacy_feedback_is_materialized_at_upgrade_without_backdating():
    database.init_db()
    _insert_article("https://news.test/legacy", "旧反馈样例", 7.5)
    created_at = int(time.time()) - 86400
    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO article_feedback "
            "(link, label, target_score, note, created_at) VALUES (?, ?, ?, ?, ?)",
            ("https://news.test/legacy", "good", 8.0, "旧数据", created_at),
        )
        conn.commit()

    database.init_db()
    database.init_db()
    profile = build_few_shot_profile(_cfg())
    sample = profile["snapshot"][0]
    assert sample["feedback_created_at"] == created_at
    assert sample["captured_at"] >= created_at + 86400
    assert sample["snapshot_origin"] == "posthoc_materialized"
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM feedback_example_snapshots WHERE link = ?",
            ("https://news.test/legacy",),
        ).fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_latest_feedback_per_link_is_the_only_active_example():
    database.init_db()
    _insert_article("https://news.test/relabel", "重新标注", 8.0)
    database.add_article_feedback("https://news.test/relabel", "good")
    database.add_article_feedback("https://news.test/relabel", "bad")
    profile = build_few_shot_profile(_cfg())

    rows = [row for row in profile["snapshot"] if row["link"].endswith("relabel")]
    assert len(rows) == 1
    assert rows[0]["bucket"] == "negative"
    assert rows[0]["label"] == "bad"

    database.add_article_feedback("https://news.test/relabel", "duplicate")
    profile = build_few_shot_profile(_cfg())
    assert not [
        row for row in profile["snapshot"] if row["link"].endswith("relabel")
    ]


def test_latest_feedback_without_snapshot_does_not_fall_back_to_old_label():
    database.init_db()
    link = "https://news.test/missing-latest-snapshot"
    _insert_article(link, "缓存即将清理", 8.0)
    database.add_article_feedback(link, "good")
    with database.get_connection() as conn:
        conn.execute("DELETE FROM news_cache WHERE link = ?", (link,))
        conn.commit()

    database.add_article_feedback(link, "bad")
    profile = build_few_shot_profile(_cfg())
    assert not [row for row in profile["snapshot"] if row["link"] == link]
    quality = database.get_quality_metrics(hours=24, cfg={})
    assert quality["precision_at_7"] is None
    assert quality["precision_n_at_7"] == 0
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM article_feedback WHERE link = ?", (link,)
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM feedback_example_snapshots WHERE link = ?",
            (link,),
        ).fetchone()[0] == 1


def test_nonempty_legacy_contract_schema_fails_closed(monkeypatch, tmp_path):
    legacy_db = tmp_path / "legacy-contract.db"
    monkeypatch.setattr(database, "DB_PATH", str(legacy_db))
    with database.get_connection() as conn:
        conn.execute(
            "CREATE TABLE scoring_contract_snapshots ("
            "strategy_version TEXT PRIMARY KEY, "
            "base_strategy_version TEXT NOT NULL, "
            "feedback_profile_hash TEXT NOT NULL, "
            "feedback_sample_ids_json TEXT NOT NULL, "
            "feedback_snapshot_json TEXT NOT NULL, "
            "feedback_context_hash TEXT NOT NULL, "
            "created_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO scoring_contract_snapshots VALUES "
            "('old:fb-1234', 'old', ?, '[]', '[]', ?, 1)",
            ("1" * 64, "2" * 64),
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="cannot safely upgrade"):
        database.init_db()


def test_empty_legacy_contract_schema_migrates_to_v2(monkeypatch, tmp_path):
    legacy_db = tmp_path / "empty-legacy-contract.db"
    monkeypatch.setattr(database, "DB_PATH", str(legacy_db))
    with database.get_connection() as conn:
        conn.execute(
            "CREATE TABLE scoring_contract_snapshots ("
            "strategy_version TEXT PRIMARY KEY, "
            "base_strategy_version TEXT NOT NULL, "
            "feedback_profile_hash TEXT NOT NULL, "
            "feedback_sample_ids_json TEXT NOT NULL, "
            "feedback_snapshot_json TEXT NOT NULL, "
            "feedback_context_hash TEXT NOT NULL, "
            "created_at INTEGER NOT NULL)"
        )
        conn.commit()

    database.init_db()
    with database.get_connection() as conn:
        columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(scoring_contract_snapshots)"
            ).fetchall()
        }
    assert {"feedback_profile_json", "feedback_context"} <= columns
    assert database.get_app_meta("scoring_contract_schema_version") == "2"


def test_resolved_contract_is_versioned_registered_and_immutable():
    database.init_db()
    _insert_article("https://news.test/1", "冻结样例", 8.5)
    database.add_article_feedback(
        "https://news.test/1", "good", target_score=9.0, note="保留"
    )

    resolved = resolve_scoring_contract(_cfg(), register_snapshot=True)
    suffix = resolved["_few_shot_profile_hash"][:32]
    assert resolved["scoring_strategy_version"] == (
        f"manual-v1:static-fingerprint:fb-{suffix}"
    )
    assert resolve_scoring_contract(resolved) is resolved

    frozen = database.get_scoring_contract_snapshot(
        resolved["scoring_strategy_version"]
    )
    assert frozen is not None
    assert frozen["base_strategy_version"] == "manual-v1:static-fingerprint"
    assert frozen["feedback_profile_hash"] == resolved["_few_shot_profile_hash"]
    assert frozen["feedback_sample_ids"] == resolved["_few_shot_sample_ids"]
    assert frozen["feedback_snapshot"][0]["raw_title"] == "冻结样例"
    assert frozen["feedback_context"] == resolved["_few_shot_context"]
    assert frozen["feedback_profile_payload"]["samples"] == frozen["feedback_snapshot"]
    assert frozen["feedback_profile_payload"]["context"] == frozen["feedback_context"]

    with database.get_connection() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE scoring_contract_snapshots SET feedback_context_hash = ? "
                "WHERE strategy_version = ?",
                ("0" * 64, resolved["scoring_strategy_version"]),
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "DELETE FROM scoring_contract_snapshots WHERE strategy_version = ?",
                (resolved["scoring_strategy_version"],),
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "INSERT OR REPLACE INTO scoring_contract_snapshots "
                "SELECT strategy_version, base_strategy_version, ?, "
                "feedback_profile_json, feedback_sample_ids_json, "
                "feedback_snapshot_json, feedback_context, "
                "feedback_context_hash, created_at "
                "FROM scoring_contract_snapshots WHERE strategy_version = ?",
                ("0" * 64, resolved["scoring_strategy_version"]),
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "INSERT OR REPLACE INTO feedback_example_snapshots "
                "SELECT feedback_id, link, label, target_score, note, "
                "feedback_created_at, '篡改', score, tag, logic, "
                "source_strategy_version, captured_at, snapshot_origin "
                "FROM feedback_example_snapshots WHERE feedback_id = ?",
                (resolved["_few_shot_sample_ids"][0],),
            )


def test_feedback_profile_change_isolates_similarity_candidates_by_full_contract():
    database.init_db()
    _insert_article("https://news.test/profile-1", "正面样例", 8.5)
    database.add_article_feedback("https://news.test/profile-1", "good")
    first = resolve_scoring_contract(_cfg(), register_snapshot=True)

    now = int(time.time())
    with database.get_connection() as conn:
        candidate_link = "https://news.test/similarity-candidate"
        event_id, event_version = database._resolve_article_event(
            conn,
            link=candidate_link,
            title="Legacy similarity candidate",
            event_type="test-news",
            direction="neutral",
            priority=9.0,
            occurred_at=now,
            strategy_version=first["scoring_strategy_version"],
            observed_at=now,
        )
        conn.execute(
            "INSERT INTO news_cache "
            "(link, title, raw_title, score, tag, logic, created_at, summary, "
            "feed_url, strategy_version, score_path, event_id, event_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                candidate_link,
                "旧画像候选",
                "旧画像候选",
                9.0,
                "测试",
                "旧画像得分",
                now,
                "",
                "https://feed.test/rss",
                first["scoring_strategy_version"],
                "llm",
                event_id,
                event_version,
            ),
        )
        conn.commit()
    assert [row[0] for row in database.get_recent_entries(hours=72, cfg=first)] == [
        "https://news.test/similarity-candidate"
    ]

    _insert_article("https://news.test/profile-2", "负面样例", 2.0)
    database.add_article_feedback("https://news.test/profile-2", "bad")
    second = resolve_scoring_contract(_cfg(), register_snapshot=True)
    assert second["scoring_strategy_version"] != first["scoring_strategy_version"]

    # Display/history readers may intentionally bridge a manual strategy
    # prefix.  Similarity is an inference input and must not import a score from
    # the previous feedback profile into this effective contract.
    assert database.get_recent_entries(hours=72, cfg=second) == []
    assert database.get_scoring_contract_snapshot(
        first["scoring_strategy_version"]
    ) is not None
    assert database.get_scoring_contract_snapshot(
        second["scoring_strategy_version"]
    ) is not None


def test_contract_registration_rejects_inconsistent_payload():
    database.init_db()
    _insert_article("https://news.test/1", "一致性样例", 8.0)
    database.add_article_feedback("https://news.test/1", "good")
    profile = build_few_shot_profile(_cfg())
    base = _cfg()["scoring_strategy_version"]
    contract = {
        "strategy_version": f"{base}:fb-{profile['profile_hash'][:32]}",
        "base_strategy_version": base,
        "feedback_profile_hash": profile["profile_hash"],
        "feedback_profile_payload": profile["profile_payload"],
        "feedback_context": profile["context"],
        "feedback_context_hash": profile["context_hash"],
        "feedback_sample_ids": profile["sample_ids"],
        "feedback_snapshot": profile["snapshot"],
    }

    bad_suffix = dict(contract, strategy_version=f"{base}:fb-{'0' * 32}")
    with pytest.raises(ValueError, match="strategy version"):
        database.register_scoring_contract_snapshot(bad_suffix)
    bad_context = dict(contract, feedback_context_hash="0" * 64)
    with pytest.raises(ValueError, match="context hash"):
        database.register_scoring_contract_snapshot(bad_context)
    changed_context = "替换后的上下文"
    rebound_context = dict(
        contract,
        feedback_context=changed_context,
        feedback_context_hash=hashlib.sha256(
            changed_context.encode("utf-8")
        ).hexdigest(),
    )
    with pytest.raises(ValueError, match="profile payload"):
        database.register_scoring_contract_snapshot(rebound_context)
    bad_ids = dict(contract, feedback_sample_ids=[999999])
    with pytest.raises(ValueError, match="sample IDs"):
        database.register_scoring_contract_snapshot(bad_ids)
    bad_payload = json.loads(json.dumps(profile["profile_payload"], ensure_ascii=False))
    bad_payload["samples"][0]["raw_title"] = "被篡改"
    with pytest.raises(ValueError, match="snapshot"):
        database.register_scoring_contract_snapshot(
            dict(contract, feedback_profile_payload=bad_payload)
        )


def test_registered_context_hash_matches_model_visible_feedback_fragment(monkeypatch):
    database.init_db()
    _insert_article("https://news.test/1", "提示词样例", 8.0)
    database.add_article_feedback("https://news.test/1", "good")
    resolved = resolve_scoring_contract(_cfg(), register_snapshot=True)
    backend = LLMBackend(
        name="test", api_key="", base_url="", model="m", rate_limit=0,
    )
    backend.client = object()
    scorer = Scorer()
    monkeypatch.setattr(scorer, "backends", [backend])
    captured = {}

    def fake_call(_backend, messages, max_tokens=None, **_kwargs):
        captured["messages"] = messages
        return "[8.0]\n[宏观]\n[影响风险偏好]\n[看多]"

    monkeypatch.setattr(scorer, "_call_llm", fake_call)
    scorer.score(
        "标题", "摘要", "规则", {"llm_score_cache": {"enabled": False}},
        few_shot_context=resolved["_few_shot_context"],
    )
    user_text = captured["messages"][1]["content"]
    visible = user_text.split("【不可信反馈样例开始】\n", 1)[1].split(
        "\n【不可信反馈样例结束】", 1
    )[0]
    assert visible == resolved["_few_shot_context"]
    assert hashlib.sha256(visible.encode("utf-8")).hexdigest() == (
        resolved["_few_shot_context_hash"]
    )


def test_read_resolved_contract_is_registered_before_scoring():
    database.init_db()
    _insert_article("https://news.test/1", "延迟登记样例", 8.5)
    database.add_article_feedback("https://news.test/1", "good")

    read_cfg = resolve_scoring_contract(_cfg(), register_snapshot=False)
    assert read_cfg["_scoring_contract_registered"] is False
    assert database.get_scoring_contract_snapshot(
        read_cfg["scoring_strategy_version"]
    ) is None

    scoring_cfg = resolve_scoring_contract(read_cfg, register_snapshot=True)
    assert scoring_cfg["_scoring_contract_registered"] is True
    assert database.get_scoring_contract_snapshot(
        scoring_cfg["scoring_strategy_version"]
    ) is not None


def test_disabled_few_shot_keeps_base_strategy_and_does_not_write_contract():
    database.init_db()
    cfg = {
        "scoring_strategy_version": "base-only",
        "few_shot_feedback": {"enabled": False},
    }
    resolved = resolve_scoring_contract(cfg, register_snapshot=True)
    assert resolved["scoring_strategy_version"] == "base-only"
    assert resolved["_few_shot_context"] == ""
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM scoring_contract_snapshots"
        ).fetchone()[0] == 0


def test_scoring_contract_schema_version_is_explicit_v2():
    database.init_db()
    assert database.get_app_meta("scoring_contract_schema_version") == "2"


def test_process_entry_fails_closed_for_unresolved_feedback_contract():
    class Entry:
        title = "测试"
        link = "https://news.test/unresolved"

        @staticmethod
        def get(_key, default=None):
            return default

    with pytest.raises(RuntimeError, match="not resolved"):
        process_entry(
            Entry(),
            [],
            "prompt",
            {
                "scoring_strategy_version": "base",
                "few_shot_feedback": {"enabled": True},
            },
        )


def test_process_entry_rejects_context_outside_registered_contract():
    class Entry:
        title = "测试"
        link = "https://news.test/mismatch"

        @staticmethod
        def get(_key, default=None):
            return default

    with pytest.raises(RuntimeError, match="does not match"):
        process_entry(
            Entry(),
            [],
            "prompt",
            {
                "scoring_strategy_version": "base:fb-example",
                "few_shot_feedback": {"enabled": True},
                "_scoring_contract_resolved": True,
                "_scoring_contract_registered": True,
                "_few_shot_context": "registered context",
            },
            few_shot_context="different context",
        )
