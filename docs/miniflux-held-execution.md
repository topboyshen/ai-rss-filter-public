# Held 精确执行 CAS：隔离适配器

本变更只增加隔离审定/执行层，不改固定 Miniflux 2.3.2 outbox 补丁、不修改 `.126`，不部署。CLI 必须显式 `--access-mode isolation`，数据库名只能是 `synthetic_outbox`、目标只能 loopback；接收端还须返回审批绑定的隔离 identity。生产适配器、真实停止证明、登录角色/权限和实际凭据仍未绑定，**不能用于生产执行**。

## 与旧审定层的区别

原 `reserve` 的 `authorization_reserved_only` 语义不变。新 `execution.sql` 单独安装于新隔离 schema，复用实际固定版本的 `webhook_outbox` 表，提供 `execution_approve/claim/begin/finish/query`。它不属于应用启动迁移，也不占用新的官方 schema 版本；原补丁 migration 132 与未来官方迁移冲突的限制仍然存在，不能直接官方升级。

- reviewer、executor 是不同真实 PostgreSQL `session_user`，不能靠 `SET ROLE` 冒充分权；同时属于两角色会被拒绝。
- 登录角色只有指定函数 `EXECUTE`，没有原 outbox 或审计表的任意 SELECT/UPDATE/DELETE/ALTER、重置 attempts、改 endpoint、重签入口。NOLOGIN definer 仅有必要列 UPDATE，固定 search_path；PUBLIC 函数执行被撤销。
- installer-owned 窄 helper 只读取**当前连接**的实际 backend start，用于 PID 重用防护；没有给 definer `pg_read_all_stats`。安装者/数据库 owner/superuser 仍可绕过权限，不能声称整个数据库被防篡改。
- 旧 heldtool 的隔离合成管理员模式保留用于原测试；不能拿它代替本次分权执行入口。

## 一次执行的持久边界

1. Reviewer 读取原始 held 行生成精确计划，批准原 body/完整 endpoint/HMAC/contract/attempts/created/lease 的 snapshot 哈希、停止证明和 operator note。本文工具只核原 HMAC 字符串未变，不读取密钥重新计算或重新签名。
2. Executor 领取：固定锁顺序为 advisory → stopped marker → outbox row → approval。在锁内重验审批、原 snapshot、20 次/7 天预算、无自动 inflight，并同步提交 `claimed` 审计、原 attempts 增一（仅 retry_once）、`manual:<operation>` 隔离标识。`created_at` 永不重置。
3. 同一真实 DB connection 再开事务，重新锁 marker 与完整 outbox 行并比对领取后的 snapshot；确认原连接 PID＋backend start，且没有结果。**持锁期间**执行无原消息的隔离 identity probe 和最多一次原样 HTTP，再同事务 CAS 更新 outbox 与追加结果审计。每 SQL/锁等待 3 秒，网络 probe 1 秒、原消息 10 秒，整体网络事务 20 秒、空闲事务 15 秒；客户端超时不等于服务端副作用没发生。
4. 只有原协议严格 `schema=1,durable=true,body_sha256=delivery_id=原body SHA256` 且匹配 HTTP 202/receipt 200 才进入 acked。普通202、503、404、超时、错误/缺失回执不释放 held；manual 标识保留，不放回自动 pending，不自动重复。

领取提交丢 ACK、领取后崩溃、HTTP 成功后审计失败、最终提交结果未知，一律只 `execution_query` 同一 operation，查询不发 HTTP。结果不存在返回 `claimed_outcome_unknown`，不能据此推断未发送。恢复只能在另行确认原操作已停止后，由另一份明确审定的 **confirm_receipt** 操作查原消息的已签名 receipt；不能用它重发 `/webhook`。旧操作的未知审计不改写。接口的原文读取能力仅限可信执行器；它不是对已获得原文的恶意 executor 的外部网络强制沙箱。

`stop marker` 是隔离 fixture 的事实绑定，不是生产停止证明。生产统一 scheduler/manual/legacy 入口停流尚须独立实现/核验；`pg_stat_activity`、outbox 无 inflight 或 marker 布尔值本身均不能替代完整生产者停止。

## 容量和保留

新 execution 审计独立预留每 operation 3 行、12288 bytes，上限 10000 行和 16 MiB（实际最多1365份审批）；审批时即为后续 claim/result 留空间。原审定层还有自己的16MiB预算，两层不能合称共享16MiB。没有自动审计 GC、删除/更改结果通路，满额显式背压。原 sender 仍受5000行/256MiB、单次20尝试和7天预算；7天不是未交接消息的自动删除许可。未完成/超龄原件继续保留，容量耗尽可能阻止新 entry 提交，不声称无限无损。

## 可执行隔离验证

固定实际 migration 文件 SHA256 必须为 `42ee96b4a961887d5387f8a0261ec6f8e9132f78eb362760451c172341640dad`。测试程序在固定 PostgreSQL 容器内运行，包含真实不同角色 session、原行锁、loopback HTTP、并发、未知结果和审计回滚，不是 fake cursor。资源固定384MiB/64PID/1CPU，比授权768MiB/256PID更严；本机arm64不称原生amd64性能。

```sh
python ops/miniflux/held-access/verify_execution.py --repo /absolute/repo --source /absolute/fixed-patched-source --output /new/private/evidence --run
```

该本机 wrapper 创建新的内部网络、私有合成卷及容器，无生产挂载/外部端口；保留失败和成功原件、卷，只停止本次所属容器。CI 交叉编译真实测试二进制后，在已有受限原生 amd64 PG service 内执行，需复制 `install.sql`、`execution.sql` 并设置 `HELD_ACCESS_SQL_DIR`。运行 `go test` 没有真实 PG DSN 时会跳过集成项，不能将跳过称作通过。

执行入口使用私有审批文件及环境 DSN，不把凭据放命令参数：

```sh
heldtool --access-mode isolation --access-approval /private/exact-approval.json
heldtool --access-mode isolation --access-query ORIGINAL_32_HEX_OPERATION
```

测试成功不等于真实生产停止、网络隔离、权限绑定、断电耐久性或未来实际部署已通过；生产启用仍需单独批准。
