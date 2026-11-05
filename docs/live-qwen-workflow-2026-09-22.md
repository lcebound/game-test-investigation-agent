# 2026-09-22 Qwen 驱动的通用工作流真实任务

本次启用用户指定的 `qwen3.8-flash`，经 OpenAI 兼容 Chat Completions 接口和显式 `json_prompt` 模式，从通用调查 API 发起 Lyra Windows Client 队伍关系比较用例。服务 token/API Key 只由进程环境注入，未写入仓库配置或测试记录。

成功的调查是 `inv-3a1325724b34470fb9374c001e525af7`：

- 第一轮 Qwen 决策 `experiment`，明确选择 `test.run` / `platform-local`。通用规划器冻结动作 `act-3a1325724b34470f-001-001`，通过策略预算和平台适配器提交，不是手动 Integration API 调用。
- 平台真实 Run `run-7032059e-eb49-47bc-bedc-3aa63a16a7e6`，结果修订 1→2→3（queued→running→succeeded），终态 `execution_source=real`、`test_verdict=passed`。执行时间 `2026-09-21T16:22:53.569433Z` 至 `2026-09-21T16:23:15.130700Z`。
- 两份真实证据已摄取：HTML 报告 `artifact-f46772759939474b2e73be29`，SHA-256 `d73240703aee217c56bf4eadc0aefe110ca8052f37fa20a602336ad43d74eeba`；日志 `artifact-424fac9c7c7effaf1625b6cb`，SHA-256 `265be4d8eacf31fadfa060cc15e76fe586a9b4a09c06cfa7d4b65dd82fb971a2`。
- 第二轮 Qwen 决策 `report`，没有再提交运行。原始报告为 `inconclusive` / `observation`，因为尚无可计算验收规则，且构建哈希、版本及环境指纹缺失。

过程中有两个没有提交平台任务的模型失败，均被门禁安全停止：`inv-112af7bfebed47028cefd8b66d4e2d26` 的 `experiment` 缺少能力/提供者 ID；`inv-e693853af6794821b9aaffb8f9206bec` 的响应未通过 `NextStep` JSON 校验。收紧兼容模式提示、提高输出额度后，第三次调查完成了模型决策→真实运行→模型再决策的闭环。模型输出额度与耗时仍需作为稳定性风险持续观测；不能据此认定单一原因已被证明。

本次还发现通用上下文原先只展示动作提交时的 `queued` 状态，未展示最新运行快照的 `passed` 判定。已修复为在权威 `working_state.completed_actions` 中携带最新执行状态、测试判定、真实/模拟来源、结果修订、摄取状态和缺失字段，并增加回归测试。修复后的**只读模型回放**基于上述真实调查账本，Qwen 明确表达“该 Run 的平台判定是 passed”，同时指出证据质量为 partial、构建/环境来源不完整，不能扩展为特定构建验收。该回放没有新增平台运行，也没有改写原调查报告；不能把它描述为原工作流第二轮的实际输出。

复现入口：[真实工作流脚本](../scripts/live_workflow_smoke.py)；[只读模型回放脚本](../scripts/replay_investigator.py)。本轮不覆盖 Temporal、进程重启恢复或有判别力的第二次实验设计。
