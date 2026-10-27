# 受控生产激活与首次关闭初始化

本协议补充 [生产迁移与闭门启动](production-startup.md) 的后续阶段：在同一批已获授权、
已完成闭门初始化的真实进程内准备业务，再打开接收端，最后恢复发送端。
代码和测试夹具提供的是可审查的执行路径，不代表任何站点已经迁移、激活或部署。
实际 Go、PostgreSQL、容器与完整联合场景是否通过，必须引用同一最终源码的真实 CI 证据；
本文不预先登记验证结果。

## 能力与兼容边界

| 状态或能力 | 可以证明 | 不能证明 |
|---|---|---|
| Miniflux schema 134 的 `pause` / `stop_new` | 原有受控停流、消费者排空阶段及其精确操作 | 首次从未开放的初始化、生产 activation 能力 |
| schema 135 的 `initialize_closed` | 首次可见 gate 为 `closed`，generation 为 0，操作与状态同事务提交 | 队列为空、历史发送全部可追溯、旧生产入口已被外部围栏隔离 |
| 接收端 `initial_closed` | 新 fence 从未开放，仍为 generation 0 / `frozen` | 历史生产者都停止、旧数据库可安全迁移、部署许可 |
| 各进程 `business_prepared` | 原 owner/PID/容器、所需业务线程、依赖和实时 pulse 仍匹配 | 所有现场入口安全、历史兼容性、模型质量或业务正确率 |
| 接收端 confirmed resume | 同一 fence 的新代次确实开放，原 activation 确认仍有效 | Miniflux 已恢复、整个站点已完成部署 |
| `verify_active` | 当前候选容器与实际运行合同、原创建及 grant 链匹配 | 发布登记完成、sender 已恢复、历史数据无损 |

迁移 135 只追加允许的操作类型，不改写 132、133、134 的历史 SQL，不改旧回执、原始消息、
held、legacy 记录或 attempts。发送端读者只识别精确的 134 和 135；空白、前导零、未来版本
或非规范 marker 不会被转换为受支持版本。新初始化与生产恢复只接受规范文本 `135`。
原 `/registration` 和 134 停流路径保留；旧 `/initialize` 仍表示显式 OPEN，不能用于首次 CLOSED。
旧 `/resume` 不接受 `initialize_closed` 作为前置关闭操作。134 的兼容边界指既有停流契约与库存读者，
不表示新增 Go 二进制可以跳过其启动时的 schema 升级检查；新增 activation 必须完成 135。

135 是前向迁移。禁止把 marker 降回 134、删除新操作、重写旧回执来迁就旧执行文件。
实际旧 `.125` 镜像的运行和回退兼容性必须另有独立证明，不能从源码、旧测试、标签或
“新版本能读旧数据”推导出来。本文不提供自动回退到 `.125` 的分支，也不变更 `.126`。也不能依赖 marker 135 让旧 134
sender 自动拒绝启动：旧源码的 schema 检查采用最低版本判断，版本号更大不代表旧二进制理解
`initialize_closed` 或能参与新 activation。这里是源码兼容边界，不是已执行旧镜像的验证结果；
旧 sender 的自动回退同样没有授权路径。

## 执行前的实际绑定

执行者必须持有完整、仍有效的私有发布合同和对应的源、镜像、配置、凭据引用及现场入口证据。
发送端绑定包含固定源码锁、实际容器 ID / 镜像 / revision / 启动时间 / restart count / 资源、
manifest、PG 集群及数据库/schema/OID/用户身份和原操作。新源锁不等于已构建或部署新 sender 镜像。

接收端迁移许可还绑定实际 SQLite 文件、WAL/SHM、支持的起点与目标 schema、原始库存、
外层锁、fence 和有限窗口。不是传入 `ready=true`、`empty=true` 或一份旧采样即可放行。
`PostgresReader.locked()` 持有真实 sender activity 与 metadata 锁，检查实际库存与确认关闭操作；
135 的 generation 0 `initialize_closed` 可以作为关闭边界，库存中的 held、legacy、pending、
inflight 或未知 lease 仍然阻止把它当成已排空。

