# 首次被拒证据的可回放留存（first-rejection retention）

状态：已实现，**开关默认关闭**（`first_rejection_retention.enabled: false`）。本文说明它存什么、
不存什么、怎样回放，以及哪些结论它**不能**支持。标注：【已证实】= 代码或本机离线重算可见；
【估算】= 由已证实数字推出；【未测量】= 没有数据。

## 1. 它解决什么

stage1 先后调用多个后端。首选后端的输出被验证器拒绝后，最终落库的只剩被接受的那次（常是固定
安全中性 4.0）。被拒的原始输出和当时验证器看到的输入不在任何持久记录里，所以“这次拒绝是否
合理”事后无法判断。本功能把**每个 stage 的第一次被拒尝试**连同判断它所需的全部输入写入一张
独立的、追加型的诊断表，保留 7 天。

## 2. 一行证据绑定了什么

表 `first_rejection_evidence`（`db/first_rejection_evidence.py`）。一行 = 一个（文章, 策略版本,
stage）里**首次尝试被拒**的那一次：

| 绑定项 | 列 |
|---|---|
| 该次尝试的确切输入（验证器看到的） | `input_title`、`input_source`（≤20000 字符）、`input_source_chars/bytes`（原始大小）、`input_hash`（完整输入哈希）、`request_hash`（请求消息哈希，消息本身不存） |
| 首次被拒的原始输出与解析结果 | `rejected_raw`、`rejected_parsed`、`rejected_backend/model/attempt` |
| 拒绝原因 | `failure_reason`、`failure_detail`、`fact_failure_kind`、`repair_trigger` |
| 最终处理结果 | `later_attempts`（之后每次尝试：后端、是否接受、解析、原始输出哈希与前 2048 字符）、`final_handling`（落库的 score/logic/direction/score_path/selected/scored_at/strategy_version/event_id、该 stage 的接受方与是否固定中性）、`accepted_is_fixed_neutral` |
| 账本引用（只读） | `event_id`、`signal_key`（= 账本 `signal_key` 的同一哈希，用于连表；表本身不被账本引用） |
| 代码版本 | `run_revision`（`AI_FILTER_BUILD_GIT_SHA`，没有则 `AI_FILTER_VERSION`，再没有则 `unknown`）、`validator_version`、`validator_files`（验证器依赖的每个文件的 SHA-256） |
| 可回放性 | `replayable`、`replay_limit`（`source_over_cap`、`title_over_cap`、`raw_over_cap`、`capture_incomplete`、`not_initial_attempt`、`source_not_in_request`、`validator_unknown`） |

`validator_version` 不是策略名：它是“验证判决路径读取的全部项目源码文件”的哈希（`core/scorer.py`、`core/score_evidence.py`、`scoring_provenance.py`；后者含固定中性常量 `SAFE_NEUTRAL_CONTRACT_VALUES`，参与判决）。依赖集合由 AST 闭包审计得出，守卫测试在判决路径读到未列入的项目模块时失败；遥测落点（`record_score_validation`、`capture_validation_attempt`、`core.metrics`）只记录不决定，不列入。标准库与第三方库不在哈希内。任何一个列入文件变化，`validator_version` 即变化（只改 `scoring_provenance.py` 里的常量也会变）。判决依赖验证器版本——
同一份输入在不同日期的验证器下结论可以不同（见 §7）。

## 3. 上限与“不可完整回放”

- 来源文本：≤20000 字符，且 ≤80000 UTF-8 字节；标题 ≤2000 字符；被拒原始输出 ≤4096 字符/16384 字节。
  配置只能调低，不能调高。
- **超过任一上限**：该行仍写入，但 `replayable=0`，`replay_limit` 写明原因，`input_source` 只存前缀，
  `input_source_chars` 和 `input_hash` 保留原始大小与整体哈希。截断过的行**不会**被当作完整证据，
  回放工具直接跳过并报告 `not_replayable`。
