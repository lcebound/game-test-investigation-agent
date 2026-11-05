from __future__ import annotations

from game_test_agent.contracts.models import DiagnosticPlaybook, PlaybookExperimentTemplate


DiagnosticStrategy = DiagnosticPlaybook


PLAYBOOKS: dict[str, DiagnosticPlaybook] = {
    "test_failure": DiagnosticPlaybook(
        playbook_id="functional-failure",
        version="1.0.0",
        anomaly_kind="test_failure",
        applicable_when=["functional test reports a failed assertion or unexpected state"],
        initial_checks=["断言与失败步骤", "前置状态", "测试版本", "随机种子", "最近变更"],
        required_fields=["test_id", "test_version", "failure_step", "build_id"],
        valuable_investigations=["固定种子复现", "隔离前置条件", "正常构建对照"],
        experiment_templates=[
            PlaybookExperimentTemplate(
                template_id="fixed-seed-reproduction",
                question="固定随机性和前置状态后失败是否仍可复现？",
                capability_operation="experiment",
                controls=["build", "environment", "test_version", "preconditions"],
                changed_variables=["random_seed"],
                required_evidence_types=["functional_result", "runtime_log"],
                outcome_interpretation={
                    "reproduced": "支持缺陷在受控条件下稳定存在",
                    "not_reproduced": "仅降低复现稳定性，不能证明原缺陷不存在",
                },
                stop_conditions=["达到目标要求的独立复现次数", "前置条件无法重建"],
                acceptance_eligible=True,
            )
        ],
        forbidden_conclusions=["单次复测通过即证明原缺陷不存在"],
    ),
    "crash": DiagnosticPlaybook(
        playbook_id="crash-diagnosis",
        version="1.0.0",
        anomaly_kind="crash",
        applicable_when=["run terminates with a crash signal, dump, or fatal exception"],
        initial_checks=["崩溃信号", "dump 完整性", "符号匹配", "构建匹配", "调用栈"],
        required_fields=["build_id", "crash_signal", "symbol_version"],
        valuable_investigations=["补充匹配符号", "采集完整 dump", "稳定复现", "对照构建"],
        experiment_templates=[
            PlaybookExperimentTemplate(
                template_id="symbolized-reproduction",
                question="匹配符号和完整 dump 下是否得到稳定调用栈指纹？",
                capability_operation="experiment",
                controls=["build", "symbols", "environment", "scenario"],
                required_evidence_types=["crash_dump", "symbolized_stack", "runtime_log"],
                outcome_interpretation={
                    "same_fingerprint": "支持同一崩溃族被复现",
                    "different_fingerprint": "应拆分崩溃族，不能混合归因",
                },
                estimated_cost_class="medium",
                stop_conditions=["获得完整且构建匹配的 dump", "重复崩溃属于不同指纹"],
                acceptance_eligible=True,
            )
        ],
        forbidden_conclusions=["最后一行日志必然是根因", "未符号化地址足以确认具体函数"],
    ),
    "performance_regression": DiagnosticPlaybook(
        playbook_id="performance-regression",
        version="1.0.0",
        anomaly_kind="performance_regression",
        applicable_when=["comparable metric crosses a declared threshold or baseline"],
        initial_checks=["指标口径与单位", "采样窗口", "环境可比性", "温度与后台负载", "采样器开销"],
        required_fields=["metric", "unit", "sampling_window", "environment_fingerprint"],
        valuable_investigations=[
            "同环境重复采样",
            "单变量对照",
            "线程与 GPU 摘要",
            "更深 profiler 导出",
        ],
        experiment_templates=[
            PlaybookExperimentTemplate(
                template_id="controlled-repeat-sampling",
                question="同环境重复采样后回归是否仍超过阈值？",
                capability_operation="experiment",
                controls=["build", "scene", "environment", "sampling_profile"],
                required_evidence_types=["metric_series", "environment_snapshot"],
                outcome_interpretation={
                    "stable_regression": "支持当前配置存在可复现回归",
                    "high_spread": "证据不足，应先控制噪声来源",
                },
                estimated_cost_class="medium",
                stop_conditions=["满足最小独立运行数", "离散度超过目标允许范围"],
                acceptance_eligible=True,
            ),
            PlaybookExperimentTemplate(
                template_id="single-variable-localization",
                question="改变一个候选变量能否显著改变异常指标？",
                capability_operation="experiment",
                controls=["build", "environment", "all_non_target_settings"],
                changed_variables=["one_hypothesis_variable"],
                required_evidence_types=["metric_series", "profiler_summary"],
                outcome_interpretation={
                    "metric_changes": "支持变量与异常相关，但不自动确认根因",
                    "metric_unchanged": "削弱对应假设",
                },
                estimated_cost_class="high",
                stop_conditions=["候选假设被区分", "设备预算不足"],
                acceptance_eligible=False,
            ),
        ],
        forbidden_conclusions=["降帧必然是 GPU 问题", "不同环境指标可直接比较"],
    ),
    "memory_growth": DiagnosticPlaybook(
        playbook_id="memory-growth",
        version="1.0.0",
        anomaly_kind="memory_growth",
        applicable_when=["memory grows across comparable repeated cycles"],
        initial_checks=["内存种类", "热身阶段", "循环趋势", "GC 与卸载状态", "缓存策略"],
        required_fields=["memory_kind", "cycle_boundaries", "warmup_policy", "build_id"],
        valuable_investigations=["热身后重复循环", "快照差异", "存活对象", "引用链"],
        experiment_templates=[
            PlaybookExperimentTemplate(
                template_id="post-warmup-cycle-trend",
                question="排除热身后，重复循环是否仍呈单调增长？",
                capability_operation="experiment",
                controls=["build", "scenario", "environment", "cycle_count"],
                required_evidence_types=["memory_series", "lifecycle_events"],
                outcome_interpretation={
                    "continued_growth": "支持存在未释放或无界缓存现象",
                    "plateau": "不支持持续增长，但不能排除其他内存种类泄漏",
                },
                estimated_cost_class="medium",
                stop_conditions=["趋势达到稳定判定", "出现资源安全阈值"],
                acceptance_eligible=True,
            )
        ],
        forbidden_conclusions=["RSS 上升即确认泄漏", "强制 GC 后回落即排除原生泄漏"],
    ),
    "infrastructure": DiagnosticPlaybook(
        playbook_id="infrastructure-failure",
        version="1.0.0",
        anomaly_kind="infrastructure",
        applicable_when=["execution fails before trustworthy game observations are produced"],
        initial_checks=["设备健康", "调度状态", "网络与存储", "工具版本", "平台原始错误"],
        required_fields=["resource_id", "scheduler_status", "tool_version", "provider_error"],
        valuable_investigations=["查询同资源同期任务", "健康检查", "隔离资源池复核"],
        experiment_templates=[
            PlaybookExperimentTemplate(
                template_id="resource-health-query",
                question="同资源同期任务是否出现一致的基础设施异常？",
                capability_operation="retrieve_evidence",
                controls=["time_window", "resource_pool"],
                required_evidence_types=["resource_health", "scheduler_event"],
                outcome_interpretation={
                    "correlated_failures": "支持基础设施故障分类",
                    "isolated_failure": "保留游戏或任务配置原因",
                },
                estimated_cost_class="free",
                stop_conditions=["取得资源健康结论", "读取权限不足"],
            )
        ],
        forbidden_conclusions=["基础设施失败等同游戏缺陷"],
    ),
}


def strategy_for(kind: str | None) -> DiagnosticPlaybook | None:
    return PLAYBOOKS.get(kind or "")
