# Harness 审查修复与验收

日期：2026-11-04。对应[11 月 3 日审查](harness-review-2026-11-03.md)。本批完成约定的代码补齐与本机可执行验证，没有部署或发布到 GitHub。

## 完成矩阵

| 审查项 | 本次实现 | 结果 |
| --- | --- | --- |
| 独立长操作结果漏采 | 恢复采集绕过普通参数读取缓存；确认返回的是本 call_id 的证据 ID 后才 collected | 47/99 分别入证据；第二项失败结果、重启及摄取后确认丢失均有回归 |
| 清理饥饿 | 每项尝试都记录并轮换更新时间，缺失 Provider/配置也会让出批次位置 | 最早四项不可用，第五项仍能在后续批次清理 |
| Local 终态清理 | 生命周期管理的有界恢复循环，扫描已结束调查；持有 Local 独占后仅对账资源 | 不重跑调查、不改变终态；跳过 Temporal 所有者；关闭时取消恢复任务 |
| 恢复容量/时间 | MCP 查询与摄取经过工具准入，RPC 使用剩余期限，截止后不再发查询；取消控制调用共用总期限和工具准入 | 容量耗尽不消耗轮询预算；期限耗尽保留结果与缺口 |
| 观察事件异常闭环 | 新增授权重试接口，封存新批次身份，原始事件及旧评估/回执不变 | 禁止重投已归属或评估中的事件，重复 pending 请求幂等 |
| 工作台可用性 | 事件与综合面板：原事件、归属重试、长操作进度、2–8 个终态任务综合及原报告读取 | 真实无头浏览器操作通过；桌面/手机截图与 JS 错误检查通过 |
| 综合重复消耗 | 按任务头、来源报告哈希、问题及模型配置缓存，进程内串行合并同类请求 | 来源变化仍走核验，不复用陈旧报告；首次生成记审计事件 |
| 运维观测 | 增加观察/作业/路由评估状态、最长在途作业时间、清理尝试及综合/重试事件计数 | 继续按租户/项目过滤；缓存命中不新增模型调用计数 |
| 效果评测 | 六类证据、真实原文引用验证、实际读取/复用/预算收尾指标、固定来源对照和报告哈希绑定的语义标注入口 | 不把 claims 非空当引用有效，不把关键词当语义准确率 |
| 报告收尾 | 最后有效轮次只保留报告工具，并请求指定报告工具；报告 scope 菜单随已知来源收窄 | 没有来源 run_id 时只提供 unknown，不能凭 JSON 内的 ID 伪造运行来源 |
| 沙盒弹窗 | 保留进程层隐藏；移除会使当前 PSEC PowerShell 启动失败的 -WindowStyle 参数 | 原生套件 27 项通过；有界桌面采样未发现新增可见控制台 |
| CI/文档 | 在既有依赖 CI 中接入新 MySQL/Temporal 回归和 MCP 传输验证，增加无头浏览器任务与产物上传 | CI 配置已写入，未声称 GitHub 远端矩阵已运行；统一当前状态入口 |

SQLite schema 24、MySQL 020、NativeMemory 6 不变，无依赖声明或冻结契约变更。浏览器验收使用本机额外安装的 Playwright 1.64.1；CI 使用临时 --with 环境，不加入运行时依赖。锁文件检查通过。

## 验证结果

- 最终完整回归：**910 passed / 5 skipped**，耗时 203.09 秒。显式启用 GTA_RUN_NATIVE_SANDBOX_TESTS=1；跳过的是五项真实依赖集成测试，不是原生沙盒。
- 原生沙盒定向回归：**27 passed**。修正 PowerShell 参数后，中文输出、隔离、资源额度、取消、恢复、进程树清理和 Activity 使用相同 runner 的路径均通过。
- 隐藏窗口实际观察：真实 PSEC Shell 作业执行和 verify 期间，**207 次桌面采样，新增可见控制台窗口 0**。只代表该有界测试，不保证任意程序自行创建 UI 都不可见。
- 真实 Temporal：独立本机 CLI 服务，调查完成、Worker 中断接手、取消、历史回放全部通过，真实游戏运行数 0。服务在验证后关闭。
- 真实 MCP 传输：动态端口的 Streamable HTTP 测试通过，远端是明确标记的模拟只读作业服务；提交、客户端重开、原结果采集及清理确认均通过。
- 真实浏览器：观察原文、重新归属、操作状态、任务多选综合、来源报告、390px 窄屏与无 JS 异常检查通过。
- Ruff、Node 语法检查、冻结契约、Wheel 构建、隔离导入、uv lock --check --offline 通过。

完整回归前曾出现 PowerShell 启动失败，以及旧测试仍要求最后一轮暴露计划编辑工具。前者通过移除不兼容参数解决；后者调整为验证合法收尾菜单，并新增明确要求最后一轮只保留报告工具的回归。没有通过跳过或放宽证据判断规则隐藏失败。

## 真实模型效果记录

使用 deepseek-v4-pro，同一小上下文配置：12,000 上下文、最多 6 轮、12 次读取、240 秒、0 次真实运行，每例最多预留 60,000 Token。最终六例全部通过保留性检查，且都以 native_mcp_investigation_complete 结束，没有预算耗尽收尾。实际共 **143,718 Token / 125.792 秒**；未配置单价，金额为 null。

