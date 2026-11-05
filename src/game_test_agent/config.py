from __future__ import annotations

import os
import json
import re
import math
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path
    artifact_root: Path
    environment: str = "development"
    database_backend: str = "sqlite"
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_database: str = "game_test_agent"
    mysql_user: str = ""
    mysql_password: str = field(default="", repr=False)
    mysql_ssl_ca: str | None = None
    artifact_backend: str = "local"
    artifact_max_bytes: int = 128 * 1024 * 1024
    s3_bucket: str = ""
    s3_prefix: str = "game-test-agent"
    s3_region: str | None = None
    s3_endpoint_url: str | None = None
    s3_server_side_encryption: str = "AES256"
    s3_kms_key_id: str | None = None
    model_name: str = "gpt-5-mini"
    enable_model: bool = False
    enable_semantic_router: bool = False
    enable_router_recovery: bool = True
    router_timeout_seconds: float = 10.0
    model_api: str = "responses"
    model_output_mode: str = "json_schema"
    model_extra_body_json: str = field(default="{}", repr=False)
    model_timeout_seconds: float = 60.0
    model_context_window_tokens: int = 32768
    model_max_output_tokens: int = 4096
    model_max_total_tokens: int = 250_000
    model_max_estimated_cost: float | None = None
    model_cost_per_input_token: float = 0.0
    model_cost_per_output_token: float = 0.0
    model_reasoning_effort: str | None = None
    max_rounds: int = 4
    max_real_runs: int = 3
    max_tool_reads: int = 40
    max_device_minutes: float = 120.0
    max_wall_seconds: int = 7200
    max_concurrent_investigations: int = 4
    max_concurrent_models: int = 4
    max_concurrent_tools: int = 8
    max_concurrent_analyses: int = 2
    max_queued_operations: int = 64
    temporal_address: str = "127.0.0.1:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "game-test-agent"
    rabbitmq_url: str = "amqp://guest:guest@127.0.0.1/"
    rabbitmq_exchange: str = "game_test.events.v1"
    enable_local_event_bridge: bool = False
    platform_id: str = "local"
    workflow_backend: str = "local"
    dispatcher_poll_seconds: float = 1.0
    mcp_providers_json: str = "[]"
    mcp_native_tools: bool = False
    event_source_policies_json: str = "[]"
    api_host: str = "127.0.0.1"
    api_port: int = 8080
    auth_mode: str = "dev_headers"
    jwt_jwks_url: str | None = None
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwt_leeway_seconds: int = 30
    jwt_jwks_cache_seconds: int = 300
    jwt_jwks_timeout_seconds: int = 5
    allow_insecure_dev_headers: bool = False
    sandbox_image_digests: tuple[str, ...] = ()
    sandbox_runner_ids: tuple[str, ...] = ()
    sandbox_backend: str = "disabled"
    sandbox_runtime: str = "psec"
    sandbox_runtime_digest: str = ""
    sandbox_python_path: Path | None = None
    sandbox_approval_key_file: Path | None = None
    sandbox_runner_root: Path | None = None
    sandbox_max_repairs: int = 2
    sandbox_max_jobs_per_investigation: int = 4
    sandbox_max_cpu_seconds: int = 120
    sandbox_max_memory_mb: int = 1024
    sandbox_max_process_count: int = 16
    sandbox_max_disk_mb: int = 512
    sandbox_max_wall_seconds: int = 180
    sandbox_max_output_bytes: int = 32 * 1024 * 1024
    sandbox_max_program_bytes: int = 1024 * 1024
    log_level: str = "INFO"

    def default_budget(self):
        from game_test_agent.contracts.models import Budget

        return Budget(
            max_rounds=self.max_rounds, max_real_runs=self.max_real_runs,
            max_tool_reads=self.max_tool_reads, max_device_minutes=self.max_device_minutes,
            max_wall_seconds=self.max_wall_seconds,
        )

    def validate(self) -> None:
        if self.enable_semantic_router and not self.enable_model:
            raise ValueError("semantic router requires GTA_ENABLE_MODEL")
        if not math.isfinite(self.router_timeout_seconds) or not 0 < self.router_timeout_seconds <= 60:
            raise ValueError("router timeout must be finite and between 0 and 60 seconds")
        if any(value < 1 for value in (self.max_concurrent_investigations, self.max_concurrent_models,
                                       self.max_concurrent_tools, self.max_concurrent_analyses)):
            raise ValueError("execution concurrency must be positive")
        if self.max_queued_operations < 0:
            raise ValueError("operation queue limit must be nonnegative")
        if self.environment not in {"development", "test", "production"}:
            raise ValueError("environment must be development, test or production")
        if self.model_api not in {"responses", "chat_completions"}:
            raise ValueError("model API must be responses or chat_completions")
        if self.model_output_mode not in {"json_schema", "json_prompt", "json_object"}:
            raise ValueError("model output mode must be json_schema, json_prompt or json_object")
        if self.model_output_mode == "json_object" and self.model_api != "chat_completions":
            raise ValueError("json_object output requires chat_completions")
        try:
            extra_body = json.loads(self.model_extra_body_json)
        except json.JSONDecodeError as error:
            raise ValueError("model extra body must be valid JSON") from error
        if not isinstance(extra_body, dict):
            raise ValueError("model extra body must be a JSON object")
        if self.model_timeout_seconds <= 0:
            raise ValueError("model timeout must be positive")
        if self.model_max_total_tokens < 1:
            raise ValueError("model total token budget must be positive")
        for value in (self.model_cost_per_input_token, self.model_cost_per_output_token,
                      self.model_max_estimated_cost):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError("model prices and cost budget must be finite and nonnegative")
        if self.model_max_estimated_cost is not None and (
            self.model_cost_per_input_token <= 0 or self.model_cost_per_output_token <= 0
        ):
            raise ValueError("model cost budgets require explicit input and output token prices")
        if not 1 <= self.model_max_output_tokens <= 131072:
            raise ValueError("model max output tokens must be between 1 and 131072")
        if not 2 <= self.model_context_window_tokens <= 1_000_000:
            raise ValueError("model context window must be between 2 and 1000000 tokens")
        if self.model_max_output_tokens >= self.model_context_window_tokens:
            raise ValueError("model output token reserve must be below the context window")
        if self.model_reasoning_effort not in {
            None,
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            raise ValueError("model reasoning effort is invalid")
        if self.workflow_backend not in {"local", "temporal"}:
            raise ValueError("workflow backend must be local or temporal")
        if self.enable_local_event_bridge and self.workflow_backend != "local":
            raise ValueError("local event bridge requires the local workflow backend")
        if self.database_backend not in {"sqlite", "mysql"}:
            raise ValueError("database backend must be sqlite or mysql")
        if not 1 <= self.mysql_port <= 65535:
            raise ValueError("MySQL port must be between 1 and 65535")
        if self.database_backend == "mysql":
            if not all(
                [self.mysql_host, self.mysql_database, self.mysql_user, self.mysql_password]
            ):
                raise ValueError("MySQL backend requires host/database/user/password")
            if self.mysql_host not in {"127.0.0.1", "localhost", "::1"} and not self.mysql_ssl_ca:
                raise ValueError("remote MySQL requires a CA file")
        if self.artifact_backend not in {"local", "s3"}:
            raise ValueError("artifact backend must be local or s3")
        if self.artifact_backend == "s3" and not self.s3_bucket:
            raise ValueError("S3 artifact backend requires a bucket")
        if self.artifact_max_bytes < 1:
            raise ValueError("artifact_max_bytes must be positive")
        normalized_prefix = self.s3_prefix.strip("/")
        if not normalized_prefix or any(
            part in {"", ".", ".."} for part in normalized_prefix.split("/")
        ):
            raise ValueError("S3 prefix contains an unsafe segment")
        if self.s3_server_side_encryption not in {"AES256", "aws:kms"}:
            raise ValueError("S3 encryption must be AES256 or aws:kms")
        if self.s3_server_side_encryption == "aws:kms" and not self.s3_kms_key_id:
            raise ValueError("S3 KMS encryption requires a key ID")
        if self.s3_endpoint_url:
            endpoint = urlparse(self.s3_endpoint_url)
            if endpoint.username or endpoint.password or endpoint.fragment:
                raise ValueError("S3 endpoint URL cannot contain credentials or a fragment")
            loopback = endpoint.hostname in {"127.0.0.1", "localhost", "::1"}
            if not endpoint.hostname or endpoint.scheme not in {"http", "https"}:
                raise ValueError("S3 endpoint URL must use HTTP or HTTPS")
            if endpoint.scheme != "https" and not loopback:
                raise ValueError("non-loopback S3 endpoint URL must use HTTPS")
        if self.auth_mode not in {"dev_headers", "jwt"}:
            raise ValueError("auth mode must be dev_headers or jwt")
        if (
            self.auth_mode == "dev_headers"
            and self.api_host not in {"127.0.0.1", "localhost", "::1"}
            and not self.allow_insecure_dev_headers
        ):
            raise ValueError("dev header authentication may only bind to loopback")
        if self.auth_mode == "jwt":
            if not all([self.jwt_jwks_url, self.jwt_issuer, self.jwt_audience]):
                raise ValueError("JWT auth requires JWKS URL, issuer and audience")
            parsed = urlparse(self.jwt_jwks_url or "")
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError("JWT JWKS URL must use HTTPS")
        if not 0 <= self.jwt_leeway_seconds <= 300:
            raise ValueError("JWT leeway must be between 0 and 300 seconds")
        if self.jwt_jwks_cache_seconds < 1 or self.jwt_jwks_timeout_seconds < 1:
            raise ValueError("JWT JWKS cache and timeout must be positive")
        if not 1 <= self.api_port <= 65535:
            raise ValueError("API port must be between 1 and 65535")
        if not math.isfinite(self.dispatcher_poll_seconds) or self.dispatcher_poll_seconds <= 0:
            raise ValueError("dispatcher poll interval must be positive")
        if self.max_rounds < 1 or self.max_wall_seconds < 1:
            raise ValueError("round and wall-clock budgets must be positive")
        if self.max_real_runs < 0 or self.max_tool_reads < 0 or self.max_device_minutes < 0:
            raise ValueError("run, tool-read and device-minute budgets cannot be negative")
        for digest in self.sandbox_image_digests:
            if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
                raise ValueError("sandbox images must use lowercase sha256 digests")
        if self.sandbox_runtime_digest and re.fullmatch(r"sha256:[0-9a-f]{64}", self.sandbox_runtime_digest) is None:
            raise ValueError("sandbox runtime digest must use lowercase sha256")
        if bool(self.sandbox_image_digests or self.sandbox_runtime_digest) != bool(self.sandbox_runner_ids):
            raise ValueError("sandbox environment and runner allowlists must be configured together")
        if any(not runner.strip() for runner in self.sandbox_runner_ids):
            raise ValueError("sandbox runner IDs cannot be empty")
        if self.sandbox_backend not in {"disabled", "windows"}:
            raise ValueError("sandbox backend must be disabled or windows; rerun scripts/setup_sandbox.py to migrate Docker configuration")
        if self.sandbox_runtime != "psec":
            raise ValueError("Windows code execution requires psec; rerun scripts/setup_sandbox.py to migrate")
        if not 0 <= self.sandbox_max_repairs <= 3:
            raise ValueError("sandbox repairs must be between zero and three")
        if self.sandbox_backend == "windows":
            if not self.sandbox_runtime_digest or len(self.sandbox_runner_ids) != 1:
                raise ValueError("Windows execution requires one pinned runtime and runner")
            if self.sandbox_max_wall_seconds < 5 or self.sandbox_max_memory_mb < 64:
                raise ValueError("Windows execution requires wall >= 5 seconds and memory >= 64 MiB")
        sandbox_limits = {
            "sandbox_max_jobs_per_investigation": self.sandbox_max_jobs_per_investigation,
            "sandbox_max_cpu_seconds": self.sandbox_max_cpu_seconds,
            "sandbox_max_memory_mb": self.sandbox_max_memory_mb,
            "sandbox_max_process_count": self.sandbox_max_process_count,
            "sandbox_max_disk_mb": self.sandbox_max_disk_mb,
            "sandbox_max_wall_seconds": self.sandbox_max_wall_seconds,
            "sandbox_max_output_bytes": self.sandbox_max_output_bytes,
            "sandbox_max_program_bytes": self.sandbox_max_program_bytes,
        }
        if any(value < 1 for value in sandbox_limits.values()):
            raise ValueError("sandbox limits must be positive")
        if self.environment == "production":
            required = {
                "workflow_backend=temporal": self.workflow_backend == "temporal",
                "database_backend=mysql": self.database_backend == "mysql",
                "artifact_backend=s3": self.artifact_backend == "s3",
                "auth_mode=jwt": self.auth_mode == "jwt",
            }
            missing = [name for name, ready in required.items() if not ready]
            if missing:
                raise ValueError("production profile requires " + ", ".join(missing))
        if self.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("log level is invalid")

    @property
    def model_extra_body(self) -> dict:
        return json.loads(self.model_extra_body_json)

    @classmethod
    def from_env(cls) -> "Settings":
        legacy_providers = os.getenv("GTA_INTEGRATION_PROVIDERS_JSON", "[]").strip()
        if legacy_providers and legacy_providers != "[]":
            raise ValueError(
                "HTTP API providers are no longer supported; configure "
                "GTA_MCP_PROVIDERS_JSON with the platform MCP endpoint"
            )
        data_root = Path(os.getenv("GTA_DATA_ROOT", "data")).resolve()
        workflow_backend = os.getenv("GTA_WORKFLOW_BACKEND", "local").lower()
        if workflow_backend not in {"local", "temporal"}:
            raise ValueError("GTA_WORKFLOW_BACKEND must be 'local' or 'temporal'")
        artifact_backend = os.getenv("GTA_ARTIFACT_BACKEND", "local").lower()
        if artifact_backend not in {"local", "s3"}:
            raise ValueError("GTA_ARTIFACT_BACKEND must be 'local' or 's3'")
        s3_bucket = os.getenv("GTA_S3_BUCKET", "")
        if artifact_backend == "s3" and not s3_bucket:
            raise ValueError("GTA_S3_BUCKET is required for the S3 artifact backend")
        auth_mode = os.getenv("GTA_AUTH_MODE", "dev_headers").lower()
        if auth_mode not in {"dev_headers", "jwt"}:
            raise ValueError("GTA_AUTH_MODE must be 'dev_headers' or 'jwt'")
        jwt_jwks_url = os.getenv("GTA_JWT_JWKS_URL") or None
        jwt_issuer = os.getenv("GTA_JWT_ISSUER") or None
        jwt_audience = os.getenv("GTA_JWT_AUDIENCE") or None
        if auth_mode == "jwt" and not all([jwt_jwks_url, jwt_issuer, jwt_audience]):
            raise ValueError("JWT auth requires GTA_JWT_JWKS_URL/ISSUER/AUDIENCE")
        database_backend = os.getenv("GTA_DATABASE_BACKEND", "sqlite").lower()
        settings = cls(
            database_path=Path(
                os.getenv("GTA_DATABASE_PATH", str(data_root / "game_test_agent.db"))
            ).resolve(),
            artifact_root=Path(
                os.getenv("GTA_ARTIFACT_ROOT", str(data_root / "artifacts"))
            ).resolve(),
            environment=os.getenv("GTA_ENVIRONMENT", "development").lower(),
            database_backend=database_backend,
            mysql_host=os.getenv("GTA_MYSQL_HOST", "127.0.0.1"),
            mysql_port=int(os.getenv("GTA_MYSQL_PORT", "3306")),
            mysql_database=os.getenv("GTA_MYSQL_DATABASE", "game_test_agent"),
            mysql_user=os.getenv("GTA_MYSQL_USER", ""),
            mysql_password=os.getenv("GTA_MYSQL_PASSWORD", ""),
            mysql_ssl_ca=os.getenv("GTA_MYSQL_SSL_CA") or None,
            artifact_backend=artifact_backend,
            artifact_max_bytes=int(os.getenv("GTA_ARTIFACT_MAX_BYTES", str(128 * 1024 * 1024))),
            s3_bucket=s3_bucket,
            s3_prefix=os.getenv("GTA_S3_PREFIX", "game-test-agent"),
            s3_region=os.getenv("GTA_S3_REGION") or None,
            s3_endpoint_url=os.getenv("GTA_S3_ENDPOINT_URL") or None,
            s3_server_side_encryption=os.getenv("GTA_S3_SERVER_SIDE_ENCRYPTION", "AES256"),
            s3_kms_key_id=os.getenv("GTA_S3_KMS_KEY_ID") or None,
            model_name=os.getenv("GTA_MODEL", "gpt-5-mini"),
            enable_model=os.getenv("GTA_ENABLE_MODEL", "false").lower() in {"1", "true", "yes"},
            enable_semantic_router=os.getenv("GTA_ENABLE_SEMANTIC_ROUTER", os.getenv("GTA_ENABLE_MODEL", "false")).lower() in {"1", "true", "yes"},
            enable_router_recovery=os.getenv("GTA_ENABLE_ROUTER_RECOVERY", "true").lower() in {"1", "true", "yes"},
            router_timeout_seconds=float(os.getenv("GTA_ROUTER_TIMEOUT_SECONDS", "10")),
            model_api=os.getenv("GTA_MODEL_API", "responses").lower(),
            model_output_mode=os.getenv("GTA_MODEL_OUTPUT_MODE", "json_schema").lower(),
            model_extra_body_json=os.getenv("GTA_MODEL_EXTRA_BODY_JSON", "{}"),
            model_timeout_seconds=float(os.getenv("GTA_MODEL_TIMEOUT_SECONDS", "60")),
            model_context_window_tokens=int(os.getenv("GTA_MODEL_CONTEXT_WINDOW_TOKENS", "32768")),
            model_max_output_tokens=int(os.getenv("GTA_MODEL_MAX_OUTPUT_TOKENS", "4096")),
            model_max_total_tokens=int(os.getenv("GTA_MODEL_MAX_TOTAL_TOKENS", "250000")),
            model_max_estimated_cost=(float(os.environ["GTA_MODEL_MAX_ESTIMATED_COST"])
                                      if os.getenv("GTA_MODEL_MAX_ESTIMATED_COST") else None),
            model_cost_per_input_token=float(os.getenv("GTA_MODEL_COST_PER_INPUT_TOKEN", "0")),
            model_cost_per_output_token=float(os.getenv("GTA_MODEL_COST_PER_OUTPUT_TOKEN", "0")),
            model_reasoning_effort=os.getenv("GTA_MODEL_REASONING_EFFORT") or None,
            max_rounds=int(os.getenv("GTA_MAX_ROUNDS", "4")),
            max_real_runs=int(os.getenv("GTA_MAX_REAL_RUNS", "3")),
            max_tool_reads=int(os.getenv("GTA_MAX_TOOL_READS", "40")),
            max_device_minutes=float(os.getenv("GTA_MAX_DEVICE_MINUTES", "120")),
            max_wall_seconds=int(os.getenv("GTA_MAX_WALL_SECONDS", "7200")),
            max_concurrent_investigations=int(os.getenv("GTA_MAX_CONCURRENT_INVESTIGATIONS", "4")),
            max_concurrent_models=int(os.getenv("GTA_MAX_CONCURRENT_MODELS", "4")),
            max_concurrent_tools=int(os.getenv("GTA_MAX_CONCURRENT_TOOLS", "8")),
            max_concurrent_analyses=int(os.getenv("GTA_MAX_CONCURRENT_ANALYSES", "2")),
            max_queued_operations=int(os.getenv("GTA_MAX_QUEUED_OPERATIONS", "64")),
            temporal_address=os.getenv("GTA_TEMPORAL_ADDRESS", "127.0.0.1:7233"),
            temporal_namespace=os.getenv("GTA_TEMPORAL_NAMESPACE", "default"),
            temporal_task_queue=os.getenv("GTA_TEMPORAL_TASK_QUEUE", "game-test-agent"),
            rabbitmq_url=os.getenv("GTA_RABBITMQ_URL", "amqp://guest:guest@127.0.0.1/"),
            rabbitmq_exchange=os.getenv("GTA_RABBITMQ_EXCHANGE", "game_test.events.v1"),
            enable_local_event_bridge=os.getenv("GTA_ENABLE_LOCAL_EVENT_BRIDGE", "false").lower()
            in {"1", "true", "yes"},
            platform_id=os.getenv("GTA_PLATFORM_ID", "local"),
            workflow_backend=workflow_backend,
            dispatcher_poll_seconds=float(os.getenv("GTA_DISPATCHER_POLL_SECONDS", "1")),
            mcp_providers_json=os.getenv("GTA_MCP_PROVIDERS_JSON", "[]"),
            mcp_native_tools=os.getenv("GTA_MCP_NATIVE_TOOLS", "false").lower() in {"1", "true", "yes"},
            event_source_policies_json=os.getenv("GTA_EVENT_SOURCE_POLICIES_JSON", "[]"),
            api_host=os.getenv("GTA_API_HOST", "127.0.0.1"),
            api_port=int(os.getenv("GTA_API_PORT", "8080")),
            auth_mode=auth_mode,
            jwt_jwks_url=jwt_jwks_url,
            jwt_issuer=jwt_issuer,
            jwt_audience=jwt_audience,
            jwt_leeway_seconds=int(os.getenv("GTA_JWT_LEEWAY_SECONDS", "30")),
            jwt_jwks_cache_seconds=int(os.getenv("GTA_JWT_JWKS_CACHE_SECONDS", "300")),
            jwt_jwks_timeout_seconds=int(os.getenv("GTA_JWT_JWKS_TIMEOUT_SECONDS", "5")),
            allow_insecure_dev_headers=os.getenv("GTA_ALLOW_INSECURE_DEV_HEADERS", "false").lower()
            in {"1", "true", "yes"},
            sandbox_image_digests=tuple(
                item.strip()
                for item in os.getenv("GTA_SANDBOX_IMAGE_DIGESTS", "").split(",")
                if item.strip()
            ),
            sandbox_runner_ids=tuple(
                item.strip()
                for item in os.getenv("GTA_SANDBOX_RUNNER_IDS", "").split(",")
                if item.strip()
            ),
            sandbox_max_jobs_per_investigation=int(
                os.getenv("GTA_SANDBOX_MAX_JOBS_PER_INVESTIGATION", "4")
            ),
            sandbox_backend=os.getenv("GTA_SANDBOX_BACKEND", "disabled"),
            sandbox_runtime=os.getenv("GTA_SANDBOX_RUNTIME", "psec"),
            sandbox_runtime_digest=os.getenv("GTA_SANDBOX_RUNTIME_DIGEST", ""),
            sandbox_approval_key_file=Path(os.environ["GTA_SANDBOX_APPROVAL_KEY_FILE"]).resolve()
            if os.getenv("GTA_SANDBOX_APPROVAL_KEY_FILE") else None,
            sandbox_python_path=Path(os.environ["GTA_SANDBOX_PYTHON_PATH"]).resolve()
            if os.getenv("GTA_SANDBOX_PYTHON_PATH") else None,
            sandbox_runner_root=Path(os.environ["GTA_SANDBOX_RUNNER_ROOT"]).resolve()
            if os.getenv("GTA_SANDBOX_RUNNER_ROOT") else None,
            sandbox_max_repairs=int(os.getenv("GTA_SANDBOX_MAX_REPAIRS", "2")),
            sandbox_max_cpu_seconds=int(os.getenv("GTA_SANDBOX_MAX_CPU_SECONDS", "120")),
            sandbox_max_memory_mb=int(os.getenv("GTA_SANDBOX_MAX_MEMORY_MB", "1024")),
            sandbox_max_process_count=int(os.getenv("GTA_SANDBOX_MAX_PROCESS_COUNT", "16")),
            sandbox_max_disk_mb=int(os.getenv("GTA_SANDBOX_MAX_DISK_MB", "512")),
            sandbox_max_wall_seconds=int(os.getenv("GTA_SANDBOX_MAX_WALL_SECONDS", "180")),
            sandbox_max_output_bytes=int(
                os.getenv("GTA_SANDBOX_MAX_OUTPUT_BYTES", str(32 * 1024 * 1024))
            ),
            sandbox_max_program_bytes=int(
                os.getenv("GTA_SANDBOX_MAX_PROGRAM_BYTES", str(1024 * 1024))
            ),
            log_level=os.getenv("GTA_LOG_LEVEL", "INFO"),
        )
        settings.validate()
        return settings
