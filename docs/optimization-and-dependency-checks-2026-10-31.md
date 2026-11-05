# 调查执行优化与真实依赖检查

本轮修复标准 JSON 诊断、搜索重复读取、诊断重复计费和拥塞失败，并增加真实 MySQL、S3 兼容服务及 Temporal 的集成检查。模型响应使用确定性替身，不调用模型供应商或游戏平台，不进行真实任务能力评测。

## 输入兼容与诊断复用

诊断配方兼容 `application/json`、带 charset 的 JSON 和 UTF-8 文本：配方服务已经解码的对象不再调用 `json.loads`。崩溃和帧耗时均通过完整服务链验证，而非只验证直接调用配方函数。

诊断身份包含调查 ID、完整配方和组件版本、参数、输入 Artifact 身份、SHA-256 与来源 revision。相同身份复用已保存的派生证据，不重复计算、不重复扣取读取预算，也不作为新进展。参数不同但结果相同的两次分析仍有不同执行身份。

派生证据行携带确定性 `analysis_job_id`，作为完成记录。即使进程在证据摄取之后、诊断事件或会话检查点之前中断，恢复后也能取回结果。读取缓存仍复核 Artifact 哈希和调查归属，并补齐中断时未写出的来源旁注。尚未落库的执行不视为完成；其预算保留采用原有保守语义。

## 搜索读取与异步边界

单次搜索对每份选中证据最多完整读取一次。保存本次有界扫描缓冲区，直接从已校验内容构造命中摘录，不再为每条命中重新下载、读取和校验整份 Artifact。

20 条命中场景的回归断言为一次完整读取，同时逐项复核 UTF-8 边界、原文字节、源哈希、摘录哈希、偏移、总长度及审计记录。命中靠近扫描末尾时，摘录受当前缓冲区限制，`next_byte_offset` 仍指向原文件，后续可以继续读取。

文件读取、哈希、注册配方计算及其输出存储移到后台线程；正则匹配继续使用可终止的独立进程。数据库选取、作用域检查、证据摄取和审计写入保留在原所有者线程。后台计算完成后重新检查输入是否仍为当前证据。

扫描内容仍限制为每文件 2 MiB、每次 4 MiB；这不是对象存储下载字节数的限制，完整来源哈希验证仍可能读取较大对象。后台线程中的读取/计算不能被强制终止，但取消后不继续摄取输出；S3 超时与注册配方输入限制仍然适用。跨请求的分块校验/缓存属于后续工作。

## 排队和容量

Local 只为可调度的调查创建执行 Task，其他已接受调查留在待处理队列和持久调查/启动账本中。实际取得调查槽位后才确认启动；暂时容量不足会重新排队，不发布系统失败报告。关闭进程后，下一所有者通过现有恢复入口重新加载未完成调查。排队任务可直接取消，成功取消也会处理对应启动 Outbox。

Temporal 原生活动在容量不足时返回 `capacity_deferred`，Workflow 等待后重试，不消耗模型轮次和调查轮次边界。每 100 次延期通过 Continue-As-New 限制历史增长；取消仍能打断等待。墙钟预算从原调查创建时间计算，不随排队、恢复或 Continue-As-New 重置。

新增 `native-capacity-wait-v1` patch 标记保留旧 Workflow 历史路径。新路径优先使用 SDK 的确定性 `workflow.wait`；旧 SDK 保持原兼容入口。并发池仍为进程内限制，未实现跨副本总配额或项目公平调度。

## MySQL 联调修复

真实服务检查发现并修复此前替身测试未覆盖的问题：

- `cursor` 是 MySQL 保留字，迁移执行和运行时 SQL 的标识符需要引用；字符串、注释和已经引用的名称保持原文。
- 迁移 002 删除旧唯一索引前，需要保留支撑调查外键的索引；保持外键检查开启。
- 完整来源复合唯一索引超过 InnoDB 的 3072 字节限制。执行兼容层改为完整类型化来源元组的 SHA-256 生成列唯一索引，保留完整字段和查询索引，不截断来源 ID。真实测试覆盖共享长前缀的 Unicode 来源身份。
- 驱动返回的 UTC datetime 转成带时区 ISO 字符串，与 Repository 的 SQLite 协议一致；运行时和迁移连接显式使用 UTC session，避免本机时区改变截止时间。
- 运行时普通查询使用 autocommit，所有写事务仍显式 begin/commit/rollback。普通读取不再长期保留 SERIALIZABLE 读锁和旧快照，另一连接能及时更新并读取最新状态。

