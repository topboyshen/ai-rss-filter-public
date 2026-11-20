# 数据边界

[架构说明](architecture.md)的“运行部分（服务边界）”回答**谁在运行**：API、Worker、网关、Redis 和 RSSHub 各自做什么。本文回答**记录本身**的边界：一类记录代表什么、谁写、谁读、哪些必须留存、哪些可以从下层重建，以及证据不足时页面应该显示什么。本文是记录边界说明，不是生产验收，也不是当前数据快照。

本文用 **R01 / R18**（同一座储能电站并网事件）走一遍从来源到页面的链路。示例取自本地冻结的事件雷达 P3 样本（`p3-sample-manifest.csv`、`p3-labels.csv`）。示例不查询生产。示例时间只属于该样本，不代表当前状态。

- R18 是目标事件，`event_id = 6bd0886a-b842-4c5a-84fe-3cdbf1582392`。标题是“世界首个300兆瓦人工硐室型压缩空气储能电站并网发电”。事件时间（`occurred_at`）是 2026-11-16 10:37:32（北京时间）。
- R01 是已合并源事件，`event_id = d9de97ac-752d-4216-a0f0-14b672076bb1`。标题相同。事件时间是 10:49:47。R01 于 11:15:29 合入 R18。
- 合并规则记录的理由是：“跨独立来源发布方的完整规范化标题与冻结快照均一致，且位于固定 36 小时窗口。”这是规则对发布方分组和快照的判断，不等于已完成独立采写核验。
- 列表只把 R18 作为**当前活跃事件**展示。系统保留 R01 作为可审计的源事件。

## 1. 这条链路里有哪些记录

| 层 | 记录 | 代表什么 | 谁写 | 谁读 |
|---|---|---|---|---|
| 来源观察 | `news_cache`、`article_sources` | 一条规范化链接的标题、评分、来源和首次/最近观察；同一链接被哪些 feed 看到 | Worker 刷新与评分路径（`services/refresh.py`、`core/processor.py`、`db/database.py`） | 评分、事件增强、情报页、事件雷达、回测与质量视图 |
| 信号账本 | `backtest_signals`、`backtest_signal_securities` | 一次评分结果及其冻结的证券快照：分数、方向、评分路径、策略/映射版本、入选状态、可用时间 | 评分路径写入；只追加 revision，不覆盖旧信号 | 事件结果、模型质量、历史后验、事件雷达 |
| 事件头 | `events`、`event_identities` | 合并后的代表性事件，以及“哪条文章/身份指向哪个事件” | 文章/信号入库与事件增强（`resolve_or_create_event`） | 事件雷达、日报/周报、首页摘要、API |
| 合并审计 | `event_merge_history` + `events.merge_state/merged_into` | 哪个源事件在何时、以什么理由并入哪个目标事件；版本号是多少 | 事件增强（`merge_events`） | 事件组读取、审计、页面历史说明 |
| 来源证据 | `event_evidence` | 一条来源实际说了什么：冻结标题/摘录、快照哈希与文本、立场、可靠度、观察时间 | 文章入库时冻结；映射时派生 mapping 证据 | 事件详情、来源列表、映射证据、页面阶段标签 |
| 公司映射 | `event_securities`、`event_mapping_replay_ledger` | 事件与证券的关系（直接/上下游/概念/市场引用）、方向、置信度、理由、版本；这次映射是否可复现 | 事件增强的映射 lane | 事件雷达股票条、结果页、审计 |
| 市场结果 | `event_outcomes` | 冻结的 T+1/T+3/T+5/T+20 结果：基准日、测量日、价格、收益、观察状态、方法/数据版本、revision | 结果物化器（`services/event_outcomes.py`） | 事件详情的市场反应、报告、后验统计 |
| 相似关系 | `event_similarities` | 两条事件在某个方法/模型版本下的相似度和理由 | 事件图 lane | 事件详情的“历史相似事件” |
| 页面读模型 | `services/event_radar_query.py` 的返回值、页面/报告缓存 | 列表、详情、来源、股票条、市场结果的只读投影 | Worker 刷新/发布，或查询时计算 | API、模板、日报/周报/首页摘要 |
| 调用旁证 | `llm_call_logs`、`news_processing_logs` | 某次模型调用的后端、耗时、响应哈希、处理关联 | 评分与处理路径 | 成本/故障排查、处理追踪 |

