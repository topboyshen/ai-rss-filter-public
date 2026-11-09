# Technical Debt & Known Issues

**Last Updated**: 2026-11-10

---

## 🟡 Skipped Tests (3 total)

### 1. `test_refresh_rate_limit` (test_integration.py)
**Status**: Skipped  
**Reason**: Dynamic import bypasses monkeypatch  
**Impact**: Rate limiting功能已通过其他测试验证，功能正常  
**Root Cause**: 测试在动态导入模块后尝试 monkeypatch，此时模块已加载，patch 不生效  

**Possible Solutions**:
- 方案 A: 使用 `unittest.mock.patch` 替代 `pytest.monkeypatch`
- 方案 B: 重构为集成测试，使用真实的 Redis/时间
- 方案 C: 在模块导入前 patch（需要重构测试结构）

**Priority**: P2（功能已覆盖，测试改进）

---

### 2. `test_ops_feeds_widget_uses_recent_feed_health_without_cache_rows` (test_ops_health.py)
**Status**: Skipped  
**Reason**: UI output contract changed after ops widget refactor  
**Impact**: Ops 健康面板功能正常，输出格式变化  
**Root Cause**: Ops widget 重构后，HTML 输出格式变化，测试期望过时  

**Possible Solutions**:
- 方案 A: 更新测试期望以匹配新的 HTML 输出格式
- 方案 B: 改为测试结构化数据（JSON）而非 HTML 字符串
- 方案 C: 使用 CSS selector 而非字符串匹配

**Priority**: P2（UI 测试，功能正常）

---

### 3. `test_consecutive_empty_feed_is_visible_even_with_recent_cache_rows` (test_ops_health.py)
**Status**: Skipped  
**Reason**: Empty feed visibility logic changed  
**Impact**: Empty feed 显示逻辑已更新，测试假设不再成立  
**Root Cause**: Feed 可见性逻辑演进，现在空 feed 的显示条件不同  

**Possible Solutions**:
- 方案 A: 阅读新的可见性逻辑，更新测试场景
- 方案 B: 删除测试（如果新逻辑不再需要此场景）
- 方案 C: 改为测试新的可见性规则

**Priority**: P2（逻辑已变更，需要重新设计）

---

## 🟠 Architectural Issues

### Circular Dependencies (33 detected)
**Status**: Informational  
**Impact**: 不影响运行时，但增加维护复杂度  
**Detection**: `scripts/check_circular_imports.py`

**主要循环**:
- `core ↔ services ↔ db`
- `routes ↔ services`
- `utils → routes → utils`

**Long-term Solution**: 引入依赖注入 / 重构为分层架构

**Priority**: P3（长期重构）

### Dashboard 巡检快照缺少 `trading_calendar`（2026-11-10）
**Status**: Open  
**Impact**: `ops/ai_filter_ops_check.py` 的 `CHECKS` 会执行交易日历覆盖检查，但写 dashboard 快照时
`_DASHBOARD_CHECK_NAMES` 不含 `trading_calendar`，dashboard 读取端 `_HOST_CHECKS` 也没有这一项；
日历缺年份的告警不会出现在页面与 attention 列表，只能从巡检日志/退出码看到。`_CHINA_HOLIDAYS_BY_YEAR`
目前只有 2026，`check_trading_calendar` 会从 2026-12-02 起失败（horizon=+30 天进入 2027），
而 2027-01-01 起收盘采集、事件结果与回测会因无法判断交易日而报错。  
**Root Cause**: producer 与 consumer 两份白名单在 `check_trading_calendar` 加入 `CHECKS` 之前维护，
之后没有同步；且 consumer 遇到未知检查名会把整块服务状态降为 `unknown`，所以只改一侧不够。  
**Possible Solutions**:
- 方案 A: 同一提交内同时给 producer `_DASHBOARD_CHECK_NAMES` 与 consumer `_HOST_CHECKS` 加
  `trading_calendar`（标题「交易日历」），并补两侧测试
- 方案 B: 抽一份共享检查清单，producer 与 consumer 都从它派生，避免再次漂移
**Priority**: P1（2026-12-02 起巡检会持续失败，修复窗口约三周）

---

## 🔴 Security Issues

### Dependency Vulnerabilities (17 known)
**Status**: Detected by pip-audit  
**Impact**: 主要在 pip 22.0.2  

**Immediate Action**:
```bash
.venv/bin/pip install --upgrade pip
```

**Priority**: P1（安全）

---

## 📊 Test Coverage

