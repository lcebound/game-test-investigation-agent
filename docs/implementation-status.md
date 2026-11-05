# 实现状态

2026-11-05 更新：统一主会话、语义 Router 的任务归属与来源授权已接入，监督工作台已适配 Router–Worker 调查。最新行为与验收边界见[统一协调入口](unified-router-implementation-2026-11-05.md)及[监督工作台](supervisor-ui-implementation-2026-11-05.md)。历史阶段记录保留当时的范围。

2026-11-04 更新：已修复长操作独立结果漏采、清理饥饿、恢复准入与期限边界，接入 Local 终态清理、事件重试、工作台事件与综合、版本缓存、运行指标及 CI 验证。SQLite 24 / MySQL 020 / NativeMemory 6 不变；最新实现、测试与外部阻塞见[审查修复记录](harness-review-fixes-2026-11-04.md)。

2026-10-31 补充：原生调查已支持 Local/Temporal 分轮执行；JSON 诊断、持久复用、搜索读取和排队已修复，并完成隔离 MySQL/S3/Temporal 联调。最新结果与边界见[调查执行优化与真实依赖检查](optimization-and-dependency-checks-2026-10-31.md)。以下历史阶段说明保留原有验收范围。

2026-10-14 更新：测试平台接入统一为 MCP，直接 HTTP API 适配器与独立配置已移除。通用 v2 与 Lyra v1 工具格式均由 MCP Provider 处理；历史 HTTP 联调记录保留原状，不作为当前 MCP 路径的真实游戏验收。

本工程按设计稿的分期方式交付。《游戏测试 Agent 整体架构改进方案》的阶段 A、阶段 B 核心闭环、阶段 C 的共享状态/索引读取/上下文预算/Delta/Playbook/可回查摘要，以及阶段 D 的案例记忆、分析沙箱控制面与离线成对轨迹评分器已实现并通过本地自动化测试。参考 MCP 端到端只证明协议兼容，不代表真实引擎或生产沙箱验收。

## 已实现

