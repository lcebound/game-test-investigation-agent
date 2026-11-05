# 运行与部署说明

## 进程与权威边界

| 进程 | 职责 | 不负责 |
| --- | --- | --- |
| `game-test-agent` | HTTP Intake、查询投影；local 模式下执行单进程闭环 | 不应与 Temporal 同时处理同一调查 |
| `game-test-agent-temporal-worker` | 持久工作流 Activities、MCP 调用、证据和报告 | 不直接消费 RabbitMQ |
| `game-test-agent-bridge` | RabbitMQ Inbox/Outbox、启动/命令投递 | 不替代 Workflow 状态权威 |
| `game-test-reference-mcp` | 协议测试替身 | 不代表真实引擎或真实性能结果 |
| 外部 analysis runner/broker | 隔离执行已准入的分析作业、分配 fencing token、上传输出和回收沙箱 | 不属于本仓库内置进程；不能继承 Agent 凭据或宿主控制接口 |

Temporal Workflow 是控制状态权威，Agent 数据库是查询投影及动作/证据领域账本，provider 是外部运行状态权威。禁止用其中一方的状态无条件覆盖另外两方。

## 关键环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `GTA_ENVIRONMENT` | `development` | `development`、`test` 或 fail-closed 的 `production` |
| `GTA_WORKFLOW_BACKEND` | `local` | `local` 或 `temporal` |
| `GTA_DATABASE_BACKEND` | `sqlite` | `sqlite` 或 `mysql` |
| `GTA_DATABASE_PATH` | `data/game_test_agent.db` | SQLite 开发数据库 |
| `GTA_MYSQL_HOST/PORT/DATABASE/USER/PASSWORD` | 无 | MySQL 迁移和运行时 Repository 共用配置 |
| `GTA_MYSQL_SSL_CA` | 无 | 非 loopback MySQL 强制提供 CA 文件 |
| `GTA_ARTIFACT_BACKEND` | `local` | `local` 或 `s3` |
| `GTA_ARTIFACT_ROOT` | `data/artifacts` | 本地内容寻址 Artifact Store |
| `GTA_ARTIFACT_MAX_BYTES` | `134217728` | 单个 artifact 最大字节数 |
| `GTA_S3_BUCKET/PREFIX/REGION` | 无/`game-test-agent`/无 | S3 存储位置 |
| `GTA_S3_SERVER_SIDE_ENCRYPTION` | `AES256` | `AES256` 或 `aws:kms` |
| `GTA_S3_KMS_KEY_ID` | 无 | 使用 `aws:kms` 时必填 |
| `GTA_AUTH_MODE` | `dev_headers` | `dev_headers` 或 `jwt`；生产强制 `jwt` |
| `GTA_JWT_JWKS_URL/ISSUER/AUDIENCE` | 无 | JWT 校验参数；JWKS 必须为 HTTPS |
| `GTA_LOG_LEVEL` | `INFO` | JSON 结构化日志级别 |
| `GTA_MCP_PROVIDERS_JSON` | `[]` | provider allowlist；配置格式见下文 |
| `GTA_EVENT_SOURCE_POLICIES_JSON` | `[]` | 运维授予的事件源项目与 scopes |
| `GTA_TEMPORAL_ADDRESS` | `127.0.0.1:7233` | Temporal frontend |
| `GTA_TEMPORAL_NAMESPACE` | `default` | Temporal namespace |
| `GTA_TEMPORAL_TASK_QUEUE` | `game-test-agent` | Worker task queue |
| `GTA_RABBITMQ_URL` | 本地 guest URL | 生产环境应通过密钥注入 |
| `GTA_RABBITMQ_EXCHANGE` | `game_test.events.v1` | durable topic exchange |
| `GTA_PLATFORM_ID` | `local` | 路由段，只允许小写字母、数字和连字符 |
| `GTA_ENABLE_MODEL` | `false` | 显式开启 Agents SDK 推理 |
| `GTA_MODEL` | `gpt-5-mini` | 模型配置，不写入业务事件 |
| `GTA_MODEL_API` | `responses` | `responses` 或 OpenAI 兼容的 `chat_completions` |
| `GTA_MODEL_TIMEOUT_SECONDS` | `60` | 单次模型请求超时，超时后生成有边界的失败报告 |
| `GTA_MODEL_MAX_OUTPUT_TOKENS` | `4096` | 单次模型输出 token 上限 |
| `GTA_MODEL_MAX_TOTAL_TOKENS` | `250000` | 每项原生 MCP 调查累计 Token 上限，含失败/中断预留 |
| `GTA_MODEL_MAX_ESTIMATED_COST` | 无 | 原生调查估算费用上限；需同时配置下面两项正单价 |
| `GTA_MODEL_COST_PER_INPUT_TOKEN` / `GTA_MODEL_COST_PER_OUTPUT_TOKEN` | `0` | 运维提供的每 Token 单价，币种需一致 |
| `GTA_MODEL_REASONING_EFFORT` | 无 | 可选：`none`、`minimal`、`low`、`medium`、`high`、`xhigh` 或 `max` |
| `GTA_SANDBOX_BACKEND` | `disabled` | `windows` 启用原生执行，旧 `docker` 配置需运行 setup 迁移 |
| `GTA_SANDBOX_RUNTIME` | `psec` | 自有 PSEC 绑定，不降级 |
| `GTA_SANDBOX_RUNTIME_DIGEST` | 无 | setup 生成的原生运行时指纹 |
| `GTA_SANDBOX_PYTHON_PATH` | 当前 Python | 本机 Python 可执行文件；不再配置 Codex 路径 |
| `GTA_SANDBOX_IMAGE_DIGESTS` | 无 | 外部镜像 runner 的历史 allowlist，原生执行无需镜像 |
| `GTA_SANDBOX_RUNNER_IDS` | 无 | 允许的 runner，须同时配置 runtime 指纹或镜像 allowlist |
| `GTA_SANDBOX_MAX_*` | 见 Settings | 作业和执行额度；磁盘是监测阈值，非硬配额 |

