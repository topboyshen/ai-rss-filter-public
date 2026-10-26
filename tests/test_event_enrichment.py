import hashlib
import json

import pytest

from db.event_core import (
    resolve_or_create_event,
    upsert_event_evidence,
    upsert_event_security,
    upsert_security,
)
from services.event_enrichment import (
    EVENT_MAPPING_CONTRACT_VERSION,
    EVENT_MAPPING_SEMANTIC_FINGERPRINT,
    enrich_event_security_mappings,
    event_mapping_migration_status,
    resolve_event_graph,
)


def _seed_event(
    conn,
    key,
    title,
    source,
    occurred_at,
    *,
    priority=8.6,
    excerpt="",
    snapshot_text=None,
    direction="看多",
    legacy=False,
):
    event_id = resolve_or_create_event(
        conn,
        {"article_url_sha256": key},
        title=title,
        event_type="AI算力",
        direction=direction,
        priority=priority,
        occurred_at=occurred_at,
        available_at=occurred_at + 60,
        strategy_version="strategy-v1",
        model_version="model-v1",
        now=occurred_at + 60,
    )
    from core.source_contract import ARTICLE_CLAIM_PREFIX

    # Explicit publisher-body fixtures are historical contract inputs. Ordinary
    # new titles use the real title-only identity, not a marker on AI excerpts.
    legacy = legacy or bool(excerpt) or (snapshot_text is not None and snapshot_text != title)
    snapshot = ((title + "\n" + excerpt if legacy else title) if snapshot_text is None else snapshot_text).strip()
    digest = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
    upsert_event_evidence(
        conn,
        event_id,
        source_uri=source,
        claim_key=f"article:{key}" if legacy else ARTICLE_CLAIM_PREFIX + digest,
        claim_text=title,
        excerpt=excerpt,
        snapshot_text=snapshot,
        snapshot_hash=digest,
        stance="supports",
        reliability=0.88,
        announcement_at=occurred_at,
        observed_at=occurred_at + 60,
        now=occurred_at + 60,
    )
    return event_id


def test_retire_stale_mapping_uses_event_id_index_without_full_mapping_scan():
    from db import database

    database.init_db()
    with database.get_connection() as conn:
        security_id = upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        event_id = _seed_event(
            conn,
            "retire-query-plan",
            "寒武纪发布新一代人工智能算力芯片",
            "https://example.test/retire-query-plan",
            1_000,
        )
        mapping_id = upsert_event_security(
            conn,
            event_id,
            security_id,
            relation_type="direct",
            direction="看多",
            confidence=0.9,
            relevance=10.0,
            rationale="原文直接点名寒武纪。",
            mapping_version="asset-map:test-contract",
            now=1_100,
        )
        conn.commit()

        statements = []
        conn.set_trace_callback(statements.append)
        assert (
            database.retire_stale_rule_event_mappings(
                conn,
                event_id=event_id,
                keep_mapping_ids=[mapping_id],
                current_mapping_version="asset-map:test-contract",
            )
            == 0
        )
        conn.set_trace_callback(None)
        retire_query = next(
            statement
            for statement in statements
            if statement.startswith("WITH RECURSIVE event_group(event_id)")
            and "SELECT mapping.mapping_id" in statement
        )
        plan = [
            str(row[3])
            for row in conn.execute("EXPLAIN QUERY PLAN " + retire_query)
        ]

    assert "mapping.event_id IN (SELECT event_id FROM event_group)" in retire_query
    assert "ORDER BY mapping.mapping_id" not in retire_query
    assert any(
        "SEARCH mapping USING INDEX uq_event_securities_current (event_id=?)"
        in detail
        for detail in plan
    ), plan
    assert not any(detail.startswith("SCAN mapping") for detail in plan), plan


def test_duplicate_merge_keeps_highest_member_priority():
    from db import database
    from db.event_core import canonical_event_id

    database.init_db()
    duplicate_title = "同一产业事件由两个独立来源确认"
    with database.get_connection() as conn:
        earlier = _seed_event(
            conn,
            "priority-low",
            duplicate_title,
            "https://alpha.example/priority",
            1_000,
            priority=5.0,
        )
        later = _seed_event(
            conn,
            "priority-high",
            duplicate_title,
            "https://beta.example/priority",
            1_600,
            priority=6.5,
        )
        conn.commit()

    result = resolve_event_graph(since=0)
    assert result["merged"] == 1
    with database.get_connection() as conn:
        root = canonical_event_id(conn, later)
        assert root == earlier
        assert conn.execute(
            "SELECT priority FROM events WHERE event_id = ?", (root,)
        ).fetchone()[0] == 6.5


def test_same_source_exact_replay_merges_only_inside_narrow_window():
    from db import database
    from db.event_core import canonical_event_id

    database.init_db()
    duplicate_title = "同一来源短时间重复发布的完整产业事件标题"
    with database.get_connection() as conn:
        first = _seed_event(
            conn,
            "same-source-first",
            duplicate_title,
            "https://wire.example/articles/first",
            1_000,
        )
        replay = _seed_event(
            conn,
            "same-source-replay",
            duplicate_title,
            "https://wire.example/articles/replay",
            1_000 + 90 * 60,
        )
        later = _seed_event(
            conn,
            "same-source-later",
            duplicate_title,
            "https://wire.example/articles/later",
            1_000 + 4 * 3600,
        )
        conn.commit()

    result = resolve_event_graph(since=0)

    assert result["merged"] == 1
    with database.get_connection() as conn:
        assert canonical_event_id(conn, replay) == first
        assert canonical_event_id(conn, later) == later
        history = conn.execute(
            "SELECT reason FROM event_merge_history ORDER BY merged_at"
        ).fetchall()
        assert [row[0] for row in history] == [
            "同一来源、完整规范化标题与快照均一致，且位于固定2小时窗口"
        ]


def test_same_source_replay_window_cannot_expand_by_transitive_pairs():
    from db import database
    from db.event_core import canonical_event_id

    database.init_db()
    title = "同一来源按固定窗口重复发布的完整产业事件标题"
    with database.get_connection() as conn:
        first = _seed_event(
            conn, "chain-first", title, "https://wire.example/first", 1_000
        )
        middle = _seed_event(
            conn,
            "chain-middle",
            title,
            "https://wire.example/middle",
            1_000 + 90 * 60,
        )
        last = _seed_event(
            conn,
            "chain-last",
            title,
            "https://wire.example/last",
            1_000 + 180 * 60,
        )
        conn.commit()

    result = resolve_event_graph(since=0)

    assert result["merged"] == 1
    with database.get_connection() as conn:
        assert canonical_event_id(conn, middle) == first
        assert canonical_event_id(conn, last) == last


def test_exact_merge_uses_full_title_not_truncated_prefix():
    from db import database
    from db.event_core import canonical_event_id

    database.init_db()
    prefix = "这是一个超过旧版八十字符截断边界的事件标题" * 8
    with database.get_connection() as conn:
        approved = _seed_event(
            conn,
            "long-approved",
            prefix + "方案甲最终获批",
            "https://alpha.example/long",
            1_000,
        )
        terminated = _seed_event(
            conn,
            "long-terminated",
            prefix + "方案乙最终终止",
            "https://beta.example/long",
            1_100,
        )
        conn.commit()

    result = resolve_event_graph(since=0)

    assert result["merged"] == 0
    with database.get_connection() as conn:
        assert canonical_event_id(conn, approved) == approved
        assert canonical_event_id(conn, terminated) == terminated


def test_identity_key_keeps_fact_bearing_bracket_contents():
    from core.digest_engine import (
        normalize_event_identity_key,
        normalize_event_key,
    )

    approved = "【方案甲】数据中心项目最终获批并进入建设阶段"
    terminated = "【方案乙】数据中心项目最终获批并进入建设阶段"

    # Preserve the existing digest grouping behavior, while ensuring the
    # irreversible Event Radar identity path can tell the facts apart.
    assert normalize_event_key(approved) == normalize_event_key(terminated)
    assert normalize_event_identity_key(approved) != normalize_event_identity_key(
        terminated
    )


def test_same_source_equal_title_with_different_snapshot_is_not_merged():
    from db import database
    from db.event_core import canonical_event_id

    database.init_db()
    title = "同一来源标题相同但正文事实发生变化的产业事件"
    with database.get_connection() as conn:
        first = _seed_event(
            conn,
            "snapshot-approved",
            title,
            "https://wire.example/snapshot-a",
            1_000,
            excerpt="方案甲已经获批。",
        )
        second = _seed_event(
            conn,
            "snapshot-terminated",
            title,
            "https://wire.example/snapshot-b",
            1_100,
            excerpt="方案甲已经终止。",
        )
        conn.commit()

    result = resolve_event_graph(since=0)

    assert result["merged"] == 0
    with database.get_connection() as conn:
        assert canonical_event_id(conn, first) == first
        assert canonical_event_id(conn, second) == second


def test_cross_source_equal_title_with_different_snapshot_is_not_merged():
    from db import database
    from db.event_core import canonical_event_id

    database.init_db()
    title = "两个独立来源使用同一标题但正文事实互相矛盾的产业事件"
    with database.get_connection() as conn:
        approved = _seed_event(
            conn,
            "cross-snapshot-approved",
            title,
            "https://alpha.example/cross-snapshot",
            1_000,
            excerpt="方案甲已经获批。",
        )
        terminated = _seed_event(
            conn,
            "cross-snapshot-terminated",
            title,
            "https://beta.example/cross-snapshot",
            1_100,
            excerpt="方案甲已经终止。",
        )
        conn.commit()

    first = resolve_event_graph(since=0)
    second = resolve_event_graph(since=0)

    assert first["merged"] == second["merged"] == 0
    with database.get_connection() as conn:
        assert canonical_event_id(conn, approved) == approved
        assert canonical_event_id(conn, terminated) == terminated
        assert conn.execute("SELECT COUNT(*) FROM event_merge_history").fetchone()[0] == 0


def test_cross_source_equal_title_without_complete_snapshot_fails_closed():
    from db import database
    from db.event_core import canonical_event_id

    database.init_db()
    title = "两个独立来源同标题但一侧缺少冻结快照的产业事件"
    with database.get_connection() as conn:
        frozen = _seed_event(
            conn,
            "cross-snapshot-present",
            title,
            "https://alpha.example/snapshot-present",
            1_000,
        )
        missing = _seed_event(
            conn,
            "cross-snapshot-missing",
            title,
            "https://beta.example/snapshot-missing",
            1_100,
            snapshot_text="",
        )
        conn.commit()

    result = resolve_event_graph(since=0)

    assert result["merged"] == 0
    with database.get_connection() as conn:
        assert canonical_event_id(conn, frozen) == frozen
        assert canonical_event_id(conn, missing) == missing


def test_subdomains_of_one_publisher_are_not_independent_confirmation():
    from db import database
    from db.event_core import canonical_event_id

    database.init_db()
    title = "同一注册域的不同子域转载完全相同的产业事件标题"
    with database.get_connection() as conn:
        first = _seed_event(
            conn,
            "publisher-subdomain-www",
            title,
            "https://www.publisher.example/article-a",
            1_000,
        )
        second = _seed_event(
            conn,
            "publisher-subdomain-api",
            title,
            "https://api.publisher.example/article-b",
            1_000 + 3 * 3600,
        )
        conn.commit()

    result = resolve_event_graph(since=0)

    # Three hours is outside the same-publisher replay window but inside the
    # independent-confirmation window, so a full-host comparison would overmerge.
    assert result["merged"] == 0
    with database.get_connection() as conn:
        assert canonical_event_id(conn, first) == first
        assert canonical_event_id(conn, second) == second


def test_multi_label_suffix_subdomains_share_one_publisher_identity():
    from db import database
    from db.event_core import canonical_event_id

    database.init_db()
    title = "同一中国注册域的新闻与接口子域转载相同产业事件标题"
    with database.get_connection() as conn:
        first = _seed_event(
            conn,
            "publisher-com-cn-news",
            title,
            "https://news.publisher.com.cn/article-a",
            1_000,
        )
        second = _seed_event(
            conn,
            "publisher-com-cn-api",
            title,
            "https://api.publisher.com.cn/article-b",
            1_000 + 3 * 3600,
        )
        conn.commit()

    result = resolve_event_graph(since=0)

    assert result["merged"] == 0
    with database.get_connection() as conn:
        assert canonical_event_id(conn, first) == first
        assert canonical_event_id(conn, second) == second


def test_ineligible_exact_pair_does_not_consume_qualified_merge_budget(
    monkeypatch,
):
    from uuid import UUID

    from db import database
    from db import event_core
    from db.event_core import canonical_event_id

    database.init_db()
    generated_ids = iter(str(UUID(int=index)) for index in range(1, 9))
    monkeypatch.setattr(event_core, "_new_uuid", lambda: next(generated_ids))

    with database.get_connection() as conn:
        bad_title = "固定排序靠前但冻结快照互相冲突的事件标题"
        bad_left = _seed_event(
            conn,
            "budget-bad-left",
            bad_title,
            "https://bad-alpha.example/a",
            1_000,
            excerpt="项目已经获批。",
        )
        bad_right = _seed_event(
            conn,
            "budget-bad-right",
            bad_title,
            "https://bad-beta.example/b",
            1_100,
            excerpt="项目已经终止。",
        )
        good_title = "固定排序靠后且具备完整确认快照的事件标题"
        good_left = _seed_event(
            conn,
            "budget-good-left",
            good_title,
            "https://good-alpha.example/a",
            2_000,
        )
        good_right = _seed_event(
            conn,
            "budget-good-right",
            good_title,
            "https://good-beta.example/b",
            2_100,
        )
        conn.commit()

    result = resolve_event_graph(since=0, max_pairs=1)

    assert result["examined_pairs"] == 2
    assert result["qualified_merges"] == 1
    assert result["candidate_pairs"] == 1
    assert result["merged"] == 1
    with database.get_connection() as conn:
        assert canonical_event_id(conn, bad_left) == bad_left
        assert canonical_event_id(conn, bad_right) == bad_right
        assert canonical_event_id(conn, good_right) == good_left


def test_third_source_cannot_wash_old_publisher_replay_into_merge_budget(
    monkeypatch,
):
    from uuid import UUID

    from db import database
    from db import event_core
    from db.event_core import canonical_event_id

    database.init_db()
    generated_ids = iter(str(UUID(int=index)) for index in range(1, 7))
    monkeypatch.setattr(event_core, "_new_uuid", lambda: next(generated_ids))
    title = "第三方独立确认不得洗白同一发布方超窗重放的事件标题"
    with database.get_connection() as conn:
        anchor = _seed_event(
            conn,
            "wash-anchor",
            title,
            "https://news.publisher.example/anchor",
            1_000,
        )
        old_replay = _seed_event(
            conn,
            "wash-old-replay",
            title,
            "https://api.publisher.example/old-replay",
            1_000 + 3 * 3600,
        )
        independent = _seed_event(
            conn,
            "wash-independent",
            title,
            "https://independent.example/confirmation",
            1_000 + 3 * 3600 + 60,
        )
        conn.commit()

    result = resolve_event_graph(since=0, max_pairs=1)

    assert result["qualified_merges"] == 1
    assert result["merged"] == 1
    assert result["skipped"] == 0
    with database.get_connection() as conn:
        assert canonical_event_id(conn, independent) == anchor
        assert canonical_event_id(conn, old_replay) == old_replay


def test_each_merge_revalidates_after_prior_edge_expands_publishers(monkeypatch):
    from uuid import UUID

    from db import database
    from db import event_core
    from db.event_core import canonical_event_id

    database.init_db()
    generated_ids = iter(str(UUID(int=index)) for index in range(1, 7))
    monkeypatch.setattr(event_core, "_new_uuid", lambda: next(generated_ids))
    title = "同轮批量边不得把同发布方超窗重放一起洗入独立来源根"
    with database.get_connection() as conn:
        alpha = _seed_event(
            conn,
            "sequential-alpha",
            title,
            "https://alpha.example/sequential",
            1_000,
        )
        beta = _seed_event(
            conn,
            "sequential-beta",
            title,
            "https://www.beta.example/sequential",
            11_800,
        )
        beta_late = _seed_event(
            conn,
            "sequential-beta-late",
            title,
            "https://api.beta.example/sequential",
            22_600,
        )
        conn.commit()

    result = resolve_event_graph(since=0, max_pairs=2)

    assert result["qualified_merges"] == 2
    assert result["merged"] == 1
    assert result["skipped"] == 1
    with database.get_connection() as conn:
        beta_roots = {
            canonical_event_id(conn, beta),
            canonical_event_id(conn, beta_late),
        }
        assert alpha in beta_roots
        assert len(beta_roots) == 2
        assert conn.execute("SELECT COUNT(*) FROM event_merge_history").fetchone()[0] == 1