- 单行字节上限 `row_max_bytes`（默认 262144）：超过则整行丢弃并计数（`skipped_row_cap`），不存半行。
  默认上限下一行通常远小于该值（旧样本来源摘要最大 18284 字符）；该上限是兜底，触发即丢弃并计数。
- 总量上限：`max_rows` **硬上限 20000**、`max_total_bytes` **硬上限 64 MiB**（配置只能调低；代码常量 `MAX_ROWS_HARD` / `MAX_TOTAL_BYTES_HARD`，插入语句内部也再取一次 `min`），写入语句内原子判断，超出则跳过并计数。
- 配置值校验：非有限数（inf/nan）、布尔、字符串、小数、负数、0、超出范围的值一律视为无效，**回退到默认值**（对上限类参数默认值就是硬上限），不会抛异常，也不会被“夹”到更大的值。保留天数有效范围 7–30，其余见代码。启用开关后的初始化（含配置解析）自身被异常隔离：任何异常都只意味着“本条不留存”，评分照常完成。
- 估算【估算】：约 418 次被拒/天 × 7 天 ≈ 2900 行，按旧样本来源摘要平均数 KB 计，约数 MB 到十余 MB；
  64 MiB 总量上限约为此的数倍。**未实测**，开启后看表大小再定。

## 4. 开关、写入与对评分线程的影响

- 开关：`first_rejection_retention.enabled`，**默认关**。只有字面 `true` 才开；字符串 `"true"`、缺省、
  非字典都视为关。关闭时**跳过**：为本功能建立捕获上下文、给条目挂待写标记、构造行、起写线程、
  连数据库，所以**零写入、无写线程**。但**不是零开销**：每次评分仍会经过捕获包装函数，读一次
  `first_rejection_retention` 配置并判断 `enabled is True`，落库后 `persist_first_rejection` 还会做一次
  待写标记检查再返回（旧 `score_evidence_capture` 同时关闭时，包装函数随即直接调用原评分函数）。
  这几步很轻，但**没有测量**，不能写成“零开销”。另外 `init_db` 建表建触发器、到期清理照常执行（见 §5）。
- 与旧的 `score_evidence_capture`（目录）的开关、门槛和写入通道相互独立（共用同一个记录评分尝试的钩子，
  但本功能不看旧目录的状态）：旧目录写满（`.capture.complete`）或没配置，
  都不影响本功能；本功能不创建、不写、不清理旧目录。**本功能不启用 #192 的轮换目录。**
- 写入是**异步**的，写 SQLite 这一步不在评分线程上：评分落库**之后**才构造行，然后放入有界内存队列
  （`queue_max`，默认 256，`put_nowait`）。守护线程用 `write_timeout_ms`（默认 200 ms）的 busy 超时写
  SQLite。队列满、库被锁、写入变慢、写入抛错：都是**计数后丢弃**（`first_rejection_retention.STATS`），
  不会让评分等待或抛错，已落库的评分结果不变；日志不含原文。
- **异步不等于零延迟。** 开关打开后，评分线程上仍同步做这些事：评分过程中记录每次尝试（原始输出、
  请求哈希等）、落库后构造行（截取并计算来源文本字节数、多处 JSON 序列化、SHA-256、行大小统计）、
  `put_nowait` 入队；进程里第一次入队还会创建写线程，第一次构造行会读取并哈希验证器依赖文件。
  写线程与评分线程共用同一 Python 进程和同一个 `cache.db`，对评分耗时的影响**没有测量**。
  现有测试只证明粗粒度的界：队列满时连续 6 次评分合计不超过 5 秒、被锁库的一次写入受超时限制
  （< 1.5 秒），并且评分结果不变；它们不是延迟基准，不能据此说“没有延迟”。
- 默认配置下（`queue_max` 256、`row_max_bytes` 262,144 = 256 KiB），队列中等待写入的行的计量载荷
  上限约 64 MiB（256 × 256 KiB，由代码常量推算）；这不是进程内存上界——Python 对象开销、正在处理的行
  和构造行时的临时开销另算，峰值没有测量。
