# AI-RSS-Filter 文档索引

根目录 [README](../README.md) 面向首次访问者，说明项目定位、模块边界和最小启动。
本目录保存需要长期维护的数据契约、集成与运维说明。

| 文档 | 适合谁 | 内容 |
|---|---|---|
| [新闻首页与 Glance 阅读布局](../ops/GLANCE-READING.md) | 读者与站点维护者 | 新闻优先、手机布局、四页外观及配套安装／验收边界 |
| [首页今日事件变化](../ops/HOME-EVENT-CHANGES.md) | 读者与站点维护者 | 日报有限投影、采集缺口、日周报入口及独立 Glance 配置候选 |
| [模型质量与数据契约](model-quality.md) | 读者、策略与审计开发者 | 评分解释、历史后验、版本隔离和研究门槛 |
| [保守处理记录](neutral-records.md) | 读者与排查人员 | 登录入口、已记录原因、当前契约及有界只读查询 |
| [新闻处理与模型调用关联](news-processing-trace.md) | 评分与成本排查人员 | 新闻执行、既有调用日志与最终处理结果的关联及未知用量边界 |
| [Webhook 持久交接](durable-webhook-handoff.md) | 接收集成与发布人员 | 原始消息回执、严格同步、幂等、拒收保全及回退边界 |
| [首次被拒证据留存](first-rejection-retention.md) | 评分与审计开发者 | 可回放的首次被拒证据表、上限、追加型触发器、回放工具与隐私边界 |
| [Quant 集成契约](quant-integration.md) | AI/Quant 集成开发者 | 双向只读边界、Outbox 游标、市场旁证与恢复语义 |
| [部署、迁移与恢复](deployment-and-recovery.md) | 运维与发布人员 | 不可变镜像、服务凭据、数据迁移、回滚和备份边界 |
| [运行镜像依赖边界](runtime-image-dependencies.md) | 构建与审计人员 | 基础镜像锁定、打包工具清除、最终安装内容检查与审计限制 |
| [日常操作入口](../ops/DAILY-OPS.md) | 运维与发布人员 | 状态查看、有界等锁、正式发布入口及失败后的核对顺序 |
| [主机运维](../ops/README.md) | 生产运维人员 | systemd、cron、备份、恢复演练、监控与漂移验证 |

根 README 介绍主线能力，不承诺具体站点已安装全部页面或功能。站点外观和应用镜像
分别生效；部署版本以发布合同与运行核验为准，不在此索引维护易过期的生产标签。
发布命令集中维护在日常操作入口，本页不重复复制。

## 源码入口

- 评分与 Briefing：[`core/`](../core/)
- HTTP 与 Feed：[`routes/`](../routes/)
- 数据与稳定事件：[`db/`](../db/)
- 后验、Quant 和事件服务：[`services/`](../services/)
- 收盘行情与证券主数据：[`workers/`](../workers/)
- 测试契约：[`tests/`](../tests/)
