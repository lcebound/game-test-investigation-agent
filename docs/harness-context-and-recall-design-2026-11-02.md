# Harness 上下文隔离、溢出保存与链式召回设计

> 实现状态更新（2026-11-03）：本文保留设计时的缺口描述；当前能力、版本及真实验证结果以[收尾记录](harness-completion-2026-11-03.md)为准。

日期：2026-11-02，更新：2026-11-03。状态：已实现有界输入、字段取证、单步 Worker、多任务、运行反馈、历史召回和消息规则归属；完整语义 Router 等未实现范围见各批记录。

已实现接口及离线验证见[第一批实现](harness-phase1-implementation-2026-11-02.md)、[第二批字段导航](harness-phase2-implementation-2026-11-02.md)、[第三批问题账本](harness-phase3-implementation-2026-11-02.md)、[第四批单步 Worker](harness-phase4-implementation-2026-11-02.md)、[第五批后台代码作业](harness-phase5-implementation-2026-11-02.md)、[第六批会话多任务](harness-phase6-implementation-2026-11-02.md)、[第七批运行反馈协调](harness-phase7-implementation-2026-11-02.md)、[第八批跨任务召回](harness-phase8-implementation-2026-11-02.md)、[第九批全会话总览](harness-phase9-implementation-2026-11-02.md)、[第十批自动选择与召回执行一致性](harness-phase10-implementation-2026-11-02.md)、[第十一批消息归属与投递](harness-phase11-implementation-2026-11-03.md)及[第十二批 Worker 退回与重新归属](harness-phase12-implementation-2026-11-03.md)。已接入任务卡、运行订阅、不可变批次、关联历史读取链和全会话状态投影；默认 Worker 仍隔离，完整语义 Router 与跨任务技术归纳仍为后续目标。

本文件补充[Python Harness 渐进改造方案](super-agent-migration-plan-2026-11-02.md)，将 Router/Worker 隔离、Inline & Spill、链式召回和成熟 Harness 的机制落实到现有项目模块。继续保留 Python、现有存储、MCP 和 Local/Temporal，不引入另一套 Agent 框架。

## 1. 上一版已经借鉴与尚未补齐的部分

上一版覆盖任务执行分层、已有任务反馈直达、外部持久状态和按轮让出，但对“上下文外的信息如何被发现、按引用读取并重新进入窗口”的接口仍不够具体。

本版补齐六个行为契约：

1. Router 和每个 Worker 的上下文边界；
2. 事件合并批处理与控制消息优先；
3. 短内容内联、长内容溢出保存，并带可读取引用；
4. 从任务/会话索引沿引用找回输入、原文和结论；
5. 上下文重建、检查点恢复与长期记忆分开；
6. 压缩后可恢复性和任务隔离的专门评测。

这些机制服务于当前调查效果，不要求先完成多任务 Router。单个 Worker 的 Spill/Recall 也应先落地。

## 2. 对上游代码的核实

DeepSeek 固定版本：
`6337fc823ce20251c8de8085948165c81aaa701e`。

实际代码可确认：

- cards.ts 生成有界预览及 read 资源；
- resources.ts 按任务、输入、报告与执行记录分页读取，并检查 Worker 可读范围；
- assessment.ts 提供有界的批次、线程邻域和相关任务信息；
- core/intake.ts 进行订阅扇出，evals.ts 合并待处理事件；
- Worker 的详细执行记录与主会话收到的状态卡分开。

来源：
[cards.ts](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/app/cards.ts)、
[resources.ts](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/app/resources.ts)、
[assessment.ts](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/app/assessment.ts)、
[架构中的读取资源与会话消息](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/007-architecture.md)。

这里借鉴 Inline & Spill 和沿引用召回的行为，不据此认定原型已经实现通用“近期/高频自动分层”算法，也不认定任意 Worker 能读取其他 Worker 的完整上下文。频率只是本项目拟议的相关性信号；重要约束和反证不能因为低频而被隐藏。

## 3. 现有项目的可复用基础

