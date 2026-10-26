# 评分人工 Gold v2：证据 → 盲标 → 理由审查 → 仲裁

**现状：工程工具不是已完成人工 Gold。七项真实标签仍须由人填写。**
规则回放、合成测试、HTTP 成功率、校验验收率和路径占比都不是模型准确率或投资胜率。
本版仅本地实现；未自动部署、启用采集、重新导出生产工件或执行模型对照调用。

## 1. 来源与评估单位

- 来源冻结在 scorer 实际调用边界：原始标题、清洗后的 RSS 摘要、输入 hash、策略身份。
  摘要可以为空——前提是**当时实际输入就是空字符串**，而不是事后从空缓存猜测。
- 同时保存 stage、配置中的 backend/model、初次/修复标记、请求 hash、请求是否含新闻来源、
  原始返回、解析值、验收结果、主题/数字事实/事件事实失败类别。短 copy-only repair
  不含新闻数据，不能冒充“模型重新理解了来源”；`source_in_request=false` 明确区分。
- 数据库写入成功、取得 event ID 后才发布证据；保存最终决策路径用于审计。
  来源不读取 `news_cache.summary`，也不使用后生成的摘要、展示标题或展示理由补证。
- 候选单位是 **validated_model_response**：有评分可解析的单次返回，包括被拒绝的理由。
  不是新闻最终决策、全部 HTTP 调用或修复成功数。没有响应的传输失败不在此分母中；
  格式失败的原始返回留在证据文件，但无有效分数的返回不进入七标签评估。
  缓存命中不伪造新的模型尝试。没有采集到的历史尝试不补写。
- 每包一个 `strategy_version`；事件 ID 与归一化事件 key 联合去重，一事件最多一个返回。
  动态事件归并仍可能遗漏语义重复；置信区间不是对全体新闻独立性的保证。

## 2. 受控采集与单批预算

`services/score_evidence_capture.py` 不联网、不操作数据库、不上传、不自动删除文件。
功能在未配置时仍默认关闭。以下仅为合成路径的配置示例，不代表对任何环境的采集授权；启用前须确认来源文本留存边界：

```yaml
score_evidence_capture:
  enabled: true
  directory: /app/data/score_evidence/example-cohort
  max_records: 500
  max_record_bytes: 262144
```

目录必须为调用者所有、0700，不经过符号链接；文件 0600。总量上限默认 500，
可配 1–5000；单文件默认 256 KiB，可配 1 KiB–1 MiB。每条最多 16 次校验返回。
超限、输入不一致或缺事件身份不截断后冒充完整证据；采集失败不改变分数，写低基数告警。
单文件字节数包含 UTF-8 编码及末尾换行，超限整条拒绝，不截断。500 条记录至多约 125 MiB，
不含目录、锁和完成标记等文件系统开销。写入通过跨进程锁、内容 hash、独占原子发布防止覆盖。
满额写入私有 `.capture.complete` 标记；重启或人工移走记录也不自动补采。无自动轮换、备份或上传。
下一批须另行授权新目录；不得删除完成标记来自动续采。
若使用文档中的默认安装目录，示例宿主路径为
`/root/ai-rss-filter/data/score_evidence/example-cohort`；
父目录与批次目录均 0700，文件及标记 0600。通过 Git 忽略规则防止意外提交。
采集只冻结来源、模型返回与决策审计，不生成七项人工标签；后续候选导出仍全部留空。
证据含完整来源和模型输出，是私有审查材料；不要放公共目录、Prometheus 标签或代码仓库。
请求仅保存 hash，不保存系统提示/凭据；文章 link 仅保存 hash，不保存可能带 token 的 URL。

## 3. 不可覆盖的分契约候选包

在证据所在的受控环境操作，不把生产数据库或凭据复制到开发机：

```bash
python scripts/export_score_gold_candidates.py \
  --evidence-dir /ABS/PRIVATE/score-evidence-run \
  --out-dir /ABS/PRIVATE/gold --run-id review-001 \
  --strategy-version 'EXACT_CONTRACT' --target 300 --seed 20260911
```

`run-id` 不可复用。每个契约目录名取契约 hash，`index.json` 保存映射。
目录内包含 `candidates.json`（只给审定负责人）与空白 `source.csv`。
JSON/CSV 共用内容稳定的 `gold_id`、`input_hash` 和契约字段；不截断来源或被拒理由。
JSON 内绑定原始 evidence，源/预测/包 hash 有差异时拒绝导入或计算。
重复执行不能覆盖 JSON、CSV 或已有人工编辑；失败目录保留，不静默重试覆盖。

