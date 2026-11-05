"""Evidence-bound native report validation, draft recovery and publication."""
from __future__ import annotations

import json
from game_test_agent.memory.recall import RecallService
from game_test_agent.storage.feedback import FeedbackStore
from copy import deepcopy
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from game_test_agent.contracts.models import Claim, ContextAccessScope, Coverage, InvestigationReport, InvestigationStatus
from game_test_agent.context.access import ContextAccess
from game_test_agent.reports import ReportBuilder, ReportService
from game_test_agent.evidence.assertions import AssertionVerifier
from game_test_agent.storage import canonical_json
from game_test_agent.workflows.native_evidence import NativeFinding, finding_errors, source_index, text_consistency_errors
from game_test_agent.workflows.native_questions import report_question_progress

class NativeDiagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=8000)
    citations: list[dict[str, str]] = Field(default_factory=list, max_length=20)
    limitations: list[str] = Field(default_factory=list, max_length=30)
    recommended_actions: list[str] = Field(default_factory=list, max_length=30)
    interpretation: Literal["expected_effect", "unexpected_effect", "unknown"] = "unknown"
    interpretation_basis: str = Field(default="", max_length=2000)
    hypotheses: list[str] = Field(default_factory=list, max_length=10)
    findings: list[NativeFinding] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def bounded_assertions(self):
        if sum(len(f.assertions) for f in self.findings) > 32:
            raise ValueError("report permits at most 32 structured assertions")
        return self


def report_schema(*, structured: bool):
    """Expose JSON-field assertions only when collected material can use them."""
    schema = NativeDiagnosis.model_json_schema()
    if not structured:
        schema = deepcopy(schema)
        schema["$defs"]["NativeFinding"]["properties"].pop("assertions", None)
        schema["$defs"].pop("EvidenceAssertion", None)
    return schema



