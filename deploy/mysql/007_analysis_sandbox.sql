-- Provider-neutral control-plane records for externally isolated analysis sandboxes.
ALTER TABLE cleanup_operations
  ADD COLUMN resource_kind VARCHAR(32) NOT NULL DEFAULT 'action',
  ADD COLUMN cleanup_owner VARCHAR(128) NOT NULL DEFAULT 'action-provider';

CREATE TABLE IF NOT EXISTS analysis_jobs (
  job_id VARCHAR(128) PRIMARY KEY,
  idempotency_key VARCHAR(256) NOT NULL UNIQUE,
  tenant_id VARCHAR(128) NOT NULL,
  project_id VARCHAR(128) NOT NULL,
  investigation_id VARCHAR(96) NOT NULL,
  state VARCHAR(32) NOT NULL,
  spec_hash CHAR(64) NOT NULL,
  record_json JSON NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  updated_at TIMESTAMP(6) NOT NULL,
  KEY ix_analysis_jobs_investigation (investigation_id, created_at),
  CONSTRAINT fk_analysis_job_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS analysis_result_snapshots (
  job_id VARCHAR(128) NOT NULL,
  result_revision INT UNSIGNED NOT NULL,
  investigation_id VARCHAR(96) NOT NULL,
  result_hash CHAR(64) NOT NULL,
  snapshot_json JSON NOT NULL,
  ingestion_state VARCHAR(32) NOT NULL,
  ingestion_error VARCHAR(512) NULL,
  created_at TIMESTAMP(6) NOT NULL,
  updated_at TIMESTAMP(6) NOT NULL,
  PRIMARY KEY (job_id, result_revision),
  CONSTRAINT fk_analysis_result_job FOREIGN KEY (job_id)
    REFERENCES analysis_jobs(job_id),
  CONSTRAINT fk_analysis_result_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS derived_fact_candidates (
  candidate_id VARCHAR(128) NOT NULL,
  revision INT UNSIGNED NOT NULL,
  investigation_id VARCHAR(96) NOT NULL,
  analysis_job_id VARCHAR(128) NOT NULL,
  status VARCHAR(32) NOT NULL,
  candidate_hash CHAR(64) NOT NULL,
  candidate_json JSON NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  PRIMARY KEY (candidate_id, revision),
  KEY ix_derived_fact_candidates_investigation (
    investigation_id, analysis_job_id, candidate_id, revision
  ),
  CONSTRAINT fk_derived_fact_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id),
  CONSTRAINT fk_derived_fact_analysis_job FOREIGN KEY (analysis_job_id)
    REFERENCES analysis_jobs(job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