容量受限的联合贪心抽样中，一条候选同时贡献所有分层；优先保留可用高分配额，
最终不再随机截断。记录种子、有序输入 hash、事件库存、配额、达到数和短缺。
这是启发式算法，不保证发现所有可行配额解，也不把所有短缺说成“库存不足”。
目标允许 1–500；不足 200 仅作候选/诊断，不能 finalize 为完整 Gold。

`--db` 与 `--evidence-dir` 二选一。`--db` 使用 SQLite `mode=ro`，仅输出
**legacy_cache_diagnostic / evidence_incomplete**，不拿缓存摘要或展示标题补原始输入，
不能进入人工导入或指标流程。旧混合包/旧格式工件原样保留，v2 不猜测迁移它们的标签。

## 4. 必须人工完成的两阶段工作

| 阶段 | 字段 | 取值 |
|---|---|---|
| 来源盲标 | relevance、should_be_selected | 0/1 |
| 来源盲标 | direction | 看多 / 看空 / 中性 |
| 来源盲标 | score_band | high / mid / low |
| 理由审查 | factual_claim_correct、theme_correct | 0/1/na |
| 理由审查 | reason_is_supported | 0/1 |

分档契约：high 为 7.5–10，mid 为 5–<7.5，low 为 0–<5。
`should_be_selected` 是人工按冻结投资新闻相关性/催化标准的判断，不照抄模型结果。
`na` 仅用于确实没有可审查的事实/主题主张，不代表“不确定”；有争议交仲裁。

1. 负责人只向两名独立标注者发 `source.csv`，不发 candidates JSON。
   CSV 不含模型分数、方向、模型身份或理由；每人填写四项来源标签与 `annotator_id`。
2. 冻结两份来源盲标后导入，工具此时才生成 `reason.csv`：

   ```bash
   python scripts/import_score_gold_labels.py candidates.json --phase source \
     --csv source-A.csv source-B.csv --out-dir /ABS/PRIVATE/gold/source-frozen \
     --human-attestation
   ```

3. 两人独立查看来源和 tag/logic，填写三项理由标签。仍不展示模型分数、方向、后端。
   任何七项标签的分歧都由与原评审不同的第三人填写 `adjudication.csv` 的完整七项标签。
   无争议行可留空；程序不会推断缺失值或用多数/模型投票补标签。
4. 完成理由导入与审定：

   ```bash
   python scripts/import_score_gold_labels.py source-reviewed.json --phase reason \
     --csv reason-A.csv reason-B.csv --adjudication adjudication.csv \
     --out-dir /ABS/PRIVATE/gold/final-001 --human-attestation
   python scripts/score_gold_metrics.py /ABS/PRIVATE/gold/final-001/finalized.json
   ```

没有分歧时省略 `--adjudication`。相同审核者、缺行、重复 ID、未知 ID、修改来源/契约、
非法枚举、空标签、缺证据、未解决争议均 fail closed，CLI 返回 2。
人工标签只从 CSV 原值导入；`--human-attestation` 是操作者对人工来源的声明，
不是密码学身份认证，不能证明真实人员独立性。不得由 agent、模型或脚本代填声明或标签。
测试中的合成标签只存在于临时 fixture，不是人工标注，也不计作评估结果。

## 5. 指标的严格含义

- `high_band_agreement`：预测高分中，人工也标 high 的比例。
- `high_selection_precision`：预测高分中，人工 relevance=1 且 should_be_selected=1 的比例。
- `safe_neutral_precision`：固定安全中性中，人工同时标中性、low、不应入选的比例。
- `safe_neutral_recall_on_gold_neutral`：上述人工应中性集合中，预测固定安全中性的比例。
- `sample_safe_neutral_share`：样本中的模板路径占比，**不是生产占比或覆盖率**。
- 方向一致率、理由可支持率、事实/主题正确率；后两者排除 na，并显示实际分母。

每项输出分子、分母、估计和 Wilson 区间；空分母为 null/N/A。另按 backend、model、
path、stage、attempt 拆分。配额样本不作总体加权推断，区间仅作样本条件下的描述。
拒绝混合契约、未审定工件、仅把 ready/unlabeled 标志手改掉的文件。

## 6. 真正的模型人工对照仍待开展

当前路由中的 Kimi/Qwen 输入不相同，repair 也不等于独立预测臂，不能直接排名。
下一阶段先冻结同一来源集合、预先约定样本/分层与指标，再分别运行独立预测臂，
保存各自完整契约、模型身份和请求证据；来源标签盲标，理由标签分别盲审。
必须核对 event/input hash、预测独立性、时间与语料范围，分别报告各契约结果，
再作配对差异分析。此工具不自动发模型请求，也不因有了 CSV 就宣称完成对照评估。
人工标注完成之前，不报告“模型变准”或投资效果。