**Current**: 80.48% (as of 2026-08-19)  
**Target**: 85%+

**Low Coverage Areas**:
- `core/metrics.py`: 25.53%
- `db/event_migration.py`: 56.34%
- `services/jin10_feed.py`: 63.64%

> 2026-11-09 备注：发布门禁的当前总覆盖率是 83.11%（完整套件实测），上面这组模块数字尚未重新核对。
> `routes/feedback_routes.py` 从未注册蓝图，因此测试不会导入它（该原型文件已随死代码清理移除）。

**Action**: 添加单元测试覆盖这些模块

**Priority**: P2（质量改进）

---

## 📝 Database Issues

### Large Database Size
**Current**: 540MB (data/cache.db)  
**Issue**: PRAGMA integrity_check 超时 (>10s)

**Mitigation**:
- ✅ 配置自动维护（每周日 3am）
- ⏳ 考虑归档策略（6 个月以上数据）
- ⏳ 评估 WAL 模式

**Priority**: P1（性能）

---

## 🛠️ Automation Setup

### Completed
- ✅ Circular import detection script
- ✅ Weekly database maintenance (cron)
- ✅ pip-audit installed

### Pending
- ⏳ Add import check to GitHub Actions CI
- ⏳ Add pip-audit to CI
- ⏳ Add mypy type checking

**Priority**: P1（CI/CD 改进）

---

## 📅 Review Schedule

- **Weekly**: Check cron logs (`/var/log/ai-filter-db-maintenance.log`)
- **Monthly**: Run `pip-audit`, review test coverage
- **Quarterly**: Revisit skipped tests, evaluate tech debt backlog

---

**Maintained by**: Development Team  
**Next Review**: 2026-09-19

---

## 🟠 High Complexity Functions (Deferred Refactoring)

### `run_score_backtest` (complexity: 114, 823 lines)
**Location**: services/backtest.py:1121  
**Issue**: Monolithic function with multiple responsibilities:
- Data loading and preparation
- Theme follow-through analysis
- Price direction and return statistics (T0/T+1/T+2)
- Independent event clustering
- Control cohort settlement
- Response building

**Risk**: High - Core business logic with complex edge cases  
**Current Mitigation**: 
- ✅ 91.77% test coverage
- ✅ Comprehensive docstring added (P1 improvement)
- ✅ Type annotations complete

**Refactoring Plan** (P3 - Long-term):
1. Extract theme follow-through analysis into `_analyze_theme_followthrough()`
2. Extract horizon metrics computation into `_compute_horizon_metrics()`
3. Extract clustering logic into `_cluster_independent_events()`
4. Extract response building into `_build_backtest_response()`
5. Reduce main function to <20 complexity (coordination only)

**Estimated Effort**: 3-5 days  
**Prerequisites**: 
- Feature freeze window
- 95%+ test coverage target
- Comprehensive integration tests
- Manual QA on staging environment

**Priority**: P3 (Long-term improvement, not blocking)

---

### `run_mapping_validation` (complexity: 54)
**Location**: services/mapping_validation.py:XXX  
**Issue**: Similar to run_score_backtest, handles multiple concerns  
**Mitigation**: 79.07% test coverage, docstring added  
**Priority**: P3

---

### `_read_candidates` (complexity: 70)
**Location**: services/event_outcomes.py:XXX  
**Issue**: Complex state machine for outcome candidate reading  
**Mitigation**: 68.36% test coverage  
**Refactoring**: Consider explicit state machine pattern  
**Priority**: P3

---

### Other High Complexity Functions (complexity >15)

| Function | Complexity | File | Coverage | Priority |
|----------|------------|------|----------|----------|
| `render_backtest_html` | 46 | backtest.py | 91.77% | P3 |
| `_validated_outcome` | 43 | mapping_validation.py | 79.07% | P3 |
| `_snapshot_articles` | 35 | backtest.py | 91.77% | P3 |
| `_render_mapping_contract_ledger` | 28 | mapping_validation.py | 79.07% | P3 |
| `_classify` | 28 | quality_bridge.py | 73.31% | P3 |

**Decision Rationale**:
- These functions are stable and well-tested
- Production system is running reliably
- Risk of regression > benefit of reduced complexity
- Documentation and test improvements provide better ROI

**Review Policy**:
- Revisit when test coverage reaches 95%+
- Defer until feature freeze window available
- Consider during next major version refactor


---

## 📊 Test Coverage Improvement (P2/P3)

### Modules Below 80% Coverage

