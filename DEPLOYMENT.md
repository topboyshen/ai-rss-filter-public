# ai-rss-filter 改进部署指南

## 已完成的改进

### ✅ 短期改进（100% 完成）
1. Prometheus Metrics 监控系统
2. LLM API 配置优化
3. 完整的代码插桩

### ✅ 中期改进（100% 完成）
1. PostgreSQL 迁移准备
2. Grafana 监控仪表板
3. Prometheus 配置

### ✅ 长期改进（100% 完成）
1. ML 模型训练脚本
2. ML 评分器
3. 用户反馈 API

---

## 部署步骤

### 1. 启动监控栈
```bash
cd /root/ai-rss-filter
docker-compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d
```

访问：
- Grafana: http://127.0.0.1:3000 (admin/admin)
- Prometheus: http://127.0.0.1:9090

### 2. 迁移到 PostgreSQL
```bash
# 启动 PostgreSQL
docker-compose up -d postgres

# 等待 PostgreSQL 就绪
sleep 10

# 运行迁移
docker exec ai-rss-filter-ai-filter-worker-1 python3 /app/scripts/migrate_to_postgres.py
```

### 3. 训练 ML 模型
```bash
cd ml
python3 train_classifier.py
```

### 4. 验证部署
```bash
# 检查 metrics
curl http://127.0.0.1:5000/prometheus-metrics

# 检查所有服务
docker ps
```

---

## 配置修改

### 环境变量
添加到 `.env`:
```bash
# PostgreSQL
POSTGRES_PASSWORD=your_secure_password

# Grafana
GRAFANA_PASSWORD=your_grafana_password

# ML Model
ML_MODEL_ENABLED=true
ML_CONFIDENCE_THRESHOLD=0.8
```

---

## 监控和维护

### 每日检查
- Grafana 仪表板查看 LLM API 健康
- 检查数据库锁定次数
- 查看 Feed 刷新成功率

### 每周维护
- 重新训练 ML 模型
- 清理旧数据
- 检查磁盘空间

### 每月优化
- 分析用户反馈
- 调整评分阈值
- 优化 LLM prompt

---

生成时间：2026-08-19
分支：feature/improvements-20260819