def test_qualified_merge_budget_converges_across_runs():
    from db import database
    from db.event_core import canonical_event_id

    database.init_db()
    with database.get_connection() as conn:
        first_title = "第一组需要在有限预算内最终收敛的独立确认事件"
        first_left = _seed_event(
            conn, "converge-1a", first_title, "https://alpha.example/1a", 1_000
        )
        first_right = _seed_event(
            conn, "converge-1b", first_title, "https://beta.example/1b", 1_100
        )
        second_title = "第二组需要在有限预算内最终收敛的独立确认事件"
        second_left = _seed_event(
            conn, "converge-2a", second_title, "https://alpha.example/2a", 2_000
        )
        second_right = _seed_event(
            conn, "converge-2b", second_title, "https://beta.example/2b", 2_100
        )
        conn.commit()

    first = resolve_event_graph(since=0, max_pairs=1)
    second = resolve_event_graph(since=0, max_pairs=1)
    third = resolve_event_graph(since=0, max_pairs=1)

    assert [first["merged"], second["merged"], third["merged"]] == [1, 1, 0]
    assert [
        first["deferred_merges"],
        second["deferred_merges"],
        third["deferred_merges"],
    ] == [1, 0, 0]
    with database.get_connection() as conn:
        assert canonical_event_id(conn, first_right) == first_left
        assert canonical_event_id(conn, second_right) == second_left
        assert conn.execute("SELECT COUNT(*) FROM event_merge_history").fetchone()[0] == 2


def test_hard_merge_outside_large_exact_bucket_window_is_not_starved():
    from db import database
    from db.event_core import canonical_event_id

    database.init_db()
    title = "超大同标题桶中的合格冻结快照必须在线性预算内收敛"
    with database.get_connection() as conn:
        qualified_left = _seed_event(
            conn,
            "large-bucket-qualified-left",
            title,
            "https://alpha.example/large-bucket-qualified",
            1_000,
            excerpt="项目已经获批。",
        )
        qualified_right = _seed_event(
            conn,
            "large-bucket-qualified-right",
            title,
            "https://beta.example/large-bucket-qualified",
            1_001,
            excerpt="项目已经获批。",
        )
        # These newer, permanently conflicting snapshots fill the bounded
        # quadratic neighborhood. The qualified oldest pair sits outside it.
        for index in range(80):
            _seed_event(
                conn,
                f"large-bucket-conflict-{index}",
                title,
                f"https://conflict-{index}.example/large-bucket",
                2_000 + index,
                excerpt=f"互不相同的冻结事实版本 {index}。",
            )
        conn.commit()

    result = resolve_event_graph(since=0, max_pairs=1)

    assert result["qualified_merges"] >= 1
    assert result["merged"] == 1
    with database.get_connection() as conn:
        assert canonical_event_id(conn, qualified_right) == qualified_left


def test_small_budget_collapses_replays_before_independent_confirmation(monkeypatch):
    from db import database
    from db import event_core
    from db.event_core import canonical_event_id

    generated_ids = iter(
        [
            "00000000-0000-4000-8000-000000000001",
            "10000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-000000000002",
            "10000000-0000-4000-8000-000000000002",
            "00000000-0000-4000-8000-000000000003",
            "10000000-0000-4000-8000-000000000003",
        ]
    )
    monkeypatch.setattr(event_core, "_new_uuid", lambda: next(generated_ids))

    database.init_db()
    title = "同一批候选中的来源重放必须先于跨来源确认收敛"
    with database.get_connection() as conn:
        alpha = _seed_event(
            conn,
            "phase-alpha",
            title,
            "https://alpha.example/phase",
            1_000,
            excerpt="完全一致的冻结事实。",
        )
        beta = _seed_event(
            conn,
            "phase-beta",
            title,
            "https://www.beta.example/phase",
            1_100,
            excerpt="完全一致的冻结事实。",
        )
        beta_replay = _seed_event(
            conn,
            "phase-beta-replay",
            title,
            "https://api.beta.example/phase",
            1_150,
            excerpt="完全一致的冻结事实。",
        )
        conn.commit()

    results = [resolve_event_graph(since=0, max_pairs=1) for _ in range(3)]

    assert [item["merged"] for item in results] == [1, 1, 0]
    with database.get_connection() as conn:
        assert canonical_event_id(conn, beta) == alpha
        assert canonical_event_id(conn, beta_replay) == alpha
        assert conn.execute("SELECT COUNT(*) FROM event_merge_history").fetchone()[0] == 2


def test_similarity_write_budget_converges_without_rewriting_stable_prefix():
    from db import database

    database.init_db()
    with database.get_connection() as conn:
        security_id = upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        event_ids = []
        for index, suffix in enumerate(("甲", "乙", "丙")):
            event_id = _seed_event(
                conn,
                f"similarity-budget-{index}",
                f"寒武纪算力芯片订单进入交付阶段{suffix}",
                f"https://example.test/similarity-budget-{index}",
                1_000 + index,
            )
            event_ids.append(event_id)
            upsert_event_security(
                conn,
                event_id,
                security_id,
                relation_type="direct",
                direction="看多",
                confidence=0.9,
                relevance=10.0,
                rationale="测试共同证券相似边。",
                mapping_version="manual:test-similarity-budget",
                now=1_100 + index,
            )
        conn.commit()

    results = [resolve_event_graph(since=0, max_pairs=1) for _ in range(4)]

    assert [item["similarities"] for item in results] == [1, 1, 1, 0]
    assert [item["candidate_pairs"] for item in results] == [1, 1, 1, 0]
    with database.get_connection() as conn:
        rows = conn.execute(
            "SELECT version FROM event_similarities "
            "WHERE method = 'event-graph-v1' "
            "AND model_version = 'deterministic-title-security-v1' "
            "ORDER BY event_id, similar_event_id"
        ).fetchall()
        assert len(rows) == 3
        assert {int(row[0]) for row in rows} == {1}


def test_similarity_edge_is_removed_when_current_basis_disappears():
    from db import database
    from services.event_radar_query import _load_similar_events

    database.init_db()
    with database.get_connection() as conn:
        security_id = upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        event_ids = []
        mapping_ids = []
        for index, suffix in enumerate(("甲", "乙")):
            event_id = _seed_event(
                conn,
                f"similarity-retire-{index}",
                f"寒武纪算力芯片订单进入交付阶段{suffix}",
                f"https://example.test/similarity-retire-{index}",
                1_000 + index,
            )
            event_ids.append(event_id)
            mapping_ids.append(
                upsert_event_security(
                    conn,
                    event_id,
                    security_id,
                    relation_type="direct",
                    direction="看多",
                    confidence=0.9,
                    relevance=10.0,
                    rationale="测试相似边撤销。",
                    mapping_version="manual:test-similarity-retire",
                    now=1_100 + index,
                )
            )
        conn.commit()

    created = resolve_event_graph(since=0)
    assert created["similarities"] == 1
    settled = resolve_event_graph(since=2_000)
    assert settled["graph_dirty_groups_acked"] == 2
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE event_securities SET is_current = 0 WHERE mapping_id = ?",
            (mapping_ids[1],),
        )
        conn.commit()

    retired = resolve_event_graph(since=0)
    stable = resolve_event_graph(since=0)

    assert retired["deleted_similarities"] == 1
    assert stable["deleted_similarities"] == 0
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM event_similarities "
            "WHERE method = 'event-graph-v1' "
            "AND model_version = 'deterministic-title-security-v1'"
        ).fetchone()[0] == 0
        root = conn.execute(
            "SELECT occurred_at, available_at FROM events WHERE event_id = ?",
            (event_ids[0],),
        ).fetchone()
        related, truncated = _load_similar_events(
            conn,
            event_ids[0],
            int(root[0]),
            int(root[1]),
            2_000_000_000,
        )
        assert related == []
        assert truncated is False


def test_similarity_reconciles_out_of_scope_counterpart_for_recent_event():
    from db import database
    from services.event_radar_query import _load_similar_events

    database.init_db()
    with database.get_connection() as conn:
        security_id = upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        old_event = _seed_event(
            conn,
            "similarity-cutoff-old",
            "寒武纪算力芯片订单进入交付阶段甲",
            "https://old.example.test/similarity-cutoff",
            900,
        )
        recent_event = _seed_event(
            conn,
            "similarity-cutoff-recent",
            "寒武纪算力芯片订单进入交付阶段乙",
            "https://recent.example.test/similarity-cutoff",
            1_100,
        )
        for index, event_id in enumerate((old_event, recent_event)):
            mapping_id = upsert_event_security(
                conn,
                event_id,
                security_id,
                relation_type="direct",
                direction="看多",
                confidence=0.9,
                relevance=10.0,
                rationale="测试跨时间窗相似边回收。",
                mapping_version="manual:test-similarity-cutoff",
                now=1_200 + index,
            )
            if event_id == recent_event:
                recent_mapping_id = mapping_id
        conn.commit()

    created = resolve_event_graph(since=0)
    assert created["similarities"] == 1
    settled = resolve_event_graph(since=2_000)
    assert settled["graph_dirty_groups_acked"] == 2
    with database.get_connection() as conn:
        recent = conn.execute(
            "SELECT occurred_at, available_at FROM events WHERE event_id = ?",
            (recent_event,),
        ).fetchone()
        visible, _truncated = _load_similar_events(
            conn,
            recent_event,
            int(recent[0]),
            int(recent[1]),
            2_000_000_000,
        )
        assert [item["event_id"] for item in visible] == [old_event]
        conn.execute(
            "UPDATE event_securities SET is_current = 0 WHERE mapping_id = ?",
            (recent_mapping_id,),
        )
        conn.commit()

    reconciled = resolve_event_graph(since=1_000)

    assert reconciled["events"] == 1
    assert reconciled["deleted_similarities"] == 1
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM event_similarities WHERE method = 'event-graph-v1'"
        ).fetchone()[0] == 0
        recent = conn.execute(
            "SELECT occurred_at, available_at FROM events WHERE event_id = ?",
            (recent_event,),
        ).fetchone()
        visible, truncated = _load_similar_events(
            conn,
            recent_event,
            int(recent[0]),
            int(recent[1]),
            2_000_000_000,
        )
        assert visible == []
        assert truncated is False


def test_recent_merged_member_keeps_old_canonical_group_in_graph_scope():
    from db import database
    from db.event_core import merge_events

    database.init_db()
    with database.get_connection() as conn:
        security_id = upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        old_root = _seed_event(
            conn,
            "similarity-recent-child-root",
            "寒武纪算力芯片订单进入交付阶段",
            "https://root.example.test/similarity-recent-child",
            800,
        )
        recent_child = _seed_event(
            conn,
            "similarity-recent-child-member",
            "寒武纪算力芯片订单进入交付阶段",
            "https://child.example.test/similarity-recent-child",
            1_100,
        )
        old_peer = _seed_event(
            conn,
            "similarity-recent-child-peer",
            "寒武纪算力芯片订单开始批量交付",
            "https://peer.example.test/similarity-recent-child",
            900,
        )
        child_mapping_id = upsert_event_security(
            conn,
            recent_child,
            security_id,
            relation_type="direct",
            direction="看多",
            confidence=0.9,
            relevance=10.0,
            rationale="近期成员提供共同证券。",
            mapping_version="manual:test-recent-child-scope",
            now=1_200,
        )
        upsert_event_security(
            conn,
            old_peer,
            security_id,
            relation_type="direct",
            direction="看多",
            confidence=0.9,
            relevance=10.0,
            rationale="旧端点提供共同证券。",
            mapping_version="manual:test-recent-child-scope",
            now=1_201,
        )
        merge_events(
            conn,
            recent_child,
            old_root,
            reason="测试近期成员的 canonical scope",
            actor="test",
            now=1_300,
        )
        conn.commit()

    created = resolve_event_graph(since=0)
    assert created["similarities"] == 1
    settled = resolve_event_graph(since=2_000)
    assert settled["graph_dirty_groups_acked"] == 2
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE event_securities SET is_current = 0 WHERE mapping_id = ?",
            (child_mapping_id,),
        )
        conn.commit()

    reconciled = resolve_event_graph(since=1_000)

    assert reconciled["events"] == 1
    assert reconciled["deleted_similarities"] == 1
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM event_similarities WHERE method = 'event-graph-v1'"
        ).fetchone()[0] == 0


def test_similarity_reconciliation_does_not_own_old_old_edge_via_hydration():
    from db import database
    from db.event_core import upsert_event_similarity

    database.init_db()
    with database.get_connection() as conn:
        old_left = _seed_event(
            conn,
            "similarity-non-owned-left",
            "完全无关的旧事件甲",
            "https://old-left.example.test/non-owned",
            800,
        )
        old_right = _seed_event(
            conn,
            "similarity-non-owned-right",
            "完全无关的旧事件乙",
            "https://old-right.example.test/non-owned",
            900,
        )
        recent_left = _seed_event(
            conn,
            "similarity-owned-left",
            "完全无关的近期事件甲",
            "https://recent-left.example.test/non-owned",
            1_100,
        )
        recent_right = _seed_event(
            conn,
            "similarity-owned-right",
            "完全无关的近期事件乙",
            "https://recent-right.example.test/non-owned",
            1_200,
        )
        for left, right, rationale in (
            (recent_left, old_left, "待回收的近期甲边"),
            (recent_right, old_right, "待回收的近期乙边"),
            (old_left, old_right, "本轮不拥有的旧旧边"),
        ):
            upsert_event_similarity(
                conn,
                left,
                right,
                similarity=0.8,
                method="event-graph-v1",
                model_version="deterministic-title-security-v1",
                rationale=rationale,
                common_securities=[],
                shared_paths=[],
                now=1_300,
            )
        conn.execute("DELETE FROM event_similarity_replay_dirty_roots")
        conn.commit()

    reconciled = resolve_event_graph(since=1_000)

    assert reconciled["deleted_similarities"] == 2
    with database.get_connection() as conn:
        rows = conn.execute(
            "SELECT event_id, similar_event_id, rationale FROM event_similarities "
            "WHERE method = 'event-graph-v1' ORDER BY event_id, similar_event_id"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (*sorted((old_left, old_right)), "本轮不拥有的旧旧边")
        ]


def test_similarity_incident_query_batches_both_endpoints_and_deduplicates(
    monkeypatch,
):
    from db import database
    from db.event_core import upsert_event_similarity
    from services import event_enrichment

    database.init_db()
    with database.get_connection() as conn:
        event_ids = sorted(
            _seed_event(
                conn,
                f"similarity-incident-{index}",
                f"相似关系端点索引测试事件{index}",
                f"https://example.test/similarity-incident-{index}",
                800 + index,
            )
            for index in range(3)
        )
        expected_pairs = [
            (event_ids[0], event_ids[1]),
            (event_ids[1], event_ids[2]),
        ]
        for index, pair in enumerate(expected_pairs):
            upsert_event_similarity(
                conn,
                pair[0],
                pair[1],
                similarity=0.8,
                method="event-graph-v1",
                model_version="deterministic-title-security-v1",
                rationale=f"当前契约边{index}",
                common_securities=[],
                shared_paths=[],
                now=1_000 + index,
            )
        upsert_event_similarity(
            conn,
            event_ids[0],
            event_ids[2],
            similarity=0.9,
            method="manual",
            model_version="operator-v1",
            rationale="其他契约不得混入。",
            common_securities=[],
            shared_paths=[],
            now=1_100,
        )
        conn.commit()

        monkeypatch.setattr(event_enrichment, "_SIMILARITY_QUERY_BATCH", 1)
        rows = event_enrichment._incident_similarity_rows(
            conn,
            tuple(reversed(event_ids)),
        )
        center_rows = event_enrichment._incident_similarity_rows(
            conn,
            (event_ids[1],),
        )

    assert [(row[0], row[1]) for row in rows] == expected_pairs
    assert [row[3] for row in rows] == ["当前契约边0", "当前契约边1"]
    assert [(row[0], row[1]) for row in center_rows] == expected_pairs