- 严格领域契约：调查、预算、能力、动作、许可、证据、假设、计划、报告、异常事件。
- 冻结的 `contracts/v1` 与当前 `contracts/v2` JSON Schema，并由测试检查 Draft 2020-12 合法性。
- Action / Experiment / Run / Observation / Artifact 身份分离；观测使用来源键幂等，Artifact 仍可按内容寻址复用。
- `IngestOutcome` 区分 created/replayed/conflict/rejected；同源改内容报冲突，不同 run 的同字节保留独立观测和 Fact。
- `RunResultSnapshot` 在证据下载前持久化，终态查询返回已保存结果，失败运行的 dump/日志也可摄取。
- `query_run` 只对账已有 action，不进入新运行提交；平台能力通过可信的操作语义字段映射。
- 重复实验序号由 Repository 事务分配，语义哈希排除自然语言描述。
- 结构化 `ObjectiveSpec` 和确定性 `CriterionAssessment`；至少达到要求的独立 run 数才能通过/失败，证据不足保持 insufficient。
- 停止决策在非墙钟预算之前保留已形成的目标判定；执行额度只阻止新实验，不阻止已有结果查询和报告。
- ReportBuilder 消费同一份持久化 Assessment，分别输出 goal_met、goal_failed 或 inconclusive。
- SQLite v16、MySQL 002–012 扩展迁移已完成，未改写 MySQL 初始迁移；工作台会话元数据和消息收据使用独立表。
- TRACE 工作台提供持续会话、历史搜索/重命名/置顶/归档、分轮执行与报告查看、认证 SSE 通知、幂等消息重试、草稿恢复、证据完整性校验与下载、深浅主题和窄屏抽屉；平台通过 Lyra v1 / 通用 v2 MCP Provider 边界连接。
- 新增平台无关的 `InvestigationKernel`：在调用模型前冻结 `ContextSnapshot`，校验后持久化 `DecisionRecord`，并用调查 revision 拒绝旧决策执行。Local、Controller 和 Temporal Activity 共用该内核语义。
- `DurableActionPlanner` 在外部提交前持久化同一份 `ActionProposal` 和规范化 `ActionSpec`；Activity 重试直接读取 `PlannedAction`，不再调用模型重新规划参数。
- `InvestigationWorkingState` 从权威账本重建目标判定、活动/反驳假设、完成/失败/在途动作、待取证项与剩余额度；明细上下文截断不会删除这些必选状态。
- “无进展”由有效观测、运行状态推进、假设变化、目标缺口和用户输入的权威指纹判定，不使用模型文本或截断列表长度。
- 模型调用统一记录 profile/version、raw 摘要、尝试分类、token/时延/费用及 provider request ID；重试共享 deadline 与总预算，失败尝试也计入限额。
- `ContextWindowUsage` 使用保守 UTF-8 估算、部署可配窗口和输出预留限制输入；可选 Fact/证据/假设/动作按优先级裁剪并记录遗漏数，必选状态超限时显式标记而不静默丢失。
- `ContextAccess` 提供作用域隔离的 `search_evidence`、`read_evidence`、`get_run` 和 `get_history`；文本分片保持 UTF-8 边界并复核 Artifact SHA-256，二进制证据留给具备媒体能力的读取器。
- `PlanDelta/HypothesisDelta` 随类型化决策持久化：计划使用 base revision 乐观并发，重试幂等，已运行步骤不可替换；假设必须提交单调递增的完整 revision，不能复用 revision 改写内容。
- 五类诊断策略升级为版本化 `DiagnosticPlaybook`，记录适用条件、必需字段、实验问题、控制/改变变量、证据类型、结果解释、成本和停止条件；只绑定通用操作语义。
- `ContextSummary` 按权威账本 source hash 持久化，记录真实事件 cursor 范围、证据/事实/假设 revision/动作来源和生成器版本；摘要只从原始账本重建，不对上一份摘要再次压缩，过期或来源变化后不再作为当前摘要返回。
- 冻结决策新增权威状态指纹，覆盖证据、动作、假设、目标判定、计划和用户输入；调查 revision 未变化但账本已变化时同样拒绝旧决策，新旧决策重放仍保持幂等。
- `CaseMemoryRecord` 使用不可变 revision 和乐观并发实现 candidate/reviewed/active/deprecated/retracted 生命周期；候选只能来自已完成调查及其不可变报告，保留来源报告哈希、证据 ID、审核者、可信等级、适用引擎/构建/环境、有效期和状态理由。
- 案例检索强制租户/项目隔离，只返回 active、未过期且适用的 `HistoricalCaseReference`；该类型固定声明 `historical_reference` 和“不是当前证据”的警告，且不提供当前 Fact/Evidence 接口。来源撤回可批量产生 retracted revision，后续检索立即失效。
- 上下文组装可加入少量适用案例并纳入统一 token 预算，案例优先于本次证据被裁剪；核心服务只消费通用引擎/构建标识，API scope 与平台身份在边界层映射。
- 分析沙箱控制面固定代码 Artifact/哈希、授权输入、运行时或镜像摘要、依赖锁、runner、资源额度和预期输出 Schema。2026-10-20 内置执行器使用 Windows PSEC，模型可生成 Python/PowerShell 并有限修复，不依赖 Docker/WSL。额外目录读写和联网先由工作台用户单次审批，授权绑定代码、输入、任务和运行时；模型不能自批，拒绝/过期/取消不执行。PSEC 负责目录与网络隔离，supervisor 和 Job Object 管理子进程与资源，工作区磁盘为监测阈值。见 [自主代码分析](sandbox-code-execution.md)。
- 分析 operation 使用 fencing token，终态结果先保存不可变快照再摄取输出；输出逐项复核作用域、大小和 SHA-256，以 `analysis` 来源写入不可变 Evidence，保留代码及输入证据的 `derived_from` 链。
- 沙箱输出不能直接成为 Fact。`DerivedFactCandidate` 必须引用同一作业输出，数据与原始 JSON 完全一致并通过声明的 JSON Schema；只有可信 verifier 明确接受后才生成 Fact。候选 revision、Fact 与事件在同一事务提交；API 从认证主体绑定 verifier/创建者，不接受请求体伪造。当前报告门禁仍把沙箱输出视为 untrusted，不会仅靠派生分析升级已验证结论。
- Cleanup Ledger 已扩展资源种类和清理所有者；普通 action 对账不会误处理 sandbox 资源。分析状态和派生事实 revision 已进入工作状态、摘要 source hash 与冻结决策权威指纹。
- 用户 Intake：`Idempotency-Key`、请求哈希冲突检测、调查与启动 Outbox 同事务。
- API 强制主体、租户、项目作用域；创建、输入、取消和重开写入脱敏审计记录。
- 事件 Intake：256 KiB 限制、事件 payload 哈希、Inbox/观察/调查/启动 Outbox 同事务、incident 聚合。
- 状态投影：单调 revision/transition sequence、持久化事件游标。
- 能力注册：项目、scope、版本、风险、幂等和 action lookup 门禁。
- `ObjectiveCompiler` 生成多个候选计划并确定性排序；计划节点声明类型化输入绑定/输出、前置与完成条件、资源需求、节点成本上限和失败策略。`PlanValidator` 检查依赖缺失与环、输入来源和类型、可运行状态、能力版本/可用性、操作语义、资源域、节点及总预算。
- `PlanRuntimeProjector` 从权威 action/assessment/input/report 账本投影 running/completed/failed/ready，并只在依赖完成后提升后继节点。实验动作必须精确绑定一个依赖已满足的计划节点；`ActionSpec`、`ResourceRequest`、`AdmissionTicket` 和 `ActionPermit` 贯穿冻结 `plan_step_id + plan_node_revision`，Repository 在 dispatch 前复核票据与动作属于同一节点。只读回放检查 decision→action→plan node→admission 的一致性。
- 策略准入：项目隔离、JSON Schema 参数、首版高影响动作拒绝、原子预算预留、动作语义去重。
- MCP/provider 边界：稳定 provider 路由、标准错误、未知副作用优先 action lookup。
- 动作生命周期：持久化 dispatching/accepted/waiting/unknown/终态，对账前禁止盲目换 key 重试。
- 证据与报告：不可变证据、项目隔离、引用完整性、模拟/不可信证据不能升级为已验证结论、Markdown 渲染。
- 性能 CSV 内容哈希复核、数值校验和 P50/P95/P99 Fact 提取。
- 用例失败、崩溃、内存趋势与基础设施 JSON 确定性解析；自动路由生成 Fact，解析失败保留原证据并产生可查询事件。
- 假设账本、证据关系、事实引用以及确定性结论评估器。
- 推理结果缓存：按 investigation/plan revision/reasoning step 保存并校验上下文哈希。
- 本地与 S3 ArtifactStore：内容寻址、租户/项目路径限制、对象大小限制、上传/读取哈希复核，以及 SSE-S3/SSE-KMS 参数。
- OpenAI Agents SDK：有界结构化推理；默认关闭，未配置模型时使用确定性安全退化策略。
- 本地工作流后端：开发/CI 使用，可运行完整的能力发现、计划、准入、提交、轮询、证据摄取、评估和报告闭环。
- Temporal Workflow：细粒度 Activities、持久 timer 轮询、可在 run 前安全接收且去重的输入/证据/取消 signals、状态 query、最长 24 小时的子清理 Workflow，以及带兼容 patch 的 Continue-As-New 有界历史续接。
- Temporal Worker/Client 和稳定 workflow ID 启动、幂等 signal 接口。
- RabbitMQ consumer/publisher：消息大小、路由字符、confirm、手动 ACK、prefetch、quorum queue、有限重投和独立 DLQ。
- 启动、命令与结果事件的持久 Outbox dispatcher。
- Outbox 投递失败持久化尝试次数、错误类型、下次执行时间并做指数退避；终态 Workflow 的迟到命令标记 obsolete，避免永久重试。
- 独立 Bridge 进程组合 RabbitMQ 与 Temporal，分别消费异常与证据更新；API 的 local/temporal 模式互斥。
- 真实复测、工具读取、推理轮次和设备分钟的独立预算计数与停止策略。
- 用例失败、崩溃、性能、内存、基础设施五类证据边界指南。
- FastAPI：创建、详情投影、追加输入、取消、事件游标、报告、能力查询；详情包含计划/预算/缺口/外部运行/清理状态。
- Streamable HTTP MCP 会话、allowlist、凭据环境变量引用、能力工具绑定与响应大小约束。
- 可单独启动的参考 MCP provider，完成发现→执行→查询→证据获取；全部结果强制标为模拟。
- Lyra v1 MCP 适配器：通过平台 MCP 工具映射能力、目录、运行和证据，保留项目、运行身份、SHA-256 与大小校验；原直接 HTTP API 适配器已移除。2026-09 的真实 HTTP 试跑仅作为历史记录。
- 2026-09-22 使用 `qwen3.8-flash` 完成一次模型驱动的通用工作流真实试跑：模型选择实验、平台执行通过并摄取证据、模型选择报告。发现并修复工作上下文未展示最新运行判定的问题；修复后的只读模型回放能区分单次平台 `passed` 与构建验收。详情见 [Qwen 试跑](live-qwen-workflow-2026-09-22.md)。
- 可选 `json_prompt` 模型输出模式：用于无法完成 JSON Schema 输出的 OpenAI 兼容端点；模型原始文本必须是单个有效 JSON 对象，随后按类型化契约和原有决策门禁验证。默认 `json_schema` 不变。
- “提交成功但响应丢失”通过 action lookup 对账的自动化验收，确认不重复提交。
- MySQL 8 初始 DDL、运行时 Repository adapter 和迁移执行器，覆盖 SQL/时间参数适配、事务隔离、全局迁移锁、版本/校验和防篡改以及幂等重跑。
- JWT/JWKS 生产认证：固定 RS256、签名、issuer、audience、必需时效声明、租户/项目选择和 scope 校验；开发 Header 模式默认仅允许 loopback。
- `production` 配置 fail-closed：强制 Temporal、MySQL、S3 和 JWT 组合，远程 MySQL 强制 CA，JWKS 强制 HTTPS。
- JSON 结构化日志、请求 ID 贯穿、结构化字段递归脱敏，以及按租户/项目隔离的运维指标接口。
- 运维 doctor：数据库、Schema、Artifact Store、provider 配置、事件策略及可选网络发现。
- 离线评测 runner 与 8 个固定证据包，覆盖模拟、缺证、来源失配、过期、时效和提示注入边界。
- 成对轨迹评分器要求同一首轮 inconclusive 观测，分开计算“有判别力的第二次实验设计”与“外部标注的实际不确定性减少”；检查对照变量、目标指标、竞争假设预期、可用能力、引用与过早报告，并公开有结果标签的样本量。附带的演示轨迹不是模型运行成绩。

