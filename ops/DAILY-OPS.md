# 日常查看、发布与恢复（一页）

本页只回答四件事：看状态、怎么发布、怎么恢复、失败后怎么办。
发布与回滚的完整契约见 [deployment-and-recovery.md](../docs/deployment-and-recovery.md) 与 [README.md](README.md)。

**历史核验记录**：2026-09-19，`my-ai-filter:2026.09.19.52`（运行提交 `736b320`）；当前运行版本以下方发布合同查询为准。
**命令约定**：标【只读】的可随时执行；标【操作】的会改变运行版本，必须逐步确认、逐步执行，
不要整段粘贴后任其跑完。

## 1. 看状态【只读】

| 想知道 | 去哪里看 | 命令／位置（仓库根下） |
|---|---|---|
| 当前运行版本 | 发布合同 | `sed -n '1,2p' data/releases/current.state` |
| 应用是否健康 | 健康入口 | `curl -fsS http://127.0.0.1:5000/health` |
| 容器状态与镜像 | Docker | `docker ps --format '{{.Names}} {{.Status}}'` |
| 最近发布记录 | 发布历史 | `tail -3 data/releases/history.tsv` |
| **新闻刷新**最近完整时间与 stale/degraded | 就绪接口 | `curl -fsS http://127.0.0.1:5000/readiness`（`last_full_refresh_at`、`seconds_since_full_refresh`、`refresh_in_progress`） |
| **收盘采集**最近收据 | 收盘工件 | `ls -t data/market_close/*_last.json` 后查看对应文件 |
| 零输出原因 | Feed 源健康面板 | `/ops-feeds` |
| 评分可用性 | 模型质量接口 | `/score-quality`（需请求头携带 API key；不要把密钥放进 URL） |
| 保守处理的已记录原因 | 保守处理记录 | 模型质量页 → 保守处理记录，需登录或有效请求头 |

- **新闻刷新与收盘采集是两套工件，不能互证**：`data/market_close/*_last.json` 只证明收盘任务结果；
  新闻刷新看 `/readiness`。
- 保守处理记录说明的是**保守处理的已记录原因**：主题关联、事实表述、格式字段属于校验类别；
  「初次中性」「原因未完整记录」不能当作已确认的校验失败。它不指出具体缺少哪条证据，
  也不代表模型准确率或投资效果。

## 2. 发布【操作】

唯一正式入口是 `scripts/release.sh`；发布前先锁定**已通过 CI 的具体提交 SHA**，
每一步只有上一步成功才继续。`origin/master` 可能已经前进，不能用它代替要发布的版本。
首次发布先按[生产部署说明](../README.md#完整生产部署)准备 Python 3.11 门禁环境。

```bash
git fetch origin master
git log --oneline -1 origin/master        # 先看它现在指向哪
git merge --ff-only <已通过CI的SHA>        # 无法快进就停下核对，不要强行合并
./scripts/run-release-gate.sh             # 固定全量测试 + 覆盖率；失败即停
RELEASE_LOCK_WAIT_SECONDS=30 ./scripts/release.sh build "my-ai-filter:<未占用的新版本号>"
RELEASE_LOCK_WAIT_SECONDS=30 ./scripts/release.sh deploy "my-ai-filter:<同上>"
RELEASE_LOCK_WAIT_SECONDS=30 ./scripts/release.sh verify "my-ai-filter:<同上>"
```

- 版本号必须是未使用过的新标签；`build` 拒绝浮动引用与已登记标签，并要求干净的工作树。
- `RELEASE_LOCK_WAIT_SECONDS` 为 0–300 的整数，默认 0（立即取锁）；显式设为 30 可等待
  正常维护任务释放锁。等待超时与 `flock` 执行错误会分别输出原因及原始返回码，
  对外仍返回 6。失败后先核对历史与运行状态，不自动重跑；继承 FD 调用保持原有协议。
- SSH 可能中断的长命令沿用已有 `systemd-run --service-type=oneshot` 方式，指定唯一 unit、
  工作目录和日志路径。先查看该 unit 的退出状态与日志，再结合发布历史确认结果；
  命令没有返回输出时，不重新启动同一发布步骤。
- `IMAGE_REF` 保护不变：定时任务从 `data/releases/current.state` 读取不可变镜像引用；
  裸 `docker compose` 因缺少 `IMAGE_REF` 报错属预期，不要为此新增门禁或改 Compose。

## 3. 恢复【操作】

```bash
sed -n '1,2p' data/releases/current.state      # 先只读：当前实际运行版本
sed -n '1,2p' data/releases/previous.state     # 再只读：可回退的目标
./scripts/release.sh rollback "my-ai-filter:<previous 中的版本号>"
```

- 只接受 `current`/`previous` 中保存了完整 manifest 与健康集合的目标。
- 回滚使用该版本发布时归档的拓扑，不是拿旧镜像套今天的 Compose 文件。
- **回退镜像不等于回滚数据库**：镜像回退不恢复 SQLite，也不撤销迁移或冻结账本。
  含迁移的变更需另有人工确认的备份与恢复方案。
  当前 [NO_BACKUP 策略](README.md#jd-policy-no_backup-2026-09-10)继续适用；本页不授权创建备份或恢复备份调度。

## 4. 失败后怎么办

- **停止，不连续补版本**：一次失败就记录并停下，不在一轮里连发多个版本试错。
- **先确认是否已自动回滚**：脚本默认支持自动回滚。失败后先只读查看
  `data/releases/current.state` 与 `data/releases/history.tsv` 最新记录，确认实际运行版本，
  再决定是否需要手动回退，避免重复操作。
- 保留信息：失败命令与输出、`history.tsv` 对应行、`data/releases/` 下的 build contract 与
  manifest、相关 systemd 日志、当时的健康与源健康输出。不要删除失败证据。
- 明确不做：不手改 `complete` 标记、不伪造 attestation、不为“补通过”放宽校验或阈值、
  不重建或删除 `data/` 下的数据与工件。

## 边界

本页只说明日常操作与“处理原因可查看”，不代表模型准确率或投资效果；
不新增监控系统或门禁。