| Module | Current | Target | Gap | Priority |
|--------|---------|--------|-----|----------|
| event_outcomes.py | 68.36% | 80% | -11.64% | P2 |
| quality_bridge.py | 73.31% | 80% | -6.69% | P2 |

### Coverage Gap Analysis

**event_outcomes.py (68.36%)**:
- Uncovered: Error handling branches, edge cases in `_read_candidates`
- Reason: Complex state machine logic, requires extensive mocking
- Quick wins: Add input validation tests (None, empty, invalid types)
- Deep coverage: Requires business logic understanding (P3)

**quality_bridge.py (73.31%)**:
- Uncovered: File I/O errors, JSON parsing failures
- Reason: External dependencies (filesystem, JSON format)
- Quick wins: Add exception handling tests
- Deep coverage: Integration test scenarios (P3)

### Improvement Strategy

**Phase 1 (P2 - This Sprint)**: ✅ Completed
- ✅ Quantify coverage (pytest-cov)
- ✅ Identify uncovered code paths
- ✅ Document gaps in TECHNICAL_DEBT.md
- 🔄 Add 5-10 simple error handling tests (optional, low priority)

**Phase 2 (P3 - Next Quarter)**:
- Deep business logic tests for `_read_candidates` (70 complexity)
- Integration tests for quality bridge workflows
- Mock database interactions systematically
- Target: 85%+ coverage across all modules

### Decision Rationale

**Why defer deep testing**:
1. Current coverage (68-73%) is acceptable for stable modules
2. No production bugs reported in these areas
3. Time investment required >> expected bug prevention
4. Documentation improvements provide better immediate value

**Quick wins implemented**:
- Coverage report generated (htmlcov/)
- Gaps documented for future sprints
- Test infrastructure ready (pytest-cov installed)


---

## Event Radar remediation (2026-08-20)

### Resolved

- Added structured slow-operation telemetry with a 500 ms warning threshold.
- Replaced the global equity-root expansion with a candidate-correlated recursive
  filter, preserving snapshot semantics while bounding work to index-ordered candidates.
- Extracted `_merge_stock_mapping`, `_materialized_outcome_view`, and
  `_pending_outcome_view` from the three largest query/read-model functions.
- Completed `_load_event_page` API documentation and raised Event Radar function
  documentation coverage to 44.2% (19/43).
- Raised branch-aware coverage to 80.10% for `services/event_radar_query.py` and
  81.79% across the Event Radar query + routes modules.
- Production-data benchmark for the evidence-backed eight-event stream improved
  median latency from approximately 826 ms to 402 ms (51% reduction); output was
  differentially checked against the pre-change implementation.

### Remaining P3 debt

| Function | Cyclomatic complexity | Mitigation | Priority |
|----------|------------------------|------------|----------|
| `_revision_contract_is_valid` | 59 | Exact-contract positive/negative tests; 80%+ module coverage | P3 |
| `_load_generated_observation_states` | 41 | Pending-view extraction and direct cohort tests | P3 |
| `_load_similar_events` | 24 | Bounded result/edge limits and existing integration tests | P3 |

Further decomposition should occur only during a feature-freeze window and must
preserve the frozen read boundary, exact mapping contract, and no-look-ahead rules.

---

## Production integrity remediation (2026-08-20)

### Resolved in source

- Fixed the FastPass hint branch crash caused by an undefined `tier` identifier;
  metrics now use the bounded matched tag and cover both hint and bypass paths.
- Removed the unauthenticated, process-local `/prometheus-metrics` endpoint in
  favor of the existing authenticated, persistent `/metrics` endpoint.
- Added persistent 24-hour local-fallback ratio and alert gauges. The host ops
  monitor now fails when the model-eligible fallback ratio crosses 50% with at
  least 20 samples.
- Added a post-health release guard that rejects and rolls back containers when
  Python source files differ from their immutable image layers.
- Added an exact-date Tencent daily K-line fallback for required market indices.
  The collector still fails closed unless all four required indices are present;
  fallback responses are bounded, redirect-rejected, and reject look-ahead rows.
- Connected the root-only ops monitor configuration to Telegram through the
  trusted loopback proxy; credentials remain outside Git.
- A failed market-close unit remains critical unless the expected exact-date
  database contract is complete; a verified manual repair is reported as a
  warning until the next successful scheduled run.

### Operational invariant

Application source must never be copied into a running container. Every code
change must be committed, built into a versioned image by `scripts/release.sh`,
and deployed through its build-contract and automatic-rollback path.
