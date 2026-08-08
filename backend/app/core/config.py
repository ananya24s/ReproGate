"""Application-wide configuration loaded from the environment."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the ReproGate backend.

    Values are read from the process environment and, in local development,
    from a ``.env`` file at the backend root. See ``.env.example``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- Application ----------------------------------------------------
    app_name: str = "ReproGate"
    environment: Literal["local", "development", "staging", "production"] = "local"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"

    # -- Server ---------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000

    # -- Logging --------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    # -- CORS -----------------------------------------------------------
    # Vite serves on 5173; localhost and 127.0.0.1 are distinct browser origins.
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
    )

    # -- Persistence ----------------------------------------------------
    database_url: PostgresDsn = Field(
        default="postgresql+asyncpg://reprogate:reprogate@localhost:5432/reprogate"
    )
    database_echo: bool = False
    database_pool_size: int = 5
    database_max_overflow: int = 10
    database_pool_timeout: int = 30
    database_pool_recycle: int = 1800

    # -- GitHub ---------------------------------------------------------
    github_api_url: str = "https://api.github.com"
    github_token: str | None = None
    github_request_timeout_seconds: int = 30
    github_max_retries: int = 3
    github_retry_backoff_seconds: float = 0.5
    # -- Repository cloning ---------------------------------------------
    git_binary: str = "git"
    clone_shallow: bool = True
    clone_depth: int = 1
    clone_timeout_seconds: int = 300

    # -- Repository analysis ----------------------------------------------
    analysis_max_files: int = 50_000
    analysis_max_depth: int = 20
    # Manifests (package.json, lockfiles) are small; anything larger is not one.
    analysis_max_manifest_bytes: int = 2 * 1024 * 1024

    # -- Relevant file retrieval -------------------------------------------
    retrieval_max_candidates: int = 50
    retrieval_max_keywords: int = 40
    retrieval_max_seed_files: int = 25
    retrieval_traversal_depth: int = 2
    # Import edges are parsed for at most this many source files.
    retrieval_max_graph_files: int = 5_000
    retrieval_max_source_bytes: int = 512 * 1024
    # Rate-limit resets can be far in the future; never block a request longer
    # than this waiting for one.
    github_max_retry_wait_seconds: float = 30.0

    # -- LLM ------------------------------------------------------------
    llm_provider: Literal["openai"] = "openai"
    llm_model: str = "gpt-4o"
    llm_request_timeout_seconds: int = 120
    openai_api_key: str | None = None
    # Zero temperature keeps replies as repeatable as the provider allows;
    # parsing and validation are deterministic regardless.
    llm_temperature: float = 0.0
    llm_max_output_tokens: int = 4096
    llm_max_retries: int = 2

    # -- Issue analysis -----------------------------------------------------
    issue_analysis_body_char_limit: int = 20_000

    # -- Reproduction test generation ---------------------------------------
    test_generation_max_context_files: int = 12
    test_generation_snippet_char_limit: int = 4_000
    test_generation_context_char_limit: int = 60_000

    # -- Sandbox --------------------------------------------------------
    docker_host: str | None = None
    sandbox_timeout_seconds: int = 300
    """Wall-clock limit for the test phase."""

    sandbox_install_timeout_seconds: int = 600
    sandbox_memory_limit_mb: int = 2048
    sandbox_cpu_limit: float = 1.0
    sandbox_pids_limit: int = 512
    sandbox_network_enabled: bool = False
    """Whether the test phase keeps network access. Install always needs it."""

    sandbox_workspace_root: str = "/tmp/reprogate"
    sandbox_workspace_container_path: str = "/workspace"
    sandbox_node_image: str = "node:20-bookworm-slim"
    sandbox_pull_missing_image: bool = True
    sandbox_read_only_rootfs: bool = False
    sandbox_tmpfs_size_mb: int = 256
    sandbox_run_as_user: str | None = None
    """``uid:gid`` for the container. ``None`` derives it from the host user."""

    sandbox_max_output_bytes: int = 1_000_000

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: object) -> object:
        """Accept a comma-separated string so the value is easy to set via env."""
        if isinstance(value, str) and not value.startswith("["):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
