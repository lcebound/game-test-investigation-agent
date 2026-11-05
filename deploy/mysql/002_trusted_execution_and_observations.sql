-- Separate artifact content identity from observation source identity and persist run results.
ALTER TABLE evidence_records
  DROP INDEX uq_evidence_content,
  ADD COLUMN source_type VARCHAR(32) NOT NULL DEFAULT 'legacy' AFTER project_id,
  ADD COLUMN provider_id VARCHAR(128) NOT NULL DEFAULT '' AFTER source_type,
  ADD COLUMN run_id VARCHAR(256) NOT NULL DEFAULT '' AFTER provider_id,
  ADD COLUMN source_evidence_id VARCHAR(256) NULL AFTER run_id,
  ADD COLUMN source_revision BIGINT UNSIGNED NOT NULL DEFAULT 1 AFTER source_evidence_id;

UPDATE evidence_records
SET source_evidence_id = evidence_id
WHERE source_evidence_id IS NULL;

ALTER TABLE evidence_records
  MODIFY source_evidence_id VARCHAR(256) NOT NULL,
  ADD UNIQUE KEY uq_evidence_source (
    tenant_id, project_id, source_type, provider_id, run_id,
    source_evidence_id, source_revision
  ),
  ADD KEY ix_evidence_content (investigation_id, content_hash);

CREATE TABLE IF NOT EXISTS run_result_snapshots (
  tenant_id VARCHAR(128) NOT NULL,
  provider_id VARCHAR(128) NOT NULL,
  project_id VARCHAR(128) NOT NULL,
  run_id VARCHAR(256) NOT NULL,
  result_revision BIGINT UNSIGNED NOT NULL,
  action_id VARCHAR(160) NOT NULL,
  investigation_id VARCHAR(96) NOT NULL,
  result_json JSON NOT NULL,
  result_hash CHAR(64) NOT NULL,
  ingestion_state VARCHAR(32) NOT NULL,
  ingestion_error VARCHAR(512) NULL,
  created_at TIMESTAMP(6) NOT NULL,
  updated_at TIMESTAMP(6) NOT NULL,
  PRIMARY KEY (tenant_id, provider_id, project_id, run_id, result_revision),
  UNIQUE KEY uq_run_result_action_revision (action_id, result_revision),
  KEY ix_run_result_ingestion (investigation_id, ingestion_state, updated_at),
  CONSTRAINT fk_run_result_action FOREIGN KEY (action_id)
    REFERENCES investigation_actions(action_id),
  CONSTRAINT fk_run_result_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS experiment_runs (
  investigation_id VARCHAR(96) NOT NULL,
  experiment_id VARCHAR(160) NOT NULL,
  run_sequence BIGINT UNSIGNED NOT NULL,
  action_id VARCHAR(160) NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  PRIMARY KEY (investigation_id, experiment_id, run_sequence),
  UNIQUE KEY uq_experiment_action (action_id),
  CONSTRAINT fk_experiment_run_action FOREIGN KEY (action_id)
    REFERENCES investigation_actions(action_id),
  CONSTRAINT fk_experiment_run_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS criterion_assessments (
  investigation_id VARCHAR(96) NOT NULL,
  criterion_id VARCHAR(160) NOT NULL,
  evaluated_state_revision BIGINT UNSIGNED NOT NULL,
  assessment_json JSON NOT NULL,
  assessment_hash CHAR(64) NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  PRIMARY KEY (investigation_id, criterion_id, evaluated_state_revision),
  CONSTRAINT fk_assessment_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
