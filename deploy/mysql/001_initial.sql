-- Game Test Agent business database, MySQL 8.0+
-- Temporal owns its own persistence; this schema contains only Agent domain state.

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INT PRIMARY KEY,
  checksum CHAR(64) NOT NULL,
  applied_at TIMESTAMP(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS investigations (
  investigation_id VARCHAR(96) PRIMARY KEY,
  workflow_id VARCHAR(192) NOT NULL,
  tenant_id VARCHAR(128) NOT NULL,
  project_id VARCHAR(128) NOT NULL,
  idempotency_key VARCHAR(512) NOT NULL,
  request_hash CHAR(64) NOT NULL,
  request_json JSON NOT NULL,
  status VARCHAR(32) NOT NULL,
  resolution VARCHAR(32) NULL,
  revision BIGINT UNSIGNED NOT NULL,
  transition_seq BIGINT UNSIGNED NOT NULL,
  stop_reason VARCHAR(512) NULL,
  created_at TIMESTAMP(6) NOT NULL,
  updated_at TIMESTAMP(6) NOT NULL,
  UNIQUE KEY uq_investigation_workflow (workflow_id),
  UNIQUE KEY uq_investigation_idempotency (tenant_id, project_id, idempotency_key),
  KEY ix_investigation_scope_status (tenant_id, project_id, status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS investigation_authorizations (
  investigation_id VARCHAR(96) PRIMARY KEY,
  principal_id VARCHAR(256) NOT NULL,
  scopes_json JSON NOT NULL,
  policy_profile VARCHAR(128) NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  CONSTRAINT fk_authorization_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS investigation_relations (
  parent_investigation_id VARCHAR(96) NOT NULL,
  child_investigation_id VARCHAR(96) NOT NULL,
  relation_type VARCHAR(32) NOT NULL,
  reason VARCHAR(2000) NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  PRIMARY KEY (parent_investigation_id, child_investigation_id, relation_type),
  UNIQUE KEY uq_relation_child (child_investigation_id),
  CONSTRAINT fk_relation_parent FOREIGN KEY (parent_investigation_id)
    REFERENCES investigations(investigation_id),
  CONSTRAINT fk_relation_child FOREIGN KEY (child_investigation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS investigation_observations (
  event_id VARCHAR(256) PRIMARY KEY,
  finding_id VARCHAR(256) NOT NULL,
  incident_key VARCHAR(768) NOT NULL,
  investigation_id VARCHAR(96) NOT NULL,
  result_revision BIGINT UNSIGNED NOT NULL,
  observation_json JSON NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  KEY ix_observation_incident (incident_key(255), created_at),
  CONSTRAINT fk_observation_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS investigation_plans (
  investigation_id VARCHAR(96) NOT NULL,
  revision BIGINT UNSIGNED NOT NULL,
  capability_snapshot_hash CHAR(64) NOT NULL,
  plan_json JSON NOT NULL,
  plan_hash CHAR(64) NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  PRIMARY KEY (investigation_id, revision),
  CONSTRAINT fk_plan_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS workflow_start_outbox (
  investigation_id VARCHAR(96) PRIMARY KEY,
  state VARCHAR(16) NOT NULL DEFAULT 'pending',
  attempts INT UNSIGNED NOT NULL DEFAULT 0,
  last_error VARCHAR(128) NULL,
  next_attempt_at TIMESTAMP(6) NULL,
  updated_at TIMESTAMP(6) NOT NULL,
  CONSTRAINT fk_start_outbox_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS workflow_command_outbox (
  command_id VARCHAR(256) PRIMARY KEY,
  investigation_id VARCHAR(96) NOT NULL,
  kind VARCHAR(32) NOT NULL,
  payload_json JSON NOT NULL,
  state VARCHAR(16) NOT NULL,
  attempts INT UNSIGNED NOT NULL DEFAULT 0,
  last_error VARCHAR(128) NULL,
  next_attempt_at TIMESTAMP(6) NULL,
  created_at TIMESTAMP(6) NOT NULL,
  updated_at TIMESTAMP(6) NOT NULL,
  KEY ix_command_outbox_pending (state, created_at),
  CONSTRAINT fk_command_outbox_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS event_inbox (
  source VARCHAR(128) NOT NULL,
  event_id VARCHAR(256) NOT NULL,
  payload_hash CHAR(64) NOT NULL,
  payload_json JSON NOT NULL,
  investigation_id VARCHAR(96) NOT NULL,
  received_at TIMESTAMP(6) NOT NULL,
  PRIMARY KEY (source, event_id),
  KEY ix_event_inbox_investigation (investigation_id, received_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS event_outbox (
  event_id VARCHAR(256) PRIMARY KEY,
  investigation_id VARCHAR(96) NOT NULL,
  event_type VARCHAR(128) NOT NULL,
  payload_json JSON NOT NULL,
  payload_hash CHAR(64) NOT NULL,
  state VARCHAR(16) NOT NULL,
  attempts INT UNSIGNED NOT NULL DEFAULT 0,
  last_error VARCHAR(128) NULL,
  next_attempt_at TIMESTAMP(6) NULL,
  created_at TIMESTAMP(6) NOT NULL,
  updated_at TIMESTAMP(6) NOT NULL,
  KEY ix_event_outbox_pending (state, created_at),
  CONSTRAINT fk_event_outbox_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS investigation_events (
  cursor BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  investigation_id VARCHAR(96) NOT NULL,
  event_type VARCHAR(128) NOT NULL,
  payload_json JSON NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  KEY ix_progress_cursor (investigation_id, cursor),
  CONSTRAINT fk_progress_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS capability_snapshots (
  provider_id VARCHAR(128) NOT NULL,
  capability_id VARCHAR(256) NOT NULL,
  capability_version VARCHAR(64) NOT NULL,
  descriptor_json JSON NOT NULL,
  descriptor_hash CHAR(64) NOT NULL,
  validated_at TIMESTAMP(6) NOT NULL,
  PRIMARY KEY (provider_id, capability_id, capability_version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS investigation_actions (
  action_id VARCHAR(160) PRIMARY KEY,
  investigation_id VARCHAR(96) NOT NULL,
  idempotency_key VARCHAR(320) NOT NULL,
  semantic_hash CHAR(64) NOT NULL,
  request_hash CHAR(64) NOT NULL,
  action_json JSON NOT NULL,
  permit_json JSON NULL,
  state VARCHAR(32) NOT NULL,
  operation_ref_json JSON NULL,
  created_at TIMESTAMP(6) NOT NULL,
  updated_at TIMESTAMP(6) NOT NULL,
  UNIQUE KEY uq_action_idempotency (idempotency_key),
  UNIQUE KEY uq_action_semantic (investigation_id, semantic_hash),
  KEY ix_action_state (investigation_id, state),
  CONSTRAINT fk_action_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS budget_reservations (
  action_id VARCHAR(160) PRIMARY KEY,
  investigation_id VARCHAR(96) NOT NULL,
  device_minutes DECIMAL(18,6) NOT NULL,
  state VARCHAR(16) NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  KEY ix_budget_investigation (investigation_id, state),
  CONSTRAINT fk_budget_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS budget_counter_reservations (
  investigation_id VARCHAR(96) NOT NULL,
  counter_name VARCHAR(64) NOT NULL,
  reservation_id VARCHAR(192) NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  PRIMARY KEY (investigation_id, counter_name, reservation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS reasoning_steps (
  investigation_id VARCHAR(96) NOT NULL,
  plan_revision BIGINT UNSIGNED NOT NULL,
  reasoning_step_id VARCHAR(128) NOT NULL,
  context_hash CHAR(64) NOT NULL,
  result_json JSON NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  PRIMARY KEY (investigation_id, plan_revision, reasoning_step_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS evidence_records (
  evidence_id VARCHAR(256) PRIMARY KEY,
  investigation_id VARCHAR(96) NOT NULL,
  tenant_id VARCHAR(128) NOT NULL,
  project_id VARCHAR(128) NOT NULL,
  evidence_json JSON NOT NULL,
  content_hash CHAR(64) NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  UNIQUE KEY uq_evidence_content (investigation_id, content_hash),
  KEY ix_evidence_scope (tenant_id, project_id, investigation_id),
  CONSTRAINT fk_evidence_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS facts (
  fact_id VARCHAR(256) PRIMARY KEY,
  investigation_id VARCHAR(96) NOT NULL,
  fact_json JSON NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  KEY ix_fact_investigation (investigation_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS evidence_relations (
  investigation_id VARCHAR(96) NOT NULL,
  source_evidence_id VARCHAR(256) NOT NULL,
  target_id VARCHAR(256) NOT NULL,
  relation_type VARCHAR(32) NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  PRIMARY KEY (investigation_id, source_evidence_id, target_id, relation_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS hypotheses (
  investigation_id VARCHAR(96) NOT NULL,
  hypothesis_id VARCHAR(256) NOT NULL,
  revision BIGINT UNSIGNED NOT NULL,
  hypothesis_json JSON NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  PRIMARY KEY (investigation_id, hypothesis_id, revision)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS reports (
  investigation_id VARCHAR(96) NOT NULL,
  revision BIGINT UNSIGNED NOT NULL,
  report_json JSON NOT NULL,
  report_hash CHAR(64) NOT NULL,
  markdown MEDIUMTEXT NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  PRIMARY KEY (investigation_id, revision)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS cleanup_operations (
  action_id VARCHAR(160) PRIMARY KEY,
  investigation_id VARCHAR(96) NOT NULL,
  state VARCHAR(16) NOT NULL,
  attempts INT UNSIGNED NOT NULL DEFAULT 0,
  last_error VARCHAR(512) NULL,
  updated_at TIMESTAMP(6) NOT NULL,
  KEY ix_cleanup_pending (state, updated_at),
  CONSTRAINT fk_cleanup_action FOREIGN KEY (action_id)
    REFERENCES investigation_actions(action_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS audit_events (
  audit_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  principal_id VARCHAR(256) NOT NULL,
  tenant_id VARCHAR(128) NOT NULL,
  project_id VARCHAR(128) NOT NULL,
  action VARCHAR(128) NOT NULL,
  target_id VARCHAR(256) NULL,
  metadata_json JSON NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  KEY ix_audit_scope (tenant_id, project_id, created_at),
  KEY ix_audit_target (target_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
