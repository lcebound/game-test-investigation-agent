"""Read-only conversation totals over Worker heads and unresolved resource ledgers."""

COORDINATION_CTES = """,
resource_counts AS (
  SELECT ranked.root_id,
    SUM(CASE WHEN c.state='pending' THEN 1 ELSE 0 END) AS cleanup_count,
    MIN(CASE WHEN c.state='pending' THEN ranked.investigation_id ELSE NULL END) AS cleanup_investigation_id,
    MAX(c.updated_at) AS cleanup_updated_at
  FROM ranked JOIN cleanup_operations c ON c.investigation_id=ranked.investigation_id
  GROUP BY ranked.root_id
), input_return_counts AS (
  SELECT ranked.root_id,COUNT(*) AS pending_return_count
  FROM ranked JOIN task_input_returns returned ON returned.source_investigation_id=ranked.investigation_id
  WHERE returned.state='pending' GROUP BY ranked.root_id
), permission_counts AS (
  SELECT ranked.root_id,COUNT(*) AS approval_count
  FROM ranked JOIN code_permission_requests p ON p.investigation_id=ranked.investigation_id
  WHERE ranked.position=1 AND ranked.status NOT IN ('completed','failed','canceled','canceling')
    AND p.state='pending' AND p.expires_at>?
    AND NOT EXISTS (SELECT 1 FROM workflow_command_outbox input_command
      WHERE input_command.investigation_id=ranked.investigation_id AND input_command.kind='input'
        AND input_command.created_at>p.created_at)
    AND NOT EXISTS (SELECT 1 FROM task_input_returns returned
      WHERE returned.source_investigation_id=ranked.investigation_id AND returned.created_at>p.created_at)
  GROUP BY ranked.root_id
), task_events AS (
  SELECT root_id,
    MAX((SELECT MAX(e.cursor) FROM investigation_events e
         WHERE e.investigation_id=ranked.investigation_id)) AS event_cursor,
    MAX((SELECT e.created_at FROM investigation_events e
         WHERE e.investigation_id=ranked.investigation_id ORDER BY e.cursor DESC LIMIT 1)) AS event_at
  FROM ranked GROUP BY root_id
), task_state AS (
  SELECT ranked.*, COALESCE(link.conversation_id,ranked.root_id) AS conversation_id,
    COALESCE(resources.cleanup_count,0) AS cleanup_count, resources.cleanup_investigation_id,
    COALESCE(permissions.approval_count,0) AS approval_count,
    COALESCE(returned.pending_return_count,0) AS pending_return_count,
    COALESCE(events.event_cursor,0) AS event_cursor,
    CASE WHEN COALESCE(resources.cleanup_updated_at,'')>COALESCE(events.event_at,ranked.conversation_updated_at)
      AND resources.cleanup_updated_at>ranked.conversation_updated_at THEN resources.cleanup_updated_at
      WHEN COALESCE(events.event_at,'')>ranked.conversation_updated_at THEN events.event_at
      ELSE ranked.conversation_updated_at END AS activity_at,
    CASE WHEN report.revision IS NOT NULL AND ranked.status IN ('completed','failed','canceled')
      THEN 1 ELSE 0 END AS has_report
  FROM ranked
  LEFT JOIN conversation_task_links link ON link.task_root_id=ranked.root_id
    AND link.tenant_id=ranked.tenant_id AND link.project_id=ranked.project_id
  LEFT JOIN resource_counts resources ON resources.root_id=ranked.root_id
  LEFT JOIN permission_counts permissions ON permissions.root_id=ranked.root_id
  LEFT JOIN input_return_counts returned ON returned.root_id=ranked.root_id
  LEFT JOIN task_events events ON events.root_id=ranked.root_id
  LEFT JOIN reports report ON report.investigation_id=ranked.investigation_id
    AND report.revision=(SELECT MAX(r.revision) FROM reports r WHERE r.investigation_id=ranked.investigation_id)
  WHERE ranked.position=1
), task_attention AS (
  SELECT task_state.*,
    CASE WHEN status='waiting_input' OR approval_count>0 OR cleanup_count>0 OR pending_return_count>0
      OR status='failed' OR resolution='goal_failed'
      OR status IN ('completed','failed','canceled') AND has_report=0 THEN 1 ELSE 0 END AS needs_attention
  FROM task_state
), conversation_totals AS (
  SELECT conversation_id,COUNT(*) AS task_count,SUM(investigation_count) AS total_investigation_count,
    SUM(CASE WHEN status NOT IN ('completed','failed','canceled','waiting_input') THEN 1 ELSE 0 END) AS active_tasks,
    SUM(CASE WHEN status='waiting_input' THEN 1 ELSE 0 END) AS waiting_input_tasks,
    SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed_tasks,
    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_tasks,
    SUM(CASE WHEN status='canceled' THEN 1 ELSE 0 END) AS canceled_tasks,
    SUM(CASE WHEN resolution='goal_failed' THEN 1 ELSE 0 END) AS goal_failed_tasks,
    SUM(CASE WHEN status='completed' AND resolution='inconclusive' THEN 1 ELSE 0 END) AS inconclusive_tasks,
    SUM(CASE WHEN status IN ('completed','failed','canceled') AND has_report=0 THEN 1 ELSE 0 END) AS missing_report_tasks,
    SUM(CASE WHEN approval_count>0 THEN 1 ELSE 0 END) AS approval_tasks,
    SUM(approval_count) AS approval_requests,SUM(cleanup_count) AS pending_cleanup_resources,
    SUM(pending_return_count) AS pending_input_returns,
    SUM(CASE WHEN cleanup_count>0 THEN 1 ELSE 0 END) AS cleanup_tasks,
    SUM(needs_attention) AS attention_tasks,SUM(has_report) AS result_tasks,
    MAX(activity_at) AS activity_at, MAX(event_cursor) AS event_cursor
  FROM task_attention GROUP BY conversation_id
) """


def task_summary(row):
    fields = ("task_count", "active_tasks", "waiting_input_tasks", "completed_tasks", "failed_tasks",
              "canceled_tasks", "goal_failed_tasks", "inconclusive_tasks", "approval_tasks",
              "approval_requests", "pending_cleanup_resources", "pending_input_returns", "cleanup_tasks", "attention_tasks", "result_tasks", "missing_report_tasks")
    result = {key: int(row[key]) for key in fields}
    result["investigation_count"] = int(row["total_investigation_count"])
    result["all_terminal"] = result["active_tasks"] + result["waiting_input_tasks"] == 0
    result["state"] = "needs_attention" if result["attention_tasks"] else "running" if not result["all_terminal"] else "finished"
    return result
