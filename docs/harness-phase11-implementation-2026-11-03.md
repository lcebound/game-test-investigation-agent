# 第十一批：参考 Super-agent 的消息归属与投递边界

日期：2026-11-03。基于[渐进改造方案](super-agent-migration-plan-2026-11-02.md)，接入一条可用的“解析消息归属 → 保存决定 → 原接口投递”路径。本批是规则路由，完整模型语义 Router 仍未实现。

## 对照了哪些公开代码

继续使用已审查的 DeepSeek 公开实现固定版本：deepseek-ai/dsh-superagent-prototype@6337fc823ce20251c8de8085948165c81aaa701e。

| 上游源码 | 采用的行为 | 本项目落点 |
| --- | --- | --- |
| [Intake.receive](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/core/intake.ts) | 已有归属的补充直达原任务，不重新分配调查策略 | 普通补充保留当前任务；平台通知继续使用已有运行订阅 |
| [EnginePart.leftToWorkers](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/core/part.ts) | Worker 判断自己负责的问题，Router 不代做技术调查 | message_router 只返回任务身份、新任务目标或归属澄清，不改候选取证和问题账本 |
| [Orchestrator 提示](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/prompts/orchestrator.md) | 明确引用才能挂到旧任务；主题相似不构成挂接依据；保留原始请求，不改写成执行方案 | 仅识别句首的任务编号、明确任务名称和新任务指令；不按整段内容相似度自动串任务 |
| [Evals.start/decide](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/core/evals.ts) | 请求身份与路由结果分离；同一请求不能反复创建工作 | 沿用消息收据表，保存原文和固定归属；网络重试保持同一目标与幂等键 |
| [Effects](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/app/effects.ts) | 状态先落盘，副作用随后执行，执行阶段重新检查 | 路由只落收据，调查启动和输入投递继续复用已有事务、Outbox、授权及陈旧任务头检查 |

这些是行为与边界的借鉴。没有逐行翻译 TypeScript、复制上游运行时或新建第二套数据库队列；Python 实现复用当前 WorkbenchStore、调查关系、消息收据和工作流入口。上游完整 Evals 批处理、评估超时、Worker handback、Mandate 模型等没有因此成为本项目已完成能力。

## 用户能怎样使用

工作台任务栏新增“按内容识别任务”，默认开启；关闭后沿用原来的定向发送。明确点击“新增独立任务”时继续直接创建，不经过归属判断。

支持的表达包括：

- “继续”：交给当前正在查看的任务；
- “继续「GPU卡顿」：补充低画质对照”：按该任务最初的目标名称定位；
- “关于 GPU卡顿：补充低画质对照”或“继续任务 GPU卡顿：……”：同样按名称定位；
- “继续 inv-具体编号：补充”：定位当前会话内的任务，沿它自己的续查链找到任务头；
- “新任务：调查登录崩溃”：在当前会话创建独立调查，使用界面显示的测试条件与预算。

名称必须有至少两个非空白字符，并匹配原始目标的有界短表示。常见指代如“刚才那个掉帧问题，换个思路看看”尚没有模型语义识别；本批不宣称支持任意自然语言。没有被识别为明确地址的普通补充继续属于当前任务，由原 Worker 决定其调查含义。

同名、短名称命中多项、未知名称、不完整引号或没有默认任务时，返回归属澄清，不先把消息投递给某一项。页面显示有界任务选择，也允许返回编辑，未发送草稿保留。跨任务投递使用目标任务自己的条件，当前任务的预算、构建和约束不会套过去。

本批没有加入自然语言停止、批量广播或自动授权；停止和授权仍使用原有明确入口。工作台与 API 应一起更新。

## Router 保持轻量

新增 events/message_router.py，只使用当前消息、会话归属、任务身份、原始目标短表示和任务头状态。任务目录最多检查 20 项，每个目标最多 240 UTF-8 字节；不会载入 Worker 日志、报告正文、证据原文、代码或检查点。

超过目录上限时，不把已查看部分的唯一命中当作全会话唯一命中；要求更明确的编号或选择。已知任务编号和当前任务归属可直接解析，不依赖这 20 张目录卡。

