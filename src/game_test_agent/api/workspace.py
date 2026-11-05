"""Scoped workbench projections and conversations over the investigation control plane."""

import asyncio
import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from game_test_agent.api.auth import Principal
from game_test_agent.api.workspace_models import ConversationMessage, ConversationUpdate, CodePermissionDecision, ConversationRouteRequest
from game_test_agent.analysis.permissions import CodePermissionStore
from game_test_agent.analysis.approval_keys import local_approval_key
from game_test_agent.contracts.models import (
    ContextAccessScope,
    InputCommand,
    InvestigationRequest,
    InvestigationStatus,
    ReopenRequest,
    TestObjective,
)
from game_test_agent.mcp_gateway import ProviderManager, load_provider_configs
from game_test_agent.mcp_gateway.catalog import TestCatalogPage, TestCatalogProvider
from game_test_agent.mcp_gateway.gateway import StandardToolError
from game_test_agent.storage import ConflictError, NotFoundError
from game_test_agent.storage.repository import payload_hash
from game_test_agent.storage.workbench import WorkbenchStore
from game_test_agent.events.coordinator import ConversationCoordinator
from game_test_agent.events.semantic_router import RouterModelAssessor, SemanticConversationRouter
from game_test_agent.events.router_service import ConversationRoutingService
from game_test_agent.storage.route_evaluations import RouteEvaluationBusy
from game_test_agent.events.input_returns import InputReturnService, InputReturnDecision
from game_test_agent.events.returned_input_router import ReturnedInputRouter
from game_test_agent.events.observation_router import ObservationBatchRouter, ObservationEvent
from game_test_agent.memory.synthesis import SynthesisService, SynthesisRequest
from game_test_agent.events.feedback import RunFeedbackEvent, RunSubscription
from game_test_agent.events.ingress import EventIngress
from game_test_agent.events.policy import load_event_source_policies
from game_test_agent.storage.feedback import FeedbackStore
from game_test_agent.workflows.native_recall import ContextRead
from game_test_agent.memory.recall import RecallService, RecallLink, HistorySearch
from game_test_agent.planning.runtime import PlanRuntimeProjector
from game_test_agent.workflows.native_evidence import source_index
from game_test_agent.workflows.native_questions import question_coverage


def _provider_error_code(error: Exception) -> str:
    known = {
        "INTEGRATION_NOT_CONFIGURED",
        "PERMISSION_DENIED",
        "AUTHENTICATION_FAILED",
        "CONNECTION_LOST",
        "CONNECTION_FAILED",
        "TIMEOUT",
        "SCHEMA_MISMATCH",
        "CAPABILITY_UNAVAILABLE",
        "NOT_FOUND",
        "RATE_LIMITED",
        "UNSUPPORTED_OPERATION",
        "PROVIDER_UNAVAILABLE",
        "SERVICE_UNAVAILABLE",
        "INVALID_REQUEST",
    }
    if isinstance(error, StandardToolError) and error.code in known:
        return error.code
    return "TIMEOUT" if isinstance(error, TimeoutError) else "CONNECTION_FAILED"


def _public_endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))
    except ValueError:
        return ""


