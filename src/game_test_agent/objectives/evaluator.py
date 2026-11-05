from __future__ import annotations

import math
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from pydantic import ValidationError

from game_test_agent.contracts.models import (
    CriterionAssessment,
    EvidenceRecord,
    Fact,
    InvalidationPlan,
    ObjectiveSpec,
)
from game_test_agent.provenance import ProvenanceIndex
from game_test_agent.storage import Repository, canonical_json


RuleEvaluator = Callable[
    [str, int, ObjectiveSpec, list[EvidenceRecord], list[Fact], datetime],
    CriterionAssessment,
]


@dataclass(frozen=True, slots=True)
class IncrementalEvaluationResult:
    assessment: CriterionAssessment | None
    invalidations: tuple[InvalidationPlan, ...]


class ObjectiveEvaluator:
    """Evaluate provider-neutral, structured acceptance criteria.

    Providers only supply normalized runs, observations, and facts.  This class owns
    the decision semantics so no platform-specific status or field leaks into the
    investigation core.
    """

    CONSTRAINT_KEY = "objective_spec"
    POLICY_VERSION = 2

    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self._rules: dict[str, RuleEvaluator] = {"metric-threshold-v1": self._assess}

    def register(self, rule_version: str, evaluator: RuleEvaluator) -> None:
        if not rule_version:
            raise ValueError("rule version is required")
        if rule_version in self._rules:
            raise ValueError(f"objective rule {rule_version!r} is already registered")
        self._rules[rule_version] = evaluator

    def evaluate(self, investigation_id: str) -> CriterionAssessment | None:
        investigation = self.repository.get_investigation(investigation_id)
        raw_spec = investigation.request.constraints.get(self.CONSTRAINT_KEY)
        if raw_spec is None:
            return None
        try:
            spec = ObjectiveSpec.model_validate(raw_spec)
        except ValidationError as error:
            raise ValueError("constraints.objective_spec is invalid") from error
        if spec.objective_id != investigation.request.objective.objective_id:
            raise ValueError("objective_spec does not describe the investigation objective")

        evidence = self.repository.list_current_evidence(investigation_id)
        facts = self.repository.list_current_facts(investigation_id)
        # Scope is enforced for built-in and registered rules alike. Unrelated
        # historical evidence may remain in the investigation but cannot satisfy it.
        eligible, scope_notes = [], []
        requested_build = investigation.request.build_ref
        requested_environment = investigation.request.constraints.get("environment_fingerprint")
        if requested_environment is not None and (not isinstance(requested_environment, str) or not requested_environment.strip()):
            raise ValueError("environment_fingerprint must be a nonempty string")
        for item in evidence:
            build = item.build_ref
            build_ok = requested_build is None or (
                build is not None and build.id == requested_build.id
                and (requested_build.revision is None or build.revision == requested_build.revision)
                and all(build.configuration.get(k) == v for k, v in requested_build.configuration.items())
            )
            environment_ok = requested_environment is None or item.environment_fingerprint == requested_environment
            if build_ok and environment_ok and item.retrieval_status == "ready":
                eligible.append(item)
            else:
                scope_notes.append(f"evidence {item.evidence_id} is outside the requested scope or is not ready")
        eligible_ids = {item.evidence_id for item in eligible}
        scoped_facts = [fact for fact in facts if set(fact.evidence_ids).issubset(eligible_ids)]
        ledger_revision = (
            investigation.revision * 1_000_000_000_000
            + len(self.repository.list_evidence(investigation_id)) * 1_000_000
            + len(self.repository.list_facts(investigation_id)) * 1_000
            + sum(item.revision for item in self.repository.list_hypotheses(investigation_id))
        )
        # Namespace corrected semantics so existing assessments at the same
        # source revision are not silently reused or overwritten.
        ledger_revision = ledger_revision * 10 + self.POLICY_VERSION
        evaluator = self._rules.get(spec.rule_version)
        if evaluator is None:
            raise ValueError(f"objective evaluator rule is unavailable: {spec.rule_version}")
        assessment = evaluator(
            investigation_id,
            ledger_revision,
            spec,
            eligible,
            scoped_facts,
            investigation.updated_at,
        )
        if scope_notes:
            assessment = assessment.model_copy(update={
                "limitations": [*assessment.limitations, *scope_notes],
            })
        stored, _ = self.repository.save_assessment(assessment)
        return stored

    def evaluate_incremental(
        self,
        investigation_id: str,
        changed_source_ids: list[str],
        *,
        provenance: ProvenanceIndex | None = None,
    ) -> IncrementalEvaluationResult:
        index = provenance or ProvenanceIndex(self.repository)
        invalidations = tuple(
            index.invalidate(investigation_id, source_id)
            for source_id in dict.fromkeys(changed_source_ids)
        )
        affected = {node_id for item in invalidations for node_id in item.affected_node_ids}
        should_evaluate = bool(changed_source_ids) and (
            any(node_id.startswith("assessment:") for node_id in affected)
            or any(node_id.startswith("fact") for node_id in affected)
            or any(
                item.evidence_id in changed_source_ids
                for item in self.repository.list_current_evidence(investigation_id)
            )
        )
        assessment = self.evaluate(investigation_id) if should_evaluate else None
        return IncrementalEvaluationResult(assessment, invalidations)

    def _assess(
        self,
        investigation_id: str,
        investigation_revision: int,
        spec: ObjectiveSpec,
        evidence: list[EvidenceRecord],
        facts: list[Fact],
        generated_at: datetime,
    ) -> CriterionAssessment:
        evidence_by_id = {item.evidence_id: item for item in evidence}
        state_revision = investigation_revision
        candidates: dict[tuple[str, str, str], list[tuple[Fact, EvidenceRecord, float]]] = (
            defaultdict(list)
        )
        limitations: list[str] = []
        missing: list[str] = []

        for fact in facts:
            if fact.data.get("column") != spec.metric:
                continue
            if not self._matches_filters(fact.data, spec.fact_filters):
                continue
            if spec.statistic not in fact.data:
                continue
            linked = [evidence_by_id.get(item) for item in fact.evidence_ids]
            if linked and all(item is not None and item.execution_source not in spec.allowed_execution_sources for item in linked):
                limitations.append(f"fact {fact.fact_id} uses an excluded execution source")
                continue
            if fact.data.get("unit") != spec.unit:
                limitations.append(f"fact {fact.fact_id} uses a different unit")
                missing.append(f"fact {fact.fact_id} has an incompatible or missing declared unit")
                continue
            raw_value = fact.data.get(spec.statistic)
            try:
                valid_number = (not isinstance(raw_value, bool) and isinstance(raw_value, (int, float))
                                and math.isfinite(float(raw_value)))
            except OverflowError:
                valid_number = False
            if not valid_number:
                limitations.append(f"fact {fact.fact_id} has no finite {spec.statistic}")
                if spec.statistic in fact.data:
                    missing.append(f"fact {fact.fact_id} has an invalid measurement")
                continue
            if any(item is None or item.retrieval_status != "ready" for item in linked):
                missing.append(f"fact {fact.fact_id} has missing or unreadable source evidence")
                continue
            run_items = [item for item in linked if item.run_ref is not None]
            run_keys = {
                (item.run_ref.provider_id, item.run_ref.project_id, item.run_ref.run_id)
                for item in run_items
                if item.run_ref is not None
            }
            if len(run_keys) != 1:
                limitations.append(f"fact {fact.fact_id} is not attributable to one run")
                missing.append(f"fact {fact.fact_id} has unresolved run provenance")
                continue
            evidence_item = run_items[0]
            if any(item.execution_source not in spec.allowed_execution_sources for item in linked):
                limitations.append(
                    f"run {evidence_item.run_ref.run_id} uses disallowed "
                    f"execution source {evidence_item.execution_source}"
                )
                missing.append(f"fact {fact.fact_id} mixes allowed and excluded execution sources")
                continue
            builds = {canonical_json(item.build_ref.model_dump(mode="json")) if item.build_ref else None for item in linked}
            environments = {item.environment_fingerprint for item in linked}
            if len(builds) > 1 or len(environments) > 1:
                missing.append(f"fact {fact.fact_id} mixes source provenance")
                continue
            candidates[next(iter(run_keys))].append((fact, evidence_item, float(raw_value)))

        selected: list[tuple[Fact, EvidenceRecord, float]] = []
        for run_key, observations in candidates.items():
            values = {item[2] for item in observations}
            scopes = {canonical_json(item[0].data.get("measurement_scope", {})) for item in observations}
            provenance = {(canonical_json(item[1].build_ref.model_dump(mode="json")) if item[1].build_ref else None,
                           item[1].environment_fingerprint) for item in observations}
            if len(values) != 1 or len(scopes) != 1 or len(provenance) != 1:
                issue = f"run {run_key[2]} has conflicting metric facts or unresolved measurement scopes"
                limitations.append(issue)
                missing.append(issue)
                continue
            selected.append(observations[-1])

        selected.sort(key=lambda item: item[1].run_ref.run_id if item[1].run_ref else "")
        values = [item[2] for item in selected]
        supporting: list[str] = []
        contradicting: list[str] = []
        for fact, _evidence, value in selected:
            target = (
                supporting
                if self._compare(value, spec.comparison, spec.threshold)
                else contradicting
            )
            target.append(fact.fact_id)

        checks = [
            f"metric={spec.metric}",
            f"unit={spec.unit}",
            f"distinct_runs={len(selected)}",
        ]
        if len(selected) < spec.min_independent_runs:
            missing.append(
                f"requires {spec.min_independent_runs} independent runs; found {len(selected)}"
            )
        if selected:
            limitations.append(
                "distinct run identifiers are necessary but do not prove statistical independence"
            )
        builds = {
            json.dumps(
                item[1].build_ref.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
            )
            for item in selected
            if item[1].build_ref is not None
        }
        environments = {
            item[1].environment_fingerprint
            for item in selected
            if item[1].environment_fingerprint is not None
        }
        checks.extend(
            [
                f"distinct_builds={len(builds)}",
                f"distinct_environments={len(environments)}",
            ]
        )
        if len(builds) > 1:
            missing.append("selected runs use different builds")
        if len(environments) > 1:
            missing.append("selected runs use different environments")
        if spec.max_spread is not None and values:
            spread = max(values) - min(values)
            checks.append(f"spread={spread:g}")
            if spread > spec.max_spread:
                missing.append(f"observed spread {spread:g} exceeds allowed {spec.max_spread:g}")

        verdict = "insufficient"
        if not missing:
            verdict = "failed" if contradicting else "passed"

        source_values = {item[1].execution_source for item in selected}
        scope = "simulation" if "simulated" in source_values else "real_target"
        if (
            not selected
            and "simulated" in spec.allowed_execution_sources
            and "real" not in spec.allowed_execution_sources
        ):
            scope = "simulation"

        return CriterionAssessment(
            criterion_id=spec.criterion_id,
            rule_version=spec.rule_version,
            investigation_id=investigation_id,
            evaluated_state_revision=state_revision,
            verdict=verdict,
            scope=scope,
            supporting_fact_ids=supporting,
            contradicting_fact_ids=contradicting,
            missing_requirements=missing,
            comparability_checks=checks,
            limitations=limitations,
            observed_values=values,
            generated_at=generated_at,
        )

    @staticmethod
    def _matches_filters(data, filters):
        for key, expected in filters.items():
            if key not in data:
                return False
            actual = data[key]
            if isinstance(actual, bool) != isinstance(expected, bool) or actual != expected:
                return False
        return True

    @staticmethod
    def _compare(value: float, comparison: str, threshold: float) -> bool:
        return {
            "lt": value < threshold,
            "le": value <= threshold,
            "eq": value == threshold,
            "ge": value >= threshold,
            "gt": value > threshold,
        }[comparison]