`news_cache` 不是全文库：它保留标题、原始标题、评分和来源信息，正文并不保证留存。`event_evidence.snapshot_text` 也是有界的标题/摘录快照，不是第三方文章全文。

**读写纪律**：

- API 和页面只读；`load_event_radar` 使用只读连接，不建任务、不调用模型、不写回。
- Worker 的刷新/评分路径写 `news_cache`、`article_sources`、`backtest_signals` 和文章级 `event_evidence`。
- 事件增强写 `events`、`event_identities`、合并历史、公司映射和映射回放账本。
- 结果物化器只写 `event_outcomes`；页面和报告不直接写这些表。
- 写入由刷新锁、数据库锁和发布锁串行化；停止一个容器不等于没有写入者。

## 2. R01/R18 从来源到页面

### 2.1 来源观察：先有“谁看到了什么”

1. 一财和同花顺的 feed 各出现相同标题。Worker 把它们写入 `news_cache`（按规范化链接去重），并在 `article_sources` 记录“这个链接被哪些 feed 看到”。
2. 入库时 `_record_article_evidence` 只把**发布方原始标题**冻结为来源快照：`source_uri`、`claim_key = article_snapshot:<写入契约>:<快照哈希>`（当前写入契约为 `publisher-title-v2`）、`snapshot_hash`、`snapshot_text`、`observed_at`、`announcement_at`。模型生成的摘要不能占据 `snapshot_text`；没有原始标题就不建立来源证据。
3. 于是 R01 和 R18 各自拥有文章级证据；两者的标题、快照和发布方关系满足合并条件后，R01 被并入 R18。

**边界**：`news_cache` / `article_sources` 证明“系统在何时看到了哪个链接”，不证明文章内容为真，也不证明发布方独立。`event_evidence` 的冻结快照才是来源证据；页面摘要、模型理由、相似事件都不是来源证据。

### 2.2 评分与信号：评分是历史行为，不是可随意重算的结论

1. 评分路径把结果写入 `news_cache` 的评分字段，并把正式信号追加到 `backtest_signals`；证券快照写入 `backtest_signal_securities`。
2. 信号记录 `available_at`、`scored_at`、`score`、`direction`、`score_path`、`strategy_version`、`mapping_version`、`selected`、`event_id/event_version` 等字段。后续自动化修订只能追加 revision，不能覆盖已发生的信号。
3. 市场结果只读取**已选中的正式信号**及其冻结证券快照；没有 `selected=1` 的正式信号，就不进入结果契约。

**边界**：评分和入选是一次带时间的历史行为。LLM 输出、规则命中和阈值都可能变化，所以**不能用今天的模型重算并覆盖旧信号**。`news_cache` 里的标题和信号可以被审计，但“当时为什么是这个分数”只能由评分路径、契约版本和调用旁证解释，不能由页面结果反推。

### 2.3 事件形成：事件头是可审计的合并结果

1. 文章链接先解析成事件身份（`event_identities`），再调用 `resolve_or_create_event` 创建或更新 `events`。
2. `events` 保存标题、类型、方向、重要度、发生/可用/首末观察时间、版本、`merge_state`/`merged_into`、策略/模型/合并契约。`event_identities` 保存身份到事件的映射。
3. 同一个身份重复出现只推进观察时间；只有元数据或身份变化才增加 `events.version`。

**边界**：事件标题是合并后的代表性主张。它不等于所有来源共同确认。事件 ID、版本和首末观察时间属于历史记录。系统不能按“同标题”重新建一个事件，否则会破坏 R01/R18 的审计关系。

### 2.4 合并：R01 → R18 是一次不可改写的历史动作

