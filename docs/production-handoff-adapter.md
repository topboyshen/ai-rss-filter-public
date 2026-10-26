# 生产入口切换适配器：当前仍阻塞

本次静态锚点是合并后 `a5ffd64fa987f3a64676478c05a212aa8146418a`，tree `9a9be370acbcf59a82ffbe13075f0529f4faafa1`。本地准备工作区的 snapshot HEAD 不等于远程 SHA，不能据此改写版本归属。`scripts/production_handoff_adapter.py --source-root <tree>` 逐文件输出实际 SHA256 和代码行号；不导入应用、不读数据库、不启动模型、不执行 SSH/Docker/发布命令。输出是静态证据，不是生产现场状态。

## 为什么现在不能直接执行

现有代码没有跨所有入口的共享 admission 屏障：

- `app.py` 的 `before_request` 负责 webhook 验签和 OPTIONS，不是全入口停流登记。
- `routes/feed_routes.py` 的 `/filter`、`/filter/multi` 冷路径可直接调用 `process_feed`；局部并发信号量不受自然刷新 flock 控制。
- `routes/admin_routes.py` 的 `/refresh`、`routes/dashboard_routes.py` 的 briefing LLM 可自行入队；`/feedback` 还会写库并触发投影。
- `services/refresh.py` 的 `.refresh-execution.lock` 仅约束使用它的路径。`routes/webhook_routes.py` 的 spool flock 序列化队列变更，但不是所有 API 请求/后台生产者的屏障。
- `worker_main.py` 会自行恢复后台子系统、补投首页任务；只停外部 NGINX 入口不等于这些生产者停止。
- cron 维护哨兵覆盖定时刷新/增强，不覆盖手动调用和全部写路径。Compose 内 API 暴露5000端口，现有 gateway 是统一转发；不能凭更改 gateway 就认定直连、已有连接和旁路关闭。
- 本 Compose 不提供实际 Miniflux 旧异步发送、外部 cron/手工进程及网络旁路的完整身份和在途证明。旧请求没有冻结原始 body 时，当前 entry 行不能重建原消息身份。

因此生产模式和 `--execute` **始终拒绝**，没有 `force`、环境变量或调用者 `all_closed=true` 可绕过。新增脚本名称中的 production 表示它审查的目标，不表示该适配器已具备现场执行资格。保持生产及 `.126` 不动。

## 接口与可运行的隔离实现

`ProductionHandoffAdapter(root, manifest, mode='isolation')` 只有显式私有隔离目录可运行；须先 `init_isolation` 创建全新目录，绑定 manifest SHA、run_id、adapter 文件实际 SHA、完整入口清单与隔离身份。

- `snapshot(expected_identity, generation)`：持有真实 release、refresh 和 admission flock，读取严格私有文件原始清单；返回 identity、generation、锁 inode、`admission_total`、原门槛计数和逐条文件证据。不是接收调用者传入的零计数或布尔声明。
- `stop(operation_id, reason, expected_generation)`：隔离生产者进入实际 SH admission 临界区并一直持有到在途工作结束；停止持 EX，自然等待原请求完成后先同步 transition，再同步 closed 状态及确认回执。新请求不再进入；未完成 transition（包括残缺 pending）也会拒绝新请求，不能因重启看到旧 open 状态而漏接。
- `query_operation(operation_id)`：只确认对应原操作，校验 manifest/身份/generation/closed 状态，重新进行严格同步；完整 transition 可恢复同一次停止，不重发另一停止、不增加 generation。残缺、冲突或同步失败保留原件并拒绝确认。
- `verify_switch_gate(...)`：要求入口关闭、全部原零门槛满足，但返回 `switch_authorized=false`。锁释放后的快照不是“稍后可以切镜像”的许可证；本模块没有 deploy 方法。

ACK 字段包括 `state='closed'`、`durable=true`、`operation_id`、`reason`、`previous_generation`、`generation=previous+1`、完整 identity 和 **adapter manifest 自己的** `manifest_sha256`。容量 observer 应另绑定该 SHA，不把 observer policy 的 SHA 当 adapter manifest，也不构造循环哈希。

状态、transition 和 ACK 的代次均须为严格非负整数且不超过 `2^63-1`，布尔值或等值浮点数不能冒充代次；停止后的代次必须恰好加一。状态入口集合必须完整且一致，损坏的代次/部分入口状态不自动“修好”后确认，而是保留原件并拒绝接收、快照或停止恢复。

真实隔离计数来自保留的 admissions 文件，每条校验原文 SHA、入口和完整身份；重复消息计数不增加。最多512份 admission+work 文件，scandir 到上限即停止而非先无界列目录；单条合成请求64KiB，base64长度在解码前检查。原件不自动清理。隔离 work 清单含 queue pending/inflight、journal pending/running/commit_pending、archive/terminal_pending、active_requests、旧路由在途/未知、拒收未补偿、原文缺失、buffer在途/不确定、旧异步在途/未保全及未知生产者。它不是对真实应用 spool/journal 格式的已完成采集器。

组件核验函数 `validate_inspect` 接受原始 docker-inspect 形状并逐组件核完整ID、Image、revision、StartedAt、RestartCount、Running及OOM状态；调用者布尔健康声明不能代替它。当前没有生产采集器，测试里的 inspect 是明确的合成原始记录，不能称现场组件已验证。

## 隔离运行及证据

```sh
AI_FILTER_HANDOFF_EVIDENCE_DIR=/private/synthetic-evidence python -m unittest discover -s tests -p test_production_handoff_adapter.py -v
```

仅 stdlib；测试保留独立目录、原文、transition和操作回执。真实子进程持有 admission SH 期间，stop 会超时且不确认；子进程自然退出后同门槛可闭合。不杀未知进程、不发模型请求、不运行真实发布。目录同步失败、ACK丢失、旧状态恢复、身份变化、未知生产者及每种原零门槛都有负例。文件同步/进程级恢复不等于物理断电耐久性。

## 若另获授权，必要的最小应用补改范围

1. 增加共享 admission 服务，采用宿主/容器都绑定的同一 inode、代际状态和在途持有协议；新请求的登记、接收与停流必须互斥，不能只有检查文件后再开始工作的 TOCTOU。
2. 将真实 API 所有评分、入队、feedback写库入口及 worker内部生产者接入该服务；需明确缓存纯读是否放行。增加跨进程并发、请求中断、异常、重启、不可用状态 fail-closed 测试。已进入请求自然完成，不强杀、不重新评分。
3. 另绑定实际 Miniflux 定时/手动/旧异步发送、可靠原文保全和真实网络入口/旁路；无法观察或冻结旧请求原始 body 时停止，不用“等了10秒”或“schema里有entry”替代。
4. 宿主 adapter 在同一持锁临界段核实际旧版本、release/refresh锁、全原始活动状态和发布追平输入，再将锁传给唯一发布操作；继续保留单次切换和ACK不确定时只查询同operation的约束。

以上应用 hook、现场 backend、真实组件切换与 Miniflux/schema132 回退审定均未在本补件中实现。必须另获批准后修改和验证，不能仅通过隔离测试就宣布生产停流/部署闭环。`.125`、旧 `.126` 及旧sender不能冒充持久协议的无条件回退目标；所有终态证据仍须保留。
