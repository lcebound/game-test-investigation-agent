-- Explicit human approval, separate from model/tool messages and platform scopes.
CREATE TABLE IF NOT EXISTS code_permission_requests (
  approval_id VARCHAR(128) PRIMARY KEY,
  job_id VARCHAR(128) NOT NULL UNIQUE,
  investigation_id VARCHAR(96) NOT NULL,
  request_json JSON NOT NULL,
  request_hash CHAR(64) NOT NULL,
  state VARCHAR(16) NOT NULL,
  decision_json JSON,
  created_at TIMESTAMP(6) NOT NULL,
  expires_at TIMESTAMP(6) NOT NULL,
  INDEX ix_code_permissions_investigation (investigation_id, created_at),
  CONSTRAINT fk_code_permissions_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
