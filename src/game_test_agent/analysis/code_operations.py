"""Bounded submit/poll/collect over admitted persistent analysis jobs."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from game_test_agent.contracts.models import AnalysisJobState


ACTIVE_ANALYSES = {AnalysisJobState.PROPOSED, AnalysisJobState.SUBMITTED,
                   AnalysisJobState.RUNNING, AnalysisJobState.UNKNOWN}


class CodeOperations:
    def __init__(self, service):
        self.service = service
        self._lock = asyncio.Lock()

    def key(self, job_id):
        return ("code", self.service.runner.runner_id, job_id)

    def release(self, job_id):
        self.service.execution_limits.release_background(self.key(job_id))

    def feedback(self, job, status="running", failure=None):
        return {"job_id": job.spec.job_id, "execution_status": status, "exit_code": None,
                "failure_kind": failure, "stdout": "", "stderr": "", "outputs": [],
                "output_evidence_ids": [], "isolation_kind": job.spec.environment.isolation_kind,
                "warning": "Execution is not a verified fact. Poll the persisted job; do not resubmit it."}

    def not_executed(self, job, failure):
        repo = self.service.repository
        event_type = "analysis.code_not_executed." + job.spec.job_id
        saved = repo.list_recent_events_by_type(job.spec.investigation_id, event_type, limit=1)
        payload = saved[0]["data"] if saved else self.feedback(job, "not_executed", failure)
        if not saved:
            repo.record_investigation_event(job.spec.investigation_id, event_type, payload)
        self.release(job.spec.job_id)
        return payload

    async def _recover_capacity(self):
        service = self.service
        for job in service.repository.unfinished_analysis_jobs(service.runner.runner_id):
            operation = job.operation_ref or await service.runner.recover_operation(job.spec)
            if operation is not None:
                if job.operation_ref is None:
                    service.sandbox.bind_operation(job.spec.job_id, operation)
                await service.execution_limits.adopt_background(self.key(job.spec.job_id),
                    scope=(job.spec.tenant_id, job.spec.project_id))

    async def advance(self, job, inputs=None):
        """One bounded control pass; no sleep loop and no model invocation."""
        async with self._lock:
            return await self._advance(job, inputs)

    async def _advance(self, job, inputs):
        service = self.service
        repo, runner = service.repository, service.runner
        snapshot = repo.get_latest_analysis_result(job.spec.job_id)
        if snapshot is not None:
            service.sandbox.record_result(job.operation_ref, snapshot.result)
            await service._cleanup(job.operation_ref)
            return service._feedback(job.spec.job_id)
        if job.state not in ACTIVE_ANALYSES:
            return self.not_executed(job, "analysis_" + str(job.state))
        await self._recover_capacity()
        job = repo.get_analysis_job(job.spec.job_id)
        spec, operation = job.spec, job.operation_ref
        view = repo.get_investigation(spec.investigation_id)
        now = datetime.now(timezone.utc)
        stopped = view.status in {"canceling", "canceled", "completed", "failed"}
        expired = now.timestamp() >= min(view.created_at.timestamp() + view.request.budget.max_wall_seconds,
                                        job.created_at.timestamp() + spec.limits.wall_seconds)
        superseded = service.permissions.input_superseded({
            "investigation_id": spec.investigation_id, "requested_at": job.created_at.isoformat()})
        if (stopped or expired or superseded) and operation is None:
            repo.transition_analysis_job(spec.job_id, AnalysisJobState.CANCELED)
            service.sandbox.confirm_cleanup(spec.job_id)
            return self.not_executed(job, "analysis_superseded" if superseded else "analysis_stopped")
        if operation is None:
            if job.state != AnalysisJobState.PROPOSED:
                raise RuntimeError("accepted analysis lost its operation; automatic resubmission forbidden")
            if not await service.execution_limits.try_background(self.key(spec.job_id),
                    scope=(spec.tenant_id, spec.project_id)):
                return self.feedback(job, "waiting_capacity")
            repo.record_resource_cleanup(spec.investigation_id, spec.job_id,
                resource_kind="sandbox", cleanup_owner=runner.runner_id)
            selected = inputs if inputs is not None else [repo.get_evidence(eid) for eid in spec.input_evidence_ids]
            # Validate again after a queued job obtains capacity, before making
            # the runner's immutable manifest or launching any code.
            if any(e.retrieval_status != "ready" or not service.artifacts.validate_scope(
                    e.artifact_ref, spec.tenant_id, spec.project_id) for e in selected):
                self.release(spec.job_id)
                raise PermissionError("queued analysis input is no longer readable")
            runner.authorize_inputs(spec, selected)
            submission = asyncio.create_task(runner.submit(spec))
            try:
                operation = await asyncio.shield(submission)
                service.sandbox.bind_operation(spec.job_id, operation)
            except asyncio.CancelledError:
                # Shutdown cannot orphan an accepted, unbound submission. Keep
                # its operation and capacity; the next owner polls this job.
                operation = await asyncio.shield(submission)
                service.sandbox.bind_operation(spec.job_id, operation)
                raise
            except Exception:
                recovered = await runner.recover_operation(spec)
                if recovered is not None:
                    service.sandbox.bind_operation(spec.job_id, recovered)
                    raise
                repo.transition_analysis_job(spec.job_id, AnalysisJobState.CANCELED)
                service.sandbox.confirm_cleanup(spec.job_id)
                return self.not_executed(job, "analysis_submission_rejected")
        if stopped or expired or superseded:
            await runner.request_cancel(operation)  # Request only; not proof of termination.
        result = await runner.query(operation)
        if result is None:
            return self.feedback(job, "running" if operation.execution_status == "running" else "waiting_result",
                "cancellation_requested" if stopped or expired or superseded else None)
        service.sandbox.record_result(operation, result)
        await service._cleanup(operation)
        feedback = service._feedback(spec.job_id)
        repo.record_investigation_event(spec.investigation_id, "analysis.code_completed", {
            "decision_id": spec.created_by[6:], "job_id": spec.job_id,
            "execution_status": feedback["execution_status"], "failure_kind": feedback["failure_kind"],
            "output_evidence_ids": feedback["output_evidence_ids"],
            "repair_of_job_id": spec.program.parameters.get("repair_of_job_id")})
        return feedback