- 进程退出时队列中尚未写入的行会丢失——这是诊断数据，接受该代价。

## 5. 追加型与到期删除

迁移（`init_first_rejection_storage`）实际创建：1 张表、1 个显式索引 `idx_first_rejection_evidence_created`
（外加 `dedupe_key UNIQUE` 的自动索引），以及 **3 个触发器**——不是 4 个：
`trg_first_rejection_evidence_no_conflict_write`（BEFORE INSERT）、`trg_first_rejection_evidence_no_update`
（BEFORE UPDATE）、`trg_first_rejection_evidence_expiry_only`（BEFORE DELETE）。这是对代码和一个空库里
实际建出的对象核对的结果。（PR #197 的描述曾写“4 个触发器”，不准确，以此处为准。）

- `BEFORE INSERT` 触发器（冲突写入保护）：新行的 `dedupe_key` 或 `id` 已存在时，在 SQLite 解决冲突**之前** `RAISE(IGNORE)` 丢弃这一行。结果：`INSERT OR REPLACE` / `REPLACE INTO` / `ON CONFLICT DO UPDATE` / `OR FAIL` 等都不会改动已有行——与行龄、连接上的 `recursive_triggers` 开/关均无关（测试覆盖两种设置 × 行龄 0/3/8/400 天 × 7 种写法 × 两种冲突键）。幂等的 `INSERT OR IGNORE` 仍然是“无声不写”。
- `BEFORE UPDATE` 触发器：任何 UPDATE 都 ABORT。
- `BEFORE DELETE` 触发器：只允许删除 `created_at` 早于 7 天的行；7 天下限是常量。它只管显式 DELETE；REPLACE 不靠它（`recursive_triggers` 关闭时 SQLite 不会为 REPLACE 的内部删除触发 DELETE 触发器，开启时又会放行 7 天以上的旧行，这正是需要上面 INSERT 触发器的原因）。
- 到期清理在 `cleanup_old_entries` 里执行，保留天数取配置但夹紧在 7–30 天；清理失败只告警，
  不会中断其余清理。关闭开关后旧行仍按期清理。
- `init_db` 会建空表、1 个索引和 3 个触发器（加性迁移），即使开关关闭。
- 保护的边界（残余限制）：这些是数据库层触发器，防的是应用代码和普通连接的误写；持有数据库文件写权限的人仍可 `DROP TRIGGER`/`DROP TABLE` 或用 `PRAGMA writable_schema` 绕过。被丢弃的冲突写入不报错，只能通过“未写入”察觉。表存在于旧版本（仅有 UPDATE/DELETE 触发器）的库会在下次 `init_db` 补上 INSERT 触发器。

## 6. 幂等与账本

- `dedupe_key = sha256(article_id, strategy_version, stage, request_hash, 原始输出哈希)`，`UNIQUE`，
  `INSERT OR IGNORE`：webhook 重试或部分批次恢复得到相同输出只存一行；不同输出是另一次尝试，存新行。
  同一链接并发由 #175 的单飞保证只评分一次，因而只产生一行。
- 账本身份不变：新表不被 `news_cache`、`backtest_signals`、`event_evidence` 引用；`signal_key`、
  `revision_state_key` 的计算不变；测试对开/关两次独立库逐列比较。

## 7. 回放工具

`python scripts/replay_first_rejection.py --db <库副本> [--code-root DIR | --git-repo DIR] [--id N] [--json]`

只读（`mode=ro`），无模型调用、无网络，在干净子进程里运行所选代码树的验证器（`record_score_validation`
被置空，不写遥测）。`--git-repo` 会按行内 `run_revision`（必须是提交 SHA）用 `git archive` 取出当时
代码；`run_revision` 只是发布标签时无法取出，会回退到当前仓库并在报告里注明。

