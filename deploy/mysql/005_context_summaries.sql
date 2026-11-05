-- Persist source-addressed summaries without replacing any source ledger records.
CREATE TABLE IF NOT EXISTS context_summaries (
  summary_id VARCHAR(160) PRIMARY KEY,
  investigation_id VARCHAR(96) NOT NULL,
  source_event_cursor_end BIGINT UNSIGNED NOT NULL,
  source_hash CHAR(64) NOT NULL,
  generator_version VARCHAR(128) NOT NULL,
  summary_hash CHAR(64) NOT NULL,
  summary_json JSON NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  UNIQUE KEY uq_context_summary_source (
    investigation_id, source_hash, generator_version
  ),
  KEY ix_context_summary_latest (investigation_id, source_event_cursor_end, created_at),
  CONSTRAINT fk_context_summary_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
