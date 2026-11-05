# Harness 第七批实现：运行订阅与持久反馈批次

日期：2026-11-02。接续[第六批会话多任务](harness-phase6-implementation-2026-11-02.md)，继续实现[渐进方案](super-agent-migration-plan-2026-11-02.md)的 P3。交付范围是运行反馈的确定归属、有界批次、唤醒和当前调查内召回。

## 行为变化

平台反馈携带明确的 source 和 RunRef 时，可通过订阅直接找到相关调查。一次运行可以被多个明确绑定该运行的调查订阅；兄弟任务、其他运行和其他项目不会收到广播。

异常事件创建或补充调查时自动绑定该事件的来源运行。已有调查也可以通过 API 订阅已绑定的 source_run_ref 或已受理动作的运行；未绑定的任意运行会被拒绝。续查沿用相同 source_run_ref 时，仅继承父调查对该来源运行的有效订阅。改变来源运行后不复制旧订阅，独立任务保持独立。

新反馈只补充已有任务。未匹配的通知保留接收记录，不创建调查；已结束或正在取消的调查不接收新的投递。重复 source、tenant/project、event_id 返回原回执，修改同一身份的载荷会冲突。取消订阅停止后续接收，已接收的通知及原文引用保留供审计。

原有 Agent 的动作选择、判断规则、权限和预算继续生效。反馈不会改变用户目标、增加执行权限、重置截止时间或自动变成已核验证据。

## 新反馈协议与接入

新增 events/feedback.py 定义 game_test.run.feedback。它用于平台的运行通知，字段包括：

- source、tenant_id、project_id、event_id；
- 带时区的 occurred_at、published_at，以及 root_event_id、causation_id；
- origin=platform；
- data.run_ref、result_revision、kind、status、summary、details。

kind 支持 progress、failed、recovered、counterevidence、phase。原文最多 64 KiB；超过时应使用平台产物引用。RunRef 的项目必须匹配事件项目。Agent 自身输出不属于该协议，既有调查结果发布也不会订阅回这个入口。

来源需配置在现有 GTA_EVENT_SOURCE_POLICIES_JSON 的对应项目中。HTTP 接收还要求当前项目的 events:write 权限；订阅修改要求 runs:read。通知中的身份声明沿用既有受信任平台接入方式，部署方须控制该发布通道和事件写入权限。

| 接口或路由 | 用途 |
| --- | --- |
| POST /v1/events/run-feedback | 接收 RunFeedbackEvent，返回去重回执及投递目标 |
| RabbitMQ run.feedback.{platform_id} | Local/Temporal Bridge 的新增消费路由 |
| GET /v1/investigations/{id}/subscriptions | 分页查看运行订阅 |
| POST /v1/investigations/{id}/subscriptions | 提交 source、run_ref，启用已绑定运行的订阅 |
| DELETE /v1/investigations/{id}/subscriptions/{subscription_id} | 停止后续接收 |
| GET /v1/investigations/{id}/feedback?after=0&limit=10 | 查看通知引用，after 为游标 |
| GET /v1/investigations/{id}/feedback/{cursor}?byte_offset=0&max_bytes=4096 | 按 UTF-8 字节读取当前调查的原始通知 |

旧的 anomaly.detected 和 evidence.updated 协议继续保留。新增反馈类型位于事件模块，没有修改 contracts/v1、contracts/v2 冻结 Schema。

## 持久批次与发送边界

新增 storage/feedback.py，复用 Repository 事务及 workflow_command_outbox。原 event_inbox 仅能绑定一份调查，新协议需要记录多目标投递，因此增加四类记录：

1. task_subscriptions：明确的运行订阅；
2. task_feedback_events：保存一次原始通知及其规范化 SHA-256；
3. task_feedback_deliveries：每个接收调查的独立游标、输入版本和批次归属；
4. task_feedback_batches：不可变批次、generation 和有界卡片。

每批最多 20 条、8,192 UTF-8 字节；单次构建最多处理 100 条通知。数量、字节数或用户补充的输入指纹发生变化时切批。批次保存原 event_id、哈希和游标范围，details 留在原事件中。失败、恢复、反证、阶段变化保留各自事件身份，普通进度也可从引用目录回查。

批次封存后不再追加。发送中产生新通知时，后续构建生成新的 generation 和命令 ID；旧回执只确认旧命令。这避免了可变待办在 ACK 时误删新通知的问题，也让重启后能够继续发送同一批次。

取消命令先于批次构建和普通命令发送，即使批次构建失败也能先处理取消。原有取消 API 继续即时生效。若原文损坏，批次保留不可读引用并展示缺口，原文读取拒绝通过；单条损坏记录不会阻塞其他任务的正常反馈。

这些记录仍使用原数据库与现有投递器，不增加消息中间件、另一套 Worker 调度或模型 Router。