退出码（同时出现多种时取最高优先级，汇总行总是列出各状态计数；`--json` 输出含 `exit_code`、`summary`、`rows`）：

| 退出码 | 含义 | 优先级 |
|---|---|---|
| 2 | 执行失败：数据库打不开，或至少一行无法执行重放（`status=error`，如代码目录不可用）；结果不完整 | 最高 |
| 1 | 至少一行 `MISMATCH_SAME_VALIDATOR` | 次之 |
| 3 | 至少一行 `not_replayable`（且无 error、无 mismatch） | 再次 |
| 0 | 没有 error、没有同验证器不一致、没有不可回放行；**包含**“另一验证器下的漂移”和“库里没有任何行” | — |

**退出码 0 不证明所有历史判决都被复现。** 0 同时覆盖 `match`、`match_other_validator`，也覆盖
`differs_other_validator`（所选代码树的验证器与记录时不同，判决漂移——这是版本差异，不是复现成功），
还覆盖表为空的情形。要判断“是否全部复现”，必须逐行看 `status`（或 `--json` 的 `summary`/`rows`，
并核对 `validator_same`）：只有每行都是 `match` 才是同一验证器下的完整复现。

每行报告 `validator_same`（所选代码树的文件哈希是否与记录完全相同）并分类：

| 状态 | 含义 |
|---|---|
| `match` | 同一验证器，判决、解析、后续尝试复判均一致 |
| `MISMATCH_SAME_VALIDATOR` | 同一验证器却不一致：记录或非确定性问题，退出码 1 |
| `match_other_validator` / `differs_other_validator` | 验证器不同；后者是**版本漂移**，不是“误拒”结论 |
| `not_replayable` | 行被标为不可完整回放，不重放 |

“同一验证器”意味着判决可复现，**不**意味着该拒绝“合理”；合理性仍需人工或另行标注。

## 8. 隐私

存：文章标题与来源文本（这是可回放的代价）、被拒原始输出与解析。不存：文章链接（只存 `article_id`
哈希和账本 `signal_key`）、请求消息与系统提示词（只存哈希）、任何密钥或配置。表在 `cache.db`，
没有公开路由、SSE 或日志引用它。因为这里**新增留存第三方新闻文本**，开启前应确认该留存范围可接受。

## 9. 回放边界与不能下的结论

公开仓库不包含私有新闻采集批次、真实模型原始输出或人工业务标注；
历史私有样本的统计结果也不作为此公开版本的验收证明。

- `initial` 与 `repair` 必须分开计数。修复尝试的判决同样依赖原始输出、标题、来源、
  `attempt` 与 `repair_trigger`，不能因漏跑某类尝试就断言它不可回放。
- 必须记录采集时的验证器版本；换验证器后的判决变化不是同版本复现失败，也不能直接
  判定哪个验证器正确。回放可复现不等于拒绝合理。
- 某批样本截断后没有改判，不保证未来输入安全；超过来源上限的记录仍必须标为不可完整
  回放，不能截断后声称证据完整。
- 定时采集各源单轮条数之和不覆盖 webhook、冷请求、多轮采集、后备和 repair，
  不能作为全天模型调用量或费用上限。
- “首次被拒 → 修复后固定中性”的路径比例不是误拒率；准确率需要独立人工标注。
- 旧 capture 目录满额后不再记录；本功能有自己的开关和上下文，不依赖旧批次是否满额。

## 10. 发布须知

- 公开配置中的 capture 目录是示例路径，没有附带私有批次。部署前仍须审查完整配置与
  来源文本留存授权，不应因示例配置存在就自动启用或轮换真实采集批次。
- 本功能不修改评分指纹文件，策略代码指纹与完整策略版本不变；开关打开也不改变指纹。因此不会触发
  重评分。
- 在镜像不导出 git SHA 之前，`run_revision` 为发布标签（`AI_FILTER_VERSION`）；精确代码仍由
  `validator_files` 的文件哈希固定。
- 打开开关是单独的生产配置变更，不在本改动内。
