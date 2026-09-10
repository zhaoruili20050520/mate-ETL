"""Application configuration loaded from environment variables.

All config values are read here via Pydantic Settings. Other modules
should import ``settings`` from this file rather than calling os.environ directly.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Central configuration for ETL-Agent."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ---- Environment ----
    env: str = Field(default="development", alias="ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # ---- PostgreSQL ----
    postgres_host: str = Field(default="localhost", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    postgres_user: str = Field(default="etl_agent", alias="POSTGRES_USER")
    postgres_password: str = Field(default="changeme", alias="POSTGRES_PASSWORD")
    postgres_db: str = Field(default="etl_agent", alias="POSTGRES_DB")
    database_url: str = Field(
        default="postgresql+psycopg://etl_agent:changeme@localhost:5432/etl_agent",
        alias="DATABASE_URL",
    )

    # ---- Redis ----
    redis_host: str = Field(default="localhost", alias="REDIS_HOST")
    redis_port: int = Field(default=6379, alias="REDIS_PORT")
    redis_db: int = Field(default=0, alias="REDIS_DB")

    # ---- Celery ----
    celery_broker_url: str = Field(default="redis://localhost:6379/1", alias="CELERY_BROKER_URL")
    celery_result_backend: str = Field(
        default="redis://localhost:6379/2", alias="CELERY_RESULT_BACKEND"
    )

    # ---- SeaTunnel ----
    seatunnel_host: str = Field(default="localhost", alias="SEATUNNEL_HOST")
    seatunnel_port: int = Field(default=5801, alias="SEATUNNEL_PORT")
    seatunnel_api_version: str = Field(default="legacy", alias="SEATUNNEL_API_VERSION")

    # ---- Vault ----
    vault_addr: str = Field(default="http://localhost:8200", alias="VAULT_ADDR")
    vault_token: str = Field(default="root", alias="VAULT_TOKEN")
    vault_secret_path: str = Field(default="etl-agent", alias="VAULT_SECRET_PATH")

    # ---- Capability signing key (share across API and workers) ----
    capability_private_key_pem: str = Field(
        default="",
        alias="CAPABILITY_PRIVATE_KEY_PEM",
    )

    # ---- JWT ----
    jwt_secret_key: str = Field(default="change-this-to-a-random-string", alias="JWT_SECRET_KEY")
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    jwt_expire_minutes: int = Field(default=480, alias="JWT_EXPIRE_MINUTES")

    # ---- LLM ----
    model_api_key: str = Field(default="", alias="MODEL_API_KEY")
    model_api_base: str = Field(
        default="https://aip.baidubce.com/rpc/2.0/ai_custom/v1",
        alias="MODEL_API_BASE",
    )
    model_name: str = Field(default="ernie-4.0-8k", alias="MODEL_NAME")

    # ---- Local knowledge Q&A model (Ollama / OpenAI-compatible) ----
    knowledge_llm_base_url: str = Field(
        default="http://127.0.0.1:11434/v1",
        alias="KNOWLEDGE_LLM_BASE_URL",
    )
    knowledge_llm_model: str = Field(
        default="qwen2.5-coder:7b",
        alias="KNOWLEDGE_LLM_MODEL",
    )
    knowledge_llm_api_key: str = Field(
        default="ollama",
        alias="KNOWLEDGE_LLM_API_KEY",
    )

    # ---- FastAPI ----
    api_v1_prefix: str = Field(default="/api/v1", alias="API_V1_PREFIX")

    # ---- Derived properties ----
    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def seatunnel_base_url(self) -> str:
        return f"http://{self.seatunnel_host}:{self.seatunnel_port}"

    @property
    def is_dev(self) -> bool:
        return self.env == "development"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()


settings = get_settings()
