# 第十七批：Worker 跨进程独占与运行后端绑定

日期：2026-11-03。延续[第十六批语义再分派](harness-phase16-implementation-2026-11-03.md)，补齐支持入口上的调查控制执行互斥。继续使用现有 Python WorkerStep、Local 队列、Temporal Activity、领域幂等与资源清理账本。

## 参考 Superagent 的哪些机制

固定版本 deepseek-ai/dsh-superagent-prototype@6337fc823ce20251c8de8085948165c81aaa701e：

| 上游实现 | 借鉴的行为 | 本项目落点 |
| --- | --- | --- |
| [Assignments.started/turnEnded](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/core/assignments.ts) | 任务归属与执行轮次分开，当前执行者才能推进状态 | investigation.backend_selected 固定运行后端，WorkerOwnership 保护每次控制执行 |
| [Assignments.interrupted](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/core/assignments.ts) | 重启后检查当前状态，不重复已经发生的外部操作 | 持有者退出后接手，继续读取原检查点、在途操作和累计预算 |
| [Effects.Later/attempt/drain](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/app/effects.ts) | 暂时无法执行时延后，不误记业务失败；关闭时停止领取并收尾 | Local 占用冲突回队列；native Temporal 返回延后；typed Activity 使用可重试错误 |

这些是行为借鉴，没有复制上游 TypeScript 实现，也没有另建 Ticket、Assignment 或 Session 数据模型。本项目使用平台相应的独占原语，不将路由评估的超时占用直接套到可能仍在运行的调查执行上。

## 修改前的缺口

Local 的任务字典、并发槽与 NativeStep 锁都只在进程内生效。两个服务同时恢复同一数据库，可能分别进入同一调查。原有 execution_mode 只能区分 typed/native，不能阻止 Local 与 Temporal 同时驱动相同调查。

本批增加两个独立约束：

1. **后端绑定**：第一次取得执行占用后，通过已有调查事件账本原子记录 `local` 或 `temporal`。后续读取最早绑定，不因空闲或重启自动切换后端。每项调查只增加一次绑定事件，不改请求权限。
2. **控制执行互斥**：SQLite 使用规范数据库路径旁的 OS 文件锁，MySQL 使用数据库实例上的会话命名锁。活跃持有者仍在时不按计时强制抢占，避免旧执行者恢复后与新执行者同时推进。

绑定不同后端时拒绝进入控制逻辑，保留调查与已有证据，不把配置冲突变成一份失败调查报告。切换运行后端不能只改环境变量后并跑原调查；新调查可以选择新的后端。没有新增运行中迁移接口。

## Local 与取消路径

Local 的受控运行、直接 `run`、恢复、取消收尾均取得同一调查占用。完整 typed 执行持有到本次调用结束，native 单步在让出或等待后释放占用，再由既有队列安排下一步。

占用冲突时不进入模型或工具，也不调用失败收尾，将调查留在队列延后重试。接手后再次检查状态，已经结束的调查不会因旧排队快照重新调用控制器。

取消请求仍可及时写入 `canceling`，作为用户控制信号；外部取消与最终取消报告则需要执行占用。另一个执行者尚未退出时保留取消意图，取得占用后继续清理。没有 Controller 且可立即独占的同步取消保持原有即时报告行为。取消清理不占用普通调查容量槽。

当前任务自身的嵌套调用可复用占用，另一个协程即使继承了 ContextVar，也不能冒充同一个控制执行者再次领取。运行结束后旧子任务保留失效的写入凭据，不能提交迟到的控制状态。

## Temporal 接入

所有 CoreActivities 入口均使用相同占用约束；`query_action` 从已有 Action 解析调查身份，不把 action_id 当作调查编号。装饰器保留 Activity 名称、类型信息和参数调用方式，原工作流命令与版本标记不变。

native 单步占用冲突返回 `ownership_deferred` 与既有 `capacity_deferred`，继续由工作流计时器延后。typed Activity 占用冲突产生可重试错误，遵循既有有限 Activity 重试策略；后端不匹配为不可重试配置错误。

活动之间释放占用，允许既有清理工作流在独占条件下继续收尾；后端仍固定为 Temporal。调查工作流身份继续依赖既有稳定 workflow_id 和 Temporal 的单工作流约束。本批没有变成支持任意多个不同 workflow_id 驱动同一调查的调度器。

