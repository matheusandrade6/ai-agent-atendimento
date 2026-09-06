"""Settings da aplicacao (RNF-05, RNF-08).

Segredos vem sempre do ambiente ou do secret manager, nunca de YAML versionado.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- ambiente ---
    environment: Environment = "local"
    debug: bool = False
    log_level: str = "INFO"

    # --- infraestrutura ---
    database_url: PostgresDsn = Field(
        default=PostgresDsn("postgresql+asyncpg://datamind:datamind@localhost:5433/datamind")
    )
    redis_url: RedisDsn = Field(default=RedisDsn("redis://localhost:6380/0"))
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_echo: bool = False

    # --- config de tenants ---
    tenants_config_dir: str = "config/tenants"

    # --- LLM ---
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-5"
    llm_extraction_model: str = "claude-haiku-4-5-20251001"
    llm_max_tokens: int = 1024

    # --- canais ---
    whatsapp_app_secret: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_api_version: str = "v21.0"

    # --- seguranca ---
    secret_key: str = "dev-only-change-me"
    slot_token_secret: str = "dev-only-change-me"

    # --- limites globais (teto sobre o que o tenant configura) ---
    max_tool_iterations: int = 6
    default_debounce_seconds: int = 6

    # --- observabilidade ---
    otel_enabled: bool = False
    otel_service_name: str = "datamind-agenda"
    otel_exporter_otlp_endpoint: str = ""

    def check_production_ready(self) -> None:
        """Falha cedo se um segredo de desenvolvimento vazar para producao."""
        if self.environment != "production":
            return
        placeholders = {
            "secret_key": self.secret_key,
            "slot_token_secret": self.slot_token_secret,
        }
        bad = [name for name, value in placeholders.items() if value.startswith("dev-only")]
        if bad:
            raise RuntimeError(f"segredos de desenvolvimento em producao: {', '.join(bad)}")
        if not self.anthropic_api_key:
            raise RuntimeError("anthropic_api_key ausente em producao")

    @property
    def sync_database_url(self) -> str:
        """URL psycopg/sincrona — usada pelo Alembic e por testes de integracao."""
        return str(self.database_url).replace("+asyncpg", "")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
