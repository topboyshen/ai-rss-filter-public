# 固定 Miniflux 统一暂停：实际镜像隔离验收

本次是独立发送端补丁的验证，不是生产发布。保留原 `0001-durable-webhook-outbox.patch`，
另以独立锁将 `0002` 应用于同一个上游 Miniflux 2.3.2 完整 revision；需要新建 Miniflux 镜像，
不修改、不重标记 AI-Filter `.126`。生产、JD、真实 RSS、真实模型和生产数据库均不在本次范围。

## 门的语义与启用限制

`HANDOFF_ENABLED=1` 需要 outbox 开启、非空审计契约、固定 manifest、私有 loopback 控制地址和 token。
空门表不能因进程启动就自动打开；首次开放必须显式初始化。默认关闭时的旧行为与旧数据库兼容性须由
源码／回归另核，不能因启用模式隔离通过而默认成立。

受控模式的 HTTP lease 由最外层请求生命周期持有，在现有凭据验证成功后、首次认证时间戳或业务写入前
才持久获取。匿名请求、失败凭据、静态资源和 CORS OPTIONS 不消耗持久租约容量。UI 在此模式只允许
已有且验证有效的登录会话：匿名、新登录、OAuth／认证代理新建会话均明确拒绝，不先创建匿名 session。
这是短时受控模式的功能限制，不是透明替换；正式启用前须确认可用的既有 UI 会话／私有控制通路。
关闭该模式时保留上游登录行为。清空历史和删除用户在启用时同步完成，避免 HTTP 已结束而后台仍写库。

控制接口是 `/registration`、`/initialize`、`/pause`、`/operations/<operation_id>`、`/resume`。
回执必须同时绑定 manifest、精确 operation/action/代次/状态、`durable/confirmed` 和活动／未知 lease 数。
普通 200/202 或仅外层 `confirmed=true` 不够。Pause 先持久拒绝新准入，再等已准入工作退出；
HTTP 超时、409、PG 不可用都不是“已停成功”。丢回执只查询原 operation；不能盲发新暂停或自动 resume。
恢复必须以准确已闭合操作和新 operation 显式 CAS，不能把旧回执跨代次使用。

暂停保护的是**新版本中实际接入的入口和在途生命周期**。它不能回溯已运行的未打补丁旧 Miniflux
goroutine，也不能恢复以前未保留的 raw body。旧实例的在途消息仍须在受控切换前单独保全和核清。
lease 归零说明这些工作已结束，不代表 outbox、held、接收端 queue/journal 或第三方投递全成功。

当前门也**不覆盖任意宿主手工 CLI 或启动阶段写入**：`-migrate`、重置源错误／下次检查时间、
清理会话、创建管理员、重置密码等管理命令，以及 `RUN_MIGRATIONS`／`CREATE_ADMIN` 启动工作，
不因 daemon 已 paused 就被自动禁止。正式切换必须由未来外部受控方案阻断旧实例／旧 cron 的派生、
限制手工管理入口，并将新实例首次迁移和启动单独绑定到受控阶段；不能拿本次 daemon 暂停回执代替这些证明。

门控操作和生命周期 lease 各最多保留 10000 条，不自动清理；达到上限拒绝新准入，不绕过保护。
恢复操作不能耗尽最后一个停流操作名额：没有下一次持久暂停的剩余空间就保持 closed，不先开放再报容量不足。
空 outbox 轮询只做有界只读候选查询，不为每秒空轮询新建 lease；查询结果既不是可靠接收回执，
也不能证明排空。并发插入在下一 tick 再观察，真正领取、清理和网络发送仍在已准入生命周期内。
保留容量不是无限运行承诺；正式启用前必须批准容量观察、停流与证据处置方式。

## 实际隔离 harness

`scripts/verify_miniflux_pause_native.py` 默认仅输出计划；`--run` 强制 Linux/x86_64 和实际 Docker
daemon 架构，拒绝 AMD64 仿真。镜像以完整本地 image ID 绑定，复制镜像内真实 `/usr/bin/miniflux`
与固定源码编译的二进制逐字节核对；锁中各修改文件逐个核 SHA256。镜像标签本身不算源码证明。

CI 新 job 按固定 Go 1.26.4 应用两份补丁，跑完整 Go tests/vet（包含真实 PostgreSQL 回归），再构建
新的隔离发送端镜像。业务进程和固定 Python 测试客户端均限制 **768 MiB（swap 同值）／256 PID／1 CPU**，
真实 PostgreSQL 为 **384 MiB／64 PID／1 CPU**。网络为新建 `--internal`，无宿主端口；控制请求由固定
Python 短生命周期客户端加入准确 sender network namespace 后访问它的 `127.0.0.1:8787`，不放宽监听地址。

