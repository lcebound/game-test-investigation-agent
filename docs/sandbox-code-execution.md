# Agent 自主代码分析：Windows 原生沙盒

Agent 可自主编写 Python / PowerShell，分析本次调查已授权、下载的证据，读取 stdout/stderr 后有限修复。原生 MCP 的 `agent_execute_code`、结构化决策的 `NextStep(kind="execute_code")` 和 Temporal Activity 共用 `CodeExecutionService`。

## 环境与启动

执行器由项目自行编写，使用 Python 标准库 `ctypes` 直接调用 **Windows PSEC 原生 API**，不依赖 Codex CLI、MXC SDK、Docker、WSL 或 bubblewrap。调查仍使用项目配置的模型，例如 Qwen。调用链、官方参考及边界见 [独立实现说明](windows-sandbox-references.md)。

需要提供 PSEC 1.1、文件拒绝和 ingress 能力的 Windows 更新版本以及本地 Python。安装脚本先探测接口和真实隔离行为，不安装系统组件或第三方沙盒运行时。当前机器 Windows build 26200 已通过；其他机器必须重新探测。

```powershell
py -3.12 scripts/setup_sandbox.py
./scripts/start_workbench.ps1
```

安装先检测目录拒绝读取、输入不可写、工作目录可写和本机网络连接被拒绝，再生成 `data/local/sandbox.env.json`。本机已通过。启动脚本已加载这个独立配置，正在运行的工作台需重启；配置不含模型或平台凭据。直接启动 API/Temporal worker 需自行加载其中的 `GTA_SANDBOX_*` 环境变量。`GTA_SANDBOX_BACKEND=disabled` 可关闭代码工具。

本机开发模式请从 `start_workbench.ps1` 终端输出的完整工作台链接进入。链接携带只交给浏览器的本机审批凭据，打开后从地址栏移除，存于该标签页的 sessionStorage；它不进入普通 workspace 响应、模型上下文或沙盒环境。不要向 Agent 粘贴或公开分享该入口。直接使用普通地址的页面可以查看调查，但不能凭伪造开发身份头批准权限。

自定义解释器使用 `--python-path`。运行时指纹固定系统 `processmodel.dll`、Python、PowerShell 可执行文件、Windows build 和本项目隔离实现；不等同于整个 Python 安装的依赖锁，解释器目录中的库还需运维管理。升级后重新运行 setup 并重启服务。PSEC 不可用、权限探测失败或指纹不匹配均拒绝执行，没有低权限模式或无隔离回退。

## 模型如何使用

Harness 向模型提供代码工具、Windows 环境、可选证据和额度。示例：

```json
{
  "language": "python",
  "purpose": "分析采样数据",
  "input_evidence_ids": ["本次调查中的证据ID"],
  "code": "import json, os\nfrom pathlib import Path\nm=json.loads(Path(os.environ['GTA_INPUT_MANIFEST']).read_text())\nprint(Path(m['evidence'][0]['path']).read_text())",
  "repair_of_job_id": null
}
```

`GTA_INPUT_MANIFEST` 指向只读清单，包含 `evidence_id/path/sha256/mime_type`；`GTA_OUTPUT_DIR` 指向结果目录。当前目录可写，每次任务独立，不直接读取项目源码、用户目录或上次任务文件；继续分析旧结果需显式选择其派生证据 ID。

Python 以 `-I` 模式启动；`shell` / `powershell` 都是 PowerShell，优先 pwsh，否则 Windows PowerShell。没有 Linux `/bin/sh`，默认不能联网安装依赖。可解析日志、关联数据、计算统计及生成 JSON/CSV；额外目录和联网必须先审批。审批不提供游戏进程/设备访问接口，也不注入平台凭据；当前未实现 GUI/桌面隔离；平台查询、补测仍通过 MCP 工具。

源码和输入先进入不可变作业记录，再经独立 supervisor 创建安全环境，将环境句柄附加至 `CreateProcessW`，在暂停状态确认资源 Job 绑定后恢复执行。模型代码不会插入宿主 PowerShell 命令。反馈包含 `job_id/execution_status/exit_code/failure_kind/stdout/stderr/output_evidence_ids`；stdout/stderr 各预览1800字节，完整捕获内容作为证据保存。报错后可修改代码并填写 `repair_of_job_id`，默认同一修复链最多2次，分支同样计数，同代码同输入重复提交被拒绝。代码工具消耗调查轮次、工具额度和时间。

## 隔离、限额与恢复

