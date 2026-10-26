# Miniflux 2.3.2 的发送端持久 outbox 边界

状态：独立发送端补丁，默认关闭，尚未合并或部署。不修改 AI-Filter `.126`，也不以新标签覆盖该镜像。本补丁需要另建 Miniflux 镜像和新增 PostgreSQL 表；不能只安装接收端就宣称上游拒收问题解决。

发送端和接收端在私有开发历史中独立实现；公开版本包含代码，不附带历史私有 PR 或运维资料。运行时依赖下述准确 durable receipt 协议，接收端 PR 本身不能消除原发送端已拒收消息的缺口。

## 已核对的固定源代码

- 上游：`miniflux/v2`，tag `2.3.2`，完整提交 `51f2e0d8199ea8fa305081f6e175bba64b0ef94b`。
- 原运行镜像：`miniflux/miniflux@sha256:cd353a6af9ce663291f405eddef26fa0d567f486b521c5d34e3fb50e41938b9a`。
- 锁文件：`ops/miniflux/source-lock.json`；补丁 SHA256 必须与锁文件一致，应用前要求独立克隆、固定完整提交、干净工作区及基线 `git archive --format=tar HEAD` 哈希匹配。补丁采用零上下文避免把补丁上下文误报为源代码空白错误；应用后再逐文件核对预期 SHA256，不放宽原门禁。
- `storage.RefreshFeedEntries` 原本**每条 entry 单独提交事务**。`reader/handler` 在提交后才读 integration，并用 goroutine 调 `integration.PushEntries`；webhook 失败只有日志，没有持久重试。
- 原 `webhook.makeRequest` 使用 `json.Marshal(WebhookNewEntriesEvent)` 和 `crypto.GenerateSHA256Hmac(secret, exact_body)`。新路径复用相同 payload 类型与签名算法，不按数据库当前行猜原始消息。

既有固定版本隔离证据表明：接收端恢复后，之前拒收／超时的旧 entry 未因再次刷新自动重发；数据库里有 entry 不等于保存了原 webhook body、批次或交付收据。因此接收端无法单独补上前置拒收。

## 最小行为改动

配置 `WEBHOOK_OUTBOX=1` 才启用；`WEBHOOK_OUTBOX_CONTRACT` 必须有值，仅记录入队时约定的策略作为审计信息，**不伪称 Miniflux 能独立知道最终评分策略**。模式关闭时新表仍由迁移创建，但旧 webhook 行为保留。

启用且用户 webhook 已开启时：

1. 在原新 entry 的同一个 PostgreSQL 事务中取得最终 ID、时间字段。
2. 用已有序列化类型生成**单条** `new_entries`，冻结 exact body、原 HMAC、实际 endpoint、body SHA256 和审计契约。
3. 校验并发容量、执行同步提交设置；entry 与 outbox 同时提交。序列化、outbox 入库、容量或提交失败都不能报告该条成功。
4. 提交后由独立轮询发送器处理，不再为这个 webhook 启动旧直接发送 goroutine。其他 integrations 保持原来的路径。

重要行为变化：原来一次刷新产生一个多条 webhook 批次，现在每个新 entry 对应一个 singleton webhook。它保持一条新闻的字段与 HMAC 算法，不保持旧批次的字节／次数。HTTP 请求数可能增加；接收端须对 exact body 幂等，不能把“单条消息”理解为只调用一次模型。

初次建源时的历史 entry、手动保存的 `save_entry`、已有 entry 更新，不在这次 `RefreshFeedEntries` 新新闻路径之外扩展补偿。新补丁不扫描、重签或补发既有历史记录。

## 接收保全协议，不等于评分完成

`delivery_id = lowercase SHA256(exact raw body)`。发送端保持原 `X-Miniflux-Signature` 与 `X-Miniflux-Event-Type`，另带同值 `X-AI-Filter-Delivery-ID`。

仅 HTTP 202 且响应包含以下准确字段才可将 outbox 标记 `acked`：

```json
{"schema":1,"durable":true,"body_sha256":"<exact-body-sha256>","delivery_id":"<same-sha256>"}
```

