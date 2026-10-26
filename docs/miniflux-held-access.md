# Miniflux held 权限与审计：隔离验证方案

状态：仅隔离方案和真实合成 PostgreSQL 验证。未查询生产角色、账号、GRANT、superuser 状态或生产数据；不部署、不改变 `.126`，也不修改 sender 补丁／现有 heldtool 的行为。

`ops/miniflux/heldtool` **仍只允许 isolation**。本目录提供独立的授权／审计平面，不能直接发送 HTTP、改 outbox 或执行 held 恢复。它输出 `authorization_reserved_only`、`outbox_modified=false`、`forwarding_allowed=false`；不能将成功预留解释成补发、评分或交接完成。生产 executor adapter 仍未连接，另行批准后还须在真正执行时再次做精确 CAS，不能只凭早先预留执行。

## 最小权限与分权

| 角色 | 允许 | 不允许 |
|---|---|---|
| owner（NOLOGIN） | 固定 SECURITY DEFINER 函数所有者；读取原 outbox 计算 hash；写私有批准／审计 | outbox UPDATE／DELETE；直接登录 |
| reviewer（NOLOGIN 组角色） | 从一条 held 生成无敏感原文的精确计划；逐条登记 plan/proof 与审阅说明 hash | 原 body／HMAC／URL 读取，执行者预留，修改／删除批准或审计 |
| executor（NOLOGIN 组角色） | 领取另一个数据库登录身份审定的准确 operation/plan/proof/note | 创建自己的审批，重置 attempts/created，改 endpoint／签名，修改 sender 行 |
| auditor（NOLOGIN 组角色） | 读取安全批准、预留与追加审计 | 原文／HMAC，任何写入或执行 |

生产登录身份应独立授予一个组角色，不授予 owner，reviewer/executor 不能由同一登录同时持有。函数使用不可被 `SET ROLE` 冒充的 `session_user`；双重成员身份直接拒绝。所有函数固定 `search_path=pg_catalog,<私有schema>,pg_temp`，表引用完整限定；撤销 PUBLIC EXECUTE、schema/table 访问。内部 snapshot/validator 不向普通角色授予执行。

这仅约束**这些人工处置角色**。既有 Miniflux 应用账号可能是表 owner 或高权限账号，现有 sender 也需要正常写入 outbox；本轮没有核实它的实际权限。不能据此宣称整个数据库、表 owner、superuser 或拥有底层文件权限的操作者无法绕过。生产账号清点、最小权限改造和凭证发放必须另案审定。

## 精确审定与停止证明

计划由数据库读取原始 held 行生成，包含 delivery/body SHA、endpoint/HMAC 的 SHA、契约、attempts、created_at、manual lease 和整行 snapshot SHA。无 body／签名／完整 URL 输出。动作仅 `retry_once` 或 `confirm_receipt`；它们在此只表示未来执行意图。计划不能增加字段、改值或借空值绕过检查。

独立控制证明必须是 schema1、mode=isolation，绑定 operation、run、实际合成 PG identity、完整入口清单 manifest、固定 adapter identity、时间及逐项生产者关闭／sender 停止／无活动 lease 状态。由 fixture provision 的私有 marker 必须逐项相等；证明至多60秒旧／1秒未来，用数据库 `clock_timestamp()` 复核，不由 caller 提供“现在”。HTTP-only 切换 fixture 没有 PG marker，不能代替此证明。marker 的停止声明来自外部适配器，不因数据库里暂时没有活动会话就自动成立；本次 fixture 没有启动任何生产者／sender。

reviewer 在精确计划／证明上登记审阅说明 SHA（人工应确认原消息身份和未知结果风险）；executor 必须提供同一个 operation、plan SHA、proof SHA、说明 SHA。这里的 plan/proof SHA 是 PostgreSQL **jsonb 规范化值**的 UTF-8 SHA，不是原 JSON 文件字节 SHA，也不同于 heldtool 的 Go 对象 hash；将来 adapter 必须显式转换和校验，不能混用。

预留时再次核原行 snapshot、fresh proof 和预算：attempts 达20或创建满七天不得授权 retry；仅准确 receipt 查询意图可审定。人工 lease 未结束时要匹配已停止的旧 operation。由于本层不拥有 outbox 写权限，预留不锁定未来 sender 行；后续真正执行必须停止相关角色并重新检查，严禁将这种授权预留当作已经取得可转发的任务所有权。

## 审计、容量、事务

