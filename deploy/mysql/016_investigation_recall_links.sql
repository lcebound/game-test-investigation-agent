CREATE TABLE IF NOT EXISTS investigation_recall_links (
  investigation_id VARCHAR(96) NOT NULL,
  source_investigation_id VARCHAR(96) NOT NULL,
  manifest_json JSON NOT NULL,
  manifest_hash CHAR(64) NOT NULL,
  revision BIGINT NOT NULL,
  active BOOLEAN NOT NULL,
  updated_at TIMESTAMP(6) NOT NULL,
  PRIMARY KEY (investigation_id,source_investigation_id),
  FOREIGN KEY (investigation_id) REFERENCES investigations(investigation_id),
  FOREIGN KEY (source_investigation_id) REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
