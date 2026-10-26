# HTTP 准入屏障的边界

本模块是应用内共享屏障的 HTTP 集成，不是生产停流完成证明。默认 `AI_FILTER_ADMISSION_MODE=disabled`，请求不创建活动文件，不改变原有响应。启用、实际容器与共同挂载绑定、旧发送端保全及发布须另行审定；本 PR 不部署。

## 顺序与入口

`app.py` 保留原 Miniflux 验签作为第一个请求钩子，再安装屏障。`/webhook` 和 `/webhook/receipt` 无效签名仍直接 401，不读取屏障或占用名额。其余认证仍由原业务函数执行，屏障不实现另一份 API key、浏览器会话或 CSRF 校验。

- 仅 GET/HEAD、**同时匹配已审核 endpoint 和精确 URL rule** 的四个接口免准入：`/health`、`/dashboard/events`、`/dashboard/events/daily`、`/dashboard/events/weekly`。健康不连接数据库；后三个真实查询使用既有数据库的 `mode=ro` 连接。不是 `/dashboard/` 前缀放行，也不是所有 GET 放行。原接口的认证、限流和查询预算仍生效；只读不等于零负载。
- `llm` 或 `push` 参数一旦出现，连同重复参数、`false`、空值都保守归为 intake；不复制业务参数解析器。`/filter`、`/filter/multi` 即使碰巧命中缓存也按 intake，避免冷路径旁路。
- POST `/webhook/receipt` 是 drain：查询会恢复回执并严格同步文件，不称为纯读。draining 可以核对已有消息，freezing/frozen 不允许新查询写入。
- 反馈、refresh、配置刷新、统计重置、浏览器登录/退出、未知 GET/POST/OPTIONS 和未知 URL rule 默认 intake。`GET /stats` 与 `GET /dashboard` 会执行统计 flush 落库，也不能豁免。登录会话写 Redis，不被伪称为只读豁免。健康可读不等于可以重新登录或业务入口开放。
- 其他看似只读的接口也保守按 intake：状态/指标/日志、首页/SSE/便宜卡、Briefing/feed、证券历史、集成信号等使用写能力连接（可创建数据库、设置 WAL）或存在该回退路径。score-quality/neutral 主查询虽只读，但配置开启 few-shot 时策略解析也可能调用写能力连接。**enforced 的短时 draining/frozen 下这些页面会 503**，不宣称全部页面仍可用。默认 disabled 行为不变；本 PR 不修改数据库连接层或评分契约来扩大白名单。

## 同步生命周期与失败

准入在 handler 及原业务锁之前取得活动共享 lease；同线程的内层守卫复用，不重复计数。lease 贯穿同步 handler。正常响应发送前严格完成 lease；若最终持久同步失败，响应降为 503，不发送原来的成功 202。原 lease/失败证据不删，不补造成功完成。

5xx、未处理异常以及未定义的 producer 流式响应留下 `uncertain` 完成，后续冻结不得把它当成功排空。producer 流式生成器不会被放到 lease 结束之后运行：启用时拒绝该模式并保留不确定证据。Glance endpoint 不在上述四项豁免内。

`teardown_request` 仅作异常兜底，不能证明异步任务结束。HTTP 返回之后的执行器、队列 claim、progress、finish/archive、派生任务及可选异步写线程必须各自持有独立责任；本模块没有把 HTTP 202 等同于后台处理完成。重复 teardown 不重复完成或删除证据。

`initialize_app()` 和兼容入口 `init_db()` 同样受 intake 守卫。enforced 且 frozen 或缺少有效绑定时不能自动初始化、迁移或偷偷 open；实际候选启动需要后续受控迁移方案，不以启动失败为由绕过。

## 名额口径

计数是**被准入的 HTTP 请求**，不是成功新闻数、唯一新闻数或模型次数。重复请求、业务 4xx、通过屏障后被原业务认证拒绝的请求也可占名额；未通过原 webhook 验签的请求不占名额。不会因请求失败退款、延长试运行或扩大模型预算。

HTTP 的 `declared_request_bytes` 是声明的 `Content-Length` 准入额度，交给共享 lease 的 `charge_bytes` 字段累计；不是实际或持久字节用量，不是 receiver 存储预留，也不是 Miniflux outbox 容量证明。启用时有 Transfer-Encoding、未知长度的已终结输入流或非法长度均 fail-closed 503，不把未知用量记 0；无正文 framing 的普通空请求记 0。该检查不消费请求体，不扩大原有 `MAX_CONTENT_LENGTH`。原验签在此前，签名请求体仍按原实现读入并缓存。

接收端自身仍须校验真实原始字节、签名、持久回执和容量。`charge_bytes`/`charged_bytes` 不能在报告或现场回执里被改称实际持久字节；不同入口的内部/启动活动可能无 HTTP 正文，名额和 bytes 也不等于新闻数或磁盘占用。

## 隔离验证

`tests/test_admission_http.py` 使用真实 Flask 钩子、原验签和临时私有磁盘屏障；业务替身只用于防止模型、外部网络和数据库业务写入。覆盖精确只读白名单、未知入口拒绝、冷热 filter 入口、feedback、LLM/推送、receipt drain、内层复用、认证不被豁免、启动守卫、同步失败降 503、异常原件以及 disabled 兼容性。它不是生产启用或硬件断电测试。
