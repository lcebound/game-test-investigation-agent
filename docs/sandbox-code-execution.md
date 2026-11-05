# Windows 原生代码分析

调查 Agent 可编写 Python 或 PowerShell 分析已授权的证据，查看输出并在预算内有限修复。执行入口共用 `CodeExecutionService`，隔离由 Windows PSEC 与 Job Object 提供。

## 启用

默认关闭。需要支持 PSEC 1.1、文件拒绝和网络 ingress 的 Windows 更新版本，以及本地 Python；每台机器均须通过真实隔离探测。

```powershell
.venv/Scripts/python scripts/setup_sandbox.py
./scripts/start_workbench.ps1
```

安装脚本验证目录拒绝读取、输入不可写、工作目录可写和禁网行为，生成本地配置 `data/local/sandbox.env.json`。启动脚本自动加载它；直接启动 API 或 Temporal Worker 时需加载同一组 `GTA_SANDBOX_*` 变量。设置 `GTA_SANDBOX_BACKEND=disabled` 可关闭。

执行器直接调用系统 API，不依赖 Docker 或额外沙盒 SDK。系统能力不足、隔离探测失败或运行时指纹变化时拒绝执行；升级 Windows 或解释器后应重新运行安装探测并重启服务。

## 使用与权限

每次作业独立保存源码、选定证据与输出。`GTA_INPUT_MANIFEST` 指向只读证据清单，`GTA_OUTPUT_DIR` 指向输出目录。Python 使用 `-I` 模式，`shell` / `powershell` 使用 PowerShell，优先 `pwsh`。代码执行不提供游戏进程或设备访问接口，平台操作仍通过 MCP。

默认只能读取选定输入及运行库、写入作业目录，禁止联网，包括 localhost；不继承模型密钥、平台令牌与代理配置。分析输出按不可信证据处理，成功退出不等同于确认根因。

需要额外目录或网络时，由代码工具提出申请，工作台展示代码、用途和权限，用户选择允许本次或拒绝。授权绑定具体代码、输入、调查与运行时，代码改变后须重新申请；聊天消息不作为审批凭据。

本机开发模式应从启动终端显示的完整链接进入审批页面；其中的审批凭据不可公开分享。生产审批使用所属租户、项目下具有 `agent:analysis:approve` scope 的 JWT 身份。

额外目录只接受存在的本机绝对路径，拒绝盘根、UNC、reparse point 及项目或凭据目录。批准联网会开放本次进程对公网、本机和局域网的访问，没有域名白名单；外部写入不会自动回滚。

## 限额与恢复

Job Object 限制 CPU、内存和进程数量，独立 supervisor 管理墙钟、输出、工作区大小与取消。额度取自已准入作业及调查预算。工作区大小是监测阈值，存在短时超量窗口；批准的外部目录不计入该阈值。

检查点与不可变执行记录用于在 API/Worker 重启后接回原 supervisor；已完成结果可重放，已接受代码不自动重新执行。Supervisor 丢失则失败并清理。跨节点迁移不受支持，作业目录及输出须按保留周期管理。

这是同内核的进程隔离，未提供完整桌面或虚拟机隔离。PowerShell 可能受系统 ConstrainedLanguage 限制；复杂 .NET 操作可改用 Python。调用链与系统边界见[实现参考](windows-sandbox-references.md)。

## 验证

```powershell
$env:GTA_RUN_NATIVE_SANDBOX_TESTS = '1'
.venv/Scripts/python -m pytest -q tests/test_code_execution.py
```

上述测试实际启动原生沙盒；未启用时相关用例跳过。部署与数据库迁移见[运行说明](operations.md)，第三方许可见[声明](third-party-notices/microsoft-mxc.txt)。
