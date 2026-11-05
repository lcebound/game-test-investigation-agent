"""Durable worker handbacks over immutable input commands and the existing outbox.

Behavior reference: dsh-superagent-prototype@6337fc823ce20251c8de8085948165c81aaa701e,
core/evals.ts::handback, core/part.ts::handBack, core/model.ts::followupOwners.
Only a reached input can be returned; a returned task cannot receive it again.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_test_agent.contracts.models import InputCommand, TERMINAL_STATUSES
from game_test_agent.events.coordinator import excerpt
from game_test_agent.events.message_router import ConversationMessageRouter
from game_test_agent.storage.repository import ConflictError, NotFoundError, canonical_json, payload_hash
from game_test_agent.storage.workbench import WorkbenchStore


SQLITE_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS task_input_returns (
        return_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
        source_task_id TEXT NOT NULL,source_investigation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
        source_command_id TEXT NOT NULL,trigger_id TEXT NOT NULL,
        origin_investigation_id TEXT NOT NULL REFERENCES investigations(investigation_id),origin_command_id TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,reason TEXT NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL,
        target_investigation_id TEXT,target_command_id TEXT,decision_json TEXT,
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
        UNIQUE(source_investigation_id,source_command_id),UNIQUE(target_investigation_id,target_command_id))""",
    "CREATE INDEX IF NOT EXISTS ix_input_returns_pending ON task_input_returns(conversation_id,state,created_at,return_id)",
    "CREATE INDEX IF NOT EXISTS ix_input_returns_origin ON task_input_returns(trigger_id,source_task_id)",
)


class InputHandback(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    input_id: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=300)


class InputReturnDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    action: Literal["attach", "dismiss"]
    target_investigation_id: str | None = Field(default=None, min_length=1, max_length=96)

    @model_validator(mode="after")
    def coherent_target(self):
        if (self.action == "attach") != (self.target_investigation_id is not None):
            raise ValueError("attach requires a target; dismiss has no target")
        return self


