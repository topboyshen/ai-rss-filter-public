# 固定 RSSHub 候选的构建期兼容层

基础镜像严格固定为 `diygod/rsshub@sha256:e7cd1bd8838b91f89c2ed56b0889f3754edc6aa1f1f5dac002e2e97e1079bd54`，上游 revision 为 `f23ada8a636fbbb5f92d8e91558cf4e8ffaca66a`。最终部署必须锁定**派生镜像本身**的 registry manifest digest，不能把未修改的基础镜像 digest 当作兼容修复产物。

## 兼容范围

- `/wallstreetcn/news` 和 `/wallstreetcn/news/global` 使用 `global-channel`，恢复文章与快讯混合流；保留候选的详情处理、日期校验、广告过滤及失效详情过滤。
- 其他已列明合法分类保留候选原来的 channel。`china` 并非合法分类，已明确退役；它和其他未知分类在任何上游请求前抛出候选实际 `NotFoundError`，由 RSSHub 返回 404。不能使用 `InvalidParameterError`：该候选的错误处理中它会落到 503。
- 仅 `/cls/telegraph` 的 category 参数缺失时，恢复旧版直接使用 `shareurl` 的行为：上游提供真实链接则保留；没有链接时不拼接 detail 地址、不设置显式 guid，原 RSS 序列化器使用规范化后的 title 生成 GUID。`watch` 等分类仍保留候选的真实链接或 detail 回退，`/cls/depth` 完全不修改。标题、内容和发布时间原样交给候选处理逻辑。

这不是泛化补丁，也不在运行时装载外部目录。两个编译模块与错误类的完整 SHA256 必须匹配，替换片段必须恰好出现一次；任何漂移或二次执行都会失败。先验证所有输入再写模块。构建没有下载依赖，不改变基础镜像的 ENTRYPOINT、CMD、USER。

AI-Filter 同时在读取入口明确退役 `wallstreetcn/news/china`：`/filter`、`/filter/multi`、`cached_feed` 和 `/feed/json|rss|html` 不再从已有缓存返回该路由的旧文，返回 410 提示。这是应用侧的缓存入口保护，与 RSSHub 的 404 拒绝互补；不删除历史数据，不改变 feed 配置或评分阈值。

## 可复核构建

在仓库根目录执行，`deploy/rsshub-compat` 是唯一 build context：

```sh
compat_context_sha=$(node deploy/rsshub-compat/patch.mjs --context-digest deploy/rsshub-compat)
docker build --platform linux/amd64 \
  --build-arg COMPAT_CONTEXT_SHA256="$compat_context_sha" \
  --tag ai-filter-rsshub-compat:validation deploy/rsshub-compat
```

context SHA 按排序后的相对文件名、文件 SHA256 计算，符号链接拒绝；构建内重新计算并核对。上下文内容标识不依赖当前 Git HEAD，后续只改 Compose 的派生镜像 digest 不会改变此构建身份。镜像中 `/app/ai-filter-rsshub-compat.json` 记录基础镜像、基础 revision、context SHA、模块修改前后 SHA 和依赖 SHA。镜像标签也记录 context SHA。

离线回归仅运行真实编译模块的变换及隔离依赖，不能替代 HTTP/序列化验收。发布资格还需原生 amd64、768 MiB/256 PID、默认启动入口下的冻结 HTTP 正反例和完整实际路由对照。成功的重复请求不是缓存命中证明；实时 A/B 内容变化也不能直接算解析回归。

## 回退边界

此目录不执行部署。升级后应保留旧 RSSHub 完整固定 digest 和对应发布清单；回退必须恢复 RSSHub 镜像配置，而非仅回退 AI-Filter 应用镜像。已有 Miniflux/RSS 缓存和已评分数据不随镜像回退撤销。是否重新产生模型调用取决于输入身份和下游处理，不能因评分代码指纹不变就断言零调用变化。
