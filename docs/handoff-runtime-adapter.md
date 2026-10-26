# 受控宿主入口：可执行隔离协议与尚未闭环的生产边界

本组件只允许 `scope=isolation`，没有 SSH、生产 CLI、部署、自动恢复接收、杀进程或清理入口。
它没有修改或重新构建 `.126`。现有生产版本、实际 cron 和 Miniflux 配置均未在本次读取。
普通 HTTP 202、环境变量、声明式清单和调用者的 `stopped=true` 都不能证明可靠停流。

## 已实现的实际控制

`scripts/handoff_host_cron.py` 是可以安装在隔离目录的固定 argv 包装器，必须同时携带
`durable_handoff_cutover.py`。目录 0700、数据和锁 0600、同有效 UID；登记同时绑定包装器与
严格文件读写依赖的源码哈希，以及实际 gate 目录的设备号、inode 和 UID。普通哈希只保证一致性，
不能抵御同 UID/root 同时改写全部原件。

隔离初始化需显式调用 `provision(root, manifest)`，仅接受全新目录。manifest 固定
`schema=1/scope=isolation/run_id/control_id/source_sha256/entrances/commands`；
`commands` 中每项是绝对可执行路径的 argv 列表，绝不交给 shell。该门只保护实际经它执行的进程；
把某个入口名称写进 manifest 不会让现有服务自动接入。

隔离 cron 的每条任务必须按下面的单行格式冻结：

```text
0 * * * * /absolute/python -B /private/frozen-wrapper.py --root /private/gate --command registered_job --isolation
```

覆盖清单必须含实际完整 crontab 的字节哈希和每条任务、包装器内容/权限/UID；所有非注释任务均须命中，
不能跳过“无关”旧 cron。`@reboot`、shell 拼接、重定向、未登记命令目前拒绝而不是猜测转换。
测试只在私有目录预演安装，没有执行 `crontab <file>` 或替换任何现存系统任务。

新增 `legacy_cron_binding(original, installed, replacements, wrappers)` 将旧完整 crontab 原始字符串、
新完整字符串、各自字节 SHA256、**每一条**旧行到新行及实际 gate 的登记 argv 同时绑定。
每条旧行逐字比较，调度字段不得改变；SHELL/PATH/MAILTO/TZ/CRON_TZ 顺序和值保持一致。
旧任务数量相同或命令名字相同都不算覆盖，任何额外旧任务、重复映射、未登记 argv、shell 拼接、
`@reboot` 等未知语法均拒绝。注释/空白也保留在完整字节哈希内，不以格式化文本替代原件。
`NativeDockerTransport(..., legacy_cron={"archive": <0600 JSON 路径>, "replacements": [...]})`
实际读取私有隔离归档 `{"crontab": "原始全文"}` 与当前隔离读取的 crontab，生成该证明；
plan 的 `cron.legacy_binding` 必须完全匹配。它不安装新 cron，也不证明未读取过的生产归档真实有效。

包装器先取得 activity 共享锁，再持久记录运行原件，随后才派生可能持旧 refresh/release 锁的命令。
派生子进程继承 activity 和本次运行所有权 FD。控制器先持久停止新派生，然后等待旧进程自然退出，
不先持 refresh 锁等待活动 lease，避免旧锁顺序互等。只有观察到子进程退出并严格写完 finish，才可能确认停流。
进程崩溃、超时或丢失退出观察会保留原件并拒绝确认，不能仅凭 PID 消失或内核锁释放认定任务已完成。

最多 256 条运行原件，不自动 GC；容量满拒绝新任务。单次进程等待/锁等待最多 5 秒。
超时不杀子进程，不补跑，且可能留下仍活动的子进程；退出观察缺失即使后来退出也不自动补造 finish。
这不是断电耐久性或任意守护化程序验证：登记命令必须在前台等待所有子工作结束，不可 fork 后提前返回。

## 真正的私有 loopback 控制端点

`scripts/handoff_control_endpoint.py --root <gate> --token-file <0600 JSON> --isolation`
启动 `127.0.0.1` 随机端口；token 文件严格只有 `token`（64 位小写 hex）。它直接调用实际
`HostCronGate`，不是返回调用者提供的“已停止”布尔值。

- `GET /registration`：读取实际源码/目录绑定。
- `POST /stop`：精确 `operation_id/generation/reason`，请求体最多 8192 字节，拒绝重复键、重复长度和分块编码。
- `GET /operations/<64hex>`：只查询原操作；可能恢复严格同步，**不是只读诊断接口**。

