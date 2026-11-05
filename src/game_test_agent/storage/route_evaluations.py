"""Durable request ownership and generation fences over the existing route receipts.

Behavior reference: dsh-superagent-prototype@6337fc823ce20251c8de8085948165c81aaa701e
core/evals.ts (evaluation identity/deadline/requeues) and app/effects.ts (generation).
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from game_test_agent.storage.repository import ConflictError, canonical_json


SQLITE_SCHEMA = ("""CREATE TABLE IF NOT EXISTS conversation_route_evaluations (
    conversation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
    principal_id TEXT NOT NULL, request_key TEXT NOT NULL, payload_hash TEXT NOT NULL,
    original_json TEXT NOT NULL, policy_version TEXT NOT NULL,
    generation INTEGER NOT NULL, owner_token TEXT NOT NULL,
    state TEXT NOT NULL, lease_deadline REAL NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    PRIMARY KEY(conversation_id,principal_id,request_key))""",)


class RouteEvaluationBusy(RuntimeError):
    """Retry this SAME request identity; it is neither a new message nor a conflict."""


@dataclass(frozen=True)
class RouteEvaluationLease:
    conversation_id: str
    principal_id: str
    request_key: str
    generation: int
    owner_token: str
    policy_version: str
    exhausted: bool = False


class RouteEvaluationStore:
    # Match the upstream principle: initial evaluation plus two recoveries, finite.
    MAX_GENERATIONS = 3
    RECOVERY_DELAY = 5

    def __init__(self, repository, *, clock=time.time):
        self.repository = repository
        self.clock = clock

    def claim_in_transaction(self, connection, root, principal, token, digest, original,
                             policy_version, timeout_seconds):
        """Caller holds the conversation-root row lock and checks access/archive."""
        now = self.clock()
        row = connection.execute("""SELECT * FROM conversation_route_evaluations
            WHERE conversation_id=? AND principal_id=? AND request_key=?""", (root, principal, token)).fetchone()
        if row is not None:
            if row["payload_hash"] != digest or row["original_json"] != canonical_json(original):
                raise ConflictError("Idempotency-Key was already used for a different message")
            if row["state"] == "finished":
                raise ConflictError("finished route evaluation is missing its receipt")
            if row["state"] not in {"running", "retryable"}:
                raise ConflictError("route evaluation has an invalid state")
            if row["state"] == "running" and row["lease_deadline"] > now:
                raise RouteEvaluationBusy("消息正在判断归属，请稍后重试同一条消息。")
            generation = row["generation"] + 1
        else:
            generation = 1
        exhausted = generation > self.MAX_GENERATIONS
        # The extra generation owns only a deterministic unavailable receipt.
        owner = secrets.token_hex(16)
        deadline = now + timeout_seconds + 5
        if row is None:
            connection.execute("""INSERT INTO conversation_route_evaluations
                (conversation_id,principal_id,request_key,payload_hash,original_json,policy_version,
                 generation,owner_token,state,lease_deadline,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,'running',?,?,?)""",
                (root, principal, token, digest, canonical_json(original), policy_version,
                 generation, owner, deadline, now, now))
        else:
            connection.execute("""UPDATE conversation_route_evaluations SET policy_version=?,generation=?,
                owner_token=?,state='running',lease_deadline=?,updated_at=?
                WHERE conversation_id=? AND principal_id=? AND request_key=?""",
                (policy_version, generation, owner, deadline, now, root, principal, token))
        return RouteEvaluationLease(root, principal, token, generation, owner, policy_version, exhausted)

    def finish_in_transaction(self, connection, lease):
        result = connection.execute("""UPDATE conversation_route_evaluations SET state='finished',updated_at=?
            WHERE conversation_id=? AND principal_id=? AND request_key=? AND generation=?
            AND owner_token=? AND policy_version=? AND state='running' AND lease_deadline>?""",
            (self.clock(), lease.conversation_id, lease.principal_id, lease.request_key,
             lease.generation, lease.owner_token, lease.policy_version, self.clock()))
        if result.rowcount != 1:
            raise RouteEvaluationBusy("这次归属判断已过期，请重试同一条消息。")

    def abandon(self, lease):
        # Cancellation/rollback releases only its own generation, not a newer one.
        with self.repository.transaction() as connection:
            connection.execute("""UPDATE conversation_route_evaluations SET state='retryable',updated_at=?
                WHERE conversation_id=? AND principal_id=? AND request_key=? AND generation=?
                AND owner_token=? AND state='running'""",
                (self.clock(), lease.conversation_id, lease.principal_id, lease.request_key,
                 lease.generation, lease.owner_token))

    def due(self, *, limit=4):
        """Bounded metadata scan; original text is loaded only for each selected row."""
        if not 1 <= limit <= 16:
            raise ValueError("route recovery batch must contain 1–16 items")
        now = self.clock()
        return self.repository._connection.execute("""SELECT e.conversation_id,e.principal_id,
            e.request_key,e.payload_hash,e.generation,e.owner_token,i.tenant_id,i.project_id
            FROM conversation_route_evaluations e
            JOIN investigations i ON i.investigation_id=e.conversation_id
            LEFT JOIN workbench_conversations w ON w.root_id=e.conversation_id
            WHERE ((e.state='running' AND e.lease_deadline<=?)
                OR (e.state='retryable' AND e.updated_at<=?)) AND COALESCE(w.archived,0)=0
            AND NOT (e.principal_id='input-return-router' AND e.request_key LIKE 'return-%')
            AND NOT (e.principal_id='observation-router' AND e.request_key LIKE 'obatch-%')
            AND NOT EXISTS (SELECT 1 FROM workbench_message_receipts r
                WHERE r.root_id=e.conversation_id AND r.principal_id=e.principal_id
                    AND r.request_key=e.request_key)
            ORDER BY e.updated_at,e.conversation_id,e.principal_id,e.request_key LIMIT ?""",
            (now, now - self.RECOVERY_DELAY, limit)).fetchall()

    def ensure_due_in_transaction(self, connection, root, principal, token, generation):
        row = connection.execute("""SELECT state,generation,lease_deadline,updated_at
            FROM conversation_route_evaluations WHERE conversation_id=? AND principal_id=? AND request_key=?""",
            (root, principal, token)).fetchone()
        now = self.clock()
        valid = row is not None and row["generation"] == generation and (
            (row["state"] == "running" and row["lease_deadline"] <= now)
            or (row["state"] == "retryable" and row["updated_at"] <= now - self.RECOVERY_DELAY))
        if not valid:
            raise RouteEvaluationBusy("评估状态已变化，本次后台恢复不再接手。")

    def original_for_recovery(self, candidate):
        row = self.repository._connection.execute("""SELECT SUBSTR(original_json,1,40001) AS original_json
            FROM conversation_route_evaluations WHERE conversation_id=? AND principal_id=? AND request_key=?
            AND generation=? AND owner_token=?""", (candidate["conversation_id"], candidate["principal_id"],
            candidate["request_key"], candidate["generation"], candidate["owner_token"])).fetchone()
        if row is None:
            raise RouteEvaluationBusy("评估已被其他执行者更新。")
        return row["original_json"]

    def defer_recovery(self, candidate, *, unavailable=False):
        # CAS protects a live replacement against an old scanner's error handler.
        with self.repository.transaction() as connection:
            connection.execute("""UPDATE conversation_route_evaluations SET state=?,updated_at=?
                WHERE conversation_id=? AND principal_id=? AND request_key=? AND generation=?
                AND owner_token=? AND state IN ('running','retryable')""",
                ("unavailable" if unavailable else "retryable", self.clock(), candidate["conversation_id"],
                 candidate["principal_id"], candidate["request_key"], candidate["generation"], candidate["owner_token"]))
