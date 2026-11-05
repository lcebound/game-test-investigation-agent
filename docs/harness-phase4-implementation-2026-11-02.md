# Harness 第四批实现：单步 Worker、让出槽位与恢复协议

日期：2026-11-02。接续[第三批问题账本](harness-phase3-implementation-2026-11-02.md)，实现[渐进方案](super-agent-migration-plan-2026-11-02.md)中 P2 的执行接口基础。

本批为现有原生调查循环增加单轮 Worker 接口，让 Local 与新版本 Temporal 工作流共用执行与返回语义。继续使用现有模型、取证判断、Evidence、检查点、权限请求和预算账本，不增加人工调度步骤，也不提高并发上限。

## 原来的问题与本批行为

| 场景 | 修改前 | 本批 |
| --- | --- | --- |
| Local 调查执行 | 一个任务持有调查槽位直到整题结束 | 每个模型轮次结束后返回队尾，让其他就绪任务获得槽位 |
| 代码请求等待已有权限决定 | 执行协程持续等待，持有调查/分析槽位 | 返回 waiting，保存调用组和检查点，释放槽位 |
| Local / Temporal 返回值 | Local 跑完整题，Temporal 通过 max_model_turns=1 与调查状态推断下一步 | 共用 WorkerStepResult；Temporal 保留旧活动兼容分支 |
| Activity 已完成但确认丢失 | 重试可能推进到下一个模型轮次 | 稳定步骤身份查回结果；开始标记结合已消费轮次处理确认前中断 |
| 权限等待期间有新追问 | 旧提议可能继续等待并恢复执行 | 未启动的旧提议不再执行，反馈 superseded_input 后交回 Agent 判断 |

让出槽位发生在模型轮次边界，以及额外代码权限的等待期间。一个轮次内仍可能包含多个顺序工具调用；本批没有将所有 MCP 读取或运行中的代码任务拆成 submit/poll/collect，也不提供抢占式调度。

## 单步接口

新增内部类型位于 workflows/native_step.py：

- WorkerStepResult.state：continue、waiting、completed、failed、canceled；
- status：权威 InvestigationStatus；
- continuation：轻量执行位置与资源引用快照；
- wait_reason：permission、owner 或 cancellation；
- retry_after_seconds：重试建议，waiting 通常为 1 秒。

completed 仍只表示调查流程结束，不表示目标验收通过或根因确认。canceling 保持 waiting/cancellation，不能将尚未清理完成的任务直接呈现为 canceled；取消流程继续由原有后端处理。

调用示意：

```python
authorization = repository.get_authorization(investigation_id)
result = await controller.run_step(
    investigation_id,
    principal_id=authorization["principal_id"],
    scopes=set(authorization["scopes"]),
    step_id="owner-run/activity-id",
)
```

run_step 最多推进一个模型轮次，可能先恢复已有代码操作。原 run/max_model_turns 接口保留，直接调用旧接口的行为不变。新接口先检查所需作用域和冻结的执行模式；模型、工具与分析仍受现有资源准入和预算校验约束。

进程内的同一调查只能有一个活跃 run_step，其余调用返回 waiting/owner；步骤结束即回收该锁，避免长期积累。调用方不能通过提交旧 continuation 改写预算、权限或调查状态，新一步始终从权威账本恢复。

## ContinuationPacket

快照包含：

- investigation_id、调查 revision、next_turn、原始调查截止时间；
- 当前完整输入的指纹、计划版本；
- 最近检查点的 artifact 引用和版本；
- 已消耗的读取次数及模型 Token 账本值；
- 待决定权限请求的数量与最多 16 个引用 ID。

它不包含对话历史、完整用户补充、生成代码或原始工具结果。检查点引用仍校验项目范围。快照只是执行位置提示，不是权限凭证、实时状态或模型摘要；Activity 重试返回的旧快照可以落后于最新输入，新步骤必须重新读取账本。

现有版本 3 检查点负责恢复模型上下文、问题计划、摘录与输入读取范围；调用结果日志负责补回检查点前已完成的读取。本批没有增加 NativeMemory 字段或改变冻结契约。

## 确认丢失与中断

可选 step_id 表示一次调度请求，而不是模型自选任务 ID。新 Temporal Activity 使用 workflow_run_id:activity_id，重试沿用同一个身份，continue-as-new 后使用新的运行身份，避免 Activity 编号重置造成冲突。

执行前按该身份记录开始时的轮次位置；执行后将结果写入原有事件表。身份经过 SHA-256 生成专用事件类型，查找不受一般事件分页窗口影响；通用步骤事件与身份结果事件在同一事务中写入。

若进程在模型轮次已预留或检查点已提交、但步骤确认尚未写入时中断，同一身份的重试只返回当前执行位置，不会把下一轮当作本次请求继续执行。下一次调度从检查点及已完成调用日志恢复；未完成的工具批次不会被盲目重放。中断的模型尝试继续计入原有预算，不退还或重置。

这不承诺所有外部读取严格只执行一次。原有读取缓存有时效，未来读取可能需要重新查询；代码操作继续依赖原有持久化作业身份、操作绑定和清理机制。

