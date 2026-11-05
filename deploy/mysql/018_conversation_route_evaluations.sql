CREATE TABLE IF NOT EXISTS conversation_route_evaluations (
  conversation_id VARCHAR(96) NOT NULL,
  principal_id VARCHAR(256) NOT NULL,
  request_key VARCHAR(96) NOT NULL,
  payload_hash CHAR(64) NOT NULL,
  original_json LONGTEXT NOT NULL,
  policy_version CHAR(64) NOT NULL,
  generation BIGINT NOT NULL,
  owner_token CHAR(32) NOT NULL,
  state VARCHAR(16) NOT NULL,
  lease_deadline DOUBLE NOT NULL,
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  PRIMARY KEY(conversation_id,principal_id,request_key),
  FOREIGN KEY (conversation_id) REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