响应最多 4096 bytes，4 个保留键必须准确小写，逐字段解析，拒绝其所有大小写别名、重复 JSON 键、尾随第二个对象、错误类型、错误身份或 `durable=false`。不能让 `durable:false,Durable:true` 利用 Go struct 大小写宽松规则变为 ACK。允许接收端附带 `accepted_strategy`／`revision` 等审计信息，不把它们当作成功评分证明。原签名证明请求身份；响应仍依赖受信任 endpoint／传输，不是独立签名的跨网络凭证。

普通 200、普通空 202、重定向或错误收据不算交接成功。HTTP 客户端禁止跟随重定向；301/302/303/307/308 的本地双服务回归确认目标未收到 body 或签名，直接目标的准确收据仍可通过。503、429 和网络超时保留原 bytes 并进入有界重试；401、其他不支持状态、错误收据转 `held` 待人工处理，不删除或改签名。endpoint 和签名密钥轮换后的旧消息不能静默改目的地、重新签名。

接收端另提供原 body／原签名的 `POST /webhook/receipt`。它不评分，但可能执行 durable barrier、确认已有交接或恢复回执，**不能称为无写入只读接口**。自动发送器目前不依赖此查询，只重试幂等的原 POST。查询 404 不能证明“从未执行”，也不能据此自行重构消息。

## 持久与预算边界

- 一个追加的 `webhook_outbox` 表、due 索引和 payload／删除保护触发器。没有 entry／feed 外键级联删除，删 feed 不能顺带删未交接证据。
- 写入 exact body 与 entry 同事务；入队和领取 attempt 两类事务都强制 `synchronous_commit=on`，要求 PostgreSQL `fsync` 和 `full_page_writes` 开启，不能因异步提交丢失领取计数而突破预算。这不是存储硬件断电耐久性证明。
- 启用路径每条 entry 的全部 DB SQL（含附件写入和 outbox）共享 3 秒 context，quota 使用 `pg_try_advisory_xact_lock`，持锁时立即显式背压并回滚该条，不内部轮询重试。默认关闭路径继续使用原 `sql.Tx`，不改变原超时行为。3 秒是 DB 操作预算，不是 Go 序列化 CPU／进程调度的硬实时承诺。
- 容量检查在获得事务 advisory lock 后互斥执行：最多 5000 行、256 MiB 计费字节；每条原 body 最多 1 MiB。计费包含 body、endpoint、契约及每条 1024-byte 开销估计，不宣称是精确磁盘占用。
- 容量满、DB 写失败会回滚当前新 entry，显式使刷新失败；之前已提交的条目各有 outbox。不改 RSS 条数／评分阈值，也不把剩余条目算成功。这仍不能保证长期背压时已滑出上游列表的未入库新闻不漏读。
- 每个 sender 串行发送，每轮最多 10 条，每秒发起下一轮；HTTP 原预算 10 秒、租约 30 秒、DB 操作上下文 3 秒。多进程用 `FOR UPDATE SKIP LOCKED` 和 lease token 避免同时拥有一行。
- 最多 20 次**传输领取**，指数延后最多 1024 秒；进程领取后崩溃也占一次。7 天仍未完成或耗尽预算转 `held`，没有自动删除。这个上限不是模型调用预算、每天调用上限或队列全局吞吐保证。
- 已确认交接的状态、确认时间和原 body 至少保留 7 天，之后只按限量批次清理 `acked`；**不保存接收端原始响应 JSON**，不能把本地状态当成完整外部回执。`pending`、`inflight`、`held` 不自动清理。表容量包含交接状态留存，满额可能使新采集背压。
- 新表沿用现有私有 PostgreSQL volume 与数据库授权，不新增公网接口／宿主目录。body 是第三方新闻全文，签名可重放，按私有敏感数据处理；不得打印原文、签名、完整 URL 或密钥。sender 仅记录 delivery ID 和安全类别。

## 崩溃、重复交付与回退

发送前宕机：事务原子性确保不会出现“新 entry 已提交但 outbox 没提交”。HTTP 成功后、写 `acked` 前宕机：原 outbox 仍在，租约超时后原样重复请求，由接收端 durable receipt 幂等承接。过期 worker 的 lease token 不能确认新领取的任务。失败收据不伪装完成。

