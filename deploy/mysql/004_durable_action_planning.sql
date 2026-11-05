-- Persist the exact proposal and normalized action before provider submission.
CREATE TABLE IF NOT EXISTS planned_actions (
  planning_id VARCHAR(160) PRIMARY KEY,
  investigation_id VARCHAR(96) NOT NULL,
  plan_revision BIGINT UNSIGNED NOT NULL,
  decision_id VARCHAR(160) NOT NULL,
  proposal_hash CHAR(64) NOT NULL,
  action_hash CHAR(64) NOT NULL,
  planned_json JSON NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  UNIQUE KEY uq_planned_action_round (investigation_id, plan_revision),
  UNIQUE KEY uq_planned_action_hash (action_hash),
  CONSTRAINT fk_planned_action_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id),
  CONSTRAINT fk_planned_action_decision FOREIGN KEY (decision_id)
    REFERENCES decision_records(decision_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
