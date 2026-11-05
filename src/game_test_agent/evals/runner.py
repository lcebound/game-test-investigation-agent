from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from pydantic import TypeAdapter

from game_test_agent.evals.models import (
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationMetrics,
    EvaluationSuiteResult,
)
from game_test_agent.reports import ConclusionEvaluator
from game_test_agent.storage import Repository


def load_cases(path: Path) -> list[EvaluationCase]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = TypeAdapter(list[EvaluationCase]).validate_python(payload)
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("evaluation case_id values must be unique")
    return cases


class EvaluationRunner:
    """Runs deterministic report-boundary cases without external providers or models."""

    def run(self, cases: list[EvaluationCase]) -> EvaluationSuiteResult:
        results = [self._run_case(case) for case in cases]
        total = len(results)
        passed = sum(item.passed for item in results)
        acceptance_correct = sum(
            item.evaluator_accepted == item.expected_accepted for item in results
        )
        unsafe = [item for item in results if not item.expected_accepted]
        rejected_unsafe = sum(not item.evaluator_accepted for item in unsafe)
        citation_average = (
            sum(item.citation_validity_rate for item in results) / total if total else 1.0
        )
        metrics = EvaluationMetrics(
            total_cases=total,
            passed_cases=passed,
            case_pass_rate=passed / total if total else 1.0,
            acceptance_accuracy=acceptance_correct / total if total else 1.0,
            citation_validity_rate=citation_average,
            unsafe_case_rejection_rate=rejected_unsafe / len(unsafe) if unsafe else 1.0,
        )
        return EvaluationSuiteResult(
            passed=passed == total,
            metrics=metrics,
            cases=results,
        )

    @staticmethod
    def _run_case(case: EvaluationCase) -> EvaluationCaseResult:
        with tempfile.TemporaryDirectory(prefix="game-test-eval-") as directory:
            repository = Repository(Path(directory) / "eval.db")
            try:
                investigation, _ = repository.create_investigation(
                    case.request,
                    f"eval/{case.case_id}",
                    principal_id="offline-evaluator",
                )
                for evidence in case.evidence:
                    repository.add_evidence(investigation.investigation_id, evidence)
                report = case.candidate_report.model_copy(
                    update={"investigation_id": investigation.investigation_id}
                )
                evaluation = ConclusionEvaluator(repository).evaluate(report)
                known_evidence = {item.evidence_id for item in case.evidence}
                cited = [eid for claim in report.claims for eid in claim.evidence_ids]
                valid_citations = sum(eid in known_evidence for eid in cited)
                citation_rate = valid_citations / len(cited) if cited else 1.0
                violations: list[str] = []
                expectation = case.expectation
                if expectation.should_accept:
                    if report.conclusion_level not in expectation.allowed_conclusion_levels:
                        violations.append(
                            f"conclusion level {report.conclusion_level} is outside the labeled boundary"
                        )
                    missing_required = set(expectation.required_evidence_ids) - set(cited)
                    if missing_required:
                        violations.append(
                            f"required evidence was not cited: {sorted(missing_required)}"
                        )
                    limitations = {
                        limitation for claim in report.claims for limitation in claim.limitations
                    }
                    missing_limitations = set(expectation.required_limitations) - limitations
                    if missing_limitations:
                        violations.append(
                            "required limitations were not preserved: "
                            f"{sorted(missing_limitations)}"
                        )
                    for fragment in expectation.stop_reason_contains:
                        if fragment not in report.stop_reason:
                            violations.append(f"stop reason does not contain {fragment!r}")
                for fragment in expectation.expected_blocking_issue_contains:
                    if not any(fragment in issue for issue in evaluation.blocking_issues):
                        violations.append(f"blocking issues do not contain {fragment!r}")
                accepted_matches = evaluation.accepted == expectation.should_accept
                return EvaluationCaseResult(
                    case_id=case.case_id,
                    category=case.category,
                    passed=accepted_matches and not violations,
                    evaluator_accepted=evaluation.accepted,
                    expected_accepted=expectation.should_accept,
                    citation_validity_rate=citation_rate,
                    blocking_issues=evaluation.blocking_issues,
                    label_violations=violations,
                )
            finally:
                repository.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the offline diagnostic evaluation suite")
    parser.add_argument("fixture", type=Path, help="JSON file containing evaluation cases")
    parser.add_argument("--output", type=Path, help="Optional path for the JSON result")
    args = parser.parse_args()
    result = EvaluationRunner().run(load_cases(args.fixture))
    rendered = result.model_dump_json(indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    raise SystemExit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