| 当前模块 | 已有能力 | 补齐方向 |
| --- | --- | --- |
| NativeMemory | 每调查消息、检查点、摘录、输入、读取范围 | 将“裁掉”变成“留下可读引用” |
| ContextBroker / ContextManifest | 按目标组装、按需能力、被纳入的来源身份 | 形成角色与阶段一致的 ContextPacket |
| ContextAccess | 有作用域的原文读取、历史、运行与摘要 | 增加统一引用解析；保持原有权限 |
| InvestigationSummaryService | 从权威账本重建、带来源哈希的摘要 | 生成轻量任务卡和恢复包，避免摘要套摘要 |
| Artifact Store | 不可变原始内容和 SHA-256 | 存放 Spill 内容，不另建文件系统数据库 |
| provenance / derived_from | 证据依赖与失效关系 | 召回时核对版本，旧材料保持历史身份 |
| WorkbenchStore / investigation_relations | 会话和调查续查关系 | 扩展显式独立任务关系及检索入口 |
| CaseMemoryService | 审核后的跨调查流程经验 | 保持与当前调查事实分开 |
| Inbox / Outbox / 预算账本 | 去重、投递、预留和恢复 | 承载召回审计及事件批次，不增加第二条队列体系 |

ContextAccess.get_history/get_summary 已存在，但目前不能据此认为原生模型已经拥有相应召回工具；需要把能力明确暴露给模型。

## 4. Router / Worker 的上下文合同

### 4.1 Router 只读轻量任务目录

Router ContextPacket 包括：

- 本轮待处理事件卡及输入来源；
- 当前相关任务的 ID、目标、执行/等待状态；
- WorkerResultCard 的短结果、未决问题和原文引用；
- 显式关联、订阅、取消状态与剩余协调预算；
- 必要的用户交互约束。

不默认包括 Worker 消息历史、全部日志、原始平台返回、代码执行正文、完整假设账本或所有旧任务。

任务目录也分页；相关任务很多时只加载本批需要的卡片和索引。不能让一个“轻量卡片”列表随着运行数月无限增长。

规则能确定路由时不调用 Router 模型。语义 Router 存在时也只处理归属与交互，不接管单任务实验选择和根因判断。

### 4.2 Worker 拥有调查专属上下文

Worker ContextPacket 绑定：

`tenant_id + project_id + investigation_id + input_revision + context_epoch`。

包括自己的 TaskBrief、问题、计划、假设、关键反证、当前证据、工具和预算。不同调查不共享可变 messages/notes/cache。

多个 Worker 可共享不可变 Artifact 的底层存储，但实际读权限、使用范围和预算分别核对。一个模型客户端或连接池可以共享，业务上下文不能共享。

现有代码已经按 investigation_id 持久检查点；改造主要把隔离约束显式化，补测试，不重新创建十套数据库。十个任务可有十份独立状态，实际同时执行数仍受现有并发配置限制。

### 4.3 WorkerResultCard

拟议字段：

~~~text
task_id / input_revision / progress_revision
state / result_scope
summary
answered_question_ids / remaining_question_ids
finding_refs / report_ref
critical_counterevidence_refs / coverage_ref
pending_operations / cleanup_state
read_ref
~~~

Router 收到的是卡片，默认不加载它背后的执行记录。汇总结果保留来源和限制，不把 Worker 的假设经 Router 转述后变成已验证结论。

跨任务需要原始材料时，创建有明确范围的 Recall 请求或将引用交给目标 Worker。不能靠全量拼接两个上下文完成“同步”。

## 5. 事件合并批处理

用现有 Inbox/Outbox 承载 EventBatch，不增加消息中间件。

流程：

~~~text
持久接收原事件
  → 先处理取消/停止等控制事件
  → 明确归属的反馈直达原任务
  → 其余按项目、线程/任务和事件类型合并
  → 生成有界卡片批次
  → 规则路由；仍有歧义才进入 Router 模型
~~~

批次保留原 event_id、载荷哈希和游标区间；数量或字节数达到上限即切批。批次不是丢弃原事件，只是减少重复的模型唤醒与上下文。

合并规则：

- 重复状态通知可以只展示最新状态，但所有原事件仍可回查；
- 失败、恢复、反证和阶段切换保留不同事件身份；
- 用户取消不等待普通批次，立即持久生效并使旧决策失效；
- 用户修改目标后，批次按输入版本分界，旧版本结果不能覆盖新要求；
- operation 完成等已绑定消息 pass-through，不再全局研判一次；
- Agent 自己的报告/状态不能触发无限接单；
- 更新中的批次使用 generation，旧发送回执不能删除新到事件。

批处理只减少协调开销，不承诺节省 Worker 的全部模型成本。记录批次压缩前后事件数、字节数、延迟及实际模型调用。

## 6. Inline & Spill：存储与窗口不是同一件事

### 6.1 内容分层

