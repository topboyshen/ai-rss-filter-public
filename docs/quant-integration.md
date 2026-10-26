# Quant 集成契约

AI-RSS-Filter 与 Quant-Server 有两个方向相反、权限独立的只读通道：

| 方向 | 数据 | 用途 | 是否影响 AI 评分 |
|---|---|---|---|
| AI → Quant | 不可变信号修订 Outbox | Quant 消费评分、方向和冻结契约 | 否 |
| Quant → AI | 市场风险、波段决策及有效性摘要 | Briefing 的市场旁证 | 否，严格展示通道 |

两个通道使用不同的 Bearer token，不复用 Dashboard/API Key，也不提供交易写接口。

## AI → Quant：信号修订流

端点：

```text
GET /integration/v1/signals
Authorization: Bearer <dedicated-service-token>
```

生产凭据来自 API 容器独占的 Docker secret
`secrets/ai_filter_integration_token`。查询参数不接受 token，接口不授予浏览器 CORS。

### 首次引导与游标

首次请求：

```text
GET /integration/v1/signals?schema_version=v4&limit=20
```

响应提供 `ledger_id`、`epoch`、`floor_seq`、`last_seq` 和页面的
`next_after_seq`。后续请求必须携带完整游标：

```text
GET /integration/v1/signals?schema_version=v4&ledger_id=<32-hex>&epoch=<32-hex>&after_seq=42&limit=20
```

消费者必须显式固定 `schema_version=v3` 或 `v4`，不能依赖当前默认 v3。需要
`mapping_confidence`、`mapping_relevance` 和 `expected_gap` 时使用 v4，并在每次游标请求
中保持同一 wire schema。

消费者应先把整页数据提交到本地持久层，再原子推进到 `next_after_seq`。重试语义为
at-least-once，使用不可变 `signal_revision_id` 幂等去重，并严格按 `seq` 应用修订。
`available_at` 是事件时间，不是交付顺序。

异常游标不会被静默纠正：

- 游标低于保留窗口返回 410；
- `ledger_id`/`epoch` 不匹配返回 409；
- 游标超前于恢复后数据库同样返回 409；
- 以上情况都需要消费者显式 bootstrap 或 reconciliation。

Outbox 与 `backtest_signals` 是独立的连续日志，默认保留 90 天、最多 20,000 行，单页
最多 50 行且响应不超过 64 KiB。清理历史信号不能在 Outbox 中制造无提示缺口。

### 最小数据面

输出只包含评分状态、冻结策略/映射字段、修订元数据与哈希化独立性标识，不导出原始文章
URL、标题、摘要或来源查询。`direction` 仅允许看多、看空、中性；分数保留历史上的
`-1` 解析失败哨兵至 10。

## Quant → AI：市场旁证

后台 Worker 通过固定 allowlist 隧道读取 Quant 市场上下文：

```text
http://host.docker.internal:18092/integration/v1/market-context
```

凭据来自 Worker 独占的 `secrets/quant_market_context_token`。请求禁用环境代理和重定向，
限制响应大小、超时、轮询频率和 JSON 结构。Web 进程不直接访问 Quant，只读取 Redis 中
的短期快照。

上下文包括数据时点、行情日期、风险等级、波段决策状态、决策审计和有效性摘要。过期、
缺失、结构错误或来源时间越界时，页面进入技术上的 fail-neutral/部分降级状态，不会用
旧值伪装实时结论；这表示“旁证不可用”，不是“市场判断为中性”。

在 Dashboard/Digest 路径中，该上下文只在 Briefing 组装完成后附加；同一净化后的只读
DTO 也可由本地 MCP 查询。两条读取路径都遵守以下边界：

- 不进入来源筛选；
- 不改变 AI 分数、方向或主题；
- 不改变个股映射；
- 不进入模型质量样本；
- 不触发交易或 Quant 决策。

因此“AI 领先、Quant 未确认”或“风险冲突”只是双轴展示结果，不是否决 AI 情报，也不
代表指数当天涨跌的简单比较。

## 恢复与 epoch

恢复或破坏性重建 `cache.db` 后，旧 Quant 游标可能指向另一个数据库历史。执行 epoch
旋转前必须暂停 Quant consumer，并停止 API、所有 SQLite reader/writer 及相关 cron/timer；
`--confirm-writes-stopped` 只是人工断言，不会替操作者检查进程。

```bash
python3 scripts/rotate_integration_outbox_epoch.py \
  --db /root/ai-rss-filter/data/cache.db \
  --confirm-writes-stopped
```

命令使用 `BEGIN IMMEDIATE`，执行 SQLite `quick_check` 与 Outbox 连续性检查，再原子旋转
epoch。正常应用镜像发布不需要旋转 epoch。`--clear-retained` 属于最后手段，不应作为
普通恢复流程使用。

旋转完成后继续保持 API 和 consumer 停止，备份 Quant 本地状态，对账新的
`ledger_id`/`epoch`/`floor_seq`/`last_seq` 与旧消费记录，处理恢复造成的 orphan revision
和反向差异，再原子提交新的游标并恢复消费。仅清空游标或把它重置到 `floor_seq` 不构成
完整 reconciliation。

## 相关实现

- HTTP 鉴权与边界：[`routes/integration_routes.py`](../routes/integration_routes.py)
- Outbox 查询：[`services/integration_signals.py`](../services/integration_signals.py)
- Outbox 存储：[`db/integration_outbox.py`](../db/integration_outbox.py)
- Quant 市场旁证：[`services/quant_market_context.py`](../services/quant_market_context.py)
