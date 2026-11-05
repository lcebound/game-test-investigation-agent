"""Scoped conversation projections over investigation lineage, with separate UI metadata."""

from datetime import datetime, timezone
from typing import Any
from game_test_agent.storage.conversation_projection import COORDINATION_CTES, task_summary

import json
import sqlite3

from game_test_agent.storage.repository import (
    ConflictError,
    NotFoundError,
    Repository,
    canonical_json,
)


class WorkbenchStore:
    def __init__(self, repository: Repository):
        self.repository = repository

    def receipt(self, root: str, principal_id: str, key: str, digest: str):
        row = self.repository._connection.execute(
            "SELECT payload_hash,response_json FROM workbench_message_receipts "
            "WHERE root_id=? AND principal_id=? AND request_key=?",
            (root, principal_id, key),
        ).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != digest:
            raise ConflictError("Idempotency-Key was already used for a different message")
        return json.loads(row["response_json"])

    def save_receipt(self, root: str, principal_id: str, key: str, digest: str, response):
        with self.repository.transaction() as connection:
            self.save_receipt_in_transaction(connection, root, principal_id, key, digest, response)

    def save_receipt_in_transaction(self, connection, root, principal_id, key, digest, response):
        if self.receipt(root, principal_id, key, digest) is not None:
            return
        connection.execute("INSERT INTO workbench_message_receipts VALUES (?,?,?,?,?,?)",
            (root, principal_id, key, digest, canonical_json(response), datetime.now(timezone.utc).isoformat()))

    def root(self, investigation_id: str, tenant_id: str, project_id: str) -> str:
        seen = set()
        current = investigation_id
        for _ in range(100):
            if current in seen:
                raise ConflictError("investigation lineage contains a cycle")
            seen.add(current)
            view = self.repository.get_investigation(current)
            if (view.request.tenant_id, view.request.project_id) != (tenant_id, project_id):
                raise NotFoundError("conversation not found in this project")
            parent = self.repository._connection.execute(
                "SELECT parent_investigation_id FROM investigation_relations "
                "WHERE child_investigation_id=? AND relation_type='supersedes'",
                (current,),
            ).fetchone()
            if not parent:
                return current
            current = parent[0]
        raise ConflictError("conversation exceeds the supported lineage depth")

    def conversation_root(self, investigation_id: str, tenant_id: str, project_id: str) -> str:
        task_root = self.root(investigation_id, tenant_id, project_id)
        link = self.repository._connection.execute(
            "SELECT conversation_id FROM conversation_task_links "
            "WHERE task_root_id=? AND tenant_id=? AND project_id=?",
            (task_root, tenant_id, project_id),
        ).fetchone()
        if not link:
            return task_root
        root = self.root(link["conversation_id"], tenant_id, project_id)
        if root != link["conversation_id"]:
            raise ConflictError("conversation anchor cannot be a continuation")
        return root

    def task(self, conversation_id: str, investigation_id: str, tenant_id: str, project_id: str):
        root = self.conversation_root(conversation_id, tenant_id, project_id)
        task_root = self.root(investigation_id, tenant_id, project_id)
        if self.conversation_root(task_root, tenant_id, project_id) != root:
            raise NotFoundError("task not found in this conversation")
        views = self.investigations(task_root, tenant_id, project_id, limit=1)
        if not views:
            raise NotFoundError("task not found")
        return task_root, views[0]

    def task_heads(self, conversation_id, tenant_id, project_id, *, offset=0, limit=20, category=None):
        """Bounded metadata projection. Worker contexts and tool payloads stay outside it."""
        if not 1 <= limit <= 21 or not 0 <= offset <= 1_000_000:
            raise ValueError("task page is outside the supported bounds")
        root = self.conversation_root(conversation_id, tenant_id, project_id)
        if category not in {None, "attention", "results"}:
            raise ValueError("unknown conversation category")
        predicate = " AND needs_attention=1" if category == "attention" else " AND has_report=1" if category == "results" else ""
        order = "activity_at DESC,root_id" if category == "results" else "needs_attention DESC,activity_at DESC,root_id" if category else "root_id"
        return self.repository._connection.execute(self._coordination(True)
            + "SELECT * FROM task_attention WHERE conversation_id=?" + predicate
            + " ORDER BY " + order + " LIMIT ? OFFSET ?",
            (tenant_id, project_id, root, root, tenant_id, project_id,
             datetime.now(timezone.utc).isoformat(), root, limit, offset)).fetchall()

    def task_version(self, conversation_id, tenant_id, project_id):
        root = self.conversation_root(conversation_id, tenant_id, project_id)
        row = self.repository._connection.execute(
            self._tree(conversation=True) + """SELECT COUNT(DISTINCT ranked.investigation_id) AS investigations,
              COUNT(DISTINCT ranked.root_id) AS tasks,
              COALESCE(MAX((SELECT MAX(e.cursor) FROM investigation_events e
                WHERE e.investigation_id=ranked.investigation_id)),0) AS cursor
              FROM ranked""",
            (tenant_id, project_id, root, root, tenant_id, project_id),
        ).fetchone()
        return f"{row['tasks']}:{row['investigations']}:{row['cursor']}"

    @staticmethod
    def _tree(seed: bool = False, conversation: bool = False) -> str:
        sql = """WITH RECURSIVE tree(root_id, investigation_id, depth) AS (
          SELECT i.investigation_id, i.investigation_id, 0 FROM investigations i
          WHERE i.tenant_id=? AND i.project_id=? AND NOT EXISTS (
            SELECT 1 FROM investigation_relations r
            WHERE r.child_investigation_id=i.investigation_id AND r.relation_type='supersedes')
          UNION ALL
          SELECT tree.root_id, child.investigation_id, tree.depth+1
          FROM tree JOIN investigation_relations r ON r.parent_investigation_id=tree.investigation_id
          JOIN investigations child ON child.investigation_id=r.child_investigation_id
          WHERE r.relation_type='supersedes' AND child.tenant_id=? AND child.project_id=?
            AND tree.depth<99
        ), ranked AS (
          SELECT tree.root_id, i.*, ROW_NUMBER() OVER (
            PARTITION BY tree.root_id ORDER BY i.created_at DESC, i.investigation_id DESC) AS position,
            COUNT(*) OVER (PARTITION BY tree.root_id) AS investigation_count,
            MAX(i.updated_at) OVER (PARTITION BY tree.root_id) AS conversation_updated_at
          FROM tree JOIN investigations i ON i.investigation_id=tree.investigation_id
        ) """
        if conversation:
            return sql.replace("AND NOT EXISTS", """AND (i.investigation_id=? OR EXISTS (
                SELECT 1 FROM conversation_task_links selected_link
                WHERE selected_link.conversation_id=? AND selected_link.task_root_id=i.investigation_id
                  AND selected_link.tenant_id=i.tenant_id AND selected_link.project_id=i.project_id))
                AND NOT EXISTS""", 1)
        return (
            sql.replace("AND NOT EXISTS", "AND i.investigation_id=? AND NOT EXISTS", 1)
            if seed
            else sql
        )

    @classmethod
    def _coordination(cls, scoped=False):
        return cls._tree(conversation=scoped) + COORDINATION_CTES

    def list(
        self,
        tenant_id: str,
        project_id: str,
        *,
        query: str = "",
        archived: bool = False,
        offset: int = 0,
        limit: int = 50,
        root_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if root_id:
            root_id = self.conversation_root(root_id, tenant_id, project_id)
        params: list[Any] = [
            tenant_id,
            project_id,
            *([root_id, root_id] if root_id else []),
            tenant_id,
            project_id,
            datetime.now(timezone.utc).isoformat(),
        ]
        predicate = "latest.position=1"
        if root_id:
            predicate += " AND latest.root_id=?"
            params.append(root_id)
        else:
            predicate += " AND COALESCE(meta.archived,0)=?"
            params.append(int(archived))
            predicate += " AND NOT EXISTS (SELECT 1 FROM conversation_task_links linked " \
                         "WHERE linked.task_root_id=latest.root_id)"
        if query:
            predicate += (
                " AND (LOWER(COALESCE(meta.title, root.request_json)) LIKE ? ESCAPE '!' "
                "OR EXISTS (SELECT 1 FROM ranked match_message WHERE "
                "(match_message.root_id=latest.root_id OR match_message.root_id IN "
                "(SELECT task_root_id FROM conversation_task_links "
                "WHERE conversation_id=latest.root_id)) AND "
                "LOWER(match_message.request_json) LIKE ? ESCAPE '!'))"
            )
            escaped = query.lower().replace("!", "!!").replace("%", "!%").replace("_", "!_")
            params.extend([f"%{escaped}%"] * 2)
        params.extend([limit, offset])
        rows = self.repository._connection.execute(
            self._coordination(bool(root_id))
            + """SELECT latest.*, root.request_json AS root_request_json,
              root.created_at AS root_created_at, meta.title, COALESCE(meta.pinned,0) AS pinned,
              COALESCE(meta.archived,0) AS archived, COALESCE(meta.revision,0) AS metadata_revision,
              totals.task_count,totals.total_investigation_count,totals.active_tasks,
              totals.waiting_input_tasks,totals.completed_tasks,totals.failed_tasks,totals.canceled_tasks,
              totals.goal_failed_tasks,totals.inconclusive_tasks,totals.approval_tasks,totals.approval_requests,
              totals.missing_report_tasks,
              totals.pending_cleanup_resources,totals.pending_input_returns,totals.cleanup_tasks,totals.attention_tasks,totals.result_tasks,
              totals.activity_at,totals.event_cursor
              FROM ranked latest JOIN investigations root ON root.investigation_id=latest.root_id
              JOIN conversation_totals totals ON totals.conversation_id=latest.root_id
              LEFT JOIN workbench_conversations meta ON meta.root_id=latest.root_id
              WHERE """
            + predicate
            + """ ORDER BY COALESCE(meta.pinned,0) DESC,
              totals.activity_at DESC, latest.root_id DESC LIMIT ? OFFSET ?""",
            params,
        ).fetchall()
        from game_test_agent.contracts.models import InvestigationRequest

        return [
            {
                "conversation_id": row["root_id"],
                "latest_investigation": self.repository._view(row),
                "title": row["title"]
                or InvestigationRequest.model_validate_json(
                    row["root_request_json"]
                ).objective.description[:200],
                "pinned": bool(row["pinned"]),
                "archived": bool(row["archived"]),
                "revision": row["metadata_revision"],
                "investigation_count": int(row["total_investigation_count"]),
                "anchor_investigation_count": row["investigation_count"],
                "task_summary": task_summary(row),
                "event_cursor": row["event_cursor"],
                "created_at": row["root_created_at"],
                "updated_at": row["activity_at"],
            }
            for row in rows
        ]

    def get(self, investigation_id: str, tenant_id: str, project_id: str) -> dict[str, Any]:
        root = self.conversation_root(investigation_id, tenant_id, project_id)
        results = self.list(tenant_id, project_id, root_id=root, limit=1)
        if not results:
            raise NotFoundError("conversation not found")
        return results[0]

    def investigations(
        self,
        root_id: str,
        tenant_id: str,
        project_id: str,
        *,
        before: str | None = None,
        limit: int = 20,
    ):
        self.root(root_id, tenant_id, project_id)
        params: list[Any] = [tenant_id, project_id, root_id, tenant_id, project_id, root_id]
        predicate = "root_id=?"
        if before:
            if self.root(before, tenant_id, project_id) != root_id:
                raise NotFoundError("cursor not found in this conversation")
            cursor = self.repository.get_investigation(before)
            predicate += " AND (created_at<? OR (created_at=? AND investigation_id<?))"
            params.extend([cursor.created_at.isoformat(), cursor.created_at.isoformat(), before])
        params.append(limit)
        rows = self.repository._connection.execute(
            self._tree(True)
            + "SELECT * FROM ranked WHERE "
            + predicate
            + " ORDER BY created_at DESC, investigation_id DESC LIMIT ?",
            params,
        ).fetchall()
        return [self.repository._view(row) for row in reversed(rows)]

    def update(
        self, root_id: str, tenant_id: str, project_id: str, expected_revision: int, **changes
    ):
        root = self.conversation_root(root_id, tenant_id, project_id)
        with self.repository.transaction() as connection:
            self.repository._lock_investigation_row(connection, root)
            row = connection.execute(
                "SELECT * FROM workbench_conversations WHERE root_id=?", (root,)
            ).fetchone()
            revision = row["revision"] if row else 0
            if expected_revision != revision:
                raise ConflictError(f"conversation metadata revision is {revision}")
            title = (
                row["title"]
                if row
                else self.repository.get_investigation(root).request.objective.description[:200]
            )
            values = {
                "title": title,
                "pinned": bool(row["pinned"]) if row else False,
                "archived": bool(row["archived"]) if row else False,
                **changes,
            }
            now = datetime.now(timezone.utc).isoformat()
            if row:
                result = connection.execute(
                    "UPDATE workbench_conversations SET title=?, pinned=?, archived=?, revision=?, "
                    "updated_at=? WHERE root_id=? AND revision=?",
                    (
                        values["title"],
                        int(values["pinned"]),
                        int(values["archived"]),
                        revision + 1,
                        now,
                        root,
                        expected_revision,
                    ),
                )
                if result.rowcount != 1:
                    raise ConflictError("conversation metadata was changed; refresh before editing")
            else:
                try:
                    connection.execute(
                        "INSERT INTO workbench_conversations VALUES (?,?,?,?,?,?)",
                        (
                            root,
                            values["title"],
                            int(values["pinned"]),
                            int(values["archived"]),
                            1,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    raise ConflictError(
                        "conversation metadata was changed; refresh before editing"
                    ) from error
        return self.get(root, tenant_id, project_id)