固定原版 2.3.2 的 `IsSchemaUpToDate` 只拒绝 `currentVersion < schemaVersion`，`Migrate` 也只向上执行。后续固定实际旧镜像在合成 schema 132 上已验证可启动、且不会消费遗留 outbox；新镜像关闭开关同样不消费，重新启用后保留原 body／attempt。该隔离证据不是生产回退证明。直接换回原镜像会停止消费 outbox，同时重新启用旧异步发送路径，**不是安全回退**。未来回退前必须停止生产者和新 sender、保留新表与全部 pending／inflight／held 记录及交接状态、核对任务归属；若有未交接记录，需另行批准兼容消费者或保留修复版 sender。不得删表、降 schema_version 或靠重新刷新补偿。

本补丁新增了一个顺序 migration，已占用原版后面的 schema 版本号。未来官方版本可能使用同一个编号做不同迁移；**禁止直接升级到新的官方镜像**，必须另做逐项迁移对账与兼容方案，不能靠版本号较高推断已执行官方迁移。

AI-Filter `.126` 仍保持旧候选不变；本补丁不动评分文件。接收端修复会需要新的应用候选，但不属于这个独立发送端 PR。旧已拒收且没有 exact payload 的新闻不能自动从当前 Miniflux 表重新生成同一 signed delivery，必须独立人工审定与授权。

## 验证与尚未验证

已完成本地固定源码完整 `go test ./...`、实际 PostgreSQL 的事务回滚／重复交付／容量满／16 并发生产者与领取／租约恢复／证据不级联删除；新增持锁立即背压、真实 `pg_sleep(8)` 被 3 秒共享截止取消且 entry/outbox 双零、数据库会话默认异步提交时领取仍同步、默认关闭旧路径的正例。并发被背压者确认双零，测试中稍后显式重交可幂等完成，不在生产补跑。另有独立合成 PG 强制退出后重启、新进程恢复原 body／签名／attempt 的测试。HTTP 使用离线 transport 和本地合成 HTTP 服务，不调用任何真实模型或生产入口。

首次 Go 全量受本机沙箱回环监听和未缓存模块影响；首次 PG 预演因内部网络不发布端口而停止。失败材料保留，后续在同样隔离范围修正：PG 不再开放宿主端口，交叉编译测试二进制在该合成 PG 容器里运行。重启测试人为令 lease 到期，不宣称实际等待了 30 秒，也不宣称机器掉电测试。

旧 head `8920bc31` 已有远端完整 CI，以及原生 amd64 固定 sender/receiver 镜像联合隔离验证（run `39806925312`）：覆盖拒收、超时、ACK 丢失、sender 崩溃、普通 202 不确认和多 entry 单条身份。它们证明各自的精确候选与合成场景，不证明这次文档／隔离处置工具的新 head 已通过 CI，也不证明生产已切换。原生 sender 二进制绑定为 `3a533d2b670b14acd1248e5e79b6f2200ceab89bf4f6828f7a5bb901306b192a`；Build Date 来自上游 VCS 时间，不是本轮编译时间。联合核验器曾在 CI 后修正离线 oracle，原始失败保留；没有重生成应用／请求证据。

发布前仍须完成本次最终 head 的完整 CI，以及现场入口清单／停流适配器／交接容量与留存治理的单独审定。CI 默认运行的两项原 storage 重启 phase 测试会 skip，因为它们需要独立重启 harness；其证明来自本地隔离证据，不能声称该 CI 执行了这两个 phase。现有 SQL 写入故障测试不替代 PostgreSQL 真实磁盘 ENOSPC／机器掉电。这里不提供生产执行命令。

## 容量与留存的启用前置条件

5000 行包含 `acked` 的至少七天留存，不只是积压。即使立即收到每一条 ACK、没有 held、忽略字节与清理延迟，5000 / 7 也仅约 714 条／日的理想持续上限；它不是已测生产容量。256 MiB 计费上限和 1 MiB 单消息上限还可能更早触发，实际 PostgreSQL／WAL／审计磁盘用量另计。

开启前必须按入口统计新增 singleton 数、正文／计费字节及峰值，评估七日 ACK 留存、现有 held、计划停流余量和清理吞吐（每轮至多 20 条）。确定负责人及有界检查周期、剩余行／字节／最老未确认年龄／attempt／held 数和清理滞后的告警阈值。没有这些测量和处置安排，**代码可以独立合并，发送端开启仍不获推荐**。