def test_graph_rows_keep_complete_members_and_in_memory_merge_unions_them():
    from db import database
    from db.event_core import merge_events
    from services import event_enrichment

    database.init_db()
    with database.get_connection() as conn:
        target = _seed_event(
            conn,
            "similarity-members-target",
            "事件图成员集合目标事件",
            "https://target.example.test/similarity-members",
            800,
        )
        child = _seed_event(
            conn,
            "similarity-members-child",
            "事件图成员集合历史子事件",
            "https://child.example.test/similarity-members",
            900,
        )
        source = _seed_event(
            conn,
            "similarity-members-source",
            "事件图成员集合事务内来源事件",
            "https://source.example.test/similarity-members",
            1_000,
        )
        merge_events(
            conn,
            child,
            target,
            reason="测试完整 canonical member 集合",
            actor="test",
            now=1_100,
        )
        conn.commit()

        rows = event_enrichment._event_graph_rows(conn, 0)

    by_id = {str(item["event_id"]): item for item in rows}
    assert by_id[target]["member_ids"] == {target, child}
    assert by_id[source]["member_ids"] == {source}

    event_enrichment._merge_graph_row_state(by_id, source, target)

    assert source not in by_id
    assert by_id[target]["member_ids"] == {target, child, source}


def test_similarity_reconciliation_requeries_incident_edges_three_times(monkeypatch):
    from db import database
    from db.event_core import upsert_event_similarity
    from services import event_enrichment

    database.init_db()
    with database.get_connection() as conn:
        left = _seed_event(
            conn,
            "similarity-three-phase-left",
            "完全无关的三阶段重验事件甲",
            "https://left.example.test/similarity-three-phase",
            800,
        )
        right = _seed_event(
            conn,
            "similarity-three-phase-right",
            "完全无关的三阶段重验事件乙",
            "https://right.example.test/similarity-three-phase",
            900,
        )
        upsert_event_similarity(
            conn,
            left,
            right,
            similarity=0.8,
            method="event-graph-v1",
            model_version="deterministic-title-security-v1",
            rationale="需要在事务内回收的旧边。",
            common_securities=[],
            shared_paths=[],
            now=1_000,
        )
        conn.commit()

    original = event_enrichment._incident_similarity_rows
    phases = []

    def traced_incident_rows(conn, member_ids):
        phases.append(tuple(member_ids))
        return original(conn, member_ids)

    monkeypatch.setattr(
        event_enrichment,
        "_incident_similarity_rows",
        traced_incident_rows,
    )

    result = resolve_event_graph(since=2_000, max_pairs=1)

    assert result["deleted_similarities"] == 1
    assert len(phases) == 3
    assert len(phases[0]) == 1
    assert set(phases[0]).issubset({left, right})
    assert phases[1] == phases[2] == phases[0]


def test_out_of_window_mapping_retirement_has_durable_graph_handoff():
    from db import database

    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        first = _seed_event(
            conn,
            "mapping-graph-handoff-first",
            "寒武纪算力芯片订单进入交付阶段甲",
            "https://first.example.test/mapping-graph-handoff",
            800,
        )
        second = _seed_event(
            conn,
            "mapping-graph-handoff-second",
            "寒武纪算力芯片订单进入交付阶段乙",
            "https://second.example.test/mapping-graph-handoff",
            900,
        )
        conn.execute(
            "UPDATE events SET event_type = '其他事件', version = version + 1, "
            "updated_at = 1000 WHERE event_id IN (?, ?)",
            (first, second),
        )
        conn.commit()

    mapped = enrich_event_security_mappings(since=0)
    assert mapped["mapped_evidence"] == 2
    created = resolve_event_graph(since=0)
    assert created["similarities"] == 1
    settled = resolve_event_graph(since=2_000)
    assert settled["graph_dirty_groups_acked"] == 2
    with database.get_connection() as conn:
        evidence = conn.execute(
            "SELECT source_uri, claim_key, snapshot_text, announcement_at, observed_at "
            "FROM event_evidence WHERE event_id = ? AND mapping_id IS NULL",
            (second,),
        ).fetchone()
        upsert_event_evidence(
            conn,
            second,
            source_uri=str(evidence[0]),
            claim_key=str(evidence[1]),
            claim_text="某产业项目披露常规建设进展",
            excerpt="项目按既定计划推进。",
            snapshot_text=str(evidence[2]),
            stance="supports",
            reliability=0.88,
            announcement_at=int(evidence[3]),
            observed_at=int(evidence[4]),
            now=2_000,
        )
        conn.commit()

    replayed = enrich_event_security_mappings(since=2_000)

    assert replayed["retired_mappings"] == 1
    assert replayed["graph_dirty_groups"] == 1
    with database.get_connection() as conn:
        # This is the crash boundary: mapping committed, graph has not run.
        assert conn.execute(
            "SELECT COUNT(*) FROM event_similarity_replay_dirty_roots "
            "WHERE root_event_id = ?",
            (second,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM event_similarities"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM event_mapping_replay_dirty_roots"
        ).fetchone()[0] == 0

    reconciled = resolve_event_graph(since=2_000)

    assert reconciled["events"] == 1
    assert reconciled["deleted_similarities"] == 1
    assert reconciled["graph_dirty_groups_acked"] == 0
    with database.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM event_similarities").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM event_similarity_replay_dirty_roots"
        ).fetchone()[0] == 2
    final = resolve_event_graph(since=2_000)
    assert final["graph_dirty_groups_acked"] == 2
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM event_similarity_replay_dirty_roots"
        ).fetchone()[0] == 0


def test_graph_handoff_and_new_mapping_input_are_independent_obligations():
    from db import database

    database.init_db()
    source_uri = "https://example.test/dual-replay-obligations"
    frozen_snapshot = "寒武纪算力产品进展\n原始冻结快照。"
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        event_id = resolve_or_create_event(
            conn,
            {"article_url_sha256": "dual-replay-obligations"},
            title="寒武纪算力产品进展",
            event_type="AI算力",
            direction="看多",
            priority=8.0,
            occurred_at=800,
            available_at=860,
            strategy_version="strategy-v1",
            model_version="model-v1",
            now=860,
        )
        upsert_event_evidence(
            conn,
            event_id,
            source_uri=source_uri,
            claim_key="article:dual-replay-obligations",
            claim_text="寒武纪算力产品进展",
            excerpt="原文直接点名寒武纪。",
            snapshot_text=frozen_snapshot,
            stance="supports",
            reliability=0.88,
            announcement_at=800,
            observed_at=860,
            now=860,
        )
        conn.commit()

    enrich_event_security_mappings(include_legacy=True, since=0)
    resolve_event_graph(since=0)
    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO event_similarity_replay_dirty_roots "
            "(root_event_id, dirty_at) VALUES (?, ?)",
            (event_id, 1_000),
        )
        upsert_event_evidence(
            conn,
            event_id,
            source_uri=source_uri,
            claim_key="article:dual-replay-obligations",
            claim_text="寒武纪发布新一代人工智能算力芯片",
            excerpt="新的映射输入仍直接点名寒武纪。",
            snapshot_text=frozen_snapshot,
            stance="supports",
            reliability=0.88,
            announcement_at=800,
            observed_at=860,
            now=2_000,
        )
        conn.commit()

    graph = resolve_event_graph(since=2_000)

    assert graph["graph_dirty_groups_acked"] == 1
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM event_similarity_replay_dirty_roots"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT reason FROM event_mapping_replay_dirty_roots "
            "WHERE root_event_id = ?",
            (event_id,),
        ).fetchone()[0] == "base_evidence_update"

    mapping = enrich_event_security_mappings(include_legacy=True, since=2_000)
    assert mapping["processed_evidence"] == 1
    assert mapping["graph_dirty_groups"] == 1
    final_graph = resolve_event_graph(since=2_000)
    assert final_graph["graph_dirty_groups_acked"] == 1
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM event_mapping_replay_dirty_roots"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM event_similarity_replay_dirty_roots"
        ).fetchone()[0] == 0


def test_graph_dirty_root_is_acked_only_after_all_budgeted_edges_converge():
    from db import database
    from db.event_core import upsert_event_similarity

    database.init_db()
    with database.get_connection() as conn:
        root = _seed_event(
            conn,
            "graph-budget-root",
            "完全无关的历史中心事件",
            "https://root.example.test/graph-budget",
            800,
        )
        peers = [
            _seed_event(
                conn,
                f"graph-budget-peer-{index}",
                f"完全无关的历史外围事件{index}",
                f"https://peer-{index}.example.test/graph-budget",
                900 + index,
            )
            for index in range(2)
        ]
        for peer in peers:
            upsert_event_similarity(
                conn,
                root,
                peer,
                similarity=0.8,
                method="event-graph-v1",
                model_version="deterministic-title-security-v1",
                rationale="待预算回收的历史边。",
                common_securities=[],
                shared_paths=[],
                now=1_000,
            )
        conn.execute("DELETE FROM event_similarity_replay_dirty_roots")
        conn.execute(
            "INSERT INTO event_similarity_replay_dirty_roots "
            "(root_event_id, dirty_at) VALUES (?, ?)",
            (root, 1_100),
        )
        conn.commit()

    first = resolve_event_graph(since=2_000, max_pairs=1)
    with database.get_connection() as conn:
        assert first["deleted_similarities"] == 1
        assert first["graph_dirty_groups_acked"] == 0
        assert conn.execute("SELECT COUNT(*) FROM event_similarities").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM event_similarity_replay_dirty_roots "
            "WHERE root_event_id = ?",
            (root,),
        ).fetchone()[0] == 1

    results = [first]
    for _attempt in range(10):
        with database.get_connection() as conn:
            edge_count = conn.execute(
                "SELECT COUNT(*) FROM event_similarities"
            ).fetchone()[0]
            marker_count = conn.execute(
                "SELECT COUNT(*) FROM event_similarity_replay_dirty_roots"
            ).fetchone()[0]
            if edge_count == marker_count == 0:
                break
            if edge_count:
                assert conn.execute(
                    "SELECT COUNT(*) FROM event_similarity_replay_dirty_roots "
                    "WHERE root_event_id = ?",
                    (root,),
                ).fetchone()[0] == 1
        results.append(resolve_event_graph(since=2_000, max_pairs=1))
    else:
        pytest.fail("budgeted similarity reconciliation did not converge")

    assert sum(item["deleted_similarities"] for item in results) == 2
    with database.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM event_similarities").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM event_similarity_replay_dirty_roots"
        ).fetchone()[0] == 0


def test_suppressed_old_merge_keeps_both_dirty_roots_pending():
    from db import database
    from db.event_core import canonical_event_id

    database.init_db()
    duplicate_title = "历史独立来源同标题事件必须保留待合并标记"
    with database.get_connection() as conn:
        first = _seed_event(
            conn,
            "suppressed-old-merge-first",
            duplicate_title,
            "https://alpha.example/suppressed-old-merge",
            1_000,
        )
        second = _seed_event(
            conn,
            "suppressed-old-merge-second",
            duplicate_title,
            "https://beta.example/suppressed-old-merge",
            1_600,
        )
        conn.executemany(
            "INSERT INTO event_similarity_replay_dirty_roots "
            "(root_event_id, dirty_at) VALUES (?, ?)",
            ((first, 2_000), (second, 2_000)),
        )
        conn.commit()

    result = resolve_event_graph(allow_merges=False)

    assert result["merges_allowed"] is False
    assert result["qualified_merges"] == 1
    assert result["deferred_merges"] == 1
    assert result["suppressed_merges"] == 1
    assert result["merged"] == 0
    assert result["candidate_pairs"] == 0
    assert result["graph_dirty_groups_acked"] == 0
    with database.get_connection() as conn:
        assert {
            (str(row[0]), int(row[1]))
            for row in conn.execute(
                "SELECT root_event_id, generation "
                "FROM event_similarity_replay_dirty_roots"
            )
        } == {(first, 1), (second, 1)}
        assert canonical_event_id(conn, first) == first
        assert canonical_event_id(conn, second) == second
        assert conn.execute(
            "SELECT COUNT(*) FROM event_merge_history"
        ).fetchone()[0] == 0