## 唤醒与 Worker 上下文

Local API 新增持久命令派发循环，未启用 MQ 时也能在重启后封存并投递已接收反馈。MQ 中断不会阻止 HTTP 接收和本地命令恢复。Local Bridge 及独立 Temporal Bridge 均接入新消费路由。

批次使用现有 evidence 命令唤醒原调查。Temporal 沿用 evidence_updated 信号及稳定 command_id 去重，没有修改工作流定义或历史 patch 分支；实际 Temporal 服务回放仍未验收。

原生 Worker 的工作记忆增加有界 external_run_feedback：从最近 20 批中选择最近的不可读通知、失败、恢复、反证、阶段信息，以及最新两条通知；卡片材料总预算 2,500 字节，连同目录和说明保持有限大小。预算容纳不下的卡片仍保存在原批次和引用目录。每条可见卡片标注是否早于当前用户输入版本。

模型通过现有 agent_read_context 读取 feedbacks 目录，再沿 feedback/{cursor} 获取原文。目录按游标分页，原文按 UTF-8 分片、可做字面搜索并核对哈希。引用仅在接收该事件的调查中有效，当前工具不会把兄弟任务的记录带进来。

通知明确标记为不可信外部数据。原文读取会扣既有工具读取预算，重复读取可复用缓存；新批次使旧目录缓存失效。通知不会登记到 EvidenceService，所以反馈引用无法直接充当报告证据引用。模型仍需通过原有取证与核验机制得出结论。

若模型推理期间出现新批次，旧响应中的工具组会被丢弃，下一步重新读取工作状态；已消耗的模型预算仍照常计入。分析容量等待也识别新批次，允许重新考虑是否仍需原诊断动作。其他已有长作业继续由自己的作业账本控制。

Typed 路径在既有 diagnostic_guidance 中获得同类短卡并沿用证据信号唤醒。原文读取 API 共用；本批没有为 Typed 路径新增专属模型读取工具。

工作台执行面板展示外部反馈和旧输入版本标记，并按需读取原始通知。使用 textContent 展示通知，HTML 只作为文字；损坏通知标记为不可读。

## 迁移、恢复与回退

SQLite 自动升级至版本 19；MySQL 新增 015_task_feedback.sql，当前运行时最低要求版本 015。历史迁移文件保持不变，Wheel 包含新迁移。

NativeMemory 检查点升级为版本 4，当前读取器兼容版本 1/2/3/4。旧数据没有反馈字段时使用默认空值，再从权威账本同步。API 和 Worker 应共同升级；旧代码不能读取版本 4，也不能投递新反馈命令。

新增表保留通知原文和订阅关系。回退前应停止新接入、处理待发反馈并保留这些表和检查点；不能仅替换控制器文件。跨进程 Worker 所有权及分布式容量保护仍是后续范围，本批继续依赖既有单一执行所有者要求。

## 验证与交付

新增 tests/test_task_feedback.py，共 19 项离线测试。覆盖来源策略、自身输出拒收、订阅归属、跨项目隔离、幂等冲突、突发合批、输入版本分界、旧 ACK 与新 generation、取消优先、重启恢复、UTF-8 原文与哈希、损坏原文、目录缓存、Worker 工具可见性、推理中途反证、续查继承以及检查点 3/4 兼容。

最终完整回归：**602 passed、28 skipped**。全项目 Ruff、JavaScript 语法、冻结契约检查、Wheel 构建及隔离导入通过。跳过项不计为通过。

另用 Microsoft Edge 和隔离数据库完成真实页面/API 检查：运行订阅、授权接收、SSE 与本地派发、失败/恢复/反证展示、原文按需读取、重复接收、兄弟任务隔离及恶意 HTML 转义。已检查桌面与移动端截图。该浏览器场景关闭了工作流启动，没有调用真实模型、游戏平台、沙箱、RabbitMQ 或 Temporal。

本轮未重启用户现有服务、安装依赖或执行实际 MySQL 迁移；离线控制流与界面通过不表示实际调查准确率或推理成本已改善。

交付记录：

- 修改前备份：data/local/harness-phase7-before-d983d8ba.zip；
- 源码差异：data/local/harness-phase7-source-20261102.patch；
- 最终回归：data/local/harness-phase7-full-tests-20261102.xml；
- 浏览器检查：data/local/harness-phase7-browser-20261102/result.json；
- 检查记录：data/local/harness-phase7-validation-20261102.json；
- Wheel：data/local/harness-phase7-dist-20261102/game_test_agent-0.1.0-py3-none-any.whl。

## 后续范围

本批完成运行反馈的确定性协调。operation/外部线程订阅、跨任务资源发现与完整 Chain of Recall、语义归属 Router、会话级完整汇总、通用 MCP 长操作拆分、跨进程租约，以及真实案例质量/成本评测仍待实现。
