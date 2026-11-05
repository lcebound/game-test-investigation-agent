from __future__ import annotations

from game_test_agent.contracts.models import ConclusionLevel, InvestigationReport, ClaimVerification
from game_test_agent.storage import Repository
from game_test_agent.provenance.projections import ProjectionCoordinator
from game_test_agent.observability.tracing import trace_operation


class ReportService:
    def __init__(self, repository: Repository, artifacts=None) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self.projections = ProjectionCoordinator(repository)

    @trace_operation("report.publish", identity="object")
    def publish(self, report: InvestigationReport) -> str:
        from game_test_agent.reports.evaluator import ConclusionEvaluator

        if sum(len(c.assertions) for c in report.claims) > 32:
            raise ValueError("report permits at most 32 structured assertions")
        self.projections.refresh(report.investigation_id)
        self.projections.assert_fresh(report.investigation_id)
        evaluation = ConclusionEvaluator(self.repository).evaluate(report)
        if not evaluation.accepted:
            raise ValueError(
                "report failed deterministic evaluation: " + "; ".join(evaluation.blocking_issues)
            )
        investigation = self.repository.get_investigation(report.investigation_id)
        evidence = {
            item.evidence_id: item
            for item in self.repository.list_current_evidence(report.investigation_id)
        }
        for claim in report.claims:
            missing = set(claim.evidence_ids) - evidence.keys()
            if missing:
                raise ValueError(
                    f"claim {claim.claim_id} references missing evidence: {sorted(missing)}"
                )
            for evidence_id in claim.evidence_ids:
                item = evidence[evidence_id]
                if (item.tenant_id, item.project_id) != (
                    investigation.request.tenant_id,
                    investigation.request.project_id,
                ):
                    raise PermissionError("report references cross-project evidence")
        if report.conclusion_level in {
            ConclusionLevel.REPRODUCED,
            ConclusionLevel.BOTTLENECK_SUPPORTED,
            ConclusionLevel.ROOT_CAUSE_CONFIRMED,
        }:
            qualities = {
                evidence[eid].quality
                for claim in report.claims
                for eid in claim.evidence_ids
                if eid in evidence
            }
            if not qualities or qualities <= {"simulated", "untrusted"}:
                raise ValueError(
                    "simulated or untrusted evidence cannot support a verified conclusion"
                )
        facts_by_id = {f.fact_id: f for f in self.repository.list_current_facts(report.investigation_id)}
        source_events = self.repository.list_recent_events_by_types(
            report.investigation_id, ["mcp.native.tool_completed"], limit=5000)
        sources = {r["data"]["evidence_id"]: r["data"].get("source", {}).get("values", {})
                   for r in source_events}
        from game_test_agent.evidence.assertions import AssertionVerifier
        if any(c.assertions for c in report.claims) and self.artifacts is None:
            raise ValueError("structured claims require a configured artifact store")
        verifier = AssertionVerifier(self.repository, self.artifacts, investigation)
        for claim in report.claims:
            claim.verification_status = "verified"
            # Reset caller-supplied checks: a model cannot certify its own inference.
            details = ClaimVerification()
            if claim.assertions:
                if not {a.evidence_id for a in claim.assertions}.issubset(claim.evidence_ids):
                    raise ValueError("assertions must reference cited evidence")
                if claim.basis != "evidence_excerpt" or claim.text != verifier.render(claim.assertions):
                    raise ValueError("structured claim text must match independently computed assertions")
                details.support = "deterministic"
            if claim.basis == "evidence_excerpt":
                details.citation = "verified"
                if claim.text == claim.supporting_excerpt:
                    details.support = "literal_only"
            if claim.basis == "run_verdict" or (
                claim.basis == "fact" and any(facts_by_id[fid].statement == claim.text
                    for fid in claim.fact_ids if fid in facts_by_id)
            ):
                details.support = "deterministic"
            cited = [evidence[eid] for eid in claim.evidence_ids]
            if cited and all(e.run_ref and e.build_ref and e.environment_fingerprint
                             and e.quality == "verified" and e.execution_source == "real" for e in cited):
                details.provenance = "verified"
            elif cited:
                if all(all(sources.get(eid, {}).get(key) for key in ("run_id", "build_id", "environment"))
                       for eid in claim.evidence_ids):
                    details.provenance = "provider_declared"
            claim.verification_details = details
        report_ref = self.repository.save_report(report, self.render_markdown(report))
        self.repository.record_investigation_event(
            report.investigation_id,
            "report.claims_verified",
            {
                "report_revision": report.revision,
                "claim_ids": [claim.claim_id for claim in report.claims],
                "validator": "conclusion-evaluator-v2",
                "scope": "deterministic_structural_checks",
            },
        )
        return report_ref

    @staticmethod
    def render_markdown(report: InvestigationReport) -> str:
        lines = [
            f"# 调查报告 {report.investigation_id}",
            "",
            report.summary,
            "",
            f"- 状态：{report.status}",
            f"- 结论：{report.resolution}",
            f"- 结论等级：{report.conclusion_level}",
            f"- 停止原因：{report.stop_reason}",
            "",
            "## 结论与证据",
            "",
        ]
        if report.claims:
            for claim in report.claims:
                lines.append(
                    f"- {claim.text}（证据：{', '.join(claim.evidence_ids)}；"
                    f"结构校验：{claim.verification_status}）"
                )
                checks = claim.verification_details
                lines.append(f"  - 分项校验：引用={checks.citation}；来源={checks.provenance}；"
                             f"主张支持={checks.support}；因果={checks.causality}")
                for limitation in claim.limitations:
                    lines.append(f"  - 限制：{limitation}")
        else:
            lines.append("当前没有足以支撑事实性结论的证据。")
        lines.extend(["", "## 覆盖与未解问题", ""])
        lines.append("- 已完成目标：" + (", ".join(report.coverage.completed_objectives) or "无"))
        lines.append("- 未覆盖目标：" + (", ".join(report.coverage.uncovered_objectives) or "无"))
        for question in report.unresolved_questions:
            lines.append(f"- 未解：{question}")
        lines.extend(["", "## 建议", ""])
        lines.extend(f"- {item}" for item in report.recommended_actions)
        return "\n".join(lines).rstrip() + "\n"
