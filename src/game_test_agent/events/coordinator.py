"""Rule routing and bounded conversation cards; never assemble Worker memory here."""

from game_test_agent.storage import NotFoundError
from game_test_agent.storage.workbench import WorkbenchStore
from game_test_agent.storage.repository import canonical_json, payload_hash


def excerpt(value: str, max_bytes: int) -> str:
    return value.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


class ConversationCoordinator:
    def __init__(self, repository):
        self.repository = repository
        self.store = WorkbenchStore(repository)

    def route(self, conversation_id, investigation_id, tenant_id, project_id):
        # A selected task provides an unambiguous routing identity. Reasoning and
        # prioritization within the investigation remain the existing Worker's job.
        return self.store.task(conversation_id, investigation_id, tenant_id, project_id)

    def cards(self, conversation_id, tenant_id, project_id, *, offset=0, limit=20):
        if not 1 <= limit <= 20:
            raise ValueError("at most 20 task cards can be inlined")
        rows = self.store.task_heads(conversation_id, tenant_id, project_id,
                                     offset=offset, limit=limit + 1)
        cards = [self._card(row) for row in rows[:limit]]
        return {
            "items": cards,
            "next_offset": offset + limit if len(rows) > limit else None,
            "context_policy": "task_cards_only_details_require_scoped_reads",
        }

    def _card(self, row, *, brief=False):
        view = self.repository._view(row)
        card = {
                "task_id": row["root_id"],
                "investigation_id": view.investigation_id,
                "objective_excerpt": excerpt(view.request.objective.description, 180 if brief else 600),
                "objective_truncated": len(view.request.objective.description.encode("utf-8")) > (180 if brief else 600),
                "status": view.status.value,
                "resolution": view.resolution.value if view.resolution else None,
                "investigation_count": row["investigation_count"],
                "updated_at": row["activity_at"],
                "event_cursor": row["event_cursor"],
                "pending_approval_count": int(row["approval_count"]),
                "pending_cleanup_count": int(row["cleanup_count"]),
                "pending_input_return_count": int(row["pending_return_count"]),
                "cleanup_investigation_id": row["cleanup_investigation_id"],
                "attention": [],
                "report": None,
            }
        for present, reason in ((view.status == "waiting_input", "waiting_input"),
                (row["approval_count"] > 0, "permission"), (row["cleanup_count"] > 0, "cleanup"),
                (row["pending_return_count"] > 0, "input_return"),
                (view.status == "failed", "execution_failed"), (view.resolution == "goal_failed", "goal_failed"),
                (view.status in {"completed", "failed", "canceled"} and not row["has_report"], "missing_report")):
            if present:
                card["attention"].append(reason)
        try:
            report, _, digest = self.repository.get_latest_report(view.investigation_id)
            card["report"] = {
                    "summary_excerpt": excerpt(report.summary, 240 if brief else 800),
                    "summary_truncated": len(report.summary.encode("utf-8")) > (240 if brief else 800),
                    "resolution": report.resolution.value,
                    "revision": report.revision,
                    "sha256": digest,
                    "url": f"/v1/investigations/{view.investigation_id}/reports/{report.revision}",
                    "usage": "worker_report_not_independent_verification",
            }
        except NotFoundError:
            pass
        return card

    @staticmethod
    def version(summary):
        return payload_hash({key: summary[key] for key in
                             ("event_cursor", "revision", "updated_at", "task_summary")})

    def overview(self, conversation_id, tenant_id, project_id, *, attention_offset=0, result_offset=0, limit=8):
        if not 1 <= limit <= 10:
            raise ValueError("overview accepts at most 10 cards per category")
        summary = self.store.get(conversation_id, tenant_id, project_id)
        root = summary["conversation_id"]
        result = {"conversation_id": root, "task_summary": summary["task_summary"],
                  "updated_at": summary["updated_at"], "version": self.version(summary),
                  "attention": [], "results": [], "next_attention_offset": None, "next_result_offset": None,
                  "context_policy": "task_cards_only_worker_contexts_stay_isolated"}
        for category, offset, key, next_key in (
                ("attention", attention_offset, "attention", "next_attention_offset"),
                ("results", result_offset, "results", "next_result_offset")):
            rows = self.store.task_heads(root, tenant_id, project_id, offset=offset, limit=limit + 1, category=category)
            for row in rows[:limit]:
                card = self._card(row, brief=True)
                if len(canonical_json([*result[key], card]).encode()) > 6144:
                    break
                result[key].append(card)
            if len(rows) > len(result[key]):
                result[next_key] = offset + len(result[key])
        if len(canonical_json(result).encode()) > 16384:
            raise ValueError("overview metadata exceeds its byte bound")
        return result