任务名称使用原始目标，而不使用后续补充中顺带提到的其他主题。例如 CPU 任务后来有一轮目标是“和 GPU 问题比较”，它不会因此获得“GPU”这个任务名称。

“这个任务也许与 CPU 有关”这样的正文不会触发跨任务挂接。Router 不建立取证计划、提高预算或扩大权限，原有自动候选选择保持不变。

## 原文、幂等与状态变化

新增 POST /v1/conversations/{id}/messages/route，要求 Idempotency-Key，接受 content 和可选 context_investigation_id。

接口按原有租户、项目及会话关系鉴权。收据键同时包含会话、用户和请求键，返回 task、new_task 或 clarify。内部把规范化后的原始请求与 route_decision 分开保存，trigger_id 与 route_id 对应同一请求身份；除去输入两端空白，不用 Router 生成的执行方案替换用户正文。

归属判断和收据保存位于同一个数据库事务内，复用 workbench_message_receipts，不新增迁移。接口本身不输入消息、创建调查、启动 Worker 或执行平台动作。它不是无写入查询：会保存一条归属收据。

前端取得决定后，使用既有 /messages 或 /tasks 入口实际投递。重试复用原请求键与已固定的目标；输入与新调查的幂等保护仍来自现有账本。选择性测试还验证了“服务端已收到消息，但浏览器丢失回执”的情况，重试没有再次输入或创建。

路由结果不是授权凭证。任务头已变化时，旧决定仍指向旧任务头，实际发送返回冲突，不能悄悄改投新一轮；刷新后用新的发送请求重新解析。会话在路由后被归档时，实际投递依旧拒绝。身份或会话切换后，旧的异步归属响应不能继续触发发送或重新填充澄清窗口。

任务选择、调查详情和补充信息随实际目的地切换。澄清中的标题使用 textContent，任务文本中的 HTML 不执行。

## 与已有批处理机制的关系

本批没有把每条运行日志送去重新研判。第七批的运行订阅、去重、不可变反馈批次和唤醒保持使用，明确归属的外部事件直达原 Worker。Worker 仍通过已有输入账本在下一轮吸收补充。

上游 Passes/Effects 的完整通用事件泵没有另行移植。当前“用户消息归属”和“平台反馈批处理”共用现有调查与 Outbox 边界，尚不是一个支持所有来源、模型评估和退回请求的通用 Router。

## 验证和兼容

新增 tests/test_message_routing.py，29 项离线用例覆盖：明确名称、编号与普通追问、新任务识别、歧义、未知目标、不完整语法、陈旧任务头、目录溢出、任务原始名称、跨项目/租户、无 Worker 明细读取、归属收据重试、原文保存、新任务幂等、归档后的投递拒绝，以及相似主题不跨任务挂接。

Microsoft Edge 隔离浏览器验证了跨任务输入、创建独立任务、丢失回执后重试一次、歧义后的明确选择、草稿保留、HTML 转义、身份变化后的回执丢弃；检查了桌面及移动端截图。工作流启动被测试替身禁用，没有调用真实模型、游戏、平台或沙箱。

SQLite 仍为版本 20、MySQL 最低迁移仍为 016、NativeMemory 检查点仍为 5。没有新增依赖，没有修改冻结契约。没有主动重启现有服务。

最终完整回归：**689 passed、28 skipped**，耗时 141.13 秒。Ruff、JavaScript 语法、冻结契约、Wheel 构建及隔离导入通过；本批变更文档的 59 个本地链接检查通过。

文件校验与上游快照审查记录见 data/local/harness-phase11-validation-20261103.json。环境跳过项不计为通过；真实 MySQL/Temporal 联调、模型语义归属、Worker 退回消息、跨任务技术归纳、同题质量/成本评测和跨进程 Worker 所有权仍待完成。

交付产物：

- 修改前备份：data/local/harness-phase11-before-5e797d0f.zip；
- 源码差异：data/local/harness-phase11-source-20261103.patch；
- 完整回归：data/local/harness-phase11-full-tests-20261103.xml；
- 浏览器检查：data/local/harness-phase11-browser-20261103/result.json；
- Wheel：data/local/harness-phase11-dist-20261103/game_test_agent-0.1.0-py3-none-any.whl。