| 层 | 内容 | 模型默认可见性 |
| --- | --- | --- |
| Pin | 当前目标、有效约束、关键反证、在途动作和预算 | 必须保留；原文过长时放有来源的短表示及引用 |
| Inline | 当前问题相关片段、近期必要反馈、当前工具结果 | 在本轮预算内显示 |
| Spill | 完整旧消息、长工具返回、大代码/分析输出、过往细节 | 保存原文，只显示引用与有界预览 |
| CaseMemory | 审核后的通用经验 | 作为规划提示按需加载 |

选取优先级首先是任务相关性和重要性，再考虑近期与复用次数。重复读取的无关内容不能因为“高频”挤掉反证。

Spill 继续使用现有数据库和 Artifact Store。大工具结果应在第一次返回时就保存并返回卡片，避免先把很大的首尾预览塞进窗口、超限后再反复裁剪。

### 6.2 必须留下可执行的读取入口

拟议资源卡：

~~~json
{
  "kind": "evidence",
  "resource_id": "evidence-id",
  "revision": "source-revision",
  "content_hash": "sha256",
  "preview": "有界预览；不替代原文",
  "total_bytes": 92109,
  "truncated": true,
  "read_ref": "evidence/evidence-id",
  "origin_task_id": "investigation-id"
}
~~~

字段和资源协议尚未实现。不同对象使用适合自己的 revision/哈希，不能伪造不存在的版本。

每个面向模型展示的 read_ref 必须能由相应读取工具解析；只有无法读取的哈希或数据库 ID 不算召回入口。权限拒绝、过期、对象缺失和预算不足分别返回，不把“读不到”描述成“内容不存在”。

资源引用是应用内类型化句柄，不接受模型提交任意磁盘路径或对象存储地址。返回长内容时继续分页，并提供 next_cursor/remaining_bytes。

### 6.3 压缩顺序

1. 将已持久化的大结果替换为资源卡。
2. 保留完整 assistant/tool 组的协议关系，再移出无关旧组。
3. 从权威账本构造问题进展和任务简报。
4. 仍超限时，生成带来源清单的摘要；原始内容继续 Spill。
5. 无法容纳任务关键状态时，明确失败或形成有限交付，不静默删除关键约束。

摘要不证明原文，不能作为验收事实。结构化事实与报告继续经过现有 AssertionVerifier、ObjectiveEvaluator 和发布校验。

## 7. Chain of Recall：先找任务，再找原文

### 7.1 最小工具面

先提供两个工具门面，复用现有 ContextAccess，而非为每种对象增加一个工具：

- `agent_find_context(query, filters, cursor)`：返回有界任务/输入/证据/报告卡，不自动加载正文；
- `agent_read_context(ref, cursor, max_bytes)`：按引用读取、分页、核对作用域、版本和哈希。

这些是拟议接口。原有高频证据搜索和读取工具可暂时保留，逐步统一返回形状，避免一次破坏全部提示词和回归。

默认先用关系与文本字段查找，不为“记忆”先引入向量数据库。只有固定评测证明关系/关键词召回不足时，再增加语义检索候选层。

### 7.2 引用链

~~~mermaid
flowchart LR
    Q["新会话中的旧事线索"] --> I["授权范围内的任务目录"]
    I --> T["旧任务卡"]
    T --> R["旧报告 / 问题进展 / 输入目录"]
    R --> E["原始证据片段"]
    T --> C["必要时读取旧检查点的公开记录"]
    E --> N["当前任务中的历史引用或比较证据"]
~~~

模型不必每次逐节点调用。关系明确时，读取接口可一次返回两级有界目录；仍保留每层来源与可继续读取的引用。记录调用次数、读取字节和链深度，防止召回自身变成漫无目的的翻页。

### 7.3 示例：新会话里问“上次为什么没确认恢复”

1. Router 以项目、问题词和已有会话关系找到候选任务卡。
2. 若旧任务唯一且范围明确，将引用交给当前 Worker；多个候选则返回有限候选及区分信息。
3. Worker 读取旧报告、当时的恢复问题状态，再取相关原文。
4. 回答区分“当时缺少独立恢复证据”和“恢复失败”。
5. 旧任务保持历史时间与构建标签；不因为被召回就描述成当前环境状态。

用户的明确旧任务引用应直接解析，不要求为了读取已有授权资料再次人工审批。确实无法消除的身份歧义才需要澄清。

### 7.4 跨 Worker 访问边界

