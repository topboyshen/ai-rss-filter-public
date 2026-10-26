# 部署、迁移与恢复

本文说明生产发布的安全边界。具体 systemd、cron、备份、恢复演练和告警资产见
[`ops/README.md`](../ops/README.md)。
日常发布与镜像回退的完整命令统一维护在[日常操作入口](../ops/DAILY-OPS.md)。

## 1. 生产前提

生产拓扑需要 Docker Compose、持久化 `data/`、Redis、RSSHub、Nginx 网关以及根目录
`.env`。至少配置：

- `AI_FILTER_API_KEY`：过滤、刷新和管理接口；
- 一个或多个 LLM Key：按 `config.yaml` 的模型路由使用；
- `MINIFLUX_WEBHOOK_SECRET`：启用 Webhook 时必需；
- 精确的 CORS 来源与公共基础 URL。

三个服务凭据必须相互独立：

```bash
set -eu
install -d -m 0700 ./secrets
for secret in dashboard_session_secret ai_filter_integration_token quant_market_context_token; do
  test ! -e "./secrets/$secret"
done
(umask 077; openssl rand -hex 32 > ./secrets/dashboard_session_secret)
(umask 077; openssl rand -hex 32 > ./secrets/ai_filter_integration_token)
(umask 077; openssl rand -hex 32 > ./secrets/quant_market_context_token)
chmod 0400 ./secrets/dashboard_session_secret \
  ./secrets/ai_filter_integration_token \
  ./secrets/quant_market_context_token
```

不要把 `.env`、`secrets/`、数据库、日志或备份提交到 Git。Compose 只把各角色实际需要的
凭据名从 `.env` 显式投影到容器，不再通过共享 `env_file` 注入行为参数。API、LLM、Webhook
与 Telegram 必须使用相互独立、最小权限且可单独轮换的凭据。环境变量值可被拥有 Docker
inspect 权限的主体读取；三个服务间凭据继续使用 Docker secret 文件，不得回退到 `.env`。
域名、限额、开关、调度和网络地址等行为参数只能随 `config.yaml` 或版本化 Compose manifest
发布；修改它们必须分配新版本并重新构建。

## 2. 不可变镜像发布

发布脚本拒绝 `latest`、`main` 等浮动引用，并记录镜像 digest、Compose manifest、
`config.yaml` 哈希、源码身份和健康服务集合。应用配置在构建时写入镜像；生产 Compose
不再从宿主机覆盖挂载 `config.yaml`，因此镜像与实际配置属于同一个不可变发布单元：

首次发布前创建独立的 Python 3.11 测试环境：

```bash
python3.11 -m venv .venv-release-gate
.venv-release-gate/bin/python -m pip install -r requirements.txt
```

随后按[日常操作入口](../ops/DAILY-OPS.md)选择已通过 CI 的提交，依次执行
`run-release-gate.sh` → `release.sh build` → `deploy` → `verify`。

每次使用新的不可变标签。部署会在发布锁内重建服务并执行健康检查；只有 registry 中
存在不同且完整的 previous/current 状态时，失败才有可用的自动回滚目标。首次发布或没有
distinct previous 时不能假定脚本可以自动恢复。

`run-release-gate.sh` 先在隔离的 clean-HEAD worktree 中固定执行 shell 语法检查、完整 pytest
和 75% 覆盖率门槛。成功后才会在 `/run/ai-filter-release-gate/attestations` 原子写入权限
`0600`、最长有效 6 小时且绑定 HEAD、runner、Python 与测试日志的本机收据。新一轮测试开始
会先撤销同 HEAD 的旧收据；失败或中断不会留下 PASS。收据是防漏跑的本地发布控制，不是抵抗
root 的密码学签名。

`build` 先检查 history、build contract、current/previous state 和覆盖审计记录；标签一旦登记，
即使本地镜像已被 prune 也永远不能用同名重建。随后它要求 clean HEAD、默认的已提交
`docker-compose.yml` 和有效测试收据，任何一项不满足都会在调用 Docker 前拒绝。构建上下文
是权限 `0700` 的指定 commit 私有快照；dirty override 只保留给 deploy/recovery，不能授权
build。Docker 不读取构建期间仍可变化的工作目录。

