"""Durable human approvals. Tool/model output is never an approval channel."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath

from game_test_agent.contracts.models import CodeExecutionPermissions, CodeExecutionRequest
from game_test_agent.storage import ConflictError, NotFoundError, canonical_json, payload_hash


def same_permissions(left, right):
    """Compare accepted scopes without reopening paths during result replay."""
    def key(value):
        if value is None:
            return None
        return (value.network, value.reason, tuple(
            (os.path.normcase(os.path.abspath(item.path)), item.access) for item in value.directories))
    return key(left) == key(right)


def validate_directories(permissions: CodeExecutionPermissions, protected: list[Path]):
    """Allow local existing directories, never aliases into trusted agent state."""
    directories, seen = [], set()
    for item in permissions.directories:
        raw = item.path
        if os.name == "nt":
            windows = PureWindowsPath(raw)
            if (not windows.is_absolute() or len(windows.drive) != 2
                    or ":" in raw[2:] or raw.startswith(("\\\\", "//"))):
                raise ValueError("permission paths must be absolute local drive directories")
        candidate = Path(raw)
        if not candidate.is_absolute() or "\x00" in raw:
            raise ValueError("permission paths must be absolute local directories")
        # Check every component, not only the final directory.
        for part in [candidate, *candidate.parents]:
            if part.is_symlink() or getattr(part.lstat(), "st_file_attributes", 0) & 0x400:
                raise PermissionError("permission paths cannot contain symlinks or reparse points")
        path = candidate.resolve(strict=True)
        if not path.is_dir() or path == Path(path.anchor):
            raise ValueError("permission paths must name existing directories, not drive roots")
        for boundary in protected:
            boundary = boundary.resolve()
            if path == boundary or path in boundary.parents or boundary in path.parents:
                raise PermissionError("agent state, credentials, OS and runtime directories cannot be granted")
        key = os.path.normcase(str(path))
        if key in seen:
            raise ValueError("permission directories must be unique")
        seen.add(key)
        directories.append(item.model_copy(update={"path": str(path)}))
    return permissions.model_copy(update={"directories": directories})


class CodePermissionStore:
    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def _view(row):
        expires = row["expires_at"]
        if isinstance(expires, datetime):
            expires = expires.replace(tzinfo=timezone.utc).isoformat()
        return {**json.loads(row["request_json"]), "request_hash": row["request_hash"],
                "status": row["state"], "expires_at": expires,
                "decision": json.loads(row["decision_json"]) if row["decision_json"] else None}

    def get(self, approval_id):
        row = self.repository._connection.execute(
            "SELECT * FROM code_permission_requests WHERE approval_id=?", (approval_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(approval_id)
        result = self._view(row)
        if result["status"] == "pending":
            view = self.repository.get_investigation(result["investigation_id"])
            if view.status in {"canceling", "canceled", "completed", "failed"}:
                self._finish_pending(approval_id, "canceled")
            elif datetime.now(timezone.utc) >= datetime.fromisoformat(result["expires_at"]):
                self._finish_pending(approval_id, "expired")
            else:
                return result
            return self.get(approval_id)
        return result

    def list(self, investigation_id):
        rows = self.repository._connection.execute(
            """SELECT approval_id FROM code_permission_requests WHERE investigation_id=?
               ORDER BY created_at,approval_id""", (investigation_id,)
        ).fetchall()
        return [self.get(row["approval_id"]) for row in rows]

    def input_superseded(self, record):
        requested = datetime.fromisoformat(record["requested_at"])
        returned = self.repository._connection.execute("SELECT 1 FROM task_input_returns WHERE source_investigation_id=? AND created_at>? LIMIT 1",
            (record["investigation_id"], record["requested_at"])).fetchone()
        if returned:
            return True
        for row in self.repository.list_user_inputs(record["investigation_id"]):
            created = row["created_at"]
            if not isinstance(created, datetime):
                created = datetime.fromisoformat(created)
            if created.replace(tzinfo=created.tzinfo or timezone.utc) > requested:
                return True
        return False

    def propose(self, investigation_id, decision_id, job_id, request, runtime_digest, input_hashes):
        view = self.repository.get_investigation(investigation_id)
        now = datetime.now(timezone.utc)
        deadline = view.created_at + timedelta(seconds=view.request.budget.max_wall_seconds)
        if view.status in {"canceling", "canceled", "completed", "failed"} or now >= deadline:
            raise PermissionError("investigation no longer permits permission requests")
        approval_id = "approval-" + payload_hash([investigation_id, job_id])[:40]
        immutable = {"investigation_id": investigation_id, "decision_id": decision_id,
            "job_id": job_id, "request": request.model_dump(mode="json"),
            "runtime_digest": runtime_digest, "input_hashes": input_hashes}
        digest = payload_hash(immutable)
        record = {**immutable, "approval_id": approval_id,
                  "requested_at": now.isoformat(), "expires_at": min(deadline, now+timedelta(minutes=10)).isoformat()}
        with self.repository.transaction() as connection:
            inserted = connection.execute("""INSERT OR IGNORE INTO code_permission_requests (
                approval_id,job_id,investigation_id,request_json,request_hash,state,created_at,expires_at
                ) VALUES (?,?,?,?,?,'pending',?,?)""",
                (approval_id, job_id, investigation_id, canonical_json(record), digest,
                 record["requested_at"], record["expires_at"])).rowcount
            row = connection.execute("SELECT * FROM code_permission_requests WHERE approval_id=?", (approval_id,)).fetchone()
            if row["request_hash"] != digest:
                raise ConflictError("code, inputs, runtime or permissions changed after the permission request")
            # Event emission is idempotent with request creation.
            if inserted:
                self.repository._insert_event(connection, investigation_id, "analysis.permission_requested",
                    {"approval_id": approval_id, "job_id": job_id, "request_hash": digest})
        return self.get(approval_id)

    def _finish_pending(self, approval_id, state):
        with self.repository.transaction() as connection:
            row = connection.execute("SELECT * FROM code_permission_requests WHERE approval_id=?", (approval_id,)).fetchone()
            if row is None:
                raise NotFoundError(approval_id)
            changed = connection.execute("UPDATE code_permission_requests SET state=? WHERE approval_id=? AND state='pending'",
                (state, approval_id)).rowcount
            if changed:
                self.repository._insert_event(connection, row["investigation_id"], "analysis.permission_resolved",
                    {"approval_id": approval_id, "status": state})

    def cancel_pending(self, investigation_id):
        for record in self.list(investigation_id):
            if record["status"] == "pending":
                self._finish_pending(record["approval_id"], "canceled")

    def decide(self, approval_id, request_hash, decision, principal, reason=""):
        record = self.get(approval_id)
        view = self.repository.get_investigation(record["investigation_id"])
        if (principal.tenant_id, principal.project_id) != (view.request.tenant_id, view.request.project_id):
            raise PermissionError("permission request scope mismatch")
        if "agent:analysis:approve" not in principal.scopes:
            raise PermissionError("agent:analysis:approve scope required")
        if request_hash != record["request_hash"]:
            raise ConflictError("permission request changed; refresh before deciding")
        state = {"approve": "approved", "deny": "denied"}[decision]
        receipt = {"principal_id": principal.principal_id, "decision": decision,
                   "reason": reason, "decided_at": datetime.now(timezone.utc).isoformat()}
        with self.repository.transaction() as connection:
            if view.status in {"canceling", "canceled", "completed", "failed"}:
                raise ConflictError("investigation no longer accepts permission decisions")
            changed = connection.execute("""UPDATE code_permission_requests SET state=?,decision_json=?
                WHERE approval_id=? AND state='pending' AND expires_at>?""",
                (state, canonical_json(receipt), approval_id, receipt["decided_at"])).rowcount
            if not changed:
                current = self._view(connection.execute("SELECT * FROM code_permission_requests WHERE approval_id=?", (approval_id,)).fetchone())
                if (current["status"] == state and current["decision"]
                        and current["decision"]["principal_id"] == principal.principal_id
                        and current["decision"]["reason"] == reason):
                    return current
                raise ConflictError("permission request has already been decided or expired")
            self.repository._insert_event(connection, record["investigation_id"], "analysis.permission_resolved",
                {"approval_id": approval_id, "status": state, "principal_id": principal.principal_id})
            connection.execute("""INSERT INTO audit_events (
                principal_id,tenant_id,project_id,action,target_id,metadata_json,created_at
                ) VALUES (?,?,?,?,?,?,?)""", (principal.principal_id, principal.tenant_id, principal.project_id,
                "analysis.permission_decide", approval_id, canonical_json({"request_hash": request_hash, **receipt}), receipt["decided_at"]))
        return self.get(approval_id)

    def authorize_spec(self, spec, *, require_fresh=True):
        if spec.permissions is None:
            if spec.permission_approval_id or spec.environment.network_access != "disabled":
                raise PermissionError("additional access requires human approval")
            return None
        record = self.get(spec.permission_approval_id)
        request = CodeExecutionRequest.model_validate(record["request"])
        expected_inputs = {e.evidence_id: e.sha256 for e in self.repository.list_current_evidence(spec.investigation_id)
                           if e.evidence_id in spec.input_evidence_ids}
        matches = (
            record["job_id"] == spec.job_id and record["investigation_id"] == spec.investigation_id
            and record["runtime_digest"] == spec.environment.runtime_digest
            and record["input_hashes"] == expected_inputs
            and request.permissions == spec.permissions and request.language == spec.program.language
            and hashlib.sha256(request.code.encode()).hexdigest() == spec.program.code_sha256
            and request.purpose == spec.purpose and request.input_evidence_ids == spec.input_evidence_ids
            and request.repair_of_job_id == spec.program.parameters.get("repair_of_job_id")
            and spec.created_by == "agent:" + record["decision_id"]
        )
        if not matches or record["status"] != "approved":
            raise PermissionError("permission approval is not valid for this exact code job")
        if require_fresh and datetime.now(timezone.utc) >= datetime.fromisoformat(record["expires_at"]):
            raise PermissionError("permission approval expired before execution")
        return request.permissions
