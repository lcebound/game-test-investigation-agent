# Super-agent Harness 改造收尾记录

日期：2026-11-03。接续[第十七批实现](harness-phase17-implementation-2026-11-03.md)，补齐本轮约定的框架缺口，保留现有 Python、MCP、Repository、预算和报告机制。

## 1. 当前实现范围

| 能力 | 最终实现 | 验证与边界 |
| --- | --- | --- |
| Router–Worker 隔离 | Router 只读任务卡与有界事件，Worker 独立维护问题计划、证据和检查点 | 既有分层、来源召回和作用域回归继续保留 |
| Inline & Spill、链式召回 | 输入、证据、反馈和历史来源使用摘要/目录及可核验引用，超出预算的原文按需读取 | Router 不读取全部 Worker 日志，不向所有任务广播完整上下文 |
| 通用外部观察事件 | 新增 ObservationEvent、持久接收、不可变批次、可恢复语义归属及原文查询 | 每批最多 10 条、每次最多 4 组；只投递明确引用的既有活动任务 |
| 只读 MCP 长操作 | 显式声明 submit/lookup/query/cancel；提交前持久化身份，等待让出、重启接手、原结果摄取与清理 | 仅适配声明的只读作业协议；不是任意阻塞 RPC 的透明恢复，也不是 MCP Tasks 全协议实现 |
| 跨任务技术综合 | 同会话 2–8 个终态任务的固定报告版本、主张引用、差异及待验证问题 | 综合内容是推断，不自动变成本次证据、共同根因、构建验收或补测授权 |
| 模型卡在计划编辑 | 返回当前 revision、合法证据/输入 ID；有有效计划但连续两次无效编辑时，先读取证据再恢复计划编辑 | 纠偏状态独立保存，避免污染工具缓存 |
| 持续运行 | 原有后端绑定、跨进程独占、检查点、预算、反馈投递和清理继续复用 | 真实 MySQL/Temporal 故障验证见下文 |

本批主要文件：`events/observation_router.py`、`workflows/native_operations.py`、`memory/synthesis.py`、`evals/harness_live.py`，以及原有 API、MCP、桥接和 Worker 入口的适配。没有替换为 TypeScript 运行时，没有引入第二个协调数据库。

## 2. 参考 Super-agent 的落点

沿用前期核对的固定版本：`deepseek-ai/dsh-superagent-prototype@6337fc823ce20251c8de8085948165c81aaa701e`。

- [orchestrator 提示与任务组织](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/prompts/orchestrator.md)：协调者读取轻量任务信息，执行细节留给独立 Worker；本项目沿用已有卡片、摘要、原文引用和权限过滤。
- [评估身份与恢复](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/core/evals.ts)：复用本项目已有持久路由评估、代次与超时机制管理观察事件批次，封存成员后新增事件不能混入旧批次。
- [Effects](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/app/effects.ts)：等待让出、持久副作用身份和有限恢复；本项目复用 MCP 作业账本、原有 EvidenceService、清理义务和预算。

上述为行为借鉴与 Python 适配，没有直接移植上游 TypeScript 文件。测试调查仍由现有问题账本、证据判断和自动执行逻辑驱动。

## 3. 外部观察事件接入

HTTP：`POST /v1/events/observations`。调用者须具有同租户/项目的 `events:write`，事件源还须在 `GTA_EVENT_SOURCE_POLICIES_JSON` 中配置。MQ 队列为 `agent.observations.v1`，路由键为 `observation.{platform_id}`。

```json
{
  "event_type": "game_test.observation",
  "event_id": "monitor-event-17",
  "source": "monitor",
  "tenant_id": "studio-a",
  "project_id": "game-a",
  "conversation_id": "inv-00000000000000000000000000000000",
  "thread_key": "login-monitor",
  "kind": "counterevidence",
  "summary": "回到登录崩溃任务：新构建仍出现同类错误",
  "details": {"build_id": "candidate-build"}
}
```

`conversation_id` 必须替换为实际存在、同项目的会话。`kind` 支持 progress/failed/recovered/counterevidence/phase；整条事件限 32 KiB。相同源和 event_id 重试幂等，内容变更拒绝。

事件在接收时保存原文与哈希，归属评估时封存成员和来源快照，提交时再次核对来源、任务版本、活动状态、会话归档状态和执行期限。失败、恢复、反证分别保存，不会因合批丢失。成功归属后复用反馈批次及原有唤醒通路，不伪装成用户输入，也不创建任务、授予权限或提高预算。

来源不明确的事件保留 unresolved 状态。关闭语义模型时只接收并保留 pending，不会猜测归属。启用需配置模型，并设置 `GTA_ENABLE_MODEL=true`、`GTA_ENABLE_SEMANTIC_ROUTER=true`；后台恢复默认开启，可由 `GTA_ENABLE_ROUTER_RECOVERY` 控制。

