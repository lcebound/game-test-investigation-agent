# Lyra 真实工作流复核：2026-09-23

## 结论

使用 qwen3.8-flash，通过通用调查工作流自动提交 Lyra 真实任务。单次条件与预算修正后的重复性条件均完成预期路径，第二次实验节点扩展在真实平台上得到验证。此次共执行 3 个调查、4 个平台 Run；4 个 Run 均为 real / succeeded / passed。不能将结果表述为三个调查全部成功，也不能据此估计长期成功率。

首次重复性调查由于测试启动参数沿用默认 10 设备分钟预算而中断，保留原账本；修正为 30 后新建调查重跑。代码、提示词和模型配置在两批执行前后均通过同一冻结清单校验，没有修改业务源代码。

## 结果

| 调查 | 设备分钟上限 / Run 上限 | 决策路径 | 平台运行 | 耗时 | 评价 |
|---|---|---|---|---|---|
| single | 10 / 1 | experiment → report | 1 passed | 69.38 秒 | 通过 |
| repeatability 初次 | 10 / 3 | experiment → experiment，规划被拒 | 1 passed | 60.47 秒 | 未完成目标；测试预算配置不足 |
| repeatability 修正预算 | 30 / 3 | experiment → experiment → report | 2 passed | 85.34 秒 | 通过，未提交第三次 |

- 单次调查：`inv-c9e71c8ccb1a4fd08589c17672fd56c4`
  - `run-94065d31-cdb7-4a36-93d2-531500af82ee`
- 预算不足调查：`inv-3b00f4e3d66f44928e7233dabf92e010`
  - `run-e49428cd-c893-46ab-a56e-b49ab1b8fb78`
- 修正预算调查：`inv-aa66ef0c2f454264ab2768ab5877ca32`
  - `run-c5551153-a4e0-40b9-becb-32a2a928c099`
  - `run-2cae0c35-d7bb-4b21-a0d5-9ef159b1786c`

用例为 `case:lyra.cqtest.basic.teams.compare`，目标 `windows_client`，`capture_profile=none`，每次动作超时 180 秒。重复性调查保持相同用例和目标。设备分钟是预算上限，并非实际占用时长；平台能力描述给出的每次预估为 10 分钟。

## 关键验证

1. **模型驱动闭环**：Qwen 选择 experiment 后，由通用工作流和 Lyra 适配器提交；无手工代替提交，也未使用模拟 Provider。
2. **追加计划节点**：成功补测的第一个动作绑定 `collect-evidence` / revision 1；第二个动作绑定 `experiment-dec-56356ab453f7519f44032c82f958304e` / revision 2，run_sequence 为 2。没有重复绑定已完成节点。
3. **及时停止**：成功补测允许最多 3 次运行，Qwen 在 2 次通过后选择 report。单次任务也明确选择 report，但因上限为 1，其证据强度弱于留有余量的补测任务。
4. **证据与审计**：三个调查分别摄取 2、2、4 条证据。成功补测中的两份 HTML 内容哈希相同，但仍分别关联两个独立 Run。8 条证据对应本地文件哈希复核全部通过；三个调查的审计完整性均为 complete=true、issues=[]。
5. **证据阅读**：单次及成功补测的停止理由均明确引用 working_state 的 test_verdict=passed，没有重现“字段存在却声称缺失”的现象。仅说明这些样本表现正确。
6. **报告边界**：成功补测报告为“2 次 Run 的平台判定均通过；指定构建验收证据不足。”平台仍缺少构建与环境溯源字段，最终 resolution 保持 inconclusive。

## 预算失败的准确归因及待改进项

在原账本的独立副本中，用原决策和冻结上下文重放动作规划，没有调用模型或提交平台，得到具体异常：

`ValueError: additional experiment plan rejected: plan exceeds the device-minute budget`

原请求总预算为 10，首个节点预估 10，第二节点使计划总预估变为 20。此处预算校验正确拦截，不能归因于旧的节点重复绑定问题。测试启动参数是本次直接原因。

产品层面仍有改进空间：

- 测试入口应校验验收目标与预估预算是否匹配，避免重复性目标带着仅够一次的预算开始。
- 控制器应将预算拒绝明确分类并给出所需/可用额度，而不是仅输出 `action_planning_failed:ValueError`。
- 当前静态计划预估与实际已消耗额度的语义值得单独审核；本次未改变预算算法，也未证明其适用于所有成本结算方式。
- 平台标准启动脚本的 MySQL 容器名仍不一致。此次直接启动现有 `lyra-test-mysql`，没有修改平台脚本或数据库配置。

## 范围与复现材料

本次证明受约束目标下的自动提交、补测、停止、证据保留与计划绑定；重复性目标明确要求两次，因此不证明模型能自主提出复杂诊断策略。未验证服务崩溃恢复、并发调查、任意游戏用例、长期稳定率或指定构建验收。

模型使用 Chat Completions + json_object，enable_thinking=false，timeout=120 秒，max_output_tokens=2048，context_window=8192。代码指纹：`60250faca3e1c61fec8bc1f727bbdc2fcecb42a7456852f656de6e94c3e1c2e7`。

- 冻结清单：acceptance-freeze-live-review-2026-09-23.json（本地历史材料，不随源码发布）
- 决策、Run 结果、证据哈希、节点绑定和审计结果：live-lyra-review-2026-09-23.json（本地历史材料，不随源码发布）
- 本地账本与原始脚本输出：`data/live-review-20260923/`。其中 `repeatability.stdout` 保留原失败，`repeatability-budget30.stdout` 为新调查。

本次未修改业务源代码、未重复执行此前已通过的 172 项离线测试。API 仅监听 127.0.0.1:18080；临时服务凭据仅通过进程环境传递。完成后临时 API、Worker 进程树已结束，MySQL 恢复停止状态，18080 无监听。指定源码、文档与本次日志目录的密钥前缀检查未发现落盘。