首次 fence 和首次 sender CLOSED 都不能隔离仍在运行的旧未 instrumented 入口。
旧入口的外层停止/隔离与旧应用已安全停止，是这条路径的先决条件；当前 container lifecycle
不会替操作者停止旧实例。无法核实现场入口、PG 身份或容量时应停止，不能靠新 fence 的空目录
补齐这份证明。接受 T0 前历史缺口未知，也不等于接受已知 held 原件被丢弃或跳过库存门禁。

## 固定的阶段顺序

1. 固定发布、源与镜像合同，核实外层入口边界和容量。旧应用容器保持停止且保留；新候选使用
   独立服务名，不能复用或重建原容器。发送端若尚无 gate，由真实新 Go 服务执行
   `SenderActivationDriver.initialize_closed(operation)`；恢复仅使用 `query_initialize_closed`。
   接收端新 fence 使用 `production_initial_fence.confirm`，不先 `open_trial` 再关闭。
2. 在 `production_migration.locked_session` 内复核真实库存并执行原固定迁移。每步业务写和
   journal 同 SQLite 事务；PG 与 SQLite 不是跨库原子事务。提交后锁失联保留已提交证据并停止，
   不重复迁移，也不把未知结果说成回滚成功。
3. `production_container_lifecycle.create` 写入 intent 后，只执行一次受控 Compose
   `create --no-recreate --no-build --pull never --no-deps`。对实际新建、未启动的精确容器 ID
   授权 descriptor 和每个 slot 的 grant。只有完整 `created_granted` 原件才允许后续 start。
4. `start` 先保存固定 ID 的 start intent，再调用一次 `docker start`。`query` 复核实际进程、
   owner lease、slot 和容器，确认全部 `closed_initialized`。此时普通业务尚未开放。
   容器或进程发生替换、重启、身份变化，不能自动套用原 grant。
5. 在原闭门能力下 `production_activation_control.arm` 绑定全部原 slot 回执。
   随后结束迁移 session，再用 `locked_activation` 取得新的真实锁能力；不能让两个连接
   嵌套持有同一组 PG 锁。`prepare` 发出一次准备许可，运行中的原进程实际启动所需子系统，
   `query` 检查原 owner、线程与 pulse，`confirm` 固定完整 readiness 摘要。
6. `ReceiverActivationSession.resume()` 在同一 fence、原 generation 和确认摘要上执行 CAS。
   接收端仍是原容器和原 PID。明确确认开放后，有序释放用于冻结库存的 PG / spool / activity 锁，
   继续持有 release / refresh / controller 边界；否则发送端 Go 的锁会与主机锁冲突。
7. 独立 `verify_active` 核验实际候选运行合同。发送端最后调用
   `SenderActivationDriver.production_resume(closed_operation, generation, receiver_activation_proof)`。
   该参数必须是真实 `ReceiverActivationSession`；主机在发送前后重查其原 activation、
   descriptor、manifest、binding、generation、全部 slot 和实时状态。
8. 归档完整原件和最终实际运行核验。`verify_active` 明确返回 `release_registered=False`，
   不修改发布登记，也不替发送端恢复。完成这些阶段后仍需显式处理发布登记及现场验收；
   单个库函数成功不构成“部署完成”。

`release.sh` 的普通部署路径遇到受控启动 manifest 会被 guard 拒绝。普通
`up --force-recreate` 会破坏按容器 ID 授权的创建、grant、启动次序，因此不是上述步骤的替代命令。
当前生产入口由这些固定主机 API 组成；原生验证脚本是合成演练，不是生产一键部署工具。

## 精确恢复、容量与停止条件

发送端生产恢复的 operation ID 由完整安全 receiver proof、sender binding、closed operation
和 generation 的 canonical hash 派生。完整 proof 与 intent 先持久保存，再发一个 POST。
Go 在单个事务内核实自己的 schema、关闭状态与 CAS；它不声称独立验证跨服务的 receiver。

