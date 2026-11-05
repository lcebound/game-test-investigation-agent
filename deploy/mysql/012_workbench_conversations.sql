-- Presentation metadata lives outside the investigation and evidence ledgers.
CREATE TABLE IF NOT EXISTS workbench_conversations (
  root_id VARCHAR(96) PRIMARY KEY,
  title VARCHAR(200) NOT NULL,
  pinned BOOLEAN NOT NULL DEFAULT FALSE,
  archived BOOLEAN NOT NULL DEFAULT FALSE,
  revision BIGINT UNSIGNED NOT NULL,
  updated_at TIMESTAMP(6) NOT NULL,
  CONSTRAINT fk_workbench_conversation_root FOREIGN KEY (root_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS workbench_message_receipts (
  root_id VARCHAR(96) NOT NULL,
  principal_id VARCHAR(256) NOT NULL,
  request_key CHAR(64) NOT NULL,
  payload_hash CHAR(64) NOT NULL,
  response_json JSON NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  PRIMARY KEY (root_id, principal_id, request_key),
  CONSTRAINT fk_workbench_message_root FOREIGN KEY (root_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
