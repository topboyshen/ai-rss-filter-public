# 容量观察与停止确认边界

本方案不改接收端或发送端运行时预算，不清理、归档、搬走或重建原件、索引、负收据；不部署，不修改 `.126`。观察脚本是操作层组件，不调用模型，也不调用可能恢复落盘的 `/webhook/receipt`。

**当前生产启用仍被阻止。** 合并版本没有覆盖全部入口的共享准入屏障。`ProductionHandoffAdapter(mode="production")` 明确拒绝执行；容量观察器会保留停止意图并返回 `stop_unconfirmed`，不能把隔离文件屏障、`buffer.pause`、caller boolean 或普通 HTTP 202 说成生产已经停流。独立补齐生产屏障及真实镜像/进程/入口绑定、切换执行器并经授权验证前，不得有界启用生产。

## 固定预算和留存

| 项目 | 不可放宽的运行时边界 | 观察和处置 |
| --- | --- | --- |
| receiver receipts | 4096 条，256 MiB reservation | accepted/completed/terminal 以及别名都占容量；不自动删原文、receipt 或 task index |
| sender outbox | 5000 行，256 MiB charged bytes | pending/inflight/held/acked 全计入；按实际 SQL 字节口径计算 |
| sender 清理 | 仅已 ack 且至少 7 天的行符合既有清理条件 | 观察器不执行删除，估计可清理行也不给容量抵扣；未 ack/held 不因到龄自动释放 |
| receiver 别名预留 | `MAX_ALIAS_RESERVATION`，约 24.05 MiB | 除阈值之外另保留此最坏准入余量；不把磁盘空闲当逻辑容量 |

没有安全的常态归档/幂等墓碑/长期容量运维闭环，因此本方案不声称长期运行已解决。接近阈值、held、到龄未确认记录或观察缺证只请求停止；不能自动 resume、强制 release、删除或延长预算。停止确认也不等于队列已排空、可以发布或可跨契约回退。

## 实际只读采样

`scripts/observe_handoff_capacity.py` 的 `sample_receiver()` 不实例化 `ReceiptStore`，不恢复 `.pending`，不 mkdir/chmod/fsync。它按实际 inode/device/uid 绑定现存私有 spool，取现存 `.lock` 的非阻塞共享锁，严格核验全部 bounded receipt 内容、reservation header、primary task index 和新 alias intent。扫描最多 12296 个目录项、总读取最多 256 MiB，单个文件沿用 8 MiB 安全上限，默认最多 3 秒；任何 partial/pending、权限异常、未知文件、锁竞争、身份冲突或限额触顶都不能当作空库成功。私有原件保持不变。此采样较在线局部残缺隔离更保守，会在无法完整盘点时请求停止。

`sample_postgres(connect, dsn, expected, seconds=3)` 接收明确的 psycopg 连接函数和隔离 DSN，不自行发现生产凭证。真实 PostgreSQL 使用 repeatable-read / READ ONLY 事务、每 SQL 最多 3 秒，并核对真实 database/schema/system identifier/postmaster start/table OID。只取最多 5001 行聚合；5001 表示超过硬行数并拒绝，不把截断计数当全库计数。输出状态数量、费用计数、最大 attempts、ACK 时间完整性和保留时长，不返回原文、不运行清理。`pg_control_system()` 权限不足也停止，不回退成未经验证的身份。PostgreSQL 不是 SQLite；普通单测的 cursor fake 只检验 SQL 顺序，实际 PG 证据由独立隔离 job 提供。

只读不等于无负载。线程和客户端超时只限制调用方等待，不证明尚在执行的文件扫描或服务端 SQL 已取消。发生超时不重试，不与另一轮采样叠加；未知状态留存，随后仅请求一次停止。生产适配器当前不可用，不能因此宣称真实生产停流。

CLI 仅提供只读探测，不自动启用观察器：

```sh
python -B scripts/observe_handoff_capacity.py --sample-receiver /isolated/spool --receiver-binding /isolated/spool-identity.json
HANDOFF_CAPACITY_ISOLATED_PG_DSN=postgresql://...@127.0.0.1:5432/synthetic_outbox python -B scripts/observe_handoff_capacity.py --sample-pg --pg-binding /isolated/pg-identity.json
```

绑定文件是权限 0600 的私有 JSON，spool 是 0700；静态文件本身不是现场证明，两个采样器必须读取真实状态后逐字段比较。PG CLI 仅接受 loopback 与名为 `synthetic_outbox` 的隔离数据库；不能用变更环境变量把它作为生产执行授权。代码库函数供受控隔离 job 注入连接使用。

## 观察器与适配器协议

`StorageSampler(adapter, spool, spool_binding, connect, dsn, pg_binding)` 在文件/PG 采样前后调用实际 `adapter.snapshot(identity, generation)`。两次身份/代次须相同，真实持久 admissions 计数不得减少。sample 的实际观测时刻在读完后生成；caller 预填的 `all_paused=true` 不参与判定。

`CapacityObserver(policy, backend, journal_root).run()` 是有限循环；也可逐次 `tick()`。它持有独立 journal 锁，每一步严格持久保存；最多 48 次采样、最多 7200 秒，策略可收紧而不能增大硬限额，重启不重置时限、计数基线或意图。策略必须包括：

