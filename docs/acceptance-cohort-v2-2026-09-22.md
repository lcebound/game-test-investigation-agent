# 冻结验收 V2：配对真实任务

冻结清单：acceptance-freeze-v2-2026-09-22.json（本地历史材料，不随源码发布）。代码指纹 `5d6ad940255053fc1e66fdbede7d852565fb5225fdea2b5663227a52337dc37a`，提示词指纹 `fdc5977002e40a54227b9207c4e716f6beddd37dd85c1a662179d3f0f6ad90aa`。模型为 `qwen3.8-flash`，Chat Completions / JSON Object，关闭 thinking；120 秒超时，2048 输出 token，8192 上下文窗口。每次最多 3 次真实运行、4 轮、30 设备分钟，故有余量检验适时停止。每次试跑前验证冻结指纹。

| 序号 | 条件 | Investigation | 模型决策 | 真实 Run | 判定 |
| --- | --- | --- | --- | --- | --- |
| 1 | 至少两次独立结果 | `inv-8ff9b100b15d4dc2839284bf36c6cf85` | `experiment → experiment → ValidationError` | `run-81e3007b-5acb-444b-852f-62a0c345fd23`、`run-0a9b4cb5-723d-4e57-b982-87ad6a7f27a0`，均 `passed` | 正确设计并执行第二次受控实验；两次足够后模型未能有效提交 report，系统兜底收尾，不计完整成功 |
| 2 | 单次结果已足够 | `inv-db83730ff24b4ab496cde4718ad5f4a5` | `experiment → ValidationError` | `run-c00ff236-ae09-4e04-8d92-86de258d020d`，`passed` | 首次结果充分，但模型未能有效提交 report；系统兜底收尾，不计主动停止 |
| 3 | 至少两次独立结果 | `inv-4d400d78335f4c34b4831156153487fc` | 首轮 `ValidationError` | 无 | 输出契约失败，安全停止 |
| 4 | 单次结果已足够 | `inv-3a7ba50b091b4172a7cfde7ffac045c5` | `experiment → report` | `run-f10ee9f4-d4d9-42e5-b41e-f40eeab2a79f`，`passed` | 成功：剩余两次运行额度存在仍主动停止 |

四次均生成报告；有真实运行的三次，报告准确区分平台 `passed` 与指定构建证据不足。模型完整通过配对路径为 1/4；单次条件 1/2 主动停下；重复性条件 1/2 正确发起第二次实验，但 0/2 在证据满足后由模型主动停下。三个失败或部分成功样本均原样保留。系统兜底是安全性，不是模型调查能力的成功记录。本版仍不足以证明稳定重复成功。