## 下一代方案代码完成矩阵（2026-09-23）

| 设计项 | 仓库内状态 | 已落地边界 |
| --- | --- | --- |
| 目标编译与计划 | 已实现 | Objective IR、候选计划、节点契约、静态校验、PlanDelta、动作及资源票据的节点绑定 |
| 证据依赖图 | 已实现 | Evidence/Fact/Assessment/Claim/Summary 边、来源追溯、传递失效、报告 revision、投影 checkpoint 与发布门禁 |
| 按需上下文与分层记忆 | 已实现 | Context Broker/Manifest、按需证据与能力、token 预算、可回查摘要、案例记忆、版本化 ProjectPack |
| 统一模型网关 | 已实现 | ModelProfile、结构化输出校验、白名单规范化、共享 deadline、总 token/费用预算、尝试与供应商使用量审计 |
| 资源调度与分析 | 控制面已实现 | 公平队列、持久 admission、租约/fencing、计划节点身份、AnalysisRecipe DAG 与沙箱作业契约；真实执行侧拒绝旧 token 和真实隔离强度仍待环境验收 |
| 决策审计与回放 | 已实现 | DecisionEnvelope/Outcome、只读状态/决策/分支回放、完整性检查、版本化 ReleaseUnit；真实模型 shadow/盲测仍待数据 |

