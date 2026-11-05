# 2026-09-22 通用工作流发起真实平台任务

此次由本项目调查 API 创建调查，使用本地 `InvestigationController` 的通用路径完成能力发现、决策冻结、动作规划、预算预留、提交、轮询、运行快照、产物摄取和报告。API 在进程内通过 `TestClient` 调用，不等同于部署后跨进程/Temporal 验收；推理使用确定性 fallback，未调用 LLM。

- 问题：执行 Lyra Windows Client 的队伍关系比较用例，并仅对本次运行给出有证据边界的结果。
- 用例：`case:lyra.cqtest.basic.teams.compare`；`target_kind=windows_client`；`capture_profile=none`。
- 调查：`inv-7b84d474c9fd434d86bee83786b6df17`。
- 决策：第 1 轮 `experiment`，第 2 轮 `report`；动作 `act-7b84d474c9fd434d-001-001`。
- 平台 Run：`run-3b28b799-7530-47c4-ac47-6273f47ebc13`，结果修订 1→2→3（queued→running→succeeded）；终态 `execution_source=real`、`test_verdict=passed`。平台时间为 `2026-09-21T16:12:09.143333Z` 到 `2026-09-21T16:12:34.227885Z`，即香港时间 9 月 22 日 00:12 左右。
- 两份真实产物已摄取：日志 `artifact-5ce4341eba38cc40b8bddee8`，SHA-256 `3c47bab0e3efe9420f8725d2e6ae5e116bcd495fa774a4a27198aea742521862`；HTML 报告 `artifact-22dd3537e798e370fc6352ee`，SHA-256 `d73240703aee217c56bf4eadc0aefe110ca8052f37fa20a602336ad43d74eeba`。两份均为 `real` 来源、`ready`、`partial` 质量，保留了同一平台 Run 的引用。
- 调查终态为 `completed`，报告结论为 `inconclusive` / `observation`：用例通过是平台运行结果，但请求未提供可计算的 `objective_spec`，且平台没有返回构建哈希、版本与环境指纹，因此通用报告没有宣称特定构建已经验收。

第一次工作流尝试 `inv-81ad0db6cefc47489205aebf2f4f3738` 在本地预算预留阶段停止，没有提交平台：平台能力声明预估成本 10 设备分钟，而调查预算只有 3 分钟。第二次将调查预算设为 10 分钟，仍限制最多 1 次真实运行、单次平台执行超时 180 秒，随后得到上述唯一真实运行。平台最终队列为 0，运行总数中的 passed 从 110 增至 111。

可复现入口为 [脚本](../scripts/live_workflow_smoke.py)：它从环境加载可选 provider 配置和服务 token，不会把凭据写入本地数据或输出。平台专有字段只出现在适配器/装配层；调查控制器、策略和报告仍使用通用契约。
