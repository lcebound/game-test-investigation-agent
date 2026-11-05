CREATE TABLE IF NOT EXISTS task_subscriptions (
  subscription_id CHAR(64) PRIMARY KEY,
  investigation_id VARCHAR(96) NOT NULL,
  tenant_id VARCHAR(128) NOT NULL, project_id VARCHAR(128) NOT NULL,
  source VARCHAR(128) NOT NULL, provider_id VARCHAR(128) NOT NULL,
  run_id VARCHAR(256) NOT NULL, active BOOLEAN NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  KEY ix_task_subscriptions_run (tenant_id,project_id,provider_id,run_id),
  FOREIGN KEY (investigation_id) REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS task_feedback_events (
  event_key CHAR(64) PRIMARY KEY,
  tenant_id VARCHAR(128) NOT NULL, project_id VARCHAR(128) NOT NULL,
  source VARCHAR(128) NOT NULL, event_id VARCHAR(256) NOT NULL,
  payload_hash CHAR(64) NOT NULL, payload_json JSON NOT NULL,
  created_at TIMESTAMP(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS task_feedback_batches (
  batch_id VARCHAR(80) PRIMARY KEY,
  investigation_id VARCHAR(96) NOT NULL,
  generation BIGINT NOT NULL, input_fingerprint CHAR(64) NOT NULL,
  packet_json JSON NOT NULL, created_at TIMESTAMP(6) NOT NULL,
  UNIQUE KEY uq_feedback_batch_generation (investigation_id,generation),
  FOREIGN KEY (investigation_id) REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS task_feedback_deliveries (
  cursor BIGINT PRIMARY KEY AUTO_INCREMENT,
  event_key CHAR(64) NOT NULL, investigation_id VARCHAR(96) NOT NULL,
  input_fingerprint CHAR(64) NOT NULL, batch_id VARCHAR(80),
  created_at TIMESTAMP(6) NOT NULL,
  UNIQUE KEY uq_feedback_delivery (event_key,investigation_id),
  KEY ix_feedback_delivery_pending (batch_id,investigation_id,cursor),
  KEY ix_feedback_delivery_recall (investigation_id,cursor),
  FOREIGN KEY (event_key) REFERENCES task_feedback_events(event_key),
  FOREIGN KEY (investigation_id) REFERENCES investigations(investigation_id),
  FOREIGN KEY (batch_id) REFERENCES task_feedback_batches(batch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
