# 独立隔离宿主入口验证

本工具只验证一件事：在独立 Linux network/mount namespace 中，实际 Docker
端口发布能否被该 namespace 内的宿主 loopback HTTP 客户端访问。它不是部署
入口，不证明 runner 全局 loopback 或完整生产切换已经通过。

## 为什么需要单独验证

既有完整 activation 使用唯一的 `internal` Docker bridge。已留存的固定版本
失败证据中，容器启动成功，但实际 `NetworkSettings.Ports` 为空。不能以请求的
`PortBindings`、容器 IP、`docker exec` 或模拟 HTTP 替代真实宿主入口。

新证明使用独立 namespace 中的私有 Docker daemon 和普通 bridge。普通 bridge
没有连接 runner 的外部接口；不得把这套拓扑直接替换为生产配置，也不修改原完整
activation 的网络、身份校验或成功标准。

## 隔离与身份

- 只允许 Linux root 和精确 checkout 的 root 私有副本；原 checkout 不改属主。
- namespace 只有 loopback 和本次自有 bridge/veth，核对 IPv4/IPv6 全表无默认
  路由、外部接口或 gateway；DNS 指向 namespace 本地的 `127.0.0.1`。
- mount propagation 设为 private，并在 namespace 内遮蔽 `/run`。私有 daemon
  不使用 runner 的 Docker/containerd socket；数据、exec、PID、配置目录均独立。
- 固定 nginx 完整 digest 只在外层预拉取一次。外层 RepoDigest、Linux/amd64、
  image ID、完整 Config 和 RootFS DiffIDs 经离线 tar SHA256 绑定到内层加载结果。
  `docker save/load` 不保证保留 RepoDigests；这是独立的内容证明，不伪造内层
  RepoDigests，也不替代生产适配器现有的 RepoDigest 门槛。
- 核对私有 socket 的 peer PID/UID/GID、进程启动身份和 cgroup。容器实际端口映射、
  容器 PID、启动身份在真实 HTTP 请求前后分别验证；只允许本次精确 loopback 端口。

## 资源和时限

所有本工具新建的 helper、私有 dockerd/containerd/shim/runtime 和容器进程归入
同一父 cgroup：768 MiB、swap 0、256 PID、1 CPU。nginx 子限额为 128 MiB、
swap 0、64 PID、1 CPU。现有 runner Docker daemon 不属于这组新进程；外层仅
执行固定镜像的只读检查和离线导出，不宣称该既有 daemon 的开销也受此父组约束。

缺少已委派的 cgroup controller 时停止，不修改宿主根 controller 配置。实际进程
清单检查子树逃逸；nginx worker 只按已绑定容器的 cgroup 和 namespace 精确接纳。

helper 必须接收不超过 180 秒的绝对 monotonic 截止时间。准备、导出、加载、
hash/fsync、HTTP、停止及证据整理共享这一截止时间；普通命令不超过 3 秒、HTTP
不超过 10 秒、正常停止不超过 15 秒。外层归档客户端 200 秒不是额外业务额度。
独立 CI job 最多 5 分钟，固定镜像预拉取另限 60 秒。

原 `native-amd64` job 的 15 分钟限制和完整 activation 代码保持不变。原脚本的
600 秒计时并非所有直接构建/探针的统一硬限，不能宣称两套验证已共享一个经过验证
的完整 600 秒硬截止。

## 失败和保全

成功必须确认本次容器已停止、daemon 正常退出、日志 EOF 及受限资源状态。
不删除本次容器、镜像、namespace 证据或 daemon 存储，不调用强制 KILL/prune。

创建/启动/停止结果未知、外来对象、OOM、日志超限、原件写入失败或超时均停止。
未知容器状态下不终止 daemon。原 inner helper 脱离外层收集客户端的进程组；任何
活 daemon 的原日志读 FD 都由它被动保留，额外 retainer 不能作为退出许可。
日志线程无法创建、retainer 退出、错误报告或 stdout 失败同样不能绕过此保护。

这种失败可能留下受限、未清理的被动进程；它不是成功清理或无限业务重试。超时
只说明验证停止，不能据此声称容器已经停止。CI VM 最终回收也不是工具完成清理
的证据。

## 证据与验收口径

原件独占创建、私有权限并同步到盘。安全输出包含选定实际端口、HTTP、namespace、
进程和资源 profile，以及对应原件字节哈希，不输出 Env、模型消息或私有文本。
安全包最多 512 KiB，读取原件总量最多 16 MiB；不遍历 daemon 存储或跟随链接。
`safe_evidence.complete` 只表示这些指定材料完整，不表示全量原始数据已归档；
`full_raw_archive_verified` 始终为 false。既有失败、STOP 和下载归档缺口全部保留。

CI 的 `isolated-host-ingress` 与原 `native-amd64` 是两个独立 job。前者成功不能
覆盖后者失败，也不能授权合并或部署。离线回归只检验边界逻辑；Linux namespace、
Docker、资源峰值和真实 loopback 行为必须以本次固定 SHA 的托管 CI 原件为准。
生产 socket 授权、实际旧镜像兼容性、完整切换与正式发布门禁仍须各自通过。