使用真实 Docker inspect 校验实际 image、标签、网络、内存、PID、CPU、禁止特权和 no-new-privileges。
前后捕获 cgroup `memory.peak`、`memory.events`，不以进程 RSS 和累计值代替峰值，不声称每个瞬间都有采样；
进程重启前后分别留档。失败仍保存命令 stdout/stderr、inspect、固定身份、PG 合成 dump、HTTP 原文和 fixture。
只停止本 run 精确标签容器，不清卷、不删原件、不改变任何已有标签。

Go 回归及独立 legacy 权限审计使用专用 service 数据库 `synthetic_outbox`，与实际镜像八场景另建的 `synthetic_pause` 分开。
CI 总会尝试保存前者的实际非系统 schema 清单和**整库合成 dump**，涵盖 `handoff_test_*`、
`outbox_test_*`、`legacy_integration_*`、`pause_worker_*`、`pause_http_*` 及后续新增的回归 schema；
不再只保存 `handoff_test_*`。inspect、内存峰值、schema 清单和 dump 各自保留 stdout、stderr 和真实退出码；
一项失败不阻止尝试保存其余材料，最终仍以失败退出，不把截断 dump 当完整证据。旧 CI 原件不改、不补造。

### 八个独立结论

1. 未初始化时实际公共 API 返回 503；精确 initialize 后正常手动刷新写出 outbox，真实 sender 获取准确协议 ACK。
2. 合成 feed 到期后，由**真实一分钟 scheduler timer**处理，核 `scheduler→queued_refresh` lease 和新增 outbox。
   不改调度器时间单位、不手动替代 timer、不使用真实源。
3. 手动 `save_entry` 的 webhook 原始 body/HMAC/身份保存为 `held`，不发送接收端不支持的事件。
   同时启用指向 fixture 的真实 Readeck 保存客户端，利用其实际旧异步 goroutine 的阻塞 HTTP 握手验证：
   pause 未确认、公共刷新被拒；释放请求并观察完成后，精确 query 才确认 closed。
4. sender 同镜像真实进程重启后仍 closed，公共刷新仍拒绝，原 PG 操作／lease 证据保留。
5. 显式准确 resume 后，新增真实 outbox HTTP 在途；pause 不假成功，释放后 outbox 仍按原 body 精确 ACK，
   lease 结束且原操作 closed。错误 prior operation 的 resume 必须拒绝。
6. closed 时隔离 PostgreSQL 停止，API 与控制 query 不得假确认；原合成 PG 数据卷重启后仍 closed。
   这是受控正常停启，不是断电证明。
7. closed 后再让合成 feed 到期，等待真正下一次 scheduler 的拒绝记录，并核 fixture feed 请求数未增加。
   仅“没有请求”不能证明 timer 曾实际到达，因此必须同时有新拒绝日志。
8. 实际复制的 cron wrapper 执行登记 argv；独立在途子进程持活动 FD，关闭新派生后先拒绝确认，
   子进程自然结束并严格记录 finish 后只查询同一个操作。再通过真实镜像、真实 PG 私有协议及逐次 Docker
   身份核验的 bridge 完成在途 pause→原操作 query。原旧 cron 字节、逐行替换与实际登记 argv 留档，
   不读取或安装宿主系统 cron。这只覆盖三组 sender 入口及一个合成旧 cron，不冒充其他应用入口全覆盖。

fixture 的 ACK 只用于发送端协议测试，不是实际 AI-Filter durable receiver 的保全证明，也不证明评分完成。
真实 sender→真实 receiver 联合交接、控制器排空及旧实例切换仍要分别使用对应固定镜像证据，不能互相替代。
本次新增 PG 控制协议尚不是现有宿主适配器的生产接线凭证；不能把独立 sender 验证作为生产切换许可。

`wait` 将同一阶段的剩余预算传给每次 Docker 命令，不能逐次重置，也拒绝超期后才返回的成功。
本地同步 I/O 不是可抢占的；客户端超时不证明服务端请求已经停止。
cgroup 峰值或必要 OOM 计数缺失时保留缺证并失败，不能把缺失计数按零处理。

## 失败边界与保留

任何源码／镜像／资源／代次偏离或必需链路缺证，harness 立即标失败或证据不足，不扩大预算重试业务。
只允许有界 readiness/原操作 query 观察，不重发 pause、refresh 或 save 来凑成功。
等待 fixture 释放是显式同步握手，不靠 sleep 推断是否已有在途请求。
`save_entry` 返回 202 只表示原文保全与其他集成受控启动，held 不是成功送达或已评分；不得自动释放/删除。

回归脚本：`python -B -m unittest discover -s tests -p test_miniflux_pause_harness.py -v`。
这些本机 guard 测试不执行 Docker，不冒充原生实际镜像验收；最终结论以固定 CI artifact 为准。
本文件不会预写 CI 成功或将尚未跑过的实际场景标为已验证。
