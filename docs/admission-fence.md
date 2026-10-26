# 应用准入屏障：部署绑定与操作边界

本变更必须随**新应用镜像**验证和发布，不能让已构建 `.126` 获得原本不存在的能力；本 PR 不重新构建、修改标签或部署 `.126`。发布和启用分别审批。默认关闭，不授权停止生产，不给现有运维脚本补造 `switch_authorized=true`。

这是依赖操作准备 PR #253 的 stacked PR，不能在依赖尚未合并时单独先合并；最终同批发布仍需重新审完整 SHA、完整 CI、门禁、候选镜像和实际旧镜像兼容性。本页不替代上述审批。

## 配置与共享目录

- `AI_FILTER_ADMISSION_MODE` 仅允许 `disabled` / `enforced`。未设置等同 `disabled`，未知值拒绝，不静默回退。
- `AI_FILTER_ADMISSION_ROOT=/app/data/admission_fence` 是所有应用执行角色共同看到的屏障根目录。Compose 的 API、worker、market-close-collector、event-enrichment、security-master-sync 均使用现有 `./data:/app/data` 绑定。不是每个容器的 `/tmp`、进程内变量、单独私有 volume，也不放在应用不可写的 `data/releases`。
- Compose 明确固定 `disabled`，不通过 `.env` 插值悄悄启用。不能只让 API enforced、worker/cron 仍 disabled 后宣称全入口停止。私有 provision 和运行时绑定缺失时不得启用。
- 根目录要求 0700、文件 0600、正确所有者，无软链接/硬链接替身；不递归 chown `data/` 或发布登记。当前应用角色使用同一现有 UID；未来迁移 UID 需要独立目录权限迁移审定。
- provision 创建的初始状态必须冻结，应用启动、容器重启、缺失或残缺状态不能自动 open。开启试运行必须显式操作，绑定有限条数和期限；不增加原 receiver/outbox/续作预算。
- 共享 `deployment_nonce` 表示一次明确 provision 的部署实例，不等于容器 `StartedAt` 或每进程启动身份。每个进程/活动 lease 另有独立身份，崩溃遗留不能凭 PID 重用或重启清零。

环境变量和本地 manifest 是期望值，不是实际运行证明。镜像 ID、完整 revision、契约、容器启动时刻、资源与真实共享挂载仍需来自独立的现场原始 inspect/核验；本地 CLI 不执行 Docker/SSH，不会从可任意填写的 binding JSON 推断这些核验已通过。

## 两阶段停止，不能阻断自己的排空

1. **open**：允许 intake（新任务）和 drain（既有持久任务继续执行），准入前在屏障状态锁内严格持久保留名额与活动 lease。
2. **draining**：停止新 intake；已接受的队列项允许 drain。普通 202 不是新任务已可靠交接的证明，仍按 durable receiver receipt 协议确认。
3. **freezing**：拒绝新的 intake/drain，等待已有活动共享 lease 自然退出；超时返回未确认，不杀任务、不清活动原件。
4. **frozen**：只确认当前屏障覆盖范围内活动 lease 为零；**不等于**业务队列、续作 journal、archive_pending、held、外部 Miniflux in-flight 为零，不能据此切换或回退。

worker lease 必须跨 claim → executor/future → 最终 finish/archive 的完整生命周期，不能在 `executor.submit()` 返回时释放。request 返回也不释放已转交后台的独立活动责任。嵌套同一次 intake 可重用明确 permit，但不得让无 permit 的另一个线程/入口绕过检查。已接受任务派生的内部任务与新外部消息分类不同，不能无意双计预算，也不能借内部标签无界新建任务。

固定锁顺序须一致：应用活动共享锁在原 refresh/spool 等锁之前；控制器先有发布锁，再活动独占屏障，再核 refresh/spool。元数据锁只覆盖短状态事务，不能一边持元数据锁一边等待业务 lease 完成。出现锁竞争、同步失败、未知活动或身份冲突即不确认，不放宽旧发布门禁。

## 必须覆盖与不得宣称覆盖的入口

本次入口核查包括签名 webhook（含会恢复落盘的 receipt 查询）、自然/手动 refresh、冷 `/filter` / `/filter/multi`、`briefing` / `briefing-card` 的 LLM 或推送路径、已注册反馈和管理写接口、worker 的持久队列、内部快照任务及行情/Quant 轮询，以及 Compose 显式维护 profile。仅 GET/HEAD 的 `/health`、`/dashboard/events`、`/dashboard/events/daily`、`/dashboard/events/weekly` 按精确已审 endpoint/rule 免准入；其他看似缓存的页面也可能写库，不豁免。健康心跳不代表业务入口开放，也不执行评分/业务数据库变更。

路由名单以实际注册和函数调用为证据，文件存在但未注册的历史 blueprint 不能当成已覆盖入口。默认不能把未知写请求放行；别名、旁路 Python 调用和容器外命令须在同一叶函数/任务边界守卫或由操作前置明确禁止。

以下事实不能由应用屏障独自证明：