1. 事件图判定 R01 与 R18 为同一事件后，`merge_events` 在同一个事务里：
   - 更新目标 R18 的重要度、可用时间、首末观察时间和版本；
   - 向 `event_merge_history` 追加一条源/目标、双方版本、理由、actor、合并时间的记录；
   - 把 R01 置为 `merge_state='merged'`、`merged_into=R18`，并写入 `merge_contract`。
2. `event_merge_history` 有不可更新、不可删除的触发器；R01 的旧证据、旧映射和旧结果不会被搬走或删除。页面用递归事件组（`events.merged_into`）把 R01 的成员记录聚合到 R18 的详情里。

**边界**：
- 页面显示 R18 时，来源数可以包含 R01 的来源；但 R01 的证据行仍然属于 R01，不能改写成 R18 自己说过的话。
- 合并历史是审计记录，不是可重建的“当前状态”。如果规则改变，只能形成新的判断或新的记录，不能删除旧合并。
- 没有 `event_merge_history` 就不能声称两条事件曾经合并；读取层不能按标题相似度临时拼一个合并。

### 2.5 来源证据：快照是根证据，摘要不是

1. `event_evidence` 以 `(event_id, source_uri, claim_key)` 为自然键，保存来源主张、冻结快照、立场（`supports` / `contradicts` / `context`）、可靠度、公告/观察时间。
2. 映射时，系统还可以从同一来源快照派生出 mapping 证据（`mapping:<写入契约>:<mapping_id>:<evidence_id>`），把来源与某个证券映射绑定；它复制的仍是同一份来源快照，不是新的第三方事实。
3. 快照不可原地改写：同一 `claim_key` 的快照变化会被拒绝；发布方修改标题时应使用新的 `claim_key`，保留旧观察。

**R01/R18 的具体表现**：
- R18 详情按事件组读取到两个不同 `source_uri`（一财、同花顺），所以来源数是 2；R01 还有一条一财观察，但 R01 已合并，不能再算作独立活跃事件。
- 两条来源的主张都只有标题，没有点名南网储能或阳光电源。系统后来写入的两条 `concept` 映射没有原文公司名支持，页面必须标成“概念（来源未点名）”，不能标成直接影响。
- “两条来源”只说明有两个链接/发布方被观察到，不说明它们独立采写，也不说明事件已被多源交叉验证。

**边界**：来源快照不能从页面、摘要、映射理由或相似事件反推。上游文章消失时，`event_evidence` 的冻结快照是唯一可审计副本。系统不得用模型生成的摘要补造一份“原文”。

### 2.6 公司映射：关系有版本，概念不是事实

1. 映射 lane 读取事件组内的来源证据，运行确定性规则/主题池，写入 `event_securities`（`relation_type`、`direction`、`confidence`、`relevance`、`rationale`、`mapping_version`、`revision`、`is_current`）。
2. 每次映射回放写入 `event_mapping_replay_ledger`：证据版本、根事件版本、映射版本、输入/输出指纹、`mapped` / `no_match` / `failed`、尝试次数和失败码。
3. 当前映射必须有当前写入契约和可核对的来源证据；旧映射以 `is_current=0` 保留为历史，不因为“以前出现过”而自动升级为当前结论。

**R01/R18 的具体表现**：
- R18 有南网储能、阳光电源两条间接/概念映射。来源标题没有点名两家公司，页面显示“概念（来源未点名）”，并明确“仅为主题池映射；不构成公司事件关系”。
- R01 没有当前公司映射；这不等于 R01 的事件不存在，只等于这条源事件没有可展示的公司关系。
- 如果映射回放是 `no_match` 或 `failed`，页面应显示“来源未点名/证据不足”，不能从主题、摘要或相似事件猜一个公司。

### 2.7 市场结果：只冻结有信号、有映射、有行情的组合