`GTA_ENVIRONMENT=production` 会强制 `temporal + mysql + s3 + jwt`，任何开发后端或缺失安全配置都会在进程启动前失败。JWT 使用固定 RS256 allowlist，同时校验签名、issuer、audience、`exp`、`iat`、`sub`、租户、项目列表和 scopes。开发 Header 身份模式默认只允许 API 绑定 loopback。

运行 `scripts/setup_sandbox.py` 验证原生 PSEC 隔离并生成 `GTA_SANDBOX_BACKEND=windows` 配置后，模型可编写 Python / PowerShell 离线执行。`start_workbench.ps1` 自动加载 `data/local/sandbox.env.json`，已有进程需重启；直接启动 API/Worker 需加载相同配置。执行器直接调用 Windows PSEC API，不依赖 Codex CLI 或 MXC SDK。权限探测失败时拒绝执行，不回退到 Docker、WSL 或无隔离进程。运行时升级后重新运行 setup。限制与恢复边界见 [自主代码分析](sandbox-code-execution.md)。已有外部 runner 回调接口继续支持 `AnalysisJobSpec`、`SandboxOperationRef` 和 `AnalysisJobResult`；提交、runner 回调和事实复核分别使用 `agent:analysis:submit`、`agent:analysis:runner`、`agent:analysis:verify` scope。

测试平台仅通过 MCP 连接，配置项为 `GTA_MCP_PROVIDERS_JSON`。旧的非空 `GTA_INTEGRATION_PROVIDERS_JSON` 会在启动时被拒绝；用户访问 Agent 的 API 与 RabbitMQ 事件入口不受影响。