def test_suppressed_old_merge_converges_after_merges_are_enabled():
    from db import database
    from db.event_core import canonical_event_id

    database.init_db()
    title = "被迁移门禁抑制的历史合并在正常路径恢复后应有限收敛"
    with database.get_connection() as conn:
        first = _seed_event(
            conn,
            "suppressed-then-enabled-first",
            title,
            "https://alpha.example/suppressed-then-enabled",
            1_000,
        )
        second = _seed_event(
            conn,
            "suppressed-then-enabled-second",
            title,
            "https://beta.example/suppressed-then-enabled",
            1_600,
        )
        conn.executemany(
            "INSERT INTO event_similarity_replay_dirty_roots "
            "(root_event_id, dirty_at) VALUES (?, ?)",
            ((first, 2_000), (second, 2_000)),
        )
        conn.commit()

    suppressed = resolve_event_graph(allow_merges=False)
    merged = resolve_event_graph(since=0, allow_merges=True)
    verified = resolve_event_graph(since=0, allow_merges=True)

    assert suppressed["deferred_merges"] == 1
    assert merged["merged"] == 1
    assert merged["deferred_merges"] == 0
    assert verified["qualified_merges"] == 0
    assert verified["deferred_merges"] == 0
    with database.get_connection() as conn:
        assert canonical_event_id(conn, second) == first
        assert conn.execute(
            "SELECT COUNT(*) FROM event_mapping_replay_dirty_roots "
            "WHERE root_event_id = ?",
            (first,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM event_similarity_replay_dirty_roots"
        ).fetchone()[0] == 0


def test_selected_merge_failure_keeps_dirty_roots_for_retry(monkeypatch):
    from db import database
    from db.event_core import EventCoreConflictError, canonical_event_id
    from services import event_enrichment

    database.init_db()
    title = "已选中但落库失败的历史合并不得丢失重试义务"
    with database.get_connection() as conn:
        first = _seed_event(
            conn,
            "selected-merge-failure-first",
            title,
            "https://alpha.example/selected-merge-failure",
            1_000,
        )
        second = _seed_event(
            conn,
            "selected-merge-failure-second",
            title,
            "https://beta.example/selected-merge-failure",
            1_600,
        )
        conn.executemany(
            "INSERT INTO event_similarity_replay_dirty_roots "
            "(root_event_id, dirty_at) VALUES (?, ?)",
            ((first, 2_000), (second, 2_000)),
        )
        conn.commit()

    real_merge_events = event_enrichment.merge_events

    def fail_selected_merge(*_args, **_kwargs):
        raise EventCoreConflictError("simulated live merge conflict")

    monkeypatch.setattr(event_enrichment, "merge_events", fail_selected_merge)
    failed = resolve_event_graph(since=0, allow_merges=True)

    assert failed["qualified_merges"] == 1
    assert failed["merged"] == 0
    assert failed["deferred_merges"] == 1
    assert failed["graph_dirty_groups_acked"] == 0
    with database.get_connection() as conn:
        assert {
            str(row[0])
            for row in conn.execute(
                "SELECT root_event_id FROM event_similarity_replay_dirty_roots"
            )
        } == {first, second}
        assert canonical_event_id(conn, second) == second

    monkeypatch.setattr(event_enrichment, "merge_events", real_merge_events)
    retried = resolve_event_graph(since=0, allow_merges=True)
    verified = resolve_event_graph(since=0, allow_merges=True)

    assert retried["merged"] == 1
    assert retried["deferred_merges"] == 0
    assert verified["qualified_merges"] == 0
    with database.get_connection() as conn:
        assert canonical_event_id(conn, second) == first
        assert conn.execute(
            "SELECT COUNT(*) FROM event_similarity_replay_dirty_roots"
        ).fetchone()[0] == 0


def test_graph_dirty_ack_uses_generation_compare_and_swap(monkeypatch):
    from db import database
    from services import event_enrichment

    database.init_db()
    with database.get_connection() as conn:
        event_id = _seed_event(
            conn,
            "graph-dirty-cas",
            "历史事件图标记必须使用代次确认",
            "https://example.test/graph-dirty-cas",
            800,
        )
        conn.execute(
            "INSERT INTO event_similarity_replay_dirty_roots "
            "(root_event_id, dirty_at) VALUES (?, ?)",
            (event_id, 1_000),
        )
        conn.commit()

    original_graph_rows = event_enrichment._event_graph_rows
    calls = 0

    def graph_rows_with_new_wakeup(conn, cutoff, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            event_enrichment._mark_mapping_group_similarity_dirty(
                conn,
                event_id,
                dirty_at=1_000,
            )
        return original_graph_rows(conn, cutoff, **kwargs)

    monkeypatch.setattr(
        event_enrichment,
        "_event_graph_rows",
        graph_rows_with_new_wakeup,
    )

    raced = resolve_event_graph(since=2_000)

    assert calls == 2
    assert raced["graph_dirty_groups_acked"] == 0
    with database.get_connection() as conn:
        assert tuple(
            conn.execute(
                "SELECT generation, dirty_at FROM "
                "event_similarity_replay_dirty_roots WHERE root_event_id = ?",
                (event_id,),
            ).fetchone()
        ) == (2, 1_000)

    monkeypatch.setattr(event_enrichment, "_event_graph_rows", original_graph_rows)
    settled = resolve_event_graph(since=2_000)
    assert settled["graph_dirty_groups_acked"] == 1


def test_child_keyed_similarity_is_migrated_then_removed_when_basis_disappears():
    from db import database
    from db.event_core import merge_events, upsert_event_similarity

    database.init_db()
    with database.get_connection() as conn:
        security_id = upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        duplicate_title = "寒武纪算力芯片订单进入交付阶段"
        root = _seed_event(
            conn,
            "similarity-child-root",
            duplicate_title,
            "https://alpha.example/similarity-child",
            1_000,
        )
        child = _seed_event(
            conn,
            "similarity-child-member",
            duplicate_title,
            "https://beta.example/similarity-child",
            1_001,
        )
        peer = _seed_event(
            conn,
            "similarity-child-peer",
            "寒武纪算力芯片订单开始批量交付",
            "https://gamma.example/similarity-child",
            1_002,
        )
        child_mapping = upsert_event_security(
            conn,
            child,
            security_id,
            relation_type="direct",
            direction="看多",
            confidence=0.9,
            relevance=10.0,
            rationale="子事件共同证券。",
            mapping_version="manual:test-child-similarity",
            now=1_100,
        )
        peer_mapping = upsert_event_security(
            conn,
            peer,
            security_id,
            relation_type="direct",
            direction="看多",
            confidence=0.9,
            relevance=10.0,
            rationale="相关事件共同证券。",
            mapping_version="manual:test-child-similarity",
            now=1_101,
        )
        upsert_event_similarity(
            conn,
            child,
            peer,
            similarity=0.8,
            method="event-graph-v1",
            model_version="deterministic-title-security-v1",
            rationale="合并前子事件相似边。",
            common_securities=[security_id],
            shared_paths=[],
            now=1_200,
        )
        merge_events(
            conn,
            child,
            root,
            reason="测试相似边 canonical 迁移",
            actor="test",
            now=1_300,
        )
        conn.commit()

    migrated = resolve_event_graph(since=0)

    assert migrated["migrated_similarities"] == 1
    with database.get_connection() as conn:
        rows = conn.execute(
            "SELECT event_id, similar_event_id, version FROM event_similarities "
            "WHERE method = 'event-graph-v1' AND model_version = "
            "'deterministic-title-security-v1'"
        ).fetchall()
        assert [tuple(row) for row in rows] == [(*sorted((root, peer)), 2)]
        conn.execute(
            "UPDATE event_securities SET is_current = 0 WHERE mapping_id IN (?, ?)",
            (child_mapping, peer_mapping),
        )
        conn.commit()

    removed = resolve_event_graph(since=0)

    assert removed["deleted_similarities"] == 1
    with database.get_connection() as conn:
        # This is also the previous-image reader contract: there is no zero-score
        # tombstone for an older query implementation to display after rollback.
        assert conn.execute(
            "SELECT COUNT(*) FROM event_similarities WHERE method = 'event-graph-v1'"
        ).fetchone()[0] == 0


def test_similarity_candidate_is_revalidated_inside_write_transaction(monkeypatch):
    from db import database
    from services import event_enrichment

    database.init_db()
    with database.get_connection() as conn:
        security_id = upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        mapping_ids = []
        for index, suffix in enumerate(("甲", "乙")):
            event_id = _seed_event(
                conn,
                f"similarity-revalidate-{index}",
                f"寒武纪算力芯片订单进入交付阶段{suffix}",
                f"https://example.test/similarity-revalidate-{index}",
                1_000 + index,
            )
            mapping_ids.append(
                upsert_event_security(
                    conn,
                    event_id,
                    security_id,
                    relation_type="direct",
                    direction="看多",
                    confidence=0.9,
                    relevance=10.0,
                    rationale="测试事务内相似边重验。",
                    mapping_version="manual:test-similarity-revalidate",
                    now=1_100 + index,
                )
            )
        conn.commit()

    original_graph_rows = event_enrichment._event_graph_rows
    calls = 0

    def graph_rows_with_late_retirement(conn, cutoff, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            conn.execute(
                "UPDATE event_securities SET is_current = 0 WHERE mapping_id = ?",
                (mapping_ids[1],),
            )
        return original_graph_rows(conn, cutoff, **kwargs)

    monkeypatch.setattr(
        event_enrichment,
        "_event_graph_rows",
        graph_rows_with_late_retirement,
    )

    result = resolve_event_graph(since=0)

    assert calls == 2
    assert result["similarities"] == 0
    assert result["skipped"] == 1
    with database.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM event_similarities").fetchone()[0] == 0


def test_stable_similarity_is_reconciled_again_inside_write_transaction(monkeypatch):
    from db import database
    from services import event_enrichment

    database.init_db()
    with database.get_connection() as conn:
        security_id = upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        mapping_ids = []
        for index, suffix in enumerate(("甲", "乙")):
            event_id = _seed_event(
                conn,
                f"similarity-stable-race-{index}",
                f"寒武纪算力芯片订单进入交付阶段{suffix}",
                f"https://example.test/similarity-stable-race-{index}",
                1_000 + index,
            )
            mapping_ids.append(
                upsert_event_security(
                    conn,
                    event_id,
                    security_id,
                    relation_type="direct",
                    direction="看多",
                    confidence=0.9,
                    relevance=10.0,
                    rationale="测试稳定相似边事务内重验。",
                    mapping_version="manual:test-similarity-stable-race",
                    now=1_100 + index,
                )
            )
        conn.commit()

    created = resolve_event_graph(since=0)
    assert created["similarities"] == 1

    original_graph_rows = event_enrichment._event_graph_rows
    calls = 0

    def graph_rows_with_late_retirement(conn, cutoff, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            conn.execute(
                "UPDATE event_securities SET is_current = 0 WHERE mapping_id = ?",
                (mapping_ids[1],),
            )
        return original_graph_rows(conn, cutoff, **kwargs)

    monkeypatch.setattr(
        event_enrichment,
        "_event_graph_rows",
        graph_rows_with_late_retirement,
    )

    reconciled = resolve_event_graph(since=0)

    assert calls == 2
    assert reconciled["pending_similarity_writes"] == 0
    assert reconciled["deleted_similarities"] == 1
    with database.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM event_similarities").fetchone()[0] == 0


def test_irreversible_merge_is_revalidated_inside_write_transaction(monkeypatch):
    from db import database
    from db.event_core import canonical_event_id
    from services import event_enrichment

    database.init_db()
    title = "预检后证据发生变化时必须在写事务内拒绝合并的事件标题"
    with database.get_connection() as conn:
        first = _seed_event(
            conn,
            "revalidate-first",
            title,
            "https://alpha.example/revalidate",
            1_000,
        )
        second = _seed_event(
            conn,
            "revalidate-second",
            title,
            "https://beta.example/revalidate",
            1_100,
        )
        conn.commit()

    original_graph_rows = event_enrichment._event_graph_rows
    calls = 0

    def graph_rows_with_late_contradiction(conn, cutoff, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            upsert_event_evidence(
                conn,
                second,
                source_uri="https://beta.example/revalidate-correction",
                claim_key="article:revalidate-correction",
                claim_text=title,
                excerpt="后续冻结证据显示项目已经终止。",
                snapshot_text=title + "\n后续冻结证据显示项目已经终止。",
                stance="contradicts",
                reliability=0.88,
                announcement_at=1_100,
                observed_at=1_200,
                now=1_200,
            )
        return original_graph_rows(conn, cutoff, **kwargs)

    monkeypatch.setattr(
        event_enrichment,
        "_event_graph_rows",
        graph_rows_with_late_contradiction,
    )

    result = resolve_event_graph(since=0)

    assert calls == 2
    assert result["qualified_merges"] == 1
    assert result["merged"] == 0
    assert result["skipped"] == 1
    with database.get_connection() as conn:
        assert canonical_event_id(conn, first) == first
        assert canonical_event_id(conn, second) == second
        assert conn.execute("SELECT COUNT(*) FROM event_merge_history").fetchone()[0] == 0


def test_canonical_group_evidence_blocks_seen_publisher_but_accepts_new_source():
    from db import database
    from db.event_core import canonical_event_id
    from services.event_enrichment import _event_graph_rows

    database.init_db()
    replay_title = "Canonical publisher replay identity headline"
    independent_title = "Canonical independent publisher identity headline"
    with database.get_connection() as conn:
        security_id = upsert_security(
            conn,
            exchange="SSE",
            ticker="600000",
            name="Canonical Group Test Security",
            security_id="SSE:600000",
            now=100,
        )
        replay_alpha = _seed_event(
            conn,
            "group-replay-alpha",
            replay_title,
            "https://alpha.example/replay",
            1_000,
            excerpt="shared replay account",
        )
        replay_beta = _seed_event(
            conn,
            "group-replay-beta",
            replay_title,
            "https://beta.example/replay",
            1_200,
            excerpt="shared replay account",
        )
        independent_alpha = _seed_event(
            conn,
            "group-independent-alpha",
            independent_title,
            "https://alpha.example/independent",
            1_000,
        )
        independent_beta = _seed_event(
            conn,
            "group-independent-beta",
            independent_title,
            "https://beta.example/independent",
            1_200,
        )
        # The member mapping remains immutable on beta after the merge and must
        # still participate in the canonical root's similarity evidence.
        upsert_event_security(
            conn,
            replay_beta,
            security_id,
            relation_type="direct",
            direction="neutral",
            confidence=0.9,
            relevance=8.0,
            rationale="canonical group aggregation regression",
            mapping_version="test-v1",
            now=1_300,
        )
        conn.commit()

    first_pass = resolve_event_graph(since=0)
    assert first_pass["merged"] == 2

    with database.get_connection() as conn:
        replay_root = canonical_event_id(conn, replay_alpha)
        assert canonical_event_id(conn, replay_beta) == replay_root
        independent_root = canonical_event_id(conn, independent_alpha)
        assert canonical_event_id(conn, independent_beta) == independent_root
        graph_rows = {
            str(row["event_id"]): row for row in _event_graph_rows(conn, 0)
        }
        assert graph_rows[replay_root]["sources"] == {
            "alpha.example",
            "beta.example",
        }
        assert len(graph_rows[replay_root]["snapshots"]) == 1
        assert graph_rows[replay_root]["mappings"] == {
            security_id: "direct"
        }

        repeated_beta = _seed_event(
            conn,
            "group-replay-beta-again",
            replay_title,
            "https://beta.example/replay-again",
            1_600,
            excerpt="shared replay account",
        )
        independent_gamma = _seed_event(
            conn,
            "group-independent-gamma",
            independent_title,
            "https://gamma.example/independent",
            1_600,
        )
        conn.commit()

    second_pass = resolve_event_graph(since=0)
    assert second_pass["merged"] == 1
    with database.get_connection() as conn:
        # beta is already represented in this canonical group, so title/time
        # equality cannot make another beta observation independent evidence.
        assert canonical_event_id(conn, repeated_beta) == repeated_beta
        # gamma is not represented by any canonical member and still qualifies.
        assert canonical_event_id(conn, independent_gamma) == independent_root


def test_event_enrichment_lanes_report_normal_results_and_parameter_bounds(
    monkeypatch,
):
    from services import event_enrichment

    calls = []

    def run_graph(**kwargs):
        calls.append(("graph", kwargs))
        return {"merged": 1}

    def run_mappings(**kwargs):
        calls.append(("mappings", kwargs))
        return {"mapped_evidence": 4}

    monkeypatch.setattr(event_enrichment, "resolve_event_graph", run_graph)
    monkeypatch.setattr(
        event_enrichment,
        "enrich_event_security_mappings",
        run_mappings,
    )

    result = event_enrichment.run_event_enrichment_lanes(
        since=123,
        mapping_limit=456,
        max_pairs=789,
    )

    assert calls == [
        ("graph", {"since": 123, "max_pairs": 789}),
        ("mappings", {"since": 123, "limit": 456}),
    ]
    assert result == {
        "graph": {"merged": 1},
        "mappings": {"mapped_evidence": 4},
        "status": "ok",
    }


def test_event_enrichment_lanes_reconcile_graph_after_mapping_output_change(
    monkeypatch,
):
    from services import event_enrichment

    calls = []

    def run_graph(**kwargs):
        calls.append(("graph", kwargs))
        return {"deleted_similarities": len(calls) - 1}

    def run_mappings(**kwargs):
        calls.append(("mappings", kwargs))
        return {"mapped_evidence": 1, "graph_dirty_groups": 1}

    monkeypatch.setattr(event_enrichment, "resolve_event_graph", run_graph)
    monkeypatch.setattr(
        event_enrichment,
        "enrich_event_security_mappings",
        run_mappings,
    )

    result = event_enrichment.run_event_enrichment_lanes(
        since=123,
        mapping_since=456,
        mapping_limit=7,
        max_pairs=8,
    )

    assert calls == [
        ("graph", {"since": 123, "max_pairs": 8}),
        ("mappings", {"since": 456, "limit": 7}),
        ("graph", {"since": 123, "max_pairs": 8}),
    ]
    assert result == {
        "graph": {"deleted_similarities": 0},
        "mappings": {"mapped_evidence": 1, "graph_dirty_groups": 1},
        "graph_after_mapping": {"deleted_similarities": 2},
        "status": "ok",
    }


def test_real_equity_mapping_is_bound_to_frozen_source_and_graph_is_unified():
    from db import database

    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            industry="半导体",
            concepts=["AI算力"],
            now=100,
        )
        duplicate_title = "寒武纪发布新一代人工智能算力芯片并披露交付计划"
        first = _seed_event(
            conn, "event-a", duplicate_title, "https://alpha.example/a", 1_000
        )
        second = _seed_event(
            conn, "event-b", duplicate_title, "https://beta.example/b", 1_600
        )
        third = _seed_event(
            conn,
            "event-c",
            "寒武纪新一代人工智能算力芯片开始客户交付",
            "https://gamma.example/c",
            2_000,
        )
        conn.commit()

    mapping_result = enrich_event_security_mappings(since=0)
    assert mapping_result["mapped_evidence"] == 3
    assert mapping_result["mapping_rows"] == 3

    with database.get_connection() as conn:
        mappings = conn.execute(
            "SELECT mapping_id, event_id, security_id, relation_type, direction, "
            "expected_gap, confidence, rationale FROM event_securities "
            "WHERE security_id = 'SSE:688256' AND is_current = 1 ORDER BY event_id"
        ).fetchall()
        assert len(mappings) == 3
        assert all(row[2] == "SSE:688256" for row in mappings)
        assert all(row[3] == "direct" for row in mappings)
        assert all(row[4] == "看多" for row in mappings)
        assert all(row[5] is None for row in mappings)
        assert all(row[6] >= 0.9 for row in mappings)
        for mapping in mappings:
            evidence = conn.execute(
                "SELECT source_uri, snapshot_text, reliability, mapping_revision "
                "FROM event_evidence WHERE mapping_id = ?",
                (mapping[0],),
            ).fetchone()
            assert evidence is not None
            assert evidence[0].startswith("https://")
            assert "寒武纪" in evidence[1]
            assert evidence[2] == 0.88
            assert evidence[3] == 1

    graph_result = resolve_event_graph(since=0)
    assert graph_result["merged"] == 1
    assert graph_result["similarities"] >= 1

    with database.get_connection() as conn:
        merged = conn.execute(
            "SELECT source_event_id, target_event_id, actor, reason "
            "FROM event_merge_history"
        ).fetchone()
        assert {merged[0], merged[1]} == {first, second}
        assert merged[2] == "event-resolver-v1"
        assert "独立来源" in merged[3]
        similarity = conn.execute(
            "SELECT common_securities, shared_paths, rationale "
            "FROM event_similarities WHERE event_id = ? OR similar_event_id = ?",
            (third, third),
        ).fetchone()
        assert json.loads(similarity[0]) == ["SSE:688256"]
        assert json.loads(similarity[1])[0]["security_id"] == "SSE:688256"
        assert "共同证券" in similarity[2]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_data_provider_attribution_does_not_create_direct_equity_mapping():
    from db import database

    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SZSE",
            ticker="300226",
            name="上海钢联",
            security_id="SZSE:300226",
            asset_type="equity",
            aliases=["上海钢联", "300226"],
            now=100,
        )
        provider_event = _seed_event(
            conn,
            "steel-data-provider",
            "上海钢联发布数据显示，今日MMLC电池级碳酸锂价格上涨",
            "https://wire.example/steel-data-provider",
            1_000,
            excerpt="电池级碳酸锂中间价较前一日上涨。",
        )
        legacy_mapping_id = upsert_event_security(
            conn,
            provider_event,
            "SZSE:300226",
            relation_type="direct",
            direction="看多",
            confidence=0.9,
            relevance=10.0,
            rationale="旧规则把数据发布者误认为事件主体。",
            mapping_version="asset-map:event-mapping-store-v4:legacy",
            now=1_100,
        )
        own_event = _seed_event(
            conn,
            "steel-own-results",
            "上海钢联发布半年度业绩报告，净利润同比增长",
            "https://wire.example/steel-own-results",
            2_000,
            excerpt="公司披露营业收入与净利润。",
        )
        conn.commit()

    enrich_event_security_mappings(include_legacy=True, since=0)

    with database.get_connection() as conn:
        provider_mappings = conn.execute(
            "SELECT mapping.mapping_id FROM event_securities AS mapping "
            "JOIN securities AS security "
            "ON security.security_id = mapping.security_id "
            "WHERE mapping.event_id = ? AND mapping.is_current = 1 "
            "AND mapping.relation_type = 'direct' "
            "AND security.name = '上海钢联'",
            (provider_event,),
        ).fetchall()
        own_mappings = conn.execute(
            "SELECT mapping.relation_type, security.name "
            "FROM event_securities AS mapping "
            "JOIN securities AS security "
            "ON security.security_id = mapping.security_id "
            "WHERE mapping.event_id = ? AND mapping.is_current = 1",
            (own_event,),
        ).fetchall()

    assert provider_mappings == []
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT is_current FROM event_securities WHERE mapping_id = ?",
            (legacy_mapping_id,),
        ).fetchone()[0] == 0
    assert [(row[0], row[1]) for row in own_mappings] == [
        ("direct", "上海钢联")
    ]


def test_broker_market_outlook_does_not_create_direct_equity_mapping():
    from db import database

    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="601377",
            name="兴业证券",
            security_id="SSE:601377",
            asset_type="equity",
            aliases=["兴业证券", "601377"],
            now=100,
        )
        outlook_event = _seed_event(
            conn,
            "broker-market-outlook",
            "A股收评：算力硬件板块上涨",
            "https://wire.example/broker-market-outlook",
            1_000,
            excerpt="兴业证券展望后市认为科技股波动有望缓解。",
        )
        own_event = _seed_event(
            conn,
            "broker-own-earnings",
            "兴业证券发布半年度业绩公告",
            "https://wire.example/broker-own-earnings",
            2_000,
            excerpt="公司披露营业收入与净利润。",
        )
        conn.commit()

    result = enrich_event_security_mappings(include_legacy=True, since=0)
    assert result["failed_evidence"] == 0
    with database.get_connection() as conn:
        mappings = conn.execute(
            "SELECT event_id, relation_type FROM event_securities "
            "WHERE security_id = 'SSE:601377' AND is_current = 1 "
            "ORDER BY event_id"
        ).fetchall()
    assert [(row[0], row[1]) for row in mappings] == [(own_event, "direct")]
    assert outlook_event != own_event


def test_mapping_replay_ledger_advances_oldest_equal_timestamp_backlog_once():
    from db import database

    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        for index in range(3):
            _seed_event(
                conn,
                f"mapping-ledger-{index}",
                f"寒武纪发布第{index + 1}代人工智能算力芯片",
                f"https://example.test/mapping-ledger-{index}",
                1_000,
            )
        conn.commit()

    first = enrich_event_security_mappings(since=0, limit=2)
    second = enrich_event_security_mappings(since=0, limit=2)
    third = enrich_event_security_mappings(since=0, limit=2)

    assert [
        first["processed_evidence"],
        second["processed_evidence"],
        third["processed_evidence"],
    ] == [2, 1, 0]
    assert f":{EVENT_MAPPING_CONTRACT_VERSION}:" in first["mapping_contract"]
    assert (
        f":manifest-{EVENT_MAPPING_SEMANTIC_FINGERPRINT}:"
        in first["mapping_contract"]
    )
    assert ":rules-" in first["mapping_contract"]
    assert len(first["mapping_contract"]) <= 255
    assert ":master-" in first["mapping_contract"]
    assert {
        first["mapping_contract"],
        second["mapping_contract"],
        third["mapping_contract"],
    } == {first["mapping_contract"]}
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM event_mapping_replay_ledger "
            "WHERE status = 'mapped'"
        ).fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(DISTINCT mapping_version) "
            "FROM event_mapping_replay_ledger"
        ).fetchone()[0] == 1
        revisions = conn.execute(
            "SELECT event_id, MAX(revision), SUM(is_current) FROM event_securities "
            "WHERE security_id = 'SSE:688256' GROUP BY event_id ORDER BY event_id"
        ).fetchall()
        assert len(revisions) == 3
        assert all((row[1], row[2]) == (1, 1) for row in revisions)


