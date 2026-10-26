# 运行镜像依赖边界

## 构建约束

基础镜像锁定官方 `python:3.11.17-slim-trixie` 的多平台摘要
`sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce`。
这是 Python 3.11 补丁升级，不是 Python 次版本或应用依赖迁移。发布前仍须用最终镜像核验，
不能以基础镜像的标签、requirements 清单或开发环境的审计替代。

生产依赖仍由 `requirements-prod.txt` 决定。构建仅接受 wheel，缺少目标平台 wheel 时直接
失败，不临时增加编译器或执行源码包的构建脚本。安装后先执行 `pip check`，然后在同一层
卸载 `pip`、`setuptools`、`wheel`，删除标准库 `ensurepip`（包括内置 wheel 归档）。

原因是这些工具并非应用运行依赖，且升级独立的 wheel 不会替换 setuptools、pip 私有
`_vendor` 目录中的副本。只删除元数据或命令入口也不够。保留应用需要的 `packaging`，
不笼统删除其他包的 `_vendor`；例如 OpenAI SDK 的内嵌兼容模块不属于本次清理对象。

运行镜像不再提供 `pip install`、`pip list`、`pip check` 或 `ensurepip`。升级依赖必须
重建不可变镜像；检查实际安装版本使用 `importlib.metadata`。宿主发布脚本和测试环境的
pip 不受影响。删除最终文件系统中的工具不等于上游基础镜像的历史层从未含有这些文件，
也不是对所有构建层、标准库或可利用性的完整审计。

## 最终镜像验证

CI 构建后以断网、只读、无生产卷和无生产密钥的容器执行
`scripts/check_runtime_image.py`。它检查：

- 生产版本锁与已安装包的有效运行依赖；
- 8 组本次已核实的 Debian 修复最低版本，使用 `dpkg` 的发行版版本比较规则；
- 打包工具的 distribution、可导入模块、物理目录、入口脚本、`.pth`、残留元数据和
  `ensurepip` 归档；
- Web、Worker、行情入口及主要第三方依赖可导入。

回归测试包含“元数据已删但内嵌目录仍在”等反例，防止只看安装清单漏检。
实际启动 `/app/start.sh` 和读取 `/health` 应在隔离临时文件系统中另外验收；
Worker 的完整运行依赖 Redis 和后台任务，单纯导入不代表已验证其真实流量行为。

系统包核对应使用实际 `dpkg-query` 的 binary/source 版本，并用 Debian 的发布分支修复
版本判定，不能直接按上游版本号下结论。已知有修复版本的项和 Debian 尚未修复、
`no-dsa`、低优先级、功能不适用等项分别记录；不把一次扫描写成“零漏洞”。

本次只在本机验证 Linux amd64 镜像；多平台摘要不代表其他架构也完成构建或运行验收。
没有调用真实模型，不改变评分、映射、阈值、证据留存开关或生产配置。