容量接近耗尽时，不自动删除未交接行、不缩短七天 ACK 留存、不扩大预算。须在未耗尽前按另行批准的受控方案停止所有新 entry 生产入口并保存有界上游对账材料；若已满，当前 entry 会回滚并使刷新失败，先前提交的前缀仍保全。后续列表滑窗可能永久丢失未入库条目，不能承诺无限无损。其他 integrations 对失败批次前缀的交付也不能由 outbox 反推成功。

`held` 永不自动删除；仅 endpoint 恢复不足以解除。精确原 body 不在则不可重建相同 signed delivery。需要准确 ACK 才能确认原交接；未超龄且未耗尽者的原样一次重试必须逐条授权，可能产生额外模型调用。过期／耗尽者不得借人工处置重置 created/attempt 或重签，若需新交付必须另案审定，不能伪装旧预算内恢复。

## 隔离 held 处置工具与生产禁止边界

`ops/miniflux/heldtool` 是独立 Go 工具，不编入 Miniflux 镜像、不改上游补丁、不新增生产迁移。它只接受标明 `mode=isolation` 的本地合成 PG、随机 `held_isolation_*` schema 和真实隔离实例 marker；生产模式硬拒。**完成该工具测试不等于已经有可执行的生产停机／恢复 adapter。** HTTP fixture 的切换证明没有 PG identity，不能直接授权本工具。

处置先生成不含原文／签名／完整 URL 的精确计划，再以计划 hash、停止证明 hash 及 operation_id 显式批准。停止证明绑定全部入口清单 manifest、run、adapter、PG identity 和新鲜时间；必须确认所有生产者入口关闭、sender 已停止、无活跃发送 lease。数据库会话／lease 查询只是交叉检查，不能单独证明外部入口已停止。

执行在同步 PG 事务内 CAS 精确 held 快照，并追加有界审计。一次原样重试先把原 attempts 加一，保持 `held`、写专用人工 lease，提交后只 POST 一次；不改成 pending，不交给自动重试器。成功才 acked，任何超时／错误收据仍 held。执行崩溃留下人工 lease，普通 sender 不会领取，需另一份停机证明和明确的 receipt-only 恢复计划。查询确认可用于已耗尽／超龄行，但必须拿到精确 durable 回执；404／普通 202／时间接近均不构成确认。审计保留安全状态与响应哈希而非完整外部响应，不把它说成原始回执档案。

隔离工具只能在独立测试容器网络中使用；loopback 不等于合成服务。发送前还要做不含原文／签名的最多1秒 fixture identity 探测，和审批／PG marker 中的 receiver identity、endpoint hash 精确对应。控制探测与原消息 HTTP 尝试分列；本地拒绝计原消息0次。原 contract 只是冻结行的审计值，不证明接收端仍是相同策略，不能由本工具放行生产跨契约恢复。

隔离审计表有独立条数／计费上限，满额时在领取前背压；本工具不自动删审计、不清理 outbox。现场若将来引入此方案，须另外审定维护迁移、权限、审计归档／清理、停止证明可信来源和逐条授权流程，不能把测试 schema 直接当作生产安装方案。

## 可重复构建步骤（只给方法，不执行部署）

1. 独立 clone 固定上游 commit；运行 `scripts/apply_miniflux_outbox.py --source <clone>`，检查通过后再显式 `--apply`。不能对浮动 HEAD 或脏 clone 打补丁。
2. 以锁定 Go 1.26.4 和原 `go.sum` 跑完整 tests、实际合成 PostgreSQL 回归、`go vet ./...`。新 `.github/workflows/miniflux-outbox.yml` 专门执行这些门禁；不替代本仓库原完整 CI。
3. `CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags "-X miniflux.app/v2/internal/version.Version=2.3.2 -X miniflux.app/v2/internal/version.Commit=51f2e0d-outbox-0bf94e8f"` 构建新的 Miniflux 二进制。记录源码完整 SHA、补丁 SHA256、Go 版本、二进制 SHA256；后续补丁变更必须同步构建标识，不能冒充原官方二进制。
4. 仅把已验证二进制 `miniflux` 放入独立构建上下文，使用 `ops/miniflux/Dockerfile` 的固定原运行镜像作底座。新镜像必须使用全新 Miniflux 标签并最终锁定完整 digest；不得替换 AI-Filter `.126`，不预填尚不存在的镜像 digest。
5. 构建后检查实际二进制哈希与镜像平台，在无生产数据／密钥的环境验证；满足前述未验证项后再独立申请上线。