镜像产生后，脚本核对 image digest、五个 OCI label（version、Git revision、源码身份、Compose
manifest 哈希、配置哈希），并在禁网只读容器中重新计算 `/app/config.yaml` 的 SHA-256。全部
通过后才写入权限 `0600` 的本地 build contract；`deploy` 默认在任何字段缺失或漂移时拒绝
继续，带 build contract 的 rollback 也会复核镜像内契约。紧急绕过必须同时设置明确的操作人
和原因并写入审计日志。契约和 label 只记录 ref/哈希/源码身份，绝不能写入凭据值。
`verify` 仍不替代运行验收；容器健康、release state、只读接口和 `ops` 检查须单独通过。

执行回滚前先只读确认 release registry。脚本只接受 `current`/`previous` 中保存了完整
manifest 与健康集合的目标，不支持任意历史标签。查询和回退命令见
[日常操作入口](../ops/DAILY-OPS.md)。

回滚使用历史发布时归档的拓扑和健康集合，而不是拿旧镜像套用今天的 Compose 文件。若迁移
前的旧 manifest 仍把宿主机 `./config.yaml` 挂到 `/app/config.yaml`，必须在任何
`git checkout`、`git pull`、配置编辑或构建之前完成一次性封存：

```bash
CURRENT_REF="$(cat data/releases/current.ref)"
./scripts/release.sh seal-current "$CURRENT_REF"
# 只读核对 current.state 的 manifest 已不再包含 ./config.yaml bind 后，
# 才能 checkout/pull/edit，然后按 build -> deploy 顺序发布新版本。
```

`seal-current` 把此刻仍属于旧发布的配置复制到权限受控的 release registry，生成绝对绑定该
快照的新 manifest，并原子更新 current bundle/mirrors（ref、digest 和健康集合不变）。以后
修改宿主机配置不会改变该回滚目标。`deploy` 仍会在发现未封存旧 manifest 时先做保底封存并
在任何新版本 preflight/容器变更之前持久化 current state；但它只能捕获 deploy 当时已经可见
的配置，不能证明这是 checkout/pull 之前的旧发布配置，不能代替上述显式步骤。

## 3. 应用回滚不等于数据回滚

镜像回滚只处理应用与 Compose manifest，不恢复 SQLite。若某次变更包含数据库迁移，
发布前必须有经过 `quick_check`、SHA-256 校验并完成隔离恢复演练的备份。

