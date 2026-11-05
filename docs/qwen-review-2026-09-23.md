# Qwen 复审与模型驱动工作流检查

日期：2026-09-23。

## 范围

使用用户指定的 `qwen3.8-flash` 发起真实模型请求，经通用 InvestigationController、调查内核、模型网关、动作规划、执行账本及证据摄取完成检查。测试 provider 明确标为 simulated，返回固定 CSV 内容，每次运行拥有不同 run ID。

本次没有启动 Lyra 平台、MySQL 或真实设备。检查时本机 18080 端口关闭，进程未配置平台服务令牌；本次结果不能替代真实平台在线验收。API Key 只注入临时进程环境，不写入脚本、配置或结果文件。

## 复审发现与修改

真实模型输出暴露了 JSON Object 提示与 NextStep 契约之间的歧义：Qwen 会输出顶层 `parameters`，而动作参数属于独立 ActionPlanner，严格 NextStep 校验因此拒绝请求。

修改 Investigator 的 JSON Object/JSON Prompt 指令：

- 明确输出是 NextStep，而非 ActionProposal 或工具调用。
- 明确普通决策可选字段，禁止将参数、目标选择器、能力版本和运行序号放入决策顶层。
- 追加实验必须引用上下文中的证据 ID。
- 从 `working_state.completed_actions[].test_verdict` 读取平台判定，不从执行成功推导测试通过。

保留严格校验，未新增删除非法字段或替模型改变分支的兼容逻辑。增加回归测试，确认顶层 `parameters` 仍会被拒绝。

## 调试与结果记录

| 批次 | 结果 | 说明 |
| --- | --- | --- |
| 初始 | 单次路径通过；二次条件在首轮 RuntimeError | 新测试脚本对每个场景分别 asyncio.run，与 SDK 共用异步客户端不兼容；已改成整个批次共用一个事件循环 |
| 脚本修复后 | 单次首轮、补测第二轮出现 ValidationError | 没有强行放行或提交非法动作 |
| 字段级诊断 | 两个场景首轮均拒绝顶层 parameters | 保存字段位置与错误类型，不保存原始供应商错误或凭据 |
| 提示约束修复后 | 两个场景均通过 | 见下表，属于新版本结果，不能与前批次混算成功率 |

最终模型配置：Chat Completions / JSON Object，`enable_thinking=false`，120 秒模型超时，2048 输出上限；上下文窗口 8192，输出预留 2048。两项任务的最大运行数均为 3、最大轮数 4；测试请求明确只要求模拟运行，不能将其解释为真实游戏验收。

| 条件 | 模型决策 | 模拟运行数 | 总耗时 | 契约错误 |
| --- | --- | --- | --- | --- |
| 单次足够 | experiment → report | 1 | 12.25 秒 | 0 |
| 至少两次独立观测 | experiment → experiment → report | 2 | 16.03 秒 | 0 |

补测第二轮正确引用已有证据，进入追加实验节点和真实执行代码路径；两个独立模拟 Run 均通过后主动停止，没有为用完额度提交第三次。单次条件的报告理由明确读取了工作状态中的 passed。

最终批次成功调用账本合计 5 次模型调用，输入 19,923 token、输出 976 token。账本里的 estimated_cost=0 代表未配置价格，不能解释为调用免费。当前账本主要保存成功返回的 ReasoningStep，失败尝试可能未进入 model_usage，因此此统计也不是整个调试过程的总费用或总调用量。

## 工程检查

- `py -3 -m pytest -q`：172 passed。
- `py -3 -m ruff check src tests scripts`：通过。
- `py -3 -m ruff format --check src tests scripts`：通过。
- `py -3 -m compileall -q src tests scripts`：通过。
- 扫描 src/tests/scripts/docs，未发现此次密钥前缀文本。

最终 Investigator 文件 SHA-256：`6db71500a22dba99bbd390c0ec38e7a2aebd119510c948644ab4b09b85434e77`。这只是该文件指纹，不是完整版本冻结清单。

## 可复核产物

- [可复用测试脚本](../scripts/model_workflow_smoke.py)。凭据从进程环境读取，测试账本在临时目录内用完即清理。
- 首次记录（本地历史材料，不随源码发布）。
- 脚本修复后记录（本地历史材料，不随源码发布）。
- 字段诊断记录（本地历史材料，不随源码发布）。
- 最终通过记录（本地历史材料，不随源码发布）。

结论：此前动态计划的修复已在真实 Qwen 驱动、模拟 provider 执行的双场景工作流中得到验证。输出契约歧义得到针对性修正，但一次最终双场景通过不证明长期稳定性、真实平台恢复或复杂根因诊断能力。失败尝试的用量审计仍是后续改进项。
