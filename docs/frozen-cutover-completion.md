# 冻结切换补齐：隔离实现，不是生产部署许可

本变更叠加 #253、#254。禁止把本地测试中的 Docker-shaped fixture、
`scope=isolation` 的停止回执或临时 SQLite，写成 JD 生产已停流／已切换。
既有 `.126` 不修改、不重标；将来需要重新审定的新应用及发送端镜像。

## 独立排空与锁顺序

`scripts/independent_drain_inventory.py` 不构造 ReceiptStore，不消费任务，
不恢复或删除残件，不 chmod。检查实际私有目录的 inode/owner/权限、
队列 pending/inflight、终态归档中间态、完整回执及原始任务索引/别名、
全部续作状态，以及只读 SQLite 发布水位；PG 必须由实际绑定的只读采样器提供。
PG 缺证不能用全零补齐。未知文件、残件、缺失锁、超时和身份改变都停止。
每次文件读取有限额，单 SQL 最多 3 秒，扫描总预算独立配置为 1–10 秒。

“存储已排空”要求没有队列工作、没有活跃续作、没有 accepted 未完成回执、
没有未完成发送，且 input=published。已完成和终止证据保留；held 不自动删除，
也不因 held 已保留就获得重新发送许可。历史完成收据不是原始消息保全证明。

`scripts/frozen_cutover.py` 的顺序：

1. 停止所有已注册生产者并等旧 cron 子进程退出，不能先占它需要的 release 锁。
2. 独占本次 controller → release → refresh → 旧应用 activity 锁。
3. 重新核运行身份与 frozen、active=orphaned=uncertain=0。
4. 在实际 spool/journal/逐新闻锁持有期间重新读取，不复用已释放的零计数快照。
5. 严格持久记录原 operation intent 后只调用一次冻结启动驱动；锁一直持有到启动结果验证。
   全部锁上下文退出身份复核成功后，才写最终成功回执；此时生产者和业务仍保持关闭。
6. ACK 丢失只查询原 operation，重新持有排空锁并核对原锁身份；不重新停流、迁移、启动，
   不自动放行／回滚。停止生产者阶段未知时，须查询运行适配器原 operation，不自动续完组合切换。

该组合控制器没有生产 CLI，也没有自动开启业务的步骤；`completed_frozen`
仅表示所绑定的隔离驱动完成，`switch_authorized=false` 保留。
实际目标镜像及启动身份须由驱动另证，不能从声明的 binding 推断。

## 仍然阻断生产切换的条件

运行镜像 inspect 并不能证明 Miniflux 内部调度、手动入口和遗留异步发送都停止。
HostCronGate 只能管真正经其 wrapper 派生的进程。若这些内部入口没有同版本的
停止能力／在途原文保全和可查询回执，就必须阻断，而非把容器暂停当零丢失切换。
具体缺口与部署前条件见同 PR 的 `handoff-runtime-adapter.md`。

冻结迁移只许可固定真实 initializer；中间提交后结果未知时不能重跑。
新角色授权不开放普通 intake/drain，角色启动回执也不是健康或页面通过证明。
held 的 execution-time CAS 与原始预算、独立审定／执行身份另见
`miniflux-held-execution.md`；SQL 的隔离 stop marker 不代表真实生产入口停流。

## 验证口径与后续部署建议

完整 CI 保留全部测试、函数长度、Compose 和镜像构建，不豁免本机失败。
新原生 amd64 job 使用独立依赖测试镜像：768 MiB（含 swap）、256 PID、1 CPU，
只读源码与根文件系统、无外部网络、cap-drop ALL。测试实际文件、SQLite 初始化、
进程与 loopback HTTP；另以原生实际 Docker inspect 验证镜像/角色/挂载/限制绑定。
held 在固定 PostgreSQL 中用真实独立登录与 HTTP 校验，不拿游标 mock 当数据库证据。
失败日志、命令退出状态、源哈希、容器 inspect/OOM/峰值内存全部保留。

本轮不连接生产，因此不提供当下 JD 入口完整清单、旧 cron 安装结果或活跃任务为零的声明。
建议暂不部署。待新增发送端统一暂停通过固定镜像验收，并完成生产控制协议接线后，
单独审定候选新镜像、实际入口绑定、旧任务原文保全、容量停流阈值、有限窗口和退出／回退方案；
不能直接启用本隔离 CLI。
一旦产生新 outbox/回执/别名/journal/负收据/终态记录，旧镜像不能盲目回退。
应先静止所有生产者消费者、独立保全原件，再核各状态可读写及人工恢复边界。
本轮不重评、不补发，不修改既有阈值、NO_BACKUP、封存样本与留存开关。
