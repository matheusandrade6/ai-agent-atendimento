"""Roteamento multi-tenant do webhook: `phone_number_id` -> tenant (secao 14.1.1).

Antes de saber qual e o tenant nao ha `tenant_id` para abrir uma `tenant_session` — por
isso a busca usa `bypass_rls_session()`, a valvula de escape documentada em
`app.core.db` exatamente para este caso.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.core.config import Settings
from app.core.db import bypass_rls_session
from app.core.telemetry import get_logger
from app.domain.tenant_config import TenantConfig

log = get_logger(__name__)


async def resolve_tenant_by_phone_number_id(
    phone_number_id: str, settings: Settings | None = None
) -> uuid.UUID | None:
    """Varre os tenants ativos e resolve a config de cada um ate achar o dono do numero.

    Sem cache: o volume de tenants em v1 nao justifica a complexidade de invalidacao
    dentro do orcamento de <1s do webhook. Revisitar se o numero de tenants ativos
    crescer o bastante para pesar (docs/DECISOES.md, D-12).
    """
    async with bypass_rls_session(settings) as session:
        rows = (
            await session.execute(sa.text("SELECT id, config FROM tenants WHERE status = 'active'"))
        ).all()

    for row in rows:
        try:
            config = TenantConfig.model_validate(row.config).resolved()
        except Exception:
            log.warning("tenant_config_invalida_no_roteamento", tenant_id=str(row.id))
            continue
        whatsapp = config.channels.whatsapp
        if (
            whatsapp is not None
            and whatsapp.enabled
            and whatsapp.phone_number_id == phone_number_id
        ):
            tenant_id: uuid.UUID = row.id
            return tenant_id
    return None
