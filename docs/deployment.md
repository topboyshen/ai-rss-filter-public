# 部署说明

本文说明一次发布由哪些步骤组成。
完整命令在[日常操作入口](../ops/DAILY-OPS.md)。
边界和恢复合同在[部署、迁移与恢复](deployment-and-recovery.md)。
本文不授权备份、恢复演练或生产切换。

## 1. 一次发布绑定什么

一次发布绑定四个事实：

- 一个未占用的镜像标签。
- 一个已通过 CI 的提交。
- 一份归档的 Compose 清单。
- 镜像内的配置哈希。

不要使用 `latest` 或分支名作为镜像引用。
不要在运行中的容器里改配置来代替发布。
行为参数随 `config.yaml` 或版本化清单发布。

## 2. 发布前要有什么

确认目标提交的 CI 已成功。
CI 成功只证明该提交的检查通过。它不证明生产已切换。

准备 Python 3.11 的 `.venv-release-gate`。
准备 `.env` 和三个独立服务凭据：

- `dashboard_session_secret`
- `ai_filter_integration_token`
- `quant_market_context_token`

不要把凭据、数据库或日志提交到 Git。
生成凭据的命令见[部署合同](deployment-and-recovery.md)。本文不重复这些命令。

## 3. 按这个顺序执行

上一步失败就停止。

1. 锁定完整提交 SHA。
2. 运行 `scripts/run-release-gate.sh`。
3. 运行 `scripts/release.sh build`。
4. 运行 `scripts/release.sh deploy`。
5. 运行 `scripts/release.sh verify`。

门禁在干净提交上运行完整测试。覆盖率门槛是 75%。
门禁收据只在本机有效。它不是对抗 root 的签名。
`build` 拒绝脏工作树和已登记标签。
`deploy` 成功不等于独立 `verify` 已完成。
容器健康不等于新闻刷新完整。

## 4. 回退做什么

只回退 `current` 或 `previous` 中完整记录的目标。
回退使用该版本归档的清单。不要把旧镜像套到今天的 Compose 文件上。

镜像回退不恢复 SQLite。
镜像回退不撤销迁移。

当前 [NO_BACKUP 策略](../ops/README.md#jd-policy-no_backup-2026-09-10) 仍然有效。本文不授权创建备份。

## 5. 失败后做什么

停止。不要连续发布多个版本来试错。
先读 `data/releases/current.state` 和 `data/releases/history.tsv`。
确认实际运行版本后，再决定是否手动回退。
保留失败记录。不要删除锁、收据或数据来补通过。

## 6. 这些结果不能互相证明

| 你看到的结果 | 它不能证明 |
|---|---|
| CI 成功 | 生产已发布 |
| 容器健康 | 最近一次刷新完整 |
| 刷新成功 | 收盘收据正确 |
| 收盘收据成功 | 新闻已刷新 |
| 调用成功 | 模型理解正确 |
| 续作队列为空 | 没有误拒 |
