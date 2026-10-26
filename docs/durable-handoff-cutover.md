# 持久交接的受控切换：隔离协议与现场缺口

本目录实现的是**可执行的隔离 adapter 协议**，不是已完成的生产发布控制器。`scripts/durable_handoff_cutover.py` 没有 SSH、Docker、shell、生产进程终止或回滚能力；只接受 `mode=isolation`、`production_adapter_bound=false`、精确 `127.0.0.1` endpoint 和 `synthetic-new-*` 两组件身份。生产运行被硬拒。没有改生产、旧 `.126` 镜像或调用模型。

## 可执行的内容

`scripts/durable_handoff_cutover_fixture.py` 在独立进程运行认证 loopback HTTP 服务，使用私有目录中的真实文件保存原始请求、缓冲、在途、终态保留件及操作回执。它不是把一组 boolean mock 当生产证明：测试会实际发送 HTTP 字节、读取落盘原件、制造延迟/丢失 ACK、异常退出并在同目录重启。不过 `switch_receiver/switch_sender` 在此 fixture 中只切换合成组件状态；**没有启动实际 AI-Filter、Miniflux 或执行 PostgreSQL schema 迁移**。两款真实镜像的联合协议验证是另外一份证据，不能替代生产入口适配。

运行 stdlib 回归（无需 pytest，也无外网）：

```sh
AI_FILTER_CUTOVER_EVIDENCE_DIR=/path/to/private/evidence python -m unittest discover -s tests -p test_durable_handoff_cutover.py -v
```

证据目录保留各测试独立子目录、所有合成原件、stderr、CAS journal 和停止结果。默认使用系统临时目录且不主动删除。测试只关闭自己创建的确切 `Popen` 子进程；先认证 `/shutdown`，无响应时只对该自有 fixture 使用 TERM，不使用 KILL。控制器没有该清理能力。失败及超时不包装成成功。

## 冻结协议

manifest 绑定 run_id、adapter 文件 SHA256、认证 token、两组件完整合成身份、完整入口清单及预算。认证 token 只放 0600 私有 manifest，不写普通日志。journal 使用 0700 目录、0600 单链接常规文件、进程 flock、递增序号、前项哈希与 CAS；文件 fsync、rename、父目录 fsync 都完成后才推进。完整 pending 可按原字节恢复；残缺/冲突 pending 保留并停止。重新启动不得修改 manifest 或延长原总截止时间。

阶段固定为 `prepared → paused → drained → receiver_switched → sender_switched → verified → resumed`。每个改变状态的操作先持久记录 intent，再发一次 POST；操作 identity 由 run_id 和 action 得到。ACK 丢失或进程退出后只查同 operation_id 的精确回执，不盲目重发。adapter 同锁内再次核零门槛和 generation，原子持久 envelope 关联组件状态与操作回执；重启恢复该原 envelope，组件切换次数仍最多一次。仅普通 202、部分 JSON、错误 run_id/source hash、非确认回执或 generation 不符都不能推进。

预算可调低但有硬上限：总计 60 秒、排空 30 秒、每次 HTTP 2 秒、最多 100 次只读排空轮询。fixture 回归采用更小预算。HTTP 用绝对 deadline、read1 有界响应和 socket shutdown watchdog，慢滴流不能靠逐字节刷新 idle timeout 延长预算。超时只停止控制器；不证明远端操作未完成、SQL 已取消或任务已经停止，下一次只能在原截止内显式恢复并查询操作回执。

原生隔离运行 `39871927168` 中，14入口批量转交的正常测试使用200ms请求预算，客户端未取得恢复接收的确认而停止在 `verified`。留档中 adapter 已为 generation4、入口开放、组件各切换一次，14条回执及保留原文逐一匹配；无OOM或adapter异常日志。这只能证明客户端未确认而远端已提交，没有逐次fsync耗时，不能把超时全部归因于磁盘。原失败证据保留；仅该正常测试在首次创建journal前将请求预算冻结为已有硬上限2秒，控制器、硬上限和生产门禁未变。其余测试仍用200ms，包括慢滴流与丢ACK负例；恢复接收丢ACK测试另核一条真实缓冲原文在查询同operation后不重复转交。

