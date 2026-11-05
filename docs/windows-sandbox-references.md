# 独立 Windows 沙盒：实现与参考

本项目的 `windows_psec` 执行器直接调用 Windows 系统 DLL，通过 Python 标准库 `ctypes` 编写绑定和策略编码器。**不调用或链接 Codex CLI、Microsoft MXC SDK、Docker、WSL 或 bubblewrap**。调查模型仍由 Agent 的模型配置决定。

## 实际调用链

`agent_execute_code → CodeExecutionService → WindowsSandboxRunner → 独立 supervisor → WindowsSecurityEnvironment → CreateProcessW → 模型程序`

1. 控制面先固定源码、输入、额度和运行时哈希；需要额外目录或联网时，先取得绑定本次代码的用户审批。
2. 执行器组装只读、可写、拒绝路径和网络策略。`windows_security.py` 自行编码 PSEC 1.1 FlatBuffer；模型不能提交原生策略或修改 capabilities。
3. 使用 `QueryProcessSecurityEnvironmentSupport`、`IsProcessSecurityEnvironmentVersionSupported` 检查能力，通过系统 `processmodel.dll` 的 `CreateProcessSecurityEnvironment` 创建安全环境。
4. 把安全环境句柄、标准流句柄白名单和 Job 句柄加入 `STARTUPINFOEX`，通过 `CreateProcessW(CREATE_SUSPENDED)` 创建受隔离进程。当前 Windows 构建可能不应用 Job-list 属性，因此必须在暂停状态检查 `IsProcessInJob`，必要时显式绑定并再次确认，之后才 `ResumeThread`。检查失败立即终止，不能无隔离继续运行。
5. 系统强制执行 AppContainer 安全上下文以及 PSEC 文件、网络策略；子进程继承隔离，Job Object 限制累计用户态 CPU、提交内存和进程数量。supervisor 管理墙钟、输出、工作区大小、取消和结果收集。
6. Job kill-on-close 和 PSEC terminate-on-close 负责 supervisor 意外退出后的进程回收；API/Temporal worker 重启时可接回仍运行的 supervisor，不自动重跑已接受代码。

默认禁网包括 localhost；批准联网后，本次进程可访问公网、本机及局域网，不提供域名级限制。目录批准仅开放具体目录，不能覆盖项目、数据库、执行记录、审批凭据和常见凭据位置。

## 兼容性与边界

需要提供 PSEC 1.1、文件拒绝和网络 ingress 能力的 Windows 更新版本及本地 Python；最终以安装时的真实隔离探测为准。缺失 DLL/接口、版本不支持、探测失败或运行时指纹改变时均拒绝执行。不会自动改用普通 `subprocess` 执行模型程序。

PowerShell 需要 Win32k/.NET 初始化，因此当前解释器配置不启用 Win32k syscall lockdown，也不宣称桌面或 GUI 隔离。脚本使用 `-NoProfile -NonInteractive`；独立隐藏控制台只用于在 PowerShell 初始化前配置 UTF-8，不修改用户终端编码。可信 UTF-8 启动脚本本身也在 PSEC 内运行，其 PowerShell 子进程继承同一隔离和资源 Job。保留系统可能施加的 ConstrainedLanguage 模式，不修改或绕过 App Control 策略。复杂 .NET 操作受限时模型可改用 Python。

这是同内核的原生进程隔离，不是虚拟机，不用于承诺运行任意恶意程序的绝对安全。默认只读的解释器与系统运行库仍可读取；不会把整个主机文件系统隐藏为一台独立机器。工作区磁盘是监测阈值而非硬配额，批准的外部写目录不计入该阈值，写入也不自动回滚。Python 库目录不是完整供应链锁，依赖安装仍需运维管理。

## 参考资料及采用范围

- [微软 CreateProcessSecurityEnvironment API](https://learn.microsoft.com/en-us/windows/win32/api/processmodel/nf-processmodel-createprocesssecurityenvironment)：安全环境创建及系统 ABI。
- [微软 PSEC 创建标志](https://learn.microsoft.com/en-us/windows/win32/api/processmodel/ne-processmodel-process_security_environment_flags)：terminate-on-close 生命周期行为。
- [Microsoft MXC 的 PSEC schema](https://github.com/microsoft/mxc/blob/main/external/windows-sdk/ProcessSecurityEnvironment.fbs)：FlatBuffer 字段顺序、类型和 PSEC 1.1 协议。
- [Microsoft MXC 的原生绑定](https://github.com/microsoft/mxc/blob/main/src/mxc-sdk/src/backends/process_container/common/secenv.rs)：核对 API 参数宽度、系统 DLL 加载及扩展启动属性；没有引入它的 Rust crate 或可执行文件。
- [Microsoft MXC 的系统版本支持表](https://github.com/microsoft/mxc/blob/main/docs/backends/process-container/os-version-support.md)：部署兼容性参考，不能代替本机隔离探测。
- [微软 AppContainer 实现文档](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)：受限安全令牌及能力访问控制机制。
- [Chromium sandbox 设计](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/docs/design/sandbox.md)：参考可信控制进程与受限执行进程的职责分离，没有复制 Chromium 实现，也不宣称具备相同全部加固措施。
- [PowerShell 语言模式](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_language_modes)：系统 ConstrainedLanguage 的兼容边界。

以上来源于 2026-10-20 检索；本次核对的 Microsoft MXC 源码 commit 为 `706c1b90c45bf977bf6df6610886c510959c54b9`。协议编码参考微软 MIT 授权文件，许可声明见 [第三方声明](third-party-notices/microsoft-mxc.txt)。这是项目编写的执行后端，隔离原语由 Windows 内核和系统服务提供，不称为自行实现操作系统沙盒。
