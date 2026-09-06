"""O `ctx` que o `on_startup` monta (Sessao S05).

Nenhum job constroi infraestrutura sozinho — todos pegam do `ctx`. Se o `on_startup`
deixar de por uma peca ali, o job quebra so quando roda, em producao. Este teste fixa o
contrato que a S06 vai herdar quando instalar o motor do agente em `ctx["turn_handler"]`.
"""

from __future__ import annotations

from typing import Any

import pytest
from redis.asyncio import Redis

from app.core.config import Settings
from app.domain.tenant_config import TenantConfig
from app.workers.base import (
    WorkerContext,
    channel_for,
    ctx_aggregator,
    ctx_outbound,
    ctx_rate_limiter,
    ctx_settings,
    on_shutdown,
    on_startup,
)

pytestmark = pytest.mark.integration


async def test_startup_monta_o_contexto_e_shutdown_fecha(redis_client: Redis) -> None:
    ctx: WorkerContext = {"redis": redis_client}

    await on_startup(ctx)
    try:
        assert isinstance(ctx_settings(ctx), Settings)
        assert ctx_aggregator(ctx) is not None, "sem agregador nao ha debounce"
        assert ctx_rate_limiter(ctx) is not None, "sem rate limit nao ha anti-flood"
        assert ctx_outbound(ctx) is not None, "sem produtor de saida a resposta nao sai"
        assert ctx["http"] is not None
    finally:
        await on_shutdown(ctx)

    assert ctx["http"].is_closed


async def test_sem_token_configurado_nao_ha_canal_de_saida(
    redis_client: Redis, tenant_config: dict[str, Any]
) -> None:
    """Em desenvolvimento nao ha token: o worker roda e o envio simplesmente nao existe."""
    ctx: WorkerContext = {"redis": redis_client}
    await on_startup(ctx)
    ctx["settings"] = ctx_settings(ctx).model_copy(update={"whatsapp_access_token": ""})
    try:
        assert channel_for(ctx, TenantConfig.model_validate(tenant_config).resolved()) is None
    finally:
        await on_shutdown(ctx)


async def test_com_token_o_canal_aponta_para_o_numero_do_tenant(
    redis_client: Redis, tenant_config: dict[str, Any]
) -> None:
    """O que separa um tenant do outro no envio e o `phone_number_id` da config (D-16)."""
    ctx: WorkerContext = {"redis": redis_client}
    await on_startup(ctx)
    ctx["settings"] = ctx_settings(ctx).model_copy(update={"whatsapp_access_token": "token"})
    config = TenantConfig.model_validate(tenant_config).resolved()
    try:
        channel = channel_for(ctx, config)
        assert channel is not None
        assert config.channels.whatsapp is not None
        assert config.channels.whatsapp.phone_number_id in channel.endpoint
    finally:
        await on_shutdown(ctx)