代码申请额外目录读写/联网时，工作台展示代码与范围，具有 `agent:analysis:approve` scope 的同项目用户可批准或拒绝。生产 JWT 必须由 IdP 授予该 scope，普通用户/事件源不应默认获得。批准只对该代码任务有效，默认10分钟内启动，绑定代码、输入哈希及运行时；拒绝/过期/取消不执行。联网批准包含本机/局域网，无域名限制；不注入服务凭据。外部目录写入不受工作区磁盘监测覆盖，也不自动回滚。详细接口及边界见沙盒文档。SQLite 自动升级至17；MySQL 先执行迁移013，API 和 Worker 使用相同数据库。

本机开发审批另需独立 `X-Code-Approval-Key`，只自报开发 scope 无法批准。启动脚本输出的完整链接会将凭据交给当前浏览器标签页，不在 API workspace、模型或沙盒中披露。默认密钥文件为 setup 配置的 `data/local/code-approval.key`，运行时禁止授予这个文件所在目录的访问权；删除密钥文件后下次启动生成新凭据，旧标签页需重新打开终端入口。不要把入口链接作为日志/线索发给 Agent。生产环境使用 JWT 审批角色，开发凭据不替代 IdP。

provider 配置示例：

```json
[
  {
    "provider_id": "platform-a",
    "url": "https://mcp.internal.example/mcp",
    "project_ids": ["game-a"],
    "token_env": "PLATFORM_A_MCP_TOKEN",
    "max_response_bytes": 2097152
  }
]
```

`contract` 默认为 `generic_v2`；Lyra 平台现有 MCP 服务使用 `lyra_v1`，并且每个配置只授权一个项目。可选 `tests_search_tool` 和 `source_run_get_tool` 分别提供目录和原始运行读取。Lyra 的这两项由适配器绑定既有 MCP 工具，无需另填。API、Worker 和 doctor 均使用相同 MCP 配置。

只有显式配置 `allow_insecure_localhost=true` 的 loopback 地址可以使用 HTTP；其他地址必须使用 HTTPS。URL 不能携带用户信息或 fragment。凭据值不进入 JSON，只引用大写环境变量名。

## 启动顺序

local 开发模式：先启动所需 MCP provider，再启动 API。Temporal 模式：先确保数据库、Temporal、RabbitMQ 和 provider 可用，然后启动 Worker、Bridge，最后启动 API。API、Worker 和 Bridge 必须使用相同的业务数据库配置。

启动前执行：

```powershell
game-test-agent-doctor --network
game-test-agent-eval evals/baseline.json --output eval-result.json
```

## 生产门禁

上线前至少验证：

1. 同一 `Idempotency-Key` 重放不创建第二个调查，payload 冲突返回 409。
2. 同一 `action_id` 重放不创建第二个运行；提交响应丢失后通过 lookup 找回原 operation。
3. Worker 在外部运行中重启后，能从动作账本继续 query 并生成报告。
4. 模拟、缺引用、构建或环境失配的证据不能发布为已验证结论。
5. 预算耗尽后不再产生写动作；取消后未解决的清理项保持可查询。
6. RabbitMQ 重复、乱序、拒绝消息和 Outbox 重试经过真实环境演练。
7. Temporal 版本升级完成 replay/兼容测试，未完成 Workflow 不永久卡住。
8. Unreal 与 Unity 分别完成真实 provider 的发现→执行→证据→报告验收后，才能声明真实引擎支持。

## 数据库说明

`deploy/mysql/001_initial.sql` 是 MySQL 8 的初始领域 Schema。`game-test-agent-mysql-migrate` 使用数据库全局锁串行迁移，保存 SHA-256 校验和，重复执行会跳过相同版本，已执行文件被修改则拒绝继续。远程 MySQL 必须配置 `GTA_MYSQL_SSL_CA`。

完成迁移后设置 `GTA_DATABASE_BACKEND=mysql`，API、Worker、Bridge 和 doctor 会通过同一 MySQL Repository adapter 执行业务事务。该 adapter 将参数占位符、upsert 和 UTC 时间转换为 MySQL 表达，连接使用 `utf8mb4`、显式事务及 `SERIALIZABLE` 隔离；部署仍须在目标 MySQL 版本上做并发、死锁重试、容量和恢复演练。

