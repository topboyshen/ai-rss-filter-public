# 已接入屏障的接收端：有界停新与冻结控制

本补件提供**接收端局部能力**：宿主对已有 admission fence 的真实 `stop_new`、`freeze` 和同 operation 查询。它不完成旧 `.125` / 旧 Miniflux 的首次接管，不签发切换许可，不修改现有 isolation controller 的范围，也不开放生产迁移、启动、重启、回退或重新接收。

## 可执行的边界

`python -m services.receiver_handoff_control --token-file /private/token.json --port PORT` 仅监听 `127.0.0.1`。必须已有 `AI_FILTER_ADMISSION_MODE=enforced`、明确 `AI_FILTER_ADMISSION_ROOT`、与 manifest 一致的 `AI_FILTER_ADMISSION_BINDING`；缺失或 disabled 拒绝启动。启动不会 provision 或 open fence。token 文件为当前用户所有的普通 0600 文件，内容仅 `{"token":"64位十六进制私密token"}`；凭据不进入 plan、intent、回执或日志。

宿主 `scripts/production_receiver_driver.py` 是供审定编排调用的库，没有任意命令、shell、SSH、部署或启动 CLI。`NativeTransport` 只执行固定 `/usr/bin/docker --host unix:///var/run/docker.sock inspect` 和经过身份认证的 loopback HTTP。inspect 同时限制输出字节与时间；HTTP 使用总时限，逐字节响应不能重置它。最多 8 个控制连接，连接总时限 3 秒，单请求 8 KiB、响应 64 KiB。超时是结果未知，不是远端取消。

`ReceiverDriver` 的 plan 明确：

- `scope=instrumented_receiver_only`，源码及实际导入的校验/持久化/容量辅助模块 SHA256。
- 全部接收端 web、worker 的完整容器 ID、镜像 ID、revision、角色、用户、StartedAt、RestartCount、内存、PID 和 mounts。
- 实际 `AI_FILTER_ADMISSION_MODE=enforced`、绑定 JSON、`WEBHOOK_SPOOL_DIR`、容器 fence/spool 路径与宿主 bind 源；支持共享 data 根目录 bind，并检查更深挂载是否遮蔽路径。重复 Env key 直接拒绝。两个角色必须共用同一个已核对 inode 的 fence。角色可以有多个实例，每个实例必须单独列出，最多 8 个。
- 控制注册、fence manifest SHA、宿主 fence/receipt spool 路径和设备/inode/用户身份。
- 具名责任人、最长两小时且不超出 fence 自身窗口的操作期限，以及 receipt 条数/预留字节/实际可用空间停流阈值。

这些绑定不是自动发现全部部署的证明。遗漏容器、外部脚本、cron、旧 sender、旁路请求等仍必须由独立生产清单审定；本局部库不能把调用者只列出的容器说成全入口。

## 停新、自然排空、冻结

`operate("stop_new", operation_id, reason)` 先保存唯一 intent，再发一次真实请求，并通过另一条 GET 核对同 operation 的严格回执。已有 intake lease 可自然完成；已持久任务继续取得 `drain` lease。停新不取 activity EX 等待全部活动结束，因此不会用停止消费者来假装完成排空。

`operate("freeze", different_operation_id, reason)` 只在本 controller 已确认 stop_new 且 fence 处于 draining 时允许。冻结阻止新 drain lease，并非阻止已有持锁任务自然结束。已有活动尚未完成时结果不确认，保留 intent；随后只能 `query(original_operation_id)`。残留 orphaned/uncertain lease 使冻结保持不确认，不自动删除，不重评、不回退。

**freeze 的 `activity_drained=true` 只说明 lease 完成，不证明持久工作为空。**所有回执固定 `queue_checked=false`、`switch_authorized=false`。原有 `independent_drain_inventory.locked_inventory` 所要求的普通 queue、receipt/alias intent/index、continuation、terminal/archive_pending、sender outbox/legacy/held 和发布水位检查仍必须在真实 release/refresh/activity/storage 锁下完成，并持续持锁到审定切换完成。这个库没有取消该门槛，也没有调用任何镜像切换。