## 权限等待与恢复

CodeExecutionService 新增可选 wait_for_permission=False，默认仍为原来的等待行为。原生 WorkerStep 使用非阻塞模式：

1. 完成原有代码、输入证据、权限范围和预算检查，保存不可变权限提议。
2. 权限尚未决定时返回 waiting_permission，不提交代码作业。
3. 完整闭合当前模型工具调用组并保存检查点，再返回 waiting。
4. Local 或 Temporal 在等待期间不调用模型、不重复扣读取次数，也不占用调查、模型或分析执行槽位。
5. 决定后从原提议恢复；批准继续走现有作业准入，拒绝/过期/取消返回 not_executed，让 Agent 使用现有证据形成有限结论。

恢复的代码分析仍受分析并发槽位控制。截止时间已经到达时，取消/收集不等待一个已过期的槽位；这与允许继续执行新代码不同。

新追问到达后，若未启动的权限提议早于该追问，旧提议返回 permission_superseded_input。未决定的请求被取消，已批准的决定保留审计记录，但不会据此执行旧方向代码；需要继续时由 Agent 重新提出适合当前输入的动作。输入证据、运行时或权限绑定已不匹配时，非阻塞恢复返回受控 stale_request 反馈，不回显底层异常内容。

这是沿用既有额外目录/网络权限要求，不增加新的审批环节。默认离线代码执行的权限边界不变。该失效检查针对等待中的、尚未建立作业的提议；运行中代码的中止和清理由现有取消流程负责。

## 两种后端

### Local

原生控制器走 run_step；其他控制器保持原路径。continue 返回后重新排到就绪队列末尾，waiting 按建议延迟重新入队，完成回调释放调查槽位再调度下一项。队列容量、调查并发和进程内其他资源上限不变。

关闭进程时，已返回 waiting 的权限提议保持持久化，重启可恢复；显式取消调查则取消待决定权限，并执行原有取消和报告流程。等待期间的新追问或权限决定通常在下一次一秒轮询中被处理，目前不是完全事件驱动的唤醒机制。

### Temporal

新工作流通过 native-worker-step-v1 patch 选择新 Activity 参数与等待处理；旧历史保持原有活动参数和控制语义。CoreActivities.native_investigation_step 增加可选 worker_steps 参数，默认 False 兼容旧活动调用，新工作流显式传 True。

新结果保留 done/status，增加 worker_step。waiting 不消耗工作流的模型轮次界限，输入/证据信号或定时器可以唤醒等待，取消仍优先走原有清理路径；累计等待按原有 continue-as-new 方式截断历史。实际轮次、读取预算与截止时间仍以数据库账本为准，不因 continue-as-new 重置。

本批没有增加跨进程租约或分布式抢占。同一调查仍应只有一个执行后端/所有者，不能同时让 Local 与 Temporal 执行它；进程内锁不能替代跨进程 fencing。

## 验证与交付

新增 tests/test_native_step.py，共 17 项离线测试，覆盖单步续跑、确认丢失、中断后相同身份不推进下一轮、轻量快照与鉴权、同调查互斥、Local 轮次与等待公平性、权限拒绝、精确批准后的准入、新输入使旧提议失效、真实原生工具调用组闭合、无预算消耗的等待、关闭恢复、等待期间取消、Temporal 等待轮次和运行身份，以及终态不再调用模型。

批准路径使用替身在现有作业准入处停止，不执行实际代码；Windows/PSEC 的真实执行验收仍依赖原有环境测试。Temporal 调度分支使用离线替身验证，未连接真实 Temporal 服务或进行真实历史回放。

完整离线回归：**549 passed、28 skipped**，记录为 data/local/harness-phase4-full-tests-20261102.xml。跳过项不计为通过。全项目 Ruff、版本化契约检查、Wheel 构建与隔离导入通过。没有改动冻结契约或数据库迁移。

交付记录：

- 修改前备份：data/local/harness-phase4-before-274410e3.zip；
- 本批源码差异：data/local/harness-phase4-source-20261102.patch；
- 检查记录：data/local/harness-phase4-validation-20261102.json；
- Wheel：data/local/harness-phase4-dist-20261102/game_test_agent-0.1.0-py3-none-any.whl。

API 与 Worker 应采用同一版本；旧 Temporal 历史通过 patch 分支保留行为，新运行使用新步骤接口。检查点保持兼容历史版本 1/2/3，回退代码时仍需处理第三批新增的计划字段。

本轮未调用付费模型、真实游戏平台或真实沙箱，未安装新包，也未重启现有服务。上述验证证明调度与恢复边界，不证明真实调查质量或成本改善。

## 后续范围

本批是 P2 的基础接口与部分等待解耦。长操作的 submit/poll/collect、跨进程所有权、完整一轮执行器拆分和真实 Temporal 回放仍待实施。P1 的相同真实案例质量/成本评测也未完成，不能据此进入大规模并发扩展。

Router、Trigger/Thread/Worker Session 映射、事件批处理、跨任务资源发现和完整 Super-agent 协调层仍在后续阶段。