## MySQL 会话失效与写入检查

命名锁包含当前数据库与调查身份，避免同一 MySQL 实例上不同数据库互相占用。额外的进程内登记防止同一个 DB 连接被其他协程利用 GET_LOCK 的会话重入语义重复领取。

持有期间监测 `IS_USED_LOCK` 与 `CONNECTION_ID`。丢锁、无法核验或连接身份改变时取消控制执行；Repository 事务入口再次核验当前占用，拒绝旧上下文及其派生的模型/工具子任务写入。释放时不向已经更换身份的连接发送解锁，不能误释放新会话的占用。

没有增加数据库自动重连。即使调用方替换或重连了底层会话，连接身份校验也会拒绝旧执行上下文。正常用户输入、取消信号和协调层消息不需要成为调查推理所有者，继续通过其原接口写入；执行独占不替代这些接口的鉴权与版本检查。

## SQLite 文件锁与恢复

锁文件位于 `<database filename>.worker-locks/`，名称使用调查 ID 哈希；Windows 使用 msvcrt 字节锁，Unix 使用 flock。目录已加入 Git 忽略。正常退出关闭句柄，进程退出由操作系统释放；遗留空锁文件不等于仍被占用。

锁文件不在释放时删除，防止等待者与新创建文件落到不同 inode。运行中不能删除、重命名这些锁文件或数据库路径。当前要求不同进程指向同一规范数据库路径；不承诺通过硬链接、文件复制、网络文件系统或不同主机挂载路径实现 SQLite 分布式互斥。

本机 Windows 实际子进程测试确认活跃持有者互斥，以及进程终止后的接手；Unix 路径尚未在本环境实机验证。

## 能保证和不能保证的范围

保护的是支持的 Local/Temporal 入口和带执行上下文的 Repository 事务。直接调用底层控制器、直接操作数据库连接或旧版本执行器，不会自动获得这套保护。部署时需要让相关服务全部使用新入口，不能与不识别占用的旧版本并跑同一调查。

旧调查尚无绑定时，由第一个新执行入口固定当前后端；不能据此推断仍在运行的旧进程已经停止。升级时要先停止旧执行器，保持正确后端配置，再恢复已有调查。本批没有主动重启服务、修改私有配置或停止用户执行器。

MySQL 命名锁作用于单个数据库服务实例，未实现跨多主写入节点的共识锁。进程内模型/工具容量仍是每进程限制，不是集群总额度。活跃持有者挂起时不会被自动强抢；需使原持有者退出或会话关闭后再接手，这是保护已有外部操作所采用的保守恢复方式。

已经发出的模型请求、平台操作或沙箱进程不因本地占用丢失而被证明撤销。继续复用原动作幂等、持久作业、预算预留与清理义务，不能宣称供应商推理或平台操作恰好一次。清理资源与持有调查控制占用仍是两套不同责任。

## 验证与剩余工作

新增 `tests/test_worker_ownership.py`，20 项离线测试覆盖独立连接互斥、后端绑定、任务重入边界、同步取消、旧句柄与迟到子任务写入、实际进程退出接手、Local 延后与取消、Temporal 两种竞争路径、Action 身份解析、MySQL 会话竞争/重入/丢锁/重连、监测取消及领取失败恢复。既有轮询预算测试补齐实际调查与占用初始化，原预算断言保持。

实际 MySQL 引擎、多主节点、网络分区与 Temporal 服务回放尚未联调；MySQL 会话约束使用替身验证，不能等同于线上故障验收。没有调用真实模型、游戏平台或沙箱。

最终完整回归：**854 passed、28 skipped**，耗时 169.53 秒。Ruff、冻结契约检查、Wheel 构建、隔离导入及文档本地链接检查通过。跳过项不计为通过。SQLite/MySQL 迁移、NativeMemory 版本、冻结契约和依赖均未改变。

仍待完成：通用未归属事件合批、通用 MCP 长操作、跨任务技术归纳、真实模型同题质量与成本评测，以及本批机制的真实 MySQL/Temporal 故障验证。

交付记录：`data/local/harness-phase17-validation-20261103.json`；完整回归：`data/local/harness-phase17-full-tests-20261103.xml`；修改前备份：`data/local/harness-phase17-before.zip`；源码差异：`data/local/harness-phase17-source-20261103.patch`；Wheel：`data/local/harness-phase17-dist-20261103/`。
