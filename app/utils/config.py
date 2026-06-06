"""
Configuration loaded from environment variables or a .env file.

Using pydantic-settings means:
  1. All config is type-checked at startup — typos in env var names
     surface immediately rather than causing silent wrong behaviour.
  2. You get a single source of truth: no scattered os.getenv() calls.
  3. Defaults are documented here, not buried in call sites.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Docker
    docker_url: str = ""  # empty = use DOCKER_HOST env var or default socket

    # Database
    db_path: str = "jobs.db"

    # Scheduler
    poll_interval_seconds: float = 1.0
    max_concurrent_jobs: int = 4

    # Logging
    log_level: str = "INFO"

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
