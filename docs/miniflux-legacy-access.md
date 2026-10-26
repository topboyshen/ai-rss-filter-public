# Legacy webhook 原件：容量、独立排空与只读权限

范围：固定 Miniflux `0001` outbox 与 `0002` migration 133 的
`webhook_legacy_originals`。这里只补观察与隔离权限验证，不修改两份应用补丁，
不开放生产 held 工具，不提供 legacy 删除、转发、补偿或重签入口。
生产角色、凭据、容器、实际业务数据和 `.126` 均不在本轮核验范围。

## 两表共享额度，不能把 held 当作空闲空间

容量观察器对 **同一 PostgreSQL repeatable-read READ ONLY 事务** 内的
`webhook_outbox` 和 `webhook_legacy_originals` 分别计数，然后合计：

- 原有总上限仍为 **5000 行／256 MiB**，不是两张表各有一份额度。
- 计费仍为 `octet_length(body)+octet_length(target_url)+octet_length(contract)+1024`。
  这是应用逻辑预留，不是 PostgreSQL 表、TOAST、索引、WAL 或磁盘的物理用量。
- outbox 的 pending／inflight／held／acked 保持独立；legacy 仅可为 held。
  不能将 legacy 原件映射成待发送任务，也没有 attempts 可重置。
- legacy 即使超过七天仍计费、保留，`cleanup_credit=0`；不套用 outbox
  已确认 ACK 后七天的清理规则，不假称有无限无损留存能力。

新 `PG_IDENTITY_SQL` 绑定两个实际表 OID。缺表、旧身份绑定、查询失败、未知状态、
任何表或两表合计超额，均停止观察，不按零处理。每表最多读取 5001 行，单 SQL／锁
预算仍为三秒；只返回聚合，不把原文读回客户端。客户端超时不等于服务端已取消。

结果 `coverage=outbox_and_legacy_v1`，`rows`／`charged_bytes` 是两表合计；
`outbox_rows`、`outbox_charged_bytes` 与 `states` 是 outbox 的分量，
`legacy` 是独立分量。不要继续将 `sum(states)` 与两表合计比较，或漏掉 legacy。
旧 `0001` 验证器只使用 `PG_OUTBOX_IDENTITY_SQL`／`PG_OUTBOX_SAMPLE_SQL`，明确输出
`outbox_only_0001_not_a_drain_proof`，不能用于新系统的完整容量或排空证明。

## 独立排空是另一道门，不是手工处置

`independent_drain_inventory` 只接受完整两表样本：

| 原件状态 | 阻塞项 |
|---|---|
| outbox pending／inflight | `sender_unfinished` |
| outbox held | `sender_held_retained` |
| 任意 legacy 原件 | `sender_legacy_held` |
| 缺 PostgreSQL 样本 | `postgres_not_observed` |
| 旧单表样本／缺字段／合计不一致 | 失败，不给零证明 |

所有 held 原件都保留，但**保留成功不等于已排空**。没有另一份已批准、可验证的
处置边界时，不能据此切换或释放接收。零 legacy 加零活动 outbox 也只满足 sender
一侧；仍需实际应用屏障、队列／journal 锁、发布追平及其他完整门禁。
本接口一直返回 `switch_authorized=false`。

## 新增最小权限只有审计，不复用 outbox executor

`legacy_audit.sql` 仅能安装在 `synthetic_outbox` 合成库和受限命名 schema。
本轮没有生产 installer。角色与原 outbox 人工执行角色分开：

| 角色 | 允许 | 不允许 |
|---|---|---|
| owner，NOLOGIN | 固定 SECURITY DEFINER 函数内部 SELECT 两表 | 登录、原件 DML、转发 |
| reviewer／auditor，独立 LOGIN 的组角色 | 单条 legacy 的安全身份摘要、两表计费汇总 | body／URL／HMAC 原文、修改、删除、TRUNCATE、ALTER、补发 |
| executor | 无 legacy 操作入口 | 审计函数、授权、领取、重试、确认、转发 |
| PUBLIC／未授权登录 | 无函数及源表访问 | 全部上述能力 |

函数使用实际 `session_user`，同时持 reviewer 和 executor 的登录也拒绝，
不能用 `SET ROLE reviewer` 掩盖。固定 `search_path=pg_catalog,<私有schema>,pg_temp`、
完整限定源表，撤销 PUBLIC EXECUTE；输入标识符严格校验，数据值单独转义。
单条审计只有 delivery/body hash、endpoint/signature/contract hash、整行 snapshot
hash、时间、计费和显式禁止转发标志。未知行、坏 identity、非 held 状态都拒绝。

这只证明这些人工审计角色的权限，不证明原应用表 owner、superuser、底层文件管理者
无法绕过，也不证明生产账号已经按此授权。真实角色绑定和凭据发放仍需另批。
固定 migration 132／133 与未来官方同号迁移的冲突仍在，不能据此直接官方升级、
降 schema 或回退旧镜像。

## 可复核的隔离验证

```sh
python ops/miniflux/held-access/verify_legacy.py \
  --container "$PG_ID" --source "$RUNNER_TEMP/miniflux-pause" \
  --output "$RUNNER_TEMP/legacy-held-evidence" --run
```

不带 `--run` 不接 Docker。源目录必须实际应用冻结 `0001+0002`，核验两份迁移的
完整 SHA；真实表直接使用固定 migration，不用简化表。PG 要求固定镜像 digest、
无宿主 bind mount、内存与 swap≤768MiB、PID≤256、CPU≤1；CI 的384MiB／64PID
更严。host Python 仅编排，SQL 在该 PG 容器执行，不把 host 编译／编排内存算入容器。

验证包含真实独立登录／拒权、未知行、同原件六会话并发审计、事务 abort、原件触发器
保留、两表精确计费、旧表缺口拒绝、独立排空 held 阻塞、4999＋1／再＋1 行数边界和
254份1MiB outbox＋1／再＋1份1MiB legacy 的字节边界。大容量 fixture 是直接按实际
schema 预置的合成历史，**不是准入、HTTP 或发送器已经接受这些消息的证明**。
每个边界 schema 都安装同一 `legacy_audit.sql` 原模板，并以真实 reviewer 登录调用
`shared_inventory()`：正常值须与观察器一致，超额须由实际 SECURITY DEFINER 函数
明确报 `shared hard quota exceeded`。观察器也独立拒绝，测试不提高额度或删除原件。

新审计会保留命令／SQL输入 SHA、真实 rc、超时 partial 输出、角色与资源、cgroup
events／peak、聚合及合成 dump。cgroup 指标不可读、peak 非非负整数、必需的
`oom`／`oom_kill` 计数缺失或非法均不得通过；不能把读取失败记成零 OOM。
私有目录0700、证据文件0600，旧证据不覆盖。
大字节 fixture 的256MiB重复原文不展开为超过512MiB的十六进制 artifact；保存逐行
delivery/body hash、长度和计费，另存明确省略范围。较小权限／失败 fixture 全量保留；
本地自有 PG 卷不删除，CI 容器销毁后的材料限于上传 artifact，不能假称原卷仍存在。

真实 PG 证明 SQL 与汇总语义；仍**不是 psycopg DB-API 传输端到端验证**，也不是
真实生产停止证明。文件锁＋实际 PG 的现场统一身份仍需另行绑定。没有 HTTP、模型、
采集或部署动作。完成这轮隔离测试也不自动授权生产 held 处置或切换。