历史 SQL 文件及原校验和未修改，迁移兼容转换只在执行阶段进行，已应用版本仍按原校验和校验和跳过。请使用 `game-test-agent-mysql-migrate`，不要直接逐文件执行历史 SQL。MySQL DDL 仍可能部分提交，本轮不提供任意中断迁移的自动修复或回滚。

## 运行真实依赖检查

先安装锁定的全部可选依赖，并启动 Docker Desktop/Engine：

```powershell
uv sync --locked --all-extras --no-editable
uv run --frozen --all-extras python scripts/run_dependency_checks.py
```

入口创建唯一项目名的 MySQL 和 S3 容器、随机本地端口、随机测试密码及加密密钥，再启动独立 Temporal 开发服务。测试完成或失败后均停止本次 Temporal 进程、移除本次 Compose 容器和测试卷，保留脱敏检查结果。不会使用现有数据库或修改 Docker 的全局镜像配置。

Temporal CLI 固定为 1.9.1，按官方发布校验和验证下载包，只提取预期可执行文件；下载中断不会把未完成包当成可执行缓存。Windows/Linux amd64 可自动安装，下载结果缓存于 `data/local/dependency-tools`。需要 HTTP 代理时显式传 `--download-proxy http://127.0.0.1:端口`。

MySQL 默认 8.4.10。S3 服务默认从固定的 MinIO 官方源码发布版本构建，构建需要下载 Go 工具链和模块。也可以提供已经存在的兼容镜像：

```powershell
uv run --frozen --all-extras python scripts/run_dependency_checks.py --mysql-image "MySQL镜像或摘要" --s3-image "支持AES256的MinIO镜像或摘要"
```

本轮本机使用已有 MySQL/MinIO 镜像摘要，真实服务版本为 MySQL 8.4.10、MinIO RELEASE.2025-09-07T16-13-09Z、Temporal CLI 1.9.1 / Server 1.32.0。默认 MinIO 源码构建和远端 GitHub Actions 尚未在本机执行；CI 已增加独立 Linux 依赖检查作业。普通离线 pytest 会明确跳过这 3 项，避免没有依赖时误称已通过集成检查。

检查内容包括迁移首次执行和再次执行、事务回滚、持久计数、双连接状态可见性、长来源身份、S3 加密读写和内容损坏、真实 Workflow 的重复启动拒绝、活动完成确认前中断后更换 Worker、诊断预算不重复消费、已采集证据报告、重复取消及两条真实历史的确定性回放。测试中断通过停止 Worker 并取消未确认活动模拟，不等同于生产机器断电验收。

运行产物在 `data/local/gta-deps-<随机ID>/`，包括 `result.json`、`results.xml`、`pytest.log`、`services.log` 和 `temporal.log`。只上传检查日志和报告，不上传数据库文件或下载的可执行程序。

本轮源码修改前备份：`data/local/optimization-20261031-before.zip`。

## 本机验证结果

- Windows / Python 3.12.3 锁定环境：**412 passed、28 skipped**；28 项包含 25 项环境相关沙箱检查和 3 项需要显式启动依赖的集成检查。
- 单独启动真实依赖后：**3 passed**，产物为 `data/local/gta-deps-9ed34adda365/`；测试容器、卷及 Temporal 测试进程已清理。
- Ruff、版本化契约检查和 `uv lock --check --offline` 通过。源码包/Wheel 构建及隔离导入、包内资源检查通过，构建位于 `data/local/optimization-20261031-build/`。
- 真实任务评测、生产集群验收和远端 CI 均未开展。本次没有修改产品 UI，也未重复开展浏览器验收。

参考：[Temporal CLI 官方发布](https://github.com/temporalio/cli/releases/tag/v1.9.1)、[官方校验和](https://github.com/temporalio/cli/releases/download/v1.9.1/checksums.txt)、[MinIO 固定源码版本](https://github.com/minio/minio/tree/RELEASE.2025-10-15T17-29-55Z)。