1. 结果物化器从 `backtest_signals` 的选中正式信号和 `backtest_signal_securities` 的冻结映射出发，读取基准日和测量日的收盘价，写入 `event_outcomes`。
2. 每行结果固定：事件、证券、`mapping_id/revision`、`signal_ledger_id`、`horizon`、`baseline_at`、`measured_at`、`baseline_price`、收益、`method_version`、`data_version`、`revision`、`is_current`、`observation_state`。
3. `observation_state` 只有：`complete`、`not_due`、`missing_asset_close`、`missing_benchmark_close`、`legacy_unknown`。缺失不估算、不前向填充、不把未到期写成 0。

**R01/R18 的具体表现**：
- 冻结样本里 R01 有 2 条 `complete` 结果，R18 自身没有结果行。页面按事件组读取时，R18 详情可能看到属于 R01 的冻结结果；这些行的 `event_id`、`mapping_id`、`signal_ledger_id` 仍然是 R01 的，不能改写成“R18 的结果”。
- 如果 R18 没有选中的正式信号，市场反应页显示“该事件没有选中的正式信号，未进入冻结结果契约；T+1/T+3/T+5/T+20 不生成结果”，而不是显示 0% 或“无影响”。

### 2.8 页面：只读投影，不是新证据

1. `event_radar_query.load_event_radar` 用只读连接和 `read_at` 边界查询：只展示 `available_at/observed_at <= read_at` 的事件、证据、映射和结果；不调用模型，不写入数据库。
2. 列表按 `merge_state='active'` 取当前事件，来源数按事件组的**不同 source_uri** 统计；详情按事件组展开 R01/R18 的来源、股票条和结果。
3. 页面上的“页面数据快照”是读取边界/刷新时间，不是新闻发布时间，也不是新证据。分页、`sources_truncated`、`has_more` 表示展示被截断，不表示不存在更多来源。

**边界**：页面、日报、周报、首页摘要都是可重建读模型。刷新、缓存或截断都会让它们过期。它们不能反过来当来源证据、合并证据或结果证据。

## 3. 记录分类：留存与重建

| 记录 | 必须保留？ | 可重建？ | 原因 |
|---|---|---|---|
| `news_cache` | 是 | 记录可重放，评分不可保证 | 保存原始标题、评分路径、策略版本和展示结果；LLM 评分是历史行为，不能覆盖重算 |
| `article_sources` | 是 | 否 | 记录同一链接被哪些 feed 看到；历史观察无法从最终页面恢复 |
| `backtest_signals` / `backtest_signal_securities` | 是 | 否 | 不可变信号账本；选中状态、可用时间和冻结证券是结果计算的输入 |
| `events` | 是 | 部分 | 事件头可从事后身份重放，但事件 ID、版本、首末观察和合并状态是历史 |
| `event_identities` | 是 | 部分 | 身份解析的审计证据；历史冲突不能靠重跑消掉 |
| `event_merge_history` | **是，且不可改** | 否 | 合并是历史动作；规则回放只能产生新记录，不能删除旧合并 |
| `event_evidence` | **是** | 否 | 来源快照是根证据；摘要、页面和相似事件不能补造原文 |
| `event_securities` | 是（含历史 revision） | 条件可重建 | 规则、主档和来源快照齐全时可重放；重放写入新 revision，不覆盖旧行 |
| `event_mapping_replay_ledger` | 是 | 否（可追加 replay） | 记录“当时能否映射、输入/输出指纹是什么”；`failed/no_match` 本身是证据 |
| `event_outcomes` | 是（含 revision） | 条件可重建 | 有行情和冻结信号/映射时可重算；历史 revision、`method_version`、`data_version` 必须保留 |
| `event_similarities` | 建议保留 | 是（结论随版本） | 相似关系是派生结果，可按方法和事件图重算；不能拿它替代事件身份或来源证据 |
| 页面/报告缓存与读模型 | 否 | 是 | 只读投影；过期或截断时显示快照时间，不反向写回 |
| `llm_call_logs` / `news_processing_logs` | 按诊断与隐私策略 | 否 | 调用旁证，证明调用发生过；不证明来源内容，也不是全文存档 |
| `event_cache` / `event_articles` | 迁移期保留 | 否 | 旧事件缓存/成员表只作历史迁移输入，不是当前身份权威 |

