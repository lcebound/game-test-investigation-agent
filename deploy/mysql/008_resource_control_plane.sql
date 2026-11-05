-- Durable resource admission, fencing, and immutable project knowledge packs.
CREATE TABLE IF NOT EXISTS resource_admission_requests (
  task_id VARCHAR(96) NOT NULL,
  node_id VARCHAR(128) NOT NULL,
  project_id VARCHAR(128) NOT NULL,
  resource_domain VARCHAR(256) NOT NULL,
  priority INT NOT NULL,
  request_json JSON NOT NULL,
  request_hash CHAR(64) NOT NULL,
  state VARCHAR(32) NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  updated_at TIMESTAMP(6) NOT NULL,
  PRIMARY KEY (task_id, node_id),
  KEY ix_resource_admission_queue (state, project_id, priority, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS resource_admission_tickets (
  ticket_id VARCHAR(64) PRIMARY KEY,
  task_id VARCHAR(96) NOT NULL,
  node_id VARCHAR(128) NOT NULL,
  project_id VARCHAR(128) NOT NULL,
  resource_domain VARCHAR(256) NOT NULL,
  fencing_token BIGINT UNSIGNED NOT NULL,
  ticket_json JSON NOT NULL,
  state VARCHAR(32) NOT NULL,
  consumed_device_minutes DOUBLE NOT NULL DEFAULT 0,
  created_at TIMESTAMP(6) NOT NULL,
  updated_at TIMESTAMP(6) NOT NULL,
  KEY ix_resource_ticket_node (task_id, node_id, created_at),
  KEY ix_resource_admission_active (state, project_id, resource_domain)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS resource_fencing (
  resource_domain VARCHAR(256) PRIMARY KEY,
  fencing_token BIGINT UNSIGNED NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS resource_scheduler_state (
  singleton_id TINYINT UNSIGNED PRIMARY KEY,
  cursor_project_id VARCHAR(128) NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS project_packs (
  tenant_id VARCHAR(128) NOT NULL,
  project_id VARCHAR(128) NOT NULL,
  version VARCHAR(64) NOT NULL,
  pack_id VARCHAR(128) NOT NULL,
  pack_json JSON NOT NULL,
  pack_hash CHAR(64) NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  PRIMARY KEY (tenant_id, project_id, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
