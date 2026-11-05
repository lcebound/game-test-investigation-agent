"""Persist an evaluation before asking a model; commit its receipt with its fence."""
from __future__ import annotations

import logging
import json

from game_test_agent.storage import ConflictError
from game_test_agent.storage.repository import canonical_json, payload_hash
from game_test_agent.storage.workbench import WorkbenchStore
from game_test_agent.storage.route_evaluations import RouteEvaluationBusy, RouteEvaluationStore


class ConversationRoutingService:
    def __init__(self, repository, router):
        self.repository = repository
        self.router = router
        self.store = WorkbenchStore(repository)
        self.evaluations = RouteEvaluationStore(repository)

    async def route(self, root, original, tenant, project, principal, token, digest, *, recovery_generation=None):
        async with self.router.request_lock(token):
            with self.repository.transaction() as connection:
                self.repository._lock_investigation_row(connection, root)
                summary = self.store.get(root, tenant, project)
                saved = self.store.receipt(root, principal, token, digest)
                if saved is not None:
                    return {**saved["route_decision"], "duplicate": True}
                if summary["archived"]:
                    raise ConflictError("restore the conversation before sending messages")
                if recovery_generation is not None:
                    self.evaluations.ensure_due_in_transaction(connection, root, principal, token, recovery_generation)
                # Validate source/context before persisting an authorized request.
                self.router.rules.resolve(root, original["content"], tenant, project,
                    context_investigation_id=original.get("context_investigation_id"))
                if original.get("bound_investigation_id"):
                    self.router.rules._target(root, original["bound_investigation_id"], tenant, project, "bound_reply")
                lease = self.evaluations.claim_in_transaction(connection, root, principal, token,
                    digest, original, self.router.policy_version, self.router.timeout_seconds)
            try:
                if lease.exhausted:
                    fallback = self.router.rules.resolve(root, original["content"], tenant, project,
                        context_investigation_id=original.get("context_investigation_id"))
                    decision = self.router._clarify(fallback, [],
                        "这条消息的归属判断多次中断，请明确任务或选择目标。", "assessment_exhausted")
                else:
                    decision = await self.router.resolve(root, original["content"], tenant, project,
                        context_investigation_id=original.get("context_investigation_id"),
                        bound_investigation_id=original.get("bound_investigation_id"))
                with self.repository.transaction() as connection:
                    self.repository._lock_investigation_row(connection, root)
                    summary = self.store.get(root, tenant, project)
                    saved = self.store.receipt(root, principal, token, digest)
                    if saved is not None:
                        return {**saved["route_decision"], "duplicate": True}
                    if summary["archived"]:
                        raise ConflictError("restore the conversation before sending messages")
                    # State and receipt either both commit, or both roll back.
                    self.evaluations.finish_in_transaction(connection, lease)
                    if self.router.policy_version != lease.policy_version:
                        decision = self.router._clarify(decision, [], "路由配置已变化，请重新发送。",
                            "policy_changed", decision.get("router_assessment"))
                    else:
                        decision = self.router.revalidate(decision, tenant, project)
                    decision = {key: value for key, value in decision.items() if not key.startswith("_")}
                    result = {**decision, "route_id": token, "trigger_id": token, "duplicate": False,
                              "evaluation_generation": lease.generation}
                    self.store.save_receipt_in_transaction(connection, root, principal, token, digest,
                        {"route_decision": result, "original_message": original})
                return result
            except BaseException:
                # Best-effort release must not replace cancellation or the original error.
                try:
                    self.evaluations.abandon(lease)
                except Exception:
                    logging.getLogger(__name__).warning("route evaluation release failed; lease will expire")
                raise

    async def recover_due(self, *, limit=4):
        result = {"scanned": 0, "completed": 0, "skipped": 0, "unavailable": 0, "deferred": 0}
        if self.router.assessor is None:
            return result
        for row in self.evaluations.due(limit=limit):
            result["scanned"] += 1
            try:
                raw = self.evaluations.original_for_recovery(row)
                original = self._recovery_original(raw, row["payload_hash"])
            except RouteEvaluationBusy:
                result["skipped"] += 1
                continue
            except (ValueError, TypeError, RecursionError):
                self.evaluations.defer_recovery(row, unavailable=True)
                result["unavailable"] += 1
                continue
            try:
                await self.route(row["conversation_id"], original, row["tenant_id"], row["project_id"],
                    row["principal_id"], row["request_key"], row["payload_hash"], recovery_generation=row["generation"])
                result["completed"] += 1
            except RouteEvaluationBusy:
                result["skipped"] += 1
            except Exception:
                # Other rows still progress. Raw exceptions and source text stay private.
                self.evaluations.defer_recovery(row)
                result["deferred"] += 1
        return result

    @staticmethod
    def _recovery_original(raw, digest):
        if not isinstance(raw, str) or len(raw) > 40000:
            raise ValueError("stored routing source exceeds recovery bounds")
        original = json.loads(raw)
        if not isinstance(original, dict) or set(original) not in ({"content", "context_investigation_id"}, {"content", "context_investigation_id", "bound_investigation_id"}):
            raise ValueError("invalid routing source fields")
        content, context = original["content"], original["context_investigation_id"]
        if not isinstance(content, str) or not content.strip() or len(content) > 4000:
            raise ValueError("invalid stored routing content")
        if context is not None and (not isinstance(context, str) or not 1 <= len(context) <= 96):
            raise ValueError("invalid stored routing context")
        bound = original.get("bound_investigation_id")
        if bound is not None and (not isinstance(bound, str) or not 1 <= len(bound) <= 96):
            raise ValueError("invalid stored reply binding")
        if payload_hash(original) != digest or canonical_json(original) != raw:
            raise ValueError("stored routing source hash changed")
        return original