批准前以事务 advisory try-lock 串行审计容量判定，最多10000个预留审计行／16MiB计费。每个 operation 保守预留2行、8192字节（批准、执行者预留），约2048个operation先触字节上限。计划＋证明＋开销必须装入这份预留；不是 PostgreSQL/WAL 物理磁盘上限。已批准未领取也保留容量，永不自动回收。

批准／领取都强制同步提交并要求 PG fsync/full_page_writes 开启。批准、预留、审计均追加型，普通角色没有直接 DML；UPDATE/DELETE 还有触发器拒绝。TRUNCATE/ALTER、重置预算、改签名／endpoint也不授予。满额在新增批准前拒绝，事务 abort 不留下半个预留或审计。并发相同 operation 用数据库唯一键与审批行锁，只能一个 executor 预留；后续重复明确失败，不默默重放。

没有自动删审计、降低七天留存、提高5000行／256MiB outbox上限或清理 held 的功能。容量耗尽需停止新增批准并人工决定审计归档／下一份受审迁移；不得通过删未完成记录腾位。长期容量与生产留存量仍需先测量。

## 隔离安装／核验接口

安装 SQL 仅在 `synthetic_outbox` 运行，schema 名必须是 `held_access_isolation_*`、原表 fixture 必须是 `held_source_isolation_*`。所有替换标识符通过严格正则，data literal 单独转义。真实 outbox 表来自锁定 Miniflux 补丁 `internal/database/webhook_outbox.go` 的实际 migration（先核SHA），不是简化表替身。

根代理的 native CI 可调用：

```sh
python ops/miniflux/held-access/verify.py --container "$PG_ID" --source "$RUNNER_TEMP/miniflux-outbox" --output "$RUNNER_TEMP/held-access-evidence" --run
```

无 `--run` 只输出计划，不接 Docker。host Python 仅编排；实际权限／函数／CAS／并发由容器里的 psql 和真实 PG 执行，要求固定 PG digest、无宿主 bind mount、内存≤768MiB、内存加swap≤768MiB、PID≤256及CPU≤1核。复用 CI PG service384MiB/64PID属于更严限制；不把 host Python 称为也在该 cgroup 内。六个独立 psql 会话使用实际 executor LOGIN 认证，不是 root 会话切角色或同一连接串行 mock。

本地 readiness 使用 TCP，避免将官方镜像初始化阶段的临时 Unix socket 当成最终服务。保存 Docker 状态以及可用的 cgroup `memory.events`／`memory.peak`；没有 `OOMKilled` 不单独等于排除所有 OOM，指标不可用时明确记录，不能推断宿主没有内存压力。fixture 的 database identity 来自实际 system identifier、数据库、postmaster起点与表OID，manifest明确本合成 fixture 未启动任何 producer/sender；这不是生产停机证明。

同一真实 PG 验证末尾只执行容量观察器明确保留的 `PG_OUTBOX_IDENTITY_SQL`／`PG_OUTBOX_SAMPLE_SQL`，在 repeatable-read READ ONLY 事务及3秒SQL／锁预算中，对合成 pending/inflight/held/七天前acked行返回安全聚合，并用 `_outbox_counts` 校验计费与零清理抵扣。输出明确为 `outbox_only_0001_not_a_drain_proof`，不覆盖新 `webhook_legacy_originals`，不能充当两表共享容量或独立排空证明。两表和 legacy 专用审计见 [新边界](miniflux-legacy-access.md)。这里证明实际 SQL／聚合语义；它通过 psql 执行，**没有验证 DB-API adapter 的端到端连接／超时传输**，不能与对应 mock sequencing 测试合称真实完整wrapper验收。

本地使用显式 Colima socket 与新建自有受限 PG 容器，禁止接 JD。成功／失败的 SQL命令SHA、真实退出码和安全输出、资源inspect、最终合成schema dump保留；CI runner销毁后的可回溯材料是上传artifact，不是临时volume。此验证不发送HTTP、不调模型、不改 sender 行来处理真实消息。测试管理员只对合成行预置容量／漂移，不能当真实审批历史。

## 发布前仍需批准

本方案不提供生产安装命令。需要另行核实生产登录角色和所属关系、独立 reviewer/executor 身份、控制适配器及审批存储保护、执行时CAS、审计归档和事故值守；在这些项完成前不应开放生产 held 工具。原私有 migration132 仍与未来官方同号迁移可能冲突，本权限层不更改 schema_version，也不解决官方升级兼容性，不能直接升级官方镜像或降schema回退。
