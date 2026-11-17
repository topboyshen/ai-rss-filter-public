# 选择语义（P0）

本文固定“一条新闻算不算入选”的口径。它只描述规则与现有出口的映射，不改变行为；
任何实现变更必须先改本文，并同步更新 `tests/test_selection_chain_acceptance.py`。

## 三个状态

| 状态 | 判定 | 用户出口 | 账本 |
|---|---|---|---|
| **可供浏览** | 实质评分，且 `score >= feed pass_threshold`（`core/processor.py:1089`、`:2005`） | 单源观察流（`feed_preview`，不设分数下限）；首页 Briefing 仍需达到聚合门槛 | `news_cache` 保留 |
| **重点入选** | 非 `non_substantive`，且 `score >= feed aggregation_threshold`（`core/processor.py:709-713`） | 首页 Briefing、聚合卡片、Quant 信号 outbox | `backtest_signals.selected=1` |
| **不可作实质判断** | `insufficient_input`、`llm_safe_neutral_*`、`quality_quarantine`（`scoring_provenance.py:98`、`:134`） | 不出现在任何公开列表，只做计数与审计 | `selected=0`，不导出 |

补充：

- `market_flash_rule`（3.0「行情」）是实质规则分，可进观察流，但不进 Briefing、不导出。
- `parse_error`、`similarity` 是审计态：不导出，通常也过不了文章门槛。
- “文章通过”和“重点入选”是两个出口。任何指标都必须先说明它对应哪个出口。

## 已定决策：封顶 5.0 的低价值内容

封顶规则 `_cap_low_value_announcement`（`core/processor.py:780`）只看标题：
标题命中会议、论坛、致辞、拍卖等低价值模式，且没有具体动作或公司名时，把高于 5.0 的分数压到 5.0。

**决策（2026-11-17）：封顶内容属于“可供浏览”，永不“重点入选”。**

- 允许：出现在单源观察流；保留在 `news_cache` 供去重与审计。
- 禁止：出现在首页 Briefing、聚合卡片，或进入 Quant 信号（`selected=1`）。
- **显式守卫（2026-11-17）**：封顶结果写入账本时直接固定 `selected=false`
  （`content_capped` → `_build_backtest_signal_snapshot`），不再依赖聚合门槛。
  生产最低聚合门槛 6.0 只是当前配置，不再是这个决策的保障。
- 残余：相似稿不继承封顶标记；若未来门槛降到 ≤5.0，相似稿需要单独的溯源处理（P3）。
- 封顶只按标题判定，摘要中的实质内容不会解除封顶；标题命中公司/行业词会绕过封顶。
  这两点是已知缺口，见“未决事项”。

## 指标口径

- **文章通过率** = 状态 1 的比例；不能用它评价状态 2。
- **入选率** = 状态 2 的比例；人工标签必须声明对应哪个出口。
- **信息不足率** = 状态 3 的比例，单独报告，不与“低分”混算。
- 误选/漏选必须带出口：观察流误选、Briefing 误选、账本 `selected` 误选是三个不同指标。

## 未决事项

1. **P1 页面出口验收（已完成 2026-11-17）**：`tests/test_selection_chain_acceptance.py`
   的 outlet 用例覆盖观察流、Briefing 与 `integration_signal_outbox`；生产只读核对用
   `/cached_feed` 的 `preview=true` 与高分出口对比。注意：账本不记录“是否被封顶”，
   生产无法从单条 5.0 反推它是否由封顶产生；这属于 P3 的溯源缺口。
2. **P2 封顶标题误伤**：会议标题 + 实质摘要仍被压分；公司/行业词绕过封顶。
3. **P3 离线复现与独立人审**：固定输入、配置、模型输出与处理链版本后再比较离线与生产；
   同时补封顶溯源的观测手段。

## 变更规则

改动任一状态的判定、出口或指标，必须在同一个 PR 内更新本文与
`tests/test_selection_chain_acceptance.py`，并在 PR 描述中列出受影响的出口。
