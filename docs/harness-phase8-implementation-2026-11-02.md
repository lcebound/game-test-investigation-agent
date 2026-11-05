# Harness 第八批实现：固定版本的跨任务召回链

日期：2026-11-02。接续[第七批运行反馈协调](harness-phase7-implementation-2026-11-02.md)，实现受限的“历史任务卡 → 已关联报告 → 报告引用目录 → 原始证据”读取链。继续复用现有 Python、报告、证据、ArtifactStore、预算和工作流后端。

## 解决的问题

此前续查会保存旧报告摘要和哈希，Worker 无法沿该引用读回完整报告及其引用的原文。新会话也缺少有界历史发现与明确的来源关联入口。

现在可以按目标关键词发现旧调查，并将选定报告作为历史来源关联到当前或下一份调查。Worker 只在需要时读取对应材料。旧报告与原文保留来源调查、报告版本、内容哈希和历史标记；它们不会被导入当前 EvidenceService，也不能直接替代当前运行证据。

调查内部的候选动作、取证优先级与判断规则继续由原 Agent 自动执行。关联历史来源表达消息要参考哪些旧材料，不增加人工调查排序步骤。

## 发现与读取分开

新增 memory/recall.py：

1. **发现**：按当前授权 tenant/project 搜索已结束调查的目标文本。使用字面子串匹配，每次最多扫描 100 份调查、返回最多 10 张卡，总响应保持在约 4 KiB；通过 next_cursor 继续扫描。约束、完整用户输入、工具记录和模型上下文不参与该搜索。
2. **关联**：来源必须属于同一项目，且已有结束后的报告。用户选择具体 report_sha256，服务保存固定报告 revision、报告 JSON 哈希以及最多 50 个被报告引用的证据 ID、哈希和 source_revision。每份当前调查最多关联 20 个来源。
3. **回读**：先读取固定版本报告，再查看其引用目录，最后读取选定的原始证据。未关联来源、报告未引用的文件、其他项目或改变后的原文会拒绝读取。

发现只返回目标/摘要短卡和报告哈希，不会创建读取关联。模型没有修改关联的工具。原始 Worker 的会话、检查点、代码执行权限和工具历史不在召回范围内，关联也不传递给来源的其他历史来源。

如果来源后来发布新版本，现有读取仍返回已选择的旧版本。搜索卡上的“已关联”仅适用于同一报告哈希；最新版本尚未关联时不会误标。报告与原文发生损坏、失效或无法核对时返回缺口，不自动改读另一份版本。

## 创建、续查与撤销

新调查支持 constraints.recall_references，格式为 source_investigation_id、report_sha256 的数组。调查、历史关联和待启动记录在同一事务中提交；引用错误或权限不足时整体回滚。创建重试返回原调查，不会重新启用已经撤销的关联。

工作台续查在已有 followup_reference 中补充报告 revision，并自动关联明确指定的父调查报告。该关联与新调查创建在同一事务中完成。新的独立任务不继承其他任务的历史关联；旧数据不批量回填，存量调查可通过历史引用入口补充关联。

运行期间增加、更新或撤销关联，会记录 recall.link.changed 并通过现有 evidence 命令唤醒同一调查。Local 使用已有持久命令循环；Temporal 继续使用现有 evidence_updated 信号和 command_id 去重，没有修改工作流历史定义。

撤销会阻止该引用的后续读取。原生 Worker 在发现关联版本改变后清理缓存、历史读取范围和旧会话工具消息，再从自己的工作账本恢复。当前调查已产生的计划、证据和审计记录继续保留。处理中遇到关联变化时，旧模型响应的工具组会被丢弃，已消耗预算仍照常计入。

同时补上报告草稿的恢复边界：草稿保存历史关联与外部反馈的 generation。恢复时若任一版本已经变化，旧草稿不能直接发布；旧格式草稿在没有这些上下文变化时仍可恢复。

## Worker 的 Inline 与按需回读

工作记忆只保留最多 3 张历史来源卡、总数和 history 目录引用。完整报告、原文和关联清单留在原有存储。构建编号只保留有界片段及身份哈希，其他历史环境信息以原报告和原文为准。

原生工具入口如下：

| 入口 | 用途 |
| --- | --- |
| agent_find_history | 在当前项目内搜索历史目标卡，before 接 next_cursor |
| agent_read_context：history | 分页查看已关联来源 |
| history/{source_id}/report | 读取固定版本报告 JSON |
| history/{source_id}/evidence | 查看报告引用且已固定的证据目录 |
| history/{source_id}/evidence/{evidence_id} | 按 UTF-8 字节读取原文或做字面搜索 |