class InputReturnService:
    def __init__(self, repository):
        self.repository = repository
        self.store = WorkbenchStore(repository)

    def _command(self, investigation_id, command_id):
        row = self.repository._connection.execute("""SELECT payload_json FROM workflow_command_outbox
            WHERE investigation_id=? AND command_id=? AND kind='input'""", (investigation_id, command_id)).fetchone()
        if row is None:
            raise NotFoundError("input did not reach this investigation")
        try:
            payload = json.loads(row["payload_json"])
            content = payload["content"]
            if not isinstance(content, str) or not 1 <= len(content) <= 20000:
                raise ValueError("invalid input content")
            return payload
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            raise ConflictError("stored input command is invalid") from error

    def _content(self, row):
        content = self._command(row["origin_investigation_id"], row["origin_command_id"])["content"]
        if hashlib.sha256(content.encode()).hexdigest() != row["content_sha256"]:
            raise ConflictError("original input changed")
        return content

    @staticmethod
    def _card(row):
        return {key: row[key] for key in ("return_id", "source_task_id", "source_investigation_id",
            "source_command_id", "state", "revision", "target_investigation_id", "created_at")} | {
                "reason_excerpt": excerpt(row["reason"], 240), "reason_assessment": "worker_routing_assessment"}

    def _get(self, return_id):
        row = self.repository._connection.execute("SELECT * FROM task_input_returns WHERE return_id=?", (return_id,)).fetchone()
        if row is None:
            raise NotFoundError("returned input not found")
        return row

    def _excluded(self, trigger_id):
        return {r["source_task_id"] for r in self.repository._connection.execute(
            "SELECT source_task_id FROM task_input_returns WHERE trigger_id=?", (trigger_id,)).fetchall()}

    def _changed(self, connection, row, event_type):
        self.repository._insert_event(connection, row["source_investigation_id"], event_type,
            {"return_id": row["return_id"], "input_id": row["source_command_id"], "state": row["state"],
             "target_investigation_id": row["target_investigation_id"]})

    def handback(self, investigation_id, request: InputHandback):
        repo = self.repository
        view = repo.get_investigation(investigation_id)
        tenant, project = view.request.tenant_id, view.request.project_id
        root = self.store.conversation_root(investigation_id, tenant, project)
        with repo.transaction() as connection:
            repo._lock_investigation_row(connection, root)
            existing = connection.execute("SELECT * FROM task_input_returns WHERE source_investigation_id=? AND source_command_id=?",
                (investigation_id, request.input_id)).fetchone()
            if existing:
                if existing["reason"] != request.reason:
                    raise ConflictError("input was already returned with another reason")
                return {**self._card(existing), "duplicate": True}
            view = repo.get_investigation(investigation_id)
            if view.status in TERMINAL_STATUSES or view.status == "canceling":
                raise ConflictError("only an active worker can return an input")
            source_task, head = self.store.task(root, investigation_id, tenant, project)
            if head.investigation_id != investigation_id:
                raise ConflictError("worker is no longer the task head")
            content = self._command(investigation_id, request.input_id)["content"]
            prior = connection.execute("SELECT * FROM task_input_returns WHERE target_investigation_id=? AND target_command_id=?",
                (investigation_id, request.input_id)).fetchone()
            origin_inv, origin_id = ((prior["origin_investigation_id"], prior["origin_command_id"]) if prior else
                                     (investigation_id, request.input_id))
            trigger = prior["trigger_id"] if prior else "input-" + payload_hash([origin_inv, origin_id])
            digest = hashlib.sha256(content.encode()).hexdigest()
            if prior and digest != prior["content_sha256"]:
                raise ConflictError("forwarded input does not match its original")
            identity, now = "return-" + payload_hash([investigation_id, request.input_id]), datetime.now(timezone.utc).isoformat()
            connection.execute("""INSERT INTO task_input_returns (
                return_id,conversation_id,source_task_id,source_investigation_id,source_command_id,trigger_id,
                origin_investigation_id,origin_command_id,content_sha256,reason,state,revision,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,'pending',1,?,?)""",
                (identity, root, source_task, investigation_id, request.input_id, trigger, origin_inv, origin_id, digest, request.reason, now, now))
            connection.execute("""UPDATE workflow_command_outbox SET state='obsolete',last_error='input_returned',updated_at=?
                WHERE command_id=? AND state='pending'""", (now, request.input_id))
            # Returning an instruction changes the context of outstanding code proposals.
            permissions = connection.execute("SELECT approval_id FROM code_permission_requests WHERE investigation_id=? AND state='pending'", (investigation_id,)).fetchall()
            connection.execute("UPDATE code_permission_requests SET state='canceled' WHERE investigation_id=? AND state='pending'", (investigation_id,))
            for permission in permissions:
                repo._insert_event(connection, investigation_id, "analysis.permission_resolved",
                    {"approval_id": permission["approval_id"], "status": "canceled", "reason": "input_returned"})
            row = self._get(identity)
            self._changed(connection, row, "task.input_returned")
            # Reassess only original explicit addressing. A worker's reason grants no destination.
            if len(content) <= 4000 and not self.store.get(root, tenant, project)["archived"]:
                route = ConversationMessageRouter(repo).resolve(root, content, tenant, project,
                    excluded_task_ids=self._excluded(trigger), allow_only_task=False)
                if route["kind"] == "task" and route["status"] not in {*TERMINAL_STATUSES, "canceling"}:
                    target = repo.get_investigation(route["investigation_id"])
                    if datetime.now(timezone.utc).timestamp() < target.created_at.timestamp() + target.request.budget.max_wall_seconds:
                        try:
                            self._attach(connection, row, route["investigation_id"], tenant, project, "coordinator", "explicit_original_address")
                        except (ConflictError, NotFoundError):
                            # The candidate is advisory. A changed head or access
                            # leaves the original handback queued, never discarded.
                            pass
            return {**self._card(self._get(identity)), "duplicate": False}

    def _attach(self, connection, row, target_id, tenant, project, principal_id, basis):
        summary = self.store.get(row["conversation_id"], tenant, project)
        if summary["archived"]:
            raise ConflictError("restore the conversation before reassigning inputs")
        target_task, target = self.store.task(row["conversation_id"], target_id, tenant, project)
        if target_id != target.investigation_id:
            raise ConflictError("target task has a newer head")
        self.repository._lock_investigation_row(connection, target_id)
        target = self.repository.get_investigation(target_id)
        if target.status in TERMINAL_STATUSES or target.status == "canceling":
            raise ConflictError("returned inputs do not resume ended or stopped tasks")
        if datetime.now(timezone.utc).timestamp() >= target.created_at.timestamp() + target.request.budget.max_wall_seconds:
            raise ConflictError("target task has exhausted its wall-clock budget")
        if target_task in self._excluded(row["trigger_id"]):
            raise ConflictError("a returned input cannot go back to a task that rejected it")
        command_id = "returned-input-" + payload_hash([row["trigger_id"], target_task])
        if connection.execute("SELECT 1 FROM workflow_command_outbox WHERE command_id=?", (command_id,)).fetchone():
            raise ConflictError("forwarded input identity already exists")
        command = InputCommand(command_id=command_id, expected_revision=target.revision, content=self._content(row))
        now = datetime.now(timezone.utc).isoformat()
        connection.execute("""INSERT INTO workflow_command_outbox
            (command_id,investigation_id,kind,payload_json,state,created_at,updated_at) VALUES (?,?,'input',?,'pending',?,?)""",
            (command_id, target_id, canonical_json(command), now, now))
        self.repository._insert_event(connection, target_id, "command.input.accepted",
            {"command_id": command_id, "input_return_id": row["return_id"]})
        decision = {"action": "attach", "target_investigation_id": target_id, "principal_id": principal_id, "basis": basis}
        connection.execute("""UPDATE task_input_returns SET state='routed',revision=revision+1,
            target_investigation_id=?,target_command_id=?,decision_json=?,updated_at=? WHERE return_id=? AND state='pending'""",
            (target_id, command_id, canonical_json(decision), now, row["return_id"]))
        self._changed(connection, self._get(row["return_id"]), "task.input_return_resolved")

    def decide(self, root, return_id, request: InputReturnDecision, tenant, project, principal_id):
        root = self.store.conversation_root(root, tenant, project)
        with self.repository.transaction() as connection:
            self.repository._lock_investigation_row(connection, root)
            row = self._get(return_id)
            if row["conversation_id"] != root:
                raise NotFoundError("returned input not found in this conversation")
            expected = {"action": request.action, "target_investigation_id": request.target_investigation_id,
                        "principal_id": principal_id, "basis": "explicit_user_decision"}
            if row["state"] != "pending":
                if json.loads(row["decision_json"] or "null") == expected and row["revision"] == request.expected_revision + 1:
                    return {**self._card(row), "duplicate": True}
                raise ConflictError("returned input has already been resolved")
            if row["revision"] != request.expected_revision:
                raise ConflictError("returned input changed; refresh before deciding")
            if self.store.get(root, tenant, project)["archived"]:
                raise ConflictError("restore the conversation before resolving inputs")
            if request.action == "attach":
                self._attach(connection, row, request.target_investigation_id, tenant, project, principal_id, "explicit_user_decision")
            else:
                connection.execute("""UPDATE task_input_returns SET state='dismissed',revision=revision+1,
                    decision_json=?,updated_at=? WHERE return_id=? AND state='pending'""",
                    (canonical_json(expected), datetime.now(timezone.utc).isoformat(), return_id))
                self._changed(connection, self._get(return_id), "task.input_return_resolved")
            return {**self._card(self._get(return_id)), "duplicate": False}

    def detail(self, root, return_id, tenant, project):
        root = self.store.conversation_root(root, tenant, project)
        row = self._get(return_id)
        if row["conversation_id"] != root:
            raise NotFoundError("returned input not found in this conversation")
        return {**self._card(row), "content": self._content(row), "sha256": row["content_sha256"],
                "excluded_task_ids": sorted(self._excluded(row["trigger_id"]))}

    def page(self, root, tenant, project, *, offset=0, limit=10):
        root = self.store.conversation_root(root, tenant, project)
        if not 0 <= offset <= 1_000_000 or not 1 <= limit <= 10:
            raise ValueError("returned-input page is outside bounds")
        count = self.repository._connection.execute("SELECT COUNT(*) FROM task_input_returns WHERE conversation_id=? AND state='pending'", (root,)).fetchone()[0]
        rows = self.repository._connection.execute("""SELECT * FROM task_input_returns
            WHERE conversation_id=? AND state='pending' ORDER BY created_at,return_id LIMIT ? OFFSET ?""", (root, limit + 1, offset)).fetchall()
        result = {"items": [], "pending_count": count, "next_offset": None}
        for row in rows[:limit]:
            card = self._card(row)
            try:
                card["content_excerpt"] = excerpt(self._content(row), 240)
            except (NotFoundError, ConflictError):
                card.update(unavailable=True, content_excerpt="原始消息不可核验；不能转交，需核对原记录。")
            if len(canonical_json([*result["items"], card]).encode()) > 15000:
                break
            result["items"].append(card)
        if len(rows) > len(result["items"]):
            result["next_offset"] = offset + len(result["items"])
        return result
