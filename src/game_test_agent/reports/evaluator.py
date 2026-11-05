from __future__ import annotations

from game_test_agent.contracts.models import (
    ConclusionLevel,
    EvaluationResult,
    InvestigationReport,
    Resolution,
)
from game_test_agent.storage import Repository


class ConclusionEvaluator:
    """Deterministic first-layer report evaluator."""

    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def evaluate(self, report: InvestigationReport) -> EvaluationResult:
        investigation = self.repository.get_investigation(report.investigation_id)
        evidence = {
            item.evidence_id: item
            for item in self.repository.list_current_evidence(report.investigation_id)
        }
        hypotheses = {
            item.hypothesis_id: item
            for item in self.repository.list_hypotheses(report.investigation_id)
        }
        facts = {
            item.fact_id: item
            for item in self.repository.list_current_facts(report.investigation_id)
        }
        assessments = {
            f"assessment:{item.criterion_id}:{item.evaluated_state_revision}": item
            for item in self.repository.list_assessments(report.investigation_id)
        }
        issues: list[str] = []
        missing: list[str] = []
        read_events = [
            event["data"]["excerpt"]
            for event in self.repository.list_recent_events(report.investigation_id)
            if event["event_type"] == "evidence.excerpt_read"
        ]
        run_results = {}
        for action in self.repository.list_actions(report.investigation_id):
            snapshot = self.repository.get_latest_run_result(action["action"]["action_id"])
            if snapshot is not None:
                run_results[snapshot.result.run_ref.run_id] = snapshot.result
        for claim in report.claims:
            absent = set(claim.evidence_ids) - evidence.keys()
            if absent:
                issues.append(
                    f"claim {claim.claim_id} references missing evidence: {sorted(absent)}"
                )
                missing.extend(sorted(absent))
            absent_facts = set(claim.fact_ids) - facts.keys()
            if absent_facts:
                issues.append(
                    f"claim {claim.claim_id} references missing facts: {sorted(absent_facts)}"
                )
            absent_assessments = set(claim.assessment_ids) - assessments.keys()
            if absent_assessments:
                issues.append(
                    f"claim {claim.claim_id} references missing assessments: "
                    f"{sorted(absent_assessments)}"
                )
            for fact_id in set(claim.fact_ids) & facts.keys():
                if not set(facts[fact_id].evidence_ids).issubset(claim.evidence_ids):
                    issues.append(f"claim {claim.claim_id} omits evidence used by fact {fact_id}")
            for assessment_id in set(claim.assessment_ids) & assessments.keys():
                assessment_facts = set(
                    assessments[assessment_id].supporting_fact_ids
                    + assessments[assessment_id].contradicting_fact_ids
                )
                if claim.fact_ids and not assessment_facts.intersection(claim.fact_ids):
                    issues.append(
                        f"claim {claim.claim_id} assessment does not evaluate its cited facts"
                    )
            if claim.basis == "fact" and not claim.fact_ids:
                issues.append(f"claim {claim.claim_id} has no cited fact")
            if claim.basis == "run_verdict":
                run_id = (claim.scope or "").removeprefix("run:")
                result = run_results.get(run_id)
                if (
                    not claim.scope or not claim.scope.startswith("run:")
                    or result is None
                    or result.execution_source != "real"
                    or result.test_verdict != claim.expected_value
                    or not claim.evidence_ids
                    or any(
                        evidence[eid].run_ref != result.run_ref
                        for eid in claim.evidence_ids if eid in evidence
                    )
                ):
                    issues.append(f"claim {claim.claim_id} contradicts its Run verdict")
            if claim.basis == "evidence_excerpt":
                if not claim.supporting_excerpt or not any(
                    event.get("evidence_id") in claim.evidence_ids
                    and event.get("source_sha256")
                    == evidence[event["evidence_id"]].sha256
                    and claim.supporting_excerpt in event.get("text", "")
                    for event in read_events
                    if event.get("evidence_id") in evidence
                ):
                    issues.append(f"claim {claim.claim_id} lacks a current read excerpt")
            if claim.basis == "analysis_result" and (
                not claim.evidence_ids
                or any(
                    evidence[eid].source_type != "analysis"
                    or evidence[eid].producer != "analysis-recipe"
                    for eid in claim.evidence_ids if eid in evidence
                )
            ):
                issues.append(f"claim {claim.claim_id} lacks registered analysis output")
            if claim.basis == "hypothesis" and claim.scope not in hypotheses:
                issues.append(f"claim {claim.claim_id} has no cited hypothesis")
        absent_hypotheses = set(report.hypothesis_ids) - hypotheses.keys()
        if absent_hypotheses:
            issues.append("report references absent hypotheses")
        verified_levels = {
            ConclusionLevel.REPRODUCED,
            ConclusionLevel.BOTTLENECK_SUPPORTED,
            ConclusionLevel.ROOT_CAUSE_CONFIRMED,
        }
        cited = [
            evidence[eid]
            for claim in report.claims
            for eid in claim.evidence_ids
            if eid in evidence
        ]
        if report.conclusion_level in verified_levels:
            if not cited:
                issues.append("verified conclusion has no resolvable evidence")
            elif all(item.quality in {"simulated", "untrusted"} for item in cited):
                issues.append("verified conclusion relies only on simulated or untrusted evidence")
            if any(item.retrieval_status != "ready" for item in cited):
                issues.append("verified conclusion includes evidence that is not ready")
            if any(item.execution_source != "real" for item in cited):
                issues.append("verified conclusion includes evidence not marked as real execution")
            expected_build = investigation.request.build_ref
            if expected_build and any(
                item.build_ref is None or item.build_ref.id != expected_build.id for item in cited
            ):
                issues.append("verified conclusion has missing or mismatched build provenance")
            expected_environment = investigation.request.constraints.get("environment_fingerprint")
            if expected_environment and any(
                item.environment_fingerprint != expected_environment for item in cited
            ):
                issues.append(
                    "verified conclusion has missing or mismatched environment provenance"
                )
            max_age = investigation.request.constraints.get("evidence_max_age_seconds")
            if max_age is not None:
                if (
                    not isinstance(max_age, (int, float))
                    or isinstance(max_age, bool)
                    or max_age < 0
                ):
                    issues.append("investigation has invalid evidence_max_age_seconds policy")
                elif any(
                    (report.generated_at - item.collected_at).total_seconds() > max_age
                    for item in cited
                ):
                    issues.append("verified conclusion relies on stale evidence")
        if report.conclusion_level == ConclusionLevel.ROOT_CAUSE_CONFIRMED:
            confirmation_kinds = {
                "crash_dump",
                "symbolized_stack",
                "reference_chain",
                "isolation_experiment",
                "fix_verification",
            }
            if not any(item.kind in confirmation_kinds for item in cited):
                issues.append("root cause confirmation lacks causal or isolation evidence")
        objective_id = investigation.request.objective.objective_id
        covered = set(report.coverage.completed_objectives) | set(
            report.coverage.uncovered_objectives
        )
        if objective_id not in covered:
            issues.append("primary objective is absent from report coverage")
        if report.resolution in {Resolution.GOAL_MET, Resolution.GOAL_FAILED}:
            latest = self.repository.latest_assessment(report.investigation_id)
            required = "passed" if report.resolution == Resolution.GOAL_MET else "failed"
            spec = investigation.request.constraints.get("objective_spec")
            if (report.status != "completed" or latest is None or latest.verdict != required
                    or not isinstance(spec, dict) or latest.criterion_id != spec.get("criterion_id")
                    or latest.rule_version != spec.get("rule_version", "metric-threshold-v1")
                    or objective_id not in report.coverage.completed_objectives):
                issues.append("goal resolution requires a matching current objective assessment")
        return EvaluationResult(
            accepted=not issues,
            blocking_issues=issues,
            missing_evidence=sorted(set(missing)),
            recommended_action=(
                "lower_conclusion_strength_or_collect_missing_evidence" if issues else None
            ),
        )
