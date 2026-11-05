from __future__ import annotations

from game_test_agent.contracts.models import (
    Claim,
    ConclusionLevel,
    Coverage,
    InvestigationReport,
    Resolution,
)
from game_test_agent.storage import Repository


class ReportBuilder:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def build(
        self,
        investigation_id: str,
        stop_reason: str,
        *,
        status: str = "completed",
        resolution: Resolution = Resolution.INCONCLUSIVE,
        summary: str | None = None,
    ) -> InvestigationReport:
        investigation = self.repository.get_investigation(investigation_id)
        evidence = self.repository.list_current_evidence(investigation_id)
        facts = self.repository.list_current_facts(investigation_id)
        hypotheses = self.repository.list_hypotheses(investigation_id)
        assessment = self.repository.latest_assessment(investigation_id)
        assessment_id = (
            f"assessment:{assessment.criterion_id}:{assessment.evaluated_state_revision}"
            if assessment is not None
            else None
        )
        assessed_fact_ids = (
            set(assessment.supporting_fact_ids + assessment.contradicting_fact_ids)
            if assessment is not None
            else set()
        )
        claims: list[Claim] = [
            Claim(
                claim_id=f"claim-{fact.fact_id}",
                text=fact.statement,
                basis="fact",
                scope=investigation.request.project_id,
                rule_id=fact.extractor_version,
                evidence_ids=fact.evidence_ids,
                fact_ids=[fact.fact_id],
                assessment_ids=(
                    [assessment_id]
                    if assessment_id is not None and fact.fact_id in assessed_fact_ids
                    else []
                ),
            )
            for fact in facts
        ]
        run_verdicts = []
        for item in self.repository.list_actions(investigation_id):
            action_id = item["action"]["action_id"]
            snapshot = self.repository.get_latest_run_result(action_id)
            if snapshot is None:
                continue
            result = snapshot.result
            if result.execution_source != "real" or result.test_verdict not in {
                "passed",
                "failed",
            }:
                continue
            linked = [
                record
                for record in evidence
                if record.run_ref == result.run_ref and record.retrieval_status == "ready"
            ]
            if not linked:
                continue
            run_verdicts.append(result)
            verdict = "通过" if result.test_verdict == "passed" else "失败"
            limitations = sorted(
                {limitation for record in linked for limitation in record.limitations}
                | set(result.missing_fields)
            )
            claims.append(
                Claim(
                    claim_id=f"claim-run-{result.run_ref.run_id}",
                    text=f"平台判定本次 Run {result.run_ref.run_id} 的测试结果为{verdict}。",
                    basis="run_verdict",
                    scope=f"run:{result.run_ref.run_id}",
                    rule_id="provider_test_verdict",
                    expected_value=result.test_verdict,
                    evidence_ids=[record.evidence_id for record in linked],
                    limitations=limitations,
                )
            )
        if not claims:
            claims = [
                Claim(
                    claim_id=f"claim-evidence-{index}",
                    text=f"已获取 {item.kind} 证据，定位为 {item.locator.value}。",
                    basis="evidence_metadata",
                    scope=investigation.request.project_id,
                    evidence_ids=[item.evidence_id],
                    limitations=item.limitations,
                )
                for index, item in enumerate(evidence, start=1)
            ]
        objective_id = investigation.request.objective.objective_id
        if status == "completed" and assessment and assessment.verdict == "passed":
            resolution = Resolution.GOAL_MET
            completed = [objective_id]
            uncovered: list[str] = []
            unresolved: list[str] = []
            recommended: list[str] = []
            default_summary = (
                f"目标验收条件已通过（{assessment.scope}），"
                f"观测值为 {assessment.observed_values}。"
            )
        elif status == "completed" and assessment and assessment.verdict == "failed":
            resolution = Resolution.GOAL_FAILED
            completed = [objective_id]
            uncovered = []
            unresolved = []
            recommended = ["根据失败指标定位回归原因，修复后使用同一目标规则重测。"]
            default_summary = (
                f"目标验收条件未通过（{assessment.scope}），"
                f"观测值为 {assessment.observed_values}。"
            )
        else:
            completed = []
            uncovered = [objective_id]
            unresolved = [investigation.request.objective.description]
            recommended = ["补充能够区分候选原因的真实证据后继续调查。"]
            if assessment and assessment.missing_requirements:
                recommended = ["；".join(assessment.missing_requirements)]
            default_summary = (
                "调查已完成受控执行并取得证据，但尚不足以满足目标判定要求。"
                if evidence
                else "调查已停止，当前没有足以形成事实结论的新证据。"
            )
            if run_verdicts and all(item.test_verdict == "passed" for item in run_verdicts):
                default_summary = (
                    "本次 Run 平台判定通过；指定构建验收证据不足。"
                    if len(run_verdicts) == 1
                    else f"{len(run_verdicts)} 次 Run 的平台判定均通过；指定构建验收证据不足。"
                )
            elif run_verdicts:
                default_summary = "真实 Run 的平台判定包含失败；指定构建验收证据不足。"
            if (
                "failed" in stop_reason
                or "rejected" in stop_reason
                or stop_reason.endswith("budget_exhausted")
                or stop_reason in {"external_result_pending", "max_rounds_reached"}
            ):
                gaps = (
                    "；".join(assessment.missing_requirements)
                    if assessment and assessment.missing_requirements
                    else investigation.request.objective.description
                )
                default_summary = (
                    f"调查因 {stop_reason} 中断，目标尚未完成：{gaps}。" + default_summary
                )
                recommended = ["修复调查执行障碍后，从已保存的运行和证据继续调查。", *recommended]
        if status == "failed":
            default_summary = (
                f"调查执行失败（{stop_reason}），未形成有效调查结论。"
                "已保存的证据和检查点予以保留；此状态不能解释为证据不足或目标通过。"
            )
            recommended = [
                "依据停止原因修复服务、配置、权限或预算问题，再利用已保存证据重新调查。"
            ]
        return InvestigationReport(
            investigation_id=investigation_id,
            revision=self.repository.next_report_revision(investigation_id),
            status=status,
            resolution=resolution,
            summary=summary or default_summary,
            conclusion_level=(ConclusionLevel.OBSERVATION if evidence else ConclusionLevel.UNKNOWN),
            claims=claims,
            hypothesis_ids=[item.hypothesis_id for item in hypotheses],
            coverage=Coverage(
                completed_objectives=completed,
                uncovered_objectives=uncovered,
            ),
            unresolved_questions=unresolved,
            recommended_actions=recommended,
            stop_reason=stop_reason,
            generated_at=investigation.updated_at,
        )
