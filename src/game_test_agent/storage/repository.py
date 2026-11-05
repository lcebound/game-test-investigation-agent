from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from game_test_agent.contracts.models import (
    ActionPermit,
    ActionSpec,
    ActionState,
    AnalysisJobRecord,
    AnalysisJobState,
    AnalysisResultSnapshot,
    AnomalyDetectedEvent,
    Budget,
    CapabilityDescriptor,
    CaseMemoryRecord,
    CaseMemoryStatus,
    ContextSnapshot,
    ContextSummary,
    CriterionAssessment,
    DecisionRecord,
    DecisionOutcomeRecord,
    DerivedFactCandidate,
    DependencyEdge,
    EvidenceRecord,
    EvidenceRelation,
    IngestOutcome,
    EvidenceUpdatedEvent,
    Fact,
    Hypothesis,
    InvestigationReport,
    InvestigationRequest,
    InvestigationResultData,
    InvestigationResultEvent,
    InvestigationStatus,
    InvestigationView,
    PlannedAction,
    ProjectPack,
    ProjectionCheckpoint,
    AdmissionTicket,
    ResourceRequest,
    Resolution,
    RunRef,
    RunResult,
    RunResultSnapshot,
    SandboxOperationRef,
    ReasoningStep,
    ReopenRequest,
    TestPlan,
)


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass


_VALID_INVESTIGATION_TRANSITIONS: dict[InvestigationStatus, set[InvestigationStatus]] = {
    InvestigationStatus.ACCEPTED: {InvestigationStatus.DISCOVERING, InvestigationStatus.CANCELING},
    InvestigationStatus.DISCOVERING: {InvestigationStatus.PLANNING, InvestigationStatus.CANCELING},
    InvestigationStatus.PLANNING: {
        InvestigationStatus.ACTING,
        InvestigationStatus.WAITING_INPUT,
        InvestigationStatus.ANALYZING,
        InvestigationStatus.REPORTING,
        InvestigationStatus.CANCELING,
    },
    InvestigationStatus.ACTING: {
        InvestigationStatus.WAITING_EXTERNAL,
        InvestigationStatus.ANALYZING,
        InvestigationStatus.CANCELING,
    },
    InvestigationStatus.WAITING_EXTERNAL: {
        InvestigationStatus.ANALYZING,
        InvestigationStatus.CANCELING,
    },
    InvestigationStatus.WAITING_INPUT: {
        InvestigationStatus.PLANNING,
        InvestigationStatus.CANCELING,
    },
    InvestigationStatus.ANALYZING: {
        InvestigationStatus.EVALUATING,
        InvestigationStatus.CANCELING,
    },
    InvestigationStatus.EVALUATING: {
        InvestigationStatus.PLANNING,
        InvestigationStatus.REPORTING,
        InvestigationStatus.CANCELING,
    },
    InvestigationStatus.REPORTING: {
        InvestigationStatus.COMPLETED,
        InvestigationStatus.CANCELING,
    },
    InvestigationStatus.CANCELING: {InvestigationStatus.CANCELED},
}

_VALID_CASE_MEMORY_TRANSITIONS: dict[CaseMemoryStatus, set[CaseMemoryStatus]] = {
    CaseMemoryStatus.CANDIDATE: {
        CaseMemoryStatus.REVIEWED,
        CaseMemoryStatus.RETRACTED,
    },
    CaseMemoryStatus.REVIEWED: {
        CaseMemoryStatus.ACTIVE,
        CaseMemoryStatus.RETRACTED,
    },
    CaseMemoryStatus.ACTIVE: {
        CaseMemoryStatus.DEPRECATED,
        CaseMemoryStatus.RETRACTED,
    },
    CaseMemoryStatus.DEPRECATED: {CaseMemoryStatus.RETRACTED},
    CaseMemoryStatus.RETRACTED: set(),
}

_VALID_ANALYSIS_JOB_TRANSITIONS: dict[AnalysisJobState, set[AnalysisJobState]] = {
    AnalysisJobState.PROPOSED: {
        AnalysisJobState.SUBMITTED,
        AnalysisJobState.CANCELED,
    },
    AnalysisJobState.SUBMITTED: {
        AnalysisJobState.RUNNING,
        AnalysisJobState.SUCCEEDED,
        AnalysisJobState.FAILED,
        AnalysisJobState.CANCELED,
        AnalysisJobState.UNKNOWN,
    },
    AnalysisJobState.RUNNING: {
        AnalysisJobState.SUCCEEDED,
        AnalysisJobState.FAILED,
        AnalysisJobState.CANCELED,
        AnalysisJobState.UNKNOWN,
    },
    AnalysisJobState.UNKNOWN: {
        AnalysisJobState.RUNNING,
        AnalysisJobState.SUCCEEDED,
        AnalysisJobState.FAILED,
        AnalysisJobState.CANCELED,
    },
    AnalysisJobState.SUCCEEDED: set(),
    AnalysisJobState.FAILED: set(),
    AnalysisJobState.CANCELED: set(),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, dict):
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _sandbox_operation_identity(operation: SandboxOperationRef) -> tuple[str, str, str, str]:
    return (
        operation.job_id,
        operation.runner_id,
        operation.operation_id,
        operation.fencing_token,
    )


