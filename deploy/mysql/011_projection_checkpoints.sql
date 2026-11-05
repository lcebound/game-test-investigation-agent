-- Event-cursor watermarks for critical read projections.
CREATE TABLE IF NOT EXISTS projection_checkpoints (
  investigation_id VARCHAR(96) NOT NULL,
  projection_name VARCHAR(64) NOT NULL,
  processed_cursor BIGINT UNSIGNED NOT NULL,
  projection_hash CHAR(64) NOT NULL,
  updated_at TIMESTAMP(6) NOT NULL,
  PRIMARY KEY (investigation_id, projection_name),
  CONSTRAINT fk_projection_checkpoint_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