- `schema=1`、32 位 `run_id`、完整 live `identity`、严格整数 `generation`。
- 独立的 `adapter_manifest_sha256`，是全 manifest 的哈希，不嵌入 identity 形成自引用。
- `baseline_admission_total`，来自**开入口前**的真实屏障 snapshot，纳入冻结计划；不能以首次观察时的计数替代。现场基线来源仍须由实际适配器/控制器审定，不把用户填写的整数当现场证据。
- `duration_seconds`、`interval_seconds`、`max_checks`、`max_new_deliveries`。
- `receiver_stop_records/bytes`、`sender_stop_rows/bytes`、`minimum_free_bytes`、`sampling_seconds`、`stop_seconds`。

时限同时使用已持久化 UTC 结束时刻和本进程 monotonic 截止，取较短剩余时间。进程内时钟回退、单调时钟异常，或重启后 UTC 早于最后已记录观察时刻，都请求停止；初始化读取/绑定异常会关闭 Journal 锁，不留下同进程无法重开的锁。重启仅把剩余持久期限映射到新的 monotonic 起点，不能重新获得完整试运行时长。此设计仍不能证明不可信系统时钟在进程退出期间未被修改，也不能在观察器死亡后强制生产入口到期关闭，独立 watchdog/准入层期限仍是缺失的生产前置条件。

80% 警戒、90% 停止可以作为具体试运行计划的建议，但这里不修改运行时阈值、不授权自动生产停机。策略值必须在启用前明确，并扣除保留原件、别名最坏 reservation、在途和采样间隔可能增长；不可用未知业务速率推导保证。

**条数/时长是观察停止条件，不是准入层原子硬额度。** 两次采样之间、正在写入的任务，以及观察器被杀死/主机断电都会带来超额或失去监督。要承诺“绝不多收一条/绝不过期接收”，必须在全部入口共享锁下 reserve 原子条数和持久期限，并有独立 watchdog/到期拒收；当前生产没有这些保障，所以仍 blocked。现有硬存储上限本身不因该限制而放宽。

## 停止状态机

1. 达到阈值、时限、检查次数，或任何 stale/missing/error/identity/generation 异常时，先严格持久写入唯一 `stop_pending` 意图。意图写盘失败不发请求，也不称停止成功。
2. 首次仅调用一次 `stop(operation_id, reason, expected_generation)`。重复 tick/重启只能 `query_operation(operation_id)`，不盲目重发 stop，更不做 resume。
3. 确认须是同 operation/reason/identity/manifest 的严格持久 ACK，`previous_generation` 等于原值，`generation=原值+1`，`state=closed`，`durable=true`。HTTP 202、caller bool、未知操作、其他代次或其他 manifest 均不够。
   重启读到 `stopped` 也必须重验这份完整 ACK，并按 `run_id + capacity_stop` 重算 intent 操作身份；同时核对保存的 phase、次数、基线和时限。仅标签或合法 JSON 不足以确认。坏记录保留，拒绝构造或再次确认，不重发 stop、不追加成功记录、不用重查掩盖坏档；有效已确认结果复用也不追加或再次请求。
4. ACK 丢失、调用超时、查询不存在或适配器缺真实能力：`stop_unconfirmed`，保留意图和原件，提示不得恢复；不把未知状态当停止失败且安全开放。超时操作可能仍在远端执行。
5. 真实隔离 adapter 共享 flock 等在途自然退出后关闭 14 个模拟入口，持久原件支持失回执重查。ACK 明确 `scope=isolation`、`production_adapter_bound=false`；此证据不替代真实应用入口或生产版本核验。

## 验证与回退

```sh
python -B -m unittest discover -s tests -p test_handoff_capacity_observer.py -v
```

该独立 stdlib suite 覆盖真实私有 receipt/alias/index 读取不写盘、pending 原件保留、锁竞争、扫描预算、计数/时间/容量阈值、首样前新增不漏计、身份/时钟异常、held/到龄未确认、意图同步失败、ACK 丢失后只查同操作、真实隔离 gate 停止全部已列入口、production mode 明确不确认。实际文件/锁不是模拟 ACK；sender cursor 协议测试不冒充真实 PostgreSQL。原生资源受限执行与实际 PG job 的结果须另留来源、版本、资源、退出码。

独立 PG job 使用真实 PostgreSQL 执行同一份身份、分类聚合和 READ ONLY 事务 SQL；这证明 SQL 与该固定 PG/合成 schema 的兼容。它若通过 psql 执行，而没有安装 psycopg，则不构成 `sample_postgres()` 这个 DB-API Python wrapper 的真实端到端证明；wrapper 的连接关闭、事务顺序和错身份分支仍仅由 cursor 协议单测覆盖，不合并夸大这两级证据。

回退前仍需独立确认所有生产者消费者静止，保全 receiver receipt/alias intent/task index、queue/journal/终态与 sender outbox 原件。有 unknown receipt、held、跨契约未完成或 terminal/archive_pending 等危险状态不能直接退回旧应用。停止 receipt 不是安全回退授权；无静止能力、容量不足或原件保全失败即停，不能删除证据腾空间。
