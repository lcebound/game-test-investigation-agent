"""Versioned offline review export; contains no evaluation labels or model credentials."""
from game_test_agent.observability.redaction import redact
from game_test_agent.storage import NotFoundError


def export_trajectory(repository, view):
    events, cursor = [], 0
    allowed = {"trace.span", "investigation.runtime_selected", "mcp.native.plan_updated",
               "mcp.native.hypotheses_updated", "mcp.native.candidate_selection",
               "mcp.native.model_completed", "mcp.native.model_error", "mcp.native.no_progress",
               "mcp.native.assessment", "analysis.diagnostic_completed", "candidate.selection", "candidate.outcome"}
    while True:
        page = repository.list_events(view.investigation_id, after=cursor, limit=500)
        events.extend(redact(event) for event in page if event["event_type"] in allowed)
        if len(page) < 500:
            break
        cursor = page[-1]["cursor"]
    try:
        report = repository.get_latest_report(view.investigation_id)[0].model_dump(mode="json")
    except NotFoundError:
        report = None
    return {"schema_version": "trajectory-v1", "investigation_id": view.investigation_id,
            "status": view.status, "objective": view.request.objective.model_dump(mode="json"),
            "budget": view.request.budget.model_dump(mode="json"),
            "budget_usage": repository.budget_usage(view.investigation_id),
            "evidence": [{"evidence_id": e.evidence_id, "sha256": e.sha256,
                          "derived_from": e.derived_from, "execution_source": e.execution_source,
                          "quality": e.quality} for e in repository.list_current_evidence(view.investigation_id)],
            "events": events, "report": redact(report) if report else None,
            "evaluation": {"performed": False, "labels": None}}
