# ai-rss-filter 改进部署指南

## 已完成的改进

### ✅ 短期改进（100% 完成）
1. Prometheus Metrics 监控系统（认证端点 `/metrics`，调用方式见 §4；可选 Prometheus 抓取栈未启用）
2. LLM API 配置优化
3. 完整的代码插桩

### ⚠️ 中期改进（仅配置，未进入默认部署）
1. PostgreSQL 迁移准备（脚本保留，镜像与编排都不提供 PostgreSQL，见 §2）
2. Grafana 监控仪表板（仪表板文件不会加载，见 §1）
3. Prometheus 配置（无活动抓取任务，见 §1）

### ⚠️ 长期改进（原型代码，未接线）
1. ML 模型训练脚本（只有 7 行打印占位信息，不产出模型；原型文件已随死代码清理移除）
2. ML 评分器（没有调用方，依赖的 `joblib` 也未在任何 requirements 中声明；原型文件已随死代码清理移除）
3. 用户反馈 API（原型蓝图未注册，实际生效的反馈入口是 `POST /feedback`；该蓝图已随死代码清理移除）

---

## 部署步骤

### 1. 启动监控栈

> 注意：这是历史可选栈，不属于默认部署，起来后也没有数据：
>
> - `monitoring/prometheus.yml` 的 `scrape_configs` 为空。Pinned Prometheus 2.48 无法在抓取配置里携带 `/metrics` 要求的 `X-API-Key`，所以没有活动抓取任务；
> - `monitoring/grafana/dashboards/` 缺少 provisioning provider 文件，里面的仪表板 JSON 不会被加载；即使加载，其面板查询的指标（如 `ai_filter_llm_calls_success`）也不在 `/metrics` 的输出里。
>
> 日常指标检查请用 `ops/ai_filter_ops_check.py`，细节见 `ops/README.md` 的 “Optional Prometheus template (inactive)”。

```bash
cd /root/ai-rss-filter
docker-compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d
```

访问：
- Grafana: http://127.0.0.1:3000（密码取 `GRAFANA_PASSWORD`，未设置时为 `admin`）
- Prometheus: http://127.0.0.1:9090

### 2. PostgreSQL 迁移（不在默认部署路径内）

当前镜像与 `docker-compose.yml` 都不提供 PostgreSQL：

- 镜像只 COPY 白名单脚本，不包含 `scripts/migrate_to_postgres.py`；
- 编排里没有 `postgres` 服务，`docker-compose up -d postgres` 会直接报错；
- 该脚本 `import psycopg2`（v2 API），而 `requirements-ops.txt` 声明的是 `psycopg` 3.x，装上也跑不起来。

默认部署使用 SQLite，不需要这一步。确实需要迁到 PostgreSQL 时，必须自行准备实例、安装 `psycopg2`，并把脚本单独拷入环境后运行；`scripts/migrate_to_postgres.py` 仅作历史参考保留。

### 3. 训练 ML 模型

历史版本这里的命令是 `cd ml && python3 train_classifier.py`，但该脚本只有 7 行、仅打印 “ML model training script created”，不读数据也不产出模型；`ml/` 也从不在镜像白名单里，容器内没有这个目录。这个原型文件已随死代码清理移除，默认部署不需要、也无法执行这一步。

### 4. 验证部署

指标端点已从 `/prometheus-metrics` 改为需要鉴权的 `/metrics`（未带 key 返回 401，旧路径返回 404）：

```bash
# 检查 metrics（key 取自配置 security.api_key 或请求头约定，勿写进 URL）
curl -H "X-API-Key: $AI_FILTER_API_KEY" http://127.0.0.1:5000/metrics

# 检查所有服务
docker ps
```

---

## 配置修改

### 环境变量
添加到 `.env`:
```bash
# Grafana（仅影响可选的监控栈）
GRAFANA_PASSWORD=your_grafana_password
```

以下变量在文档历史版本中出现过，但当前代码不读取，设置后没有效果：

```bash
# 未被任何 compose 服务使用（编排里没有 postgres 服务）
POSTGRES_PASSWORD=your_secure_password

# 未被任何代码读取；ML 评分器也没有接线
ML_MODEL_ENABLED=true
ML_CONFIDENCE_THRESHOLD=0.8
```

---

## 监控和维护

### 每日检查
- 用 `ops/ai_filter_ops_check.py` 读取鉴权后的 `/metrics`（Grafana 栈没有抓取任务，看不到 LLM API 健康）
- 检查数据库锁定次数
- 查看 Feed 刷新成功率

### 每周维护
- 重新训练 ML 模型（当前没有可用的训练脚本，暂不适用）
- 清理旧数据
- 检查磁盘空间

### 每月优化
- 分析用户反馈
- 调整评分阈值
- 优化 LLM prompt

---

生成时间：2026-08-19
分支：feature/improvements-20260819
