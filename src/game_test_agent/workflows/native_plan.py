"""Model-authored investigation guidance, independent of platform tool schemas."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_test_agent.workflows.native_questions import input_fingerprint


class InvestigationStep(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    step_id: str = Field(min_length=1, max_length=48, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=1, max_length=400)
    evidence_needed: list[str] = Field(min_length=1, max_length=4)
    completion_condition: str = Field(min_length=1, max_length=400)
    status: Literal["pending", "in_progress", "completed", "blocked", "skipped"] = "pending"
    evidence_ids: list[str] = Field(default_factory=list, max_length=10)
    outcome: str = Field(default="", max_length=800)
    counterevidence_ids: list[str] = Field(default_factory=list, max_length=10)
    remaining_gaps: list[str] = Field(default_factory=list, max_length=4)
    source_input_ids: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def bounded_evidence_descriptions(self):
        if any(not item.strip() or len(item) > 300 for item in self.evidence_needed):
            raise ValueError("evidence_needed entries must contain 1–300 characters")
        if any(not item.strip() or len(item) > 300 for item in self.remaining_gaps):
            raise ValueError("remaining_gaps entries must contain 1–300 characters")
        if any(not item or len(item) > 256 for item in
               self.evidence_ids + self.counterevidence_ids + self.source_input_ids):
            raise ValueError("reference IDs must contain 1–256 characters")
        if self.status == "completed" and (not self.evidence_ids or not self.outcome):
            raise ValueError("completed steps require evidence_ids and an outcome")
        if self.status in {"blocked", "skipped"} and not self.outcome:
            raise ValueError("blocked or skipped steps require an explanation in outcome")
        return self


class InvestigationPlanUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_revision: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=800)
    steps: list[InvestigationStep] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def coherent_steps(self):
        if len(self.model_dump_json().encode()) > 10000:
            raise ValueError("keep the complete plan within 10000 UTF-8 bytes")
        if len({step.step_id for step in self.steps}) != len(self.steps):
            raise ValueError("step_id must be unique")
        if sum(step.status == "in_progress" for step in self.steps) > 1:
            raise ValueError("only one step may be in_progress")
        if self.expected_revision == 0 and any(
            step.status not in {"pending", "in_progress"} or step.evidence_ids or step.counterevidence_ids or step.outcome
            for step in self.steps
        ):
            raise ValueError("an initial plan cannot claim completed work or collected evidence")
        return self


class InvestigationStepPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    step_id: str = Field(min_length=1, max_length=48, pattern=r"^[A-Za-z0-9_-]+$")
    title: str | None = Field(default=None, min_length=1, max_length=120)
    question: str | None = Field(default=None, min_length=1, max_length=400)
    evidence_needed: list[str] | None = Field(default=None, min_length=1, max_length=4)
    completion_condition: str | None = Field(default=None, min_length=1, max_length=400)
    status: Literal["pending", "in_progress", "completed", "blocked", "skipped"] | None = None
    evidence_ids: list[str] | None = Field(default=None, max_length=10)
    outcome: str | None = Field(default=None, max_length=800)
    counterevidence_ids: list[str] | None = Field(default=None, max_length=10)
    remaining_gaps: list[str] | None = Field(default=None, max_length=4)
    source_input_ids: list[str] | None = Field(default=None, max_length=10)


class InvestigationPlanPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=800)
    steps: list[InvestigationStepPatch] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def unique_patches(self):
        if len({step.step_id for step in self.steps}) != len(self.steps):
            raise ValueError("step_id must be unique")
        return self


def update_native_plan(repository, view, memory, arguments):
    """Validate guidance and evidence membership; never grant tool permissions."""
    revision = memory.plan["revision"] if memory.plan else 0
    if memory.plan:
        patch = InvestigationPlanPatch.model_validate_json(arguments)
        merged = {step["step_id"]: dict(step) for step in memory.plan["steps"]}
        for step in patch.steps:
            changes = step.model_dump(exclude_unset=True)
            previous = merged.get(step.step_id, {})
            changed_question = any(key in changes and changes[key] != previous.get(key)
                                   for key in ("question", "evidence_needed", "completion_condition"))
            if previous.get("status") == "completed" and changed_question:
                if ("status" not in changes or changes["status"] == "completed"
                        and not {"evidence_ids", "outcome"}.issubset(changes)):
                    raise ValueError("changed questions require an explicit status and answer review")
            merged.setdefault(step.step_id, {}).update(changes)
        update = InvestigationPlanUpdate.model_validate(
            {
                "expected_revision": patch.expected_revision,
                "reason": patch.reason,
                "steps": list(merged.values()),
            }
        )
    else:
        update = InvestigationPlanUpdate.model_validate_json(arguments)
    if update.expected_revision != revision:
        raise ValueError(f"stale plan revision; expected_revision must be {revision}")
    evidence = {
        e.evidence_id
        for e in repository.list_current_evidence(view.investigation_id)
        if e.retrieval_status == "ready"
    }
    if any(eid not in evidence for step in update.steps for eid in step.evidence_ids + step.counterevidence_ids):
        raise ValueError("plan evidence_ids must be readable evidence in this investigation")
    inputs = repository.list_user_inputs(view.investigation_id)
    input_ids = {row["command_id"] for row in inputs}
    if any(identity not in input_ids for step in update.steps for identity in step.source_input_ids):
        raise ValueError("source_input_ids must belong to this investigation")
    steps = []
    for step in update.steps:
        data = step.model_dump(mode="json")
        for key in ("counterevidence_ids", "remaining_gaps", "source_input_ids"):
            if not data[key]:
                data.pop(key)
        steps.append(data)
    touched = {step.step_id for step in patch.steps} if memory.plan else {step.step_id for step in update.steps}
    reviewed = dict(memory.plan.get("reviewed_inputs", {})) if memory.plan else {}
    stamp = input_fingerprint(inputs)
    review_changed = any(reviewed.get(sid, input_fingerprint([])) != stamp for sid in touched)
    if memory.plan and steps == memory.plan["steps"] and not review_changed:
        raise ValueError("plan is unchanged; continue collecting evidence or submit a report")
    reviewed.update({sid: stamp for sid in touched})
    plan = {"revision": revision + 1, "reason": update.reason, "steps": steps, "reviewed_inputs": reviewed}
    repository.record_investigation_event(view.investigation_id, "mcp.native.plan_updated", plan)
    memory.plan = plan
    memory.refresh_working_message()
    return {
        "result": "Investigation plan saved. Step status is a model assessment, not causal verification.",
        "revision": plan["revision"],
    }, revision == 0