“已实现”表示仓库契约、控制逻辑、持久化路径和自动化测试已经存在，不表示外部生产系统或真实引擎已经完成验收。

## 尚需真实环境接入与验收

- 在目标 MySQL、S3、JWT IdP、Temporal 和 RabbitMQ 上完成权限、容量、告警、故障恢复与升级回放演练；本轮按要求未连接这些真实服务。
- Lyra MCP 与通用 MCP 已使用真实客户端会话完成模拟协议回归；仍需平台 MCP 服务与实际游戏环境的端到端验收，以及 Temporal 重启恢复和资源所有权验证。
- 已有通用结构化解析契约及五类确定性解析器，仍需根据真实 Unreal/Unity 产物字段、符号化格式和版本差异增加薄映射。
- 将结构化日志与 `/internal/metrics` 接到组织现有日志、指标、追踪和密钥系统，并冻结实际 SLO/告警阈值。
- 扩充人工标注评测集并接入 shadow/灰度；现有 8 个报告边界基线和手写轨迹示例只验证评分机制，不是模型提升证据。
- 阶段 B 后续的 local/Temporal 全量语义一致性矩阵，以及用户输入、迟到结果和报告多 revision 的完整事件规则。
- 阶段 D 后续的真实隔离 runner/broker 适配与隔离强度验收、领域/项目知识及策略经验生命周期，以及采集真实模型决策后的“有增强/无增强”盲测对照；阶段 C 仍需用长轨迹数据验证决策保持率和历史失败实验重跑率。
- 执行 provider 必须在真实设备/账号/游戏实例边界拒绝旧 fencing token；当前控制面会签发并校验 token，但不能代替执行侧改造与故障演练。
- 完成真实 Unreal/Unity “发现→执行/读取→证据→报告”端到端验收后，才能声明真实引擎支持。

仓库内下一代通用控制面已收尾；以上项目依赖具体平台地址、凭据、测试目录、Schema、真实产物、执行环境或人工标注数据，不能由本地替身或单元测试代替验收。