重建顺序只能是“下层 → 上层”：来源快照和冻结账本 → 事件/证据 → 映射/结果 → 页面。不能从页面收益倒推来源，也不能从相似事件倒推合并。

## 4. 证据不足时页面怎么显示

| 缺失/不确定 | 页面显示 | 不能做什么 |
|---|---|---|
| 没有来源快照 | “证据不足”/不展示该来源 | 用摘要、模型理由、相似事件补成来源 |
| 来源只有背景/计划 | “背景记录”“计划（未完成）”“观点/背景” | 写成已发生、已确认或支持 |
| 来源明确否认 | “否认记录” | 合并进支持计数 |
| 只有概念映射、原文未点名 | “概念（来源未点名）”；“仅为主题池映射；不构成公司事件关系” | 标成直接影响或公司受益 |
| 旧映射/旧契约 | “历史映射 · 未按当前规则重算”/“历史映射 · 写入契约未知” | 当作当前映射证据 |
| 映射 `no_match`/`failed` | “证据不足”/不展示公司关系 | 猜一个公司或主题 |
| 没有选中正式信号 | “该事件没有选中的正式信号，未进入冻结结果契约” | 显示 0% 或“无影响” |
| 结果未到期 | “尚未到期” | 估算收益或前向填充 |
| 缺个股收盘 | “缺少个股/证券收盘价” | 用别的日期或基准代替 |
| 缺沪深300收盘 | “缺少沪深300收盘价” | 用旧基准或代理指数冒充 |
| 旧结果/旧方法 | “旧记录状态未知” | 重算后覆盖旧 revision |
| 页面截断/过期 | 快照时间、`sources_truncated`、`has_more` | 说“只有这些来源”或“当前一定如此” |
| 日报/周报覆盖不足 | “证据不足：…；不代表没有发生事件” | 把空白解释成没有事件 |

这些状态把“不知道”保留为“不知道”。页面不能用确定性语气伪造证据。

## 5. R01/R18 核对清单

| 核对项 | 读法 |
|---|---|
| 两条为什么不是重复错误 | R01 是 `merged` 源事件，R18 是活跃目标；`event_merge_history` 记录合并，页面只列活跃目标 |
| 来源数是多少 | R18 事件组有两个不同 `source_uri`（一财、同花顺）；R01 的一财观察不增加独立活跃事件数 |
| 公司关系是什么 | R18 两条间接/概念映射（南网储能、阳光电源）；原文标题未点名，页面标“来源未点名” |
| 结果归谁 | R01 有 2 条 `complete` 结果，R18 自身没有；页面按事件组可显示，但行内 ID 仍归 R01，不改写 |
| 哪些必须留 | 来源快照、合并历史、信号账本、映射回放、结果 revision 都必须保留 |
| 哪些可重建 | 页面/报告、相似关系；映射和结果在输入/行情/契约齐全时可重放，但不能覆盖历史 |
| 缺证据时 | 不把 R01 当第二条活跃事件，不把概念映射当直接影响，不把 R01 的结果改写成 R18 的结果 |

## 6. 相关文档和代码入口

- 语义与页面状态：[事件雷达语义与评估口径](event-radar-semantics.md)
- 评分、版本与后验门槛：[模型质量与数据契约](model-quality.md)
- 调用关联与未知用量：[新闻处理与模型调用关联](news-processing-trace.md)
- 首次被拒证据：[首次被拒证据留存](first-rejection-retention.md)
- 事件核心表与约束：[`db/event_core.py`](../db/event_core.py)
- 事件/证据/映射写入：[`services/event_enrichment.py`](../services/event_enrichment.py)、[`db/database.py`](../db/database.py)
- 结果物化：[`services/event_outcomes.py`](../services/event_outcomes.py)
- 只读页面模型：[`services/event_radar_query.py`](../services/event_radar_query.py)
