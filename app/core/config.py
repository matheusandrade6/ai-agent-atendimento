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
    # A aplicacao conecta com um papel SEM privilegio. Superusuario ignora RLS por
    # completo — inclusive com FORCE — e o isolamento entre tenants viraria decoracao
    # (RNF-03; ver docs/DECISOES.md, D-09).
    database_url: PostgresDsn = Field(
        default=PostgresDsn(
            "postgresql+asyncpg://datamind_app:datamind_app@localhost:5433/datamind"
        )
    )
    # Dono das tabelas. Usado apenas pelas migrations, nunca para servir request.
    database_admin_url: PostgresDsn = Field(
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
    llm_extraction_model: str = "claude-haiku-4-5"
    llm_max_tokens: int = 1024
    # Pensamento adaptativo ligado, esforco baixo: o agente decide o que perguntar e
    # quando chamar tool (decisao que se beneficia de raciocinio) sob um teto de custo
    # por conversa medido em centavos (docs/DECISOES.md, D-20).
    llm_thinking: bool = True
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"

    # --- canais ---
    whatsapp_app_secret: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_api_version: str = "v21.0"
    # Token de sistema da Business Manager. Um token cobre varios WABAs; o que separa
    # um tenant do outro no envio e o `phone_number_id` da config (docs/DECISOES.md, D-16).
    whatsapp_access_token: str = ""
    whatsapp_api_base_url: str = "https://graph.facebook.com"

    # --- seguranca ---
    secret_key: str = "dev-only-change-me"
    slot_token_secret: str = "dev-only-change-me"

    # --- limites globais (teto sobre o que o tenant configura) ---
    max_tool_iterations: int = 6
    default_debounce_seconds: int = 6

    # --- filas e workers ---
    # TTL do buffer de agregacao. Precisa ser confortavelmente maior que o maior
    # `debounce_seconds` configuravel (60s): ele so existe para o buffer nao vazar
    # quando um disparo se perde (secao 14.1.2).
    aggregation_ttl_seconds: int = 900
    # Anti-flood por contato, contado em turnos ja agregados (secao 11.5).
    contact_rate_limit_max_turns: int = 12
    contact_rate_limit_window_seconds: int = 60
    # Backoff exponencial da fila de saida (secao 14.1.3).
    outbound_max_tries: int = 5
    outbound_backoff_base_seconds: float = 2.0
    outbound_backoff_max_seconds: float = 300.0
    # Janela de idempotencia do envio: uma resposta com a mesma chave nao sai duas vezes.
    outbound_idempotency_ttl_seconds: int = 86400
    worker_max_jobs: int = 10
    worker_job_timeout_seconds: int = 120
    worker_keep_result_seconds: int = 3600

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
        """URL sincrona do papel de aplicacao — usada nos testes de integracao."""
        return str(self.database_url).replace("+asyncpg", "")

    @property
    def sync_admin_database_url(self) -> str:
        """URL sincrona do dono das tabelas — usada pelo Alembic."""
        return str(self.database_admin_url).replace("+asyncpg", "")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