- `GET /v1/conversations/{id}/observations`：分页状态与摘要。
- `GET /v1/conversations/{id}/observations/{event_key}`：可核验原事件、哈希与不可信数据标记。

## 4. 只读 MCP 长操作协议

在已有 Provider 配置中增加以下字段；工具必须由接入方实际提供：

```json
{
  "native_operations": {
    "scan_logs": {
      "lookup_tool": "lookup_scan",
      "query_tool": "get_scan",
      "cancel_tool": "cancel_scan",
      "request_id_argument": "request_id",
      "operation_id_argument": "operation_id"
    }
  }
}
```

`scan_logs` 必须标记 readOnlyHint=true。请求 ID 由系统注入，不向模型暴露；控制工具从模型菜单隐藏。工具需满足：

1. 提交接收稳定 request_id，返回 `{operation_id,status}`；服务端应对同一 request_id 幂等。
2. lookup 根据 request_id 找回已提交操作，query 根据 operation_id 查询状态。提交响应丢失或客户端重启后，只查询已提交身份，不盲目重新提交。
3. 完成返回 `{operation_id,status:"completed",result:{...}}`；失败或取消使用 failed/canceled，错误结果保留为失败证据。
4. cancel 只针对持久记录的自有操作；仅收到终态确认才结清清理义务，requested 不算取消完成。

配置、端点、原工具 Schema、请求哈希和 operation_id 都固定绑定。每次恢复查询计入读取预算；预算耗尽保留缺口和清理义务，不再无限轮询。最多 16 个在途操作，取消每轮最多 4 个且总时限 15 秒。等待期间不继续消耗模型轮次，报告入口拒绝尚未收集的在途操作。

终态结果经原有证据服务入库，句柄不会直接成为证据。摄取失败或期限已到时不标为 collected，保留结果及 collection_gap，供后续恢复和有限报告使用。

## 5. 跨任务综合

`POST /v1/conversations/{id}/synthesis`：

```json
{"task_ids":["实际任务ID一","实际任务ID二"],"question":"比较共同现象、差异与尚缺的验证"}
```

只读取同会话授权范围内终态任务的报告卡，不拼接 Worker 对话。来源固定 report revision/hash、任务头、构建及 claim 引用；推理完成后重新核对来源。每份报告最多 6 条主张，文本和未解决问题均有上限，并提供完整报告读取地址。模型输出引用未知主张时拒绝；任务被重开或报告变更时拒绝过期结果。

模型启用时使用现有 Gateway、并发限制、上下文/Token/费用预估约束；关闭时返回确定性的来源主张汇总。返回 `cross_task_inference_requires_validation`，保留原报告待解决问题。当前提供 API，没有新增工作台按钮。

## 6. 存储与兼容

- SQLite 自动升级至 schema 24；MySQL 增加 `020_harness_runtime.sql`，创建观察事件和只读作业账本。
- MySQL 迁移器现要求 001–020；未修改已发布迁移，真实隔离库已执行全部迁移。
- NativeMemory 检查点升级到 6，新增 plan_repair_streak，兼容读取版本 1–5；已有权限、反馈、历史来源和检查点恢复测试保留。
- 观察批次的内部评估 ID 与回执存储键分离；回执使用 64 位哈希，适配已有 MySQL CHAR(64)。真实联调发现并修复了该兼容问题。
- 冻结 v1/v2 领域契约和项目依赖未变，增加离线模型评测 CLI 入口。

## 7. 真实验证与效果边界

使用独立容器、独立测试库和临时调查；未启动游戏、未提交真实测试运行，也未操作用户已有 Lyra 数据库。

| 验证 | 实际结果 |
| --- | --- |
| MySQL 8.4，隔离端口 3418 | 001–020 迁移、双连接独占、强制断连后的旧持有者写入拦截、接手恢复、观察批次投递和反馈重试幂等通过 |
| Temporal，隔离端口 7248 | 真实生产 Workflow/Activities 的有限调查、历史回放、Worker 中断后接手和取消后回放通过；模型关闭，真实运行数 0 |
| 本地 Streamable HTTP MCP | 真传输、模拟远端只读作业：隐藏控制工具、提交一次、客户端存储重开、结果采集、清理确认通过 |
| 真实模型 fixed-evidence | deepseek-v4-pro，3 类固定证据 × 标准/小上下文共 6 例，当前保留性验收 6/6；不构成根因准确率 |
| 交付检查 | Ruff、冻结契约、Wheel 构建及隔离导入通过；最终无交互回归 874 passed / 28 skipped，见本地验收清单 |

真实模型当前样本：

