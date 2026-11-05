# 下一代架构实现记录

日期：2026-09-23 · 状态：已实现模块化单体的首个可运行版本

本记录对应 `agent-next-generation-architecture-and-interview.md` 的实施结果。原文保留为架构提案和设计依据，不回写为历史事实。

## 已实现

- `models/gateway.py`：版本化 ModelProfile、有限尝试、故障分类、白名单规范化、Schema 验证、每次尝试用量与原始输出摘要。`ReasoningService` 已经统一通过该网关调用。
- `planning/compiler.py`：将 InvestigationRequest 编译为带字段来源的 ObjectiveIR，生成少量分层计划候选，执行静态校验和可解释排序。本地控制器会在首轮前编译并持久化计划。
- `context/broker.py` 与 ContextManifest：必选工作状态、来源游标、权限范围、纳入/遗漏项及 token 估算均进入清单；能力支持先发现摘要、再加载精确 Schema。
- `provenance/graph.py`：在现有关系库账本上投影 Observation → Fact → Assessment 数据依赖，并保留独立论证边；支持来源追溯、反向影响定位、重算顺序和计算身份。
- DecisionEnvelope 与 `audit/decision_envelope.py`：决策记录已嵌入上下文清单、模型/提示/契约版本、预算快照和规范化日志；提供无执行凭据的状态回放、决策重放与完整性扫描。
- `scheduling/admission.py`：项目轮转、优先级、配额、并发上限、资源域互斥、TTL 和单调 fencing token。该层只做 Agent 准入，不替代测试平台的设备调度。
- `analysis/recipes.py`：受限 DAG、注册式确定性组件、JSON Schema 输出契约和可信 verifier。任意生成代码仍必须走原有隔离 runner。

## 兼容性与边界

- 保留现有 v2 对外类型的默认值，旧 ContextSnapshot 和 DecisionRecord 仍可读取。已重新导出受影响的 v2 JSON Schema。
- 证据依赖索引使用现有关系表生成读投影；它返回精确失效与重算计划，不会就地改写已发布的历史报告。
- ResourceScheduler 和 AnalysisRecipeRunner 是可组合的控制面组件，未声称已经验证真实多设备吞吐或生产隔离强度。
- 本轮不引入图数据库、新微服务或不可控的动态代码执行。

## 验证

- 新增架构测试覆盖网关修复、目标编译、渐进能力加载、增量失效、决策封套/回放、公平准入/fencing 和 recipe DAG。
- 全量 pytest、Ruff 和 compileall 作为交付门禁；实际执行结果以本次交付回复为准。

## 第二轮集成

- Local controller、LocalWorkflowBackend 和 Temporal Activity 均已通过 ContextBroker 组装每轮上下文，Manifest 不再只是独立组件的输出。
- 同一来源提交更高 revision 时，事件账本记录 `provenance.source_superseded`；旧观测和它们的派生事实不再进入新的上下文、目标评估、摘要或报告。
- 来源更正会生成反向失效计划，将引用旧证据的已支持/已确认假设降级为 unresolved，并撤回该调查派生的活跃案例记忆。
- 目标评估 revision 改为基于追加式证据/事实账本的单调身份，避免过滤旧证据后错把历史评估当成最新结果。
- AnalysisRecipeService 已接入 ArtifactStore 和 EvidenceService：有界读取当前证据，校验内容摘要，执行已注册组件，并将验证后输出作为带 `derived_from` 的新证据回写。
- 新增只读检查 API：`context-manifest`、`objective-compilation`、`provenance/{target_id}` 和 `audit/integrity`，均复用现有租户/项目作用域校验。

## 第三轮集成

- 资源准入已从进程内队列升级为 SQLite/MySQL 可恢复账本，持久化排队、公平调度游标、准入票据、消耗和单调 fencing token。
- 动作在 dispatch 前必须获得并原子绑定 admission ticket；重试可复用有效票据，过期票据可重新准入，终态会释放或取消资源。
- `ActionPermit` 现携带 `admission_ticket_id`、`resource_domain` 和 `fencing_token`；网关会将受信准入元数据附加到下发动作，并在 dispatch 前拒绝过期许可或旧 token。
- ProjectPack 版本已持久化，以 `(tenant_id, project_id, version)` 为不可变身份，进程重启后仍可解析。
- 决策后续结果以追加式 `DecisionOutcomeRecord` 记录：选中动作、run result 引用和终止依据都不会回写或覆盖原始 `DecisionEnvelope`。
- 数据库版本升级为 SQLite v13 / MySQL 009。

## 第四轮集成

- 新增通用 `provenance_edges` 账本，数据依赖边与论证边分开保存；报告发布会持久化 Evidence/Fact/Assessment 到 Claim，以及 Claim 到 Summary 的关系。
- ProvenanceIndex 现包含 Claim/Summary 节点，并沿下游传播 stale 状态；完整性扫描会检查报告主张的证据、事实、评估链路及过期依赖。
- 上游证据更正会传递失效所有派生证据和事实，它们不再进入当前上下文、评估或新报告。
- ReportBuilder 使用数据库中的下一个 revision，支持来源修正后追加新报告，不覆盖历史版本。
- `ProjectionCoordinator` 为 objective、summary 和 provenance 投影记录事件游标与内容摘要；动作提交及报告发布前必须刷新并通过新鲜度门禁。
- 数据库版本升级为 SQLite v15 / MySQL 011。

## 第五轮集成

- ModelGateway 的超时现为整次调用的共享 deadline，而不是每次重试重新获得完整时间。
- token 与费用上限在每次 provider 响应后立即累计和检查；即使该次输出是格式或 Schema 错误，也会计费并在超额时停止重试。
- Agents SDK 返回的 token 用量和 request ID 已统一转换为 ModelUsage，推理与动作规划不再默认记录为零用量。
- 模型动作规划的 profile/version、raw output 摘要、规范化日志、每次尝试及汇总 usage 随 PlannedAction 一起不可变持久化。
- 调查详情增加 `model_usage`，聚合推理与动作规划的调用数、尝试数、token、时延、估算费用和缓存命中。
