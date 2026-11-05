# 冻结验收 V3：六次配对真实任务

冻结清单：acceptance-freeze-v3-2026-09-22.json（本地历史材料，不随源码发布）。代码指纹 `f244a81661fd9c8656f6c309309980cb204ee943a58f67dacb6a0c3ab9bbc5a1`，提示词指纹 `23f3cf7da3e00f100e55dda1ea10d7be2e80ed297efae490888e47b840f142ee`。模型为 `qwen3.8-flash`，Chat Completions / JSON Object，关闭 thinking；120 秒超时，2048 输出 token，8192 上下文窗口。与 V2 的唯一行为修复是：模型明确选择 `report` 却把说明字段 `expected_observation` 设为 `null` 时，补成固定非空说明；不改变 `kind`、证据、能力或执行参数，其余契约仍严格校验。三组配对任务共享同一代码、提示词、模型配置；每次最多 3 个真实 Run、4 轮、30 设备分钟。每次试跑前及批次结束后均验证指纹一致。

| 序号 | 条件 | Investigation | 模型决策 | 真实 Run | 验收 |
| --- | --- | --- | --- | --- | --- |
| 1 | 需要两次 | `inv-4d81116b1eca4972944357d27afb17bd` | `experiment → experiment → report` | `run-f310475b-784f-480e-a005-928887d012a0`、`run-baf30621-53a0-43e1-bd5f-096fd2d23a93`，均 `passed` | 通过；第三轮 `report` 的说明字段由兼容层补全 |
| 2 | 一次足够 | `inv-811a9e1570b342e2bf4bdae49748e626` | `experiment → report` | `run-4b030e40-b5bc-445d-ad6e-fd5ef25bc231`，`passed` | 通过；主动停止 |
| 3 | 需要两次 | `inv-d0e12182028e40cf909c1732f42eca87` | `experiment → experiment → report` | `run-fc9b9431-6e41-42db-a5b7-dbec47957614`、`run-2d68f4cd-1485-43b1-8a31-1cde92c524a0`，均 `passed` | 通过；两次足够后主动停止 |
| 4 | 一次足够 | `inv-07eb29422e304a8eafd4a29bc649f7c8` | 首轮 `ValidationError` | 无 | 失败；安全停止，无平台副作用 |
| 5 | 一次足够 | `inv-6ad043c575ab4496a4c33763b859544d` | `experiment → report` | `run-acead2f8-b37b-4bcd-ae7b-27a09672bec1`，`passed` | 通过；主动停止 |
| 6 | 需要两次 | `inv-f3248c5b931245f292021c1d4b2d6e9a` | `experiment → experiment → report` | `run-34d3d9bd-d756-472f-9b19-81f08bc64e73`、`run-82a4e9e2-81ea-4910-a219-11175c146477`，均 `passed` | 通过；第三轮 `report` 的说明字段由兼容层补全 |

结果：完整预期路径 5/6；需二次实验分支 3/3，证据充分应停止分支 2/3。八个实际提交的 Run 均返回平台 `passed`，每次成功任务都在剩余运行额度存在时停止，没有第三次“凑数”运行。两个单次成功报告均写明“本次 Run 平台判定通过；指定构建验收证据不足”；三个重复性成功报告用复数形式写明“2 次 Run 的平台判定均通过；指定构建验收证据不足”。报告的整体 `resolution` 仍为 `inconclusive`，对应指定构建验收证据不足，不否认 Run 的平台通过判定。

解释边界：这证明当前平台连接、通用工作流、Qwen 分支选择和报告边界在该用例的六次固定样本中有五次端到端成功；不证明跨用例、跨目标或构建验收的普遍可靠性。首轮 `ValidationError` 仍需调查。兼容层只补说明字段，因而“模型主动选择 `report`”成立，但两例不是未经规范化的原始 JSON 严格通过。V1、V2 失败记录另存，不并入 V3 成功率。
