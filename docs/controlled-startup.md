# 冻结状态的一次性受控初始化

这是隔离验证能力，不是生产切换命令。不得修改已构建的 .126、把旧镜像当成具备此能力，或仅凭本地许可宣布真实镜像核验通过。生产驱动仍需独立审批和现场绑定；本脚本不访问 Docker、SSH、网络或模型，明确拒绝 production scope 和非系统临时目录下的数据库。

## 接口与固定范围

- `services.controlled_startup.authorize(fence, permit)`：只在初始 frozen、generation=0、普通 lease/历史操作均为空时一次性安装许可。目录 0700、文件 0600，使用现有 controller/activity 独占锁和短 metadata 锁。
- `migrate(fence, operation_id)`：固定调用真实 `db.database.init_db()`，不接受任意迁移 callback，不设置普通 intake/drain 豁免。
- `query_migration(fence, operation_id)`：查询精确原操作；完整 SQLite 标记及 schema/文件身份都吻合才重新确认外部回执，绝不再次初始化。查询会 fsync/恢复完整 pending 文件，不是只读。
- `claim_role(fence, operation_id, role)`、`query_role(...)`：只接受 web、worker。角色有独立 immutable intent、启动回执和 inode owner flock；同进程可查询自己的原回执，另一个进程或重启不可自动复用已消费角色许可。
- app 的 `initialize_app/init_db`、worker main 的初始化段，仅在显式 `AI_FILTER_CONTROLLED_STARTUP_OPERATION` 存在时走此路径，且要求原 enforced binding 和精确 `AI_FILTER_ROLE=web/worker`。无该变量时仍走原 admission 下的初始化，默认 disabled 行为不变。

许可严格字段：schema=1、scope=isolation、run_id（32 hex）、operation_id（64 hex）、manifest_sha256、完整 binding、runtime_sources_sha256、规范 db_path、roles（web/worker 各一个不同 64 hex ID）、not_before/expires_at（最多两小时）。runtime_sources_sha256 覆盖仓库运行 Python 源码，不含第三方库/动态配置；完整镜像、revision、配置与挂载需要总控制器另行独立核实。哈希不是抵御拥有私有目录写权限者的安全签名。

授权另存真实 host boot、elapsed、wall 时钟，不能凭墙钟回拨或重启重置未消费许可窗口。原许可和任何 partial/conflict 都保留，不自动清理或以新 ID 绕过。

## 为什么未知结果不能重跑

现有 init_db 会分阶段 commit，不是可由外部包装成一个原子事务的操作。因此先严格落 migration.intent，然后最多调用一次初始化。成功返回后，在既有 app_meta 中加入 `controlled_startup:<operation_id>` 完成标记（没有新增通用永久 magic 表），使用 SQLite synchronous=FULL 提交，再确认数据库/WAL/目录同步。

标记绑定许可、精确 operation/run、schema 摘要及数据库 device/inode。随后落外部完成回执：

- 意图存在、标记不存在：可能未执行，也可能已部分提交，统一为结果未知，保持 frozen，不自动重跑。
- 标记完整但外部 ACK 丢失：精确 query 可以补确认，不重新 init。
- 残缺/冲突标记、schema 变化、数据库被替换、权限或同步失败：停止并保留证据，不能以“幂等迁移”作重试依据。
- 普通数据行变动不改变 schema 摘要；schema 变化只导致拒绝，不产生重新迁移许可。
- 一次性标记本身是新增初始化元数据；回退兼容性仍需实际候选与旧镜像单独验证，不能由此文档推断。

## 角色启动不是业务开放

角色回执仅表示 `startup_initialized`；`health_verified=false`、`business_open=false`。角色 owner 锁不属于普通 work lease，不给后台、HTTP、评分、采集任何通用权限。真实 worker main 可以启动受原 guard 约束的服务线程，但冻结时 bootstrap 和业务工作仍被拒；进程健康也不能代替 open 许可。

许可已安装时，原 open-trial 还要求迁移证明完整、两个角色的原始 owner lease 仍活着。最终是否健康、入口绑定和业务/续作/终态队列是否为零仍由独立总控制器核验。open-trial 始终是另外一次显式操作，不因启动成功自动执行。

角色回执未确认或进程退出后，不允许自动重启并复用角色 ID。当前没有自动批准第二次进程启动的功能；需先查原操作并审定新的受控计划。Gunicorn 多 worker、自动重启和生产重建策略因此尚不能直接套用此隔离方案。

## 本地命令

`python scripts/run_controlled_startup.py authorize|migrate|query --scope isolation --root <fence> --binding <private-json> --permit <private-json>`

只有系统临时根下、私有目录内的合成 SQLite 可执行。许可不是 Docker inspect 结果；脚本固定输出 runtime_inspect_verified=false、switch_authorized=false。没有自动重试、自动开门、生产路径支持或回退执行。

## 验证及剩余边界

测试使用真实初始化和临时落盘 SQLite、文件/目录 fsync、线程/flock、真实子进程 os._exit(137)，覆盖初始化异常、标记后崩溃、ACK 丢失、重复执行拒绝、部分证据、数据库替换、时钟边界和角色重启拒绝。app.initialize_app 走实际入口；worker main 的 Redis/线程依赖在测试中合成，不把它当真实 Redis、完整容器健康或生产启动证据。

这些验证不证明硬件断电耐久性、不证明现有生产迁移耗时上限，也不解决旧异步发送原文缺失。外部入口、宿主 cron、旧版本排空、实际运行身份和回退材料缺证时仍应停止。NO_BACKUP 及原评分行为/阈值/重试预算不变。

