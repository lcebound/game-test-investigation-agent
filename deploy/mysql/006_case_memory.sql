-- Immutable, reviewed cross-investigation memory revisions.
CREATE TABLE IF NOT EXISTS case_memory_records (
  case_id VARCHAR(96) NOT NULL,
  revision INT UNSIGNED NOT NULL,
  tenant_id VARCHAR(128) NOT NULL,
  project_id VARCHAR(128) NOT NULL,
  status VARCHAR(32) NOT NULL,
  source_investigation_id VARCHAR(96) NOT NULL,
  source_report_revision INT UNSIGNED NOT NULL,
  record_json JSON NOT NULL,
  record_hash CHAR(64) NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  PRIMARY KEY (case_id, revision),
  KEY ix_case_memory_scope_status (
    tenant_id, project_id, status, case_id, revision
  ),
  KEY ix_case_memory_source (source_investigation_id, case_id, revision),
  CONSTRAINT fk_case_memory_source FOREIGN KEY (source_investigation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