只有用户目标/补充涉及历史，或已有来源关联时才展示历史工具。历史调用使用原工具读取预算，重复的固定原文可以复用缓存；缓存命中前仍检查关联、版本和来源状态。撤销的来源不能通过旧缓存继续读取。

正文每次最多返回 4,096 字节，保留 next_byte_offset、哈希和来源信息。历史报告或文本超过 4 MiB 时拒绝返回片段。底层仍复用 ArtifactStore 的现有对象读取方式和对象上限，本批未实现流式大对象读取。二进制材料需要后续媒体读取能力。

历史数据使用独立 resource_kind 和 source_evidence_id，不会混入当前证据目录。现有报告核验继续要求当前调查拥有的证据引用；历史解释只作为历史上下文。历史关联不扩大 Provider、代码执行或实验权限。

Typed 路径通过既有 diagnostic_guidance 获得短卡，原文可用受鉴权 API 读取。本批未增加 Typed 专用模型召回工具。

## 工作台与 API

工作台增加“历史引用”：输入目标关键词，选择旧报告后可关联到正在调查的任务，也可加入下一次调查的请求。选择项在切换任务时清空，避免悄悄带给独立任务。执行面板展示已关联来源，支持读取报告、查看引用原文和撤销；超过首批卡片时可打开完整目录。

完整报告优先显示摘要和当时未解决的问题，JSON 记录放在可展开详情中；部分报告页与原始日志显示为文本，并支持继续读取。报告和来源标题用 textContent 渲染，HTML 作为文字展示。

| 接口 | 用途 |
| --- | --- |
| GET /v1/history/search?query=...&before=... | 项目内历史卡片发现 |
| GET /v1/investigations/{id}/recall | 有界关联摘要 |
| POST /v1/investigations/{id}/recall | 关联 source_investigation_id、report_sha256 |
| DELETE /v1/investigations/{id}/recall/{source_id} | 撤销关联 |
| POST /v1/investigations/{id}/recall/read | 以既有 ContextRead 请求分页读取 |

这些入口要求当前项目的 runs:read、evidence:read，逐次核对当前调查及来源归属。鉴权沿用项目共享工作台，不新增私人会话 ACL。原始读取不允许仅凭知道其他 investigation_id 就进入其完整上下文。

## 数据与兼容

新增 investigation_recall_links，保存来源关联、固定清单、哈希、修订和启用状态。原始报告、证据、产物和工作流 Outbox 继续使用原表，无第二套 Agent 运行时。

SQLite 自动升级到版本 20；MySQL 新增 016_investigation_recall_links.sql，运行时最低要求版本 016。历史迁移和 contracts/v1、contracts/v2 冻结契约未修改。

NativeMemory 检查点升级为版本 5，当前读取器兼容 1/2/3/4/5。API、Worker 和共享代码应一同升级。旧版本不理解新检查点和关联命令，回退前需停止新接入并保留关联、报告和检查点数据。

## 验证与交付

新增 tests/test_cross_task_recall.py，共 18 项离线测试，覆盖报告到原文的链式读取、未引用文件拒绝、同项目显式关联、跨租户/项目隔离、事务回滚、重复创建不恢复撤销、固定报告版本、损坏/失效来源、缓存和检查点撤销、续查关联、推理中途撤销、UTF-8 与哈希、检查点 4/5、HTTP 权限、跨空页搜索、草稿恢复及来源数量上限。

最终完整回归：**620 passed、28 skipped**。Ruff、JavaScript 语法、冻结契约、Wheel 构建与隔离导入通过。跳过项不计为通过。

Microsoft Edge 隔离场景验证了：历史搜索与 HTML 转义、创建前选择、事务关联、报告→引用目录→原文、独立任务隔离和撤销；已检查桌面及移动端截图。历史资料为本地测试夹具，工作流启动关闭，没有调用真实模型、游戏平台或沙箱。

本轮未重启用户原服务、安装依赖或执行真实 MySQL/Temporal 联调。离线与浏览器通过不代表调查质量或模型成本已有改善。

交付记录：

- 修改前备份：data/local/harness-phase8-before-532c9d98.zip；
- 源码差异：data/local/harness-phase8-source-20261102.patch；
- 最终回归：data/local/harness-phase8-full-tests-20261102.xml；
- 浏览器检查：data/local/harness-phase8-browser-20261102/result.json；
- 检查记录：data/local/harness-phase8-validation-20261102.json；
- Wheel：data/local/harness-phase8-dist-20261102/game_test_agent-0.1.0-py3-none-any.whl。

## 尚待实施

当前链路覆盖显式关联的报告与引用原文。语义归属 Router、跨任务整体汇总、其他资源订阅、媒体与流式召回、跨进程 Worker 租约、真实服务回放，以及相同案例的实际模型质量/成本对照仍需继续。