必须单一 Bearer token，固定路径，不转发、不重定向。最多 8 个处理线程，socket 超时 2 秒。
stop 是“持久 closed 后等待已登记进程结束”；未结束返回 409，不伪装成功。回执绑定精确 operation、
原代次/新代次、source、gate、入口清单和 reason，并要求 `durable=true/inflight=0/unknown=0`。
旧回执、bool 冒充整数、部分同步或未知操作都不能通过。

## 实际宿主读取与有限操作 driver

`NativeDockerTransport` 默认执行固定 argv：
`docker --host unix:///var/run/docker.sock inspect <完整容器 ID>` 与 `crontab -l`。
它检查实际完整 image ID、revision 标签、StartedAt、重启数、运行状态、角色、配置用户、全部挂载、
Memory 和 PidsLimit；限制不高于 768 MiB／256 PID。配置用户不是进程实际 UID 证明，标签也不是源码证明；
候选构建来源与镜像内容核验仍需独立证据。命令超时 5 秒，返回大小最多 4 MiB；大小检查发生在进程输出
收集后，不宣称这个阈值是子进程峰值内存硬限制。

随后读取实际包装器字节和权限、实际 gate，核对 authenticated endpoint 登记一致。HTTP 每次最多 2 秒、
64 KiB 响应；只允许字面 `127.0.0.1`，无重定向/重试。测试注入的 inspect/cron runner 只证明参数与协议，
不充当真实 Docker 或生产 cron 证据。原生 CI 的实际 Docker inspect 与资源核验须另看其原始 artifact。

`RuntimeHandoffAdapter(plan, transport, evidence_root)` 的 plan 冻结上述原始身份、完整入口清单、
controls 代次/绑定、cron 和最多 600 秒有效期。`stop_producers(op, reason)` 先核现场，再严格写本地意图，
每个控制只调用一次 stop，随后独立 query 同一个操作才保留回执。失败不会盲重发。
`query_operation(op)` 只查询已经留有 attempt 的控制；未尝试的控制列入 `remaining_controls`，
不会偷偷继续执行。已有冲突回执保留原字节并报错，不能用新查询覆盖。

成功返回仅为 `listed_producers_stopped`，同时固定：
`scope=isolation`、`production_adapter_bound=false`、`queue_checked=false`、`switch_authorized=false`。
它不是直接切换授权，不能当旧 CapacityObserver 的全局 closed 回执。

## 固定 Miniflux PostgreSQL 私有协议接线

`scripts/miniflux_handoff_adapter.py` 的 `MinifluxPauseControl(binding, client, inspect_current, evidence_root)`
接入真正 `miniflux-pg-handoff-v1`，不把 HostCronGate 换名字当 Miniflux。绑定字段为：

- `schema=1/scope=isolation/run_id/control_id`；
- `component_name/component`：与宿主适配器同形的完整 Docker ID、image ID、revision、StartedAt、重启数、角色、
  用户、挂载和资源限制；每次控制请求前后均重新调用 `inspect_current()` 核对，不能只接受预录 inspect；
- `manifest/application_source_sha256/source_sha256`：冻结的 PG manifest、构建源码锁哈希及桥接/依赖文件复合哈希；
  源码锁是候选构建证据绑定，容器标签本身不是源码内容证明；
- `generation/initial_operation_id`：当前明确 `initialize` 或 `resume` 的 OPEN 代次与操作，先精确查询验证；
- `entrances` 必须恰为 `miniflux_scheduler/miniflux_manual/legacy_async_sender`，不能塞其他入口冒充覆盖。

`client.request(method, path, payload)` 返回实际 HTTP 状态和 JSON；原生 harness 用固定 Python 镜像的短命 client
进入**该完整 sender ID 的网络命名空间**请求字面 `127.0.0.1` 私有端点，Bearer token 不入回执，不映射宿主公开端口。
client 必须保持有界、不得重试；单测注入的协议回复不算真实 PostgreSQL 或镜像证据。
`NativeDockerTransport(..., controls={"miniflux": bridge})` 将 registration/stop/query 委派到该桥。