提交 ACK 丢失、响应无法解析或主机回执落盘失败时，保留原 operation 和原件，只查询该操作。
恢复查询可以在原变更窗口过期后读取并确认原结果；它不延长窗口，不重新 POST，不生成新 grant。
某些 query 会补齐原持久回执，不应将其描述成完全无写操作的普通读取。
activation 的 `read_confirmed` 与运行态 admission 校验会对原文件和父目录实际执行 fsync，
并读回核对一致；它们不改写原内容，但不是无持久化副作用的普通只读接口。目录 fsync 持续失败时
不得授权使用该记录。恢复后针对同一原件成功完成独立耐久确认，不会把先前 host 失败改写为成功，
也不会自动生成 T0；host 仍须查询原 operation。首次 fence 的运行态 `read_receipt` 保持纯读取，
其 host M1 `_initial_frozen` 会重新确认原文件和目录的耐久性，不能只靠 final 文件名认定成功。

发送端每个控制证据根的 **精确恢复操作查询总预算为 32**：在发出该 operation GET 前
持久预留槽位，超时和未知结果也计入；旧 query 文件与新 reservation 共用相同编号，
不因进程重启再获得 32 次。这个计数不包括 registration、身份/PG 检查或变更前的关闭操作核验；
这些调用仍各受原有 I/O 时限约束，不能把 32 解读成所有控制 HTTP 请求的总数。

部分 create、部分 grant 或创建 ACK 未知且没有完整原件时，停止并保留现有容器及文件。
不要重跑 create、补发缺失 grant、自动 start 或 recreate 来“完成”未知操作。
已保存 start intent 的情况只允许精确查询；缺失、死亡或不匹配的 owner 不是可清理后重试的空位。
receiver prepare/confirm/resume 和 sender resume 任一阶段失去能力或身份时，也按原操作查询/停止，
不能把 readiness 失败转换成重启业务线程或自动回退。

保留既有 10,000 条 operation / lease 硬上限，不删除 lease 或 held 来腾空间。恢复前要留出
一个生产者、一个 durable outbox 的双 lease 和最终 pause 的记录预算；不足即终止本窗口。
`stop_new` 确认不等于队列为空，`draining` 不等于 switch permission。ACK 的七日年龄只是诊断，
`cleanup_credit` 仍为 0，不以年龄充当可用空间。PG 原生资源上限保持 384 MiB / 64 PID，
sender 与应用夹具保持 768 MiB / 256 PID；不以抬高上限使失败变成通过。

主机的新变更与接收端 intake 受各自有限窗口、配额和身份约束。Go sender gate 没有自动到期时钟：
一旦 OPEN，不会因主机窗口到期自行变成 `stop_new` 或 `closed`；其记录和原文硬上限仍有效。
接收端窗口到期会拒绝新 intake，也不自动写入 `stop_new` 操作或回执；既有 drain 与发送端
是否停止必须分别核实，不能把接收端拒收称为 sender 已停流。
有界演练应在变更窗口结束前实际执行停流、排空和 pause。若窗口已过而 sender 仍 OPEN，
必须保留真实状态并停止本次发布判断，另行审查有界关闭操作，不能宣称它已经自动关闭。
本协议没有无限期续期、自动替换或自动清理能力；不得重置代次或原窗口伪装后续操作仍获授权。

## T0、历史缺口与验证范围

合并代码、构建镜像、初始化 CLOSED、准备线程或计划中的时间都不是 T0。
只有实际 sender / receiver 激活完成并取得对应原操作、当前身份和独立核验原件后，才记录这次
联合协议实际生效的 T0；部分激活期间不能提前宣称全链已经受保护。
各组件实际开放的时点仍分别保留，不能用较晚的联合确认掩盖较早的入口空隙。

T0 前没有持久原始 body / signature / endpoint / contract 的在途消息继续标记为未知。
链接、标题、ID、日志摘要不能重建精确原文。该授权不要求也不允许据此补发、重新调用模型或
制造“历史无损”的成功记录。有原件的 held / legacy 数据保留原状态；当前协议不替其做终结、
清除或自动补偿，相关库存阻塞需要独立处理。

合成 native 可以使用真实 Go、PG、Flask、queue、ReceiptStore、容器与 owner，同时仍使用
假评分器。即便这样的联合测试通过，也只能证明被执行的工程场景，不能证明真实旧镜像兼容、
生产入口完整、历史原文完整、实际模型费用或模型评分质量。人工 Gold 标注与质量评估是独立工作，
不由本协议的 ready、绿色 CI 或运行回执替代。