def test_mapping_replay_reuses_bounded_connections_per_batch(monkeypatch):
    from db import database
    from services import event_enrichment

    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        for index in range(3):
            _seed_event(
                conn,
                f"mapping-connection-reuse-{index}",
                f"寒武纪第{index + 1}批人工智能算力芯片进入交付",
                f"https://example.test/mapping-connection-reuse-{index}",
                1_000 + index,
            )
        conn.commit()

    connection_calls = []
    contract_calls = []
    original_get_connection = database.get_connection
    original_contract_version = event_enrichment._mapping_contract_version

    def counted_get_connection(**kwargs):
        connection_calls.append(dict(kwargs))
        return original_get_connection(**kwargs)

    def counted_contract_version(conn):
        contract_calls.append(conn)
        return original_contract_version(conn)

    monkeypatch.setattr(database, "get_connection", counted_get_connection)
    monkeypatch.setattr(
        event_enrichment,
        "_mapping_contract_version",
        counted_contract_version,
    )

    result = event_enrichment.enrich_event_security_mappings(since=0, limit=10)

    assert result["selected_groups"] == 3
    assert result["processed_evidence"] == 3
    # One connection owns selection plus every group transaction; the second
    # is the existing authoritative post-run migration-status read.
    assert len(connection_calls) == 2
    # Own commits do not advance data_version, so group boundaries do not
    # repeatedly hash the full security master. The second call is status.
    assert len(contract_calls) == 2


def test_mapping_replay_external_security_master_commit_still_fails_closed(
    monkeypatch,
):
    from db import database
    from services import event_enrichment

    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        for index in range(2):
            _seed_event(
                conn,
                f"mapping-external-master-{index}",
                f"寒武纪第{index + 1}批算力芯片进入交付",
                f"https://example.test/mapping-external-master-{index}",
                1_000 + index,
            )
        conn.commit()

    original_lock = database.db_lock
    original_contract_version = event_enrichment._mapping_contract_version
    contract_calls = []

    def counted_contract_version(conn):
        contract_calls.append(conn)
        return original_contract_version(conn)

    class ExternalMasterCommitLock:
        enters = 0

        def __enter__(self):
            self.enters += 1
            # Initial selection is enter 1 and group one is enter 2. Commit on
            # a separate SQLite handle before group two starts its transaction.
            if self.enters == 3:
                with database.get_connection() as writer:
                    upsert_security(
                        writer,
                        exchange="SSE",
                        ticker="688256",
                        name="寒武纪新名称",
                        security_id="SSE:688256",
                        asset_type="equity",
                        aliases=["寒武纪新名称", "688256"],
                        now=2_000,
                    )
                    writer.commit()
            original_lock.acquire()
            return self

        def __exit__(self, _exc_type, _exc, _traceback):
            original_lock.release()
            return False

    mutation_lock = ExternalMasterCommitLock()
    monkeypatch.setattr(database, "db_lock", mutation_lock)
    monkeypatch.setattr(
        event_enrichment,
        "_mapping_contract_version",
        counted_contract_version,
    )

    with pytest.raises(
        event_enrichment.MappingReplaySafetyError,
        match="mapping dependencies changed during replay selection",
    ):
        event_enrichment.enrich_event_security_mappings(since=0, limit=10)

    assert mutation_lock.enters == 3
    # Initial exact contract plus one re-check after data_version advances;
    # the unchanged first group performs no full security-master hash.
    assert len(contract_calls) == 2
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM event_mapping_replay_ledger "
            "WHERE status IN ('mapped', 'no_match')"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT name FROM securities WHERE security_id = 'SSE:688256'"
        ).fetchone()[0] == "寒武纪新名称"


def test_mapping_replay_root_limit_caps_dirty_and_pending_union():
    from db import database

    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        for index in range(3):
            event_id = _seed_event(
                conn,
                f"mapping-root-cap-{index}",
                f"寒武纪发布第{index + 1}代算力芯片",
                f"https://example.test/mapping-root-cap-{index}",
                1_000 + index,
            )
            upsert_event_security(
                conn,
                event_id,
                "SSE:688256",
                relation_type="direct",
                direction="看多",
                confidence=0.9,
                relevance=10.0,
                rationale="旧镜像原文直接点名寒武纪。",
                mapping_version="asset-map:old-writer",
                now=1_100 + index,
            )
        conn.commit()

    first = enrich_event_security_mappings(since=0, limit=2)
    second = enrich_event_security_mappings(since=0, limit=2)

    assert first["selected_groups"] == 2
    assert second["selected_groups"] == 1


def test_full_dirty_mapping_page_skips_all_history_pending_scan():
    from db import database
    from services import event_enrichment

    database.init_db()
    with database.get_connection() as conn:
        event_ids = [
            _seed_event(
                conn,
                f"mapping-dirty-page-{index}",
                f"第{index + 1}个映射脏组",
                f"https://example.test/mapping-dirty-page-{index}",
                1_000 + index,
            )
            for index in range(2)
        ]
        conn.executemany(
            "INSERT INTO event_mapping_replay_dirty_roots "
            "(root_event_id, reason, dirty_at) VALUES (?, 'test', ?) "
            "ON CONFLICT(root_event_id) DO UPDATE SET "
            "reason = excluded.reason, dirty_at = excluded.dirty_at",
            ((event_id, 1_000 + index) for index, event_id in enumerate(event_ids)),
        )
        contract, _active_equities = event_enrichment._mapping_contract_version(conn)
        conn.commit()

        statements = []
        conn.set_trace_callback(statements.append)
        roots, exhausted = event_enrichment._pending_mapping_roots(
            conn,
            cutoff=0,
            limit=2,
            mapping_contract=contract,
        )
        conn.set_trace_callback(None)

    assert roots == event_ids
    assert exhausted == set()
    assert not any(
        statement.startswith("WITH RECURSIVE event_roots")
        for statement in statements
    )


def test_new_mapping_contract_seeds_old_current_outside_recent_window_once():
    from db import database

    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        event_id = _seed_event(
            conn,
            "mapping-contract-outside-cutoff",
            "寒武纪发布新一代人工智能算力芯片",
            "https://example.test/mapping-contract-outside-cutoff",
            1_000,
        )
        old_mapping_id = upsert_event_security(
            conn,
            event_id,
            "SSE:688256",
            relation_type="direct",
            direction="看多",
            confidence=0.9,
            relevance=10.0,
            rationale="d359 旧 generation 映射。",
            mapping_version="asset-map:d359-dynamic-contract",
            now=1_100,
        )
        # Model an already-upgraded replay schema whose one-time schema seed
        # has run: the new contract must discover this old current output at
        # runtime instead of relying on the recent evidence cutoff.
        conn.execute("DELETE FROM event_mapping_replay_dirty_roots")
        conn.commit()

    first = enrich_event_security_mappings(include_legacy=True, since=10_000, limit=10)
    first_status = event_mapping_migration_status(include_legacy=True)
    with database.get_connection() as conn:
        first_ledger_rows = int(
            conn.execute(
                "SELECT COUNT(*) FROM event_mapping_replay_ledger"
            ).fetchone()[0]
        )
        first_mapping_rows = int(
            conn.execute(
                "SELECT COUNT(*) FROM event_securities WHERE event_id = ?",
                (event_id,),
            ).fetchone()[0]
        )
        assert conn.execute(
            "SELECT is_current FROM event_securities WHERE mapping_id = ?",
            (old_mapping_id,),
        ).fetchone()[0] == 0

    second = enrich_event_security_mappings(include_legacy=True, since=10_000, limit=10)
    second_status = event_mapping_migration_status(include_legacy=True)
    with database.get_connection() as conn:
        assert int(
            conn.execute(
                "SELECT COUNT(*) FROM event_mapping_replay_ledger"
            ).fetchone()[0]
        ) == first_ledger_rows
        assert int(
            conn.execute(
                "SELECT COUNT(*) FROM event_securities WHERE event_id = ?",
                (event_id,),
            ).fetchone()[0]
        ) == first_mapping_rows

    assert first["outdated_current_groups"] == 1
    assert first["migration_seeded_groups"] == 1
    assert first["selected_groups"] == first["processed_evidence"] == 1
    assert first_status["migration_complete"] is True
    assert second["outdated_current_groups"] == 0
    assert second["migration_seeded_groups"] == 0
    assert second["selected_groups"] == second["processed_evidence"] == 0
    assert second_status == first_status


def test_mapping_migration_ignores_non_asset_benchmark_proxy():
    from db import database

    database.init_db()
    with database.get_connection() as conn:
        benchmark_id = upsert_security(
            conn,
            exchange="SSE",
            ticker="000001",
            name="上证指数",
            security_id="SSE:000001",
            asset_type="index",
            aliases=["上证指数", "000001"],
            now=100,
        )
        event_id = resolve_or_create_event(
            conn,
            {"article_url_sha256": "legacy-benchmark-contract"},
            title="A股市场基准事件",
            event_type="大盘",
            direction="中性",
            priority=8.0,
            occurred_at=1_000,
            available_at=1_060,
            strategy_version="strategy-v1",
            model_version="model-v1",
            now=1_060,
        )
        mapping_id = upsert_event_security(
            conn,
            event_id,
            benchmark_id,
            relation_type="benchmark_proxy",
            direction="中性",
            confidence=0.8,
            relevance=8.0,
            rationale="历史回测基准代理，不属于事件规则映射。",
            mapping_version="93a-legacy-benchmark-contract",
            now=1_100,
        )
        conn.commit()

    result = enrich_event_security_mappings(since=10_000, limit=10)
    status = event_mapping_migration_status()

    assert result["migration_seeded_groups"] == 0
    assert result["selected_groups"] == 0
    assert status["old_current_mappings"] == 0
    assert status["dirty_groups"] == 0
    assert status["migration_complete"] is True
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT is_current FROM event_securities WHERE mapping_id = ?",
            (mapping_id,),
        ).fetchone()[0] == 1


def test_mapping_replay_keeps_evidence_budget_without_splitting_groups():
    from db import database
    from db.event_core import merge_events

    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        first_root = _seed_event(
            conn,
            "mapping-budget-first-root",
            "寒武纪第一组算力芯片进入交付",
            "https://alpha.example/mapping-budget-first",
            1_000,
        )
        first_child = _seed_event(
            conn,
            "mapping-budget-first-child",
            "寒武纪第一组算力芯片进入交付",
            "https://beta.example/mapping-budget-first",
            1_001,
        )
        merge_events(
            conn,
            first_child,
            first_root,
            reason="测试映射批次证据预算第一组",
            actor="test",
            now=1_100,
        )
        second_root = _seed_event(
            conn,
            "mapping-budget-second-root",
            "寒武纪第二组算力芯片进入交付",
            "https://alpha.example/mapping-budget-second",
            2_000,
        )
        second_child = _seed_event(
            conn,
            "mapping-budget-second-child",
            "寒武纪第二组算力芯片进入交付",
            "https://beta.example/mapping-budget-second",
            2_001,
        )
        merge_events(
            conn,
            second_child,
            second_root,
            reason="测试映射批次证据预算第二组",
            actor="test",
            now=2_100,
        )
        third_root = _seed_event(
            conn,
            "mapping-budget-third-root",
            "寒武纪第三组算力芯片进入交付",
            "https://alpha.example/mapping-budget-third",
            3_000,
        )
        third_child = _seed_event(
            conn,
            "mapping-budget-third-child",
            "寒武纪第三组算力芯片进入交付",
            "https://beta.example/mapping-budget-third",
            3_001,
        )
        merge_events(
            conn,
            third_child,
            third_root,
            reason="测试映射批次证据预算第三组",
            actor="test",
            now=3_100,
        )
        # Make the selected page deterministic regardless of whether this test
        # created the EventCore dirty triggers first or inherited them from an
        # earlier test in the process.
        conn.execute("DELETE FROM event_mapping_replay_dirty_roots")
        conn.executemany(
            "INSERT INTO event_mapping_replay_dirty_roots "
            "(root_event_id, reason, dirty_at) VALUES (?, 'test', ?)",
            (
                (first_root, 1_000),
                (second_root, 2_000),
                (third_root, 3_000),
            ),
        )
        conn.commit()

    first = enrich_event_security_mappings(since=0, limit=3)
    second = enrich_event_security_mappings(since=0, limit=3)
    third = enrich_event_security_mappings(since=0, limit=3)

    assert first["processed_evidence"] == 2
    assert first["deferred_groups"] == 2
    assert second["processed_evidence"] == 2
    assert second["deferred_groups"] == 1
    assert third["processed_evidence"] == 2
    assert third["deferred_groups"] == 0


