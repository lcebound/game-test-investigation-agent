# 第十二批：Worker 退回超范围补充与协调层重新归属

日期：2026-11-03。延续[第十一批消息归属](harness-phase11-implementation-2026-11-03.md)，补齐“已经投递，但 Worker 判断不属于本任务”的处理路径。保留现有 Python 执行器、自动取证判断和 Outbox，没有引入另一套运行时。

## 参考的 Super-agent 源码

固定审查版本仍为 deepseek-ai/dsh-superagent-prototype@6337fc823ce20251c8de8085948165c81aaa701e。

| 上游位置 | 借鉴的约束 | 本项目实现 |
| --- | --- | --- |
| [Evals.handback](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/core/evals.ts) | 只有收到过消息的 Worker 能退回，每个任务只退回一次 | agent_return_input 只接受当前调查收到的用户 input_id；原任务目标、平台通知和其他任务输入不能冒充补充退回 |
| [EnginePart.handBack](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/core/part.ts) | 保留原请求与退回原因，再交协调层处理，不撤销任务的其他订阅 | task_input_returns 保存原始输入身份、哈希、原因和状态；调查关系及运行订阅保持原样 |
| [followupOwners](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/core/model.ts) | 已退回的任务不再把该消息当作自己负责的工作 | 有效输入查询排除退回消息；原文仍在审计账本与工作台历史中 |
| [Engine.attach 的退回检查](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/core/engine.ts) | 不能把同一消息再次挂回已退回它的任务 | 转交沿用同一个 trigger_id，检查整条退回链；任务续查后的新 investigation_id 也不能绕过原任务的排除记录 |
| [Executor 的退回工具](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/app/tools/executor.ts) | Worker 说明哪里超范围，不改写原文或选择新授权 | 工具只有 input_id 和最多 300 字原因，不能填写新消息、目标任务、预算、权限或新任务方案 |
| [candidateCard](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/app/links.ts) | 协调层只接收短卡、身份和退回历史 | 待处理区分页提供短预览和原因；原文按需读取，Worker 判断明确标成未独立核实 |
| [Effects](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/app/effects.ts) | 记录状态与投递副作用有事务边界，重试不能丢工作 | 退回记录、原输入退出有效集、授权提案失效以及明确转交的输入 Outbox 同事务提交 |

这是一套基于当前账本的 Python 行为实现，没有复制或逐行翻译上游 TypeScript 运行时。上游第二次退回会报错，本项目对同一个 input_id 和相同原因提供幂等回执，以适配已有 WorkerStep 重试；换原因再次退回则拒绝。

## 退回后发生什么

Worker 探索阶段新增 agent_return_input。它仅处理用户补充，不能退回调查最初的目标。正常的同题纠正、约束变化和反证应继续在原任务内处理，不能以退回代替调查。

退回成功后：

1. 原文、原始命令和已发生的事件保留；原输入退出本任务的有效要求集合。
2. Worker 丢弃旧的相关对话调用组、输入缓存和读取覆盖，重新组装有效补充；原调查继续原有目标。
3. 同一模型响应中退回之后的其余调用全部返回“输入上下文已变化，未执行”，包括读取、代码、计划和报告，下一轮再判断。
4. 协调层只按用户原文里的明确任务地址重新解析，不把 Worker 的原因当成新的用户指令。
5. 能唯一指向另一项仍在进行、未过期的任务时，原文通过现有 Outbox 自动投递；否则保留为待重新归属。

相似主题、只剩一个其他任务或 Worker 认为“应该属于某任务”，都不足以自动转交。不会自动启动已完成、失败、取消中、已取消或已超过墙钟预算的任务，也不会根据退回原因创建新调查。

新建独立任务仍需要明确的用户入口与测试条件；本批未把完整语义 Router 接进来。界面提示可以先新增独立任务，再把原消息转交过去。

## 防止循环、重复和原文丢失

每条转交输入保留同一个原始 trigger_id、原始调查及命令身份和内容 SHA-256。接收方再次退回时沿用这些身份，不把转交当作全新的用户授权。

原消息不能转回退回过它的任何任务，包含该任务后续的调查轮次。用户发出的新消息拥有新的身份，仍可明确纠正或补充原任务；排除规则不构成对用户未来意愿的永久限制。

接收方的输入命令 ID 由原请求与目标任务身份确定。数据库约束和决策修订使同一转交只输入一次；回执丢失后重试相同决定返回原结果。遇到预先存在的命令 ID，不冒认它是本次原消息。

候选生成之后目标变成停止、任务头变化或不能访问时，消息留在待处理区。数据库写入失败则整个事务回滚，原输入仍属于原 Worker，不能留下“原任务不再负责、目标也没收到”的中间状态。

Outbox 分发器在实际发信前再次检查退回状态，避免已取出的旧输入队列快照继续唤醒原任务。已经送达的旧信号不被当作删除历史的理由，Worker 仍从有效输入账本取材料。

## 上下文与执行边界