当前主机执行 [NO_BACKUP 策略](../ops/README.md#jd-policy-no_backup-2026-09-10)。
本文专项迁移、重放和数据库恢复的备份前提仍有效，但不授权创建备份、恢复演练或恢复备份调度；
涉及这些操作时，沿用该策略中的单独授权边界。

SQLite 使用 WAL 和单 Writer 协调。API、Worker、行情采集、备份、恢复与迁移必须遵守
项目锁和维护哨兵，不能仅停止一个容器就假定没有写入者。

## 4. 事件个股映射重放

事件个股映射按 canonical event group 原子重放，并把输入版本、映射契约和输出指纹写入
`event_mapping_replay_ledger`。日常事件增强只治理最近窗口；事件版本、基础证据或旧镜像输出
发生变化时，`event_mapping_replay_dirty_roots` 会让授权范围内对应事件组绕过时间窗口重新核验。
常规调度只处理新 writer/来源契约；旧-only dirty 组保留，不自动占用前向预算。仅显式历史维护
模式（`include_legacy=True`）持久、幂等地将旧 `asset-map:` current 输出的 canonical root 加入队列，
包括历史孤儿映射；该模式不是 recent cron 的默认行为。

事件重放契约与回测使用的宽泛 `get_mapping_version()` 已分离。事件契约由代码中显式的
`EVENT_MAPPING_CONTRACT_VERSION`、`EVENT_MAPPING_SEMANTIC_FINGERPRINT` 与证券主数据有效解析
视图共同组成。当前显式契约相对 d359 使用的运行时 AST/hash 契约是一个新的、不可变的 ledger
generation；旧 generation 不会删除或重写。需要迁移历史输出的 store/schema、canonical group
归约或持久投影变化，必须在同一提交递增版本与指纹，发布前完成生产副本差异审计和受控全历史
重放。页面、监控和纯事件图变化不递增。此流程不是任何新代码发布的自动历史重算授权。

### 已批准的仅前向修复边界

来源证据与标题身份修复采用“仅修复今后写入，旧结果保留并明确标识”，不复制生产库、不重放或
改写历史 payload。该策略不递增 store-generation、不触发旧映射的 migration seed；另外用显式
`publisher-title-v2` 来源契约和 `title-only-v2` writer 身份区分新旧写入：

- 新原标题证据使用 `article_snapshot:publisher-title-v2:<SHA-256>`，快照只保存该原标题，摘录为空。
- 绑定新映射的来源使用 `mapping:publisher-title-v2:<mapping_id>:<base_evidence_id>`；不借用旧摘录。
- 回测映射身份为 `asset-map:title-only-v2:<rules_hash>`；事件增强身份在既有 store-generation 后
  增加 `writer-title-only-v2:rules-<rules_hash>:master-<master_hash>`，不冒充旧 writer 契约。
- 常规增强只选新来源契约的证据；旧-only dirty roots 不占用新证据预算。新旧混合事件组不按新
  规则重新计算旧证据，不退休旧 writer 输出；旧载荷与 ledger 保留。
- 前向 writer 的规则关联也只读原标题：生成的事件主题/tag、摘要与理由不驱动新概念关联；
  方向继续作为已有元信息保留，不作为公司/主题识别依据。显式历史模式保留旧规则输入行为。
- 新自然观测可正常追加不可变 mapping revision，并切换 `is_current` 指针。此正常交接不等于
  用新规则回填旧观测，更不会覆盖旧 revision 的 payload。
- 读取端明确区分当前契约、历史契约和 `legacy_unknown`。旧摘录、旧“原文直接点名”理由仅是
  未按新来源契约核验的存档，不能因为页面运行在新版本而成为新证据。

`include_legacy=False` 是常规映射入口的默认值；显式全历史维护入口才可使用 `include_legacy=True`，
并继续遵守独立授权、审计和维护锁。该历史模式使用无新 writer 标记的身份，不将旧输入贴成新
来源。前向批次成功不代表全历史迁移完成；历史未核验范围保留未知。此轮不运行历史模式。

证券主数据哈希只包含 active equity 集合中 resolver 实际读取的 `security_id/name/ticker`；active
或 equity 成员资格通过集合是否入选体现。aliases、industry、concepts、currency、version 和
`updated_at` 等不影响解析结果的元数据不会触发全历史重放。同名证券不再按刷新时间抢占，始终
按稳定 `security_id` 决胜。

需要全历史迁移的显式 store/schema 契约首次发布必须按新 generation 治理（上述仅前向 writer
例外不属于此类）：在生产副本确认新旧输出差异，并在正式维护窗口完成
受控全历史重放和验收。后续只有实际映射语义、有效证券解析视图或显式版本/指纹变化才应产生
下一代新契约。

映射结果影响事件相似边时，会在同一事务写入独立的
`event_similarity_replay_dirty_roots`；事件图只有在该 root 的公开相似边没有待处理写入后才确认并
删除标记。独立队列避免新的证据/事件版本失效信号覆盖尚未完成的图回收。首次升级会把现有
`event-graph-v1` 边的两个 canonical 端点加入该队列，以治理时间窗外的历史边。

证券主数据代次属于映射契约，但日常调度不会用今天新增或更名的证券无限追溯全部历史。
全历史重放只用于经过审计的规则迁移或专项治理，并须先在生产 SQLite 副本演练。隔离副本或
已经持有全部维护锁的 current-release one-shot 可显式使用：

```bash
python scripts/run_event_enrichment.py \
  --mapping-all-history --limit 2000 --max-pairs 10000
```

该模式会在映射前后各执行一次事件图。任务可续跑。每次运行都会输出权威的
`mapping_migration` 计数与顶层 `migration_complete`；只要旧 current、pending evidence、dirty roots
或 exhausted roots 任一未归零，命令就以非零状态退出，不能把批次成功误报为迁移完成。
`--limit` 是每批证据预算，不是历史总量；生产规模副本应使用上述有界批次反复执行，
避免一个超长事务或外部维护超时掩盖已经提交的断点。`event_mapping_replay_dirty_roots=0` 只说明
升级 seed/变更队列已清空，不能证明尚未入账本的历史 `no_match` 证据也已扫描。不能把一次返回
`0` 等同于历史已经收敛。验收至少要求：

- `mappings.deferred_groups`、`failed_evidence`、`unreplayable_groups`、
  `exhausted_groups` 均为 `0`；
- 顶层 `migration_complete=true`，且 `mapping_migration.old_current_mappings`、
  `pending_evidence`、`dirty_groups`、`exhausted_groups` 均为 `0`；
- 再运行一次时 `mappings.processed_evidence=0`，事件图也没有 deferred merge/similarity；
- `PRAGMA quick_check` 为 `ok`、外键违规为 `0`，并创建包含 replay ledger 与 dirty roots 的
  新备份并完成隔离恢复演练。

`/event-radar-card`、登录后的事件雷达和 `/events/*` 只展示事件内容，不暴露内部回放队列或账本。
`/events/*` 的条目查询仍是独立的公开 Feed 窗口；条目数据库读取失败时三种格式都返回可重试的
`503`，不会伪装成合法空 Feed。

不要从宿主机裸跑上述命令。生产应用必须继续遵守本节前述维护哨兵、锁顺序、不可变发布
镜像和备份边界；历史 revision 只能退役 current 指针，不能删除或重写。

`merge_contract` 是不可逆事件合并的数据库 epoch fence。迁移后的 SQLite 若回滚到不认识该
契约的 previous 镜像，旧事件图 writer 在遇到可合并候选时会按设计失败关闭；映射 lane 等
非合并写入仍可继续。这是预期的安全降级，不得为消除 cron 告警而删除 trigger、清空契约或
手工改写事件状态。需要恢复事件图写入时应切回当前镜像；若业务必须长期运行 previous 镜像，
必须恢复与其匹配的迁移前数据库备份，并按完整数据回滚流程执行，不能只回滚容器镜像。

## 5. 数据库恢复

真实恢复 `cache.db` 时：

1. 保持所有应用、计划任务和 SQLite reader/writer 停止；
2. 验证备份哈希、SQLite `quick_check` 和必要表；
3. 将当前 `cache.db`、`cache.db-wal`、`cache.db-shm` 作为一组移动到权限受控、可恢复的
   隔离位置，不要裸删 sidecar；
4. 放入经过验证的备份，并核对 owner、mode、普通文件及非符号链接属性；
5. 在同一维护窗口旋转 Integration Outbox epoch；
6. 创建新的恢复后备份；
7. 完成 Quant reconciliation 后，再启动服务并执行健康和只读接口验收。

epoch 旋转命令见 [Quant 集成契约](quant-integration.md#恢复与-epoch)。不要静默推进旧
Quant 游标，也不要把清空 retained Outbox 当作普通恢复步骤。

## 6. 主机资产与备份边界

`ops/install.sh --apply` 会以 root 权限修改 systemd/cron/主机资产。先审阅差异并运行
`ops/verify.sh --source-only`，确认维护窗口后再应用；随后由 `ops/verify.sh` 检查内容、
属主、权限和 cron 漂移。

本地备份只证明存在本机恢复点，不等于异地灾备。未配置加密异地上传、完整恢复演练和
实际 RPO/RTO 验证前，不应对外宣称灾难恢复能力。

## 7. 容量取舍

- SQLite 仍是单 Writer 数据库；持续写竞争出现时应评估 PostgreSQL。
- Redis 使用 AOF `everysec`，只承载会话、限流和短期共享缓存。
- Glance 实时卡片通过短周期 HTTP 轮询读取 SQLite，不维护长连接事件流。
- Web 缓存缺失的 RSS/LLM 慢路径有并发上限，避免挤占 Dashboard 与缓存读取。
- Event Radar 使用 keyset pagination；事件修订可能在不同页面请求间改变排序。

## 相关入口

- 发布脚本：[`scripts/release.sh`](../scripts/release.sh)
- Outbox epoch：[`scripts/rotate_integration_outbox_epoch.py`](../scripts/rotate_integration_outbox_epoch.py)
- 主机运维：[`ops/README.md`](../ops/README.md)
