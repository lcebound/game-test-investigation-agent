# 统一调查运行、交付与诊断接口

后续更新：排队、诊断复用和真实依赖验证见[调查执行优化与真实依赖检查](optimization-and-dependency-checks-2026-10-31.md)，其中包含本轮之后完成的 MySQL/S3/Temporal 隔离联调及新增修复。

本次实现针对原生调查与生产执行路径分离、安装环境不可复现、并发和追踪不足、诊断工具深度及报告校验表达不清的问题。真实任务评测仍暂缓；没有启动真实游戏实验或调用真实模型。

## Local 与 Temporal

原生 MCP 调查现在使用同一 `NativeMcpController`，支持在一轮模型响应及其工具调用完成后保存检查点并让出执行。Local 连续运行这些轮次，Temporal 通过 `native_investigation_step` Activity 分轮驱动同一执行器。预算、缓存、假设、报告修正与恢复均使用同一份数据库/Artifact 状态。

Temporal 分支增加 `native-mcp-rounds-v1` patch marker，已有历史没有此 marker 时保留原路径。Activity 有心跳和有限重试；取消时等待 Activity 取消完成，再执行共享清理。补充输入从持久化输入账本读取，模型生成期间到达的新输入仍使旧响应失效。Local 与 Temporal 复用失败报告收尾逻辑。

调查首次执行会持久化 `investigation.runtime_selected`。运行方式是 `typed` 或 `native_mcp`，不能通过后续配置变化静默切换。已有原生检查点或动作账本用于识别旧调查。API 与 Worker 需要相同的 Provider、原生模式、模型及存储配置；Worker 必须具备调查创建时冻结的权限，原生调查要求 `runs:read` 和 `evidence:read`。

`GTA_MCP_NATIVE_TOOLS=true` 不再限制为 Local。生产环境的 Temporal、MySQL、S3、JWT 校验继续生效；没有绕过生产安全配置。原生能力与结构化能力由 Worker 分别连接，既有结构化调查不会因为启用原生模式而被改成另一类 Provider。

原生报告发布已拆到 `workflows/native_reporting.py`，假设/候选、工具目录、项目知识、模型请求及证据搜索分别有独立模块。Temporal 工作流包改为延迟加载，避免确定性沙箱提前导入 API、Pydantic 和模型 SDK。

新的原生检查点使用版本 2，读取器兼容版本 1。回退旧代码前应先处理新检查点和工作流版本兼容；本地单进程的恢复不等于分布式任务所有权，也不能让 Local 和 Temporal 同时运行同一调查。

## 可复现安装和 CI

新增 `uv.lock`，锁定包括生产适配器和开发工具在内的依赖。在独立 `.venv` 中验证锁文件，不修改系统 Python 已安装的包。推荐从源码目录运行：

```powershell
py -3.12 -m pip install uv==0.13.6
uv sync --locked --all-extras --no-editable
uv run --frozen --all-extras pytest -q
uv run --frozen --all-extras ruff check src tests scripts/check_contracts.py scripts/package_smoke.py scripts/export_schemas.py
uv run --frozen --all-extras python scripts/check_contracts.py
uv build --no-build-isolation
uv run --frozen --all-extras python scripts/package_smoke.py
```

`.github/workflows/ci.yml` 配置 Windows/Linux 与 Python 3.10/3.12/3.13 矩阵，执行锁定安装、静态检查、离线回归、Schema 一致性、源码包/Wheel 构建、包内资源与导入检查，并保存结果文件。CI 默认不配置真实模型或 Provider；目前只完成本机等价检查，尚未在远端 GitHub Actions 执行这些矩阵。

`scripts/export_schemas.py` 显式读取当前源码，避免误从旧安装包生成 Schema。`scripts/check_contracts.py` 检查当前 v2 Schema 是否过期，并用 `contracts/v1.sha256` 检查冻结的 v1 文件。v2 新增 Claim 的分项校验字段；严格绑定旧 v2 Schema 的下游需同步更新。

## 并发和追踪

| 环境变量 | 默认 | 范围 |
| --- | --- | --- |
| `GTA_MAX_CONCURRENT_INVESTIGATIONS` | 4 | 进程内调查/原生轮次 |
| `GTA_MAX_CONCURRENT_MODELS` | 4 | 共享模型请求池 |
| `GTA_MAX_CONCURRENT_TOOLS` | 8 | 原生读取工具池 |
| `GTA_MAX_CONCURRENT_ANALYSES` | 2 | 原生代码及注册分析配方 |
| `GTA_MAX_QUEUED_OPERATIONS` | 64 | 每类池最多等待数 |

这些是单进程限制，多副本仍需结合模型网关、设备调度和部署配额。原有持久设备 admission/fencing 不由信号量代替。排队超时和取消释放等待记录与容量，不能绕过既有预算。项目指标只展示当前租户/项目的活动和等待数。模型池覆盖原生请求、结构化推理、动作规划及候选设计。

