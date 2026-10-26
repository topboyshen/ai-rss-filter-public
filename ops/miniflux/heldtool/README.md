# held review：仅隔离执行

这不是生产运维 CLI，不能以它批准生产停流／补发。它不修改固定上游补丁，也不编入 Miniflux 镜像。生产 adapter 尚未实现，执行边界是本机 `synthetic_outbox` 与 `held_isolation_<随机hex>` schema；所有 HTTP 只能到 `http://127.0.0.1:<port>/webhook`（或 localhost），不跟随重定向。**仅允许在独立隔离容器网络中使用**，不能把 localhost 当成合成服务的证明：原消息发送前还有最多1秒、无原文／HMAC的 fixture identity GET，必须匹配独立批准且与 PG marker 一致的 `receiver_identity` 和 `endpoint_sha256`。不匹配则原消息 HTTP 为0；控制探测与原消息请求数分别留档。标记不是抵抗有数据库写权限操作者的认证系统，禁止用生产服务伪装 fixture。

## 精确动作

- `retry_once`：只允许 held、未到七天、原 attempts < 20。同步事务 CAS 原行，原 attempts 加一、写 `manual:<operation_id>` lease，**state 仍是 held**。之后最多一次原 body／原 HMAC POST；普通202、503、超时、未知结果都不能 ACK，也不会返回 pending 自动重试。
- `confirm_receipt`：签名不变、原 body 不变，向相同 endpoint 的 `/webhook/receipt` 发一次 POST，必须200且严格 durable/body/ID 对应。可用于超龄／耗尽行，因为不重复评分；404不证明没执行。查询可能写接收端确认状态，不称为只读。
- 人工领取后进程中断：原 attempt 已计数且 held/manual lease 留存。新的 retry 计划被拒，只允许显式 receipt-only 恢复，并要求新的停止证明确认**原人工 operation 也已停止**。不靠租约过期自动放行。
- 领取、结束各追加一条安全审计；数据库完成提交前不报告成功。网络收到准确 ACK、最终审计失败时仍隔离 held，不声称交接状态已提交。只留响应 SHA256，不保存完整响应。

## 计划、证明与批准

`HELD_ISOLATION_DSN` 仅从环境读取，不放到命令参数或结果中；工具拒绝其他数据库名、地址、schema 和连接选项。普通 plan 路径只读隔离 PG 一条原始 outbox 行，输出 `plan` 和 `plan_sha256`，不输出正文、签名和完整 URL：

```sh
held-isolation-review --action retry_once --delivery-id <64hex> --operation-id <32hex> --database-identity <64hex>
```

将返回的 `plan` 对象独立存为私有文件，再提供隔离控制器的停止证明；`--review --plan ... --proof ...` **完全离线**输出批准模板及 Go 规范序列化的对象 hash（不是原文件字节 hash）。模板两个人工确认默认 false；审阅原始身份和未知执行风险后才可逐条批准。执行还要重新读取数据库 CAS，因此审批后行发生变化会拒绝，不会自动更新审批。

停止证明 schema=1，mode=`isolation`，绑定 run_id、operation_id、入口清单 manifest_sha256、database_identity、adapter_identity=`synthetic-pg-stopped-fixture-v1`、receiver_identity、endpoint_sha256、observed_at（最多60秒前、最多1秒未来）、producers_closed、sender_stopped、no_active_sender_leases；崩溃恢复另需 stopped_operation_id。真实隔离 PG marker 必须逐项匹配，且无自动 inflight claim／其他非工具数据库客户端。**DB 客户端和 lease 检查不能证明外部生产者都已停止**；证明只适用于明确列举、实际停止的合成 fixture。HTTP 切换 fixture 没有 PG marker，不能用于本工具。原行 contract 只用于身份不变核对，不能证明接收端仍在原策略，生产跨契约恢复禁止由此工具自动放行。

`--execute --plan ... --proof ... --approval ...` 才执行一个操作；三个文件必须私有普通文件、<=64KiB，拒绝重复键、大小写别名、未知字段、尾随对象。生产模式直接拒绝。目录权限、文件 owner 和审阅人的身份仍是隔离操作者责任；这不是认证服务。

## 容量、证据与权限

实际 outbox 使用**精确固定源码的 migration SQL**，测试先验证文件 SHA256，不另写简化替身。工具的 marker／audit 表仅由合成测试 provision，CLI 不安装任何表。audit 只追加，UPDATE／DELETE 被触发器拒绝。每个领取保守预留两个记录／8192计费字节；上限10000记录、16MiB计费（约2048个操作先触字节上限），崩溃后的结束预留不释放。不是 PostgreSQL 实际磁盘容量证明。

行与字节满额、审计写入错误、状态漂移或不满足停机证明，在 HTTP 前回滚。工具不清理 outbox／audit，不改变原5000行／256MiB／七天规则。隔离 PG volume 和日志保留；CI 临时 volume 随 runner 消失，上传的合成 SQL dump、测试日志、资源 inspect 和 SHA 清单才是持续证据。

没有 production adapter、没有生产维护迁移／审计归档机制、没有实际生产入口覆盖证明。将来上线必须另审这些项与权限，不能把测试 marker 手填到生产冒充授权。对已无 exact payload 的旧新闻仍不可自动补偿。

## 验证

`go test ./...` 默认跳过真实 PG 测试；必须提供 `MINIFLUX_OUTBOX_TEST_DSN` 和 `MINIFLUX_PATCHED_SOURCE` 才能验证真实 CAS。专用 CI 先验证／应用锁定上游补丁，host 编译测试，再在固定 PG service 的384MiB／64PID约束内运行，保存失败证据。编译不在这个 cgroup；不是性能承诺。`scripts/verify_miniflux_held_local.py` 用本机 Colima 独立内部网络／新卷做同源真实PG核验，保留卷，无生产挂载。

覆盖一次传输、精确HMAC、并发／重复审批、20次／七天边界、普通202／404、真实10秒超时、未知连接中断、准确回执、人工claim断连重开、发送成功后最终审计失败、sync_commit、容量与审计错误。断连重开不是机器断电，SQL故障注入不是磁盘真实ENOSPC；这两个限制明确保留。
