# 固定 Miniflux：旧保存路径保全边界

本文件说明本次固定 2.3.2 补丁的入口覆盖和可验证边界，不是生产部署或停止证明。生产未访问，`.126` 不变，不调用真实模型。

`HANDOFF_ENABLED` 是短时受控切换模式。公共 HTTP 在原有凭据验证成功后、首次认证／业务写入前取得
贯穿完整请求的 lease；匿名、错误凭据和静态请求不占持久租约。UI 仅允许已有有效登录会话，匿名／新登录、
OAuth 与认证代理新建会话在此模式拒绝且不创建 session；关闭时保留旧行为。启用前必须另审可用会话和控制通路。

## 实际入口与保存次序

| 入口 | 启用 `HANDOFF_ENABLED` 后的行为 |
|---|---|
| scheduler / worker / API、UI 手动刷新 | 根处理器持有共享生命周期 lease；新文章与 `new_entries` 原始消息在既有 outbox 事务内保存。后续非 webhook 集成改为同步执行，处理器继续持 lease 完成 feed 更新。 |
| API `saveEntry`、UI `saveEntry` | 读取既有文章后调用 `StartSaveEntry`；独立 `manual_save` lease 准入，再同步保存原始 `save_entry` 消息，成功后才启动其他集成及返回既有 HTTP 接受响应。 |
| Fever `saved`、GoogleReader `saved` | 同样调用 `StartSaveEntry`，不再先 `go` 后保存。批次不是原子整体：前项已保全、后项失败时保留前项，不宣称整批全成功。 |
| 直接 `integration.SendEntry/PushEntries` 或 webhook Client 旧发送方法 | 启用时拒绝未追踪路径，不绕过原件保全。关闭时保留旧异步行为。 |
| API 清空历史、删除用户 | 启用时同步完成数据库操作，实际返回前不结束 HTTP lease；失败返回错误，不把后台仍写库误报成已排空。关闭时保留原异步行为。 |

`StartPushEntries` 在启用时同步执行非 webhook 集成，是有意改变的调度/耗时边界。现有集成的错误日志不是外部交付成功证明。回调或 goroutine 崩溃、lease 完成未确认均不得伪造排空。

## `save_entry` 不当成可发送的 `new_entries`

原始消息复用旧发送器相同的 `SaveEntryPayload`、JSON 编码和 HMAC 算法，冻结 exact bytes、body SHA-256、endpoint、signature、contract、user/feed/entry 身份。feed 专用 URL 仍覆盖用户默认 URL。

本次接收端协议没有支持 `save_entry`，因此新表 `webhook_legacy_originals` 只存 `held`，不将它加入发送队列，不发送、不自动转成另一种事件、不重签、不增加重试。API 的普通 202、UI 的既有 201 文案只表示当前请求获得接受；它们不是接收端 durable ACK、评分成功或 save_entry 已外送的证明。必须分别查看原件表与实际接收回执。

原件保存发生在异步间隙之前，且只有 PostgreSQL 同步提交成功才返回成功。它不是“保存文章行与原件同事务”：此入口读取的是既有 entry，Fever 的标星等既有更改也不因此获得跨操作原子性。保存原件失败会拒绝本次接受，但不能声称更早的独立业务操作均已回滚。

## 有限容量与保留

- 单条原文不超过 1 MiB；原件和 outbox 合计最多 5000 行 / 256 MiB（计费含原文字节、endpoint、contract 和每行 1024 字节附加预留）。不是 PostgreSQL 实际磁盘大小上限。
- 两条写路径必须用同一事务 advisory lock 计费。已有 exact duplicate 可以确认，但同 body 改 endpoint、signature、contract 或身份必须拒绝。
- 原件是不可更新、不可删除的 held 证据，没有自动过期、GC 或自动补偿。5000 行是硬背压，不是无限无损承诺；容量耗尽拒绝新接受，并保留已有原件。
- outbox 的 20 次 / 7 天发送预算不适用于手工保存的原件；这里没有自动发送预算或自动重新入队。将来人工处置须单独设计和批准，不能靠改状态释放。
- 配额、WAL 设置、事务超时、写失败均显式拒绝；3 秒数据库事务预算不表示所有外部集成均有共同 3 秒执行上限。

原件包含文章内容、端点和签名，属于私有敏感数据，不写普通日志。本补丁不提供字段加密，数据库 owner/
superuser 仍可读取。既有仅针对 outbox 的 held 审阅权限不能自动当成新原件表的权限审计；正式启用前须
独立绑定应用角色、受限审阅角色与备份/导出权限，不向现有公开 API 新增原文查询或自动释放入口。

`scripts/observe_handoff_capacity.py` 现在在同一只读事务中绑定两张表的 OID，分别统计 outbox
和 `webhook_legacy_originals`，再合计共享行数／计费字节。新表不存在、身份变化或旧的单表样本均明确失败，
不会补零。`scripts/independent_drain_inventory.py` 遇到任一 outbox held 或 legacy held 都保持阻塞，
不能把门控 lease 归零说成库存已交接。本补丁的共享硬上限不变；实际 PostgreSQL/WAL 磁盘仍需另计。

新原件表的隔离权限审计独立于旧 outbox 工具，见 `docs/miniflux-legacy-access.md`。
该审计不提供 legacy held 的转交、释放、重签或删除能力；生产应用角色与实际权限仍未核验。

## 切换和回退仍有的前提

新门控无法追溯旧未修补进程已发起而尚未完成的 goroutine，也无法从数据库有 entry 推断当时 webhook 已发送。首次切换必须独立确认旧实例停流、在途和原始消息保全状态；未知不能写成零。

受控排空现在有独立 `/stop-new`：停止新增生产，允许专用 durable outbox 消费者继续；之后 `/pause`
才完整冻结消费者。它不改变 `StartSaveEntry` 同步保全、scheduler 生命周期或直接旧发送拒绝规则，
也不让 held 原件自动发送。操作确认、重启和迁移134边界见 `miniflux-pause-isolation.md`；原有
宿主bridge仍执行完整pause，未自动获得新的排空编排能力。

迁移 133 包含门控与原件表，承接本地补丁迁移 132；它们占用了上游顺序版本槽。不得直接升级官方同号迁移，也不得因旧程序可能启动就声称可安全回退。旧程序不会处理这些 held 原件，也不遵守新共享配额/停止门控。生产凭据、角色、所有进程身份与实际网络入口仍需另行绑定和批准。

## 验证口径

新增测试比较旧发送器实际 loopback 请求与保全 JSON/HMAC，覆盖未绑定准入拒绝、直接旧发送绕过拒绝、默认关闭旧异步正例。真实隔离 PostgreSQL 测试覆盖：同步提交、不可变原件、重复/冲突、共享行/字节额度、并发、锁竞争、写失败、真实 SQL 超时和清理不删除 held。只有相应测试实际执行且成功的证据才可称通过；未设置隔离 DSN 的 Go 测试会跳过 PostgreSQL 部分，不能替代真实数据库验证。
