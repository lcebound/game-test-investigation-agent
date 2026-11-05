-- Generic data and argument edges for report claim/summary lineage.
CREATE TABLE IF NOT EXISTS provenance_edges (
  investigation_id VARCHAR(96) NOT NULL,
  source_id VARCHAR(255) NOT NULL,
  target_id VARCHAR(255) NOT NULL,
  edge_kind VARCHAR(16) NOT NULL,
  relation VARCHAR(64) NOT NULL,
  algorithm_version VARCHAR(128) NULL,
  parameters_hash CHAR(64) NULL,
  created_at TIMESTAMP(6) NOT NULL,
  PRIMARY KEY (investigation_id, source_id, target_id, edge_kind, relation),
  KEY ix_provenance_edges_target (investigation_id, target_id, edge_kind),
  CONSTRAINT fk_provenance_edge_investigation FOREIGN KEY (investigation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