| 题型 | 实际 Token | 耗时（秒） |
| --- | ---: | ---: |
| 恢复与反证 | 22,830 | 20.931 |
| 基础设施与游戏问题 | 22,492 | 22.047 |
| 冲突运行记录 | 31,283 | 27.439 |
| 未符号化崩溃 | 22,352 | 17.855 |
| 证据不足 | 22,973 | 17.558 |
| 日志提示注入 | 21,788 | 19.962 |

中间失败样本原样保留。新增严格引用检查首先发现原先“有 claims 即有引用”的计分漏洞；进一步检查发现模型在缺乏来源元数据时仍选择 single_run/cross_run，并在最后一轮继续编辑计划或读取。修复针对通用工具菜单和收尾协议，没有修改题目答案或降低原文/来源校验要求。

基线比较仅匹配同 fixture 哈希、模型与上下文变体；当前文件包含三个可匹配旧案例的 Token/耗时差值。这是单次运行观察，不是统计提升，也不能把所有效果归因于某一个修改。

当前可证明引用片段来自已保存原文、字段断言按声明运算核验、反证标签保留、没有运行游戏。仍不能把 6/6 称为根因准确率：例如报告中有结构化字段、未核实解释和未回答的问题账本，它们的证据地位不同。

新增 --adjudications 接受独立评阅结果，要求 fixture_sha256、report_sha256、reviewer、0–1 的 question_coverage 及非负 unsupported_causal_claims；未提供标注时保留 null，绝不自动给满分。样例见 evals/harness-adjudications.example.json。需要人工或独立评阅者根据原文审阅，不能以关键词替代。

## 接口与使用

工作台“事件与综合”入口提供新增流程，普通任务归属仍由 Router 自动判断。重新归属是异常兜底，用户不必常态决定先查谁。

- POST /v1/conversations/{id}/observations/{event_key}/retry：同项目 events:write 权限；只重试 unresolved，pending 重复请求返回 duplicate；归档会话拒绝重试。
- GET /v1/investigations/{id}/operations：最多 16 项状态、轮询次数及清理义务，不返回 Provider 凭据或原请求正文。
- POST /v1/conversations/{id}/synthesis：沿用 2–8 个同会话终态任务的 API；生成结果有版本缓存，不改变原证据或报告。

```powershell
# 本地默认回归
.venv/Scripts/python.exe -m pytest -q

# 有界真实桌面观察；先通过 setup_sandbox 的运行时验证
.venv/Scripts/python.exe scripts/validate_sandbox_windows.py

# 无头浏览器、零模型调用、独立临时数据库
.venv/Scripts/python.exe scripts/validate_harness_browser.py

# 真实 MCP 传输，自动选临时端口
.venv/Scripts/python.exe scripts/validate_mcp_operations.py

# 有消耗的固定证据评测，不连接游戏或代码 runner
$env:PYTHONPATH='src'
.venv/Scripts/python.exe -m game_test_agent.evals.harness_live --variant compact --baseline data/local/harness-final-model-current/result.json --output data/local/review-eval/result.json
```

服务验证脚本现在支持测试配置中的 mysql_port、mysql_database，主机固定 127.0.0.1，数据库名限定 gta_ 前缀。CI 复用自己的随机端口和隔离数据库，不再依赖本机 3418。

## 外部环境边界

当前 Docker Desktop 启动失败于 dockerInference 临时套接字，保留式重命名未成功；自动审批拒绝删除该套接字的动作，因此停止修复该系统文件，没有绕过拒绝、恢复出厂、清理用户容器或修改用户 Docker 配置。后续只关闭了本轮启动的 Docker 界面/后端。

本机 MySQL 3306 可达，但项目没有该服务的配置凭据；默认开发身份只读连接未成功，未继续猜测凭据，也未触碰已有数据库。因此本轮没有重新执行 MySQL/S3 的真实依赖测试。五项集成测试在主回归中跳过；其中 Temporal 的核心恢复/取消/回放已用独立原生服务补验，但不能写成该五项全部通过。

实际 Lyra/游戏服务未提供的长操作控制工具，仍须由平台提供声明的 request_id 幂等、lookup/query/cancel 协议，Agent 不能伪造远端能力。真实游戏问题对照、生产容量/SLO、集群总并发额度仍属于外部接入与上线验收，不是当前进程内机制已经证明的结果。

## 产物

所有本地产物在 data/local，凭据和运行库不加入源代码补丁：

- harness-review-release-tests-20261104.xml：最终完整回归。
- harness-review-native-tests-20261104.xml：27 项原生定向回归。
- harness-review-window-20261104.json：实际窗口观察。
- harness-review-temporal-20261104/result.json 及 histories：真实 Temporal 与回放历史。
- harness-review-mcp-final-20261104/result.json：真实 MCP 传输。
- harness-review-browser-20261104：桌面/手机截图与浏览器结果。
- harness-review-model-release-20261104/result.json：最终六例指标、报告、数据库和来源哈希。
- harness-review-model-20261104、harness-review-model-final-20261104：保留的中间失败结果。
- harness-review-fixes-before-20261104.zip：修改前备份。
- harness-review-validation-20261104.json、harness-review-source-20261104.patch：验收清单、源码哈希和增量补丁。