def test_mapping_replay_rejects_unbounded_canonical_group(monkeypatch):
    from db import database
    from services import event_enrichment

    database.init_db()
    with database.get_connection() as conn:
        root_id = _seed_event(
            conn,
            "mapping-oversized-group",
            "超大规范组必须在写入前失败关闭",
            "https://example.test/mapping-oversized-group",
            1_000,
        )
        conn.commit()

    monkeypatch.setattr(
        event_enrichment,
        "canonical_event_group",
        lambda _conn, _root: (root_id,) + tuple(f"member-{i}" for i in range(500)),
    )

    with pytest.raises(event_enrichment.MappingReplaySafetyError, match="member bound"):
        enrich_event_security_mappings(since=0)


def test_dirty_group_without_base_evidence_is_retired_once_not_hot_looped():
    from db import database

    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        event_id = resolve_or_create_event(
            conn,
            {"article_url_sha256": "mapping-no-base-evidence"},
            title="寒武纪孤立旧映射",
            event_type="AI算力",
            direction="看多",
            priority=8.0,
            occurred_at=1_000,
            available_at=1_060,
            strategy_version="strategy-v1",
            model_version="model-v1",
            now=1_060,
        )
        mapping_id = upsert_event_security(
            conn,
            event_id,
            "SSE:688256",
            relation_type="direct",
            direction="看多",
            confidence=0.9,
            relevance=10.0,
            rationale="没有冻结证据的旧映射。",
            mapping_version="asset-map:orphan",
            now=1_100,
        )
        conn.commit()

    first = enrich_event_security_mappings(include_legacy=True, since=0)
    second = enrich_event_security_mappings(include_legacy=True, since=0)

    assert first["unreplayable_groups"] == 1
    assert first["retired_mappings"] == 1
    assert second["selected_groups"] == second["unreplayable_groups"] == 0
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT is_current FROM event_securities WHERE mapping_id = ?",
            (mapping_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM event_mapping_replay_dirty_roots"
        ).fetchone()[0] == 0


def test_schema_upgrade_seeds_existing_base_and_orphan_rule_outputs():
    from db import database
    from db.event_core import init_event_core_schema

    database.init_db()
    with database.get_connection() as conn:
        # Model a HEAD-like database: event core and old deterministic output
        # exist before any replay table or dirty trigger is installed.
        trigger_names = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND name LIKE 'event_core_mapping_replay_%'"
            ).fetchall()
        ]
        for trigger_name in trigger_names:
            conn.execute(f"DROP TRIGGER {trigger_name}")
        conn.execute("DROP TABLE event_mapping_replay_dirty_roots")
        conn.execute("DROP TABLE event_mapping_replay_ledger")

        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        base_event = _seed_event(
            conn,
            "upgrade-seed-base",
            "寒武纪发布新一代人工智能算力芯片",
            "https://example.test/upgrade-seed-base",
            1_000,
        )
        orphan_event = resolve_or_create_event(
            conn,
            {"article_url_sha256": "upgrade-seed-orphan"},
            title="寒武纪没有保留基础证据的旧映射",
            event_type="AI算力",
            direction="看多",
            priority=8.0,
            occurred_at=1_100,
            available_at=1_160,
            strategy_version="strategy-v1",
            model_version="model-v1",
            now=1_160,
        )
        base_old_mapping = upsert_event_security(
            conn,
            base_event,
            "SSE:688256",
            relation_type="direct",
            direction="看多",
            confidence=0.9,
            relevance=10.0,
            rationale="升级前旧映射。",
            mapping_version="asset-map:pre-ledger",
            now=1_200,
        )
        orphan_old_mapping = upsert_event_security(
            conn,
            orphan_event,
            "SSE:688256",
            relation_type="direct",
            direction="看多",
            confidence=0.9,
            relevance=10.0,
            rationale="升级前无基础证据旧映射。",
            mapping_version="asset-map:pre-ledger",
            now=1_200,
        )
        conn.commit()

        init_event_core_schema(conn)
        seeded = conn.execute(
            "SELECT root_event_id, reason FROM event_mapping_replay_dirty_roots "
            "ORDER BY root_event_id"
        ).fetchall()
        assert {tuple(row) for row in seeded} == {
            (base_event, "schema_upgrade_seed"),
            (orphan_event, "schema_upgrade_seed"),
        }
        conn.commit()

    replayed = enrich_event_security_mappings(include_legacy=True, since=10_000)
    stable = enrich_event_security_mappings(include_legacy=True, since=10_000)

    assert replayed["selected_groups"] == 2
    assert replayed["processed_evidence"] == 1
    assert replayed["unreplayable_groups"] == 1
    assert stable["selected_groups"] == 0
    database.init_db()
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT is_current FROM event_securities WHERE mapping_id = ?",
            (base_old_mapping,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT is_current FROM event_securities WHERE mapping_id = ?",
            (orphan_old_mapping,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM event_mapping_replay_dirty_roots"
        ).fetchone()[0] == 0


def test_partial_replay_schema_reseeds_existing_rule_output():
    from db import database
    from db.event_core import init_event_core_schema

    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        event_id = resolve_or_create_event(
            conn,
            {"article_url_sha256": "partial-replay-schema"},
            title="寒武纪部分迁移遗留映射",
            event_type="AI算力",
            direction="看多",
            priority=8.0,
            occurred_at=1_000,
            available_at=1_060,
            strategy_version="strategy-v1",
            model_version="model-v1",
            now=1_060,
        )
        upsert_event_security(
            conn,
            event_id,
            "SSE:688256",
            relation_type="direct",
            direction="看多",
            confidence=0.9,
            relevance=10.0,
            rationale="部分迁移前旧映射。",
            mapping_version="asset-map:partial-predecessor",
            now=1_100,
        )
        conn.execute("DELETE FROM event_mapping_replay_dirty_roots")
        conn.execute("DROP TABLE event_mapping_replay_ledger")
        conn.commit()

        init_event_core_schema(conn)

        assert tuple(
            conn.execute(
                "SELECT root_event_id, reason "
                "FROM event_mapping_replay_dirty_roots"
            ).fetchone()
        ) == (event_id, "schema_upgrade_seed")


def test_out_of_window_canonical_input_version_replays_from_dirty_queue():
    from db import database

    database.init_db()
    title = "寒武纪发布新一代人工智能算力芯片"
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        event_id = _seed_event(
            conn,
            "out-of-window-event-input",
            title,
            "https://example.test/out-of-window-event-input",
            1_000,
            direction="看多",
        )
        conn.commit()

    initial = enrich_event_security_mappings(since=0)
    with database.get_connection() as conn:
        resolve_or_create_event(
            conn,
            {"article_url_sha256": "out-of-window-event-input"},
            title=title,
            event_type="AI算力",
            direction="看空",
            priority=8.6,
            occurred_at=1_000,
            available_at=1_060,
            strategy_version="strategy-v1",
            model_version="model-v1",
            now=2_000,
        )
        conn.commit()
        assert tuple(
            conn.execute(
                "SELECT root_event_id, reason FROM event_mapping_replay_dirty_roots"
            ).fetchone()
        ) == (event_id, "input_version")

    replayed = enrich_event_security_mappings(since=10_000)
    stable = enrich_event_security_mappings(since=10_000)

    assert replayed["mapping_contract"] == initial["mapping_contract"]
    assert replayed["processed_evidence"] == 1
    assert stable["selected_groups"] == 0
    with database.get_connection() as conn:
        assert tuple(
            conn.execute(
                "SELECT direction, revision FROM event_securities "
                "WHERE event_id = ? AND security_id = 'SSE:688256' "
                "AND relation_type = 'direct' AND is_current = 1",
                (event_id,),
            ).fetchone()
        ) == ("看空", 2)


def test_out_of_window_base_evidence_update_replays_from_dirty_queue():
    from db import database

    database.init_db()
    source_uri = "https://example.test/out-of-window-base-input"
    initial_title = "某产业项目披露常规进展"
    initial_excerpt = "项目仍处于常规推进阶段。"
    frozen_snapshot = initial_title + "\n" + initial_excerpt
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        event_id = resolve_or_create_event(
            conn,
            {"article_url_sha256": "out-of-window-base-input"},
            title=initial_title,
            event_type="其他事件",
            direction="中性",
            priority=5.0,
            occurred_at=1_000,
            available_at=1_060,
            strategy_version="strategy-v1",
            model_version="model-v1",
            now=1_060,
        )
        upsert_event_evidence(
            conn,
            event_id,
            source_uri=source_uri,
            claim_key="article:out-of-window-base-input",
            claim_text=initial_title,
            excerpt=initial_excerpt,
            snapshot_text=frozen_snapshot,
            stance="supports",
            reliability=0.88,
            announcement_at=1_000,
            observed_at=1_060,
            now=1_060,
        )
        conn.commit()

    no_match = enrich_event_security_mappings(include_legacy=True, since=0)
    assert no_match["mapped_evidence"] == 0
    with database.get_connection() as conn:
        upsert_event_evidence(
            conn,
            event_id,
            source_uri=source_uri,
            claim_key="article:out-of-window-base-input",
            claim_text="寒武纪发布新一代人工智能算力芯片",
            excerpt="原文直接点名寒武纪。",
            snapshot_text=frozen_snapshot,
            stance="supports",
            reliability=0.88,
            announcement_at=1_000,
            observed_at=1_060,
            now=2_000,
        )
        conn.commit()
        assert tuple(
            conn.execute(
                "SELECT root_event_id, reason FROM event_mapping_replay_dirty_roots"
            ).fetchone()
        ) == (event_id, "base_evidence_update")

    replayed = enrich_event_security_mappings(include_legacy=True, since=10_000)
    stable = enrich_event_security_mappings(include_legacy=True, since=10_000)

    assert replayed["processed_evidence"] == replayed["mapped_evidence"] == 1
    assert stable["selected_groups"] == 0
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM event_securities WHERE event_id = ? "
            "AND security_id = 'SSE:688256' AND is_current = 1",
            (event_id,),
        ).fetchone()[0] == 1


def test_mapping_replay_uses_canonical_direction_and_does_not_churn_revisions():
    from db import database
    from db.event_core import merge_events

    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        root_id = _seed_event(
            conn,
            "mapping-direction-root",
            "寒武纪算力芯片订单进入交付阶段",
            "https://alpha.example/mapping-direction",
            1_000,
            direction="看空",
        )
        child_id = _seed_event(
            conn,
            "mapping-direction-child",
            "寒武纪算力芯片订单进入交付阶段",
            "https://beta.example/mapping-direction",
            1_100,
            direction="中性",
        )
        merge_events(
            conn,
            child_id,
            root_id,
            reason="测试 canonical group 方向收敛",
            actor="test",
            now=1_200,
        )
        conn.commit()

    first = enrich_event_security_mappings(since=0, limit=1)
    second = enrich_event_security_mappings(since=0)

    assert first["processed_evidence"] == 2
    assert second["processed_evidence"] == 0
    with database.get_connection() as conn:
        rows = conn.execute(
            "SELECT direction, revision, is_current, mapping_version "
            "FROM event_securities WHERE event_id = ? "
            "AND security_id = 'SSE:688256' ORDER BY revision",
            (root_id,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0:3] == ("看空", 1, 1)
        assert f":{EVENT_MAPPING_CONTRACT_VERSION}:" in str(rows[0][3])


def test_mapping_replay_repairs_output_drift_from_older_writer():
    from db import database

    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        event_id = _seed_event(
            conn,
            "mapping-output-drift",
            "寒武纪发布新一代人工智能算力芯片",
            "https://example.test/mapping-output-drift",
            1_000,
        )
        conn.commit()

    initial = enrich_event_security_mappings(since=0)
    contract = str(initial["mapping_contract"])
    with database.get_connection() as conn:
        upsert_event_security(
            conn,
            event_id,
            "SSE:688256",
            relation_type="direct",
            direction="看多",
            confidence=0.9,
            relevance=10.0,
            rationale="原文直接点名寒武纪。",
            mapping_version="asset-map:rollback-writer",
            now=2_000,
        )
        conn.commit()
        assert conn.execute(
            "SELECT mapping_version FROM event_securities "
            "WHERE event_id = ? AND security_id = 'SSE:688256' "
            "AND relation_type = 'direct' AND is_current = 1",
            (event_id,),
        ).fetchone()[0] == "asset-map:rollback-writer"

    repaired = enrich_event_security_mappings(since=0)
    stable = enrich_event_security_mappings(since=0)

    assert repaired["processed_evidence"] == 1
    assert stable["processed_evidence"] == 0
    with database.get_connection() as conn:
        current = conn.execute(
            "SELECT mapping_version, revision FROM event_securities "
            "WHERE event_id = ? AND security_id = 'SSE:688256' "
            "AND relation_type = 'direct' AND is_current = 1",
            (event_id,),
        ).fetchone()
        assert current[0] == contract
        assert current[1] == 3
        outputs = conn.execute(
            "SELECT COUNT(DISTINCT output_fingerprint) "
            "FROM event_mapping_replay_ledger WHERE evidence_id IN ("
            " SELECT evidence_id FROM event_evidence "
            " WHERE event_id = ? AND mapping_id IS NULL"
            ") AND mapping_version = ?",
            (event_id, contract),
        ).fetchone()[0]
        assert outputs == 1


def test_superseded_terminal_mapping_generation_does_not_churn():
    from db import database

    database.init_db()
    title = "寒武纪发布新一代人工智能算力芯片"
    excerpt = "寒武纪算力产品订单进入交付期。"
    source = "https://example.test/mapping-superseded-terminal"
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        event_id = _seed_event(
            conn,
            "mapping-superseded-terminal",
            title,
            source,
            1_000,
            excerpt=excerpt,
        )
        conn.commit()

    mapped = enrich_event_security_mappings(include_legacy=True, since=0)
    assert mapped["mapped_evidence"] == 1

    with database.get_connection() as conn:
        conn.execute(
            "UPDATE events SET event_type = '其他事件', direction = '中性', "
            "version = version + 1, updated_at = 2000 WHERE event_id = ?",
            (event_id,),
        )
        upsert_event_evidence(
            conn,
            event_id,
            source_uri=source,
            claim_key="article:mapping-superseded-terminal",
            claim_text="行业例行数据发布",
            excerpt="公开信息未点名任何上市公司。",
            snapshot_text=title + "\n" + excerpt,
            stance="supports",
            reliability=0.88,
            announcement_at=1_000,
            observed_at=2_000,
            now=2_000,
        )
        conn.commit()

    no_match = enrich_event_security_mappings(include_legacy=True, since=0)
    stable = enrich_event_security_mappings(include_legacy=True, since=0)

    assert no_match["processed_evidence"] == 1
    assert no_match["mapped_evidence"] == 0
    assert no_match["retired_mappings"] == 1
    assert stable["selected_groups"] == 0
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM event_securities WHERE event_id = ? "
            "AND is_current = 1",
            (event_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(DISTINCT input_fingerprint) "
            "FROM event_mapping_replay_ledger WHERE root_event_id = ?",
            (event_id,),
        ).fetchone()[0] == 2


def test_terminal_output_drift_repair_failure_fails_lane_and_stays_dirty(monkeypatch):
    from db import database
    from services import event_enrichment

    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        event_id = _seed_event(
            conn,
            "mapping-terminal-drift-failure",
            "寒武纪发布新一代人工智能算力芯片",
            "https://example.test/mapping-terminal-drift-failure",
            1_000,
        )
        conn.commit()

    initial = enrich_event_security_mappings(since=0)
    with database.get_connection() as conn:
        upsert_event_security(
            conn,
            event_id,
            "SSE:688256",
            relation_type="direct",
            direction="看多",
            confidence=0.9,
            relevance=10.0,
            rationale="回滚镜像制造的输出漂移。",
            mapping_version="asset-map:rollback-before-repair-failure",
            now=2_000,
        )
        conn.commit()

    monkeypatch.setattr(
        event_enrichment,
        "map_article_to_assets",
        lambda _article: (_ for _ in ()).throw(ValueError("sensitive input")),
    )

    with pytest.raises(
        event_enrichment.MappingReplaySafetyError,
        match="terminal mapping output drift repair failed",
    ):
        enrich_event_security_mappings(since=0)
    lane = event_enrichment.run_event_enrichment_lanes(
        since=0,
        mapping_limit=10,
        max_pairs=10,
    )

    assert lane["status"] == "degraded"
    assert lane["mappings"] == {
        "status": "failed",
        "failure_code": "security_mapping_failed",
        "error_type": "MappingReplaySafetyError",
    }
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM event_mapping_replay_dirty_roots "
            "WHERE root_event_id = ?",
            (event_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM event_mapping_replay_ledger "
            "WHERE mapping_version = ? AND status IN ('mapped', 'no_match')",
            (initial["mapping_contract"],),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM event_mapping_replay_ledger WHERE status = 'failed'"
        ).fetchone()[0] == 0


def test_mapping_replay_repairs_missing_terminal_output_after_rollback():
    from db import database

    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        event_id = _seed_event(
            conn,
            "mapping-output-missing",
            "寒武纪发布新一代人工智能算力芯片",
            "https://example.test/mapping-output-missing",
            1_000,
        )
        conn.commit()

    initial = enrich_event_security_mappings(since=0)
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE event_securities SET is_current = 0 WHERE event_id = ? "
            "AND security_id = 'SSE:688256' AND relation_type = 'direct' "
            "AND is_current = 1",
            (event_id,),
        )
        conn.commit()

    repaired = enrich_event_security_mappings(since=0)
    stable = enrich_event_security_mappings(since=0)

    assert repaired["processed_evidence"] == 1
    assert stable["processed_evidence"] == 0
    with database.get_connection() as conn:
        current = conn.execute(
            "SELECT mapping_version, revision FROM event_securities "
            "WHERE event_id = ? AND security_id = 'SSE:688256' "
            "AND relation_type = 'direct' AND is_current = 1",
            (event_id,),
        ).fetchone()
        assert tuple(current) == (initial["mapping_contract"], 2)


