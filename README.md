# Game Test Investigation Agent

参考 Super-agent 架构的游戏测试调查 Agent：通过 MCP 接入测试平台，围绕异常组织取证、分析与补测，输出带证据引用的调查报告。

- **统一入口**：主会话协调多个独立调查，支持外部事件触发和人工跟进。
- **自主调查**：Router–Worker 上下文隔离，按需召回证据，支持预算控制、检查点恢复与幂等投递。
- **监督工作台**：集中查看待关注事项、后台调查、事件、授权、证据和报告。

## 本地部署

以下命令适用于 Windows PowerShell，要求 Python 3.11+，建议使用 3.12。默认使用 SQLite 和本地工作流，不需要 Docker。

```powershell
git clone https://github.com/lcebound/game-test-investigation-agent.git
cd game-test-investigation-agent
$env:PYTHONUTF8 = "1"
python -m pip install uv==0.13.6
uv sync --locked --extra dev --no-editable

New-Item -ItemType Directory -Force data/local | Out-Null
Copy-Item configs/workbench.env.example.json data/local/workbench.env.json
Copy-Item configs/providers.native.example.json configs/providers.local.json
```

编辑两个本地配置：

1. `data/local/workbench.env.json`：填写模型名称和 `OPENAI_API_KEY`，将 `GTA_ENABLE_MODEL`、`GTA_ENABLE_SEMANTIC_ROUTER` 设为 `"true"`。兼容接口可增加 `OPENAI_BASE_URL`。
2. `configs/providers.local.json`：填写实际 MCP 服务地址、项目 ID 和允许的工具；平台令牌使用 `token_env` 引用环境变量。示例工具名称需按平台实际能力调整。

```powershell
./scripts/start_workbench.ps1 -ProviderConfig configs/providers.local.json
```

打开 `http://127.0.0.1:8080/`。首次可保持模型关闭，先检查工作台和平台连接；开启模型后会产生实际调用费用。

不使用 PowerShell 启动脚本时，可把 `GTA_MCP_PROVIDERS_JSON` 设为 Provider 数组后运行 `.venv/Scripts/python -m game_test_agent`；Linux 使用 `.venv/bin/python`。程序自动读取 `data/local/workbench.env.json`，显式环境变量优先。

## 可选部署

- 外部事件自动调查需配置事件来源及授权，见[统一协调入口](docs/unified-router-implementation-2026-11-05.md)。
- Windows 原生代码分析默认关闭，启用前先按[沙盒说明](docs/sandbox-code-execution.md)验证运行时；核心服务不依赖 Windows 沙盒。
- 长期服务或多进程部署可选 Temporal、MySQL、S3 和 RabbitMQ，见[运行部署说明](docs/operations.md)。生产模式必须配置对应的存储、认证和权限，不能直接将本地开发身份模式暴露到公网。

## 验证

```powershell
.venv/Scripts/python -m pytest -q
.venv/Scripts/python scripts/check_contracts.py
```

默认测试不调用真实模型或游戏平台；原生沙盒和真实依赖测试需单独启用。模型评测结果不等同于真实游戏根因定位准确率。

[前端与验收记录](docs/supervisor-ui-implementation-2026-11-05.md) · [当前实现状态](docs/implementation-status.md)

架构参考：[DeepSeek Super-agent 公开研究原型](https://github.com/deepseek-ai/dsh-superagent-prototype)。本项目使用现有 Python 运行时进行适配；第三方说明见 [third-party notices](docs/third-party-notices/)。
