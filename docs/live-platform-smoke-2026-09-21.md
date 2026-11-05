# 2026-09-21 Lyra 平台真实试跑

本记录是一次实际平台任务，不是模拟 MCP 或手写轨迹。平台后端和外部 Worker 使用已有 MySQL 数据库，API 只绑定 `127.0.0.1:18080`；本次临时服务凭据未保存到文件。

- 问题：Windows Client 上的“Lyra 基础测试 - 队伍关系比较”是否通过？能否据此认定特定游戏构建已经验收？
- 平台用例：`case:lyra.cqtest.basic.teams.compare`，`CompareTeams_DistinguishesSameDifferentAndInvalidActors`。
- 调用：平台正式 Integration API 提交一次，`capture_profile=none`，请求超时 180 秒，设备分钟上限 3，未使用离线/模拟设备。
- Action ID：`codex-live-11c7265b7cd24602bf6322e36ae1d894`。
- Run ID：`run-392709d2-eccc-4012-99f4-ef7e2beedc52`；平台原生 Job ID：`e2ac2671-9ccd-4d7c-bf25-b5c3343dae61`。
- 平台结果：`execution_source=real`、`execution_status=succeeded`、`test_verdict=passed`，开始于 `2026-09-21T15:19:06+08:00`，结束于 `15:19:59+08:00`。
- 证据：HTML 报告 `artifact-21748bf6885e84184c28a1fe`，SHA-256 `d73240703aee217c56bf4eadc0aefe110ca8052f37fa20a602336ad43d74eeba`；日志 `artifact-1c82a6920bac303402b351db`，SHA-256 `68eed03859b510a9648bb59ca431dfb2495850549387e695d179d61b072b3cd7`。两份内容由适配器实际读取，哈希核对通过，并摄取为两条 Agent Evidence。

平台结果未返回 `build.hash`、`build.revision`、`environment.environment_fingerprint`（也未返回 `environment.device_model`）。因此只能说“这一次 Win64 用例通过”，不能证明指定构建/环境已验收。日志里的 VisionOS SDK 缺失警告与 Win64 用例通过并不直接矛盾；不能将其当成这个 Windows 用例失败。

模型验收使用用户此前指定的 `qwen3.8-flash`：普通 Chat Completions 正常；现有 JSON Schema 输出模式两次超时。显式 `json_prompt` 模式约 42 秒返回可解析的 `NextStep(kind=request_input)`，要求补 `build.hash`、`build.revision` 和环境指纹，并正确区分 Win64 结果与 VisionOS 警告。模型只看到运行元数据、证据 ID 与简短日志片段，没有看到整份 HTML 报告；这个单次判断不证明多轮调查能力。

范围限制：本次提交由集成 API 手动发起；随后使用新适配器读取、标准化、校验 Artifact 并进入 Agent 的证据上下文。没有验证通用工作流自动发现、自动提交、断点恢复或第二轮实验。平台现有 v1 MCP 工具与通用 v2 MCP 调用形状不一致，接入被隔离在 [Lyra 适配器](../src/game_test_agent/adapters/lyra_integration.py)，没有修改调查内核来迎合平台格式。
