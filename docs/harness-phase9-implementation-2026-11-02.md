# Harness 第九批实现：全会话状态与待关注事项总览

日期：2026-11-02。接续[第八批跨任务召回](harness-phase8-implementation-2026-11-02.md)，补上轻量协调层的全会话状态投影与统一交互。本批不引入模型 Router，继续使用现有任务归属和调查判断逻辑。

## 修复的实际问题

原工作台已支持独立任务，兄弟任务事件也能触发 SSE 刷新，但历史侧栏的状态、调查次数和更新时间仍主要来自会话锚点。因此，锚点已完成时，其他任务仍在等待输入、执行授权或清理资源，也容易被忽略。

现在会话统计覆盖全部关联任务，状态只取每个任务自己的最新调查；较早调查尚未清理的资源则继续累计到所属任务。任务内部的取证顺序、动作选择、权限、预算和结论规则保持原实现。

## 只读状态投影

新增 storage/conversation_projection.py，复用 investigation lineage、conversation_task_links、报告、执行授权和 cleanup_operations，通过 SQL 组装任务头与会话计数。没有新增状态表、迁移、缓存真相源或执行器。

task_summary 包括任务/调查总数、进行中、等待输入、已完成、失败、取消、目标未达成、证据不足、有效待授权、待清理资源、缺少报告及待关注任务数量。

其中：

- 续查后只按新任务头计入当前失败/完成状态，旧失败不会重复计数；旧清理义务继续显示。
- 执行失败与 goal_failed 分开显示，不将目标未达成推断成平台缺陷。
- 同一个任务可以同时有多个问题，attention_tasks 按任务去重。
- 已结束任务若仍有资源待清理，all_terminal 可以为真，同时 state=needs_attention。
- completed 表示流程已完成，证据不足单独计数；总览界面使用“已结束”，不据此表示目标成功。
- 缺少报告的终态任务显示为待关注，不伪造报告或结果。

有效待授权仅包含当前任务头上仍为 pending、尚未过期、未被更新用户输入取代的提案；取消中及终态调查不再计入。该投影不读取代码请求正文，也不调用有过期写入行为的 CodePermissionStore.get，不修改授权状态。

## 轻量总览与结果卡

ConversationCoordinator 新增 overview，分别返回待关注事项和最近结果。结果列表使用每个任务头已结束后的最新报告；更多历史报告继续留在任务续查链中。

每类最多 10 张卡，每类卡片材料最多 6 KiB，完整响应最多 16 KiB。目标与报告摘要按 UTF-8 字节截断，达到卡片数或字节上限后使用独立的 next_attention_offset、next_result_offset 分页。

卡片只包含任务/调查身份、状态、计数、原因、报告摘要与哈希、清理来源引用。代码、用户补充、完整日志、证据原文和 Worker 检查点不进入总览。各报告摘要保留来源，不合并成新的技术结论。

待关注原因包括 waiting_input、permission、cleanup、execution_failed、goal_failed、missing_report。页面分别展示业务含义，并说明待关注项可能属于进行中或已结束任务。

## 历史列表与 SSE

会话历史现在使用全会话的调查次数和最后活动时间，兄弟任务的新事件或清理变化能使原会话出现在正确位置。latest_investigation 继续保留锚点任务头；新增 anchor_investigation_count 保存锚点续查次数，顶层 investigation_count 改为全会话总数。

SSE 新增 task_summary 和 status_scope。旧 status 字段仍表示锚点调查状态，明确标记 status_scope=anchor_investigation，客户端应使用 task_summary 展示全会话状态。

版本号纳入作用域内事件游标、会话元数据修订、汇总计数和活动时间。即使清理台账变化或授权过期没有新增调查事件，也能触发刷新；其他会话或项目的事件不会污染当前会话版本。version 是不透明值，客户端只需比较并回传 after。

单会话查询先限定锚点及关联任务，再展开续查链；事件活动只按每份调查最新游标查询时间，不载入事件载荷。全项目历史列表仍按当前授权项目计算。