def test_mapping_replay_repairs_partial_same_contract_output_drift():
    from db import database

    database.init_db()
    with database.get_connection() as conn:
        for ticker, name in (("688256", "寒武纪"), ("603019", "中科曙光")):
            upsert_security(
                conn,
                exchange="SSE",
                ticker=ticker,
                name=name,
                security_id=f"SSE:{ticker}",
                asset_type="equity",
                aliases=[name, ticker],
                now=100,
            )
        event_id = _seed_event(
            conn,
            "mapping-partial-output",
            "寒武纪与中科曙光联合发布人工智能算力产品",
            "https://example.test/mapping-partial-output",
            1_000,
        )
        conn.commit()

    initial = enrich_event_security_mappings(since=0)
    contract = str(initial["mapping_contract"])
    with database.get_connection() as conn:
        current = conn.execute(
            "SELECT mapping_id FROM event_securities WHERE event_id = ? "
            "AND is_current = 1 AND mapping_version = ? ORDER BY security_id",
            (event_id, contract),
        ).fetchall()
        assert len(current) == 2
        conn.execute(
            "UPDATE event_securities SET is_current = 0 WHERE mapping_id = ?",
            (str(current[0][0]),),
        )
        conn.commit()

    repaired = enrich_event_security_mappings(since=0)
    stable = enrich_event_security_mappings(since=0)

    assert repaired["selected_groups"] == repaired["processed_evidence"] == 1
    assert stable["selected_groups"] == stable["processed_evidence"] == 0
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM event_securities WHERE event_id = ? "
            "AND is_current = 1 AND mapping_version = ?",
            (event_id, contract),
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM event_mapping_replay_dirty_roots"
        ).fetchone()[0] == 0


def test_security_master_generation_replays_terminal_no_match():
    from db import database

    database.init_db()
    with database.get_connection() as conn:
        event_id = _seed_event(
            conn,
            "mapping-late-security-master",
            "未来科技发布新一代产品",
            "https://example.test/mapping-late-security-master",
            1_000,
            excerpt="未来科技公司订单进入交付期。",
        )
        conn.commit()

    no_match = enrich_event_security_mappings(include_legacy=True, since=0)
    assert no_match["processed_evidence"] == 1
    assert no_match["mapped_evidence"] == 0

    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688999",
            name="未来科技",
            security_id="SSE:688999",
            asset_type="equity",
            aliases=["未来科技", "688999"],
            now=2_000,
        )
        conn.commit()

    replayed = enrich_event_security_mappings(include_legacy=True, since=0)

    assert replayed["mapping_contract"] != no_match["mapping_contract"]
    assert replayed["processed_evidence"] == replayed["mapped_evidence"] == 1
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM event_securities WHERE event_id = ? "
            "AND security_id = 'SSE:688999' AND is_current = 1",
            (event_id,),
        ).fetchone()[0] == 1


def test_security_master_name_cache_invalidates_on_version_sum_change():
    from db import database

    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688001",
            name="旧名称科技",
            security_id="SSE:688001",
            asset_type="equity",
            aliases=["旧名称科技", "688001"],
            now=100,
        )
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688002",
            name="时间锚科技",
            security_id="SSE:688002",
            asset_type="equity",
            aliases=["时间锚科技", "688002"],
            now=1_000,
        )
        conn.commit()
        assert {row[1] for row in database._equity_name_rows(conn)} == {
            "旧名称科技",
            "时间锚科技",
        }

        # COUNT and MAX(updated_at) remain unchanged; SUM(version) is what must
        # invalidate the five-minute in-process resolver cache.
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688001",
            name="新名称科技",
            security_id="SSE:688001",
            asset_type="equity",
            aliases=["新名称科技", "688001"],
            now=200,
        )
        conn.commit()
        names = {row[1] for row in database._equity_name_rows(conn)}

    assert "新名称科技" in names
    assert "旧名称科技" not in names


def test_same_name_security_resolution_is_stable_by_security_id_not_refresh_time():
    from db import database

    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="600002",
            name="同名科技",
            security_id="SSE:600002",
            now=100,
        )
        upsert_security(
            conn,
            exchange="SSE",
            ticker="600001",
            name="同名科技",
            security_id="SSE:600001",
            now=200,
        )
        first = database._candidate_security_row(conn, {"name": "同名科技"})
        assert str(first[0]) == "SSE:600001"

        # Refresh only the other row's non-resolver metadata at a later time.
        upsert_security(
            conn,
            exchange="SSE",
            ticker="600002",
            name="同名科技",
            security_id="SSE:600002",
            aliases=["同名科技", "新别名"],
            industry="新行业标签",
            now=9_999,
        )
        second = database._candidate_security_row(conn, {"name": "同名科技"})

    assert str(second[0]) == "SSE:600001"


def test_non_resolver_security_metadata_does_not_replay_or_advance_revision():
    from db import database

    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            aliases=["寒武纪", "688256"],
            industry="半导体",
            concepts=["AI算力"],
            now=100,
        )
        event_id = _seed_event(
            conn,
            "mapping-master-metadata-only",
            "寒武纪发布新一代人工智能算力芯片",
            "https://example.test/mapping-master-metadata-only",
            1_000,
        )
        conn.commit()

    initial = enrich_event_security_mappings(since=0)
    with database.get_connection() as conn:
        before = conn.execute(
            "SELECT mapping_id, revision FROM event_securities "
            "WHERE event_id = ? AND security_id = 'SSE:688256' AND is_current = 1",
            (event_id,),
        ).fetchone()
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            aliases=["寒武纪", "688256", "寒武纪-U"],
            industry="半导体设备",
            concepts=["AI算力", "国产替代"],
            now=2_000,
        )
        conn.commit()

    stable = enrich_event_security_mappings(since=0)

    assert stable["mapping_contract"] == initial["mapping_contract"]
    assert stable["processed_evidence"] == 0
    with database.get_connection() as conn:
        after = conn.execute(
            "SELECT mapping_id, revision FROM event_securities "
            "WHERE event_id = ? AND security_id = 'SSE:688256' AND is_current = 1",
            (event_id,),
        ).fetchone()
    assert tuple(after) == tuple(before)


def test_empty_security_master_fails_closed_before_retirement():
    from db import database
    from services.event_enrichment import MappingReplaySafetyError

    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688999",
            name="未来科技",
            security_id="SSE:688999",
            asset_type="equity",
            aliases=["未来科技", "688999"],
            now=100,
        )
        event_id = _seed_event(
            conn,
            "mapping-empty-master",
            "未来科技发布新一代产品",
            "https://example.test/mapping-empty-master",
            1_000,
        )
        mapping_id = upsert_event_security(
            conn,
            event_id,
            "SSE:688999",
            relation_type="direct",
            direction="看多",
            confidence=0.9,
            relevance=10.0,
            rationale="原文直接点名未来科技。",
            mapping_version="asset-map:previous-image",
            now=1_100,
        )
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688999",
            name="未来科技",
            security_id="SSE:688999",
            asset_type="equity",
            aliases=["未来科技", "688999"],
            is_active=False,
            now=1_200,
        )
        conn.commit()

    with pytest.raises(MappingReplaySafetyError, match="security master is empty"):
        enrich_event_security_mappings(since=0)

    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT is_current FROM event_securities WHERE mapping_id = ?",
            (mapping_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM event_mapping_replay_ledger"
        ).fetchone()[0] == 0


def test_mapping_replay_retires_child_outputs_after_pre_mapped_group_merge():
    from db import database
    from db.event_core import merge_events

    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        root_id = _seed_event(
            conn,
            "mapping-premerge-root",
            "寒武纪发布人工智能算力芯片",
            "https://alpha.example/mapping-premerge",
            1_000,
        )
        child_id = _seed_event(
            conn,
            "mapping-premerge-child",
            "寒武纪发布人工智能算力芯片",
            "https://beta.example/mapping-premerge",
            1_100,
        )
        for event_id in (root_id, child_id):
            upsert_event_security(
                conn,
                event_id,
                "SSE:688256",
                relation_type="direct",
                direction="看多",
                confidence=0.9,
                relevance=10.0,
                rationale="旧镜像原文直接点名寒武纪。",
                mapping_version="asset-map:premerge-image",
                now=1_150,
            )
        merge_events(
            conn,
            child_id,
            root_id,
            reason="测试预映射事件合并后的整组收敛",
            actor="test",
            now=1_200,
        )
        conn.commit()

    replayed = enrich_event_security_mappings(include_legacy=True, since=10_000)
    stable = enrich_event_security_mappings(include_legacy=True, since=10_000)

    assert replayed["selected_groups"] == 1
    assert replayed["processed_evidence"] == 2
    assert stable["selected_groups"] == 0
    with database.get_connection() as conn:
        current = conn.execute(
            "SELECT event_id, mapping_version FROM event_securities "
            "WHERE event_id IN (?, ?) AND is_current = 1 "
            "AND lower(relation_type) <> 'benchmark_proxy'",
            (root_id, child_id),
        ).fetchall()
        assert len(current) == 1
        assert tuple(current[0]) == (root_id, replayed["mapping_contract"])
        assert conn.execute(
            "SELECT COUNT(*) FROM event_mapping_replay_dirty_roots"
        ).fetchone()[0] == 0


def test_mapping_replay_reacts_to_canonical_input_version_change():
    from db import database

    database.init_db()
    title = "寒武纪发布新一代人工智能算力芯片"
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        event_id = _seed_event(
            conn,
            "mapping-input-change",
            title,
            "https://example.test/mapping-input-change",
            1_000,
            direction="看多",
        )
        conn.commit()

    initial = enrich_event_security_mappings(since=0)
    with database.get_connection() as conn:
        resolved = resolve_or_create_event(
            conn,
            {"article_url_sha256": "mapping-input-change"},
            title=title,
            event_type="AI算力",
            direction="看空",
            priority=8.6,
            occurred_at=1_000,
            available_at=1_060,
            strategy_version="strategy-v1",
            model_version="model-v1",
            now=2_000,
        )
        conn.commit()
        assert resolved == event_id

    replayed = enrich_event_security_mappings(since=0)
    stable = enrich_event_security_mappings(since=0)

    assert initial["processed_evidence"] == replayed["processed_evidence"] == 1
    assert stable["processed_evidence"] == 0
    with database.get_connection() as conn:
        current = conn.execute(
            "SELECT direction, revision FROM event_securities "
            "WHERE event_id = ? AND security_id = 'SSE:688256' "
            "AND relation_type = 'direct' AND is_current = 1",
            (event_id,),
        ).fetchone()
        assert tuple(current) == ("看空", 2)
        assert conn.execute(
            "SELECT COUNT(DISTINCT input_fingerprint) "
            "FROM event_mapping_replay_ledger WHERE mapping_version = ?",
            (initial["mapping_contract"],),
        ).fetchone()[0] == 2


def test_mapping_replay_failures_are_bounded_and_lane_reports_degraded(monkeypatch):
    from db import database
    from services import event_enrichment

    database.init_db()
    with database.get_connection() as conn:
        _seed_event(
            conn,
            "mapping-bounded-failure",
            "确定性映射输入触发受控失败",
            "https://example.test/mapping-bounded-failure",
            1_000,
        )
        conn.commit()

    monkeypatch.setattr(
        event_enrichment,
        "map_article_to_assets",
        lambda _article: (_ for _ in ()).throw(ValueError("sensitive input")),
    )
    attempts = [
        enrich_event_security_mappings(since=0),
        enrich_event_security_mappings(since=0),
        enrich_event_security_mappings(since=0),
    ]
    exhausted = enrich_event_security_mappings(since=0)
    lane = event_enrichment.run_event_enrichment_lanes(
        since=0,
        mapping_limit=10,
        max_pairs=10,
    )

    assert [item["failed_evidence"] for item in attempts] == [1, 1, 1]
    assert exhausted["selected_groups"] == 0
    assert exhausted["exhausted_groups"] == 1
    assert lane["status"] == "degraded"
    assert lane["mappings"]["exhausted_groups"] == 1
    with database.get_connection() as conn:
        failure = conn.execute(
            "SELECT status, attempts, failure_code FROM event_mapping_replay_ledger"
        ).fetchone()
        assert tuple(failure) == ("failed", 3, "ValueError")


def test_mapping_replay_persistent_integrity_error_is_bounded(monkeypatch):
    import sqlite3

    from db import database
    from services import event_enrichment

    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id="SSE:688256",
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=100,
        )
        event_id = _seed_event(
            conn,
            "mapping-bounded-integrity-failure",
            "寒武纪持久化约束错误必须进入有界重试账本",
            "https://example.test/mapping-bounded-integrity-failure",
            1_000,
        )
        upsert_event_security(
            conn,
            event_id,
            "SSE:688256",
            relation_type="direct",
            direction="看多",
            confidence=0.9,
            relevance=10.0,
            rationale="旧镜像原文直接点名寒武纪。",
            mapping_version="asset-map:integrity-old-writer",
            now=1_100,
        )
        conn.commit()

    monkeypatch.setattr(
        event_enrichment.database,
        "upsert_real_event_mappings",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.IntegrityError("sensitive constraint detail")
        ),
    )
    attempts = [enrich_event_security_mappings(since=0) for _ in range(3)]
    exhausted = enrich_event_security_mappings(since=0)
    stable = enrich_event_security_mappings(since=0)

    assert [item["failed_evidence"] for item in attempts] == [1, 1, 1]
    assert exhausted["selected_groups"] == 0
    assert exhausted["exhausted_groups"] == 1
    assert stable["selected_groups"] == 0
    with database.get_connection() as conn:
        assert tuple(
            conn.execute(
                "SELECT status, attempts, failure_code "
                "FROM event_mapping_replay_ledger"
            ).fetchone()
        ) == ("failed", 3, "IntegrityError")