`stop(op, generation, reason)` 在实际登记和 OPEN 原操作验证后，先严格落本地意图，才发一次 `POST /pause`。
OPEN 查询中在途计数可以非零——这不是“已排空”，仍需先关闭入口；pause 的 `409/confirmed=false` 原始响应会保留，
不伪装失败后的零任务。`query(op)` 只查同一原操作；仅在完整 PG 回执的 manifest、操作、action、前后代次都准确，
`durable=true/confirmed=true/state=closed/active_leases=0/uncertain_leases=0` 后，才写宿主格式严格回执。
原始 PG 回执与宿主投影同时留档，不能用普通 202、仅 registration 或调用者布尔代替。
身份漂移、未知 lease、旧未修补端点、源哈希改变、部分本地意图同步、冲突回执一律停止，不自动修复。
只有精确的 `409 + durable=true + confirmed=false + state=closing` 且操作/manifest/代次和计数类型全部匹配，
才抛专用 `PausePending`，允许既定有限窗口内查询原操作；不能捕获所有 `CutoverError` 当“还在等待”，
否则会吞掉身份变化、损坏或同步故障。没有零在途/零 unknown 的完整回执，仍不确认停止。
丢失 ACK 或进程重新创建后只 query，不再 POST；最多保留 32 次 query 原件，不自动清理，额度耗尽拒绝继续确认。

恢复另有 **显式隔离** `resume(approval)` / `query_resume(approval)`，不由 stop/query/构造器自动触发。
approval 必须绑定该 bridge、原 closed 操作及代次、不同的新操作、独立审批引用；先查询原 closed 回执，
再持久保存恢复意图，最后用 PG 自身的 `closed_operation_id` CAS 发一次 `/resume`，丢 ACK 仍只查原新操作。
此结构化审批是隔离执行输入，不是赋予生产执行权限的签名凭证；`scope=production` 永远拒绝。
HostCronGate 本身仍无 resume，不能靠重新 provision/改 state 偷偷打开既有门。

## 必须保留的生产阻塞项

1. **统一暂停已在本 PR 的固定 Miniflux `0002` 补丁实现，但尚未形成生产切换绑定。** scheduler、手动刷新和
   新版本跟踪的 legacy async 生命周期须以固定镜像真实验收为证；本适配器现已提供 PG 协议桥接，
   本轮原生联合案例只针对固定隔离实例及一个实际登记的旧 cron 子进程，**没有生产实例接线证明**。
   旧未打补丁实例的在途 goroutine 和未保留原文不可追溯，仍阻止直接切换；隔离入口命名、普通 202、
   outbox pending=0、仅停 scheduler 或声明式布尔量均不能替代这些证明。
2. **真实 ingress/旁路与旧宿主 cron 尚未安装核验。** 本轮没有访问生产，旧安装副本、容器直连、Compose 外
   生产者是否全部实际走 gate 仍未知。任何未登记/未封闭路径必须阻止生产切换；应用 fence 不能约束旧宿主副本。
3. **队列与库排空独立核对。** 入口停止后仍须按正确锁顺序核原始 spool、receipt/alias/index、continuation journal、
   SQLite 待发布和实际 PG outbox/held/拒收补偿。lease=0、closed 或 gate receipt 均不能代替这些数据。
   本组件不读/清生产库，不放宽原门禁。
4. **不可自动回退/恢复。** 代次、镜像、旧契约未完成续作、unknown ACK、held 补偿或终态证据有任何缺口都停在原处。
   回退必须另行保全原件并核兼容性，不删除 terminal/archive_pending 或 receipt 以“恢复”。失 ACK 仅精确查询，
   不盲重发；部分控制尚未尝试也不等于可以无审批恢复接收。

## 回归与证据解释

`python -B -m unittest discover -s tests -p test_handoff_runtime_adapter.py -v`
覆盖实际文件同步、实际子进程握手、在途自然退出、进程崩溃留下未知状态、安装后的包装器执行、容量/权限、
失回执只查询、部分控制、冲突原件、身份变化、精确 cron，以及真实 loopback 验签/错误体/超限/严格 stop-query。
设置 `AI_FILTER_CUTOVER_EVIDENCE_DIR` 可指定私有 fixture 留档父目录，测试不自动删除原件。
本机 sandbox 禁止 loopback bind 的 EPERM 必须原样留档；不能 skip 并称 HTTP 已过。
通过这些隔离测试不证明真实生产入口全覆盖、业务 drain 完成或模型最终结果正确。

另运行 `python -B -m unittest discover -s tests -p test_miniflux_handoff_adapter.py -v`，覆盖精确 PG 协议投影、
在途/unknown、旧端点、代次/manifest/bool 混淆、身份漂移、丢 ACK、同步失败、原件冲突、32 次 query 硬限及独立恢复审批。
这些注入协议的单元测试不能取代固定镜像真实 PG 场景；对应原生案例与完整 CI 状态须按最终 head 原始 artifact 报告。
