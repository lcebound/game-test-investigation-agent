# 离线分支评测

`evals/branching.py` 根据策略每轮返回的 `NextStep` 推进环境。它与
`evals/trajectory.py` 的已录制轨迹评分并存。所有分支结果、设备分钟和运行数均由
离线场景定义；结果文件固定标记 `offline_branch_simulation`。

## 公共输入与私有环境

- 策略接口是 `decide(BranchView) -> BranchDecision`。公共输入包含初始
  `ContextBundle`、可建模操作及成本、已经取得的观测、可见证据 ID、历史操作和剩余轮数。
- 私有 `BranchNode` 保存完整转移表、允许的报告及所需引用、可接受的补充字段、
  哪些运行属于不必要运行。驱动器不把这些字段传给策略，也不采用模型自评的信息增益。
- 每个节点必须显式定义所有目录操作的结果。操作按执行字段匹配，包括实验参数、
  读取偏移/长度、配方版本/参数及目标 Run；理由、预测文字与读取目的不改变匹配。
  未建模操作返回 `unsupported`，能力不可用返回 `unavailable`，均不推进节点。
- 越界证据引用返回 `invalid`。执行前检查运行、设备分钟和工具读取预算；
  超限返回 `budget_exhausted`，不计执行成本。每次策略决策消耗一轮，反复失败不会无限循环。
- 每次回调接收深拷贝，不能通过修改公共对象改变私有标签或后续预算。

这是可信评测代码之间的数据接口，未提供进程沙箱。场景作者仍须检查公开文本、
文件名和 ID 是否泄漏答案；策略适配器不得自行读取场景文件或使用私有标签。

## 对照与评分

`run_paired_branches` 给每个场景建立两条独立轨迹，使用同一策略工厂、配置和种子，
只切换公共调查约束 `enable_experiment_selection`。每条轨迹的策略实例都是新建的，
去除开关后的初始公共输入哈希应相同。场景不允许已有执行预算用量、历史案例注入或
同时开启候选模型设计，以免把多个改动混进当前对照。

报告必须给出显式 `conclusion`，包括必要时的 `unresolved`。质量通过要求当前私有节点
支持该结论、引用覆盖指定证据且引用在可见范围内。补充信息请求必须准确匹配私有节点
标注的缺失字段。未得到支持的确定结论计为错误论断；达到轮数上限、策略异常或没有
合法终止均不算质量通过。这是离线场景的标签评分，不能替代生产报告的证据来源校验
或语义评审。

结果保存每一步决策、候选选择记录、环境响应、终止原因和成本。按 train/validation/test
分别统计质量通过率、错误论断率、模拟运行数、不必要运行数、模拟设备分钟、工具次数、
轮数、未建模操作数、模型调用数与 Token。`deltas_by_split` 是开启减去关闭后的差值；
成本差值为正表示开启后更贵。Token 和调用数须由适配器读取提供方用量回执填写。
`split_group` 不得跨集合复用；场景 ID 不得重复。

## 冻结和重现

在项目根目录使用当前源码运行，PowerShell 示例：

```powershell
$env:PYTHONPATH = 'src'
py -3.12 -m game_test_agent.evals.branching freeze evals/branch-demo.json --manifest evals/my-run.manifest.json --snapshot evals/my-run.source.zip
py -3.12 -m game_test_agent.evals.branching run evals/branch-demo.json --manifest evals/my-run.manifest.json --output evals/my-run.result.json
```

冻结清单包含每题完整内容哈希、包内全部 Python 源码哈希、Python/Pydantic/JSON Schema
运行时版本以及策略配置。运行前核对这些内容；任何变化都要求建立新的清单。
已有清单、源码 ZIP 和结果文件不会被命令覆盖。

`--snapshot` 将清单、完整场景和哈希对应的 Python 源码保存到 ZIP，源代码位于
`src/game_test_agent/`，场景位于 `evals/scenarios.json`，清单位于 `evals/manifest.json`。
以后修改代码后，可在独立目录解压，设置该目录的 `src` 为 `PYTHONPATH`，使用相同
运行时执行 `run`。ZIP 不打包 Python 或第三方依赖，也不包含原始平台服务及外部模型。
自定义模型适配器还应在配置中冻结提示词、模型档案及外部资源指纹。

## 当前训练示例

`evals/branch-demo.json` 含四个可复现的手工训练场景，由
`scripts/build_branch_demo.py` 构建。`branch_demo.py` 使用脚本生成提案与报告，
实际调用生产 `CandidateSelectionService.choose()`，没有模型调用和平台提交。
示例中的性能类别判断是场景约定，不能解释为真实 CPU/GPU 根因诊断。

2026-09-30 的冻结结果保存于：

以下为本地历史评测产物，不随源码发布；可按上面的命令重新生成。

- `evals/branch-demo-2026-09-30-v2.manifest.json`
- `evals/branch-demo-2026-09-30-v2.source.zip`
- `evals/branch-demo-2026-09-30-v2.result.json`

修订版已在独立目录从源码 ZIP 重跑，八条轨迹全部通过，冻结清单哈希、集合指标与差值
和上述结果一致。未带 `v2` 的早期归档保留供审计；后续复查使用修订版。

| 训练示例指标（每题平均，4 题） | 关闭选择 | 开启选择 |
|---|---:|---:|
| 标签质量通过率 | 100% | 100% |
| 错误论断率 | 0% | 0% |
| 模拟运行数 | 0.5 | 0 |
| 模拟设备分钟 | 1 | 0 |
| 工具次数 | 0 | 0.5 |

性能与日志补证题选择分析/读取；已有证据足够题直接结束；缺少构建题保留未解决结论。
这些数字验证驱动器和选择器接线，样本属于 train，不能作为独立模型能力提升、
真实节省成本或统计显著性的证据。

当前 `BranchView.context` 保留初始元数据并更新预算，后续观测通过
`observations/history` 提供。示例没有执行真实注册配方、写入新 Run/派生证据账本或运行
完整 controller。下一步需要实现模型/生产上下文适配器，按观测更新真实账本与
`ContextBuilder`，再建设按来源分组的 20–30 题任务集、重复采样和逐项消融。
