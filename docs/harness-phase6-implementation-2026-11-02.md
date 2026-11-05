# Harness 第六批实现：会话多任务与轻量协调入口

日期：2026-11-02。接续[第五批后台代码作业](harness-phase5-implementation-2026-11-02.md)，开始实现[渐进方案](super-agent-migration-plan-2026-11-02.md)的 P3。本批交付任务归属、卡片和显式路由，不表示整个 Router 或跨任务记忆已经完成。

## 解决的问题

原工作台把一条会话等同于一条调查续查链，只允许向链上的最新调查发送消息。若把独立任务都接到 supersedes 关系上，目标、预算和历史引用容易混在一起；若另建会话，又缺少同一会话下的统一任务入口。

现在，同一会话能包含多条独立的 Worker 调查链。续查继续使用原有 supersedes；独立任务新增 conversation_task_links 关联，不伪造续查关系。task_id 使用该任务首份 investigation_id，续查后保持稳定，当前 investigation_id 则指向本任务的最新调查。

技术调查仍由原有 Agent 自动选择动作、比较候选、判断证据和停止条件。用户选择的是当前消息讨论哪个任务；没有新增“人工决定先调查谁”的调度步骤，也没有启动额外 Router 模型。

## 实现与复用

新增 events/coordinator.py，复用 WorkbenchStore 的调查链、原有调查创建/授权、预算、报告、Outbox 和工作台 API：

1. **确定归属**：带 investigation_id 的消息在授权项目内查找所属任务，并核对会话关联。已有身份可以直接确定路由，不重新调用模型。
2. **独立执行**：新增任务建立自己的调查、预算、授权和待启动记录。后端不从兄弟任务复制约束、授权、用户输入、证据、检查点或模型消息。工作台新任务沿用当前界面显示的测试条件，发送的是完整新请求，可在发送前修改。
3. **保持连续性**：追问已结束的任务仍走既有 reopen 与历史报告引用；root_objective 来自该任务的根调查。消息检查本任务的最新调查，兄弟任务有新进展不会造成错误的 stale-head 拒绝。
4. **原子提交**：新调查、授权、关联、接受事件与 workflow_start_outbox 在同一事务写入。调度失败后可按原请求重试；既有 Outbox 可恢复启动，不额外创建一套调度账本。
5. **重试隔离**：创建任务的幂等身份包含会话和当前 principal；相同请求返回原任务，变更内容或试图重新绑定其他会话会被拒绝。已归档会话禁止新任务，但允许查询已提交请求的幂等回执。

本批借鉴 Super-agent 的 Router/Worker 职责边界和按引用读取机制，新增代码使用现有 Python 模块，没有引入上游 TypeScript 运行时或其他依赖。

## Router 的轻量上下文边界

任务卡每页最多 20 张，包含任务 ID、当前调查 ID、状态、结果状态 resolution、调查次数、更新时间、事件游标，以及可选报告引用。目标摘录最多 600 UTF-8 字节，报告摘要最多 800 字节；截断有显式标记，更多任务分页获取。

卡片不包含完整请求、constraints、用户补充、工具日志、代码、证据原文或 Worker 检查点。报告卡保留 revision、SHA-256、受鉴权的读取 URL，并标注为 Worker 报告，不能经协调层转述就当成独立核验。详细报告和调查详情继续通过既有、受项目鉴权的 API 按需读取。

这是卡片 Inline、详细记录留在既有存储中的第一步。当前没有向 Worker 开放兄弟任务的原文读取工具，也没有项目范围的自动记忆发现或完整 Chain of Recall。授权语义沿用项目内工作台共享访问，不新增私人会话 ACL。

会话 SSE 版本号覆盖关联任务的事件游标，兄弟任务的变化会触发刷新。版本查询只取每份调查的最大游标，不载入事件载荷。它是合并刷新通知，不是设计文档中保留失败/恢复边界的持久事件批处理，不能混为一谈。

## 工作台与接口

工作台新增“当前任务”选择和“新增独立任务”，分别显示任务的调查历史。任务间草稿、新任务草稿分开保存；切换和刷新后恢复所选任务。旧的单任务会话继续可用。历史列表把关联任务归在原会话下；搜索能命中其目标及续查请求。侧栏状态和调查次数仍展示会话锚点的续查链，完整任务状态以任务选择列表为准。

接口如下：

| 接口 | 用途 |
| --- | --- |
| GET /v1/conversations/{id}/tasks?offset=0&limit=20 | 有界任务卡，next_offset 分页 |
| POST /v1/conversations/{id}/tasks | 完整 InvestigationRequest + Idempotency-Key 创建独立任务 |
| GET /v1/conversations/{id}/workspace?task_id={task_id} | 仅加载所选任务的续查历史；省略时保持旧的锚点行为 |
| POST /v1/conversations/{id}/messages | 沿用 expected_investigation_id，自动确定其所属任务 |

创建和续查继续走 Local/Temporal 既有启动接口，没有修改 Temporal 工作流定义或版本 marker。

## 数据迁移与兼容

SQLite 自动迁移至版本 18；MySQL 新增 014_conversation_task_links.sql，运行时要求至少版本 014。没有修改历史迁移或 contracts/v1、contracts/v2 冻结契约。Wheel 打包检查包含新迁移和协调模块。

旧数据无需回填：会话锚点隐式作为第一条任务，新增独立任务才写关联表。回退前须停止新写入并保留关联数据；旧工作台不理解该表，会把独立任务重新列成分散会话，也不支持新的定向续聊行为。

## 验证

新增 tests/test_conversation_tasks.py，共 12 项离线测试，覆盖事务失败回滚、重启后归属与权限隔离、跨会话幂等冲突、卡片截断和分页、报告引用、兄弟任务事件通知、租户/项目隔离、定向输入、任务内续查和陈旧请求、调度失败后的重试、归档及版本 17 升级。

最终完整回归：**583 passed、28 skipped**。跳过项不计为通过。全项目 Ruff、JavaScript 语法、冻结契约、Wheel 构建和隔离导入检查通过。

另使用已安装的 Microsoft Edge，通过隔离数据库和临时本地规则模式 API 完成真实浏览器检查：新建独立任务、切换历史、分别保存草稿、定向续查、刷新恢复与无 JavaScript 异常；已查看桌面和移动端截图。该检查没有接入真实模型、游戏平台或沙箱，也不能证明调查质量提高。

本轮没有重启用户原服务、安装新依赖或运行 MySQL/Temporal 实例；MySQL 实际迁移与多进程协作尚未验收。

交付记录：

- 修改前备份：data/local/harness-phase6-before-e3132eb8.zip；
- 源码差异：data/local/harness-phase6-source-20261102.patch；
- 最终测试：data/local/harness-phase6-full-tests-20261102.xml；
- 浏览器检查：data/local/harness-phase6-browser-20261102/result.json；
- 检查记录：data/local/harness-phase6-validation-20261102.json；
- Wheel：data/local/harness-phase6-dist-20261102/game_test_agent-0.1.0-py3-none-any.whl。

## 尚待实现

任务订阅、持久事件批处理、模糊输入的有界归属判断、跨任务发现与链式召回、完整会话级汇总和跨进程 Worker 所有权仍未实现。通用 MCP 长操作拆分、真实 Temporal 回放以及同案例的实际模型质量/成本对照仍需继续；不能据本批离线结果宣称完整 Super-agent 架构已完成。