class Repository:
    """Small synchronous repository with explicit transactions and database constraints.

    SQLite is the development backend. The public methods intentionally mirror the
    transactional boundaries required by the production MySQL implementation.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            from game_test_agent.runtime.worker_ownership import validate_owned_write
            validate_owned_write(self)
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _migrate(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS investigations (
                investigation_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL UNIQUE,
                tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                request_json TEXT NOT NULL,
                status TEXT NOT NULL,
                resolution TEXT,
                revision INTEGER NOT NULL,
                transition_seq INTEGER NOT NULL,
                stop_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (tenant_id, project_id, idempotency_key)
            );
            CREATE TABLE IF NOT EXISTS workflow_start_outbox (
                investigation_id TEXT PRIMARY KEY REFERENCES investigations(investigation_id),
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS investigation_authorizations (
                investigation_id TEXT PRIMARY KEY,
                principal_id TEXT NOT NULL,
                scopes_json TEXT NOT NULL,
                policy_profile TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS investigation_relations (
                parent_investigation_id TEXT NOT NULL,
                child_investigation_id TEXT NOT NULL UNIQUE,
                relation_type TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(parent_investigation_id, child_investigation_id, relation_type)
            );
            CREATE TABLE IF NOT EXISTS event_inbox (
                source TEXT NOT NULL, event_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL, investigation_id TEXT NOT NULL,
                received_at TEXT NOT NULL,
                PRIMARY KEY(source, event_id)
            );
            CREATE TABLE IF NOT EXISTS investigation_observations (
                event_id TEXT PRIMARY KEY, finding_id TEXT NOT NULL,
                incident_key TEXT NOT NULL, investigation_id TEXT NOT NULL,
                result_revision INTEGER NOT NULL, observation_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_observations_incident
                ON investigation_observations(incident_key, created_at);
            CREATE TABLE IF NOT EXISTS workflow_command_outbox (
                command_id TEXT PRIMARY KEY,
                investigation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
                kind TEXT NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS investigation_events (
                cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                investigation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
                event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_events_investigation_cursor
                ON investigation_events(investigation_id, cursor);
            CREATE TABLE IF NOT EXISTS capabilities (
                provider_id TEXT NOT NULL, capability_id TEXT NOT NULL,
                capability_version TEXT NOT NULL, descriptor_json TEXT NOT NULL,
                descriptor_hash TEXT NOT NULL, validated_at TEXT NOT NULL,
                PRIMARY KEY(provider_id, capability_id, capability_version)
            );
            CREATE TABLE IF NOT EXISTS budget_reservations (
                action_id TEXT PRIMARY KEY, investigation_id TEXT NOT NULL,
                device_minutes REAL NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS budget_counter_reservations (
                investigation_id TEXT NOT NULL, counter_name TEXT NOT NULL,
                reservation_id TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(investigation_id, counter_name, reservation_id)
            );
            CREATE TABLE IF NOT EXISTS cleanup_operations (
                action_id TEXT PRIMARY KEY,
                investigation_id TEXT NOT NULL,
                resource_kind TEXT NOT NULL DEFAULT 'action',
                cleanup_owner TEXT NOT NULL DEFAULT 'action-provider',
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS investigation_actions (
                action_id TEXT PRIMARY KEY, investigation_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE, semantic_hash TEXT NOT NULL,
                request_hash TEXT NOT NULL, action_json TEXT NOT NULL,
                permit_json TEXT, state TEXT NOT NULL, operation_ref_json TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(investigation_id, semantic_hash)
            );
            CREATE TABLE IF NOT EXISTS reasoning_steps (
                investigation_id TEXT NOT NULL, plan_revision INTEGER NOT NULL,
                reasoning_step_id TEXT NOT NULL, context_hash TEXT NOT NULL,
                result_json TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(investigation_id, plan_revision, reasoning_step_id)
            );
            CREATE TABLE IF NOT EXISTS context_snapshots (
                snapshot_id TEXT PRIMARY KEY, investigation_id TEXT NOT NULL,
                state_revision INTEGER NOT NULL, plan_revision INTEGER NOT NULL,
                context_hash TEXT NOT NULL, snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(investigation_id, plan_revision)
            );
            CREATE TABLE IF NOT EXISTS decision_records (
                decision_id TEXT PRIMARY KEY, investigation_id TEXT NOT NULL,
                plan_revision INTEGER NOT NULL, context_snapshot_id TEXT NOT NULL,
                expected_state_revision INTEGER NOT NULL, decision_hash TEXT NOT NULL,
                decision_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(investigation_id, plan_revision),
                FOREIGN KEY(context_snapshot_id) REFERENCES context_snapshots(snapshot_id)
            );
            CREATE TABLE IF NOT EXISTS decision_outcomes (
                decision_id TEXT NOT NULL, revision INTEGER NOT NULL,
                investigation_id TEXT NOT NULL, outcome_json TEXT NOT NULL,
                outcome_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(decision_id, revision),
                FOREIGN KEY(decision_id) REFERENCES decision_records(decision_id)
            );
            CREATE INDEX IF NOT EXISTS ix_decision_outcomes_investigation
                ON decision_outcomes(investigation_id, decision_id, revision);
            CREATE TABLE IF NOT EXISTS planned_actions (
                planning_id TEXT PRIMARY KEY, investigation_id TEXT NOT NULL,
                plan_revision INTEGER NOT NULL, decision_id TEXT NOT NULL,
                proposal_hash TEXT NOT NULL, action_hash TEXT NOT NULL,
                planned_json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(investigation_id, plan_revision),
                UNIQUE(action_hash),
                FOREIGN KEY(decision_id) REFERENCES decision_records(decision_id)
            );
            CREATE TABLE IF NOT EXISTS context_summaries (
                summary_id TEXT PRIMARY KEY, investigation_id TEXT NOT NULL,
                source_event_cursor_end INTEGER NOT NULL, source_hash TEXT NOT NULL,
                generator_version TEXT NOT NULL, summary_hash TEXT NOT NULL,
                summary_json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(investigation_id, source_hash, generator_version)
            );
            CREATE INDEX IF NOT EXISTS ix_context_summaries_latest
                ON context_summaries(investigation_id, source_event_cursor_end, created_at);
            CREATE TABLE IF NOT EXISTS case_memory_records (
                case_id TEXT NOT NULL, revision INTEGER NOT NULL,
                tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
                status TEXT NOT NULL, source_investigation_id TEXT NOT NULL,
                source_report_revision INTEGER NOT NULL,
                record_json TEXT NOT NULL, record_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(case_id, revision)
            );
            CREATE INDEX IF NOT EXISTS ix_case_memory_scope_status
                ON case_memory_records(tenant_id, project_id, status, case_id, revision);
            CREATE INDEX IF NOT EXISTS ix_case_memory_source
                ON case_memory_records(source_investigation_id, case_id, revision);
            CREATE TABLE IF NOT EXISTS analysis_jobs (
                job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
                investigation_id TEXT NOT NULL, state TEXT NOT NULL,
                spec_hash TEXT NOT NULL, record_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_analysis_jobs_investigation
                ON analysis_jobs(investigation_id, created_at);
            CREATE TABLE IF NOT EXISTS analysis_result_snapshots (
                job_id TEXT NOT NULL, result_revision INTEGER NOT NULL,
                investigation_id TEXT NOT NULL, result_hash TEXT NOT NULL,
                snapshot_json TEXT NOT NULL, ingestion_state TEXT NOT NULL,
                ingestion_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(job_id, result_revision)
            );
            CREATE TABLE IF NOT EXISTS derived_fact_candidates (
                candidate_id TEXT NOT NULL, revision INTEGER NOT NULL,
                investigation_id TEXT NOT NULL, analysis_job_id TEXT NOT NULL,
                status TEXT NOT NULL, candidate_hash TEXT NOT NULL,
                candidate_json TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(candidate_id, revision)
            );
            CREATE INDEX IF NOT EXISTS ix_derived_fact_candidates_investigation
                ON derived_fact_candidates(investigation_id, analysis_job_id, candidate_id, revision);
            CREATE TABLE IF NOT EXISTS investigation_plans (
                investigation_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                capability_snapshot_hash TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                plan_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(investigation_id, revision)
            );
            CREATE TABLE IF NOT EXISTS evidence_records (
                evidence_id TEXT PRIMARY KEY, investigation_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
                source_type TEXT NOT NULL, provider_id TEXT NOT NULL,
                run_id TEXT NOT NULL, source_evidence_id TEXT NOT NULL,
                source_revision INTEGER NOT NULL,
                evidence_json TEXT NOT NULL, content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(tenant_id, project_id, source_type, provider_id, run_id,
                       source_evidence_id, source_revision)
            );
            CREATE INDEX IF NOT EXISTS ix_evidence_content
                ON evidence_records(investigation_id, content_hash);
            CREATE TABLE IF NOT EXISTS run_result_snapshots (
                tenant_id TEXT NOT NULL, provider_id TEXT NOT NULL,
                project_id TEXT NOT NULL, run_id TEXT NOT NULL,
                result_revision INTEGER NOT NULL, action_id TEXT NOT NULL,
                investigation_id TEXT NOT NULL, result_json TEXT NOT NULL,
                result_hash TEXT NOT NULL, ingestion_state TEXT NOT NULL,
                ingestion_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(tenant_id, provider_id, project_id, run_id, result_revision),
                UNIQUE(action_id, result_revision)
            );
            CREATE TABLE IF NOT EXISTS experiment_runs (
                investigation_id TEXT NOT NULL, experiment_id TEXT NOT NULL,
                run_sequence INTEGER NOT NULL, action_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                PRIMARY KEY(investigation_id, experiment_id, run_sequence)
            );
            CREATE TABLE IF NOT EXISTS criterion_assessments (
                investigation_id TEXT NOT NULL, criterion_id TEXT NOT NULL,
                evaluated_state_revision INTEGER NOT NULL,
                assessment_json TEXT NOT NULL, assessment_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(investigation_id, criterion_id, evaluated_state_revision)
            );
            CREATE TABLE IF NOT EXISTS facts (
                fact_id TEXT PRIMARY KEY, investigation_id TEXT NOT NULL,
                fact_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evidence_relations (
                investigation_id TEXT NOT NULL, source_evidence_id TEXT NOT NULL,
                target_id TEXT NOT NULL, relation_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(investigation_id, source_evidence_id, target_id, relation_type)
            );
            CREATE TABLE IF NOT EXISTS provenance_edges (
                investigation_id TEXT NOT NULL, source_id TEXT NOT NULL,
                target_id TEXT NOT NULL, edge_kind TEXT NOT NULL, relation TEXT NOT NULL,
                algorithm_version TEXT, parameters_hash TEXT, created_at TEXT NOT NULL,
                PRIMARY KEY(investigation_id,source_id,target_id,edge_kind,relation)
            );
            CREATE INDEX IF NOT EXISTS ix_provenance_edges_target
                ON provenance_edges(investigation_id,target_id,edge_kind);
            CREATE TABLE IF NOT EXISTS projection_checkpoints (
                investigation_id TEXT NOT NULL, projection_name TEXT NOT NULL,
                processed_cursor INTEGER NOT NULL, projection_hash TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(investigation_id,projection_name)
            );
            CREATE TABLE IF NOT EXISTS hypotheses (
                investigation_id TEXT NOT NULL, hypothesis_id TEXT NOT NULL,
                revision INTEGER NOT NULL, hypothesis_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(investigation_id, hypothesis_id, revision)
            );
            CREATE TABLE IF NOT EXISTS reports (
                investigation_id TEXT NOT NULL, revision INTEGER NOT NULL,
                report_json TEXT NOT NULL, report_hash TEXT NOT NULL,
                markdown TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(investigation_id, revision)
            );
            CREATE TABLE IF NOT EXISTS event_outbox (
                event_id TEXT PRIMARY KEY, investigation_id TEXT NOT NULL,
                event_type TEXT NOT NULL, payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                principal_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL, action TEXT NOT NULL,
                target_id TEXT, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS resource_admission_requests (
                task_id TEXT NOT NULL, node_id TEXT NOT NULL, project_id TEXT NOT NULL,
                resource_domain TEXT NOT NULL, priority INTEGER NOT NULL,
                request_json TEXT NOT NULL, request_hash TEXT NOT NULL,
                state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(task_id, node_id)
            );
            CREATE INDEX IF NOT EXISTS ix_resource_admission_queue
                ON resource_admission_requests(state, project_id, priority, created_at);
            CREATE TABLE IF NOT EXISTS resource_admission_tickets (
                ticket_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, node_id TEXT NOT NULL,
                project_id TEXT NOT NULL, resource_domain TEXT NOT NULL,
                fencing_token INTEGER NOT NULL, ticket_json TEXT NOT NULL,
                state TEXT NOT NULL, consumed_device_minutes REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_resource_admission_active
                ON resource_admission_tickets(state, project_id, resource_domain);
            CREATE TABLE IF NOT EXISTS resource_fencing (
                resource_domain TEXT PRIMARY KEY, fencing_token INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS resource_scheduler_state (
                singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
                cursor_project_id TEXT
            );
            CREATE TABLE IF NOT EXISTS project_packs (
                tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, version TEXT NOT NULL,
                pack_id TEXT NOT NULL, pack_json TEXT NOT NULL, pack_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(tenant_id, project_id, version)
            );
            """
        )
        self._apply_migration(1, self._backfill_authorizations)
        self._apply_migration(2, self._rebuild_legacy_observations)
        self._apply_migration(3, lambda connection: None)
        self._apply_migration(4, self._add_outbox_delivery_observability)
        self._apply_migration(5, self._upgrade_trusted_execution_schema)
        self._apply_migration(6, self._scope_run_result_identity)
        self._apply_migration(7, self._add_shared_kernel_records)
        self._apply_migration(8, self._add_planned_actions)
        self._apply_migration(9, self._add_context_summaries)
        self._apply_migration(10, self._add_case_memory)
        self._apply_migration(11, self._add_analysis_sandbox)
        self._apply_migration(12, self._add_resource_control_plane)
        self._apply_migration(13, self._add_decision_outcomes)
        self._apply_migration(14, self._add_provenance_edges)
        self._apply_migration(15, self._add_projection_checkpoints)
        self._apply_migration(16, self._add_workbench_conversations)
        self._apply_migration(17, self._add_code_permission_requests)
        self._apply_migration(18, self._add_conversation_task_links)
        self._apply_migration(19, self._add_task_feedback)
        self._apply_migration(20, self._add_recall_links)
        self._apply_migration(21, self._add_input_returns)
        self._apply_migration(22, self._add_route_evaluations)
        self._apply_migration(23, self._add_route_recovery_indexes)
        self._apply_migration(24, self._add_harness_runtime)

    @staticmethod
    def _add_harness_runtime(connection: sqlite3.Connection) -> None:
        from game_test_agent.storage.harness_runtime_schema import SQLITE_SCHEMA
        for statement in SQLITE_SCHEMA:
            connection.execute(statement)

    @staticmethod
    def _add_route_recovery_indexes(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE INDEX IF NOT EXISTS ix_route_evaluation_deadline ON conversation_route_evaluations(state,lease_deadline)")
        connection.execute("CREATE INDEX IF NOT EXISTS ix_route_evaluation_retry ON conversation_route_evaluations(state,updated_at)")

    @staticmethod
    def _add_route_evaluations(connection: sqlite3.Connection) -> None:
        from game_test_agent.storage.route_evaluations import SQLITE_SCHEMA
        for statement in SQLITE_SCHEMA:
            connection.execute(statement)

    @staticmethod
    def _add_input_returns(connection: sqlite3.Connection) -> None:
        from game_test_agent.events.input_returns import SQLITE_SCHEMA
        for statement in SQLITE_SCHEMA:
            connection.execute(statement)

    @staticmethod
    def _add_recall_links(connection: sqlite3.Connection) -> None:
        connection.execute("""CREATE TABLE IF NOT EXISTS investigation_recall_links (
            investigation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
            source_investigation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
            manifest_json TEXT NOT NULL,manifest_hash TEXT NOT NULL,revision INTEGER NOT NULL,
            active INTEGER NOT NULL,updated_at TEXT NOT NULL,
            PRIMARY KEY(investigation_id,source_investigation_id))""")

    @staticmethod
    def _add_task_feedback(connection: sqlite3.Connection) -> None:
        from game_test_agent.storage.feedback import SQLITE_SCHEMA
        for statement in SQLITE_SCHEMA:
            connection.execute(statement)

    @staticmethod
    def _add_conversation_task_links(connection: sqlite3.Connection) -> None:
        connection.execute("""CREATE TABLE IF NOT EXISTS conversation_task_links (
            task_root_id TEXT PRIMARY KEY REFERENCES investigations(investigation_id),
            conversation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
            tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, created_at TEXT NOT NULL
        )""")
        connection.execute("""CREATE INDEX IF NOT EXISTS ix_conversation_tasks
            ON conversation_task_links(tenant_id,project_id,conversation_id,task_root_id)""")

    @staticmethod
    def _add_code_permission_requests(connection: sqlite3.Connection) -> None:
        connection.execute("""CREATE TABLE IF NOT EXISTS code_permission_requests (
            approval_id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE,
            investigation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
            request_json TEXT NOT NULL, request_hash TEXT NOT NULL,
            state TEXT NOT NULL, decision_json TEXT,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL
        )""")
        connection.execute("""CREATE INDEX IF NOT EXISTS ix_code_permissions_investigation
            ON code_permission_requests(investigation_id, created_at)""")

    def _apply_migration(self, version: int, migration) -> None:
        if self._connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?", (version,)
        ).fetchone():
            return
        with self.transaction() as connection:
            migration(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, _now()),
            )

    @staticmethod
    def _backfill_authorizations(connection: sqlite3.Connection) -> None:
        connection.execute(
            """INSERT INTO investigation_authorizations(
                 investigation_id, principal_id, scopes_json, policy_profile, created_at
               )
               SELECT investigation_id, 'system', '[]', 'bounded-diagnosis', created_at
               FROM investigations
               WHERE investigation_id NOT IN (
                 SELECT investigation_id FROM investigation_authorizations
               )"""
        )

    @staticmethod
    def _rebuild_legacy_observations(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='investigation_observations'"
        ).fetchone()
        normalized = "".join((row["sql"] if row else "").upper().split())
        if "UNIQUE(FINDING_ID,RESULT_REVISION)" not in normalized:
            return
        connection.execute(
            "ALTER TABLE investigation_observations RENAME TO investigation_observations_legacy"
        )
        connection.execute(
            """CREATE TABLE investigation_observations (
                event_id TEXT PRIMARY KEY, finding_id TEXT NOT NULL,
                incident_key TEXT NOT NULL, investigation_id TEXT NOT NULL,
                result_revision INTEGER NOT NULL, observation_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """INSERT INTO investigation_observations
               SELECT event_id,finding_id,incident_key,investigation_id,result_revision,
                      observation_json,created_at
               FROM investigation_observations_legacy"""
        )
        connection.execute("DROP TABLE investigation_observations_legacy")
        connection.execute(
            """CREATE INDEX IF NOT EXISTS ix_observations_incident
               ON investigation_observations(incident_key, created_at)"""
        )

    @staticmethod
    def _add_outbox_delivery_observability(connection: sqlite3.Connection) -> None:
        additions = {
            "workflow_start_outbox": {
                "last_error": "TEXT",
                "next_attempt_at": "TEXT",
            },
            "workflow_command_outbox": {
                "attempts": "INTEGER NOT NULL DEFAULT 0",
                "last_error": "TEXT",
                "next_attempt_at": "TEXT",
                "updated_at": "TEXT",
            },
            "event_outbox": {
                "attempts": "INTEGER NOT NULL DEFAULT 0",
                "last_error": "TEXT",
                "next_attempt_at": "TEXT",
                "updated_at": "TEXT",
            },
        }
        for table, columns in additions.items():
            existing = {
                row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, definition in columns.items():
                if name not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        connection.execute(
            "UPDATE workflow_command_outbox SET updated_at=created_at WHERE updated_at IS NULL"
        )
        connection.execute("UPDATE event_outbox SET updated_at=created_at WHERE updated_at IS NULL")

    @staticmethod
    def _upgrade_trusted_execution_schema(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(evidence_records)")}
        if columns and "source_type" not in columns:
            rows = connection.execute(
                "SELECT * FROM evidence_records ORDER BY created_at"
            ).fetchall()
            connection.execute("ALTER TABLE evidence_records RENAME TO evidence_records_legacy")
            connection.execute(
                """CREATE TABLE evidence_records (
                    evidence_id TEXT PRIMARY KEY, investigation_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
                    source_type TEXT NOT NULL, provider_id TEXT NOT NULL,
                    run_id TEXT NOT NULL, source_evidence_id TEXT NOT NULL,
                    source_revision INTEGER NOT NULL,
                    evidence_json TEXT NOT NULL, content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(tenant_id, project_id, source_type, provider_id, run_id,
                           source_evidence_id, source_revision)
                )"""
            )
            for row in rows:
                value = json.loads(row["evidence_json"])
                run_ref = value.get("run_ref") or {}
                provider_id = str(run_ref.get("provider_id") or "")
                run_id = str(run_ref.get("run_id") or "")
                source_type = "run" if run_id else "legacy"
                source_evidence_id = str(value.get("source_evidence_id") or value["evidence_id"])
                source_revision = int(value.get("source_revision", 1))
                value.update(
                    {
                        "source_type": source_type,
                        "source_evidence_id": source_evidence_id,
                        "source_revision": source_revision,
                    }
                )
                connection.execute(
                    """INSERT INTO evidence_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        row["evidence_id"],
                        row["investigation_id"],
                        row["tenant_id"],
                        row["project_id"],
                        source_type,
                        provider_id,
                        run_id,
                        source_evidence_id,
                        source_revision,
                        canonical_json(value),
                        row["content_hash"],
                        row["created_at"],
                    ),
                )
            connection.execute("DROP TABLE evidence_records_legacy")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS ix_evidence_content "
            "ON evidence_records(investigation_id, content_hash)"
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS run_result_snapshots (
                tenant_id TEXT NOT NULL, provider_id TEXT NOT NULL,
                project_id TEXT NOT NULL, run_id TEXT NOT NULL,
                result_revision INTEGER NOT NULL, action_id TEXT NOT NULL,
                investigation_id TEXT NOT NULL, result_json TEXT NOT NULL,
                result_hash TEXT NOT NULL, ingestion_state TEXT NOT NULL,
                ingestion_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(tenant_id, provider_id, project_id, run_id, result_revision),
                UNIQUE(action_id, result_revision)
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS experiment_runs (
                investigation_id TEXT NOT NULL, experiment_id TEXT NOT NULL,
                run_sequence INTEGER NOT NULL, action_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                PRIMARY KEY(investigation_id, experiment_id, run_sequence)
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS criterion_assessments (
                investigation_id TEXT NOT NULL, criterion_id TEXT NOT NULL,
                evaluated_state_revision INTEGER NOT NULL,
                assessment_json TEXT NOT NULL, assessment_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(investigation_id, criterion_id, evaluated_state_revision)
            )"""
        )

    @staticmethod
    def _scope_run_result_identity(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(run_result_snapshots)")
        }
        if not columns or "tenant_id" in columns:
            return
        connection.execute(
            "ALTER TABLE run_result_snapshots RENAME TO run_result_snapshots_unscoped"
        )
        connection.execute(
            """CREATE TABLE run_result_snapshots (
                tenant_id TEXT NOT NULL, provider_id TEXT NOT NULL,
                project_id TEXT NOT NULL, run_id TEXT NOT NULL,
                result_revision INTEGER NOT NULL, action_id TEXT NOT NULL,
                investigation_id TEXT NOT NULL, result_json TEXT NOT NULL,
                result_hash TEXT NOT NULL, ingestion_state TEXT NOT NULL,
                ingestion_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(tenant_id, provider_id, project_id, run_id, result_revision),
                UNIQUE(action_id, result_revision)
            )"""
        )
        connection.execute(
            """INSERT INTO run_result_snapshots(
                   tenant_id,provider_id,project_id,run_id,result_revision,action_id,
                   investigation_id,result_json,result_hash,ingestion_state,ingestion_error,
                   created_at,updated_at
               )
               SELECT i.tenant_id,r.provider_id,r.project_id,r.run_id,r.result_revision,
                      r.action_id,r.investigation_id,r.result_json,r.result_hash,
                      r.ingestion_state,r.ingestion_error,r.created_at,r.updated_at
               FROM run_result_snapshots_unscoped r
               JOIN investigations i ON i.investigation_id=r.investigation_id"""
        )
        connection.execute("DROP TABLE run_result_snapshots_unscoped")

    @staticmethod
    def _add_shared_kernel_records(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS context_snapshots (
                snapshot_id TEXT PRIMARY KEY, investigation_id TEXT NOT NULL,
                state_revision INTEGER NOT NULL, plan_revision INTEGER NOT NULL,
                context_hash TEXT NOT NULL, snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(investigation_id, plan_revision)
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS decision_records (
                decision_id TEXT PRIMARY KEY, investigation_id TEXT NOT NULL,
                plan_revision INTEGER NOT NULL, context_snapshot_id TEXT NOT NULL,
                expected_state_revision INTEGER NOT NULL, decision_hash TEXT NOT NULL,
                decision_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(investigation_id, plan_revision),
                FOREIGN KEY(context_snapshot_id) REFERENCES context_snapshots(snapshot_id)
            )"""
        )

    @staticmethod
    def _add_planned_actions(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS planned_actions (
                planning_id TEXT PRIMARY KEY, investigation_id TEXT NOT NULL,
                plan_revision INTEGER NOT NULL, decision_id TEXT NOT NULL,
                proposal_hash TEXT NOT NULL, action_hash TEXT NOT NULL,
                planned_json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(investigation_id, plan_revision),
                UNIQUE(action_hash),
                FOREIGN KEY(decision_id) REFERENCES decision_records(decision_id)
            )"""
        )

    @staticmethod
    def _add_context_summaries(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS context_summaries (
                summary_id TEXT PRIMARY KEY, investigation_id TEXT NOT NULL,
                source_event_cursor_end INTEGER NOT NULL, source_hash TEXT NOT NULL,
                generator_version TEXT NOT NULL, summary_hash TEXT NOT NULL,
                summary_json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(investigation_id, source_hash, generator_version)
            )"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS ix_context_summaries_latest
               ON context_summaries(investigation_id, source_event_cursor_end, created_at)"""
        )

    @staticmethod
    def _add_case_memory(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS case_memory_records (
                case_id TEXT NOT NULL, revision INTEGER NOT NULL,
                tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
                status TEXT NOT NULL, source_investigation_id TEXT NOT NULL,
                source_report_revision INTEGER NOT NULL,
                record_json TEXT NOT NULL, record_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(case_id, revision)
            )"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS ix_case_memory_scope_status
               ON case_memory_records(tenant_id, project_id, status, case_id, revision)"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS ix_case_memory_source
               ON case_memory_records(source_investigation_id, case_id, revision)"""
        )

    @staticmethod
    def _add_analysis_sandbox(connection: sqlite3.Connection) -> None:
        cleanup_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(cleanup_operations)").fetchall()
        }
        if "resource_kind" not in cleanup_columns:
            connection.execute(
                "ALTER TABLE cleanup_operations ADD COLUMN "
                "resource_kind TEXT NOT NULL DEFAULT 'action'"
            )
        if "cleanup_owner" not in cleanup_columns:
            connection.execute(
                "ALTER TABLE cleanup_operations ADD COLUMN "
                "cleanup_owner TEXT NOT NULL DEFAULT 'action-provider'"
            )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS analysis_jobs (
                job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
                investigation_id TEXT NOT NULL, state TEXT NOT NULL,
                spec_hash TEXT NOT NULL, record_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS ix_analysis_jobs_investigation
               ON analysis_jobs(investigation_id, created_at)"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS analysis_result_snapshots (
                job_id TEXT NOT NULL, result_revision INTEGER NOT NULL,
                investigation_id TEXT NOT NULL, result_hash TEXT NOT NULL,
                snapshot_json TEXT NOT NULL, ingestion_state TEXT NOT NULL,
                ingestion_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(job_id, result_revision)
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS derived_fact_candidates (
                candidate_id TEXT NOT NULL, revision INTEGER NOT NULL,
                investigation_id TEXT NOT NULL, analysis_job_id TEXT NOT NULL,
                status TEXT NOT NULL, candidate_hash TEXT NOT NULL,
                candidate_json TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(candidate_id, revision)
            )"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS ix_derived_fact_candidates_investigation
               ON derived_fact_candidates(
                 investigation_id, analysis_job_id, candidate_id, revision
               )"""
        )

    @staticmethod
    def _add_resource_control_plane(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS resource_admission_requests (
                task_id TEXT NOT NULL, node_id TEXT NOT NULL, project_id TEXT NOT NULL,
                resource_domain TEXT NOT NULL, priority INTEGER NOT NULL,
                request_json TEXT NOT NULL, request_hash TEXT NOT NULL,
                state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(task_id, node_id)
            )"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS ix_resource_admission_queue
               ON resource_admission_requests(state, project_id, priority, created_at)"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS resource_admission_tickets (
                ticket_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, node_id TEXT NOT NULL,
                project_id TEXT NOT NULL, resource_domain TEXT NOT NULL,
                fencing_token INTEGER NOT NULL, ticket_json TEXT NOT NULL,
                state TEXT NOT NULL, consumed_device_minutes REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS ix_resource_admission_active
               ON resource_admission_tickets(state, project_id, resource_domain)"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS resource_fencing (
                resource_domain TEXT PRIMARY KEY, fencing_token INTEGER NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS resource_scheduler_state (
                singleton_id INTEGER PRIMARY KEY, cursor_project_id TEXT
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS project_packs (
                tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, version TEXT NOT NULL,
                pack_id TEXT NOT NULL, pack_json TEXT NOT NULL, pack_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(tenant_id, project_id, version)
            )"""
        )

    @staticmethod
    def _add_decision_outcomes(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS decision_outcomes (
                decision_id TEXT NOT NULL, revision INTEGER NOT NULL,
                investigation_id TEXT NOT NULL, outcome_json TEXT NOT NULL,
                outcome_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(decision_id, revision),
                FOREIGN KEY(decision_id) REFERENCES decision_records(decision_id)
            )"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS ix_decision_outcomes_investigation
               ON decision_outcomes(investigation_id, decision_id, revision)"""
        )

    @staticmethod
    def _add_provenance_edges(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS provenance_edges (
                investigation_id TEXT NOT NULL, source_id TEXT NOT NULL,
                target_id TEXT NOT NULL, edge_kind TEXT NOT NULL, relation TEXT NOT NULL,
                algorithm_version TEXT, parameters_hash TEXT, created_at TEXT NOT NULL,
                PRIMARY KEY(investigation_id,source_id,target_id,edge_kind,relation)
            )"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS ix_provenance_edges_target
               ON provenance_edges(investigation_id,target_id,edge_kind)"""
        )

    @staticmethod
    def _add_projection_checkpoints(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS projection_checkpoints (
                investigation_id TEXT NOT NULL, projection_name TEXT NOT NULL,
                processed_cursor INTEGER NOT NULL, projection_hash TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(investigation_id,projection_name)
            )"""
        )

    @staticmethod
    def _add_workbench_conversations(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS workbench_conversations (
                root_id TEXT PRIMARY KEY REFERENCES investigations(investigation_id),
                title TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS workbench_message_receipts (
                root_id TEXT NOT NULL, principal_id TEXT NOT NULL, request_key TEXT NOT NULL,
                payload_hash TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(root_id,principal_id,request_key)
            )"""
        )

    def enqueue_resource_request(self, request: ResourceRequest) -> bool:
        digest = payload_hash(request)
        now = _now()
        with self.transaction() as connection:
            existing = connection.execute(
                """SELECT request_hash FROM resource_admission_requests
                   WHERE task_id=? AND node_id=?""",
                (request.task_id, request.node_id),
            ).fetchone()
            if existing:
                if existing["request_hash"] != digest:
                    raise ConflictError("resource request identity is immutable")
                return False
            connection.execute(
                """INSERT INTO resource_admission_requests(
                     task_id,node_id,project_id,resource_domain,priority,request_json,
                     request_hash,state,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,'queued',?,?)""",
                (
                    request.task_id,
                    request.node_id,
                    request.project_id,
                    request.resource_domain,
                    request.priority,
                    canonical_json(request),
                    digest,
                    now,
                    now,
                ),
            )
            return True

    def get_admission_ticket(
        self, task_id: str, node_id: str, *, active_only: bool = False
    ) -> AdmissionTicket | None:
        row = self._connection.execute(
            """SELECT ticket_json,state FROM resource_admission_tickets
               WHERE task_id=? AND node_id=? ORDER BY created_at DESC LIMIT 1""",
            (task_id, node_id),
        ).fetchone()
        if not row or (active_only and row["state"] != "active"):
            return None
        return AdmissionTicket.model_validate_json(row["ticket_json"])

    def cancel_resource_request(self, task_id: str, node_id: str) -> bool:
        with self.transaction() as connection:
            row = connection.execute(
                """SELECT state FROM resource_admission_requests
                   WHERE task_id=? AND node_id=?""",
                (task_id, node_id),
            ).fetchone()
            if not row or row["state"] != "queued":
                return False
            connection.execute(
                """UPDATE resource_admission_requests SET state='canceled',updated_at=?
                   WHERE task_id=? AND node_id=? AND state='queued'""",
                (_now(), task_id, node_id),
            )
            return True

    def admit_next_resource(
        self,
        *,
        project_device_minute_limits: dict[str, float],
        max_concurrent_per_project: int,
        now: datetime | None = None,
    ) -> AdmissionTicket | None:
        current = now or datetime.now(timezone.utc)
        current_text = current.isoformat()
        with self.transaction() as connection:
            expired = connection.execute(
                """SELECT ticket_id,ticket_json FROM resource_admission_tickets
                   WHERE state='active'"""
            ).fetchall()
            for row in expired:
                ticket = AdmissionTicket.model_validate_json(row["ticket_json"])
                if ticket.expires_at <= current:
                    connection.execute(
                        """UPDATE resource_admission_tickets
                           SET state='expired',updated_at=? WHERE ticket_id=? AND state='active'""",
                        (current_text, row["ticket_id"]),
                    )
                    action = connection.execute(
                        "SELECT state FROM investigation_actions WHERE action_id=?",
                        (ticket.node_id,),
                    ).fetchone()
                    terminal = action and action["state"] in {
                        ActionState.SUCCEEDED, ActionState.FAILED, ActionState.CANCELED
                    }
                    connection.execute(
                        """UPDATE resource_admission_requests SET state=?,updated_at=?
                           WHERE task_id=? AND node_id=? AND state='admitted'""",
                        ("canceled" if terminal else "queued", current_text,
                         ticket.task_id, ticket.node_id),
                    )

            queued = connection.execute(
                """SELECT request_json,project_id,priority,created_at,node_id
                   FROM resource_admission_requests WHERE state='queued'
                   ORDER BY project_id,priority DESC,created_at,node_id"""
            ).fetchall()
            if not queued:
                return None
            projects = sorted({row["project_id"] for row in queued})
            cursor = connection.execute(
                "SELECT cursor_project_id FROM resource_scheduler_state WHERE singleton_id=1"
            ).fetchone()
            cursor_project = cursor["cursor_project_id"] if cursor else None
            if cursor_project in projects:
                offset = projects.index(cursor_project) + 1
                projects = projects[offset:] + projects[:offset]
            requests = [ResourceRequest.model_validate_json(row["request_json"]) for row in queued]
            chosen: ResourceRequest | None = None
            for project_id in projects:
                active_count = connection.execute(
                    """SELECT COUNT(*) FROM resource_admission_tickets
                       WHERE project_id=? AND state='active'""",
                    (project_id,),
                ).fetchone()[0]
                if int(active_count) >= max_concurrent_per_project:
                    continue
                request = next(item for item in requests if item.project_id == project_id)
                used = float(
                    connection.execute(
                        """SELECT COALESCE(SUM(consumed_device_minutes),0)
                           FROM resource_admission_tickets
                           WHERE project_id=? AND state='released'""",
                        (project_id,),
                    ).fetchone()[0]
                )
                if used + request.device_minutes > project_device_minute_limits.get(
                    project_id, float("inf")
                ):
                    continue
                if request.exclusive:
                    owner = connection.execute(
                        """SELECT 1 FROM resource_admission_tickets
                           WHERE resource_domain=? AND state='active'""",
                        (request.resource_domain,),
                    ).fetchone()
                    if owner:
                        continue
                chosen = request
                break
            if chosen is None:
                return None

            fence_row = connection.execute(
                "SELECT fencing_token FROM resource_fencing WHERE resource_domain=?",
                (chosen.resource_domain,),
            ).fetchone()
            fence = int(fence_row[0]) + 1 if fence_row else 1
            if fence_row:
                connection.execute(
                    "UPDATE resource_fencing SET fencing_token=? WHERE resource_domain=?",
                    (fence, chosen.resource_domain),
                )
            else:
                connection.execute(
                    "INSERT INTO resource_fencing(resource_domain,fencing_token) VALUES (?,?)",
                    (chosen.resource_domain, fence),
                )
            identity = (
                f"{chosen.project_id}:{chosen.task_id}:{chosen.node_id}:"
                f"{chosen.resource_domain}:{fence}"
            )
            ticket = AdmissionTicket(
                ticket_id="admit-" + hashlib.sha256(identity.encode()).hexdigest()[:24],
                project_id=chosen.project_id,
                task_id=chosen.task_id,
                node_id=chosen.node_id,
                plan_step_id=chosen.plan_step_id,
                plan_node_revision=chosen.plan_node_revision,
                resource_domain=chosen.resource_domain,
                cost_limit=chosen.device_minutes,
                fencing_token=fence,
                expires_at=current + timedelta(seconds=chosen.ttl_seconds),
            )
            connection.execute(
                """INSERT INTO resource_admission_tickets(
                     ticket_id,task_id,node_id,project_id,resource_domain,fencing_token,
                     ticket_json,state,consumed_device_minutes,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,'active',0,?,?)""",
                (
                    ticket.ticket_id,
                    ticket.task_id,
                    ticket.node_id,
                    ticket.project_id,
                    ticket.resource_domain,
                    ticket.fencing_token,
                    canonical_json(ticket),
                    current_text,
                    current_text,
                ),
            )
            connection.execute(
                """UPDATE resource_admission_requests SET state='admitted',updated_at=?
                   WHERE task_id=? AND node_id=? AND state='queued'""",
                (current_text, chosen.task_id, chosen.node_id),
            )
            if cursor:
                connection.execute(
                    "UPDATE resource_scheduler_state SET cursor_project_id=? WHERE singleton_id=1",
                    (chosen.project_id,),
                )
            else:
                connection.execute(
                    """INSERT INTO resource_scheduler_state(singleton_id,cursor_project_id)
                       VALUES (1,?)""",
                    (chosen.project_id,),
                )
            return ticket

    def release_admission_ticket(
        self, ticket_id: str, *, consumed_device_minutes: float = 0
    ) -> bool:
        if consumed_device_minutes < 0:
            raise ValueError("consumed device minutes cannot be negative")
        with self.transaction() as connection:
            row = connection.execute(
                """SELECT state,ticket_json FROM resource_admission_tickets
                   WHERE ticket_id=?""",
                (ticket_id,),
            ).fetchone()
            if not row:
                raise NotFoundError(ticket_id)
            if row["state"] != "active":
                return False
            ticket = AdmissionTicket.model_validate_json(row["ticket_json"])
            if consumed_device_minutes > ticket.cost_limit:
                raise ValueError("consumption exceeds the ticket cost limit")
            connection.execute(
                """UPDATE resource_admission_tickets
                   SET state='released',consumed_device_minutes=?,updated_at=?
                   WHERE ticket_id=? AND state='active'""",
                (consumed_device_minutes, _now(), ticket_id),
            )
            connection.execute(
                """UPDATE resource_admission_requests SET state='completed',updated_at=?
                   WHERE task_id=? AND node_id=? AND state='admitted'""",
                (_now(), ticket.task_id, ticket.node_id),
            )
            return True

    def cancel_admission_ticket(self, ticket_id: str) -> AdmissionTicket:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state,ticket_json FROM resource_admission_tickets WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
            if not row:
                raise NotFoundError(ticket_id)
            ticket = AdmissionTicket.model_validate_json(row["ticket_json"])
            if row["state"] == "canceled":
                return ticket
            if row["state"] != "active":
                return ticket
            ticket = ticket.model_copy(update={"canceled": True})
            connection.execute(
                """UPDATE resource_admission_tickets
                   SET state='canceled',ticket_json=?,updated_at=? WHERE ticket_id=?""",
                (canonical_json(ticket), _now(), ticket_id),
            )
            return ticket

    def accepts_fencing_token(self, resource_domain: str, token: int) -> bool:
        row = self._connection.execute(
            "SELECT fencing_token FROM resource_fencing WHERE resource_domain=?",
            (resource_domain,),
        ).fetchone()
        return bool(row and int(row[0]) == token)

    def bind_action_admission(self, action_id: str, ticket: AdmissionTicket) -> ActionPermit:
        with self.transaction() as connection:
            row = connection.execute(
                """SELECT investigation_id,action_json,permit_json,state FROM investigation_actions
                   WHERE action_id=?""",
                (action_id,),
            ).fetchone()
            if not row:
                raise NotFoundError(action_id)
            if row["state"] not in {ActionState.RESERVED, ActionState.UNKNOWN}:
                permit = ActionPermit.model_validate_json(row["permit_json"])
                if permit.admission_ticket_id == ticket.ticket_id:
                    return permit
                raise ConflictError("admission can only be bound before dispatch")
            if (ticket.task_id, ticket.node_id) != (row["investigation_id"], action_id):
                raise ConflictError("admission ticket does not belong to this action")
            action = ActionSpec.model_validate_json(row["action_json"])
            if (ticket.plan_step_id, ticket.plan_node_revision) != (
                action.plan_step_id,
                action.plan_node_revision,
            ):
                raise ConflictError("admission ticket does not belong to this plan node")
            active = connection.execute(
                """SELECT state FROM resource_admission_tickets WHERE ticket_id=?""",
                (ticket.ticket_id,),
            ).fetchone()
            if not active or active["state"] != "active":
                raise ConflictError("admission ticket is not active")
            permit = ActionPermit.model_validate_json(row["permit_json"])
            if permit.admission_ticket_id not in {None, ticket.ticket_id}:
                old = connection.execute(
                    """SELECT state FROM resource_admission_tickets WHERE ticket_id=?""",
                    (permit.admission_ticket_id,),
                ).fetchone()
                if old and old["state"] == "active":
                    raise ConflictError("action already has a different active admission ticket")
            permit = permit.model_copy(
                update={
                    "admission_ticket_id": ticket.ticket_id,
                    "resource_domain": ticket.resource_domain,
                    "fencing_token": ticket.fencing_token,
                    "plan_step_id": ticket.plan_step_id,
                    "plan_node_revision": ticket.plan_node_revision,
                    "expires_at": ticket.expires_at,
                }
            )
            connection.execute(
                "UPDATE investigation_actions SET permit_json=?,updated_at=? WHERE action_id=?",
                (canonical_json(permit), _now(), action_id),
            )
            return permit

    def get_action_permit(self, action_id: str) -> ActionPermit:
        row = self._connection.execute(
            "SELECT permit_json FROM investigation_actions WHERE action_id=?", (action_id,)
        ).fetchone()
        if not row:
            raise NotFoundError(action_id)
        return ActionPermit.model_validate_json(row["permit_json"])

    def register_project_pack(self, pack: ProjectPack) -> str:
        digest = payload_hash(pack)
        with self.transaction() as connection:
            row = connection.execute(
                """SELECT pack_hash FROM project_packs
                   WHERE tenant_id=? AND project_id=? AND version=?""",
                (pack.tenant_id, pack.project_id, pack.version),
            ).fetchone()
            if row:
                if row["pack_hash"] != digest:
                    raise ConflictError("project pack version is immutable")
                return digest
            connection.execute(
                """INSERT INTO project_packs(
                     tenant_id,project_id,version,pack_id,pack_json,pack_hash,created_at
                   ) VALUES (?,?,?,?,?,?,?)""",
                (
                    pack.tenant_id,
                    pack.project_id,
                    pack.version,
                    pack.pack_id,
                    canonical_json(pack),
                    digest,
                    _now(),
                ),
            )
            return digest

    def get_project_pack(self, tenant_id: str, project_id: str, version: str) -> ProjectPack:
        row = self._connection.execute(
            """SELECT pack_json FROM project_packs
               WHERE tenant_id=? AND project_id=? AND version=?""",
            (tenant_id, project_id, version),
        ).fetchone()
        if not row:
            raise NotFoundError(f"project pack {project_id!r}/{version!r} is unavailable")
        return ProjectPack.model_validate_json(row["pack_json"])

    def list_project_packs(self, tenant_id: str, project_id: str) -> list[ProjectPack]:
        rows = self._connection.execute(
            """SELECT pack_json FROM project_packs
               WHERE tenant_id=? AND project_id=? ORDER BY version""",
            (tenant_id, project_id),
        ).fetchall()
        return [ProjectPack.model_validate_json(row["pack_json"]) for row in rows]

    def schema_version(self) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()
        return int(row[0])

    def ping(self) -> bool:
        return self._connection.execute("SELECT 1").fetchone()[0] == 1

    def operational_metrics(self, tenant_id: str, project_id: str) -> dict[str, Any]:
        statuses = self._connection.execute(
            """SELECT status, COUNT(*) AS value FROM investigations
               WHERE tenant_id=? AND project_id=? GROUP BY status""",
            (tenant_id, project_id),
        ).fetchall()
        unknown_actions = self._connection.execute(
            """SELECT COUNT(*) FROM investigation_actions a
               JOIN investigations i ON i.investigation_id=a.investigation_id
               WHERE i.tenant_id=? AND i.project_id=? AND a.state='unknown'""",
            (tenant_id, project_id),
        ).fetchone()[0]
        pending_starts = self._connection.execute(
            """SELECT COUNT(*) FROM workflow_start_outbox o
               JOIN investigations i ON i.investigation_id=o.investigation_id
               WHERE i.tenant_id=? AND i.project_id=? AND o.state='pending'""",
            (tenant_id, project_id),
        ).fetchone()[0]
        retrying_starts = self._connection.execute(
            """SELECT COUNT(*) FROM workflow_start_outbox o
               JOIN investigations i ON i.investigation_id=o.investigation_id
               WHERE i.tenant_id=? AND i.project_id=? AND o.state='pending' AND o.attempts>0""",
            (tenant_id, project_id),
        ).fetchone()[0]
        pending_events = self._connection.execute(
            """SELECT COUNT(*) FROM event_outbox o
               JOIN investigations i ON i.investigation_id=o.investigation_id
               WHERE i.tenant_id=? AND i.project_id=? AND o.state='pending'""",
            (tenant_id, project_id),
        ).fetchone()[0]
        pending_commands = self._connection.execute(
            """SELECT COUNT(*) FROM workflow_command_outbox o
               JOIN investigations i ON i.investigation_id=o.investigation_id
               WHERE i.tenant_id=? AND i.project_id=? AND o.state='pending'""",
            (tenant_id, project_id),
        ).fetchone()[0]
        retrying_commands = self._connection.execute(
            """SELECT COUNT(*) FROM workflow_command_outbox o
               JOIN investigations i ON i.investigation_id=o.investigation_id
               WHERE i.tenant_id=? AND i.project_id=? AND o.state='pending' AND o.attempts>0""",
            (tenant_id, project_id),
        ).fetchone()[0]
        retrying_events = self._connection.execute(
            """SELECT COUNT(*) FROM event_outbox o
               JOIN investigations i ON i.investigation_id=o.investigation_id
               WHERE i.tenant_id=? AND i.project_id=? AND o.state='pending' AND o.attempts>0""",
            (tenant_id, project_id),
        ).fetchone()[0]
        pending_cleanup = self._connection.execute(
            """SELECT COUNT(*) FROM cleanup_operations c
               JOIN investigations i ON i.investigation_id=c.investigation_id
               WHERE i.tenant_id=? AND i.project_id=? AND c.state='pending'""",
            (tenant_id, project_id),
        ).fetchone()[0]
        observation_states = self._connection.execute("""SELECT o.state,COUNT(*) AS value
            FROM coordination_observations o JOIN investigations i ON i.investigation_id=o.conversation_id
            WHERE i.tenant_id=? AND i.project_id=? GROUP BY o.state""", (tenant_id, project_id)).fetchall()
        operation_states = self._connection.execute("""SELECT o.state,COUNT(*) AS value
            FROM native_tool_jobs o JOIN investigations i ON i.investigation_id=o.investigation_id
            WHERE i.tenant_id=? AND i.project_id=? GROUP BY o.state""", (tenant_id, project_id)).fetchall()
        coordination_calls = self._connection.execute("""SELECT e.event_type,COUNT(*) AS value
            FROM investigation_events e JOIN investigations i ON i.investigation_id=e.investigation_id
            WHERE i.tenant_id=? AND i.project_id=? AND e.event_type IN
            ('conversation.synthesis_completed','conversation.observation_retry') GROUP BY e.event_type""",
            (tenant_id, project_id)).fetchall()
        cleanup_retries = self._connection.execute("""SELECT COALESCE(SUM(c.attempts),0)
            FROM cleanup_operations c JOIN investigations i ON i.investigation_id=c.investigation_id
            WHERE i.tenant_id=? AND i.project_id=? AND c.state='pending'""", (tenant_id, project_id)).fetchone()[0]
        route_states = self._connection.execute("""SELECT e.state,COUNT(*) AS value
            FROM conversation_route_evaluations e JOIN investigations i ON i.investigation_id=e.conversation_id
            WHERE i.tenant_id=? AND i.project_id=? GROUP BY e.state""", (tenant_id, project_id)).fetchall()
        oldest_operation = self._connection.execute("""SELECT MIN(o.created_at)
            FROM native_tool_jobs o JOIN investigations i ON i.investigation_id=o.investigation_id
            WHERE i.tenant_id=? AND i.project_id=? AND o.state NOT IN ('collected','canceled')""", (tenant_id, project_id)).fetchone()[0]
        operation_age = None
        if oldest_operation is not None:
            instant = oldest_operation if isinstance(oldest_operation, datetime) else datetime.fromisoformat(str(oldest_operation).replace("Z", "+00:00"))
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
            operation_age = max(0, (datetime.now(timezone.utc)-instant).total_seconds())
        return {
            "route_evaluations_by_state": {row["state"]: int(row["value"]) for row in route_states},
            "oldest_pending_native_operation_seconds": operation_age,
            "observations_by_state": {row["state"]: int(row["value"]) for row in observation_states},
            "native_operations_by_state": {row["state"]: int(row["value"]) for row in operation_states},
            "coordination_events": {row["event_type"]: int(row["value"]) for row in coordination_calls},
            "pending_cleanup_attempts": int(cleanup_retries),
            "investigations_by_status": {row["status"]: int(row["value"]) for row in statuses},
            "unknown_actions": int(unknown_actions),
            "pending_workflow_starts": int(pending_starts),
            "retrying_workflow_starts": int(retrying_starts),
            "pending_result_events": int(pending_events),
            "pending_workflow_commands": int(pending_commands),
            "retrying_workflow_commands": int(retrying_commands),
            "retrying_result_events": int(retrying_events),
            "pending_cleanup_operations": int(pending_cleanup),
            "schema_version": self.schema_version(),
        }

    def ingest_anomaly(
        self,
        event: AnomalyDetectedEvent,
        *,
        principal_id: str | None = None,
        scopes: set[str] | None = None,
        policy_profile: str = "bounded-diagnosis",
        budget: Budget | None = None,
    ) -> tuple[InvestigationView, bool]:
        """Atomically persist Inbox, observation, investigation and workflow start intent."""
        scopes = scopes or set()
        digest = payload_hash(event)
        event_json = canonical_json(event)
        now = _now()
        with self.transaction() as connection:
            duplicate = connection.execute(
                "SELECT payload_hash, investigation_id FROM event_inbox WHERE source=? AND event_id=?",
                (event.source, event.event_id),
            ).fetchone()
            if duplicate:
                if duplicate["payload_hash"] != digest:
                    raise ConflictError(
                        "same source/event_id was delivered with a different payload"
                    )
                row = connection.execute(
                    "SELECT * FROM investigations WHERE investigation_id=?",
                    (duplicate["investigation_id"],),
                ).fetchone()
                return self._view(row), False

            incident = connection.execute(
                """SELECT o.investigation_id FROM investigation_observations o
                   JOIN investigations i ON i.investigation_id=o.investigation_id
                   WHERE o.incident_key=? AND i.tenant_id=? AND i.project_id=?
                     AND i.status NOT IN ('completed', 'failed', 'canceled')
                   ORDER BY o.created_at DESC LIMIT 1""",
                (event.data.incident_key, event.tenant_id, event.project_id),
            ).fetchone()
            created = incident is None
            if incident:
                investigation_id = incident["investigation_id"]
            else:
                investigation_id = f"inv-{uuid.uuid4().hex}"
                workflow_id = f"investigation/{investigation_id}"
                request = InvestigationRequest(
                    budget=budget or Budget(),
                    tenant_id=event.tenant_id,
                    project_id=event.project_id,
                    objective={
                        "objective_id": event.data.finding_id,
                        "description": (
                            f"调查 {event.data.kind} 异常（严重程度 {event.data.severity}），"
                            "验证现象并在证据边界内定位原因。"
                        ),
                        "priority": "critical" if event.data.severity == "critical" else "high",
                    },
                    build_ref=event.data.build,
                    constraints={
                        "anomaly_kind": event.data.kind,
                        "goal_type": "diagnosis",
                        "provider_allowlist": [event.data.run_ref.provider_id],
                        "environment_fingerprint": event.data.environment_fingerprint,
                        "source_run_ref": event.data.run_ref.model_dump(mode="json"),
                        "requested_policy_profile": event.data.requested_policy_profile,
                        **(
                            {
                                "action_parameters": {
                                    "test_ref": event.data.test_ref.model_dump(
                                        mode="json", exclude_none=True
                                    )
                                }
                            }
                            if event.data.test_ref is not None
                            else {}
                        ),
                        **(
                            {
                                "action_target_selector": event.data.target_selector.model_dump(
                                    mode="json", exclude_none=True
                                )
                            }
                            if event.data.target_selector is not None
                            else {}
                        ),
                    },
                    source="event",
                    root_event_id=event.root_event_id,
                )
                request_json = canonical_json(request)
                connection.execute(
                    """INSERT INTO investigations VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, 1, NULL, ?, ?)""",
                    (
                        investigation_id,
                        workflow_id,
                        event.tenant_id,
                        event.project_id,
                        f"event:{event.source}:{event.event_id}",
                        payload_hash(request),
                        request_json,
                        InvestigationStatus.ACCEPTED,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "INSERT INTO workflow_start_outbox(investigation_id, updated_at) VALUES (?, ?)",
                    (investigation_id, now),
                )
                connection.execute(
                    "INSERT INTO investigation_authorizations VALUES (?, ?, ?, ?, ?)",
                    (
                        investigation_id,
                        principal_id or f"event:{event.source}",
                        canonical_json(sorted(scopes)),
                        policy_profile,
                        now,
                    ),
                )
                self._insert_event(
                    connection,
                    investigation_id,
                    "investigation.accepted",
                    {"revision": 1, "status": "accepted", "root_event_id": event.root_event_id},
                )
            duplicate_finding = connection.execute(
                """SELECT 1 FROM investigation_observations o
                   JOIN investigations i ON i.investigation_id=o.investigation_id
                   WHERE i.tenant_id=? AND i.project_id=? AND o.finding_id=?
                     AND o.result_revision=?""",
                (
                    event.tenant_id,
                    event.project_id,
                    event.data.finding_id,
                    event.data.result_revision,
                ),
            ).fetchone()
            if duplicate_finding:
                raise ConflictError("finding/result revision already exists with another event")
            observation_event_key = f"{event.source}:{event.event_id}"
            try:
                connection.execute(
                    """INSERT INTO investigation_observations VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        observation_event_key,
                        event.data.finding_id,
                        event.data.incident_key,
                        investigation_id,
                        event.data.result_revision,
                        canonical_json(event.data.observation),
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ConflictError(
                    "finding/result revision already exists with another event"
                ) from error
            connection.execute(
                "INSERT INTO event_inbox VALUES (?, ?, ?, ?, ?, ?)",
                (event.source, event.event_id, digest, event_json, investigation_id, now),
            )
            self._insert_event(
                connection,
                investigation_id,
                "anomaly.observed",
                {
                    "event_id": event.event_id,
                    "finding_id": event.data.finding_id,
                    "result_revision": event.data.result_revision,
                },
            )
            from game_test_agent.storage.feedback import FeedbackStore
            FeedbackStore(self).bind(connection, investigation_id, event.source, event.data.run_ref,
                                     event.tenant_id, event.project_id)
            row = connection.execute(
                "SELECT * FROM investigations WHERE investigation_id=?", (investigation_id,)
            ).fetchone()
            return self._view(row), created

    def ingest_evidence_update(self, event: EvidenceUpdatedEvent) -> tuple[InvestigationView, bool]:
        digest = payload_hash(event)
        now = _now()
        with self.transaction() as connection:
            duplicate = connection.execute(
                "SELECT payload_hash, investigation_id FROM event_inbox WHERE source=? AND event_id=?",
                (event.source, event.event_id),
            ).fetchone()
            if duplicate:
                if duplicate["payload_hash"] != digest:
                    raise ConflictError(
                        "same source/event_id was delivered with a different payload"
                    )
                row = connection.execute(
                    "SELECT * FROM investigations WHERE investigation_id=?",
                    (duplicate["investigation_id"],),
                ).fetchone()
                return self._view(row), False
            row = connection.execute(
                "SELECT * FROM investigations WHERE investigation_id=?",
                (event.investigation_id,),
            ).fetchone()
            if not row:
                raise NotFoundError(event.investigation_id)
            if (row["tenant_id"], row["project_id"]) != (
                event.tenant_id,
                event.project_id,
            ):
                raise PermissionError("evidence update scope does not match investigation")
            connection.execute(
                "INSERT INTO event_inbox VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.source,
                    event.event_id,
                    digest,
                    canonical_json(event),
                    event.investigation_id,
                    now,
                ),
            )
            command_id = f"evidence:{event.source}:{event.event_id}"
            connection.execute(
                """INSERT INTO workflow_command_outbox(
                     command_id,investigation_id,kind,payload_json,state,created_at,updated_at
                   ) VALUES (?, ?, 'evidence', ?, 'pending', ?, ?)""",
                (
                    command_id,
                    event.investigation_id,
                    canonical_json(event.data),
                    now,
                    now,
                ),
            )
            self._insert_event(
                connection,
                event.investigation_id,
                "evidence.updated",
                {
                    "event_id": event.event_id,
                    "result_revision": event.data.result_revision,
                    "evidence_pending": event.data.evidence_pending,
                },
            )
            return self._view(row), True

    def create_investigation(
        self,
        request: InvestigationRequest,
        idempotency_key: str,
        *,
        principal_id: str = "system",
        scopes: set[str] | None = None,
        policy_profile: str = "bounded-diagnosis",
        conversation_root_id: str | None = None,
        _connection=None,
    ) -> tuple[InvestigationView, bool]:
        scopes = scopes or set()
        storage_key = f"{principal_id}:{idempotency_key}"
        request_json = canonical_json(request)
        request_digest = payload_hash(request)
        now = _now()
        from contextlib import nullcontext
        if _connection is not None and _connection is not self._connection:
            raise ValueError("foreign creation transaction")
        with (self.transaction() if _connection is None else nullcontext(_connection)) as connection:
            existing = connection.execute(
                "SELECT * FROM investigations WHERE tenant_id=? AND project_id=? AND idempotency_key=?",
                (request.tenant_id, request.project_id, storage_key),
            ).fetchone()
            if existing:
                if existing["request_hash"] != request_digest:
                    raise ConflictError("Idempotency-Key was already used with a different request")
                if conversation_root_id is not None:
                    link = connection.execute(
                        "SELECT conversation_id FROM conversation_task_links WHERE task_root_id=?",
                        (existing["investigation_id"],),
                    ).fetchone()
                    if not link or link["conversation_id"] != conversation_root_id:
                        raise ConflictError("request belongs to a different conversation")
                return self._view(existing), False
            if conversation_root_id is not None:
                from game_test_agent.storage.workbench import WorkbenchStore
                store = WorkbenchStore(self)
                summary = store.get(conversation_root_id, request.tenant_id, request.project_id)
                if summary["conversation_id"] != conversation_root_id:
                    raise ConflictError("use the canonical conversation identity")
                if summary["archived"]:
                    raise ConflictError("restore the conversation before creating tasks")
            investigation_id = f"inv-{uuid.uuid4().hex}"
            workflow_id = f"investigation/{investigation_id}"
            connection.execute(
                """INSERT INTO investigations VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, 1, NULL, ?, ?)""",
                (
                    investigation_id,
                    workflow_id,
                    request.tenant_id,
                    request.project_id,
                    storage_key,
                    request_digest,
                    request_json,
                    InvestigationStatus.ACCEPTED,
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO workflow_start_outbox(investigation_id, updated_at) VALUES (?, ?)",
                (investigation_id, now),
            )
            connection.execute(
                "INSERT INTO investigation_authorizations VALUES (?, ?, ?, ?, ?)",
                (
                    investigation_id,
                    principal_id,
                    canonical_json(sorted(scopes)),
                    policy_profile,
                    now,
                ),
            )
            self._insert_event(
                connection,
                investigation_id,
                "investigation.accepted",
                {"revision": 1, "status": "accepted"},
            )
            if conversation_root_id is not None:
                connection.execute(
                    "INSERT INTO conversation_task_links VALUES (?,?,?,?,?)",
                    (investigation_id, conversation_root_id, request.tenant_id, request.project_id, now),
                )
            row = connection.execute(
                "SELECT * FROM investigations WHERE investigation_id=?", (investigation_id,)
            ).fetchone()
            from game_test_agent.memory.recall import RecallService, requested_links
            for link in requested_links(request.constraints):
                if not {"runs:read", "evidence:read"}.issubset(scopes):
                    raise PermissionError("historical references require runs:read and evidence:read")
                RecallService(self).link_in_transaction(connection, self._view(row), link, wake=False)
            return self._view(row), True

    def reopen_investigation(
        self,
        parent_investigation_id: str,
        reopen: ReopenRequest,
        idempotency_key: str,
        *,
        principal_id: str = "system",
        scopes: set[str] | None = None,
        policy_profile: str = "bounded-diagnosis",
    ) -> tuple[InvestigationView, bool]:
        scopes = scopes or set()
        parent = self.get_investigation(parent_investigation_id)
        request = parent.request.model_copy(
            update={
                "objective": reopen.objective or parent.request.objective,
                "budget": reopen.budget,
                **(
                    {"build_ref": reopen.build_ref}
                    if "build_ref" in reopen.model_fields_set
                    else {}
                ),
                "constraints": {**parent.request.constraints, **reopen.constraints},
                "source": "user",
            }
        )
        scoped_key = f"{principal_id}:reopen:{parent_investigation_id}:{idempotency_key}"
        request_json = canonical_json(request)
        request_digest = payload_hash(
            {"request": request.model_dump(mode="json"), "reason": reopen.reason}
        )
        now = _now()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM investigations WHERE tenant_id=? AND project_id=? AND idempotency_key=?",
                (request.tenant_id, request.project_id, scoped_key),
            ).fetchone()
            if existing:
                if existing["request_hash"] != request_digest:
                    raise ConflictError(
                        "Idempotency-Key was already used for a different reopen request"
                    )
                return self._view(existing), False
            investigation_id = f"inv-{uuid.uuid4().hex}"
            workflow_id = f"investigation/{investigation_id}"
            connection.execute(
                """INSERT INTO investigations VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, 1, NULL, ?, ?)""",
                (
                    investigation_id,
                    workflow_id,
                    request.tenant_id,
                    request.project_id,
                    scoped_key,
                    request_digest,
                    request_json,
                    InvestigationStatus.ACCEPTED,
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO workflow_start_outbox(investigation_id, updated_at) VALUES (?, ?)",
                (investigation_id, now),
            )
            connection.execute(
                "INSERT INTO investigation_authorizations VALUES (?, ?, ?, ?, ?)",
                (
                    investigation_id,
                    principal_id,
                    canonical_json(sorted(scopes)),
                    policy_profile,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO investigation_relations VALUES (?, ?, 'supersedes', ?, ?)",
                (parent_investigation_id, investigation_id, reopen.reason, now),
            )
            self._insert_event(
                connection,
                investigation_id,
                "investigation.accepted",
                {
                    "revision": 1,
                    "status": "accepted",
                    "supersedes": parent_investigation_id,
                },
            )
            from game_test_agent.storage.feedback import FeedbackStore
            FeedbackStore(self).inherit_source_run(connection, parent_investigation_id, investigation_id, request)
            row = connection.execute(
                "SELECT * FROM investigations WHERE investigation_id=?", (investigation_id,)
            ).fetchone()
            from game_test_agent.memory.recall import RecallService, requested_links
            for link in requested_links(reopen.constraints):
                if not {"runs:read", "evidence:read"}.issubset(scopes):
                    raise PermissionError("historical references require runs:read and evidence:read")
                RecallService(self).link_in_transaction(connection, self._view(row), link, wake=False)
            reference = request.constraints.get("followup_reference")
            if (isinstance(reference, dict) and reference.get("investigation_id") == parent_investigation_id
                    and reference.get("report_sha256")):
                from game_test_agent.memory.recall import RecallLink, RecallService
                try:
                    RecallService(self).link_in_transaction(connection, self._view(row), RecallLink(
                        source_investigation_id=parent_investigation_id,
                        report_sha256=reference["report_sha256"]), wake=False)
                except (ValueError, LookupError, PermissionError, ConflictError):
                    self._insert_event(connection, investigation_id, "recall.followup.unavailable", {"source_investigation_id": parent_investigation_id})
            return self._view(row), True

    def get_authorization(self, investigation_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            """SELECT principal_id, scopes_json, policy_profile
               FROM investigation_authorizations WHERE investigation_id=?""",
            (investigation_id,),
        ).fetchone()
        if not row:
            raise NotFoundError(f"authorization for {investigation_id!r} not found")
        return {
            "principal_id": row["principal_id"],
            "scopes": json.loads(row["scopes_json"]),
            "policy_profile": row["policy_profile"],
        }

    def get_investigation(self, investigation_id: str) -> InvestigationView:
        row = self._connection.execute(
            "SELECT * FROM investigations WHERE investigation_id=?", (investigation_id,)
        ).fetchone()
        if not row:
            raise NotFoundError(f"investigation {investigation_id!r} not found")
        return self._view(row)

    def list_investigations(
        self, tenant_id: str, project_id: str, *, before: str | None = None, limit: int = 50
    ) -> list[InvestigationView]:
        """Read scoped history with a stable ID cursor; never scan other projects."""
        parameters: list[Any] = [tenant_id, project_id]
        predicate = "tenant_id=? AND project_id=?"
        if before:
            cursor = self.get_investigation(before)
            if (cursor.request.tenant_id, cursor.request.project_id) != (tenant_id, project_id):
                raise NotFoundError("history cursor not found in this project")
            predicate += " AND (created_at < ? OR (created_at = ? AND investigation_id < ?))"
            parameters.extend(
                [cursor.created_at.isoformat(), cursor.created_at.isoformat(), before]
            )
        parameters.append(min(max(limit, 1), 100))
        rows = self._connection.execute(
            f"SELECT * FROM investigations WHERE {predicate} "
            "ORDER BY created_at DESC, investigation_id DESC LIMIT ?",
            parameters,
        ).fetchall()
        return [self._view(row) for row in rows]

    def transition(
        self,
        investigation_id: str,
        status: InvestigationStatus,
        *,
        resolution: Resolution | None = None,
        stop_reason: str | None = None,
    ) -> InvestigationView:
        now = _now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM investigations WHERE investigation_id=?", (investigation_id,)
            ).fetchone()
            if not row:
                raise NotFoundError(investigation_id)
            current = InvestigationStatus(row["status"])
            if current == status:
                return self._view(row)
            allowed = _VALID_INVESTIGATION_TRANSITIONS.get(current, set())
            if status == InvestigationStatus.FAILED and current not in {
                InvestigationStatus.COMPLETED,
                InvestigationStatus.CANCELED,
                InvestigationStatus.FAILED,
            }:
                allowed = allowed | {InvestigationStatus.FAILED}
            if status not in allowed:
                raise ConflictError(f"invalid investigation transition: {current} -> {status}")
            revision = int(row["revision"]) + 1
            sequence = int(row["transition_seq"]) + 1
            connection.execute(
                """UPDATE investigations SET status=?, resolution=?, revision=?, transition_seq=?,
                   stop_reason=?, updated_at=? WHERE investigation_id=? AND transition_seq=?""",
                (
                    status,
                    resolution,
                    revision,
                    sequence,
                    stop_reason,
                    now,
                    investigation_id,
                    row["transition_seq"],
                ),
            )
            self._insert_event(
                connection,
                investigation_id,
                "investigation.updated",
                {
                    "revision": revision,
                    "transition_seq": sequence,
                    "status": status,
                    "resolution": resolution,
                },
            )
            updated = connection.execute(
                "SELECT * FROM investigations WHERE investigation_id=?", (investigation_id,)
            ).fetchone()
            return self._view(updated)

    def enqueue_command(
        self, investigation_id: str, command_id: str, kind: str, payload: Any
    ) -> bool:
        now = _now()
        with self.transaction() as connection:
            if not connection.execute(
                "SELECT 1 FROM investigations WHERE investigation_id=?", (investigation_id,)
            ).fetchone():
                raise NotFoundError(investigation_id)
            try:
                connection.execute(
                    """INSERT INTO workflow_command_outbox(
                         command_id,investigation_id,kind,payload_json,state,created_at,updated_at
                       ) VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
                    (command_id, investigation_id, kind, canonical_json(payload), now, now),
                )
            except sqlite3.IntegrityError:
                return False
            self._insert_event(
                connection, investigation_id, f"command.{kind}.accepted", {"command_id": command_id}
            )
            return True

    def list_events(
        self, investigation_id: str, after: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        self.get_investigation(investigation_id)
        rows = self._connection.execute(
            """SELECT cursor, event_type, payload_json, created_at FROM investigation_events
               WHERE investigation_id=? AND cursor>? ORDER BY cursor LIMIT ?""",
            (investigation_id, after, min(max(limit, 1), 500)),
        ).fetchall()
        return [
            {
                "cursor": row["cursor"],
                "event_type": row["event_type"],
                "data": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def list_recent_events(self, investigation_id: str, limit: int = 500) -> list[dict[str, Any]]:
        """Return the latest events for this investigation in chronological order."""
        self.get_investigation(investigation_id)
        rows = self._connection.execute(
            """SELECT cursor, event_type, payload_json, created_at FROM investigation_events
               WHERE investigation_id=? ORDER BY cursor DESC LIMIT ?""",
            (investigation_id, min(max(limit, 1), 500)),
        ).fetchall()
        return [
            {
                "cursor": row["cursor"],
                "event_type": row["event_type"],
                "data": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in reversed(rows)
        ]

    def list_recent_events_by_type(
        self, investigation_id: str, event_type: str, limit: int = 3
    ) -> list[dict[str, Any]]:
        self.get_investigation(investigation_id)
        rows = self._connection.execute(
            """SELECT cursor, event_type, payload_json, created_at FROM investigation_events
               WHERE investigation_id=? AND event_type=? ORDER BY cursor DESC LIMIT ?""",
            (investigation_id, event_type, min(max(limit, 1), 500)),
        ).fetchall()
        return [
            {
                "cursor": row["cursor"],
                "event_type": row["event_type"],
                "data": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in reversed(rows)
        ]

    def list_recent_events_by_types(
        self, investigation_id: str, event_types: list[str], limit: int = 5000
    ) -> list[dict[str, Any]]:
        if not event_types:
            return []
        self.get_investigation(investigation_id)
        placeholders = ','.join('?' for _ in event_types)
        rows = self._connection.execute(
            f"""SELECT cursor, event_type, payload_json, created_at FROM investigation_events
                WHERE investigation_id=? AND event_type IN ({placeholders})
                ORDER BY cursor DESC LIMIT ?""",
            (investigation_id, *event_types, min(max(limit, 1), 5000)),
        ).fetchall()
        return [{'cursor':r['cursor'], 'event_type':r['event_type'],
                 'data':json.loads(r['payload_json']), 'created_at':r['created_at']}
                for r in reversed(rows)]

    def latest_event_cursor(self, investigation_id: str) -> int:
        return self.event_cursor_range(investigation_id)[1]

    def event_cursor_range(self, investigation_id: str) -> tuple[int, int]:
        self.get_investigation(investigation_id)
        row = self._connection.execute(
            """SELECT COALESCE(MIN(cursor), 0), COALESCE(MAX(cursor), 0)
               FROM investigation_events
               WHERE investigation_id=?""",
            (investigation_id,),
        ).fetchone()
        return int(row[0]), int(row[1])

    def save_projection_checkpoint(self, checkpoint: ProjectionCheckpoint) -> ProjectionCheckpoint:
        self.get_investigation(checkpoint.investigation_id)
        latest = self.latest_event_cursor(checkpoint.investigation_id)
        if checkpoint.processed_cursor > latest:
            raise ValueError("projection checkpoint is ahead of the authoritative event ledger")
        with self.transaction() as connection:
            row = connection.execute(
                """SELECT processed_cursor FROM projection_checkpoints
                   WHERE investigation_id=? AND projection_name=?""",
                (checkpoint.investigation_id, checkpoint.projection_name),
            ).fetchone()
            if row and int(row["processed_cursor"]) > checkpoint.processed_cursor:
                raise ConflictError("projection checkpoint cannot move backwards")
            if row:
                connection.execute(
                    """UPDATE projection_checkpoints
                       SET processed_cursor=?,projection_hash=?,updated_at=?
                       WHERE investigation_id=? AND projection_name=?""",
                    (
                        checkpoint.processed_cursor,
                        checkpoint.projection_hash,
                        checkpoint.updated_at.isoformat(),
                        checkpoint.investigation_id,
                        checkpoint.projection_name,
                    ),
                )
            else:
                connection.execute(
                    """INSERT INTO projection_checkpoints(
                         investigation_id,projection_name,processed_cursor,
                         projection_hash,updated_at
                       ) VALUES (?,?,?,?,?)""",
                    (
                        checkpoint.investigation_id,
                        checkpoint.projection_name,
                        checkpoint.processed_cursor,
                        checkpoint.projection_hash,
                        checkpoint.updated_at.isoformat(),
                    ),
                )
        return checkpoint

    def get_projection_checkpoint(
        self, investigation_id: str, projection_name: str
    ) -> ProjectionCheckpoint | None:
        row = self._connection.execute(
            """SELECT investigation_id,projection_name,processed_cursor,
                      projection_hash,updated_at
               FROM projection_checkpoints
               WHERE investigation_id=? AND projection_name=?""",
            (investigation_id, projection_name),
        ).fetchone()
        return ProjectionCheckpoint.model_validate(dict(row)) if row else None

    def list_projection_checkpoints(self, investigation_id: str) -> list[ProjectionCheckpoint]:
        self.get_investigation(investigation_id)
        rows = self._connection.execute(
            """SELECT investigation_id,projection_name,processed_cursor,
                      projection_hash,updated_at
               FROM projection_checkpoints WHERE investigation_id=?
               ORDER BY projection_name""",
            (investigation_id,),
        ).fetchall()
        return [ProjectionCheckpoint.model_validate(dict(row)) for row in rows]

    def record_investigation_event(
        self, investigation_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        if not event_type or len(event_type) > 128:
            raise ValueError("event_type must be between 1 and 128 characters")
        with self.transaction() as connection:
            if not connection.execute(
                "SELECT 1 FROM investigations WHERE investigation_id=?",
                (investigation_id,),
            ).fetchone():
                raise NotFoundError(investigation_id)
            self._insert_event(connection, investigation_id, event_type, payload)

    def pending_workflow_starts(self, limit: int = 100) -> list[dict[str, str]]:
        now = _now()
        rows = self._connection.execute(
            """SELECT o.investigation_id, i.workflow_id FROM workflow_start_outbox o
               JOIN investigations i USING(investigation_id)
               WHERE o.state='pending' AND (o.next_attempt_at IS NULL OR o.next_attempt_at<=?)
               ORDER BY o.updated_at LIMIT ?""",
            (now, min(max(limit, 1), 500)),
        ).fetchall()
        return [dict(row) for row in rows]

    def recoverable_local_investigations(self) -> list[str]:
        return [row["investigation_id"] for row in self._connection.execute(
            """SELECT investigation_id FROM investigations
               WHERE status NOT IN ('completed','failed','canceled','waiting_input')
               ORDER BY created_at"""
        ).fetchall()]

    def mark_workflow_started(self, investigation_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                """UPDATE workflow_start_outbox
                   SET state='sent',attempts=attempts+1,updated_at=?,
                       last_error=NULL,next_attempt_at=NULL
                   WHERE investigation_id=? AND state='pending'""",
                (_now(), investigation_id),
            )

    def note_workflow_start_attempt(
        self, investigation_id: str, error: str = "delivery_failed"
    ) -> None:
        with self.transaction() as connection:
            self._record_outbox_failure(
                connection,
                "workflow_start_outbox",
                "investigation_id",
                investigation_id,
                error,
            )

    def pending_commands(self, limit: int = 100) -> list[dict[str, Any]]:
        now = _now()
        rows = self._connection.execute(
            """SELECT command_id, investigation_id, kind, payload_json, attempts
               FROM workflow_command_outbox
               WHERE state='pending' AND (next_attempt_at IS NULL OR next_attempt_at<=?)
               ORDER BY CASE WHEN kind='cancel' THEN 0 ELSE 1 END,created_at,command_id LIMIT ?""",
            (now, min(max(limit, 1), 500)),
        ).fetchall()
        return [
            {
                "command_id": row["command_id"],
                "investigation_id": row["investigation_id"],
                "kind": row["kind"],
                "payload": json.loads(row["payload_json"]),
                "attempts": int(row["attempts"]),
            }
            for row in rows
        ]

    def list_user_inputs(self, investigation_id: str, *, include_returned: bool = False) -> list[dict[str, Any]]:
        active = "" if include_returned else """ AND NOT EXISTS (SELECT 1 FROM task_input_returns returned
            WHERE returned.source_investigation_id=workflow_command_outbox.investigation_id
              AND returned.source_command_id=workflow_command_outbox.command_id)"""
        rows = self._connection.execute(
            """SELECT command_id, payload_json, created_at FROM workflow_command_outbox
               WHERE investigation_id=? AND kind='input'""" + active + " ORDER BY created_at, command_id",
            (investigation_id,),
        ).fetchall()
        return [
            {
                "command_id": row["command_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def mark_command_sent(self, command_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                """UPDATE workflow_command_outbox
                   SET state='sent',attempts=attempts+1,last_error=NULL,next_attempt_at=NULL,
                       updated_at=? WHERE command_id=? AND state='pending'""",
                (_now(), command_id),
            )

    def mark_command_obsolete(self, command_id: str, reason: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                """UPDATE workflow_command_outbox
                   SET state='obsolete',last_error=?,next_attempt_at=NULL,updated_at=?
                   WHERE command_id=? AND state='pending'""",
                (reason[:128], _now(), command_id),
            )

    def note_command_attempt(self, command_id: str, error: str) -> None:
        with self.transaction() as connection:
            self._record_outbox_failure(
                connection, "workflow_command_outbox", "command_id", command_id, error
            )

    def pending_events(self, limit: int = 100) -> list[dict[str, Any]]:
        now = _now()
        rows = self._connection.execute(
            """SELECT event_id, investigation_id, event_type, payload_json, payload_hash, attempts
               FROM event_outbox
               WHERE state='pending' AND (next_attempt_at IS NULL OR next_attempt_at<=?)
               ORDER BY created_at LIMIT ?""",
            (now, min(max(limit, 1), 500)),
        ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "investigation_id": row["investigation_id"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
                "payload_hash": row["payload_hash"],
                "attempts": int(row["attempts"]),
            }
            for row in rows
        ]

    def mark_event_published(self, event_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                """UPDATE event_outbox
                   SET state='published',attempts=attempts+1,last_error=NULL,next_attempt_at=NULL,
                       updated_at=? WHERE event_id=? AND state='pending'""",
                (_now(), event_id),
            )

    def note_event_publish_attempt(self, event_id: str, error: str) -> None:
        with self.transaction() as connection:
            self._record_outbox_failure(connection, "event_outbox", "event_id", event_id, error)

    @staticmethod
    def _record_outbox_failure(
        connection: sqlite3.Connection,
        table: str,
        identity_column: str,
        identity: str,
        error: str,
    ) -> None:
        allowed = {
            ("workflow_start_outbox", "investigation_id"),
            ("workflow_command_outbox", "command_id"),
            ("event_outbox", "event_id"),
        }
        if (table, identity_column) not in allowed:
            raise ValueError("unsupported outbox table")
        row = connection.execute(
            f"SELECT attempts FROM {table} WHERE {identity_column}=? AND state='pending'",
            (identity,),
        ).fetchone()
        if not row:
            return
        attempts = int(row["attempts"]) + 1
        now = datetime.now(timezone.utc)
        delay = min(2 ** min(attempts - 1, 8), 300)
        next_attempt = (now + timedelta(seconds=delay)).isoformat()
        connection.execute(
            f"""UPDATE {table}
                SET attempts=?,last_error=?,next_attempt_at=?,updated_at=?
                WHERE {identity_column}=? AND state='pending'""",
            (attempts, error[:128], next_attempt, now.isoformat(), identity),
        )

    def record_audit(
        self,
        *,
        principal_id: str,
        tenant_id: str,
        project_id: str,
        action: str,
        target_id: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        from game_test_agent.observability import redact

        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO audit_events(
                    principal_id,tenant_id,project_id,action,target_id,metadata_json,created_at
                ) VALUES (?,?,?,?,?,?,?)""",
                (
                    principal_id,
                    tenant_id,
                    project_id,
                    action,
                    target_id,
                    canonical_json(redact(metadata or {})),
                    _now(),
                ),
            )

    def list_audit_events(self, target_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """SELECT principal_id,tenant_id,project_id,action,target_id,metadata_json,created_at
               FROM audit_events WHERE target_id=? ORDER BY audit_id""",
            (target_id,),
        ).fetchall()
        return [
            {
                "principal_id": row["principal_id"],
                "tenant_id": row["tenant_id"],
                "project_id": row["project_id"],
                "action": row["action"],
                "target_id": row["target_id"],
                "metadata": json.loads(row["metadata_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def register_capability(self, descriptor: CapabilityDescriptor) -> None:
        value = canonical_json(descriptor)
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO capabilities VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(provider_id, capability_id, capability_version) DO UPDATE SET
                   descriptor_json=excluded.descriptor_json, descriptor_hash=excluded.descriptor_hash,
                   validated_at=excluded.validated_at""",
                (
                    descriptor.provider_id,
                    descriptor.capability_id,
                    descriptor.capability_version,
                    value,
                    payload_hash(descriptor),
                    _now(),
                ),
            )

    def list_capabilities(self, project_id: str) -> list[CapabilityDescriptor]:
        rows = self._connection.execute("SELECT descriptor_json FROM capabilities").fetchall()
        result = [CapabilityDescriptor.model_validate_json(row[0]) for row in rows]
        return [
            item
            for item in result
            if project_id in item.project_ids and item.availability != "unavailable"
        ]

    def reserve_action(
        self,
        action: ActionSpec,
        permit: ActionPermit,
        semantic_material: dict[str, Any],
        max_device_minutes: float,
        *,
        counter_name: str,
        counter_limit: int,
    ) -> tuple[ActionSpec, ActionPermit, bool]:
        with self.transaction() as connection:
            existing = connection.execute(
                """SELECT request_hash,permit_json,action_json FROM investigation_actions
                   WHERE idempotency_key=?""",
                (action.idempotency_key,),
            ).fetchone()
            if existing:
                stored_action = ActionSpec.model_validate_json(existing["action_json"])
                comparable = action.model_copy(
                    update={
                        "run_sequence": stored_action.run_sequence,
                        "repeat_index": stored_action.repeat_index,
                        "provider_revision": stored_action.provider_revision,
                    }
                )
                if existing["request_hash"] != payload_hash(comparable):
                    raise ConflictError("idempotency key conflicts with an existing action")
                return (
                    stored_action,
                    ActionPermit.model_validate_json(existing["permit_json"]),
                    False,
                )
            counter_value = connection.execute(
                """SELECT COUNT(*) FROM budget_counter_reservations
                   WHERE investigation_id=? AND counter_name=?""",
                (action.investigation_id, counter_name),
            ).fetchone()[0]
            if counter_value >= counter_limit:
                raise ConflictError(f"{counter_name} budget would be exceeded")
            used = connection.execute(
                """SELECT COALESCE(SUM(device_minutes), 0) FROM budget_reservations
                   WHERE investigation_id=? AND state IN ('reserved','settled')""",
                (action.investigation_id,),
            ).fetchone()[0]
            if used + permit.reserved_device_minutes > max_device_minutes:
                raise ConflictError("device-minute budget would be exceeded")
            if action.experiment_id:
                last_sequence = connection.execute(
                    """SELECT COALESCE(MAX(run_sequence),0) FROM experiment_runs
                       WHERE investigation_id=? AND experiment_id=?""",
                    (action.investigation_id, action.experiment_id),
                ).fetchone()[0]
                run_sequence = int(last_sequence) + 1
                action = action.model_copy(
                    update={
                        "run_sequence": run_sequence,
                        "repeat_index": run_sequence if run_sequence > 1 else None,
                    }
                )
            action_digest = payload_hash(action)
            permit = permit.model_copy(update={"request_hash": action_digest})
            semantic_material = dict(semantic_material)
            semantic_material["run_sequence"] = action.run_sequence
            semantic_hash = payload_hash(semantic_material)
            if connection.execute(
                "SELECT 1 FROM investigation_actions WHERE investigation_id=? AND semantic_hash=?",
                (action.investigation_id, semantic_hash),
            ).fetchone():
                raise ConflictError("semantically duplicate action requires a new experiment run")
            now = _now()
            connection.execute(
                "INSERT INTO budget_counter_reservations VALUES (?, ?, ?, ?)",
                (action.investigation_id, counter_name, action.action_id, now),
            )
            connection.execute(
                "INSERT INTO budget_reservations VALUES (?, ?, ?, 'reserved', ?)",
                (action.action_id, action.investigation_id, permit.reserved_device_minutes, now),
            )
            connection.execute(
                """INSERT INTO investigation_actions VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', NULL, ?, ?)""",
                (
                    action.action_id,
                    action.investigation_id,
                    action.idempotency_key,
                    semantic_hash,
                    action_digest,
                    canonical_json(action),
                    canonical_json(permit),
                    now,
                    now,
                ),
            )
            if action.experiment_id and action.run_sequence is not None:
                connection.execute(
                    """INSERT INTO experiment_runs(
                         investigation_id,experiment_id,run_sequence,action_id,created_at
                       ) VALUES (?,?,?,?,?)""",
                    (
                        action.investigation_id,
                        action.experiment_id,
                        action.run_sequence,
                        action.action_id,
                        now,
                    ),
                )
            return action, permit, True

    def _lock_investigation_row(self, connection, investigation_id: str) -> None:
        if not connection.execute("SELECT 1 FROM investigations WHERE investigation_id=?",
                                  (investigation_id,)).fetchone():
            raise NotFoundError(investigation_id)

    def investigation_execution_mode(self, investigation_id: str, default_mode: str) -> str:
        if default_mode not in {"native_mcp", "typed"}:
            raise ValueError("unknown investigation execution mode")
        with self.transaction() as connection:
            self._lock_investigation_row(connection, investigation_id)
            row = connection.execute(
                "SELECT payload_json FROM investigation_events WHERE investigation_id=? AND event_type=? ORDER BY cursor DESC LIMIT 1",
                (investigation_id, "investigation.runtime_selected"),
            ).fetchone()
            if row:
                return json.loads(row[0])["mode"]
            native_history = connection.execute(
                "SELECT 1 FROM investigation_events WHERE investigation_id=? AND event_type=? LIMIT 1",
                (investigation_id, "mcp.native.checkpoint"),
            ).fetchone()
            typed_history = connection.execute(
                "SELECT 1 FROM investigation_actions WHERE investigation_id=? LIMIT 1",
                (investigation_id,),
            ).fetchone()
            mode = "native_mcp" if native_history else "typed" if typed_history else default_mode
            self._insert_event(connection, investigation_id, "investigation.runtime_selected",
                               {"mode": mode, "version": 1})
            return mode

    def investigation_execution_backend(self, investigation_id: str, default_backend: str) -> str:
        if default_backend not in {"local", "temporal"}:
            raise ValueError("unknown investigation workflow backend")
        with self.transaction() as connection:
            self._lock_investigation_row(connection, investigation_id)
            row = connection.execute("SELECT payload_json FROM investigation_events WHERE investigation_id=? AND event_type=? ORDER BY cursor ASC LIMIT 1",
                (investigation_id, "investigation.backend_selected")).fetchone()
            if row is not None:
                backend = json.loads(row[0])["backend"]
                if backend not in {"local", "temporal"}:
                    raise ValueError("invalid persisted workflow backend")
                return backend
            self._insert_event(connection, investigation_id, "investigation.backend_selected", {"backend": default_backend})
            return default_backend

    def native_model_usage(self, investigation_id: str) -> dict:
        """An interrupted/unsettled attempt retains its full reservation."""
        rows = self.list_recent_events_by_types(investigation_id,
            ["mcp.native.model_reserved", "mcp.native.model_settled", "mcp.native.model_completed"], limit=5000)
        turns = {}
        for row in rows:
            value = row["data"]
            if row["event_type"] == "mcp.native.model_completed":
                turns.setdefault(value["turn"], {
                    "tokens": (value.get("input_tokens", 0) or 0) + (value.get("output_tokens", 0) or 0),
                    "estimated_cost": value.get("estimated_cost", 0) or 0,
                })
            else:
                turns[value["turn"]] = value
        return {"tokens": sum(v["tokens"] for v in turns.values()),
                "estimated_cost": sum(v["estimated_cost"] for v in turns.values()),
                "attempts": len(turns)}

    def reserve_native_model_call(self, investigation_id: str, turn: int, *,
                                  tokens: int, estimated_cost: float,
                                  token_limit: int, cost_limit: float | None,
                                  round_limit: int, report_reserve_tokens: int = 0,
                                  report_reserve_cost: float = 0.0) -> None:
        if report_reserve_tokens < 0 or report_reserve_cost < 0:
            raise ValueError("report reservation must be nonnegative")
        with self.transaction() as connection:
            self._lock_investigation_row(connection, investigation_id)
            usage = self.native_model_usage(investigation_id)
            if usage["tokens"] + tokens + report_reserve_tokens > token_limit:
                raise ConflictError("native_model_token_budget_exhausted")
            if cost_limit is not None and usage["estimated_cost"] + estimated_cost + report_reserve_cost > cost_limit:
                raise ConflictError("native_model_cost_budget_exhausted")
            count = connection.execute(
                "SELECT COUNT(*) FROM budget_counter_reservations WHERE investigation_id=? AND counter_name=?",
                (investigation_id, "native_model_turns"),
            ).fetchone()[0]
            if count >= round_limit:
                raise ConflictError("native_round_budget_exhausted")
            reservation_id = f"native-turn:{turn}"
            if connection.execute(
                "SELECT 1 FROM budget_counter_reservations WHERE investigation_id=? AND counter_name=? AND reservation_id=?",
                (investigation_id, "native_model_turns", reservation_id),
            ).fetchone():
                raise ConflictError("native_model_turn_already_reserved")
            connection.execute("INSERT INTO budget_counter_reservations VALUES (?, ?, ?, ?)",
                               (investigation_id, "native_model_turns", reservation_id, _now()))
            self._insert_event(connection, investigation_id, "mcp.native.model_reserved",
                               {"turn": turn, "tokens": tokens, "estimated_cost": estimated_cost,
                                "report_reserve_tokens": report_reserve_tokens,
                                "report_reserve_cost": report_reserve_cost,
                                "accounting": "conservative_reservation"})

    def reserve_native_read(self, investigation_id: str, call_id: str, identity_hash: str, limit: int) -> None:
        """Freeze call identity before dispatch; a reused ID cannot bypass read limits."""
        with self.transaction() as connection:
            self._lock_investigation_row(connection, investigation_id)
            reservation = f"native-call:{call_id}"
            if connection.execute(
                "SELECT 1 FROM budget_counter_reservations WHERE investigation_id=? AND counter_name=? AND reservation_id=?",
                (investigation_id, "tool_reads", reservation),
            ).fetchone():
                raise ConflictError("CALL_ID_REUSED")
            count = connection.execute(
                "SELECT COUNT(*) FROM budget_counter_reservations WHERE investigation_id=? AND counter_name=?",
                (investigation_id, "tool_reads"),
            ).fetchone()[0]
            if count >= limit:
                raise ConflictError("READ_BUDGET_EXHAUSTED")
            connection.execute("INSERT INTO budget_counter_reservations VALUES (?, ?, ?, ?)",
                               (investigation_id, "tool_reads", reservation, _now()))
            self._insert_event(connection, investigation_id, "mcp.native.call_started",
                               {"call_id": call_id, "identity_hash": identity_hash})

    def reserve_counter(
        self,
        investigation_id: str,
        counter_name: str,
        reservation_id: str,
        limit: int,
    ) -> bool:
        with self.transaction() as connection:
            if connection.execute(
                """SELECT 1 FROM budget_counter_reservations
                   WHERE investigation_id=? AND counter_name=? AND reservation_id=?""",
                (investigation_id, counter_name, reservation_id),
            ).fetchone():
                return False
            count = connection.execute(
                """SELECT COUNT(*) FROM budget_counter_reservations
                   WHERE investigation_id=? AND counter_name=?""",
                (investigation_id, counter_name),
            ).fetchone()[0]
            if count >= limit:
                raise ConflictError(f"{counter_name} budget would be exceeded")
            connection.execute(
                "INSERT INTO budget_counter_reservations VALUES (?, ?, ?, ?)",
                (investigation_id, counter_name, reservation_id, _now()),
            )
            return True

    def get_action(self, action_id: str) -> tuple[ActionSpec, ActionState, dict[str, Any] | None]:
        row = self._connection.execute(
            "SELECT action_json, state, operation_ref_json FROM investigation_actions WHERE action_id=?",
            (action_id,),
        ).fetchone()
        if not row:
            raise NotFoundError(f"action {action_id!r} not found")
        operation = json.loads(row["operation_ref_json"]) if row["operation_ref_json"] else None
        return (
            ActionSpec.model_validate_json(row["action_json"]),
            ActionState(row["state"]),
            operation,
        )

    def list_actions(self, investigation_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """SELECT action_json, state, operation_ref_json FROM investigation_actions
               WHERE investigation_id=? ORDER BY created_at""",
            (investigation_id,),
        ).fetchall()
        return [
            {
                "action": json.loads(row["action_json"]),
                "state": row["state"],
                "operation_ref": (
                    json.loads(row["operation_ref_json"]) if row["operation_ref_json"] else None
                ),
            }
            for row in rows
        ]

    def budget_usage(self, investigation_id: str) -> dict[str, float | int]:
        row = self._connection.execute(
            """SELECT COUNT(*) AS action_count,
                      COALESCE(SUM(device_minutes), 0) AS reserved_device_minutes,
                      COALESCE(SUM(CASE WHEN state='settled' THEN device_minutes ELSE 0 END), 0)
                        AS settled_device_minutes
               FROM budget_reservations WHERE investigation_id=?""",
            (investigation_id,),
        ).fetchone()
        counters = self._connection.execute(
            """SELECT counter_name, COUNT(*) AS value FROM budget_counter_reservations
               WHERE investigation_id=? GROUP BY counter_name""",
            (investigation_id,),
        ).fetchall()
        result: dict[str, float | int] = {
            "action_count": int(row["action_count"]),
            "reserved_device_minutes": float(row["reserved_device_minutes"]),
            "settled_device_minutes": float(row["settled_device_minutes"]),
        }
        result.update({counter["counter_name"]: int(counter["value"]) for counter in counters})
        return result

    def transition_action(
        self,
        action_id: str,
        expected: set[ActionState],
        target: ActionState,
        *,
        operation_ref: Any | None = None,
    ) -> bool:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT investigation_id, state FROM investigation_actions WHERE action_id=?",
                (action_id,),
            ).fetchone()
            if not row:
                raise NotFoundError(action_id)
            current = ActionState(row["state"])
            if current == target:
                return False
            if current not in expected:
                raise ConflictError(f"invalid action transition: {current} -> {target}")
            connection.execute(
                """UPDATE investigation_actions SET state=?, operation_ref_json=COALESCE(?, operation_ref_json),
                   updated_at=? WHERE action_id=? AND state=?""",
                (
                    target,
                    canonical_json(operation_ref) if operation_ref is not None else None,
                    _now(),
                    action_id,
                    current,
                ),
            )
            self._insert_event(
                connection,
                row["investigation_id"],
                "action.updated",
                {"action_id": action_id, "from": current, "to": target},
            )
            if target in {ActionState.SUCCEEDED, ActionState.FAILED, ActionState.CANCELED}:
                connection.execute(
                    "UPDATE budget_reservations SET state=? WHERE action_id=? AND state='reserved'",
                    ("settled", action_id),
                )
            return True

    def record_cleanup_required(self, investigation_id: str, action_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO cleanup_operations(
                     action_id,investigation_id,state,attempts,last_error,updated_at
                   ) VALUES (?,?,'pending',0,NULL,?)
                   ON CONFLICT(action_id) DO UPDATE SET
                     state=CASE WHEN cleanup_operations.state='resolved' THEN 'resolved' ELSE 'pending' END,
                     updated_at=excluded.updated_at""",
                (action_id, investigation_id, _now()),
            )

    def pending_cleanup_actions(self, investigation_id: str) -> list[str]:
        rows = self._connection.execute(
            """SELECT action_id FROM cleanup_operations
               WHERE investigation_id=? AND state='pending' AND resource_kind='action'
               ORDER BY updated_at""",
            (investigation_id,),
        ).fetchall()
        return [row["action_id"] for row in rows]

    def record_resource_cleanup(
        self,
        investigation_id: str,
        resource_id: str,
        *,
        resource_kind: str,
        cleanup_owner: str,
    ) -> None:
        if resource_kind not in {"sandbox", "device_lease", "temporary_account", "directory", "mcp_operation"}:
            raise ValueError("unsupported cleanup resource kind")
        if not cleanup_owner:
            raise ValueError("cleanup owner is required")
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT resource_kind,cleanup_owner FROM cleanup_operations WHERE action_id=?",
                (resource_id,),
            ).fetchone()
            if existing:
                if (
                    existing["resource_kind"] != resource_kind
                    or existing["cleanup_owner"] != cleanup_owner
                ):
                    raise ConflictError("cleanup resource identity was reused")
                connection.execute(
                    """UPDATE cleanup_operations SET
                         state=CASE WHEN state='resolved' THEN 'resolved' ELSE 'pending' END,
                         updated_at=? WHERE action_id=?""",
                    (_now(), resource_id),
                )
                return
            connection.execute(
                """INSERT INTO cleanup_operations(
                     action_id,investigation_id,resource_kind,cleanup_owner,state,
                     attempts,last_error,updated_at
                   ) VALUES (?,?,?,?,'pending',0,NULL,?)""",
                (resource_id, investigation_id, resource_kind, cleanup_owner, _now()),
            )

    def pending_cleanup_resources(self, investigation_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """SELECT action_id,resource_kind,cleanup_owner,attempts,last_error,updated_at
               FROM cleanup_operations WHERE investigation_id=? AND state='pending'
               ORDER BY updated_at,action_id""",
            (investigation_id,),
        ).fetchall()
        return [
            {
                "resource_id": row["action_id"],
                "resource_kind": row["resource_kind"],
                "cleanup_owner": row["cleanup_owner"],
                "attempts": int(row["attempts"]),
                "last_error": row["last_error"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def note_cleanup_attempt(self, action_id: str, error: str | None = None) -> None:
        with self.transaction() as connection:
            connection.execute(
                """UPDATE cleanup_operations SET attempts=attempts+1,last_error=?,updated_at=?
                   WHERE action_id=? AND state='pending'""",
                (error, _now(), action_id),
            )

    def resolve_cleanup(self, action_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                """UPDATE cleanup_operations SET state='resolved',updated_at=?
                   WHERE action_id=?""",
                (_now(), action_id),
            )

    def save_reasoning_step(self, step: ReasoningStep) -> tuple[ReasoningStep, bool]:
        with self.transaction() as connection:
            existing = connection.execute(
                """SELECT context_hash, result_json FROM reasoning_steps
                   WHERE investigation_id=? AND plan_revision=? AND reasoning_step_id=?""",
                (step.investigation_id, step.plan_revision, step.reasoning_step_id),
            ).fetchone()
            if existing:
                if existing["context_hash"] != step.context_hash:
                    raise ConflictError(
                        "reasoning step identity was reused with a different context"
                    )
                return ReasoningStep.model_validate_json(existing["result_json"]), False
            connection.execute(
                "INSERT INTO reasoning_steps VALUES (?, ?, ?, ?, ?, ?)",
                (
                    step.investigation_id,
                    step.plan_revision,
                    step.reasoning_step_id,
                    step.context_hash,
                    canonical_json(step),
                    _now(),
                ),
            )
            return step, True

    def save_plan(self, plan: TestPlan, capability_snapshot_hash: str) -> str:
        value = canonical_json(plan)
        digest = hashlib.sha256(value.encode()).hexdigest()
        with self.transaction() as connection:
            if not connection.execute(
                "SELECT 1 FROM investigations WHERE investigation_id=?",
                (plan.investigation_id,),
            ).fetchone():
                raise NotFoundError(plan.investigation_id)
            existing = connection.execute(
                """SELECT plan_hash FROM investigation_plans
                   WHERE investigation_id=? AND revision=?""",
                (plan.investigation_id, plan.revision),
            ).fetchone()
            if existing:
                if existing["plan_hash"] != digest:
                    raise ConflictError("plan revisions are immutable")
                return digest
            connection.execute(
                "INSERT INTO investigation_plans VALUES (?, ?, ?, ?, ?, ?)",
                (
                    plan.investigation_id,
                    plan.revision,
                    capability_snapshot_hash,
                    value,
                    digest,
                    _now(),
                ),
            )
        return digest

    def get_plan(self, investigation_id: str, revision: int) -> TestPlan:
        row = self._connection.execute(
            """SELECT plan_json FROM investigation_plans
               WHERE investigation_id=? AND revision=?""",
            (investigation_id, revision),
        ).fetchone()
        if not row:
            raise NotFoundError(f"plan {investigation_id}/{revision} not found")
        return TestPlan.model_validate_json(row[0])

    def get_latest_plan(self, investigation_id: str) -> TestPlan | None:
        row = self._connection.execute(
            """SELECT plan_json FROM investigation_plans
               WHERE investigation_id=? ORDER BY revision DESC LIMIT 1""",
            (investigation_id,),
        ).fetchone()
        return TestPlan.model_validate_json(row[0]) if row else None

    def get_reasoning_step(
        self, investigation_id: str, plan_revision: int, reasoning_step_id: str
    ) -> ReasoningStep | None:
        row = self._connection.execute(
            """SELECT result_json FROM reasoning_steps
               WHERE investigation_id=? AND plan_revision=? AND reasoning_step_id=?""",
            (investigation_id, plan_revision, reasoning_step_id),
        ).fetchone()
        return ReasoningStep.model_validate_json(row[0]) if row else None

    def model_usage(self, investigation_id: str) -> dict[str, float | int]:
        self.get_investigation(investigation_id)
        reasoning_rows = self._connection.execute(
            """SELECT result_json FROM reasoning_steps WHERE investigation_id=?""",
            (investigation_id,),
        ).fetchall()
        planning_rows = self._connection.execute(
            """SELECT planned_json FROM planned_actions WHERE investigation_id=?""",
            (investigation_id,),
        ).fetchall()
        usages: list[dict[str, Any]] = [
            ReasoningStep.model_validate_json(row[0]).usage for row in reasoning_rows
        ]
        usages.extend(PlannedAction.model_validate_json(row[0]).usage for row in planning_rows)
        return {
            "model_calls": sum(1 for item in usages if item),
            "attempt_count": sum(int(item.get("attempt_count", 0) or 0) for item in usages),
            "input_tokens": sum(int(item.get("input_tokens", 0) or 0) for item in usages),
            "output_tokens": sum(int(item.get("output_tokens", 0) or 0) for item in usages),
            "latency_ms": sum(float(item.get("latency_ms", 0) or 0) for item in usages),
            "estimated_cost": sum(float(item.get("estimated_cost", 0) or 0) for item in usages),
            "cache_hits": sum(bool(item.get("cache_hit", False)) for item in usages),
        }

    def save_context_snapshot(self, snapshot: ContextSnapshot) -> tuple[ContextSnapshot, bool]:
        if payload_hash(snapshot.context) != snapshot.context_hash:
            raise ValueError("context snapshot hash does not match its payload")
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT snapshot_json,context_hash FROM context_snapshots WHERE snapshot_id=?",
                (snapshot.snapshot_id,),
            ).fetchone()
            if existing:
                stored = ContextSnapshot.model_validate_json(existing["snapshot_json"])
                if stored.model_dump(exclude={"created_at"}) != snapshot.model_dump(
                    exclude={"created_at"}
                ):
                    raise ConflictError("context snapshot identity was reused")
                return stored, False
            by_round = connection.execute(
                """SELECT snapshot_json,context_hash FROM context_snapshots
                   WHERE investigation_id=? AND plan_revision=?""",
                (snapshot.investigation_id, snapshot.plan_revision),
            ).fetchone()
            if by_round:
                stored = ContextSnapshot.model_validate_json(by_round["snapshot_json"])
                if stored.snapshot_id != snapshot.snapshot_id:
                    if (
                        stored.authoritative_state_hash is None
                        and stored.state_revision == snapshot.state_revision
                    ):
                        return stored, False
                    raise ConflictError("plan revision already has a different context snapshot")
                return stored, False
            connection.execute(
                """INSERT INTO context_snapshots(
                     snapshot_id,investigation_id,state_revision,plan_revision,
                     context_hash,snapshot_json,created_at
                   ) VALUES (?,?,?,?,?,?,?)""",
                (
                    snapshot.snapshot_id,
                    snapshot.investigation_id,
                    snapshot.state_revision,
                    snapshot.plan_revision,
                    snapshot.context_hash,
                    canonical_json(snapshot),
                    _now(),
                ),
            )
            return snapshot, True

    def get_context_snapshot(self, snapshot_id: str) -> ContextSnapshot:
        row = self._connection.execute(
            "SELECT snapshot_json FROM context_snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if not row:
            raise NotFoundError(f"context snapshot {snapshot_id!r} not found")
        return ContextSnapshot.model_validate_json(row[0])

    def list_context_snapshots(self, investigation_id: str) -> list[ContextSnapshot]:
        self.get_investigation(investigation_id)
        rows = self._connection.execute(
            """SELECT snapshot_json FROM context_snapshots
               WHERE investigation_id=? ORDER BY plan_revision""",
            (investigation_id,),
        ).fetchall()
        return [ContextSnapshot.model_validate_json(row[0]) for row in rows]

    def save_decision_record(self, record: DecisionRecord) -> tuple[DecisionRecord, bool]:
        if payload_hash(record.decision) != record.decision_hash:
            raise ValueError("decision hash does not match its payload")
        with self.transaction() as connection:
            snapshot = connection.execute(
                """SELECT investigation_id,state_revision FROM context_snapshots
                   WHERE snapshot_id=?""",
                (record.context_snapshot_id,),
            ).fetchone()
            if not snapshot:
                raise NotFoundError(record.context_snapshot_id)
            if (
                snapshot["investigation_id"] != record.investigation_id
                or int(snapshot["state_revision"]) != record.expected_state_revision
            ):
                raise ConflictError("decision does not match its frozen context")
            existing = connection.execute(
                """SELECT decision_json,decision_hash FROM decision_records
                   WHERE investigation_id=? AND plan_revision=?""",
                (record.investigation_id, record.plan_revision),
            ).fetchone()
            if existing:
                stored = DecisionRecord.model_validate_json(existing["decision_json"])
                if (
                    existing["decision_hash"] != record.decision_hash
                    or stored.context_snapshot_id != record.context_snapshot_id
                    or stored.expected_state_revision != record.expected_state_revision
                ):
                    raise ConflictError("plan revision already has a different decision")
                return stored, False
            connection.execute(
                """INSERT INTO decision_records(
                     decision_id,investigation_id,plan_revision,context_snapshot_id,
                     expected_state_revision,decision_hash,decision_json,status,created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    record.decision_id,
                    record.investigation_id,
                    record.plan_revision,
                    record.context_snapshot_id,
                    record.expected_state_revision,
                    record.decision_hash,
                    canonical_json(record),
                    record.status,
                    _now(),
                ),
            )
            return record, True

    def list_decision_records(self, investigation_id: str) -> list[DecisionRecord]:
        self.get_investigation(investigation_id)
        rows = self._connection.execute(
            """SELECT decision_json FROM decision_records
               WHERE investigation_id=? ORDER BY plan_revision""",
            (investigation_id,),
        ).fetchall()
        return [DecisionRecord.model_validate_json(row[0]) for row in rows]

    def get_decision_record(
        self, investigation_id: str, plan_revision: int
    ) -> DecisionRecord | None:
        row = self._connection.execute(
            """SELECT decision_json FROM decision_records
               WHERE investigation_id=? AND plan_revision=?""",
            (investigation_id, plan_revision),
        ).fetchone()
        return DecisionRecord.model_validate_json(row[0]) if row else None

    def _append_decision_outcome(
        self,
        connection,
        decision_id: str,
        investigation_id: str,
        *,
        chosen_action_id: str | None = None,
        outcome_ref: str | None = None,
        termination_basis: str | None = None,
    ) -> tuple[DecisionOutcomeRecord, bool]:
        row = connection.execute(
            """SELECT outcome_json FROM decision_outcomes
               WHERE decision_id=? ORDER BY revision DESC LIMIT 1""",
            (decision_id,),
        ).fetchone()
        previous = DecisionOutcomeRecord.model_validate_json(row[0]) if row else None
        action_id = chosen_action_id or (previous.chosen_action_id if previous else None)
        refs = list(previous.observed_outcome_refs if previous else [])
        if outcome_ref is not None and outcome_ref not in refs:
            refs.append(outcome_ref)
        basis = termination_basis or (previous.termination_basis if previous else None)
        if previous and (
            action_id,
            refs,
            basis,
        ) == (
            previous.chosen_action_id,
            previous.observed_outcome_refs,
            previous.termination_basis,
        ):
            return previous, False
        record = DecisionOutcomeRecord(
            decision_id=decision_id,
            investigation_id=investigation_id,
            revision=(previous.revision + 1 if previous else 1),
            chosen_action_id=action_id,
            observed_outcome_refs=refs,
            termination_basis=basis,
        )
        connection.execute(
            """INSERT INTO decision_outcomes(
                 decision_id,revision,investigation_id,outcome_json,outcome_hash,created_at
               ) VALUES (?,?,?,?,?,?)""",
            (
                record.decision_id,
                record.revision,
                record.investigation_id,
                canonical_json(record),
                payload_hash(record),
                record.created_at.isoformat(),
            ),
        )
        return record, True

    def record_decision_outcome(
        self,
        decision_id: str,
        *,
        chosen_action_id: str | None = None,
        outcome_ref: str | None = None,
        termination_basis: str | None = None,
    ) -> tuple[DecisionOutcomeRecord, bool]:
        with self.transaction() as connection:
            decision = connection.execute(
                """SELECT investigation_id FROM decision_records WHERE decision_id=?""",
                (decision_id,),
            ).fetchone()
            if not decision:
                raise NotFoundError(decision_id)
            return self._append_decision_outcome(
                connection,
                decision_id,
                decision["investigation_id"],
                chosen_action_id=chosen_action_id,
                outcome_ref=outcome_ref,
                termination_basis=termination_basis,
            )

    def list_decision_outcomes(self, investigation_id: str) -> list[DecisionOutcomeRecord]:
        self.get_investigation(investigation_id)
        rows = self._connection.execute(
            """SELECT o.outcome_json FROM decision_outcomes o
               JOIN decision_records d ON d.decision_id=o.decision_id
               WHERE o.investigation_id=? ORDER BY d.plan_revision,o.revision""",
            (investigation_id,),
        ).fetchall()
        return [DecisionOutcomeRecord.model_validate_json(row[0]) for row in rows]

    def latest_decision_outcome(self, decision_id: str) -> DecisionOutcomeRecord | None:
        row = self._connection.execute(
            """SELECT outcome_json FROM decision_outcomes
               WHERE decision_id=? ORDER BY revision DESC LIMIT 1""",
            (decision_id,),
        ).fetchone()
        return DecisionOutcomeRecord.model_validate_json(row[0]) if row else None

    def decision_outcome_integrity_issues(self, investigation_id: str) -> list[str]:
        rows = self._connection.execute(
            """SELECT decision_id,revision,outcome_json,outcome_hash
               FROM decision_outcomes WHERE investigation_id=?
               ORDER BY decision_id,revision""",
            (investigation_id,),
        ).fetchall()
        issues: list[str] = []
        expected_revision: dict[str, int] = {}
        for row in rows:
            decision_id = row["decision_id"]
            revision = int(row["revision"])
            expected = expected_revision.get(decision_id, 1)
            if revision != expected:
                issues.append(
                    f"decision outcome {decision_id} revision gap: expected {expected}, got {revision}"
                )
            expected_revision[decision_id] = revision + 1
            record = DecisionOutcomeRecord.model_validate_json(row["outcome_json"])
            if payload_hash(record) != row["outcome_hash"]:
                issues.append(f"decision outcome {decision_id}/{revision} failed its hash")
        return issues

    def save_planned_action(self, planned: PlannedAction) -> tuple[PlannedAction, bool]:
        if payload_hash(planned.proposal) != planned.proposal_hash:
            raise ValueError("planned proposal hash does not match its payload")
        if payload_hash(planned.action) != planned.action_hash:
            raise ValueError("planned action hash does not match its payload")
        if planned.action.investigation_id != planned.investigation_id:
            raise ValueError("planned action belongs to another investigation")
        if planned.action.plan_revision != planned.plan_revision:
            raise ValueError("planned action revision does not match its planning record")
        with self.transaction() as connection:
            decision = connection.execute(
                """SELECT investigation_id,plan_revision FROM decision_records
                   WHERE decision_id=?""",
                (planned.decision_id,),
            ).fetchone()
            if not decision:
                raise NotFoundError(planned.decision_id)
            if (
                decision["investigation_id"] != planned.investigation_id
                or int(decision["plan_revision"]) != planned.plan_revision
            ):
                raise ConflictError("planned action does not match its decision")
            existing = connection.execute(
                """SELECT planned_json,proposal_hash,action_hash FROM planned_actions
                   WHERE investigation_id=? AND plan_revision=?""",
                (planned.investigation_id, planned.plan_revision),
            ).fetchone()
            if existing:
                if (
                    existing["proposal_hash"] != planned.proposal_hash
                    or existing["action_hash"] != planned.action_hash
                ):
                    raise ConflictError("plan revision already has a different planned action")
                return PlannedAction.model_validate_json(existing["planned_json"]), False
            connection.execute(
                """INSERT INTO planned_actions(
                     planning_id,investigation_id,plan_revision,decision_id,
                     proposal_hash,action_hash,planned_json,created_at
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    planned.planning_id,
                    planned.investigation_id,
                    planned.plan_revision,
                    planned.decision_id,
                    planned.proposal_hash,
                    planned.action_hash,
                    canonical_json(planned),
                    _now(),
                ),
            )
            self._append_decision_outcome(
                connection,
                planned.decision_id,
                planned.investigation_id,
                chosen_action_id=planned.action.action_id,
            )
            return planned, True

    def get_planned_action(self, investigation_id: str, plan_revision: int) -> PlannedAction | None:
        row = self._connection.execute(
            """SELECT planned_json FROM planned_actions
               WHERE investigation_id=? AND plan_revision=?""",
            (investigation_id, plan_revision),
        ).fetchone()
        return PlannedAction.model_validate_json(row[0]) if row else None

    def save_context_summary(self, summary: ContextSummary) -> tuple[ContextSummary, bool]:
        material = summary.model_dump(mode="json", exclude={"summary_hash", "created_at"})
        if payload_hash(material) != summary.summary_hash:
            raise ValueError("context summary hash does not match its payload")
        self.get_investigation(summary.investigation_id)
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT summary_json,summary_hash FROM context_summaries WHERE summary_id=?",
                (summary.summary_id,),
            ).fetchone()
            if existing:
                stored = ContextSummary.model_validate_json(existing["summary_json"])
                if stored.model_dump(exclude={"created_at"}) != summary.model_dump(
                    exclude={"created_at"}
                ):
                    raise ConflictError("context summary identity was reused")
                return stored, False
            by_source = connection.execute(
                """SELECT summary_json,summary_hash FROM context_summaries
                   WHERE investigation_id=? AND source_hash=? AND generator_version=?""",
                (
                    summary.investigation_id,
                    summary.source_hash,
                    summary.generator_version,
                ),
            ).fetchone()
            if by_source:
                stored = ContextSummary.model_validate_json(by_source["summary_json"])
                if stored.summary_hash != summary.summary_hash:
                    raise ConflictError("summary source has different generated content")
                return stored, False
            connection.execute(
                """INSERT INTO context_summaries(
                     summary_id,investigation_id,source_event_cursor_end,source_hash,
                     generator_version,summary_hash,summary_json,created_at
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    summary.summary_id,
                    summary.investigation_id,
                    summary.source_event_cursor_end,
                    summary.source_hash,
                    summary.generator_version,
                    summary.summary_hash,
                    canonical_json(summary),
                    _now(),
                ),
            )
            return summary, True

    def get_latest_context_summary(self, investigation_id: str) -> ContextSummary | None:
        row = self._connection.execute(
            """SELECT summary_json FROM context_summaries
               WHERE investigation_id=?
               ORDER BY source_event_cursor_end DESC,created_at DESC LIMIT 1""",
            (investigation_id,),
        ).fetchone()
        return ContextSummary.model_validate_json(row[0]) if row else None

    def list_context_summaries(self, investigation_id: str) -> list[ContextSummary]:
        rows = self._connection.execute(
            """SELECT summary_json FROM context_summaries
               WHERE investigation_id=? ORDER BY source_event_cursor_end,created_at""",
            (investigation_id,),
        ).fetchall()
        return [ContextSummary.model_validate_json(row[0]) for row in rows]

    def save_case_memory(
        self,
        record: CaseMemoryRecord,
        *,
        expected_previous_revision: int | None,
    ) -> tuple[CaseMemoryRecord, bool]:
        """Persist an immutable case revision with optimistic concurrency."""

        view = self.get_investigation(record.source_investigation_id)
        if (view.request.tenant_id, view.request.project_id) != (
            record.tenant_id,
            record.project_id,
        ):
            raise ValueError("case memory scope does not match its source investigation")
        value = canonical_json(record)
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        with self.transaction() as connection:
            latest_row = connection.execute(
                """SELECT revision,record_json,record_hash FROM case_memory_records
                   WHERE case_id=? ORDER BY revision DESC LIMIT 1""",
                (record.case_id,),
            ).fetchone()
            if latest_row and int(latest_row["revision"]) == record.revision:
                if latest_row["record_hash"] != digest:
                    stored = CaseMemoryRecord.model_validate_json(latest_row["record_json"])
                    concurrent_candidate = (
                        record.revision == 1
                        and stored.status == record.status == CaseMemoryStatus.CANDIDATE
                        and stored.model_dump(exclude={"created_by", "created_at", "updated_at"})
                        == record.model_dump(exclude={"created_by", "created_at", "updated_at"})
                    )
                    if not concurrent_candidate:
                        raise ConflictError("case memory revision is immutable")
                    return stored, False
                return CaseMemoryRecord.model_validate_json(latest_row["record_json"]), False

            latest_revision = int(latest_row["revision"]) if latest_row else 0
            if expected_previous_revision != latest_revision:
                raise ConflictError(
                    f"case memory expected revision {latest_revision}, "
                    f"got {expected_previous_revision}"
                )
            if record.revision != latest_revision + 1:
                raise ConflictError("case memory revisions must be consecutive")
            if latest_row:
                previous = CaseMemoryRecord.model_validate_json(latest_row["record_json"])
                immutable_previous = previous.model_dump(
                    exclude={
                        "revision",
                        "status",
                        "confidence",
                        "reviewed_by",
                        "review_notes",
                        "state_reason",
                        "state_changed_by",
                        "updated_at",
                    }
                )
                immutable_current = record.model_dump(
                    exclude={
                        "revision",
                        "status",
                        "confidence",
                        "reviewed_by",
                        "review_notes",
                        "state_reason",
                        "state_changed_by",
                        "updated_at",
                    }
                )
                if immutable_previous != immutable_current:
                    raise ConflictError("case memory provenance and content are immutable")
                if record.status not in _VALID_CASE_MEMORY_TRANSITIONS[previous.status]:
                    raise ConflictError("invalid case memory status transition")
                if previous.reviewed_by is not None and record.reviewed_by != previous.reviewed_by:
                    raise ConflictError("case memory reviewer attribution is immutable")
            connection.execute(
                """INSERT INTO case_memory_records(
                     case_id,revision,tenant_id,project_id,status,source_investigation_id,
                     source_report_revision,record_json,record_hash,created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    record.case_id,
                    record.revision,
                    record.tenant_id,
                    record.project_id,
                    record.status,
                    record.source_investigation_id,
                    record.source_report_revision,
                    value,
                    digest,
                    _now(),
                ),
            )
            return record, True

    def get_case_memory(self, case_id: str) -> CaseMemoryRecord:
        row = self._connection.execute(
            """SELECT record_json FROM case_memory_records
               WHERE case_id=? ORDER BY revision DESC LIMIT 1""",
            (case_id,),
        ).fetchone()
        if not row:
            raise NotFoundError(f"case memory {case_id!r} not found")
        return CaseMemoryRecord.model_validate_json(row[0])

    def list_case_memory_revisions(self, case_id: str) -> list[CaseMemoryRecord]:
        rows = self._connection.execute(
            """SELECT record_json FROM case_memory_records
               WHERE case_id=? ORDER BY revision""",
            (case_id,),
        ).fetchall()
        return [CaseMemoryRecord.model_validate_json(row[0]) for row in rows]

    def list_current_case_memories(
        self,
        tenant_id: str,
        project_id: str,
        *,
        statuses: set[CaseMemoryStatus] | None = None,
    ) -> list[CaseMemoryRecord]:
        rows = self._connection.execute(
            """SELECT c.record_json FROM case_memory_records c
               WHERE c.tenant_id=? AND c.project_id=?
                 AND c.revision=(
                   SELECT MAX(newer.revision) FROM case_memory_records newer
                   WHERE newer.case_id=c.case_id
                 )
               ORDER BY c.case_id""",
            (tenant_id, project_id),
        ).fetchall()
        records = [CaseMemoryRecord.model_validate_json(row[0]) for row in rows]
        if statuses is not None:
            records = [item for item in records if item.status in statuses]
        return records

    def list_current_case_memories_for_source(
        self, source_investigation_id: str
    ) -> list[CaseMemoryRecord]:
        rows = self._connection.execute(
            """SELECT c.record_json FROM case_memory_records c
               WHERE c.source_investigation_id=?
                 AND c.revision=(
                   SELECT MAX(newer.revision) FROM case_memory_records newer
                   WHERE newer.case_id=c.case_id
                 )
               ORDER BY c.case_id""",
            (source_investigation_id,),
        ).fetchall()
        return [CaseMemoryRecord.model_validate_json(row[0]) for row in rows]

    def reserve_analysis_job(
        self, record: AnalysisJobRecord, *, max_jobs_per_investigation: int
    ) -> tuple[AnalysisJobRecord, bool]:
        if max_jobs_per_investigation < 1:
            raise ValueError("analysis job limit must be positive")
        if record.state != AnalysisJobState.PROPOSED or record.operation_ref is not None:
            raise ValueError("new analysis jobs must be proposed without an operation")
        if payload_hash(record.spec) != record.spec_hash:
            raise ValueError("analysis job spec hash mismatch")
        view = self.get_investigation(record.spec.investigation_id)
        if (view.request.tenant_id, view.request.project_id) != (
            record.spec.tenant_id,
            record.spec.project_id,
        ):
            raise ValueError("analysis job scope does not match its investigation")
        with self.transaction() as connection:
            existing = connection.execute(
                """SELECT spec_hash,record_json FROM analysis_jobs
                   WHERE idempotency_key=? OR job_id=? LIMIT 1""",
                (record.spec.idempotency_key, record.spec.job_id),
            ).fetchone()
            if existing:
                if existing["spec_hash"] != record.spec_hash:
                    raise ConflictError("analysis job identity conflicts with another spec")
                return AnalysisJobRecord.model_validate_json(existing["record_json"]), False
            count = connection.execute(
                "SELECT COUNT(*) FROM analysis_jobs WHERE investigation_id=?",
                (record.spec.investigation_id,),
            ).fetchone()[0]
            if int(count) >= max_jobs_per_investigation:
                raise ConflictError("analysis job budget would be exceeded")
            now = _now()
            connection.execute(
                """INSERT INTO analysis_jobs(
                     job_id,idempotency_key,tenant_id,project_id,investigation_id,state,
                     spec_hash,record_json,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    record.spec.job_id,
                    record.spec.idempotency_key,
                    record.spec.tenant_id,
                    record.spec.project_id,
                    record.spec.investigation_id,
                    record.state,
                    record.spec_hash,
                    canonical_json(record),
                    now,
                    now,
                ),
            )
            self._insert_event(
                connection,
                record.spec.investigation_id,
                "analysis.proposed",
                {"job_id": record.spec.job_id, "spec_hash": record.spec_hash},
            )
            return record, True

    def get_analysis_job(self, job_id: str) -> AnalysisJobRecord:
        row = self._connection.execute(
            "SELECT record_json FROM analysis_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if not row:
            raise NotFoundError(f"analysis job {job_id!r} not found")
        return AnalysisJobRecord.model_validate_json(row[0])

    def list_analysis_jobs(self, investigation_id: str) -> list[AnalysisJobRecord]:
        rows = self._connection.execute(
            """SELECT record_json FROM analysis_jobs
               WHERE investigation_id=? ORDER BY created_at,job_id""",
            (investigation_id,),
        ).fetchall()
        return [AnalysisJobRecord.model_validate_json(row[0]) for row in rows]

    def unfinished_analysis_jobs(self, runner_id: str) -> list[AnalysisJobRecord]:
        """Internal runner recovery; never expose cross-project records as context."""
        rows = self._connection.execute(
            """SELECT record_json FROM analysis_jobs
               WHERE state IN ('proposed','submitted','running','unknown') ORDER BY created_at,job_id"""
        ).fetchall()
        records = [AnalysisJobRecord.model_validate_json(row[0]) for row in rows]
        return [record for record in records if record.spec.environment.runner_id == runner_id]

    def transition_analysis_job(
        self,
        job_id: str,
        target: AnalysisJobState,
        *,
        operation_ref: SandboxOperationRef | None = None,
    ) -> AnalysisJobRecord:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT record_json FROM analysis_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if not row:
                raise NotFoundError(f"analysis job {job_id!r} not found")
            current = AnalysisJobRecord.model_validate_json(row[0])
            if current.state == target:
                if (
                    operation_ref is not None
                    and current.operation_ref is not None
                    and _sandbox_operation_identity(current.operation_ref)
                    != _sandbox_operation_identity(operation_ref)
                ):
                    raise ConflictError("analysis operation identity changed")
                return current
            if target not in _VALID_ANALYSIS_JOB_TRANSITIONS[current.state]:
                raise ConflictError(
                    f"analysis job cannot transition from {current.state} to {target}"
                )
            selected_operation = operation_ref or current.operation_ref
            if target != AnalysisJobState.CANCELED and selected_operation is None:
                raise ValueError("submitted analysis state requires an operation reference")
            if current.operation_ref is not None:
                if selected_operation is None or _sandbox_operation_identity(
                    selected_operation
                ) != _sandbox_operation_identity(current.operation_ref):
                    raise ConflictError("analysis operation identity is immutable")
            updated = current.model_copy(
                update={
                    "state": target,
                    "operation_ref": selected_operation,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            connection.execute(
                """UPDATE analysis_jobs SET state=?,record_json=?,updated_at=?
                   WHERE job_id=?""",
                (target, canonical_json(updated), _now(), job_id),
            )
            self._insert_event(
                connection,
                current.spec.investigation_id,
                "analysis.updated",
                {"job_id": job_id, "from": current.state, "to": target},
            )
            return updated

    def save_analysis_result(
        self, snapshot: AnalysisResultSnapshot
    ) -> tuple[AnalysisResultSnapshot, bool]:
        if payload_hash(snapshot.result) != snapshot.result_hash:
            raise ValueError("analysis result hash mismatch")
        job = self.get_analysis_job(snapshot.result.job_id)
        if job.spec.investigation_id != snapshot.investigation_id:
            raise ValueError("analysis result investigation mismatch")
        target = AnalysisJobState(snapshot.result.execution_status)
        with self.transaction() as connection:
            existing = connection.execute(
                """SELECT result_hash,snapshot_json FROM analysis_result_snapshots
                   WHERE job_id=? AND result_revision=?""",
                (snapshot.result.job_id, snapshot.result.result_revision),
            ).fetchone()
            if existing:
                if existing["result_hash"] != snapshot.result_hash:
                    raise ConflictError("analysis result revision is immutable")
                return AnalysisResultSnapshot.model_validate_json(existing["snapshot_json"]), False
            latest_revision = connection.execute(
                """SELECT COALESCE(MAX(result_revision),0) FROM analysis_result_snapshots
                   WHERE job_id=?""",
                (snapshot.result.job_id,),
            ).fetchone()[0]
            if snapshot.result.result_revision != int(latest_revision) + 1:
                raise ConflictError("analysis result revisions must be consecutive")
            current_row = connection.execute(
                "SELECT record_json FROM analysis_jobs WHERE job_id=?",
                (snapshot.result.job_id,),
            ).fetchone()
            current = AnalysisJobRecord.model_validate_json(current_row["record_json"])
            if target not in _VALID_ANALYSIS_JOB_TRANSITIONS[current.state]:
                raise ConflictError("analysis result is inconsistent with job state")
            updated_job = current.model_copy(
                update={"state": target, "updated_at": datetime.now(timezone.utc)}
            )
            now = _now()
            connection.execute(
                """INSERT INTO analysis_result_snapshots(
                     job_id,result_revision,investigation_id,result_hash,snapshot_json,
                     ingestion_state,ingestion_error,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    snapshot.result.job_id,
                    snapshot.result.result_revision,
                    snapshot.investigation_id,
                    snapshot.result_hash,
                    canonical_json(snapshot),
                    snapshot.ingestion_state,
                    snapshot.ingestion_error,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE analysis_jobs SET state=?,record_json=?,updated_at=? WHERE job_id=?",
                (target, canonical_json(updated_job), now, snapshot.result.job_id),
            )
            self._insert_event(
                connection,
                snapshot.investigation_id,
                "analysis.result_recorded",
                {
                    "job_id": snapshot.result.job_id,
                    "result_revision": snapshot.result.result_revision,
                    "status": snapshot.result.execution_status,
                },
            )
            return snapshot, True

    def get_latest_analysis_result(self, job_id: str) -> AnalysisResultSnapshot | None:
        row = self._connection.execute(
            """SELECT snapshot_json FROM analysis_result_snapshots
               WHERE job_id=? ORDER BY result_revision DESC LIMIT 1""",
            (job_id,),
        ).fetchone()
        return AnalysisResultSnapshot.model_validate_json(row[0]) if row else None

    def mark_analysis_result_ingestion(
        self,
        job_id: str,
        result_revision: int,
        state: str,
        error: str | None = None,
    ) -> None:
        if state not in {"pending", "ingested", "failed"}:
            raise ValueError("invalid analysis ingestion state")
        with self.transaction() as connection:
            row = connection.execute(
                """SELECT snapshot_json FROM analysis_result_snapshots
                   WHERE job_id=? AND result_revision=?""",
                (job_id, result_revision),
            ).fetchone()
            if not row:
                raise NotFoundError(f"analysis result {job_id!r}/{result_revision}")
            snapshot = AnalysisResultSnapshot.model_validate_json(row[0]).model_copy(
                update={
                    "ingestion_state": state,
                    "ingestion_error": error,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            connection.execute(
                """UPDATE analysis_result_snapshots SET snapshot_json=?,ingestion_state=?,
                   ingestion_error=?,updated_at=? WHERE job_id=? AND result_revision=?""",
                (
                    canonical_json(snapshot),
                    state,
                    error,
                    _now(),
                    job_id,
                    result_revision,
                ),
            )

    def save_derived_fact_candidate(
        self,
        candidate: DerivedFactCandidate,
        *,
        expected_previous_revision: int,
        promoted_fact: Fact | None = None,
    ) -> tuple[DerivedFactCandidate, bool]:
        if candidate.status == "verified" and promoted_fact is None:
            raise ValueError("verified candidates require atomic fact promotion")
        if promoted_fact is not None and (
            candidate.status != "verified"
            or promoted_fact.fact_id != f"fact-{candidate.candidate_id}"
            or promoted_fact.investigation_id != candidate.investigation_id
            or promoted_fact.statement != candidate.statement
            or promoted_fact.evidence_ids != candidate.evidence_ids
            or promoted_fact.data != candidate.data
            or promoted_fact.extractor != "verified-sandbox-analysis"
            or promoted_fact.extractor_version != candidate.verifier
        ):
            raise ValueError("promoted fact must match the verified candidate")
        value = canonical_json(candidate)
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        with self.transaction() as connection:
            latest = connection.execute(
                """SELECT revision,candidate_hash,candidate_json
                   FROM derived_fact_candidates WHERE candidate_id=?
                   ORDER BY revision DESC LIMIT 1""",
                (candidate.candidate_id,),
            ).fetchone()
            if latest and int(latest["revision"]) == candidate.revision:
                if latest["candidate_hash"] != digest:
                    stored = DerivedFactCandidate.model_validate_json(latest["candidate_json"])
                    if stored.model_dump(
                        exclude={"created_at", "updated_at"}
                    ) != candidate.model_dump(exclude={"created_at", "updated_at"}):
                        raise ConflictError("derived fact candidate revision is immutable")
                    return stored, False
                return DerivedFactCandidate.model_validate_json(latest["candidate_json"]), False
            latest_revision = int(latest["revision"]) if latest else 0
            if latest_revision != expected_previous_revision:
                raise ConflictError(f"derived fact expected revision {latest_revision}")
            if candidate.revision != latest_revision + 1:
                raise ConflictError("derived fact revisions must be consecutive")
            if latest:
                previous = DerivedFactCandidate.model_validate_json(latest["candidate_json"])
                if previous.status != "candidate" or candidate.status not in {
                    "verified",
                    "rejected",
                }:
                    raise ConflictError("derived fact has an invalid status transition")
                if previous.model_dump(
                    exclude={
                        "revision",
                        "status",
                        "verifier",
                        "verification_reason",
                        "updated_at",
                    }
                ) != candidate.model_dump(
                    exclude={
                        "revision",
                        "status",
                        "verifier",
                        "verification_reason",
                        "updated_at",
                    }
                ):
                    raise ConflictError("derived fact source and content are immutable")
            connection.execute(
                """INSERT INTO derived_fact_candidates(
                     candidate_id,revision,investigation_id,analysis_job_id,status,
                     candidate_hash,candidate_json,created_at
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    candidate.candidate_id,
                    candidate.revision,
                    candidate.investigation_id,
                    candidate.analysis_job_id,
                    candidate.status,
                    digest,
                    value,
                    _now(),
                ),
            )
            if promoted_fact is not None:
                connection.execute(
                    "INSERT INTO facts VALUES (?, ?, ?, ?)",
                    (
                        promoted_fact.fact_id,
                        promoted_fact.investigation_id,
                        canonical_json(promoted_fact),
                        _now(),
                    ),
                )
                self._insert_event(
                    connection,
                    promoted_fact.investigation_id,
                    "fact.extracted",
                    {
                        "fact_id": promoted_fact.fact_id,
                        "evidence_ids": promoted_fact.evidence_ids,
                        "extractor": promoted_fact.extractor,
                    },
                )
            return candidate, True

    def get_derived_fact_candidate(self, candidate_id: str) -> DerivedFactCandidate:
        row = self._connection.execute(
            """SELECT candidate_json FROM derived_fact_candidates
               WHERE candidate_id=? ORDER BY revision DESC LIMIT 1""",
            (candidate_id,),
        ).fetchone()
        if not row:
            raise NotFoundError(f"derived fact candidate {candidate_id!r} not found")
        return DerivedFactCandidate.model_validate_json(row[0])

    def list_derived_fact_candidates(self, investigation_id: str) -> list[DerivedFactCandidate]:
        rows = self._connection.execute(
            """SELECT c.candidate_json FROM derived_fact_candidates c
               WHERE c.investigation_id=? AND c.revision=(
                 SELECT MAX(newer.revision) FROM derived_fact_candidates newer
                 WHERE newer.candidate_id=c.candidate_id
               ) ORDER BY c.candidate_id""",
            (investigation_id,),
        ).fetchall()
        return [DerivedFactCandidate.model_validate_json(row[0]) for row in rows]

    def add_evidence(self, investigation_id: str, evidence: EvidenceRecord) -> IngestOutcome:
        run_ref = evidence.run_ref
        provider_id = run_ref.provider_id if run_ref else ""
        run_id = run_ref.run_id if run_ref else ""
        if evidence.source_type == "analysis":
            provider_id = "analysis-sandbox"
            run_id = evidence.analysis_job_id or ""
        source_evidence_id = evidence.source_evidence_id or evidence.evidence_id
        with self.transaction() as connection:
            existing_source = connection.execute(
                """SELECT evidence_id,content_hash,investigation_id FROM evidence_records
                   WHERE tenant_id=? AND project_id=? AND source_type=?
                     AND provider_id=? AND run_id=? AND source_evidence_id=?
                     AND source_revision=?""",
                (
                    evidence.tenant_id,
                    evidence.project_id,
                    evidence.source_type,
                    provider_id,
                    run_id,
                    source_evidence_id,
                    evidence.source_revision,
                ),
            ).fetchone()
            if existing_source:
                if existing_source["investigation_id"] != investigation_id:
                    return IngestOutcome(
                        status="rejected",
                        reason="source observation belongs to another investigation",
                    )
                if existing_source["content_hash"] == evidence.sha256.lower():
                    return IngestOutcome(
                        status="replayed",
                        canonical_evidence_id=existing_source["evidence_id"],
                    )
                return IngestOutcome(
                    status="conflict",
                    canonical_evidence_id=existing_source["evidence_id"],
                    reason="source identity was reused with different content",
                )
            existing_id = connection.execute(
                "SELECT content_hash FROM evidence_records WHERE evidence_id=?",
                (evidence.evidence_id,),
            ).fetchone()
            if existing_id:
                return IngestOutcome(
                    status="conflict",
                    canonical_evidence_id=evidence.evidence_id,
                    reason="canonical evidence ID belongs to another source",
                )
            connection.execute(
                """INSERT INTO evidence_records(
                     evidence_id,investigation_id,tenant_id,project_id,source_type,
                     provider_id,run_id,source_evidence_id,source_revision,evidence_json,
                     content_hash,created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    evidence.evidence_id,
                    investigation_id,
                    evidence.tenant_id,
                    evidence.project_id,
                    evidence.source_type,
                    provider_id,
                    run_id,
                    source_evidence_id,
                    evidence.source_revision,
                    canonical_json(evidence),
                    evidence.sha256.lower(),
                    _now(),
                ),
            )
            self._insert_event(
                connection,
                investigation_id,
                "evidence.ingested",
                {"evidence_id": evidence.evidence_id},
            )
            return IngestOutcome(status="created", canonical_evidence_id=evidence.evidence_id)

    def get_evidence(self, evidence_id: str) -> EvidenceRecord:
        row = self._connection.execute(
            "SELECT evidence_json FROM evidence_records WHERE evidence_id=?", (evidence_id,)
        ).fetchone()
        if not row:
            raise NotFoundError(f"evidence {evidence_id!r} not found")
        return EvidenceRecord.model_validate_json(row[0])

    def list_evidence(self, investigation_id: str) -> list[EvidenceRecord]:
        rows = self._connection.execute(
            "SELECT evidence_json FROM evidence_records WHERE investigation_id=? ORDER BY created_at",
            (investigation_id,),
        ).fetchall()
        return [EvidenceRecord.model_validate_json(row[0]) for row in rows]

    def superseded_evidence_ids(self, investigation_id: str) -> set[str]:
        rows = self._connection.execute(
            """SELECT payload_json FROM investigation_events
               WHERE investigation_id=? AND event_type='provenance.source_superseded'
               ORDER BY cursor""",
            (investigation_id,),
        ).fetchall()
        superseded: set[str] = set()
        for row in rows:
            payload = json.loads(row[0])
            superseded.update(payload.get("superseded_evidence_ids", []))
        return superseded

    def list_current_evidence(self, investigation_id: str) -> list[EvidenceRecord]:
        evidence = self.list_evidence(investigation_id)
        stale = set(self.superseded_evidence_ids(investigation_id))
        changed = True
        while changed:
            changed = False
            for item in evidence:
                if item.evidence_id not in stale and stale.intersection(item.derived_from):
                    stale.add(item.evidence_id)
                    changed = True
        return [item for item in evidence if item.evidence_id not in stale]

    def save_assessment(self, assessment: CriterionAssessment) -> tuple[CriterionAssessment, bool]:
        digest = payload_hash(assessment)
        with self.transaction() as connection:
            existing = connection.execute(
                """SELECT assessment_json,assessment_hash FROM criterion_assessments
                   WHERE investigation_id=? AND criterion_id=?
                     AND evaluated_state_revision=?""",
                (
                    assessment.investigation_id,
                    assessment.criterion_id,
                    assessment.evaluated_state_revision,
                ),
            ).fetchone()
            if existing:
                if existing["assessment_hash"] != digest:
                    raise ConflictError("assessment revision was reused with different content")
                return CriterionAssessment.model_validate_json(existing["assessment_json"]), False
            connection.execute(
                """INSERT INTO criterion_assessments VALUES (?,?,?,?,?,?)""",
                (
                    assessment.investigation_id,
                    assessment.criterion_id,
                    assessment.evaluated_state_revision,
                    canonical_json(assessment),
                    digest,
                    _now(),
                ),
            )
            self._insert_event(
                connection,
                assessment.investigation_id,
                "objective.assessed",
                {
                    "criterion_id": assessment.criterion_id,
                    "verdict": assessment.verdict,
                    "scope": assessment.scope,
                    "evaluated_state_revision": assessment.evaluated_state_revision,
                },
            )
            return assessment, True

    def latest_assessment(self, investigation_id: str) -> CriterionAssessment | None:
        row = self._connection.execute(
            """SELECT assessment_json FROM criterion_assessments
               WHERE investigation_id=?
               ORDER BY evaluated_state_revision DESC,created_at DESC LIMIT 1""",
            (investigation_id,),
        ).fetchone()
        return CriterionAssessment.model_validate_json(row[0]) if row else None

    def list_assessments(self, investigation_id: str) -> list[CriterionAssessment]:
        rows = self._connection.execute(
            """SELECT assessment_json FROM criterion_assessments
               WHERE investigation_id=? ORDER BY evaluated_state_revision,created_at""",
            (investigation_id,),
        ).fetchall()
        return [CriterionAssessment.model_validate_json(row[0]) for row in rows]

    def record_run_result(
        self, action_id: str, result: RunResult
    ) -> tuple[RunResultSnapshot, bool]:
        digest = payload_hash(result)
        now = _now()
        with self.transaction() as connection:
            action_row = connection.execute(
                """SELECT investigation_id,action_json,state,operation_ref_json
                   FROM investigation_actions WHERE action_id=?""",
                (action_id,),
            ).fetchone()
            if not action_row:
                raise NotFoundError(action_id)
            action = ActionSpec.model_validate_json(action_row["action_json"])
            if (
                result.run_ref.provider_id != action.provider_id
                or result.run_ref.project_id != action.project_id
            ):
                raise ConflictError("run result scope does not match its action")
            operation = (
                json.loads(action_row["operation_ref_json"])
                if action_row["operation_ref_json"]
                else None
            )
            if operation and operation.get("run_ref"):
                if operation["run_ref"] != result.run_ref.model_dump(mode="json"):
                    raise ConflictError("run result identity does not match accepted operation")
            existing = connection.execute(
                """SELECT * FROM run_result_snapshots
                   WHERE tenant_id=? AND provider_id=? AND project_id=?
                     AND run_id=? AND result_revision=?""",
                (
                    action.tenant_id,
                    result.run_ref.provider_id,
                    result.run_ref.project_id,
                    result.run_ref.run_id,
                    result.revision,
                ),
            ).fetchone()
            if existing:
                if existing["result_hash"] != digest or existing["action_id"] != action_id:
                    raise ConflictError("run result revision was reused with different content")
                return self._run_result_snapshot(existing), False
            connection.execute(
                """INSERT INTO run_result_snapshots(
                     tenant_id,provider_id,project_id,run_id,result_revision,
                     action_id,investigation_id,
                     result_json,result_hash,ingestion_state,ingestion_error,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,'pending',NULL,?,?)""",
                (
                    action.tenant_id,
                    result.run_ref.provider_id,
                    result.run_ref.project_id,
                    result.run_ref.run_id,
                    result.revision,
                    action_id,
                    action.investigation_id,
                    canonical_json(result),
                    digest,
                    now,
                    now,
                ),
            )
            target = {
                "queued": ActionState.WAITING,
                "running": ActionState.WAITING,
                "succeeded": ActionState.SUCCEEDED,
                "failed": ActionState.FAILED,
                "canceled": ActionState.CANCELED,
                "unknown": ActionState.UNKNOWN,
            }[result.execution_status]
            current = ActionState(action_row["state"])
            if current != target:
                connection.execute(
                    "UPDATE investigation_actions SET state=?,updated_at=? WHERE action_id=?",
                    (target, now, action_id),
                )
                self._insert_event(
                    connection,
                    action.investigation_id,
                    "action.updated",
                    {"action_id": action_id, "from": current, "to": target},
                )
            if target in {ActionState.SUCCEEDED, ActionState.FAILED, ActionState.CANCELED}:
                connection.execute(
                    "UPDATE budget_reservations SET state='settled' "
                    "WHERE action_id=? AND state='reserved'",
                    (action_id,),
                )
            self._insert_event(
                connection,
                action.investigation_id,
                "run_result.recorded",
                {
                    "action_id": action_id,
                    "run_ref": result.run_ref.model_dump(mode="json"),
                    "result_revision": result.revision,
                },
            )
            decision = connection.execute(
                """SELECT decision_id FROM decision_records
                   WHERE investigation_id=? AND plan_revision=?""",
                (action.investigation_id, action.plan_revision),
            ).fetchone()
            if decision:
                self._append_decision_outcome(
                    connection,
                    decision["decision_id"],
                    action.investigation_id,
                    chosen_action_id=action_id,
                    outcome_ref=(
                        f"run://{result.run_ref.provider_id}/{result.run_ref.project_id}/"
                        f"{result.run_ref.run_id}@{result.revision}"
                    ),
                )
            row = connection.execute(
                """SELECT * FROM run_result_snapshots
                   WHERE tenant_id=? AND provider_id=? AND project_id=?
                     AND run_id=? AND result_revision=?""",
                (
                    action.tenant_id,
                    result.run_ref.provider_id,
                    result.run_ref.project_id,
                    result.run_ref.run_id,
                    result.revision,
                ),
            ).fetchone()
            return self._run_result_snapshot(row), True

    @staticmethod
    def _run_result_snapshot(row: Any) -> RunResultSnapshot:
        return RunResultSnapshot(
            tenant_id=row["tenant_id"],
            action_id=row["action_id"],
            investigation_id=row["investigation_id"],
            result=RunResult.model_validate_json(row["result_json"]),
            result_hash=row["result_hash"],
            ingestion_state=row["ingestion_state"],
            ingestion_error=row["ingestion_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_latest_run_result(self, action_id: str) -> RunResultSnapshot | None:
        row = self._connection.execute(
            """SELECT * FROM run_result_snapshots WHERE action_id=?
               ORDER BY result_revision DESC LIMIT 1""",
            (action_id,),
        ).fetchone()
        return self._run_result_snapshot(row) if row else None

    def get_run_result_by_ref(
        self, tenant_id: str, run_ref: Any, result_revision: int | None = None
    ) -> RunResultSnapshot | None:
        if result_revision is None:
            row = self._connection.execute(
                """SELECT * FROM run_result_snapshots
                   WHERE tenant_id=? AND provider_id=? AND project_id=? AND run_id=?
                   ORDER BY result_revision DESC LIMIT 1""",
                (tenant_id, run_ref.provider_id, run_ref.project_id, run_ref.run_id),
            ).fetchone()
        else:
            row = self._connection.execute(
                """SELECT * FROM run_result_snapshots
                   WHERE tenant_id=? AND provider_id=? AND project_id=?
                     AND run_id=? AND result_revision=?""",
                (
                    tenant_id,
                    run_ref.provider_id,
                    run_ref.project_id,
                    run_ref.run_id,
                    result_revision,
                ),
            ).fetchone()
        return self._run_result_snapshot(row) if row else None

    def mark_run_result_ingestion(
        self,
        action_id: str,
        result_revision: int,
        state: str,
        error: str | None = None,
    ) -> None:
        if state not in {"pending", "ingested", "failed"}:
            raise ValueError("invalid run result ingestion state")
        with self.transaction() as connection:
            updated = connection.execute(
                """UPDATE run_result_snapshots
                   SET ingestion_state=?,ingestion_error=?,updated_at=?
                   WHERE action_id=? AND result_revision=?""",
                (state, error, _now(), action_id, result_revision),
            )
            if getattr(updated, "rowcount", 1) == 0:
                raise NotFoundError(f"run result for {action_id!r} revision {result_revision}")

    def add_fact(self, fact: Fact) -> bool:
        with self.transaction() as connection:
            try:
                connection.execute(
                    "INSERT INTO facts VALUES (?, ?, ?, ?)",
                    (fact.fact_id, fact.investigation_id, canonical_json(fact), _now()),
                )
            except sqlite3.IntegrityError:
                return False
            self._insert_event(
                connection,
                fact.investigation_id,
                "fact.extracted",
                {
                    "fact_id": fact.fact_id,
                    "evidence_ids": fact.evidence_ids,
                    "extractor": fact.extractor,
                },
            )
            return True

    def list_facts(self, investigation_id: str) -> list[Fact]:
        rows = self._connection.execute(
            "SELECT fact_json FROM facts WHERE investigation_id=? ORDER BY created_at, fact_id",
            (investigation_id,),
        ).fetchall()
        return [Fact.model_validate_json(row[0]) for row in rows]

    def list_current_facts(self, investigation_id: str) -> list[Fact]:
        all_evidence_ids = {item.evidence_id for item in self.list_evidence(investigation_id)}
        current_evidence_ids = {
            item.evidence_id for item in self.list_current_evidence(investigation_id)
        }
        stale_evidence_ids = all_evidence_ids - current_evidence_ids
        return [
            item
            for item in self.list_facts(investigation_id)
            if not stale_evidence_ids.intersection(item.evidence_ids)
        ]

    def add_evidence_relation(self, investigation_id: str, relation: EvidenceRelation) -> bool:
        with self.transaction() as connection:
            try:
                connection.execute(
                    "INSERT INTO evidence_relations VALUES (?, ?, ?, ?, ?)",
                    (
                        investigation_id,
                        relation.source_evidence_id,
                        relation.target_id,
                        relation.relation_type,
                        _now(),
                    ),
                )
            except sqlite3.IntegrityError:
                return False
            return True

    def list_evidence_relations(self, investigation_id: str) -> list[EvidenceRelation]:
        self.get_investigation(investigation_id)
        rows = self._connection.execute(
            """SELECT source_evidence_id,target_id,relation_type
               FROM evidence_relations WHERE investigation_id=?
               ORDER BY source_evidence_id,target_id,relation_type""",
            (investigation_id,),
        ).fetchall()
        return [
            EvidenceRelation(
                source_evidence_id=row["source_evidence_id"],
                target_id=row["target_id"],
                relation_type=row["relation_type"],
            )
            for row in rows
        ]

    def add_provenance_edge(self, investigation_id: str, edge: DependencyEdge) -> bool:
        self.get_investigation(investigation_id)
        with self.transaction() as connection:
            return self._insert_provenance_edge(connection, investigation_id, edge)

    @staticmethod
    def _insert_provenance_edge(connection, investigation_id: str, edge: DependencyEdge) -> bool:
        try:
            connection.execute(
                """INSERT INTO provenance_edges(
                     investigation_id,source_id,target_id,edge_kind,relation,
                     algorithm_version,parameters_hash,created_at
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    investigation_id,
                    edge.source_id,
                    edge.target_id,
                    edge.edge_kind,
                    edge.relation,
                    edge.algorithm_version,
                    edge.parameters_hash,
                    _now(),
                ),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def list_provenance_edges(self, investigation_id: str) -> list[DependencyEdge]:
        self.get_investigation(investigation_id)
        rows = self._connection.execute(
            """SELECT source_id,target_id,edge_kind,relation,
                      algorithm_version,parameters_hash
               FROM provenance_edges WHERE investigation_id=?
               ORDER BY source_id,target_id,edge_kind,relation""",
            (investigation_id,),
        ).fetchall()
        return [
            DependencyEdge(
                source_id=row["source_id"],
                target_id=row["target_id"],
                edge_kind=row["edge_kind"],
                relation=row["relation"],
                algorithm_version=row["algorithm_version"],
                parameters_hash=row["parameters_hash"],
            )
            for row in rows
        ]

    def save_native_hypotheses(self, investigation_id: str, hypotheses: list[Hypothesis], reason: str) -> bool:
        """Atomic, revision-checked native updates using the shared hypothesis ledger."""
        from game_test_agent.evidence.ledger import HypothesisLedger

        with self.transaction() as connection:
            self._lock_investigation_row(connection, investigation_id)
            current = {h.hypothesis_id: h for h in self.list_hypotheses(investigation_id)}
            pending = []
            if len({h.hypothesis_id for h in hypotheses}) != len(hypotheses):
                raise ValueError("hypothesis IDs must be unique")
            if len(set(current) | {h.hypothesis_id for h in hypotheses}) > 10:
                raise ValueError("native investigations support at most ten hypotheses")
            for item in hypotheses:
                if item.investigation_id != investigation_id:
                    raise PermissionError("hypothesis is outside the investigation")
                old = current.get(item.hypothesis_id)
                if old == item:
                    continue
                if item.revision != (old.revision + 1 if old else 1):
                    raise ConflictError("hypothesis revision is stale")
                if item.status == "confirmed":
                    raise ValueError("native observations cannot confirm a root cause")
                evidence = HypothesisLedger(self).validate(item)
                referenced = item.supporting_evidence_ids + item.contradicting_evidence_ids
                if any(evidence[eid].retrieval_status != "ready" for eid in referenced):
                    raise ValueError("hypothesis evidence must be readable")
                if item.status == "supported" and not item.supporting_evidence_ids:
                    raise ValueError("supported hypotheses require supporting evidence")
                if item.status == "refuted" and not item.contradicting_evidence_ids:
                    raise ValueError("refuted hypotheses require contradicting evidence")
                pending.append(item)
            for item in pending:
                connection.execute("INSERT INTO hypotheses VALUES (?, ?, ?, ?, ?)",
                                   (investigation_id, item.hypothesis_id, item.revision,
                                    canonical_json(item), _now()))
                for relation, ids in (("supports", item.supporting_evidence_ids),
                                      ("refutes", item.contradicting_evidence_ids)):
                    for eid in ids:
                        if not connection.execute(
                            "SELECT 1 FROM evidence_relations WHERE investigation_id=? AND source_evidence_id=? AND target_id=? AND relation_type=?",
                            (investigation_id, eid, item.hypothesis_id, relation),
                        ).fetchone():
                            connection.execute("INSERT INTO evidence_relations VALUES (?, ?, ?, ?, ?)",
                                               (investigation_id, eid, item.hypothesis_id, relation, _now()))
            if pending:
                self._insert_event(connection, investigation_id, "mcp.native.hypotheses_updated",
                                   {"hypotheses": [h.model_dump(mode="json") for h in pending],
                                    "reason": reason, "assessment_kind": "model_hypothesis"})
            return bool(pending)

    def save_hypothesis(self, hypothesis: Hypothesis) -> None:
        with self.transaction() as connection:
            try:
                connection.execute(
                    "INSERT INTO hypotheses VALUES (?, ?, ?, ?, ?)",
                    (
                        hypothesis.investigation_id,
                        hypothesis.hypothesis_id,
                        hypothesis.revision,
                        canonical_json(hypothesis),
                        _now(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ConflictError("hypothesis revisions are immutable") from error

    def list_hypotheses(self, investigation_id: str) -> list[Hypothesis]:
        rows = self._connection.execute(
            """SELECT hypothesis_json FROM hypotheses h
               WHERE investigation_id=? AND revision=(
                 SELECT MAX(revision) FROM hypotheses
                 WHERE investigation_id=h.investigation_id AND hypothesis_id=h.hypothesis_id
               ) ORDER BY hypothesis_id""",
            (investigation_id,),
        ).fetchall()
        return [Hypothesis.model_validate_json(row[0]) for row in rows]

    def save_report(self, report: InvestigationReport, markdown: str) -> str:
        value = canonical_json(report)
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        event_id = f"evt-report-{report.investigation_id}-{report.revision}"
        with self.transaction() as connection:
            investigation = connection.execute(
                "SELECT * FROM investigations WHERE investigation_id=?",
                (report.investigation_id,),
            ).fetchone()
            if not investigation:
                raise NotFoundError(report.investigation_id)
            try:
                connection.execute(
                    "INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?)",
                    (report.investigation_id, report.revision, value, digest, markdown, _now()),
                )
            except sqlite3.IntegrityError:
                row = connection.execute(
                    "SELECT report_hash FROM reports WHERE investigation_id=? AND revision=?",
                    (report.investigation_id, report.revision),
                ).fetchone()
                if row["report_hash"] != digest:
                    raise ConflictError(
                        "immutable report revision already exists with different content"
                    )
                return digest
            summary_node_id = f"summary:report:{report.investigation_id}:{report.revision}"
            for claim in report.claims:
                claim_node_id = (
                    f"claim:{report.investigation_id}:{report.revision}:{claim.claim_id}"
                )
                for evidence_id in claim.evidence_ids:
                    self._insert_provenance_edge(
                        connection,
                        report.investigation_id,
                        DependencyEdge(
                            source_id=evidence_id,
                            target_id=claim_node_id,
                            edge_kind="argument",
                            relation="supports_claim",
                        ),
                    )
                for fact_id in claim.fact_ids:
                    self._insert_provenance_edge(
                        connection,
                        report.investigation_id,
                        DependencyEdge(
                            source_id=fact_id,
                            target_id=claim_node_id,
                            edge_kind="data",
                            relation="derived_fact",
                        ),
                    )
                for assessment_id in claim.assessment_ids:
                    self._insert_provenance_edge(
                        connection,
                        report.investigation_id,
                        DependencyEdge(
                            source_id=assessment_id,
                            target_id=claim_node_id,
                            edge_kind="data",
                            relation="evaluated_by",
                        ),
                    )
                self._insert_provenance_edge(
                    connection,
                    report.investigation_id,
                    DependencyEdge(
                        source_id=claim_node_id,
                        target_id=summary_node_id,
                        edge_kind="data",
                        relation="summarized_in",
                    ),
                )
            request = InvestigationRequest.model_validate_json(investigation["request_json"])
            operation_rows = connection.execute(
                """SELECT operation_ref_json FROM investigation_actions
                   WHERE investigation_id=? AND operation_ref_json IS NOT NULL""",
                (report.investigation_id,),
            ).fetchall()
            run_refs = []
            seen_runs: set[tuple[str, str, str]] = set()
            source_ref = request.constraints.get("source_run_ref")
            if isinstance(source_ref, dict):
                parent_ref = RunRef.model_validate(source_ref)
                if parent_ref.project_id == request.project_id:
                    seen_runs.add(
                        (parent_ref.provider_id, parent_ref.project_id, parent_ref.run_id)
                    )
                    run_refs.append(parent_ref.model_dump(mode="json"))
            for operation_row in operation_rows:
                operation = json.loads(operation_row["operation_ref_json"])
                run_ref = operation.get("run_ref")
                if run_ref:
                    identity = (
                        run_ref["provider_id"],
                        run_ref["project_id"],
                        run_ref["run_id"],
                    )
                    if identity not in seen_runs:
                        seen_runs.add(identity)
                        run_refs.append(run_ref)
            root_event_id = request.root_event_id or f"user-{report.investigation_id}"
            published_at = datetime.now(timezone.utc)
            result_event = InvestigationResultEvent(
                event_id=event_id,
                event_type="game_test.investigation.completed",
                source="game-test-agent",
                tenant_id=request.tenant_id,
                project_id=request.project_id,
                occurred_at=published_at,
                published_at=published_at,
                correlation_id=root_event_id,
                causation_id=f"report-{report.investigation_id}-{report.revision}",
                root_event_id=root_event_id,
                investigation_id=report.investigation_id,
                investigation_depth=1,
                data=InvestigationResultData(
                    investigation_id=report.investigation_id,
                    revision=report.revision,
                    status=report.status,
                    resolution=report.resolution,
                    root_event_id=root_event_id,
                    run_refs=run_refs,
                    summary=report.summary,
                    report_ref=f"report://{report.investigation_id}/{report.revision}",
                    report_hash=digest,
                ),
            )
            payload = result_event.model_dump(mode="json")
            connection.execute(
                """INSERT INTO event_outbox(
                     event_id,investigation_id,event_type,payload_json,payload_hash,state,
                     created_at,updated_at
                   ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    event_id,
                    report.investigation_id,
                    "game_test.investigation.completed",
                    canonical_json(payload),
                    payload_hash(payload),
                    _now(),
                    _now(),
                ),
            )
            self._insert_event(
                connection,
                report.investigation_id,
                "report.published",
                {"revision": report.revision, "sha256": digest},
            )
            decision = connection.execute(
                """SELECT decision_id FROM decision_records
                   WHERE investigation_id=? ORDER BY plan_revision DESC LIMIT 1""",
                (report.investigation_id,),
            ).fetchone()
            if decision:
                self._append_decision_outcome(
                    connection,
                    decision["decision_id"],
                    report.investigation_id,
                    outcome_ref=f"report://{report.investigation_id}/{report.revision}",
                    termination_basis=report.stop_reason,
                )
        return digest

    def get_report(
        self, investigation_id: str, revision: int
    ) -> tuple[InvestigationReport, str, str]:
        row = self._connection.execute(
            "SELECT report_json, markdown, report_hash FROM reports WHERE investigation_id=? AND revision=?",
            (investigation_id, revision),
        ).fetchone()
        if not row:
            raise NotFoundError(f"report {investigation_id}/{revision} not found")
        return InvestigationReport.model_validate_json(row[0]), row[1], row[2]

    def get_latest_report(self, investigation_id: str) -> tuple[InvestigationReport, str, str]:
        row = self._connection.execute(
            """SELECT report_json,markdown,report_hash FROM reports
               WHERE investigation_id=? ORDER BY revision DESC LIMIT 1""",
            (investigation_id,),
        ).fetchone()
        if not row:
            raise NotFoundError(f"no report for investigation {investigation_id!r}")
        return InvestigationReport.model_validate_json(row[0]), row[1], row[2]

    def list_reports(self, investigation_id: str) -> list[InvestigationReport]:
        self.get_investigation(investigation_id)
        rows = self._connection.execute(
            """SELECT report_json FROM reports
               WHERE investigation_id=? ORDER BY revision""",
            (investigation_id,),
        ).fetchall()
        return [InvestigationReport.model_validate_json(row[0]) for row in rows]

    def next_report_revision(self, investigation_id: str) -> int:
        self.get_investigation(investigation_id)
        row = self._connection.execute(
            "SELECT COALESCE(MAX(revision),0)+1 FROM reports WHERE investigation_id=?",
            (investigation_id,),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection, investigation_id: str, event_type: str, payload: Any
    ) -> None:
        connection.execute(
            "INSERT INTO investigation_events(investigation_id,event_type,payload_json,created_at) VALUES (?,?,?,?)",
            (investigation_id, event_type, canonical_json(payload), _now()),
        )

    @staticmethod
    def _view(row: sqlite3.Row) -> InvestigationView:
        return InvestigationView(
            investigation_id=row["investigation_id"],
            workflow_id=row["workflow_id"],
            status=row["status"],
            resolution=row["resolution"],
            revision=row["revision"],
            transition_seq=row["transition_seq"],
            request=InvestigationRequest.model_validate_json(row["request_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            stop_reason=row["stop_reason"],
        )