## 全入口屏障与原门禁

入口清单不可裁剪：Miniflux 定时、手动、旧异步发送；webhook 及绕行入口；自然 cron、手动刷新；`/filter`、`/filter/multi` 冷评分；briefing LLM、feedback、内部队列、cron 维护及 Compose 外生产者。真实现场需要逐个绑定路由/进程/锁与旁路，不把“停 Miniflux 轮询”当作旧 goroutine、HTTP 在途或其他生产者已停。

原零门槛保留，并在 adapter 锁内与切换同一临界段复核：queue pending/inflight、journal pending/running/commit_pending（含未到期、其他旧契约和已评分待写入）、archive_pending、terminal_pending、active_requests、old_route_inflight/unknown、rejected_unresolved、missing_originals、buffer_inflight/uncertain，以及旧异步 in-flight、未保全原文、未知生产者。任何非零、缺项、bool 伪装数字或身份不明均停止。历史已确认终态保留件不要求删除；`due=0`、短时空队列或只有 health200 都不替代原门禁。现场还须绑定发布锁/刷新锁、输入追平发布和原发布身份核验，此 fixture 不声称已经完成这些真实绑定。

暂停后各隔离入口的新字节进入严格同步缓冲，只有可靠落盘才返回 fixture 的 202 回执。容量有界（fixture 每目录最多16条、1MiB、单条最多64KiB）；满额或同步失败返回503，既有原件不删。真实发送者对拒收必须保留冻结 outbox，普通旧发送者的 202/非202行为不能由该 fixture 替代。缓冲转交后保留原字节与哈希，再恢复入口；未确认转交只停止，不清空缓冲。

旧异步发送必须有精确原始 body 与 SHA 身份，不能用当前 entry 行重新生成“同一消息”。缺原件、尚未确定去向或仍在运行时停止；保留的原件也不表示真实旧 sender 已停。10秒客户端超时、30秒 lease 等参数不能独自证明旧 goroutine 结束。真实旧发送流的无丢失证据仍需要现场 adapter，不能靠本合成列表代替。

## 单次切换与回退边界

实际发布仍需另外授权、锁最终 SHA、完整 CI、正式干净门禁、单次构建、候选与已保存实际旧镜像兼容核验，切换前旧契约未完成续作为零。接收端与新 durable sender 必须同批审定；`.125`、`.126` 不具备新持久回执协议，不能冒充可无条件退回的候选。本控制器不包含自动回滚或 `rollback` 操作。

sender schema132 及已冻结 outbox 存在后，不能直接换旧 sender、降 schema、删除 outbox 或把 held 重置重发。新接收/执行、跨契约 journal 或未确认归档出现后，必须另行静止生产者消费者、独立保留数据库/原始 outbox、receipt、journal、负回执与原 Compose，再逐项核兼容消费者和回退范围；切回代码不恢复被终止的任务。保全不等于允许重新评分或增加预算。

## 尚未实现、不得混报的部分

- **生产 adapter 尚未绑定**：真实入口路由切换、发布/刷新锁、组件进程切换、数据库身份和 migration、原始活动枚举、已部署镜像及回退材料现场核对都未由此脚本执行。不能直接把 endpoint 换成 JD；校验会拒绝非隔离模式。
- 没有 PostgreSQL，也不生成可供 held 工具使用的 `database_identity`。sender held 工具的真实隔离 PG marker/proof 是独立协议，不能将此 fixture snapshot 拼成现场停止证明。
- `plan_webhook_receipt_trial.py` 的冻结容量/时间/条数建议是独立规划工具。当前控制器只完成切换阶段，没有恢复接收后的持续容量观察器或自动限时停流器；不得宣称试运行最早阈值停止已经实现。部署建议必须同时说明该缺口。
- 没有真实评分、物理断电、文件系统控制器缓存持久性、生产流量丢失率或生产性能结论。进程异常退出/重启只验证进程级恢复。原生 CI 的资源/架构证据也不能外推生产切换耗时。

结论：这份实现可审查、可跑隔离回归，但**不能据此单独批准生产切换**。保留原门禁，现场 adapter 和容量停止边界准备完整后再审执行计划。