这是活状态投影，不是新的事务检查点。进入任务后，以该任务的最新状态和现有操作校验为准。

## 工作台导航

新增“会话总览”，打开后按需加载短卡。侧栏和总览按钮使用全会话计数；正在查看的任务仍显示自己的状态。

点击卡片会进入对应任务。随后补充信息仍通过该任务头的 expected_investigation_id 发送，保留现有陈旧请求和幂等保护，不会因为会话锚点已完成就错误地续查锚点。

清理事项提供“查看清理调查”。若来源早于当前加载的历史页，前端只在点击后读取该份调查作为独立检查面板，不把未加载的历史伪装成完整聊天记录。SSE 更新时，该面板也会重新读取清理状态。工作台详情现在展示所有待清理资源，不再只显示 action 类型。

切换会话或身份会清空总览及额外检查状态；异步回执核对会话代次和身份，旧响应不能重新填入新会话。标题与摘要使用 textContent 展示，HTML 作为文字呈现。

总览读取和任务导航不启动调查、执行清理或批准代码。授权仍通过原有明确审批入口处理。

## 接口与兼容

新增：

GET /v1/conversations/{id}/overview?attention_offset=0&result_offset=0&limit=8

该接口沿用工作台的 tenant/project 鉴权；会话锚点及别名先解析到授权范围，再读取关联任务。每份 Worker 的完整资料继续使用既有独立接口，点击后按需获取。

历史接口及 conversation/workspace 的 conversation 摘要增加 task_summary、event_cursor、anchor_investigation_count。tasks 卡片增加有效待授权数、待清理资源数、来源调查及原因。SSE 的 status_scope 和 task_summary 均为新增字段。

没有修改 contracts/v1、contracts/v2，SQLite 仍为版本 20、MySQL 最低迁移仍为 016，NativeMemory 检查点仍为版本 5。Wheel 包含新的前端资源和投影模块。API 和前端应采用同一版本；旧客户端忽略新增汇总字段时，仍只能显示锚点状态。

## 验证与交付

新增 tests/test_conversation_overview.py，共 16 项离线测试。覆盖：锚点完成与兄弟等待、全会话排序、续查计数、旧清理义务、多原因去重、目标失败区分、无报告状态、过期/被取代/终态授权、只读性与私有载荷隔离、有界分页、结果排序、无事件清理刷新、跨租户/项目拒绝、SSE 兼容字段及重启重建。

最终完整回归：**636 passed、28 skipped**。Ruff、JavaScript 语法、冻结契约、Wheel 构建及隔离导入检查通过。跳过项不计为通过。

Microsoft Edge 隔离场景验证了四任务会话的统计、只读卡片、恶意 HTML 转义、补充信息路由、跨越当前历史页的旧清理调查、台账更新后的 SSE 刷新、身份切换后的过期回执丢弃，并检查了桌面及移动端截图。

浏览器使用本地台账夹具并关闭工作流启动；授权提案、清理义务和清理确认均为测试数据。没有执行生成代码、连接真实模型/游戏平台/沙箱或批准任何实际权限。未运行真实 MySQL/Temporal 联调，也未主动重启用户现有服务或安装依赖。

交付记录：

- 修改前备份：data/local/harness-phase9-before-1ba8ce91.zip；
- 源码差异：data/local/harness-phase9-source-20261102.patch；
- 最终回归：data/local/harness-phase9-full-tests-20261102.xml；
- 浏览器检查：data/local/harness-phase9-browser-20261102/result.json；
- 检查记录：data/local/harness-phase9-validation-20261102.json；
- Wheel：data/local/harness-phase9-dist-20261102/game_test_agent-0.1.0-py3-none-any.whl。

## 后续范围

本批补齐规则协调层的全会话状态入口。自然语言语义路由、跨任务技术归纳、其他资源订阅、流式/媒体召回、跨进程 Worker 所有权、真实服务回放和相同案例的模型质量/成本评测仍待实施。本批回归不能证明根因定位率或实际调查效果改善。
