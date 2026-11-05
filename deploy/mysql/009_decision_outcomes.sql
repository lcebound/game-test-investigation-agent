-- Append-only observations that close the loop without mutating decision envelopes.
CREATE TABLE IF NOT EXISTS decision_outcomes (
  decision_id VARCHAR(128) NOT NULL,
  revision INT UNSIGNED NOT NULL,
  investigation_id VARCHAR(96) NOT NULL,
  outcome_json JSON NOT NULL,
  outcome_hash CHAR(64) NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  PRIMARY KEY (decision_id, revision),
  KEY ix_decision_outcomes_investigation (investigation_id, decision_id, revision),
  CONSTRAINT fk_decision_outcome_decision FOREIGN KEY (decision_id)
    REFERENCES decision_records(decision_id),
  CONSTRAINT fk_decision_outcome_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