def test_out_of_window_exhausted_mapping_failure_remains_degraded(monkeypatch):
    from db import database
    from services import event_enrichment

    database.init_db()
    with database.get_connection() as conn:
        _seed_event(
            conn,
            "mapping-out-of-window-exhausted",
            "历史映射输入的确定性失败必须持续告警",
            "https://example.test/mapping-out-of-window-exhausted",
            1_000,
        )
        conn.commit()

    monkeypatch.setattr(
        event_enrichment,
        "map_article_to_assets",
        lambda _article: (_ for _ in ()).throw(ValueError("sensitive input")),
    )
    attempts = [
        enrich_event_security_mappings(since=10_000) for _ in range(3)
    ]
    stable = enrich_event_security_mappings(since=10_000)
    lane = event_enrichment.run_event_enrichment_lanes(
        since=10_000,
        mapping_limit=10,
        max_pairs=10,
    )

    assert [item["failed_evidence"] for item in attempts] == [1, 1, 1]
    assert attempts[-1]["exhausted_groups"] == 1
    assert stable["selected_groups"] == 0
    assert stable["exhausted_groups"] == 1
    assert lane["status"] == "degraded"
    assert lane["mappings"]["exhausted_groups"] == 1


def test_complete_replay_retires_obsolete_fossil_mappings_for_clean_energy_event():
    from db import database

    database.init_db()
    with database.get_connection() as conn:
        securities = {}
        for exchange, ticker, name in (
            ("SZSE", "002202", "金风科技"),
            ("SZSE", "300772", "运达股份"),
            ("SSE", "601615", "明阳智能"),
            ("SZSE", "002493", "荣盛石化"),
            ("SSE", "601225", "陕西煤业"),
            ("SSE", "600938", "中国海油"),
        ):
            securities[name] = upsert_security(
                conn,
                exchange=exchange,
                ticker=ticker,
                name=name,
                asset_type="equity",
                aliases=[name, ticker],
                now=100,
            )
        event_id = resolve_or_create_event(
            conn,
            {"article_url_sha256": "clean-energy-report"},
            title="《2026能源产业生态报告》发布，风电光伏装机超越火电",
            event_type="能源政策",
            direction="中性",
            priority=5,
            occurred_at=1_000,
            available_at=1_060,
            strategy_version="strategy-v1",
            model_version="model-v1",
            now=1_060,
        )
        upsert_event_evidence(
            conn,
            event_id,
            source_uri="https://example.test/clean-energy",
            claim_key="article:clean-energy-report",
            claim_text="《2026能源产业生态报告》发布，风电光伏装机超越火电",
            excerpt="核能、生物质能领域领跑，新能源项目投资增长。",
            stance="supports",
            reliability=0.9,
            announcement_at=1_000,
            observed_at=1_060,
            now=1_060,
        )
        for name in ("荣盛石化", "陕西煤业", "中国海油"):
            upsert_event_security(
                conn,
                event_id,
                securities[name],
                relation_type="concept",
                direction="中性",
                confidence=0.66,
                relevance=6.6,
                rationale="旧版能源通胀规则",
                mapping_version="asset-map:obsolete",
                now=1_061,
            )
        conn.commit()

    result = enrich_event_security_mappings(include_legacy=True, since=0)

    assert result["retired_mappings"] == 3
    with database.get_connection() as conn:
        current = conn.execute(
            "SELECT security.name, mapping.direction FROM event_securities mapping "
            "JOIN securities security ON security.security_id = mapping.security_id "
            "WHERE mapping.event_id = ? AND mapping.is_current = 1 "
            "ORDER BY mapping.relevance DESC, security.name",
            (event_id,),
        ).fetchall()
        assert [row[0] for row in current[:3]] == [
            "金风科技", "运达股份", "明阳智能",
        ]
        assert all(row[1] == "看多" for row in current)
        assert not {row[0] for row in current} & {"荣盛石化", "陕西煤业", "中国海油"}


def test_explicit_all_history_runner_is_separate_and_rebuilds_graph(
    monkeypatch,
    capsys,
):
    import sys

    import scripts.run_event_enrichment as runner

    calls = []
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_event_enrichment.py",
            "--mapping-all-history",
            "--limit",
            "17",
            "--max-pairs",
            "23",
        ],
    )
    monkeypatch.setattr(runner.database, "init_db", lambda: None)

    def run_lanes(**kwargs):
        calls.append(kwargs)
        return {
            "status": "ok",
            "graph": {},
            "mappings": {
                "deferred_groups": 0,
                "failed_evidence": 0,
                "unreplayable_groups": 0,
                "exhausted_groups": 0,
            },
        }

    monkeypatch.setattr(runner, "run_event_enrichment_lanes", run_lanes)
    monkeypatch.setattr(
        runner,
        "event_mapping_migration_status",
        lambda: {
            "old_current_mappings": 0,
            "pending_evidence": 0,
            "dirty_groups": 0,
            "exhausted_groups": 0,
            "migration_complete": True,
        },
    )

    assert runner.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert calls == [
        {
            "since": 0,
            "mapping_since": 0,
            "mapping_limit": 17,
            "max_pairs": 23,
            "rebuild_graph_after_mapping": True,
            "mapping_include_legacy": True,
        }
    ]
    assert payload["mapping_scope"] == "all_history"
    assert payload["migration_complete"] is True


def test_runner_records_shadow_degradation_without_changing_core_exit(
    monkeypatch,
    capsys,
):
    import sys

    import scripts.run_event_enrichment as runner

    order = []
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_event_enrichment.py", "--limit", "17", "--provenance-limit", "19"],
    )
    monkeypatch.setattr(runner.database, "init_db", lambda: None)
    monkeypatch.setattr(
        runner,
        "run_event_enrichment_lanes",
        lambda **_kwargs: order.append("core")
        or {"status": "ok", "graph": {}, "mappings": {}},
    )
    monkeypatch.setattr(
        runner,
        "_run_provenance_shadow",
        lambda **kwargs: order.append(("shadow", kwargs))
        or {
            "status": "degraded",
            "failure_code": "provenance_shadow_projection_failed",
            "error_type": "RuntimeError",
        },
    )

    assert runner.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert order == [
        "core",
        (
            "shadow",
            {
                "evidence_scan_limit": 200,
                "max_roots": 200,
                "max_total_evidence": 19,
            },
        ),
    ]
    assert payload["status"] == "ok"
    assert payload["provenance_shadow"] == {
        "status": "degraded",
        "failure_code": "provenance_shadow_projection_failed",
        "error_type": "RuntimeError",
    }


def test_shadow_dependency_import_error_is_non_blocking_and_sanitized(monkeypatch):
    import builtins

    import scripts.run_event_enrichment as runner

    original_import = builtins.__import__

    def fail_shadow_dependency(name, *args, **kwargs):
        if name == "services.provenance_shadow":
            raise ImportError("sensitive dependency detail")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_shadow_dependency)
    monkeypatch.setattr(
        runner.database,
        "get_connection",
        lambda **_kwargs: pytest.fail(
            "database must not open after an import failure"
        ),
    )

    result = runner._run_provenance_shadow(
        evidence_scan_limit=17,
        max_roots=17,
        max_total_evidence=17,
    )

    assert result == {
        "status": "degraded",
        "failure_code": "provenance_shadow_import_failed",
        "error_type": "ImportError",
    }
    assert "sensitive" not in json.dumps(result)


def test_shadow_adapter_separates_scan_and_projection_bounds(monkeypatch):
    from contextlib import nullcontext

    import scripts.run_event_enrichment as runner
    import services.provenance_shadow as shadow_service

    connection = object()
    calls = []
    monkeypatch.setattr(
        runner,
        "load_config",
        lambda: {"source_provenance": {"rules": []}},
    )
    monkeypatch.setattr(
        runner.database,
        "get_connection",
        lambda **kwargs: calls.append(("connection", kwargs))
        or nullcontext(connection),
    )
    monkeypatch.setattr(
        shadow_service,
        "project_provenance_shadow",
        lambda conn, **kwargs: calls.append((conn, kwargs))
        or {
            "status": "ok",
            "complete": False,
            "has_more": True,
            "processed_evidence": 200,
        },
    )

    result = runner._run_provenance_shadow(
        evidence_scan_limit=200,
        max_roots=25,
        max_total_evidence=2_000,
    )

    assert calls == [
        ("connection", {"configure_storage": True}),
        (
            connection,
            {
                "config": {"source_provenance": {"rules": []}},
                "evidence_scan_limit": 200,
                "max_roots": 25,
                "max_total_evidence": 2_000,
            },
        )
    ]
    assert result == {
        "status": "ok",
        "complete": False,
        "has_more": True,
        "processed_evidence": 200,
    }


def test_shadow_adapter_degrades_on_invalid_top_level_config(monkeypatch):
    from contextlib import nullcontext

    import scripts.run_event_enrichment as runner

    monkeypatch.setattr(
        runner,
        "load_config",
        lambda: {"source_provenance": ["invalid"]},
    )
    monkeypatch.setattr(
        runner.database,
        "get_connection",
        lambda **_kwargs: nullcontext(object()),
    )

    result = runner._run_provenance_shadow(
        evidence_scan_limit=200,
        max_roots=25,
        max_total_evidence=2_000,
    )

    assert result == {
        "status": "degraded",
        "failure_code": "provenance_shadow_projection_failed",
        "error_type": "ProvenanceShadowConfigError",
    }


def test_incomplete_shadow_batch_does_not_change_successful_core_exit(
    monkeypatch, capsys
):
    import sys

    import scripts.run_event_enrichment as runner

    calls = []
    monkeypatch.setattr(sys, "argv", ["run_event_enrichment.py"])
    monkeypatch.setattr(runner.database, "init_db", lambda: None)
    monkeypatch.setattr(
        runner,
        "run_event_enrichment_lanes",
        lambda **_kwargs: {"status": "ok", "graph": {}, "mappings": {}},
    )
    monkeypatch.setattr(
        runner,
        "_run_provenance_shadow",
        lambda **kwargs: calls.append(kwargs)
        or {"status": "ok", "complete": False, "has_more": True},
    )

    assert runner.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert calls == [
        {
            "evidence_scan_limit": 200,
            "max_roots": 200,
            "max_total_evidence": 2_000,
        }
    ]
    assert payload["provenance_shadow"] == {
        "status": "ok",
        "complete": False,
        "has_more": True,
    }


def test_shadow_success_cannot_mask_a_degraded_core_exit(monkeypatch, capsys):
    import sys

    import scripts.run_event_enrichment as runner

    monkeypatch.setattr(sys, "argv", ["run_event_enrichment.py"])
    monkeypatch.setattr(runner.database, "init_db", lambda: None)
    monkeypatch.setattr(
        runner,
        "run_event_enrichment_lanes",
        lambda **_kwargs: {
            "status": "degraded",
            "graph": {"status": "failed"},
            "mappings": {},
        },
    )
    monkeypatch.setattr(
        runner,
        "_run_provenance_shadow",
        lambda **_kwargs: {"status": "ok", "projected": 3},
    )

    assert runner.main() == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "degraded"
    assert payload["provenance_shadow"] == {"status": "ok", "projected": 3}


@pytest.mark.parametrize(
    ("shadow_status", "complete", "expected_exit"),
    [
        ("ok", True, 0),
        ("ok", False, 1),
        ("pending", False, 1),
        ("degraded", False, 1),
    ],
)
def test_standalone_shadow_runner_is_strict_for_acceptance(
    monkeypatch, capsys, shadow_status, complete, expected_exit
):
    import sys

    import scripts.run_provenance_shadow as shadow_runner

    calls = []
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_provenance_shadow.py", "--limit", "23"],
    )
    monkeypatch.setattr(
        shadow_runner,
        "_run_provenance_shadow",
        lambda **kwargs: calls.append(kwargs)
        or {"status": shadow_status, "complete": complete, "projected": 5},
    )

    assert shadow_runner.main() == expected_exit
    payload = json.loads(capsys.readouterr().out)

    assert calls == [
        {
            "evidence_scan_limit": 23,
            "max_roots": 23,
            "max_total_evidence": 23,
            "configure_storage": False,
        }
    ]
    assert payload == {
        "complete": complete,
        "projected": 5,
        "status": shadow_status,
    }


def test_standalone_shadow_runner_uses_safe_independent_default(monkeypatch, capsys):
    import sys

    import scripts.run_provenance_shadow as shadow_runner

    calls = []
    monkeypatch.setattr(sys, "argv", ["run_provenance_shadow.py"])
    monkeypatch.setattr(
        shadow_runner,
        "_run_provenance_shadow",
        lambda **kwargs: calls.append(kwargs)
        or {"status": "ok", "complete": True, "has_more": False},
    )

    assert shadow_runner.main() == 0
    assert calls == [
        {
            "evidence_scan_limit": 2_000,
            "max_roots": 1_000,
            "max_total_evidence": 2_000,
            "configure_storage": False,
        }
    ]
    assert json.loads(capsys.readouterr().out)["complete"] is True


def _core_table_content_snapshot(database):
    import hashlib

    from db.provenance_shadow import PROVENANCE_SHADOW_SCHEMA_META_KEY

    excluded_tables = {
        "event_evidence_provenance_shadow",
        "event_claim_state_shadow",
    }
    snapshot = {}
    with database.get_connection(configure_storage=False) as conn:
        table_names = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            if str(row[0]) not in excluded_tables
        ]
        for table in table_names:
            quoted_table = '"' + table.replace('"', '""') + '"'
            columns = [
                str(row[1])
                for row in conn.execute(f"PRAGMA table_info({quoted_table})")
            ]
            quoted_columns = [
                '"' + column.replace('"', '""') + '"' for column in columns
            ]
            where = ""
            params = ()
            if table == "app_meta":
                where = " WHERE k <> ?"
                params = (PROVENANCE_SHADOW_SCHEMA_META_KEY,)
            order = " ORDER BY " + ", ".join(quoted_columns)
            rows = conn.execute(
                f"SELECT * FROM {quoted_table}{where}{order}", params
            ).fetchall()
            digest = hashlib.sha256()
            for row in rows:
                digest.update(repr(tuple(row)).encode("utf-8", "backslashreplace"))
                digest.update(b"\n")
            snapshot[table] = (len(rows), digest.hexdigest())
    return snapshot


def test_standalone_shadow_runner_preserves_core_content_and_delete_journal(
    monkeypatch, capsys
):
    import sys

    from db import database
    import scripts.run_provenance_shadow as shadow_runner

    database.init_db()
    with database.get_connection() as conn:
        _seed_event(
            conn,
            "standalone-shadow-invariant",
            "影子回填不得改变核心账本",
            "https://example.test/shadow-invariant",
            1_000,
        )
        conn.commit()
    with database.get_connection(configure_storage=False) as conn:
        assert str(conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower() == (
            "delete"
        )

    before = _core_table_content_snapshot(database)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_provenance_shadow.py", "--limit", "2000"],
    )

    assert shadow_runner.main() == 0
    payload = json.loads(capsys.readouterr().out)
    after = _core_table_content_snapshot(database)

    assert payload["status"] == "ok"
    assert payload["complete"] is True
    assert after == before
    with database.get_connection(configure_storage=False) as conn:
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == (
            "delete"
        )


def test_standalone_shadow_runner_fails_strictly_without_core_schema(
    monkeypatch, capsys
):
    import sqlite3
    import sys

    from db import database
    import scripts.run_provenance_shadow as shadow_runner

    monkeypatch.setattr(sys, "argv", ["run_provenance_shadow.py"])

    assert shadow_runner.main() == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload == {
        "status": "degraded",
        "failure_code": "provenance_shadow_projection_failed",
        "error_type": "ProvenanceShadowSchemaError",
    }
    with sqlite3.connect(database.DB_PATH) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0] == 0
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == (
            "delete"
        )


@pytest.mark.parametrize(
    "blocking_field",
    [
        "old_current_mappings",
        "pending_evidence",
        "dirty_groups",
        "exhausted_groups",
    ],
)
def test_all_history_runner_exits_nonzero_until_mapping_migration_is_complete(
    monkeypatch,
    capsys,
    blocking_field,
):
    import sys

    import scripts.run_event_enrichment as runner

    monkeypatch.setattr(
        sys,
        "argv",
        ["run_event_enrichment.py", "--mapping-all-history"],
    )
    monkeypatch.setattr(runner.database, "init_db", lambda: None)
    monkeypatch.setattr(
        runner,
        "run_event_enrichment_lanes",
        lambda **_kwargs: {
            "status": "ok",
            "graph": {},
            "mappings": {
                "deferred_groups": 0,
                "failed_evidence": 0,
                "unreplayable_groups": 0,
                "exhausted_groups": 0,
            },
        },
    )
    migration = {
        "old_current_mappings": 0,
        "pending_evidence": 0,
        "dirty_groups": 0,
        "exhausted_groups": 0,
        "migration_complete": False,
    }
    migration[blocking_field] = 1
    monkeypatch.setattr(
        runner,
        "event_mapping_migration_status",
        lambda: migration,
    )

    assert runner.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["migration_complete"] is False
    assert payload["mapping_migration"][blocking_field] == 1
