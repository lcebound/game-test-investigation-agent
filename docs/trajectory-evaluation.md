# 成对调查轨迹评测协议

本评分器只读取已记录的决策，不调用模型，也不向平台提交实验。`evals/trajectory-example.json` 是说明格式的手写 `train` 数据，不能作为能力或案例记忆收益的证据。

真实 A/B 采集时，先固定问题、构建、可用能力、预算、首轮实验结果与可见证据；同一模型/参数分别在关闭和开启案例记忆的上下文中继续。每个 `case_id` 必须有 `without_memory`、`with_memory` 各一条轨迹，且首轮 observation 完全一致。后续结果可以因实验不同而不同，但必须由先前设计 ID 关联，不能凭空出现。`split_group` 以项目/构建划分，测试集不得用于调提示词。

`ContextBuilder.build(..., include_historical_cases=False)` 提供只移除历史案例的消融开关；正常运行默认开启。采集器应冻结这一开关以及其他上下文、模型和平台输入，确保只有记忆条件不同。

评分器逐轮检查：首轮是否承认证据不足；是否提出有授权能力、目标指标、必需对照变量和至少一个标注判别变量的第二次实验；两个竞争假设是否预测不同可观测结果；是否引用当前可见证据；证据仍不足且可继续测试时是否过早报告。查询等合法中间动作可存在，不要求固定调用顺序。

`uncertainty_reduced` 必须由独立标注或可信的结果判据填写，缺失时记为 `null`，不能从 Agent 自述推断。结果可能减少不确定性却仍不足以下结论，因此此标签与 `state=conclusive` 分开。输出分别报告设计率、实际减少率和后者的有效样本数；`design_rate_delta` 是两组设计率差，不是统计显著性。样本量不足、失访不均衡或未盲测时不得宣称增强有效。

可以用下面的命令验证评分器和 JSON 格式：

```powershell
.venv\Scripts\python -m game_test_agent.evals.trajectory evals/trajectory-example.json --output trajectory-result.json
```

门槛只应在建立真实基线后设定，例如 `--min-design-rate 0.7 --min-design-delta 0.1`。这两个参数未提供时，仅输出评分，不因模型表现差而让命令失败。可信采集器还需把设计字段绑定到冻结的模型决策或持久化计划；JSON 本身不能证明轨迹真实、结果独立或平台成功运行。
# 可分支环境

现有文件继续用于已录制轨迹评分。根据 Agent 决策推进环境、冻结源码和场景、
比较候选选择开关的入口见 [离线分支评测](branch-evaluation.md)。