案例记忆的候选写入和人工审核分别要求 `agent:memory:write` 与 `agent:memory:review` scope。生产 IdP 应只向案例维护服务或审核角色授予这两个 scope。来源调查因隐私、错误或保留策略被撤回时，调用领域层的来源撤回流程生成 `retracted` revision；不要直接删除最新一行，否则会破坏审核轨迹。历史案例只用于规划提示，运行报告和目标判定仍必须引用当前调查的 Evidence/Fact。

## Artifact Store、身份与日志

`GTA_ARTIFACT_BACKEND=s3` 时，证据按 `prefix/tenant/project/hash-prefix/sha256` 内容寻址。写入携带 SHA-256 checksum 和元数据，读取限制最大字节数并重新计算摘要；默认使用 SSE-S3，也可配置 SSE-KMS。AWS 凭据不进入应用 JSON 配置，应由实例身份或组织密钥注入机制提供。

生产 API 使用 Bearer JWT。token 的 `tenant_id`、`project_ids` 和 `scope`/`scopes` 是授权来源；`X-Tenant-ID` 与 `X-Project-ID` 仅用于从已授权范围选择当前作用域，不能扩权。所有 HTTP 响应返回或生成 `X-Request-ID`。API、Worker 和 Bridge 输出 JSON 日志，结构化字段中的 token、password、secret 等键会递归脱敏；不要把凭据拼入自由文本日志消息。

## RabbitMQ 隔离与重试

每个输入 quorum queue 会声明对应的 `<queue>.dead-letter` quorum queue，并绑定到 `<exchange>.dead-letter`。JSON/Schema/幂等冲突等不可处理消息直接 reject 到 DLQ；暂时性内部错误先 requeue，默认超过 5 次投递后由 broker 转入 DLQ。DLQ 不由 Agent 或模型自动重放。

向已有 RabbitMQ 环境引入 `x-delivery-limit` 和 dead-letter 参数时，若原队列参数不同，broker 会拒绝重新声明。上线应通过受控拓扑迁移或新版本队列完成，不能在运行中直接覆盖参数。

Workflow start、signal command 和结果事件的 Outbox 失败会记录 `attempts`、`last_error`、`next_attempt_at`，按 1、2、4 秒逐级退避，最高 300 秒。`/internal/metrics` 区分 pending 与 retrying 项。调查已经终态时，尚未发送的迟到命令会标记为 `obsolete`，不会向已关闭的 Temporal Workflow 永久发送。

Temporal Workflow 在构造时即建立 signal 状态，因此启动确认与早到 signal 交错时不会因未初始化而失败；每个 command ID 只推进一次 revision。`state` query 只返回取消标志、输入/证据 revision 和已处理 signal 数量，不暴露原始用户内容。

主 Workflow 每经过固定数量的调查轮次使用带版本 patch 的 Continue-As-New 截断历史。续接输入保留原始墙钟截止时间、下一计划 revision、尚未消费的 signal、已处理 command ID 水位和 signal revisions；预算计数继续以数据库账本为准，不会因续接清零。目标 Temporal 集群上线前仍必须用真实历史完成 replay/兼容和 Worker 中断恢复演练。

## 故障与恢复

- MCP 写调用发生连接丢失时，动作先记为 `unknown`，只有支持 lookup 且查回相同 `action_id` 时才继续；不得生成新 key 盲重试。
- RabbitMQ 不可用时保留 Outbox，平台正常测试不应依赖 Agent 即时可用。
- Temporal 不可用时 API/Bridge 保留启动和命令意图，恢复后再投递。
- provider 不可用时不默认换 provider 重做已有动作，以免产生双重副作用。
- 本地和 S3 Artifact Store 都在读取时复核内容哈希；对象被篡改或引用逃逸会直接失败。