正则搜索改用异步、可终止的独立进程，避免等待正则时阻塞所有调查；证据读取和审计保持在原来的数据库所有者线程。大文件读取、哈希和注册配方本身仍是同步的有界操作，未来需要更大输入时应增加专用执行 Worker。

新增内容无关的 `trace.span`：稳定调查 trace ID、父子 span、阶段耗时、错误类型和少量操作标识，覆盖模型、原生工具、动作提交/对账、证据摄取、分析和报告。不会记录提示词、代码、工具参数或异常原文。追踪使用本地持久事件格式；未部署 OpenTelemetry Collector 或外部告警系统。

有作用域校验的读取接口：

- `GET /v1/investigations/{id}/trace`：最近最多 5000 个 span，明确返回是否完整。
- `GET /v1/investigations/{id}/trajectory`：分页汇总计划、候选、假设、用量、证据身份、追踪及报告，固定标记 `evaluation.performed=false`，不生成评分标签。

工作台报告增加“导出调查轨迹”，使用现有认证请求下载 JSON。事件和 Artifact 尚未自动删除；仍需按部署需求制定归档、保留期和安全回收策略。

## 注册诊断与知识引用

原生流程增加 `agent_analyze_evidence`。输入必须属于当前调查，配方仅使用注册的确定性代码，不执行模型提交的脚本。执行结果保存为带 `derived_from` 的派生证据，并继承原有信任边界。

崩溃输入使用现有 `CrashDocument` 契约，例如：

```json
{
  "recipe": "crash",
  "evidence_id": "本调查证据ID",
  "data_pointer": "/crash"
}
```

`/crash` 对应对象需要 `signal`、`dump_kind`、`symbols_matched` 和 `frames`；栈帧字段为 `index/module/symbol/file/line`。没有匹配符号时不输出可归因的首个栈位置。本次不提供二进制 Dump 符号化服务，需要平台提供结构化符号化结果。

帧耗时输入示例：

```json
{
  "recipe": "frame_time",
  "evidence_id": "本调查证据ID",
  "data_pointer": "/samples",
  "frame_column": "frame_ms",
  "component_columns": ["cpu_ms", "gpu_ms"],
  "timestamp_column": "timestamp",
  "time_from": "2026-10-29T02:00:00Z",
  "time_to": "2026-10-29T02:05:00Z"
}
```

样本为同一行对齐的数值对象，字段必须已经使用毫秒；时间过滤要求带时区。至少三个样本，最多十万行且输入总大小不超过 4 MiB。输出最近秩 P50/P95/P99、最大值及最慢帧同一行的组件测量，保留选中行号。缺失列、布尔值、非有限数值及负数拒绝计算。单个窗口不能证明独立复现、恢复或 CPU/GPU 根因。

原生工具目录只预加载最多八个 MCP 工具 Schema；工具较多时通过 `agent_find_tools` 搜索并加载原始 Schema，不改写平台协议或扩大权限。原生流程每轮重新解析指定的项目知识包与适用的已审核案例，过期/撤回后不继续作为当前引用。知识包和历史案例有独立大小上限，缺失或省略有明确标记，始终只用于规划。

## 报告的分项校验

Claim 增加 `verification_details`：

- `citation`：是否核对原文。
- `provenance`：未知、平台声明或已核验来源。
- `support`：未检查、仅原文一致或确定性支持。
- `causality`：是否独立验证因果。

发布时重建这些状态，忽略调用方自填的认证结果。保留旧 `verification_status` 的结构校验语义，工作台和 Markdown 明确展示分项状态。平台声明不等同于可信来源，注册配方不自动提升根因或修复结论。

## 验证范围和后续工作

本机完成锁定依赖环境下的离线回归、原生 Activity 分轮恢复、持久取消、Workflow 沙箱准备、异步取消、并发及作用域隔离、诊断配方、包构建和浏览器检查。没有在真实 Temporal/MySQL/S3/JWT 集群完成升级回放、容量和故障验收，也没有在真实引擎数据上验证诊断准确率。

最终本机验证：Windows / Python 3.12.3，锁定环境 **393 passed、25 skipped**；跳过项需要实际 Windows 沙箱环境。静态检查、v1 哈希与 v2 Schema 一致性、两类 Workflow 的 SDK 沙箱准备、源码包/Wheel 及包内资源、桌面与 390px 移动端、分项校验展示和认证轨迹下载均通过。原 Python 环境的新增 25 项回归也通过；其旧 pytest-asyncio 对新增配置给出一条兼容性警告。结果记录为 `data/local/runtime-delivery-validation-20261029.json`，浏览器产物为 `data/local/enhancement-ui-68f52232`。

后续仍需要真实任务评测、跨副本配额、生产依赖故障演练、完整的 Artifact 保留/回收、领域数据采集与符号化适配、受独立验证的因果判断，以及逐步拆分较大的 Repository/API 模块。此次代码为这些工作补齐可复现的执行与交付入口。
