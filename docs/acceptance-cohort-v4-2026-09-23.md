# 冻结验收 V4：改进版本复测

冻结清单：acceptance-freeze-v4-2026-09-23.json（本地历史材料，不随源码发布）。代码指纹 `0bef1761379074c52044600eef87053027d19de234c2ff49f5a505c39c5c068c`，提示词指纹 `755eafff1ed1801d3f6df7e3ad2976844f6b79d52753f0f02adaf8eee4cf8b58`，共覆盖 212 个代码、脚本、测试和配置文件。模型仍为 `qwen3.8-flash`，Chat Completions / JSON Object，关闭 thinking；120 秒超时，2048 输出 token，8192 上下文窗口。平台通过现有 Lyra Integration API 适配器接入；凭据只在进程内注入。

## 工程回归

- `pytest -q`：163 项通过。
- `ruff check`：通过。
- `compileall`：通过。
- `ruff format --check`：失败，24 个文件需要格式化；本次只测试，没有自动改写这些文件。

## 真实配对任务

| 条件 | Investigation | 模型决策 | 真实 Run | 结果 |
| --- | --- | --- | --- | --- |
| 至少两次独立结果 | `inv-b89d8880a2bf46aa84e9292e282e16d6` | `experiment → experiment` | `run-4ed47b9a-5cd0-4c77-a792-03acd2c80993`，`passed` | 第二次动作规划失败，`action_planning_failed:ValueError`；只执行了一次 Run |
| 一次结果已足够 | `inv-bdec5a5e4011475dbfbb4bf2b5743cc5` | `experiment → report` | `run-2d8db0b2-dcee-480a-b4cc-74fe9dc38edd`，`passed` | 完整结束，没有多跑；报告写明“本次 Run 平台判定通过；指定构建验收证据不足” |

两次任务的只读审计完整性检查均为 `complete=true`，没有哈希、决策 envelope、动作绑定、证据或报告来源图问题；确定性报告 evaluator 也接受两份报告。模型用量账本正常记录：重复性任务 2 次模型调用、7757 输入 token、308 输出 token；单次任务 2 次模型调用、7519 输入 token、1130 输出 token。

## 发现

1. **重复实验与静态计划节点冲突。** 新编译计划只创建一个 `collect-evidence` 实验节点。第一轮动作完成后，投影状态把该节点设为 `completed`；第二轮模型正确提出相同条件的受控重复，但 `ActionFactory` 只允许动作绑定 `pending` 或 `ready` 节点，候选数变为零并抛出 `ValueError`。因此 V3 中已经通过的“第一次不足 → 第二次实验”路径在 V4 回归。
2. **失败报告没有突出重复性目标尚未满足。** 重复性任务报告准确记录了唯一 Run 的平台 `passed`，coverage 也把 `two-run-repeatability` 放在 `uncovered_objectives`，但摘要仍使用单次通过模板。结构 evaluator 接受该报告，说明 evaluator 目前验证引用和结论强度，但没有把“工作流因规划失败而未满足显式验收条件”作为阻断项。
3. **单次任务停止正确，但模型理由不忠实。** 第二轮冻结上下文的 `working_state.completed_actions[0].test_verdict` 明确为 `passed`，无上下文裁剪；Qwen 的 reason_summary 却反复声称输入中没有 `test_verdict`，随后从 `execution_status=succeeded` 推断 passed。最终分支和确定性报告正确，但这段理由不应计为严格基于证据的判断。
4. **平台启动脚本有独立问题。** `docker-compose.yml` 的 MySQL 容器名是 `lyra-test-mysql`，`scripts/ensure_mysql.ps1` 仍检查 `android-unreal-test-mysql`，导致标准启动脚本在已成功启动 MySQL 后报错。本次通过直接使用健康的 MySQL 并启动后端完成联调，没有修改平台仓库。

结论：改进版的审计、来源图、模型用量和报告结构链路在真实任务上工作正常；单次停止分支通过，但关键的第二次实验路径发生工程回归，当前不能沿用 V3 的 3/3 重复性成功结论。
