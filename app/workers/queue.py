"""Contratos e produtores das filas (secoes 7.1, 14.1.2 e 14.1.3).

Duas filas, duas politicas de falha opostas — e a diferenca importa:

- **Entrada.** O webhook so enfileira; falhar ao enfileirar nunca pode derrubar a
  resposta 200 (RNF-04), porque a mensagem ja esta persistida. O erro e logado e o
  turno atrasa, nao se perde.
- **Saida.** Quem enfileira e um worker, nao um request. Engolir a falha aqui perderia
  a resposta ao cliente em silencio, entao a excecao sobe e o arq retenta o job.

O payload dos dois jobs e um `dict` JSON, nao um objeto: produtor e consumidor sao
processos separados que podem estar em versoes diferentes durante um deploy. Pela mesma
razao os modelos ignoram campo desconhecido em vez de recusa-lo — durante um deploy a
API pode subir antes do worker, e um campo novo no payload nao pode derrubar todo job em
voo. Isso vale para contrato de fila; para a config de tenant, campo desconhecido
continua sendo erro.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal, Protocol

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings
from pydantic import BaseModel, ConfigDict

from app.core.config import Settings
from app.core.telemetry import get_logger

log = get_logger(__name__)

#: Job que recebe uma mensagem recem-persistida e a joga no buffer de agregacao.
INBOUND_JOB_NAME = "process_inbound"
#: Job diferido que fecha a rajada e dispara o turno do agente (14.1.2).
FLUSH_JOB_NAME = "flush_conversation"
#: Job que entrega uma resposta ao canal (14.1.3).
OUTBOUND_JOB_NAME = "send_outbound"

Channel = Literal["whatsapp", "web"]


class InboundMessage(BaseModel):
    """Uma mensagem ja persistida, a caminho do buffer de agregacao.

    Carrega o conteudo em vez de so o id porque o worker de entrada nao tem uma coluna
    de "ja processada" para reler do banco — o buffer em Redis e que sabe o que ainda
    nao virou turno (docs/DECISOES.md, D-14).
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    contact_id: uuid.UUID
    channel: Channel
    message_id: uuid.UUID
    provider_msg_id: str | None = None
    content_type: str = "text"
    text: str | None = None
    media_ref: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class OutboundMessage(BaseModel):
    """Uma resposta a entregar.

    `idempotency_key` e do turno que gerou a resposta, nao do envio: se o job for
    reentregue, o worker reconhece que aquele texto ja saiu e nao manda de novo
    (invariante 5).
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    text: str
    idempotency_key: str

    def as_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class InboundQueue(Protocol):
    async def enqueue_inbound(self, message: InboundMessage) -> None: ...

    async def close(self) -> None: ...


class OutboundQueue(Protocol):
    async def enqueue_outbound(self, message: OutboundMessage) -> None: ...

    async def close(self) -> None: ...


class _ArqPool:
    """Pool preguicoso de Redis, compartilhado pelos dois produtores."""

    def __init__(self, settings: Settings, *, fast_fail: bool = False) -> None:
        self._settings = settings
        self._fast_fail = fast_fail
        self._redis: ArqRedis | None = None

    async def pool(self) -> ArqRedis:
        if self._redis is None:
            self._redis = await create_pool(
                redis_settings_for(self._settings, fast_fail=self._fast_fail)
            )
        return self._redis

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.close()
            self._redis = None


def redis_settings_for(settings: Settings, *, fast_fail: bool = False) -> RedisSettings:
    """Traduz a `REDIS_URL` para o formato do arq.

    Com `fast_fail`, desliga o backoff padrao de 5 tentativas: no webhook, tentar
    reconectar cinco vezes ja estoura sozinho o orcamento de <1s (14.1.1).
    """
    redis_settings = RedisSettings.from_dsn(str(settings.redis_url))
    if fast_fail:
        redis_settings.conn_retries = 0
        redis_settings.conn_timeout = 1
    return redis_settings


class ArqInboundQueue:
    """Produtor da fila de entrada, usado pelo webhook.

    A conexao tenta uma unica vez (`fast_fail`): a mensagem ja esta no banco antes
    deste ponto, entao Redis fora significa turno atrasado, nunca 5xx para a Meta —
    que reentregaria e multiplicaria o problema.
    """

    def __init__(self, settings: Settings) -> None:
        self._pool = _ArqPool(settings, fast_fail=True)

    async def enqueue_inbound(self, message: InboundMessage) -> None:
        try:
            redis = await self._pool.pool()
            await redis.enqueue_job(INBOUND_JOB_NAME, message.as_payload())
        except Exception:
            log.exception(
                "inbound_enfileiramento_falhou",
                conversation_id=str(message.conversation_id),
                message_id=str(message.message_id),
            )

    async def close(self) -> None:
        await self._pool.close()


class ArqOutboundQueue:
    """Produtor da fila de saida.

    Dentro de um worker, recebe o pool que o arq ja abriu (`ctx["redis"]`) em vez de
    abrir o seu — um processo, uma conexao. Fora dele (painel, scripts), recebe as
    settings e cuida do proprio pool.
    """

    def __init__(self, settings: Settings | None = None, *, redis: ArqRedis | None = None) -> None:
        if redis is None and settings is None:
            raise ValueError("informe `settings` ou um pool `redis` ja aberto")
        self._borrowed = redis
        self._pool = _ArqPool(settings) if redis is None and settings is not None else None

    async def _redis(self) -> ArqRedis:
        if self._borrowed is not None:
            return self._borrowed
        if self._pool is None:
            raise RuntimeError("produtor de saida sem pool de Redis")
        return await self._pool.pool()

    async def enqueue_outbound(self, message: OutboundMessage) -> None:
        redis = await self._redis()
        await redis.enqueue_job(OUTBOUND_JOB_NAME, message.as_payload())

    async def close(self) -> None:
        """Fecha so o que e nosso: o pool emprestado pertence a quem o abriu."""
        if self._borrowed is None and self._pool is not None:
            await self._pool.close()
