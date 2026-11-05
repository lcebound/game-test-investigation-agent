"""Reassess a worker handback, preserving the original input and rejection chain.

Behavior reference: dsh-superagent-prototype@6337fc823ce20251c8de8085948165c81aaa701e,
core/part.ts::handBack/queueForEval and core/evals.ts::handback/decide.
"""
from __future__ import annotations

import json
import logging

from game_test_agent.events.coordinator import excerpt
from game_test_agent.events.input_returns import InputReturnService
from game_test_agent.storage import ConflictError, NotFoundError
from game_test_agent.storage.repository import payload_hash
from game_test_agent.storage.route_evaluations import RouteEvaluationBusy, RouteEvaluationStore


class ReturnedInputRouter:
    PRINCIPAL = "input-return-router"

    def __init__(self, repository, router, returns=None):
        self.repository = repository
        self.router = router
        self.returns = returns or InputReturnService(repository)
        self.store = self.returns.store
        self.evaluations = RouteEvaluationStore(repository)

    def due(self, *, limit=4):
        if not 1 <= limit <= 16:
            raise ValueError("returned-input batch must contain 1–16 items")
        now = self.evaluations.clock()
        return self.repository._connection.execute("""SELECT r.return_id,r.conversation_id,r.revision,
            i.tenant_id,i.project_id FROM task_input_returns r
            JOIN investigations i ON i.investigation_id=r.conversation_id
            LEFT JOIN workbench_conversations w ON w.root_id=r.conversation_id
            LEFT JOIN conversation_route_evaluations e ON e.conversation_id=r.conversation_id
                AND e.principal_id=? AND e.request_key=r.return_id
            WHERE r.state='pending' AND COALESCE(w.archived,0)=0 AND
                (e.request_key IS NULL OR (e.state='running' AND e.lease_deadline<=?)
                 OR (e.state='retryable' AND e.updated_at<=?))
            AND NOT EXISTS (SELECT 1 FROM workbench_message_receipts receipts
                WHERE receipts.root_id=r.conversation_id AND receipts.principal_id=? AND receipts.request_key=r.return_id)
            ORDER BY r.created_at,r.return_id LIMIT ?""",
            (self.PRINCIPAL, now, now - self.evaluations.RECOVERY_DELAY, self.PRINCIPAL, limit)).fetchall()

    @staticmethod
    def _source(row):
        return {key: row[key] for key in ("return_id", "revision", "trigger_id", "content_sha256")}

    @staticmethod
    def _pending_decision(basis, audit=None):
        return {"kind": "clarify", "basis": basis,
                **({"router_assessment": audit} if audit is not None else {})}

    async def reassess(self, root, return_id, tenant, project, *, expected_revision=None):
        root = self.store.conversation_root(root, tenant, project)
        async with self.router.request_lock(return_id):
            with self.repository.transaction() as connection:
                self.repository._lock_investigation_row(connection, root)
                row = self.returns._get(return_id)
                if row["conversation_id"] != root:
                    raise NotFoundError("returned input not found in this conversation")
                source = self._source(row)
                # Revisions change after successful transfer. Inspect the fixed receipt
                # before treating such a retry as a new assessment of another source.
                saved = connection.execute("""SELECT response_json FROM workbench_message_receipts
                    WHERE root_id=? AND principal_id=? AND request_key=?""", (root, self.PRINCIPAL, return_id)).fetchone()
                if saved is not None:
                    return {**json.loads(saved["response_json"])["route_decision"], "duplicate": True}
                if row["state"] != "pending":
                    return {"kind": "superseded", "basis": "return_already_resolved", "duplicate": True}
                if expected_revision is not None and row["revision"] != expected_revision:
                    raise RouteEvaluationBusy("returned input changed since scanning")
                if self.store.get(root, tenant, project)["archived"]:
                    raise ConflictError("restore the conversation before reassigning inputs")
                if self.router.assessor is None:
                    return {**self._pending_decision("semantic_router_disabled"), "duplicate": False}
                excluded = self.returns._excluded(row["trigger_id"])
                lease = self.evaluations.claim_in_transaction(connection, root, self.PRINCIPAL, return_id,
                    payload_hash(source), source, self.router.policy_version, self.router.timeout_seconds)
                try:
                    content = self.returns._content(row)
                    if len(content) > 4000:
                        raise ValueError("returned input exceeds routing context bounds")
                except (ConflictError, NotFoundError, ValueError):
                    content = None
                context = {"source_task_id": row["source_task_id"],
                           "worker_reason_excerpt": excerpt(row["reason"], 240),
                           "reason_assessment": "unverified_worker_routing_assessment"}
            try:
                if content is None:
                    decision = self._pending_decision("source_unavailable")
                elif lease.exhausted:
                    decision = self._pending_decision("assessment_exhausted")
                elif self.router.assessor is None:
                    decision = self._pending_decision("semantic_router_disabled")
                else:
                    decision = await self.router.resolve(root, content, tenant, project,
                        excluded_task_ids=excluded, allow_only_task=False, returned_input=context)
                with self.repository.transaction() as connection:
                    self.repository._lock_investigation_row(connection, root)
                    # The generation fence precedes every possible delivery.
                    self.evaluations.finish_in_transaction(connection, lease)
                    current = self.returns._get(return_id)
                    if current["state"] != "pending" or self._source(current) != source:
                        decision = {"kind": "superseded", "basis": "return_already_resolved"}
                    elif self.store.get(root, tenant, project)["archived"]:
                        raise ConflictError("restore the conversation before reassigning inputs")
                    elif self.router.policy_version != lease.policy_version:
                        decision = self._pending_decision("policy_changed", decision.get("router_assessment"))
                    elif self.returns._excluded(current["trigger_id"]) != excluded:
                        decision = self._pending_decision("return_chain_changed", decision.get("router_assessment"))
                    else:
                        # Verify immutable original even after a model wait.
                        try:
                            if content is None or self.returns._content(current) != content:
                                raise ConflictError("input source changed")
                            decision = self.router.revalidate(decision, tenant, project)
                        except (ConflictError, NotFoundError):
                            decision = self._pending_decision("source_unavailable", decision.get("router_assessment"))
                        if decision["kind"] == "task":
                            connection.execute("SAVEPOINT returned_input_attach")
                            try:
                                self.returns._attach(connection, current, decision["investigation_id"],
                                    tenant, project, self.PRINCIPAL, "semantic_original_reference")
                            except (ConflictError, NotFoundError):
                                connection.execute("ROLLBACK TO SAVEPOINT returned_input_attach")
                                decision = self._pending_decision("target_unavailable", decision.get("router_assessment"))
                            finally:
                                connection.execute("RELEASE SAVEPOINT returned_input_attach")
                        elif decision["kind"] == "new_task":
                            # Original task conditions cannot be invented by a handback.
                            decision = self._pending_decision("independent_task_requires_intake", decision.get("router_assessment"))
                    decision = {key: value for key, value in decision.items() if not key.startswith("_")}
                    result = {**decision, "return_id": return_id, "evaluation_generation": lease.generation, "duplicate": False}
                    self.store.save_receipt_in_transaction(connection, root, self.PRINCIPAL, return_id,
                        payload_hash(source), {"route_decision": result, "original_message": source})
                return result
            except BaseException:
                try:
                    self.evaluations.abandon(lease)
                except Exception:
                    logging.getLogger(__name__).warning("returned-input evaluation release failed; lease will expire")
                raise

    async def recover_due(self, *, limit=4):
        counts = {"scanned": 0, "routed": 0, "pending": 0, "skipped": 0, "deferred": 0}
        if self.router.assessor is None:
            return counts
        for row in self.due(limit=limit):
            counts["scanned"] += 1
            try:
                result = await self.reassess(row["conversation_id"], row["return_id"], row["tenant_id"], row["project_id"],
                    expected_revision=row["revision"])
                counts["routed" if result["kind"] == "task" else "pending"] += 1
            except RouteEvaluationBusy:
                counts["skipped"] += 1
            except Exception:
                # A single inaccessible or changed return must not stop later work.
                counts["deferred"] += 1
        return counts