| 边界 | 当前实现 |
| --- | --- |
| 文件 | PSEC 文件策略拒绝盘根，仅向解释器/系统运行库和选定输入授予只读访问，本作业工作目录可写；用户可单次批准其他具体目录的读或写权限 |
| 网络与凭据 | 默认禁网，包括 localhost；单次代码经用户明确批准后可联网。仍只传必要 Windows 环境变量，不继承 Agent API key、MCP 凭据及代理配置 |
| 进程 | 创建时附加 PSEC 并暂停，在确认 Windows Job Object 成员关系后恢复；后代继承隔离和 Job，无 breakaway 授权；PSEC terminate-on-close 覆盖创建与绑定之间的崩溃窗口 |
| 默认额度 | 累计用户态 CPU 30秒、Job 内存512 MiB、16进程、60秒墙钟、256 KiB总捕获输出；包含启动器开销 |
| 工作区 | 默认64 MiB，由独立 supervisor 监测，超过后终止；**不是 NTFS 硬配额，存在短时超量窗口**；同时限制目录项数量 |
| 代码与输入 | 最多32 KiB代码、20项输入、输入共32 MiB；校验调查/项目范围、授权及 SHA-256 |
| 输出 | 最多16个普通结果文件；拒绝 reparse point、符号链接、硬链接、子目录及不安全文件名；停止整个进程树后再由宿主复制 |
| 取消与崩溃 | 独立 supervisor 负责时间/输出限制和取消；Job kill-on-close 在 supervisor 崩溃时终止整个进程树 |

API/Temporal worker 重启不主动终止 supervisor。不可变 spec、确定的 operation/fencing identity 和独占接受记录用于接回同一作业；已完成结果直接重放。Supervisor 丢失时任务失败并清理，**不会自动重新执行已接受代码**；可由模型在预算内显式修复。PID 加创建时间校验避免 PID 复用。失败恢复审计可在墙钟期限之后完成，不会把超时结果判为成功。

Cleanup Ledger 确认进程回收后完成清理；原始工作目录和结果保留用于审计，需运维按保留周期删除。跨节点部署必须保留同一 Windows 执行所有者及 spool，不支持分布式迁移。已验证共用的 Temporal Activity 入口，未验收真实 Temporal server 故障恢复。

这里是同内核的原生进程隔离，不宣称微虚拟机或完整桌面隔离。为兼容 PowerShell，未开启 Win32k syscall lockdown。PowerShell 使用独立隐藏控制台和受隔离的 UTF-8 启动脚本，保留系统 ConstrainedLanguage 限制；一般 cmdlet 可用，复杂 .NET 调用可能受限，可改用 Python。

## 向用户申请权限

基础沙盒内的代码直接执行；确需外部目录或联网时，模型在代码工具中提出申请：

```json
"permissions": {
  "directories": [
    {"path": "D:\\GameLogs", "access": "read"},
    {"path": "D:\\AnalysisResults", "access": "write"}
  ],
  "network": true,
  "reason": "读取游戏日志、保存统计结果并下载公开对照数据"
}
```

工作台展示完整代码、用途、原因和权限，提供“仅本次允许”和“拒绝”。等待期间不会创建执行作业、启动脚本或开放权限；等待计入调查总时间。申请默认10分钟内有效，且不得晚于调查截止时间。拒绝、过期或取消后返回 `not_executed`。聊天中的“已同意”、工具返回文本及模型输出都不能成为授权凭据。

审批通过独立接口提交：

- `GET /v1/investigations/{id}/code-permissions`
- `POST /v1/investigations/{id}/code-permissions/{approval_id}/decision`，提交 `request_hash`、`decision: approve|deny` 和可选 `reason`。

审批身份必须属于相同租户/项目并具有 `agent:analysis:approve` scope；生产环境由 IdP 签发 JWT，不信任浏览器自报的 scope。默认本机开发身份包含该 scope；自定义身份需自行配置。请求 hash 防止审批旧内容，状态通过数据库条件更新，只能最终决定一次；同一审批人的相同决定可幂等重放。审批与审计在同一事务中保存。

开发 Header 身份之外，审批接口还必须携带 `X-Code-Approval-Key`，以常量时间比较独立的随机凭据。它由本机启动流程读取受保护的 `code-approval.key`，默认 setup 配置为 `data/local/code-approval.key`，可通过 `GTA_SANDBOX_APPROVAL_KEY_FILE` 指定。浏览器只在审批请求上发送此凭据，普通 API 不返回它。即使已允许某段代码访问 localhost，它也不能仅靠自报 `X-Scopes` 批准另一任务。生产 JWT 模式不使用这个开发凭据；本机开发模式仍以可信操作系统用户为前提，不代替生产身份体系。