- Miniflux scheduler/manual refresh 与旧 async sender 已停止；其在途、未确认 outbox 和 held 已核对。
- 宿主实际 cron 使用新镜像/共同目录，或者所有 outside-Compose 脚本遵守屏障。
- 已有队列、续作、终态归档及数据库写入均已排空，或者回退目标理解新协议。
- 生产实际镜像/启动/挂载与 provision manifest 一致。

任一缺证都保持 `switch_authorized=false`。`scripts/production_handoff_adapter.py` 原有生产执行阻止逻辑不能仅因本 PR 增加了应用屏障就自动解除；后续必须把实际入口清单、原始运行身份、生产者停流、排空和版本切换适配完整接上并经隔离验证。

## 容量、超时、重启与回退

有限 trial 需要在同一屏障事务中原子 reserve intake 数量与持久到期条件；仅外部 observer 定时看计数不能保证采样间隔内不超收。失败、未知回执、磁盘满、fsync/rename 失败都不算成功；不得因重启清掉不确定 lease、重置期限/条数或重试新 operation 来绕过同一操作的未确认状态。

`max_admissions` 只限制 intake；屏障另有 **8192 条总 lease 证据**上限，实际 drain、活跃 claim 续租与需处理的重试同样消耗此容量，完成证据不自动 GC。空 spool 和无待发布输入已有有界只读提示，避免仅因空闲轮询不断创建 lease，但这不保证两小时额度必能用满；时间、条数、字节及证据容量任一先到即拒绝。每次实际 reserve 仍会 O(N) 重读并严格同步已有证据，尚未验证满容量性能，不能把小样本耗时外推成接近上限时的响应保障。

停止操作和查询须绑定精确 operation ID、generation、manifest 身份。`snapshot` 会对可见 lease/finish 证据重新确认文件及目录同步，`query` 还可能恢复操作；两者都不是纯只读生产探测。丢 ACK 后查询原操作，不自动换 ID 重发、不自动 resume。文件系统 `fsync` 验证不等于已证明硬件断电耐久性。

操作回执中的 `activity_drained` 是该操作完成时的历史值，不是查询时的实时活动数。`open` 或 `stop-new` 后仍可能合法创建 drain lease，须另看当前 `snapshot`；已进入后续 generation 时旧回执不能冒充当前状态。即使 freeze 确认 lease 已排空，也不等于队列、journal、archive 或 held 已为空。哈希用于一致性校验，不防拥有目录写权限者同时重写全部证据。

回退前仍必须独立静止外部生产者消费者并保全 receiver receipt/alias intent/index、queue、续作 journal/负收据、终态/archive_pending、sender outbox/held；有未知或跨契约未完成状态时不能直接退到不理解新状态的旧应用。旧 Compose、旧镜像和原始证据继续保留，屏障不自动部署、重启或回滚。

## 验证范围

`scripts/control_admission_fence.py` 提供单次本地 `provision`、`open-trial`、`stop-new`、`freeze`、`query`、`snapshot`。每次要求显式绝对路径的 0600 binding、authorization 文件，provision 另要求 policy；授权绑定输入实际字节哈希、操作、规范 root、operation ID、generation、reason 和不超过两小时的有效窗。当前只接受 `scope=isolation`，未知字段/布尔冒充整数/过期/变更输入均拒绝，不能用它执行生产停流。CLI 不访问 Docker/SSH/HTTP/数据库，不创建后台任务，不自动重试或恢复接收。

CLI 外层固定 `runtime_inspect_verified=false`、`queue_checked=false`、`switch_authorized=false`，内部 core 回执原样返回。`local_action_returned` 仅表示该次调用返回：`snapshot` 或 provision 不是停流确认，`draining` 不是冻结；真正 `frozen` 回执也只有 lease 排空含义。超时/未知状态 exit 2，保留 operation ID，后续仅显式查询同一操作，不换 ID。`snapshot` / `query` 可能严格重同步或恢复文件，不称只读。

旧 `CapacityObserver` 的单阶段 `closed` ACK 不接受 `stop-new=draining`，也不能被 CLI 擅自转换。操作适配联动需要后续两阶段能力、外部真实绑定和排空审定；在此之前保持不兼容即拒绝，不能为了接口通过编造 `closed` 或 `queue_empty`。

`tests/test_admission_fence_deployment_contract.py` 检查全部应用执行角色共享同一现有数据挂载、默认显式 disabled、没有伪造运行 binding、没有新增 Docker socket、不可变镜像及原资源权限边界保持。核心状态机、进程重启、并发、实际 fsync 故障和入口集成的测试另行记录；隔离合成证据不能写成生产启用通过。

`tests/test_admission_fence_control.py` 除 CLI 授权正反例，还用真实 core/私有文件/flock 组合验证初始冻结、stop-new 后既有 drain 可继续、在途 freeze 超时、完成后精确原操作查询、重建对象后仍冻结，以及 uncertain lease 不被丢弃或伪装成功。它不调用模型、不访问生产、不执行镜像切换。
