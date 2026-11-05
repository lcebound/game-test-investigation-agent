"""Source-pinned cross-task technical comparisons; inference never becomes evidence."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, ConfigDict, Field

from game_test_agent.contracts.models import TERMINAL_STATUSES
from game_test_agent.events.coordinator import excerpt
from game_test_agent.storage import ConflictError
from game_test_agent.storage.repository import payload_hash
from game_test_agent.storage.workbench import WorkbenchStore
from game_test_agent.models import ModelGateway
from game_test_agent.events.semantic_router import RouterModelAssessor


class SynthesisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_ids: list[str] = Field(min_length=2, max_length=8)
    question: str = Field(
        default="比较这些调查的共同现象、差异、反证与待验证问题。", min_length=1, max_length=1000
    )


class SynthesisFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str = Field(min_length=1, max_length=600)
    source_refs: list[str] = Field(min_length=1, max_length=8)


class SynthesisProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    findings: list[SynthesisFinding] = Field(default_factory=list, max_length=8)
    open_questions: list[str] = Field(default_factory=list, max_length=8)


class SynthesisService:
    def __init__(self, repository, settings=None, execution_limits=None, assessor=None):
        self.repository = repository
        self.store = WorkbenchStore(repository)
        self.assessor = assessor
        self._locks = [asyncio.Lock() for _ in range(32)]
        self.gateway = None
        if assessor is None and settings is not None and settings.enable_model:
            adapter = RouterModelAssessor(settings, execution_limits)
            self.profile = adapter.profile.model_copy(
                update={
                    "profile_id": "technical-synthesis",
                    "max_output_tokens": min(1200, settings.model_max_output_tokens),
                }
            )
            self.gateway = ModelGateway(
                [self.profile], adapter._call, execution_limits=execution_limits
            )

    def snapshot(self, root, request, tenant, project):
        root = self.store.conversation_root(root, tenant, project)
        if len(set(request.task_ids)) != len(request.task_ids):
            raise ValueError("synthesis task references must be unique")
        sources, aliases = [], set()
        for identity in request.task_ids:
            task_id, view = self.store.task(root, identity, tenant, project)
            if task_id in aliases:
                raise ValueError("synthesis references resolve to duplicate tasks")
            aliases.add(task_id)
            if view.status not in TERMINAL_STATUSES:
                raise ConflictError("synthesis sources must have finished")
            report, _, digest = self.repository.get_latest_report(view.investigation_id)
            from game_test_agent.reports.evaluator import ConclusionEvaluator

            validation = ConclusionEvaluator(self.repository).evaluate(report)
            if not validation.accepted:
                raise ConflictError("source report no longer passes deterministic validation")
            claims = [
                {
                    "ref": f"{view.investigation_id}/{claim.claim_id}",
                    "text": excerpt(claim.text, 400),
                    "verified": claim.verification_status == "verified",
                    "limitations": [excerpt(item, 200) for item in claim.limitations[:4]],
                    "evidence_ids": claim.evidence_ids[:8],
                }
                for claim in report.claims[:6]
            ]
            sources.append(
                {
                    "task_id": task_id,
                    "investigation_id": view.investigation_id,
                    "report_sha256": digest,
                    "report_revision": report.revision,
                    "report_read_ref": f"/v1/investigations/{view.investigation_id}/reports/{report.revision}",
                    "report_url": f"/v1/investigations/{view.investigation_id}/reports/{report.revision}",
                    "build_ref": view.request.build_ref.model_dump(mode="json")
                    if view.request.build_ref
                    else None,
                    "objective": excerpt(view.request.objective.description, 240),
                    "summary": excerpt(report.summary, 480),
                    "resolution": report.resolution,
                    "claims": claims,
                    "unresolved_questions": [
                        excerpt(item, 240) for item in report.unresolved_questions[:8]
                    ],
                    "omitted_claims": max(0, len(report.claims) - 6),
                    "omitted_questions": max(0, len(report.unresolved_questions) - 8),
                }
            )
        return root, sources

    async def synthesize(self, root, request, tenant, project):
        lock = self._locks[
            int(payload_hash([root, request.model_dump()])[:8], 16) % len(self._locks)
        ]
        async with lock:
            return await self._synthesize(root, request, tenant, project)

    async def _synthesize(self, root, request, tenant, project):
        root, sources = self.snapshot(root, request, tenant, project)
        cache_key = payload_hash(
            [
                "synthesis-v1",
                request.model_dump(),
                sources,
                self.profile.model_dump(mode="json") if self.gateway else "deterministic",
            ]
        )
        if self.assessor is None:
            cached = self.store.receipt(root, "technical-synthesis", cache_key, cache_key)
            if cached is not None:
                return cached
        refs = {claim["ref"] for source in sources for claim in source["claims"]}
        packet = {
            "question": request.question,
            "sources": sources,
            "policy": "Compare reports; never assert a shared root cause, transfer build acceptance, or treat a worker report as independent evidence. Every finding needs source_refs copied from claims. Preserve conflicts, limitations, missing controls and gaps. Return JSON {findings:[{statement,source_refs}],open_questions:[str]}.",
        }
        usage = None
        if self.assessor:
            result = await self.assessor(packet)
            proposal = SynthesisProposal.model_validate(result)
        elif self.gateway:
            from game_test_agent.storage.repository import canonical_json

            messages = [
                {"role": "system", "content": packet.pop("policy")},
                {"role": "user", "content": canonical_json(packet)},
            ]
            bound = len(canonical_json(messages).encode()) + 512
            if bound + self.profile.max_output_tokens > min(
                self.profile.context_limit_tokens, self.profile.max_total_tokens
            ):
                raise ValueError("synthesis context budget exceeded; select fewer tasks")
            estimated_cost = (
                bound * self.profile.cost_per_input_token
                + self.profile.max_output_tokens * self.profile.cost_per_output_token
            )
            if (
                self.profile.max_estimated_cost is not None
                and estimated_cost > self.profile.max_estimated_cost
            ):
                raise ValueError("synthesis cost budget exceeded")
            result = await self.gateway.invoke(
                self.profile.profile_id,
                {"messages": messages, "input_bound": bound},
                SynthesisProposal,
                project_scope=(tenant, project),
            )
            proposal, usage = result.output, result.usage.model_dump(mode="json")
        else:
            proposal = SynthesisProposal(
                findings=[
                    SynthesisFinding(statement=claim["text"], source_refs=[claim["ref"]])
                    for source in sources
                    for claim in source["claims"]
                ][:8]
            )
        for finding in proposal.findings:
            if not set(finding.source_refs) <= refs:
                raise ValueError("synthesis cited an unprovided claim")
        _, refreshed = self.snapshot(root, request, tenant, project)
        if payload_hash(refreshed) != payload_hash(sources):
            raise ConflictError("source reports or task heads changed during synthesis")
        gaps = list(
            dict.fromkeys(
                [
                    *(item for source in sources for item in source["unresolved_questions"]),
                    *proposal.open_questions,
                ]
            )
        )
        response = {
            "conversation_id": root,
            "sources": sources,
            "findings": [finding.model_dump() for finding in proposal.findings],
            "open_questions": gaps,
            "usage": usage,
            "status": "cross_task_inference_requires_validation",
            "limitations": [
                "Source reports retain their own build, environment and confidence boundaries.",
                "Synthesis is not new evidence, a confirmed shared cause, or permission to run tests.",
            ],
        }
        with self.repository.transaction() as connection:
            self.repository._lock_investigation_row(connection, root)
            _, current = self.snapshot(root, request, tenant, project)
            if payload_hash(current) != payload_hash(sources):
                raise ConflictError("source reports changed before synthesis commit")
            if self.assessor is None:
                self.store.save_receipt_in_transaction(
                    connection, root, "technical-synthesis", cache_key, cache_key, response
                )
            self.repository._insert_event(
                connection,
                root,
                "conversation.synthesis_completed",
                {"source_hash": payload_hash(sources), "task_count": len(sources), "usage": usage},
            )
        return response
