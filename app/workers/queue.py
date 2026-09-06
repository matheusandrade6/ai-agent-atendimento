"""Produtor da fila de entrada (secao 14.1.1 / 14.1.2).

O webhook so enfileira e responde — nunca processa o turno inline (RNF-04: nenhuma
indisponibilidade do LLM pode gerar perda de mensagem). O worker que consome
`INBOUND_JOB_NAME`, com a agregacao por debounce, e construido na Sessao S05; este
modulo define so o contrato do lado do produtor.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from app.core.config import Settings
from app.core.telemetry import get_logger

log = get_logger(__name__)

#: Nome do job que o worker inbound (S05) registra para consumir esta fila.
INBOUND_JOB_NAME = "process_inbound"


class InboundQueue(Protocol):
    async def enqueue_inbound(
        self, *, tenant_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> None: ...

    async def close(self) -> None: ...


class ArqInboundQueue:
    """Implementacao real, sobre o pool de Redis do arq.

    Uma falha ao enfileirar nunca derruba o webhook: a mensagem ja esta persistida
    antes deste ponto. Por isso a conexao tenta uma unica vez — sem o backoff padrao
    de 5 tentativas do arq, que sozinho ja estouraria o orcamento de <1s do webhook
    se o Redis estiver fora. Reconectar fica para a proxima chamada.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._redis: ArqRedis | None = None

    async def _pool(self) -> ArqRedis:
        if self._redis is None:
            redis_settings = RedisSettings.from_dsn(str(self._settings.redis_url))
            redis_settings.conn_retries = 0
            redis_settings.conn_timeout = 1
            self._redis = await create_pool(redis_settings)
        return self._redis

    async def enqueue_inbound(self, *, tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
        try:
            redis = await self._pool()
            await redis.enqueue_job(
                INBOUND_JOB_NAME,
                tenant_id=str(tenant_id),
                conversation_id=str(conversation_id),
            )
        except Exception:
            log.exception(
                "whatsapp_enfileiramento_falhou",
                conversation_id=str(conversation_id),
            )

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.close()
            self._redis = None