class NativeReportPublisher:
    def __init__(self, repository, artifacts, reports, plan_transition):
        self.repository, self.artifacts, self.reports = repository, artifacts, reports
        self.plan_transition = plan_transition

    def citation_errors(self, view, diagnosis):
        evidence = {
            e.evidence_id: e for e in self.repository.list_current_evidence(view.investigation_id)
        }
        errors = []
        all_citations = diagnosis.citations + [c.model_dump() for f in diagnosis.findings for c in f.citations]
        if not all_citations:
            errors.append("At least one literal evidence citation is required.")
        for index, citation in enumerate(all_citations):
            record = evidence.get(citation.get("evidence_id"))
            quote = citation.get("quote", "")
            if record is None or record.retrieval_status != "ready":
                errors.append(
                    f"citations[{index}]: evidence_id is not readable in this investigation."
                )
            elif not quote or len(quote.encode()) > 3000:
                errors.append(f"citations[{index}]: quote must contain 1–3000 UTF-8 bytes.")
            elif quote.encode() not in self.artifacts.read_bytes(record.artifact_ref):
                errors.append(
                    f"citations[{index}]: quote is not a literal substring of the original evidence. Copy one short continuous fragment exactly; do not join fields with ellipses or paraphrase."
                )
        if not diagnosis.findings:
            errors.extend(text_consistency_errors(diagnosis.summary, [c.get("quote", "") for c in all_citations]))
        sources = source_index(self.repository, view.investigation_id)
        if len({f.finding_id for f in diagnosis.findings}) != len(diagnosis.findings):
            errors.append("finding_id must be unique.")
        verifier = AssertionVerifier(self.repository, self.artifacts, view)
        for finding in diagnosis.findings:
            errors.extend(f"findings[{finding.finding_id}]: {e}" for e in finding_errors(finding, sources, view))
            if not {a.evidence_id for a in finding.assertions}.issubset(c.evidence_id for c in finding.citations):
                errors.append(f"findings[{finding.finding_id}]: assertions must reference cited evidence.")
            else:
                try:
                    verifier.render(finding.assertions)
                except (ValueError, PermissionError, OSError):
                    errors.append(f"findings[{finding.finding_id}]: structured assertion does not match readable source data.")
        return errors


    def pending_diagnosis(self, view):
        records = self.repository.list_recent_events_by_type(
            view.investigation_id, "mcp.native.diagnosis_ready", limit=1
        )
        if not records:
            return None
        ref = records[0]["data"]["artifact_ref"]
        if not self.artifacts.validate_scope(ref, view.request.tenant_id, view.request.project_id):
            raise PermissionError("diagnosis draft belongs to another project")
        pending = json.loads(self.artifacts.read_bytes(ref))
        if pending["investigation_id"] != view.investigation_id:
            raise PermissionError("diagnosis draft belongs to another investigation")
        current_inputs = [
            r["command_id"] for r in self.repository.list_user_inputs(view.investigation_id)
        ]
        if (pending["input_ids"] != current_inputs
                or pending.get("recall_generation", 0) != RecallService(self.repository).generation(view.investigation_id)
                or pending.get("feedback_generation", 0) != FeedbackStore(self.repository).generation(view.investigation_id)):
            return None
        diagnosis = NativeDiagnosis.model_validate(pending["diagnosis"])
        return (
            (diagnosis, pending["reason"]) if not self.citation_errors(view, diagnosis) else None
        )


    def finish(self, view, diagnosis, reason):
        if diagnosis is not None and self.citation_errors(view, diagnosis):
            diagnosis, reason = None, "native_report_validation_failed"
        if diagnosis is not None:
            draft = self.artifacts.put_bytes(
                view.request.tenant_id,
                view.request.project_id,
                canonical_json(
                    {
                        "investigation_id": view.investigation_id,
                        "reason": reason,
                        "diagnosis": diagnosis.model_dump(mode="json"),
                        "recall_generation": RecallService(self.repository).generation(view.investigation_id),
                        "feedback_generation": FeedbackStore(self.repository).generation(view.investigation_id),
                        "input_ids": [
                            r["command_id"]
                            for r in self.repository.list_user_inputs(view.investigation_id)
                        ],
                    }
                ).encode(),
            )
            self.repository.record_investigation_event(
                view.investigation_id, "mcp.native.diagnosis_ready", {"artifact_ref": draft.ref}
            )
        if (
            self.repository.get_investigation(view.investigation_id).status
            != InvestigationStatus.REPORTING
        ):
            self.plan_transition(view.investigation_id)
            self.repository.transition(view.investigation_id, InvestigationStatus.REPORTING)
        if diagnosis is None:
            report = ReportBuilder(self.repository).build(view.investigation_id, reason, status="failed")
        else:
            claims = []
            observations = []
            model_interpretations = []
            verifier = AssertionVerifier(self.repository, self.artifacts, view)
            evidence = {
                e.evidence_id: e
                for e in self.repository.list_current_evidence(view.investigation_id)
            }
            access = ContextAccess(self.repository, self.artifacts)
            all_citations = diagnosis.citations + [c.model_dump() for f in diagnosis.findings for c in f.citations]
            for citation in all_citations:
                record = evidence.get(citation.get("evidence_id"))
                quote = citation.get("quote", "")
                if record is None or not quote or len(quote.encode()) > 3000:
                    continue
                content = self.artifacts.read_bytes(record.artifact_ref)
                offset = content.find(quote.encode())
                if offset < 0:
                    continue
                excerpt = access.read_evidence(
                    view.investigation_id,
                    record.evidence_id,
                    ContextAccessScope(
                        tenant_id=view.request.tenant_id, project_id=view.request.project_id
                    ),
                    byte_offset=offset,
                    max_bytes=4096,
                )
                self.repository.record_investigation_event(
                    view.investigation_id,
                    "evidence.excerpt_read",
                    {"excerpt": excerpt.model_dump(mode="json")},
                )
                claims.append(
                    Claim(
                        claim_id=f"native-quote-{len(claims)+1}",
                        text=quote,
                        basis="evidence_excerpt",
                        supporting_excerpt=quote,
                        evidence_ids=[record.evidence_id],
                        limitations=record.limitations,
                    )
                )
            native_sources = source_index(self.repository, view.investigation_id)
            for finding in diagnosis.findings:
                text = verifier.render(finding.assertions) if finding.assertions else "待核实解释：" + finding.text
                observations.append(text)
                if finding.assertions:
                    model_interpretations.append("模型解释（未作语义支持验证）：" + finding.text)
                source_values = [native_sources.get(
                    c.evidence_id, {}).get("values", {}) for c in finding.citations]
                missing = [key for key in ("run_id", "build_id", "environment", "timestamp", "phase")
                           if any(not values.get(key) for values in source_values)]
                claims.append(Claim(
                    claim_id="finding-" + finding.finding_id[:110], text=text,
                    basis="evidence_excerpt", supporting_excerpt=finding.citations[0].quote,
                    evidence_ids=list(dict.fromkeys(c.evidence_id for c in finding.citations)),
                    scope="run:" + finding.run_id if finding.run_id else finding.scope,
                    limitations=finding.limitations + [
                        "结构化字段仅按原文和声明的运算核验，不证明因果或未覆盖范围。"
                        if finding.assertions else "引用已核对，解释的语义支持关系与因果关系未经验证。"]
                        + (["来源字段不完整：" + ", ".join(missing)] if missing else []),
                    assertions=finding.assertions,
                ))
            declared = view.request.constraints.get("experiment_expectations")
            expectations = isinstance(declared, dict) and any(
                isinstance(declared.get(k), list)
                and declared[k]
                and all(isinstance(v, str) and v.strip() for v in declared[k])
                for k in ("expected_effects", "violation_conditions")
            )
            # A textual expectation and a model label do not establish a checked
            # predicate. Keep the suggestion visible without certifying it.
            interpretation = "unknown"
            labels = {
                "expected_effect": "符合声明的预期影响",
                "unexpected_effect": "超出声明的实验预期",
                "unknown": "实验预期或证据不足，暂不判定是否异常",
            }
            limitations = list(diagnosis.limitations)
            limitations.extend(model_interpretations)
            if expectations and diagnosis.interpretation != "unknown":
                limitations.append("模型建议的预期判读为 " + diagnosis.interpretation
                                   + "，尚未绑定可执行验收规则，保持 unknown。")
            if not diagnosis.findings:
                limitations.append("兼容旧报告格式：概括未提供逐条主张绑定，不能作为语义或根因验证。")
            unavailable = self.repository.list_recent_events_by_type(
                view.investigation_id, "mcp.native.provider_unavailable", limit=100)
            limitations.extend("证据来源暂不可用：" + row["data"]["provider_id"] for row in unavailable)
            if not expectations:
                limitations.append(
                    "未提供明确的实验预期和业务基线，不能仅凭注入期阈值失败认定游戏缺陷。"
                )
            limitations.append("原文引用已核对；这不代表原因推断已被验证。")
            limitations.extend("待验证假设：" + h for h in diagnosis.hypotheses)
            self.repository.record_investigation_event(
                view.investigation_id,
                "mcp.native.assessment",
                {
                    "interpretation": interpretation,
                    "model_interpretation": diagnosis.interpretation,
                    "basis": diagnosis.interpretation_basis,
                    "hypotheses": diagnosis.hypotheses,
                },
            )
            observation_text = ("\n".join(f"- {text}" for text in observations)
                                if diagnosis.findings else "待核实解释：" + diagnosis.summary)
            summary = observation_text + "\n\n实验判读：" + labels[interpretation]
            if interpretation != "unknown" and diagnosis.interpretation_basis:
                summary += "。" + diagnosis.interpretation_basis
            report = InvestigationReport(
                investigation_id=view.investigation_id,
                revision=self.repository.next_report_revision(view.investigation_id),
                status="completed",
                resolution="inconclusive",
                conclusion_level="observation",
                summary=summary,
                claims=claims,
                hypothesis_ids=[h.hypothesis_id for h in self.repository.list_hypotheses(view.investigation_id)],
                coverage=Coverage(uncovered_objectives=[view.request.objective.objective_id]),
                unresolved_questions=limitations,
                recommended_actions=diagnosis.recommended_actions,
                stop_reason=reason,
            )
        questions = report_question_progress(self.repository, view)
        if questions:
            report = report.model_copy(update={
                "summary": report.summary + "\n\n" + questions["summary"],
                "unresolved_questions": list(dict.fromkeys(report.unresolved_questions + questions["limitations"])),
            })
            self.repository.record_investigation_event(view.investigation_id,
                "mcp.native.question_coverage", {key: questions[key] for key in
                    ("questions", "next_question_id", "assessment", "plan_revision")})
        if self.repository.pending_cleanup_resources(view.investigation_id):
            note = "仍有外部分析资源未确认清理；调查结束不代表外部操作已经停止，请以清理账本为准。"
            report = report.model_copy(update={"unresolved_questions": list(dict.fromkeys([
                *report.unresolved_questions, note]))})
        if isinstance(self.reports, ReportService):
            self.reports.artifacts = self.artifacts
        self.reports.publish(report)
        self.repository.transition(
            view.investigation_id,
            InvestigationStatus(report.status),
            resolution=report.resolution,
            stop_reason=reason,
        )
        self.repository.record_investigation_event(view.investigation_id,
            "mcp.native.outcome", {"status": report.status, "stop_reason": reason,
                                   "diagnosis_available": diagnosis is not None,
                                   "resolution": report.resolution})
        return report
