-- Independent Worker lineages belong to one conversation; continuation remains supersedes.
CREATE TABLE IF NOT EXISTS conversation_task_links (
  task_root_id VARCHAR(96) PRIMARY KEY,
  conversation_id VARCHAR(96) NOT NULL,
  tenant_id VARCHAR(128) NOT NULL,
  project_id VARCHAR(128) NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  KEY ix_conversation_tasks (tenant_id, project_id, conversation_id, task_root_id),
  CONSTRAINT fk_conversation_task_root FOREIGN KEY (task_root_id)
    REFERENCES investigations(investigation_id),
  CONSTRAINT fk_conversation_task_owner FOREIGN KEY (conversation_id)
    REFERENCES investigations(investigation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
