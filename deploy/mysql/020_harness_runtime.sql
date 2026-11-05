CREATE TABLE IF NOT EXISTS coordination_observations (
  event_key VARCHAR(96) PRIMARY KEY,
  conversation_id VARCHAR(96) NOT NULL,
  source VARCHAR(128) NOT NULL,
  thread_key VARCHAR(128) NOT NULL,
  payload_hash CHAR(64) NOT NULL,
  payload_json LONGTEXT NOT NULL,
  state VARCHAR(16) NOT NULL,
  batch_id VARCHAR(96) NULL,
  created_at TIMESTAMP(6) NOT NULL,
  KEY ix_observations_pending (state,created_at,event_key),
  FOREIGN KEY (conversation_id) REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
CREATE TABLE IF NOT EXISTS native_tool_jobs (
  job_id VARCHAR(96) PRIMARY KEY,
  investigation_id VARCHAR(96) NOT NULL,
  call_id VARCHAR(256) NOT NULL,
  provider_id VARCHAR(128) NOT NULL,
  tool_name VARCHAR(256) NOT NULL,
  request_json LONGTEXT NOT NULL,
  request_hash CHAR(64) NOT NULL,
  config_hash CHAR(64) NOT NULL,
  state VARCHAR(24) NOT NULL,
  operation_id VARCHAR(256) NULL,
  result_json LONGTEXT NULL,
  polls BIGINT NOT NULL DEFAULT 0,
  created_at TIMESTAMP(6) NOT NULL,
  updated_at TIMESTAMP(6) NOT NULL,
  UNIQUE KEY uq_native_job_call (investigation_id,call_id),
  KEY ix_native_jobs_pending (investigation_id,state),
  FOREIGN KEY (investigation_id) REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