def install_workspace(
    app: FastAPI, container: Any, providers: ProviderManager | None, authenticate: Any
) -> None:
    static = Path(__file__).parent / "static"
    app.mount("/assets", StaticFiles(directory=static), name="workbench-assets")
    settings, repository, registry = container.settings, container.repository, container.registry
    conversations = WorkbenchStore(repository)
    coordinator = ConversationCoordinator(repository)
    message_router = SemanticConversationRouter(repository,
        RouterModelAssessor(settings, container.execution_limits) if settings.enable_semantic_router else None,
        timeout_seconds=settings.router_timeout_seconds)
    app.state.message_router = message_router
    routing_service = ConversationRoutingService(repository, message_router)
    app.state.routing_service = routing_service
    input_returns = InputReturnService(repository)
    routing_service.returned_inputs = ReturnedInputRouter(repository, message_router, input_returns)
    app.state.returned_input_router = routing_service.returned_inputs
    routing_service.observations = ObservationBatchRouter(repository, message_router, load_event_source_policies(settings.event_source_policies_json))
    synthesis = SynthesisService(repository, settings, container.execution_limits)
    app.state.synthesis_service = synthesis
    feedback_store = FeedbackStore(repository)
    recall = RecallService(repository, container.context_access.artifacts)
    feedback_ingress = EventIngress(repository, load_event_source_policies(settings.event_source_policies_json))
    code_permissions = CodePermissionStore(repository)
    app.state.local_approval_key = local_approval_key(settings)
    projects = {
        item.provider_id: item.project_ids
        for item in load_provider_configs(settings.mcp_providers_json)
    }
    kinds = {provider_id: "mcp" for provider_id in projects}

    def accessible(provider_id: str, principal: Principal):
        if providers is None or principal.project_id not in projects.get(provider_id, []):
            raise HTTPException(status_code=404, detail="provider not configured for this project")
        return providers.provider(provider_id)

    def scoped(investigation_id: str, principal: Principal):
        view = repository.get_investigation(investigation_id)
        if (view.request.tenant_id, view.request.project_id) != (
            principal.tenant_id,
            principal.project_id,
        ):
            raise HTTPException(status_code=403, detail="investigation scope mismatch")
        return view

    @app.get("/", include_in_schema=False)
    async def workbench():
        return FileResponse(static / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/v1/workspace")
    async def workspace(principal: Annotated[Principal, Depends(authenticate)]):
        bridge_status = getattr(app.state, "event_bridge_status", "disabled")
        if bridge_status == "ready" and getattr(app.state.event_bridge, "connected", False) is not True:
            bridge_status = "reconnecting"
        return {
            "auth_mode": settings.auth_mode,
            "model": {
                "enabled": settings.enable_model,
                "name": settings.model_name,
                "api": settings.model_api,
                "key_configured": bool(os.getenv("OPENAI_API_KEY")),
                "compatible_base_url": _public_endpoint(os.getenv("OPENAI_BASE_URL", "")),
                "native_base_url": _public_endpoint(os.getenv("GTA_MODEL_NATIVE_BASE_URL", "")),
                "local_configured": bool(os.getenv("GTA_LOCAL_CONFIG_FILE")),
            },
            "workflow_backend": settings.workflow_backend,
            "coordination": {"semantic_router": settings.enable_semantic_router,
                "recovery_enabled": settings.enable_router_recovery,
                "execution": container.execution_limits.snapshot(scope=(principal.tenant_id,principal.project_id)),
                "sources": [{"source": policy.source,"automatic_investigation": policy.allow_observation_investigations,
                    "readonly_ready": {"runs:read","evidence:read"}.issubset(policy.scopes),"task_limit":policy.max_observation_tasks}
                    for policy in load_event_source_policies(settings.event_source_policies_json).values()
                    if (policy.tenant_id,policy.project_id)==(principal.tenant_id,principal.project_id)]},
            "mcp_mode": "native_tools" if settings.mcp_native_tools else "typed_contract",
            "default_budget": settings.default_budget(),
            "event_channel": {"enabled": settings.enable_local_event_bridge, "status": bridge_status},
            "features": {"conversations": True, "conversation_tasks": True,
                         "conversation_message_routing": "rules-v1",
                         "streaming": True, "artifact_download": True},
            "providers": [
                {
                    "provider_id": key,
                    "kind": kinds[key],
                    "catalog_supported": (
                        isinstance(accessible(key, principal), TestCatalogProvider)
                        and accessible(key, principal).catalog_supported
                    ),
                }
                for key in sorted(projects)
                if principal.project_id in projects[key]
            ],
        }

    @app.get("/v1/investigations")
    async def history(
        principal: Annotated[Principal, Depends(authenticate)],
        before: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ):
        items = repository.list_investigations(
            principal.tenant_id, principal.project_id, before=before, limit=limit
        )
        return {
            "items": items,
            "next_cursor": items[-1].investigation_id if len(items) == limit else None,
        }

    @app.post("/v1/providers/{provider_id}/probe")
    async def probe(provider_id: str, principal: Annotated[Principal, Depends(authenticate)]):
        provider = accessible(provider_id, principal)
        if "tests:read" not in principal.scopes:
            raise HTTPException(status_code=403, detail="tests:read scope required")
        try:
            descriptors = await asyncio.wait_for(provider.discover_capabilities(), timeout=35)
            # A provider must never register descriptors for a different identity/project.
            if any(
                item.provider_id != provider_id or principal.project_id not in item.project_ids
                for item in descriptors
            ):
                return {
                    "provider_id": provider_id,
                    "available": False,
                    "error_code": "SCHEMA_MISMATCH",
                    "capabilities": [],
                }
            for descriptor in descriptors:
                registry.register(descriptor)
            allowed = [
                item for item in descriptors if set(item.required_scopes).issubset(principal.scopes)
            ]
            return {
                "provider_id": provider_id,
                "available": True,
                "error_code": None,
                "capabilities": [item.model_dump(mode="json", by_alias=True) for item in allowed],
            }
        except Exception as error:
            # Do not echo upstream bodies, endpoints, tokens, or exception text into the browser.
            return {
                "provider_id": provider_id,
                "available": False,
                "error_code": _provider_error_code(error),
                "capabilities": [],
            }

    @app.get("/v1/providers/{provider_id}/tests", response_model=TestCatalogPage)
    async def tests(
        provider_id: str,
        principal: Annotated[Principal, Depends(authenticate)],
        query: Annotated[str, Query(max_length=512)] = "",
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 50,
    ):
        provider = accessible(provider_id, principal)
        if "tests:read" not in principal.scopes:
            raise HTTPException(status_code=403, detail="tests:read scope required")
        if not isinstance(provider, TestCatalogProvider) or not provider.catalog_supported:
            raise HTTPException(status_code=501, detail="provider has no test catalog")
        try:
            return await asyncio.wait_for(
                provider.list_tests(query, page=page, page_size=page_size), timeout=35
            )
        except Exception as error:
            code = _provider_error_code(error)
            raise HTTPException(status_code=502, detail=code) from error

    def projection(investigation_id: str, principal: Principal):
        view = scoped(investigation_id, principal)
        plan = repository.get_latest_plan(investigation_id)
        try:
            report, markdown, digest = repository.get_latest_report(investigation_id)
            report_view = {"report": report, "markdown": markdown, "sha256": digest}
        except NotFoundError:
            report_view = None
        actions = repository.list_actions(investigation_id)
        inputs = repository.list_user_inputs(investigation_id, include_returned=True)
        active_inputs = repository.list_user_inputs(investigation_id)
        evidence = repository.list_current_evidence(investigation_id)
        native_runtime = None
        native_plan_history = []
        if settings.mcp_native_tools:
            native_plan_history = repository.list_recent_events_by_type(
                investigation_id, "mcp.native.plan_updated", limit=20
            )
            event_types = ['model_completed', 'input_applied', 'assessment', 'tool_reused',
                           'tool_error', 'model_error']
            telemetry = repository.list_recent_events_by_types(investigation_id,
                ['mcp.native.' + name for name in event_types])
            grouped = {name: [e for e in telemetry if e['event_type'] == 'mcp.native.' + name]
                       for name in event_types}
            calls, applied, assessments = (grouped[k] for k in
                                           ('model_completed', 'input_applied', 'assessment'))
            native_runtime = {
                'model_responses':len(calls),
                'input_tokens':sum(e['data'].get('input_tokens', 0) or 0 for e in calls),
                'output_tokens':sum(e['data'].get('output_tokens', 0) or 0 for e in calls),
                'model_duration_ms':sum(e['data'].get('duration_ms', 0) or 0 for e in calls),
                'cache_hits':len(grouped['tool_reused']),
                'tool_errors':len(grouped['tool_error']),
                'model_errors':len(grouped['model_error']),
                'applied_input_ids':[e['data']['command_id'] for e in applied],
                'assessment':assessments[-1]['data'] if assessments else None,
                'model_budget_usage': repository.native_model_usage(investigation_id),
                'model_budget_limits': {'tokens': settings.model_max_total_tokens,
                                        'estimated_cost': settings.model_max_estimated_cost},
                'hypotheses': [h.model_dump(mode="json") for h in repository.list_hypotheses(investigation_id)],
                'evidence_sources': source_index(repository, investigation_id),
                'question_coverage': question_coverage(
                    native_plan_history[-1]['data'] if native_plan_history else None,
                    active_inputs, {e.evidence_id for e in evidence if e.retrieval_status == 'ready'}),
                'next_action_selection': (repository.list_recent_events_by_type(
                    investigation_id, 'mcp.native.candidate_selection', limit=1) or [{}])[-1].get('data'),
            }
        plan = PlanRuntimeProjector().project(
            plan,
            actions=actions,
            assessment=repository.latest_assessment(investigation_id),
            has_user_input=bool(active_inputs),
            report_published=report_view is not None,
        )
        return {
            "investigation": view,
            "plan": plan,
            "inputs": inputs,
            "input_returns": [input_returns._card(row) for row in repository._connection.execute(
                "SELECT * FROM task_input_returns WHERE source_investigation_id=? ORDER BY created_at", (investigation_id,)).fetchall()],
            "external_run_feedback": feedback_store.inline(investigation_id),
            "historical_sources": recall.inline(view),
            "code_permissions": code_permissions.list(investigation_id),
            "decisions": [
                {
                    "decision_id": item.decision_id,
                    "round": item.plan_revision,
                    "decision": item.decision,
                    "created_at": item.created_at,
                }
                for item in repository.list_decision_records(investigation_id)
            ],
            "actions": actions,
            "runs": [
                result
                for item in actions
                if (result := repository.get_latest_run_result(item["action"]["action_id"]))
            ],
            "evidence": evidence,
            "budget_usage": repository.budget_usage(investigation_id),
            "native_runtime": native_runtime,
            "native_plan": native_plan_history[-1]["data"] if native_plan_history else None,
            "native_plan_history": native_plan_history,
            "events": repository.list_recent_events(investigation_id, limit=100),
            "report": report_view,
            "pending_cleanup_action_ids": repository.pending_cleanup_actions(investigation_id),
            "pending_cleanup_resources": repository.pending_cleanup_resources(investigation_id),
        }

    @app.get("/v1/investigations/{investigation_id}/workspace")
    async def snapshot(
        investigation_id: str, principal: Annotated[Principal, Depends(authenticate)]
    ):
        return projection(investigation_id, principal)

    @app.get("/v1/investigations/{investigation_id}/code-permissions")
    async def list_code_permissions(
        investigation_id: str, principal: Annotated[Principal, Depends(authenticate)],
    ):
        scoped(investigation_id, principal)
        return {"items": code_permissions.list(investigation_id)}

    def require_history(principal):
        if not {"runs:read", "evidence:read"}.issubset(principal.scopes):
            raise HTTPException(status_code=403, detail="runs:read and evidence:read required for history")

    @app.get("/v1/history/search")
    async def history_search(principal: Annotated[Principal, Depends(authenticate)],
        query: Annotated[str, Query(min_length=1, max_length=200)],
        before: Annotated[str | None, Query(min_length=1, max_length=96)] = None,
        limit: Annotated[int, Query(ge=1, le=10)] = 5):
        require_history(principal)
        if not query.strip():
            raise HTTPException(status_code=422, detail="history query cannot be blank")
        return recall.search_project(principal.tenant_id, principal.project_id,
                                     HistorySearch(query=query, before=before, limit=limit))

    @app.get("/v1/investigations/{investigation_id}/recall")
    async def historical_sources(investigation_id: str, principal: Annotated[Principal, Depends(authenticate)]):
        require_history(principal)
        return recall.inline(scoped(investigation_id, principal))

    @app.post("/v1/investigations/{investigation_id}/recall")
    async def attach_history(investigation_id: str, link: RecallLink,
        principal: Annotated[Principal, Depends(authenticate)]):
        require_history(principal)
        view = scoped(investigation_id, principal)
        try:
            result = recall.link(view, link)
        except PermissionError as error:
            raise HTTPException(status_code=403, detail="historical source scope mismatch") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        repository.record_audit(principal_id=principal.principal_id, tenant_id=principal.tenant_id,
            project_id=principal.project_id, action="recall.source.attached", target_id=investigation_id,
            metadata={"source_investigation_id": link.source_investigation_id, **result})
        return result

    @app.delete("/v1/investigations/{investigation_id}/recall/{source_id}")
    async def detach_history(investigation_id: str, source_id: str,
        principal: Annotated[Principal, Depends(authenticate)]):
        require_history(principal)
        recall.revoke(scoped(investigation_id, principal), source_id)
        repository.record_audit(principal_id=principal.principal_id, tenant_id=principal.tenant_id,
            project_id=principal.project_id, action="recall.source.revoked", target_id=investigation_id,
            metadata={"source_investigation_id": source_id})
        return {"active": False}

    @app.post("/v1/investigations/{investigation_id}/recall/read")
    async def read_history(investigation_id: str, read: ContextRead,
        principal: Annotated[Principal, Depends(authenticate)]):
        require_history(principal)
        view = scoped(investigation_id, principal)
        try:
            return recall.read(view, read)
        except PermissionError as error:
            raise HTTPException(status_code=403, detail="historical reference is not attached or available") from error
        except (ValueError, OSError, LookupError) as error:
            raise HTTPException(status_code=422, detail="historical reference is unreadable or changed") from error

    @app.post("/v1/events/run-feedback", status_code=202)
    async def receive_run_feedback(
        event: RunFeedbackEvent, principal: Annotated[Principal, Depends(authenticate)],
    ):
        if "events:write" not in principal.scopes or (event.tenant_id, event.project_id) != (principal.tenant_id, principal.project_id):
            raise HTTPException(status_code=403, detail="events:write in the event project is required")
        try:
            return feedback_ingress.accept(event.model_dump_json().encode())
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/v1/events/observations", status_code=202)
    async def receive_observation(event: ObservationEvent, principal: Annotated[Principal, Depends(authenticate)]):
        if "events:write" not in principal.scopes or (event.tenant_id,event.project_id) != (principal.tenant_id,principal.project_id):
            raise HTTPException(status_code=403, detail="events:write in the event project is required")
        try:
            return feedback_ingress.accept(event.model_dump_json().encode())
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/v1/investigations/{investigation_id}/subscriptions")
    async def list_subscriptions(investigation_id: str, principal: Annotated[Principal, Depends(authenticate)],
        offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 100):
        scoped(investigation_id, principal)
        items = feedback_store.subscriptions(investigation_id, offset=offset, limit=limit + 1)
        return {"items": items[:limit], "next_offset": offset + limit if len(items) > limit else None}

    @app.post("/v1/investigations/{investigation_id}/subscriptions")
    async def subscribe_run(investigation_id: str, subscription: RunSubscription,
                            principal: Annotated[Principal, Depends(authenticate)]):
        view = scoped(investigation_id, principal)
        if "runs:read" not in principal.scopes:
            raise HTTPException(status_code=403, detail="runs:read scope required")
        try:
            identity = feedback_store.subscribe(view, subscription)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        repository.record_audit(principal_id=principal.principal_id, tenant_id=principal.tenant_id,
            project_id=principal.project_id, action="task.subscription.enabled", target_id=investigation_id,
            metadata={"subscription_id": identity})
        return {"subscription_id": identity}

    @app.delete("/v1/investigations/{investigation_id}/subscriptions/{subscription_id}")
    async def unsubscribe_run(investigation_id: str, subscription_id: str,
                              principal: Annotated[Principal, Depends(authenticate)]):
        scoped(investigation_id, principal)
        if "runs:read" not in principal.scopes:
            raise HTTPException(status_code=403, detail="runs:read scope required")
        with repository.transaction() as connection:
            if not connection.execute("SELECT 1 FROM task_subscriptions WHERE subscription_id=? AND investigation_id=?", (subscription_id, investigation_id)).fetchone():
                raise NotFoundError("subscription not found in this investigation")
            connection.execute("UPDATE task_subscriptions SET active=0 WHERE subscription_id=?", (subscription_id,))
        repository.record_audit(principal_id=principal.principal_id, tenant_id=principal.tenant_id,
            project_id=principal.project_id, action="task.subscription.disabled", target_id=investigation_id,
            metadata={"subscription_id": subscription_id})
        return {"subscription_id": subscription_id, "active": False}

    @app.get("/v1/investigations/{investigation_id}/feedback")
    async def feedback_directory(investigation_id: str, principal: Annotated[Principal, Depends(authenticate)],
        after: Annotated[int, Query(ge=0, le=2**63-1)] = 0, limit: Annotated[int, Query(ge=1, le=20)] = 10):
        scoped(investigation_id, principal)
        return feedback_store.read(investigation_id, ContextRead(ref="feedbacks", offset=after, limit=limit))

    @app.get("/v1/investigations/{investigation_id}/feedback/{cursor}")
    async def feedback_original(investigation_id: str, cursor: int,
        principal: Annotated[Principal, Depends(authenticate)],
        byte_offset: Annotated[int, Query(ge=0)] = 0, max_bytes: Annotated[int, Query(ge=128, le=4096)] = 4096):
        scoped(investigation_id, principal)
        try:
            return feedback_store.read(investigation_id, ContextRead(ref=f"feedback/{cursor}", byte_offset=byte_offset, max_bytes=max_bytes))
        except PermissionError as error:
            raise NotFoundError("feedback not found in this investigation") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/v1/investigations/{investigation_id}/code-permissions/{approval_id}/decision")
    async def decide_code_permission(
        investigation_id: str, approval_id: str, decision: CodePermissionDecision,
        principal: Annotated[Principal, Depends(authenticate)], request: Request,
    ):
        scoped(investigation_id, principal)
        if settings.auth_mode == "dev_headers" and not secrets.compare_digest(
            request.headers.get("X-Code-Approval-Key", "").encode(), app.state.local_approval_key.encode()
        ):
            raise HTTPException(status_code=403, detail="请从启动终端显示的授权入口打开工作台，再审批。")
        if code_permissions.get(approval_id)["investigation_id"] != investigation_id:
            raise HTTPException(status_code=404, detail="permission request not found in this investigation")
        try:
            return code_permissions.decide(approval_id, decision.request_hash, decision.decision,
                                           principal, decision.reason)
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    @app.get("/v1/conversations")
    async def conversation_history(
        principal: Annotated[Principal, Depends(authenticate)],
        query: Annotated[str, Query(max_length=200)] = "",
        archived: bool = False,
        offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ):
        items = conversations.list(
            principal.tenant_id,
            principal.project_id,
            query=query,
            archived=archived,
            offset=offset,
            limit=limit + 1,
        )
        return {
            "items": items[:limit],
            "next_offset": offset + limit if len(items) > limit else None,
        }

    @app.patch("/v1/conversations/{conversation_id}")
    async def update_conversation(
        conversation_id: str,
        change: ConversationUpdate,
        principal: Annotated[Principal, Depends(authenticate)],
    ):
        changes = change.model_dump(exclude_none=True, exclude={"expected_revision"})
        updated = conversations.update(
            conversation_id,
            principal.tenant_id,
            principal.project_id,
            change.expected_revision,
            **changes,
        )
        repository.record_audit(
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            project_id=principal.project_id,
            action="conversation.update",
            target_id=updated["conversation_id"],
            metadata=changes,
        )
        return updated

    @app.get("/v1/conversations/{conversation_id}/tasks")
    async def conversation_tasks(
        conversation_id: str, principal: Annotated[Principal, Depends(authenticate)],
        offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
        limit: Annotated[int, Query(ge=1, le=20)] = 20,
    ):
        return coordinator.cards(conversation_id, principal.tenant_id, principal.project_id,
                                 offset=offset, limit=limit)

    @app.get("/v1/conversations/{conversation_id}/activity")
    async def conversation_activity(conversation_id: str,
        principal: Annotated[Principal, Depends(authenticate)]):
        root = conversations.conversation_root(conversation_id, principal.tenant_id, principal.project_id)
        rows = repository._connection.execute(conversations._tree(conversation=True) +
            "SELECT root_id,investigation_id FROM ranked ORDER BY created_at DESC,investigation_id DESC LIMIT 21",
            (principal.tenant_id,principal.project_id,root,root,principal.tenant_id,principal.project_id)).fetchall()
        items = []
        more = len(rows)>20
        for row in rows[:20]:
            view = scoped(row["investigation_id"], principal)
            title = repository.get_investigation(row["root_id"]).request.objective.description[:240]
            automatic = bool(view.request.constraints.get("observation_origin"))
            items.append({"id":"goal/"+view.investigation_id,"kind":"event" if automatic else "user",
                "task_id":row["root_id"],"investigation_id":view.investigation_id,"title":title,
                "text":view.request.objective.description[:4000],"at":view.created_at})
            returned = {value["source_command_id"] for value in repository._connection.execute(
                "SELECT source_command_id FROM task_input_returns WHERE source_investigation_id=?",(view.investigation_id,)).fetchall()}
            inputs = repository.list_user_inputs(view.investigation_id)
            more = more or len(inputs)>10
            for entry in inputs[-10:]:
                if entry["command_id"] in returned:
                    continue
                items.append({"id":"input/"+entry["command_id"],"kind":"user","task_id":row["root_id"],
                    "investigation_id":view.investigation_id,"title":title,"text":entry["payload"]["content"][:4000],"at":entry["created_at"]})
            try:
                report,_,digest = repository.get_latest_report(view.investigation_id)
                items.append({"id":"report/"+view.investigation_id+"/"+str(report.revision),"kind":"report",
                    "task_id":row["root_id"],"investigation_id":view.investigation_id,"title":title,
                    "text":report.summary[:2000],"truncated":len(report.summary)>2000,"at":report.generated_at,"report_revision":report.revision,"sha256":digest})
            except NotFoundError:
                pass
        def timestamp(item):
            value = item["at"]
            return value.isoformat() if hasattr(value,"isoformat") else str(value)
        items.sort(key=lambda item:(timestamp(item),item["id"]))
        decisions = repository._connection.execute("""SELECT request_key,response_json,created_at FROM workbench_message_receipts
            WHERE root_id=? AND principal_id=? ORDER BY created_at DESC LIMIT 20""",(root,principal.principal_id)).fetchall()
        routing = []
        for row in decisions:
            receipt = json.loads(row["response_json"])
            decision = receipt.get("route_decision",{})
            if isinstance(receipt.get("original_message"),dict) and "content" in receipt["original_message"]:
                routing.append({"id":row["request_key"],"kind":decision.get("kind"),"basis":decision.get("basis"),
                    "task_id":decision.get("task_id"),"at":row["created_at"],"text":receipt["original_message"]["content"][:240]})
        return {"conversation_id":root,"items":items[-100:],"has_older":more or len(items)>100,"routing":routing}

    @app.get("/v1/conversations/{conversation_id}/overview")
    async def conversation_overview(conversation_id: str,
        principal: Annotated[Principal, Depends(authenticate)],
        attention_offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
        result_offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
        limit: Annotated[int, Query(ge=1, le=10)] = 8):
        return coordinator.overview(conversation_id, principal.tenant_id, principal.project_id,
            attention_offset=attention_offset, result_offset=result_offset, limit=limit)

    @app.get("/v1/conversations/{conversation_id}/observations")
    async def conversation_observations(conversation_id: str,
        principal: Annotated[Principal, Depends(authenticate)],
        offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0):
        from game_test_agent.events.coordinator import excerpt
        root = conversations.conversation_root(conversation_id, principal.tenant_id, principal.project_id)
        rows = repository._connection.execute("""SELECT event_key,state,payload_json FROM coordination_observations
            WHERE conversation_id=? ORDER BY created_at,event_key LIMIT 21 OFFSET ?""", (root, offset)).fetchall()
        return {"items": [{"event_key": row["event_key"], "state": row["state"],
            "summary_excerpt": excerpt(json.loads(row["payload_json"])["summary"], 240),
            "untrusted": True} for row in rows[:20]], "next_offset": offset + 20 if len(rows) > 20 else None}

    @app.post("/v1/conversations/{conversation_id}/observations/{event_key}/retry")
    async def retry_observation(conversation_id: str, event_key: str,
        principal: Annotated[Principal, Depends(authenticate)]):
        if "events:write" not in principal.scopes:
            raise HTTPException(status_code=403, detail="events:write is required")
        root = conversations.conversation_root(conversation_id, principal.tenant_id, principal.project_id)
        with repository.transaction() as connection:
            repository._lock_investigation_row(connection, root)
            if conversations.get(root, principal.tenant_id, principal.project_id)["archived"]:
                raise ConflictError("restore conversation before retrying observations")
            row = connection.execute("SELECT * FROM coordination_observations WHERE conversation_id=? AND event_key=?", (root, event_key)).fetchone()
            if row is None:
                raise NotFoundError("observation not found in this conversation")
            if payload_hash(json.loads(row["payload_json"])) != row["payload_hash"]:
                raise ConflictError("stored observation changed")
            if row["state"] == "pending":
                return {"state": "pending", "duplicate": True}
            if row["state"] != "unresolved":
                raise ConflictError("only unresolved observations may be retried")
            connection.execute("UPDATE coordination_observations SET state='pending',batch_id=NULL WHERE event_key=?", (event_key,))
            repository._insert_event(connection, root, "conversation.observation_retry", {"event_key": event_key, "prior_batch_id": row["batch_id"], "principal_id": principal.principal_id})
        return {"state": "pending", "duplicate": False}

    @app.get("/v1/investigations/{investigation_id}/operations")
    async def native_operation_progress(investigation_id: str,
        principal: Annotated[Principal, Depends(authenticate)]):
        conversations.conversation_root(investigation_id, principal.tenant_id, principal.project_id)
        rows = repository._connection.execute("""SELECT job_id,provider_id,tool_name,state,polls,created_at,updated_at
            FROM native_tool_jobs WHERE investigation_id=? ORDER BY updated_at DESC,job_id LIMIT 17""", (investigation_id,)).fetchall()
        return {"items": [dict(row) for row in rows[:16]], "has_more": len(rows)>16,
                "pending_cleanup": repository.pending_cleanup_resources(investigation_id)}

    @app.post("/v1/conversations/{conversation_id}/synthesis")
    async def conversation_synthesis(conversation_id: str, request: SynthesisRequest,
        principal: Annotated[Principal, Depends(authenticate)]):
        return await synthesis.synthesize(conversation_id, request, principal.tenant_id, principal.project_id)

    @app.get("/v1/conversations/{conversation_id}/observations/{event_key}")
    async def observation_detail(conversation_id: str, event_key: str,
        principal: Annotated[Principal, Depends(authenticate)]):
        root = conversations.conversation_root(conversation_id, principal.tenant_id, principal.project_id)
        row = repository._connection.execute("SELECT payload_json,payload_hash,state FROM coordination_observations WHERE conversation_id=? AND event_key=?",
            (root,event_key)).fetchone()
        if row is None:
            raise NotFoundError("observation not found in this conversation")
        payload = json.loads(row["payload_json"])
        if payload_hash(payload) != row["payload_hash"]:
            raise ConflictError("stored observation changed")
        return {"event":payload,"sha256":row["payload_hash"],"state":row["state"],"untrusted":True}

    @app.post("/v1/conversations/{conversation_id}/tasks", status_code=202)
    async def create_conversation_task(
        conversation_id: str, task: InvestigationRequest,
        principal: Annotated[Principal, Depends(authenticate)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=256)],
    ):
        if (task.tenant_id, task.project_id) != (principal.tenant_id, principal.project_id):
            raise HTTPException(status_code=403, detail="request scope does not match principal")
        summary = conversations.get(conversation_id, principal.tenant_id, principal.project_id)
        root = summary["conversation_id"]
        key = "conversation-task-" + hashlib.sha256(f"{root}:{idempotency_key}".encode()).hexdigest()
        task = task.model_copy(update={"budget": settings.default_budget().model_copy(
            update=task.budget.model_dump(exclude_unset=True))})
        try:
            view, created = repository.create_investigation(
                task, key, principal_id=principal.principal_id, scopes=set(principal.scopes),
                conversation_root_id=root)
        except PermissionError as error:
            raise HTTPException(status_code=403, detail="historical reference scope or permission mismatch") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        container.workflows.start(view.investigation_id)
        repository.record_audit(
            principal_id=principal.principal_id, tenant_id=principal.tenant_id,
            project_id=principal.project_id, action="conversation.task.create",
            target_id=view.investigation_id, metadata={"created": created, "conversation_id": root},
        )
        return {"conversation_id": root, "task_id": view.investigation_id,
                "investigation": view, "duplicate": not created}

    @app.get("/v1/conversations/{conversation_id}/workspace")
    async def conversation_workspace(
        conversation_id: str,
        principal: Annotated[Principal, Depends(authenticate)],
        before: str | None = None,
        limit: Annotated[int, Query(ge=1, le=20)] = 10,
        task_id: Annotated[str | None, Query(max_length=96)] = None,
    ):
        summary = conversations.get(conversation_id, principal.tenant_id, principal.project_id)
        task_root, head = coordinator.route(
            summary["conversation_id"], task_id or summary["conversation_id"],
            principal.tenant_id, principal.project_id,
        )
        views = conversations.investigations(
            task_root,
            principal.tenant_id,
            principal.project_id,
            before=before,
            limit=limit + 1,
        )
        more = len(views) > limit
        selected = views[-limit:]
        return {
            "conversation": summary,
            "task_id": task_root,
            "task_head_id": head.investigation_id,
            "investigations": [projection(v.investigation_id, principal) for v in selected],
            "older_before": selected[0].investigation_id if more else None,
        }

    @app.get("/v1/conversations/{conversation_id}/input-returns")
    async def returned_inputs(conversation_id: str, principal: Annotated[Principal, Depends(authenticate)],
        offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0, limit: Annotated[int, Query(ge=1, le=10)] = 10):
        return input_returns.page(conversation_id, principal.tenant_id, principal.project_id, offset=offset, limit=limit)

    @app.get("/v1/conversations/{conversation_id}/input-returns/{return_id}")
    async def returned_input_detail(conversation_id: str, return_id: str, principal: Annotated[Principal, Depends(authenticate)]):
        return input_returns.detail(conversation_id, return_id, principal.tenant_id, principal.project_id)

    @app.post("/v1/conversations/{conversation_id}/input-returns/{return_id}/resolve")
    async def resolve_returned_input(conversation_id: str, return_id: str, decision: InputReturnDecision,
        principal: Annotated[Principal, Depends(authenticate)]):
        return input_returns.decide(conversation_id, return_id, decision, principal.tenant_id,
            principal.project_id, principal.principal_id)

    @app.post("/v1/conversations/{conversation_id}/messages/route")
    async def route_conversation_message(conversation_id: str, message: ConversationRouteRequest,
        principal: Annotated[Principal, Depends(authenticate)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=256)]):
        summary = conversations.get(conversation_id, principal.tenant_id, principal.project_id)
        root = summary["conversation_id"]
        token = "route-" + hashlib.sha256(f"{root}:{principal.principal_id}:{idempotency_key}".encode()).hexdigest()
        original = message.model_dump(mode="json")
        if "bound_investigation_id" not in message.model_fields_set:
            original.pop("bound_investigation_id")
        digest = payload_hash(original)
        try:
            return await routing_service.route(root, original,
                principal.tenant_id, principal.project_id, principal.principal_id, token, digest)
        except RouteEvaluationBusy as error:
            # 503 preserves the workbench's pending mutation and idempotency key.
            raise HTTPException(status_code=503, detail=str(error), headers={"Retry-After": "1"}) from error

    @app.post("/v1/conversations/{conversation_id}/messages", status_code=202)
    async def conversation_message(
        conversation_id: str,
        message: ConversationMessage,
        principal: Annotated[Principal, Depends(authenticate)],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=256)
        ],
    ):
        summary = conversations.get(conversation_id, principal.tenant_id, principal.project_id)
        token = hashlib.sha256(
            f"{summary['conversation_id']}:{principal.principal_id}:{idempotency_key}".encode()
        ).hexdigest()
        canonical_message = message.model_dump(mode="json")
        # An omitted build inherits the old build; an explicit null clears it.
        if "build_ref" not in message.model_fields_set:
            canonical_message.pop("build_ref")
        digest = payload_hash(canonical_message)
        receipt = conversations.receipt(
            summary["conversation_id"], principal.principal_id, token, digest
        )
        if receipt is not None:
            return {**receipt, "duplicate": True}
        if summary["archived"]:
            raise ConflictError("restore the conversation before sending messages")
        parent = scoped(message.expected_investigation_id, principal)
        task_root, task_head = coordinator.route(
            summary["conversation_id"], parent.investigation_id,
            principal.tenant_id, principal.project_id,
        )
        input_replay = repository._connection.execute(
            "SELECT investigation_id,payload_json FROM workflow_command_outbox WHERE command_id=?",
            ("conversation-input-" + token,),
        ).fetchone()
        if input_replay:
            original = json.loads(input_replay["payload_json"])
            if (
                input_replay["investigation_id"] != parent.investigation_id
                or original["content"] != message.content
                or "build_ref" in message.model_fields_set
                or any(
                    value is not None
                    for value in (message.budget, message.constraints, message.acceptance_criteria)
                )
            ):
                raise ConflictError("Idempotency-Key was already used for a different message")
            result = {
                "conversation_id": summary["conversation_id"],
                "investigation": parent,
                "duplicate": True,
            }
            conversations.save_receipt(
                summary["conversation_id"], principal.principal_id, token, digest, result
            )
            return result
        # Resolve retries against their original investigation, before checking the current head.
        replay_key = f"{principal.principal_id}:reopen:{parent.investigation_id}:{idempotency_key}"
        replay = repository._connection.execute(
            "SELECT investigation_id FROM investigations WHERE tenant_id=? AND project_id=? AND idempotency_key=?",
            (principal.tenant_id, principal.project_id, replay_key),
        ).fetchone()
        current_id = task_head.investigation_id
        if not replay and parent.investigation_id != current_id:
            raise ConflictError("conversation has a newer investigation; refresh before sending")
        if parent.status == InvestigationStatus.CANCELING:
            raise ConflictError("wait for cancellation to finish before sending a followup")
        if parent.status in {
            InvestigationStatus.COMPLETED,
            InvestigationStatus.CANCELED,
            InvestigationStatus.FAILED,
        }:
            reference = {
                "investigation_id": parent.investigation_id,
                "usage": "historical_reference",
                "objective": parent.request.objective.description,
                "root_objective": scoped(
                    task_root, principal
                ).request.objective.description,
            }
            try:
                old_report, _, old_hash = repository.get_latest_report(parent.investigation_id)
                reference.update(
                    {
                        "report_sha256": old_hash,
                        "report_revision": old_report.revision,
                        "summary": old_report.summary[:2000],
                        "resolution": old_report.resolution.value,
                    }
                )
            except NotFoundError:
                pass
            reopen = ReopenRequest(
                reason=message.content[:2000],
                budget=message.budget or parent.request.budget,
                **(
                    {"build_ref": message.build_ref}
                    if "build_ref" in message.model_fields_set
                    else {}
                ),
                objective=TestObjective(
                    objective_id=parent.request.objective.objective_id,
                    description=message.content,
                    acceptance_criteria=message.acceptance_criteria
                    if message.acceptance_criteria is not None
                    else parent.request.objective.acceptance_criteria,
                ),
                constraints={**(message.constraints or {}), "followup_reference": reference},
            )
            try:
                view, created = repository.reopen_investigation(
                    parent.investigation_id, reopen, idempotency_key,
                    principal_id=principal.principal_id, scopes=set(principal.scopes))
            except PermissionError as error:
                raise HTTPException(status_code=403, detail="historical reference scope or permission mismatch") from error
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            container.workflows.start(view.investigation_id)
            action = "conversation.followup"
        else:
            if "build_ref" in message.model_fields_set or any(
                value is not None
                for value in (message.budget, message.constraints, message.acceptance_criteria)
            ):
                raise ConflictError("active investigation settings cannot be changed by a message")
            command = InputCommand(
                command_id="conversation-input-" + token,
                expected_revision=parent.revision,
                content=message.content,
            )
            created = repository.enqueue_command(
                parent.investigation_id, command.command_id, "input", command
            )
            if settings.workflow_backend == "local":
                repository.mark_command_sent(command.command_id)
                if created:
                    container.workflows.start(parent.investigation_id)
            view = parent
            action = "conversation.input"
        repository.record_audit(
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            project_id=principal.project_id,
            action=action,
            target_id=view.investigation_id,
            metadata={"created": created},
        )
        result = {
            "conversation_id": summary["conversation_id"],
            "investigation": view,
            "duplicate": not created,
        }
        conversations.save_receipt(
            summary["conversation_id"], principal.principal_id, token, digest, result
        )
        return result

    @app.get("/v1/conversations/{conversation_id}/stream")
    async def conversation_stream(
        conversation_id: str,
        request: Request,
        principal: Annotated[Principal, Depends(authenticate)],
        after: str = "",
        seconds: Annotated[int, Query(ge=1, le=30)] = 25,
    ):
        root = conversations.conversation_root(conversation_id, principal.tenant_id, principal.project_id)

        async def events():
            previous = after
            started = asyncio.get_running_loop().time()
            while asyncio.get_running_loop().time() - started < seconds:
                if await request.is_disconnected():
                    return
                summary = conversations.get(root, principal.tenant_id, principal.project_id)
                view = summary["latest_investigation"]
                cursor = summary["event_cursor"]
                version = coordinator.version(summary)
                if version != previous:
                    previous = version
                    payload = {
                        "conversation_id": root,
                        "investigation_id": view.investigation_id,
                        "status": view.status,
                        "status_scope": "anchor_investigation",
                        "task_summary": summary["task_summary"],
                        "cursor": cursor,
                        "version": version,
                    }
                    yield f"id: {version}\nevent: update\ndata: {json.dumps(jsonable_encoder(payload))}\n\n"
                else:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/investigations/{investigation_id}/evidence/{evidence_id}/download")
    async def download_evidence(
        investigation_id: str,
        evidence_id: str,
        principal: Annotated[Principal, Depends(authenticate)],
    ):
        scoped(investigation_id, principal)
        try:
            evidence, content = container.context_access.read_artifact(
                investigation_id,
                evidence_id,
                ContextAccessScope(tenant_id=principal.tenant_id, project_id=principal.project_id),
            )
        except PermissionError as error:
            raise HTTPException(status_code=403, detail="evidence scope mismatch") from error
        except LookupError as error:
            raise HTTPException(status_code=404, detail="evidence not found") from error
        except (ValueError, IOError) as error:
            raise HTTPException(
                status_code=409, detail="evidence unavailable or integrity check failed"
            ) from error
        return Response(
            content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(evidence_id, safe='')}",
                "X-Content-Type-Options": "nosniff",
                "X-Artifact-SHA256": evidence.sha256,
                "Cache-Control": "no-store",
            },
        )
