-- Freeze model context and validated decisions before executing side effects.
CREATE TABLE IF NOT EXISTS context_snapshots (
  snapshot_id VARCHAR(160) PRIMARY KEY,
  investigation_id VARCHAR(96) NOT NULL,
  state_revision BIGINT UNSIGNED NOT NULL,
  plan_revision BIGINT UNSIGNED NOT NULL,
  context_hash CHAR(64) NOT NULL,
  snapshot_json JSON NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  UNIQUE KEY uq_context_round (investigation_id, plan_revision),
  CONSTRAINT fk_context_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS decision_records (
  decision_id VARCHAR(160) PRIMARY KEY,
  investigation_id VARCHAR(96) NOT NULL,
  plan_revision BIGINT UNSIGNED NOT NULL,
  context_snapshot_id VARCHAR(160) NOT NULL,
  expected_state_revision BIGINT UNSIGNED NOT NULL,
  decision_hash CHAR(64) NOT NULL,
  decision_json JSON NOT NULL,
  status VARCHAR(32) NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  UNIQUE KEY uq_decision_round (investigation_id, plan_revision),
  CONSTRAINT fk_decision_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id),
  CONSTRAINT fk_decision_context FOREIGN KEY (context_snapshot_id)
    REFERENCES context_snapshots(snapshot_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