Router 只在当前用户/项目授权范围内搜索。Worker 默认读取本调查，跨调查引用通过明确绑定或召回授权的引用集提供；每次读取仍核对来源归属。

不能仅凭“同 tenant/project”就把其他 Worker 的全部会话灌入当前任务。完整执行记录按需读取公开模型输出/工具记录，不要求存取模型私有推理正文。

旧证据如要用于本次正式比较，必须按现有领域规则登记来源、角色与范围。CaseMemory 继续作为历史规划提示，不直接升级为本次 Evidence/Fact。

## 8. 压缩、检查点与长期记忆分开

| 对象 | 要解决的问题 | 项目载体 |
| --- | --- | --- |
| ContextPacket | 下一轮模型实际看什么 | ContextBroker / native context |
| Checkpoint | 中断后从哪一步继续 | NativeMemory.save、领域账本、Temporal |
| TaskBrief / Progress | 新窗口怎样理解任务和未决事项 | 来源摘要、问题账本 |
| Spill 内容 | 被移出窗口后原文在哪里 | Artifact、事件和输入账本 |
| CaseMemory | 下次相似任务学到什么流程经验 | 现有审核案例记忆 |
| Report | 对用户交付哪些有证据的结果 | 现有不可变报告与发布门禁 |

不能因为存下了检查点，就默认压缩后模型会找回旧内容；也不能因为有长期记忆，就让所有调查共用一份消息列表。

窗口切换时构造 ContinuationPacket：目标与有效约束、未决问题、支持/反证引用、覆盖、在途操作、预算、最近公开决策及下一项可行操作。它从权威来源重建，不总结上一份摘要。

新窗口首先核对输入版本与操作账本，再决定是否继续取证。恢复已提交动作以查询回执为先，不能通过回放旧消息再次运行故障注入。

## 9. 借鉴成熟 Harness 的具体机制

以下是机制来源与本项目的适配选择，不是框架迁移清单。

| 来源 | 可核实机制 | 本项目采用方式 |
| --- | --- | --- |
| DeepSeek Super-agent 原型 | 有界卡片、资源读取、订阅和任务隔离 | RouterCard、WorkerResultCard、批次与类型化引用 |
| Deep Agents | 大工具结果移出窗口；压缩时保留完整历史的读取入口 | Artifact-backed Spill；先卸载冗长内容，再压缩 |
| Anthropic 上下文工程 | 用轻量标识按需取上下文，结构化笔记及隔离子任务 | 有界 Recall、TaskBrief、独立调查上下文 |
| Anthropic 长期任务 Harness | 会话之间保留明确进展产物，并检查完成状态 | ContinuationPacket、问题清单、恢复时核对证据与运行 |
| OpenAI Agents SDK / Cookbook | 会话接口、按完整轮次裁剪、阶段压缩；记忆与输出产物分离 | 复用现有 SDK 接口思想，业务账本仍作权威来源 |
| LangGraph | 线程检查点与跨线程 Store 分开 | 保留 Local/Temporal；不把长期案例记忆混进检查点 |

原作者资料：

