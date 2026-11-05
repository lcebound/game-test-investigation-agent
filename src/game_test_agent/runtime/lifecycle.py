"""Shared failure finalization for Local and Temporal investigation owners."""
from game_test_agent.contracts.models import InvestigationStatus, Resolution
from game_test_agent.reports import ReportBuilder


async def finalize_failure(repository, reports, investigation_id, error, *, code_execution=None):
    view = repository.get_investigation(investigation_id)
    if view.status in {"completed", "failed", "canceled", "canceling"}:
        return view.status
    for item in repository.list_actions(investigation_id):
        if item["state"] not in {"succeeded", "failed", "canceled"}:
            repository.record_cleanup_required(investigation_id, item["action"]["action_id"])
    reason = "workflow_error:" + type(error).__name__
    report = ReportBuilder(repository).build(investigation_id, reason, status="failed",
        resolution=Resolution.SYSTEM_ERROR,
        summary="调查执行遇到系统错误，已保存现有证据；未确认结束的外部运行需要检查。")
    reports.publish(report)
    repository.transition(investigation_id, InvestigationStatus.FAILED,
                          resolution=Resolution.SYSTEM_ERROR, stop_reason=reason)
    if code_execution is not None:
        await code_execution.cancel_investigation(investigation_id)
    return "failed"
