"""Additive runtime records; original inputs, reports and evidence remain authoritative."""

SQLITE_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS coordination_observations (
        event_key TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
        source TEXT NOT NULL,thread_key TEXT NOT NULL,payload_hash TEXT NOT NULL,payload_json TEXT NOT NULL,
        state TEXT NOT NULL,batch_id TEXT,created_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_observations_pending ON coordination_observations(state,created_at,event_key)",
    """CREATE TABLE IF NOT EXISTS native_tool_jobs (
        job_id TEXT PRIMARY KEY,investigation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
        call_id TEXT NOT NULL,provider_id TEXT NOT NULL,tool_name TEXT NOT NULL,request_json TEXT NOT NULL,
        request_hash TEXT NOT NULL,config_hash TEXT NOT NULL,state TEXT NOT NULL,operation_id TEXT,
        result_json TEXT,polls INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
        UNIQUE(investigation_id,call_id))""",
    "CREATE INDEX IF NOT EXISTS ix_native_jobs_pending ON native_tool_jobs(investigation_id,state)",
)