- [Deep Agents 上下文管理](https://www.langchain.com/blog/context-management-for-deepagents)：工具结果外移与历史可恢复。
- [Anthropic 上下文工程](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents)：按需读取、结构化笔记和上下文隔离。
- [Anthropic 长期任务 Harness](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents)：跨窗口进展交接与完成检查。
- [OpenAI 会话记忆示例](https://developers.openai.com/cookbook/examples/agents_sdk/session_memory)：会话裁剪和压缩。
- [OpenAI 证据审查与记忆示例](https://developers.openai.com/cookbook/examples/agents_sdk/building_reliable_agents_memory_compaction)：工作上下文、可复用流程记忆和可审阅产物的分工。
- [LangGraph 持久化](https://docs.langchain.com/oss/python/langgraph/persistence)：线程检查点和跨线程数据 Store 的区别。

本节只采用各来源明确说明的机制。后文字段、接口和上线顺序是针对本项目的设计。

### 9.1 现有 Agents SDK 的复用边界

本机锁定的 openai-agents 版本为 0.22.2，已从安装源码核实 Session 的 get_items/add_items/pop_item/clear_session 接口，以及 OpenAIResponsesCompactionSession 的存在。

但 native_mcp 当前直接使用 Chat Completions 和 NativeMemory；不能只给 typed Runner 加一个 Session，就声称两条路径都完成记忆改造。

采用一个领域历史/资源存储与角色化投影：typed 需要时通过 SDK Session 适配，native 继续通过现有原生信封适配。任何路径只保留一个消息历史所有者，避免 SDK session 再次拼接 NativeMemory 的全部历史。

OpenAIResponsesCompactionSession 调用 Responses compact 接口，不能默认用于当前 DeepSeek Chat Completions。先实现供应商无关的 Spill/Recall 与确定性恢复包；特定供应商压缩能力验证后再启用。

### 9.2 薄执行钩子

借鉴 Harness middleware 的分工，逐步从大控制器提取：

- before_model：组装角色/阶段 ContextPacket、预算预留；
- after_model：归一化、截断/错误处理、输入版本检查；
- before_tool：当前权限、Schema、动作身份及预算；
- after_tool：原文保存、生成资源卡、进展和副作用回执；
- checkpoint：提交完整调用组、输入与操作游标；
- before_publish：原文、结构化主张、报告范围和当前版本校验。

优先提取已有函数，不先实现通用插件平台。不同钩子不能各自重试同一操作或重复结算。

## 10. 存储与失效

最小新增是资源目录投影和召回审计，可直接投影既有记录；只有原账本无法表达的批次/关联才增加表。

上游文本资源使用 UTF-16 字符偏移，而当前证据读取使用 UTF-8 字节偏移。复用分页思想时保留本项目既有字节契约，不照搬数值偏移，中文片段与引用仍须核对边界。

MemoryRef 至少关联对象种类、原 ID、来源调查、版本/哈希和可读句柄。不能将 preview 当成原文，也不能丢掉模型显示字符与原文字节范围的区别。

引用解析返回 current / historical / superseded / unavailable，语义分别处理。源证据撤回、输入纠正、构建变化或权限撤销后，旧摘要不再自动进入当前 Inline。已生成的不可变历史记录可以保留，但标记旧范围。

召回本身为只读动作，仍计入既有读取/字节/时间预算；不通过“查记忆”绕开调查工具限额。已核验的不可变读取可缓存，缓存不能跨项目或忽略当前授权。

## 11. 针对可恢复性的验收

优先增加能归因到机制的离线测试，而非先跑大量付费任务：

| 用例 | 必须满足 |
| --- | --- |
| 十个任务使用各自独特的输入/证据 | 任一 Worker 的默认窗口没有其他任务的详细内容 |
| 连续产生大量长结果 | Router 的活动上下文保持有界，Worker Spill 后仍可读取原文 |
| 第一次输入中埋入后来需要的约束 | 压缩后可按引用找回，不能只留最近四条 |
| 从新会话回问旧任务 | 能先定位旧任务，再按需读报告和原文，保留历史范围 |
| 高频无关结果与低频反证同时存在 | 反证不被频率策略挤掉 |
| 引用源变更或撤回 | 陈旧状态明确，不能继续自动作为当前事实 |
| 一个批次含失败、恢复和取消 | 控制及时生效，失败/恢复原事件均可回查 |
| 副作用已完成，检查点确认前崩溃 | 恢复复用操作账本，不重复外部执行 |
| 任意卡片中的 read_ref | 在权限和预算允许时可解析；失败原因明确 |
| 相同模型、证据和预算的成对任务 | 分别测回答率、错误结论、召回成本和总耗时 |

关键指标：压缩后信息恢复成功率、召回精确度、断链率、来源过期识别、任务串扰、每个答案的召回调用/字节，以及真实模型总成本。

强制小窗口和高频压缩的压力测试用于暴露缺陷，不把压力配置当生产默认，也不把压力测试分数当真实调查准确率。

## 12. 实施顺序

1. **P0**：在修复输入/报告封套时一起定义 MemoryRef 与 ContextPacket，避免先修成另一份无限增长的摘要。
2. **P1**：单 Worker 的 Spill/Recall、按来源恢复包、可读输入索引和字段取证；此时就能改善原生调查。
3. **P2**：WorkerStep 与显式读写日志，验证压缩、暂停、恢复时工具调用组闭合。
4. **P3**：Router 任务卡、批处理、pass-through、跨任务受限召回与统一交互。
5. 固定题集验证收益后，才考虑额外专家 Worker 或更复杂的语义检索。

本文最初交付为设计补充。后续第一批实现已经修改原生调查上下文和召回路径，仍未安装 DSH、Deep Agents 或 LangGraph，也未调用真实模型。