| 题型 | 标准上下文 Token / 秒 | 小上下文 Token / 秒 |
| --- | --- | --- |
| 恢复与反证 | 22,562 / 66.150 | 32,683 / 75.605 |
| 基础设施与游戏问题 | 31,682 / 69.687 | 30,504 / 60.285 |
| 冲突运行记录 | 29,089 / 56.882 | 33,155 / 53.605 |

两组为同一改造后 Harness 的不同预算：上下文 32,768/12,000，最多 6 轮、12 次读取、240 秒、0 次真实运行；每例总 Token 预留上限 60,000，6 例合计预留上限 360,000。实际 Token 总数 179,675。没有配置 Token 单价，金额为 null，不能写成零费用。小上下文并未体现更低 Token 消耗。

首轮评测为 5/6，一个小上下文样本只反复修计划而未读原文；记录原样保留。补充计划纠偏后，该失败样本单独复测通过，再次完整 6 例也通过。其中 4 例因轮次预算耗尽交付有限报告，而非提前完成所有问题。随后将纠偏计数从工具缓存迁到独立检查点字段，字段迁移由确定性回归验证，没有再次调用完整模型套件。

当前验收项为读到原始证据、报告有引用、结论等级有界、保留已标注反证及没有真实运行。反证关键词只是保留性探针，不是独立语义裁判。没有同题改造前基线，没有已授权真实游戏故障对照，不能据此声称诊断准确率提高或达到生产吞吐/可用性指标。

## 8. 弹窗修正与验证限制

原生 Windows 沙盒用例会真实启动受约束 Python/PowerShell 及其子进程。本轮开启整套原生用例后产生用户可见的窗口闪烁；已停止该轮，后续不再自动重跑整套原生沙盒测试。

启动代码已收紧：后台 supervisor 使用 CREATE_NO_WINDOW；PSEC 子进程显式设置 SW_HIDE；PowerShell 启动同时设置隐藏窗口参数和 STARTUPINFO，保留原有隔离和资源 Job。该修改有静态检查，未通过重新弹窗验证来宣称桌面效果已确认。原生运行时哈希随源码改变，使用前仍需重新运行 setup_sandbox 的实际验证，不能跳过指纹门禁；本轮未再次启动该验证。

此前一轮完整回归为 898 passed / 3 skipped，包含原生沙盒用例；随后新增接口作用域测试已单独通过。收尾回归关闭原生执行，结果为 874 passed / 28 skipped，耗时 171.35 秒；跳过项不计作通过。真实 Windows 桌面无闪烁验收留待用户允许的环境中进行。

## 9. 复现与产物

```powershell
# 默认回归不启用真实原生沙盒
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe scripts/check_contracts.py
.venv/Scripts/python.exe -m hatchling build -t wheel
.venv/Scripts/python.exe scripts/package_smoke.py

# 实际模型调用：读取现有本机配置，有消耗，固定证据、不启动游戏
$env:PYTHONPATH='src'
.venv/Scripts/python.exe -m game_test_agent.evals.harness_live --output data/local/harness-eval/result.json

# 本地真实 MCP 传输 + 模拟只读作业服务
.venv/Scripts/python.exe scripts/validate_mcp_operations.py --output data/local/mcp-validation/result.json
```

服务验证脚本 `scripts/validate_harness_services.py` 限定独立 MySQL 127.0.0.1:3418 / gta_harness，配置文件只读取 mysql_password；它会创建测试调查并强制断开该测试自身连接。Temporal 地址可用 `--temporal-address` 指定。脚本不会创建容器或迁移数据库，须先准备隔离环境，不能把生产库映射到该地址。

本轮本地产物均在被 Git 忽略的 data/local：

- harness-final-services/result.json：真实 MySQL/Temporal 验证；同目录保存历史回放文件。
- harness-final-mcp/result.json：MCP 传输与作业恢复。
- harness-final-model-suite/result.json：首次 5/6 的原始结果。
- harness-final-model-recheck/result.json：失败样本纠偏后的单独复测。
- harness-final-model-current/result.json：完整 6/6 及各例原始报告、数据库与引用。
- harness-final-noninteractive-tests-20261103.xml：关闭原生沙盒执行的最终回归。
- harness-final-before.zip：修改前文件备份，不含密钥配置。
- harness-final-validation.json、harness-final-source.patch：收尾验收清单与增量源代码补丁。

本轮结束时 Docker 引擎已关闭，停止两个自建隔离容器的请求未能连接；没有重新启动 Docker，也未影响用户 Lyra 容器。下次开启 Docker 后可手动停止 gta-harness-mysql-20261103 与 gta-harness-temporal-20261103。uv 命令当前不可用，未执行 uv lock --check；本轮未修改依赖声明。

凭据配置、数据库、运行证据和容器密码不加入 Git 或源代码补丁。本轮交付完成的是约定框架能力及其已列明验证；平台未提供的长操作控制工具需按协议接入，桌面无闪烁、真实游戏诊断效果和生产容量不在当前通过结论内。