Repository.list_user_inputs 默认返回有效输入；审计和工作台显式使用 include_returned=True。原始命令不删除、不改写。旧检查点恢复后，退回输入不会重新进入有效输入目录或留下假的“尚未召回”缺口；其他补充继续保留。

原有计划与取证账本不会被删除。涉及已退回输入的答案仍需要 Worker 按当前有效输入复核；计划状态和 Worker 退回原因都不是独立验证结论。本调查结束时，如仍有待归属补充，报告会明确说明它们未在本调查作答。

退回工具只在有有效补充的探索阶段展示。收尾阶段仍只允许更新计划与提交报告，继续保留原有小窗口、有限报告和独立收尾预算保障；到收尾时尚未处理的范围不能假装已经完成。

退回会取消原任务尚未决定的代码权限提案。已批准但尚未执行的旧提案也会因输入变化而失效，等待与恢复两条代码路径都重新检查。已经执行的作业不会被虚构为已撤销，原有清理义务保持。

转交不更改接收方的调查预算、构建、权限或授权主体；只传用户原文，不传原 Worker 的内部对话、证据或代码。

## 工作台

会话任务栏新增“退回消息”及待处理数量；全会话总览也将它计为待关注事项。即使原任务已完成，未归属消息仍可见。

待处理区可以分页查看短卡、按需打开原文、选择仍在进行的接收任务，或输入明确的当前调查编号。已停止和退回过该消息的任务不会出现在候选列表里，服务端仍会重新校验。

“不再处理这条补充”只关闭该条待归属事项，不停止原调查。原聊天中的补充继续显示，并分别标记“已退回，等待重新归属”“已转交其他任务”或“已退回，不再处理”。原因按 Worker 判断显示，HTML 以文字渲染。

切换身份或任务会清空旧窗口，过期异步响应不能重新填充新会话。没有变化的列表不重绘正在选择的表单。

## 接口与迁移

新增：

- GET /v1/conversations/{id}/input-returns?offset=0&limit=10：待归属短卡、总数和分页；
- GET /v1/conversations/{id}/input-returns/{return_id}：授权会话内的原文与排除任务；
- POST /v1/conversations/{id}/input-returns/{return_id}/resolve：expected_revision，加 attach/目标调查编号或 dismiss。

接口沿用原有租户、项目和会话鉴权。模型不能通过该接口增加权限，实际接收目标必须是本会话的有效任务头。归档后不接受新的转交或关闭决定，旧决定重试仍按既有回执返回。

待处理卡每页最多 10 项，卡片材料最多 15,000 UTF-8 字节；预览与原因各最多 240 字节。完整原文只在详情请求中读取，最多沿用 InputCommand 的 20,000 字符限制。原始记录损坏时仍显示不可核验卡，禁止转交，不让它遮住后续队列。

SQLite 新增迁移 21，MySQL 新增 017_task_input_returns.sql；MySQL 最低版本检查同步提高到 017。NativeMemory 检查点仍为版本 5，读取 1–5，不新增模型依赖，不改冻结契约。

旧二进制不会识别退回状态，不应与本版本混跑同一调查；原有单执行所有者约束仍在。真实 MySQL/Temporal 引擎联调和跨进程所有权机制不在本批完成范围内。

## 验证与交付

新增 tests/test_input_returns.py，31 项离线测试覆盖到达关系、幂等、明确转交、无歧义门槛、终态与过期目标、原文来源链、防回环、事务失败、目标状态变化、命令身份碰撞、审批失效、旧检查点恢复、损坏记录、队列快照、分页、API 作用域及真实控制器的调用组截断。既有 SQLite 升级和 MySQL 迁移清单测试同步到新版本。

隔离 Edge 浏览器验证了：原调查完成后的待归属提示、原文审计保留、明确地址自动转交、HTML 转义、停止与拒收任务过滤、转交后的权限不变、丢失回执后重试、关闭事项不停止 Worker、SSE 数量更新、身份切换后的过期回执丢弃；检查了桌面与移动端截图。

最终完整回归：**720 passed、28 skipped**，耗时 141.40 秒。Ruff、JavaScript 语法、冻结契约、Wheel 构建及隔离导入通过。跳过项不计为通过。

源码差异、迁移和上游快照审查记录见 data/local/harness-phase12-validation-20261103.json。未调用真实模型、游戏、平台或 PSEC，未主动重启用户现有服务。离线协议测试不能证明模型一定能正确判断“超范围”，同题质量与成本评测仍需另行完成。

交付产物：

- 修改前备份：data/local/harness-phase12-before-f79cff7c.zip；
- 源码差异：data/local/harness-phase12-source-20261103.patch；
- 完整回归：data/local/harness-phase12-full-tests-20261103.xml；
- 浏览器检查：data/local/harness-phase12-browser-20261103/result.json；
- Wheel：data/local/harness-phase12-dist-20261103/game_test_agent-0.1.0-py3-none-any.whl。

完整模型语义路由、跨任务技术归纳、通用未归属事件的模型评估批处理、跨进程 Worker 所有权以及真实服务回放仍待实施。