一次 controller 目录最多执行一次 stop_new、一次 freeze，每个操作最多 32 次查询；最多 32 次容量观察。所有尝试都持久计数，重启不重置预算。未知结果不换 operation、不重发 POST；查询在变更窗口过期后仍可执行，但只查原操作，且前后重新核对实际运行身份。完整且一致的本地回执 pending 可通过同操作查询同步；损坏/冲突文件保持原样并停止。

## 有限容量及拒收边界

`capacity_tick(operation_id)` 在真实 spool SH flock 下读取 receipt 主记录、alias intent/index 和所有保留终态的预留计费。不调用会恢复写盘的 receipt HTTP，也不删除原件。最多 4096 条、256 MiB 的硬容量未改变；停流阈值必须小于 4096，字节阈值必须留足最大 alias reservation，物理空间也必须保留同样余量。读失败、部分文件、阈值或观察次数上限触发接收端一次 stop_new；无法确认时抛出错误并保留精确 intent。

这是抽样停流，**不是**观测间隔内所有生产者的预留协议。接收端 fence 自身的条数/声明字节/时间预算和 ReceiptStore 原有 reservation 检查继续提供实际拒绝边界；HTTP 503 或容量拒绝不等于已持久接收。sender 必须在发送前持久保存精确原文，并在拒收/ACK未知时保留原件。此接收端局部补件不停止 sender，返回 `sender_stopped=false`；不能据此长期打开收件或承诺无限无损。

停止后无 reopen API。修改预算、清除证据、重新 provision、重签 held 原文或换回旧镜像都不是恢复手段，必须另有审定操作。

## 首次接管仍然阻塞的事实

旧 `.125` 与旧 Miniflux 没有这些屏障和完整 raw-original 登记。新控制不能追溯证明此前所有异步请求的精确原文已保存。当前 entries、queue=0、普通 HTTP 200/202、空 lease 目录、闲置 flock、等待一段时间或运维人员填写 `legacy_originals_proven=true` 均不构成这项证明；plan 明确拒绝该布尔值。

因此现有旧在途原文无法观察或可靠冻结时必须停止首次接管。需要一个单独审定、具备原始字节来源和完整生产者覆盖的旧路径保全方案；没有这种来源时不能重构或补造。即使该问题另外解决，以下工作仍不在本补件的完成声明内：

1. sender 独立停新、保留 outbox 消费自然排空、最后冻结，以及全部旧 cron/手工生产者身份绑定。
2. 新候选与实际上一镜像的双向兼容、旧原文/held 的明确处置；所有不确定原件继续阻断直接旧镜像回退。
3. 真实持久 DB 的迁移凭据、独立多 worker 启动/重启许可和唯一执行恢复。现有 `controlled_startup` 的 isolation/temp/pristine 限制保持原样。
4. 在原资源上限下实际候选及 sender 联合验证，独立完整库存重查、生产现场入口/身份核定，随后才可能考虑另行审定的开启窗口。

## 验证范围

`test_production_receiver_driver.py` 使用合成私有文件、真实子进程、实际 loopback HTTP，覆盖停新允许自然 drain、冻结未知、丢 ACK、重启后同操作查询、查询/采样预算、alias/终态计费、存储满、凭据与身份变化、旧组件拒绝、慢响应与输出上限。普通单测的 Docker inspect 行是显式合成数据，不是生产 Docker 证据。

`verify_production_receiver_native.py` 在 Linux AMD64 中启动两只固定 Python 镜像的合成容器，各限制 768 MiB、256 PID、无网络、只读根目录、drop ALL capabilities。它使用实际 Docker inspect/env/mount、同一宿主 fence inode 和真实控制 HTTP，验证容量停新→原任务自然完成→消费者 drain→freeze；保留实际 inspect 和全部原始证据。它没有运行生产候选、没有 PostgreSQL、更没有读取生产数据；PG 上限未改变。本地非 AMD64 环境拒绝运行，该证明须由原生 hosted workflow 产生。

现有 `.github/workflows/handoff-operations.yml` 增加这些源文件的触发路径、合成单测及实际容器 fixture。通过该 workflow 仍不代表生产首接管、旧镜像兼容、物理断电耐久性或生产发布完成。