批准绑定调查、job、decision、完整代码哈希、输入证据哈希、用途、权限和运行时指纹，不可用于另一份程序或任务。代码改动/修复需要新的申请。控制面和 Runner 均复核审批，Runner 每次实际启动前校验本地目录及有效期。待审批记录可在进程异常退出后恢复；正常停止会取消正在等待的申请。已完成结果直接重放，不重新执行代码，也不要求外部目录继续存在。

只支持存在的本机绝对目录，不接受盘根、UNC 路径或路径组件中的 reparse point。拒绝覆盖 Agent 项目、runner spool、Artifact Store、系统/解释器及常见凭据目录；不能借用户目录的宽泛授权覆盖它们。`write` 允许读取和修改该目录。联网为本次进程的完整网络权限，**包含 localhost/局域网，无域名白名单**，工作台明确展示这个范围。

审批不取消 PSEC 隔离、Job 资源限制或凭据过滤，不触发系统管理员/UAC 提权。工作区磁盘监测不覆盖批准的外部写目录；外部文件修改不会自动回滚。额外目录中的文件是活动数据，不会自动全部快照成证据，结果仍需独立验证。

代码执行权限记录最初引入 SQLite schema 17 / MySQL `013_code_permissions.sql`。当前版本还包含会话多任务、反馈协调和历史引用：SQLite 自动升级到 schema 20；MySQL 部署须完成 `016_investigation_recall_links.sql` 后再启动 API/Worker，见[第八批实施记录](harness-phase8-implementation-2026-11-02.md)。

## 证据与验证

源码、输入、运行时指纹及结果关联到同一作业。结果标记 `untrusted`，不会因程序成功退出就晋升为 Fact 或确认根因，仍需原始证据核对、独立验证或对照实验。历史外部 image-based runner 契约仍可读，内置 Docker 执行器已删除。

```powershell
$env:GTA_RUN_NATIVE_SANDBOX_TESTS='1'
py -3.12 -m pytest -q tests/test_code_execution.py
py -3.12 scripts/sandbox_model_smoke.py
```

测试实际运行 Windows PSEC，覆盖 Python/PowerShell、修复、输入只读、凭据不继承、禁网、限额、恢复和取消。实时模型脚本使用配置模型、明确标注的合成 MCP 数据和实际原生沙盒，结果在 `data/native-sandbox-smoke/`；这验证执行闭环，不代表真实游戏诊断效果。未显式启用时原生测试跳过。

迁移前使用 Codex CLI/MXC 的历史记录继续保留：`data/native-sandbox-smoke/20261020-184100/summary.json` 和审批 UI 记录 `data/permission-migration/ui-20261020-185359/summary.json`。这些记录不作为独立 PSEC 后端的验收证明，旧 `windows_mxc` 结果仅为历史兼容而可读，新任务统一标记 `windows_psec`。

独立 PSEC 后端验收（2026-10-20）：全量 **361 passed**，1 条既有 Starlette 弃用警告，原生测试显式开启。覆盖 Python、PowerShell 7/5.1 Unicode、子进程 AppContainer 令牌和文件/网络策略继承、运行时变更拒绝执行、PSEC terminate-on-close、资源限制、崩溃/取消和现有完整审批测试。Ruff、Python 编译与 doctor 通过，doctor 标记 `isolation_kind=windows_psec`。

真实 `qwen3.8-flash` 调用记录：`data/native-sandbox-smoke/20261020-192203/summary.json`，执行器为 `windows_psec`。模型编写的 Python 一次成功，输出 `comparison.json`：before/after 均值 15/50、比值 3.333333、变化 233.333333%，并完成引用报告；实际 Job 采集到 CPU 和峰值内存用量。输入是明确标注的合成 MCP 样本，不能据此宣称真实游戏故障诊断效果。该目录另导出了实际源码和 JSON 结果。

工作台审批浏览器回归：`data/permission-migration/ui-20261020-192159/summary.json`，批准和拒绝均成功、无 JavaScript 异常，同目录保留截图。该浏览器 fixture 不启用模型和代码执行；实际权限效果由原生测试验证。

实现与原始资料见 [独立 Windows 沙盒](windows-sandbox-references.md)。
