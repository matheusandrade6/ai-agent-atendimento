"""Agregacao por debounce das mensagens de uma conversa (secao 14.1.2).

O problema
----------
Uma pessoa manda "oi", "queria marcar", "pra sexta" em treze segundos. Sao tres
webhooks. Responder tres vezes e o comportamento errado — e responder duas ja e
errado. Cada mensagem reinicia um timer de `debounce_seconds`; quando o timer expira
sem mensagem nova, o buffer vira **um** turno com os textos concatenados na ordem.

A corrida que precisa ser fechada
---------------------------------
O disparo e a chegada de mensagem sao concorrentes por natureza: o job diferido acorda
no mesmo instante em que o quarto balao entra. Duas coisas nao podem acontecer:

1. **Dois turnos para a mesma rajada.** Garantido pelo *drain* atomico: o Lua le e
   apaga a lista numa operacao so, entao so um chamador leva as partes. Um segundo
   disparo com o mesmo `expected_seq` encontra a lista vazia e nao dispara nada.
2. **Perder a ultima mensagem.** Garantido pelo contador `seq`, que so cresce. Quem
   dispara declara qual sequencia esperava ver; se outra mensagem entrou depois, o
   contador avancou, o *drain* recusa, e o disparo daquela mensagem — que e mais novo —
   e quem leva a rajada inteira, ja com a mensagem nova dentro. O timer reiniciou, que
   e exatamente o que a 14.1.2 pede.

Como os dois scripts rodam como Lua no Redis, nao existe janela entre "conferir" e
"apagar". O ordenamento vira: ou a mensagem entra antes do disparo (e vai junto), ou
depois (e abre a proxima rajada). Nunca no meio.

Reentrega e idempotencia
------------------------
O conjunto `agg:{id}:seen` guarda os ids ja bufferizados. Um job reentregue pelo arq
(worker morto entre o buffer e o agendamento) nao duplica a parte, mas ainda reagenda o
disparo — se nao reagendasse, a ultima mensagem da rajada poderia ficar parada no
buffer ate a proxima mensagem chegar.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import cast

from redis.asyncio import Redis

from app.workers.queue import InboundMessage

#: Lista com as partes da rajada, em ordem de chegada.
BUFFER_KEY = "agg:{conversation_id}"
#: Contador monotonico da conversa. Nao e apagado no drain: e ele que distingue
#: "ninguem chegou depois de mim" de "chegou".
SEQ_KEY = "agg:{conversation_id}:seq"
#: Ids ja bufferizados, para a reentrega nao duplicar a parte.
SEEN_KEY = "agg:{conversation_id}:seen"

# KEYS: buffer, seq, seen | ARGV: parte json, message_id, ttl
_APPEND_LUA = """
local added = redis.call('SADD', KEYS[3], ARGV[2])
local seq
if added == 1 then
  seq = redis.call('INCR', KEYS[2])
  redis.call('RPUSH', KEYS[1], ARGV[1])
else
  seq = tonumber(redis.call('GET', KEYS[2]) or '0')
end
redis.call('EXPIRE', KEYS[1], ARGV[3])
redis.call('EXPIRE', KEYS[2], ARGV[3])
redis.call('EXPIRE', KEYS[3], ARGV[3])
return {seq, added}
"""

# KEYS: buffer, seq | ARGV: sequencia esperada
_DRAIN_LUA = """
local seq = redis.call('GET', KEYS[2])
if not seq or tonumber(seq) ~= tonumber(ARGV[1]) then
  return false
end
local items = redis.call('LRANGE', KEYS[1], 0, -1)
redis.call('DEL', KEYS[1])
return items
"""


@dataclass(frozen=True, slots=True)
class AppendResult:
    """`seq` e a sequencia a esperar no disparo. `buffered=False` e reentrega."""

    seq: int
    buffered: bool


@dataclass(frozen=True, slots=True)
class AggregatedTurn:
    """Uma rajada fechada, pronta para virar um turno do agente (11.1, passo 1)."""

    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    contact_id: uuid.UUID
    parts: tuple[InboundMessage, ...]

    @property
    def text(self) -> str:
        """Textos na ordem de chegada. Midia sem legenda nao contribui com texto."""
        return "\n".join(part.text for part in self.parts if part.text)

    @property
    def last_provider_msg_id(self) -> str | None:
        for part in reversed(self.parts):
            if part.provider_msg_id:
                return part.provider_msg_id
        return None


class ConversationAggregator:
    """Buffer de rajada por conversa, em Redis.

    O TTL protege contra vazamento se um disparo se perder (worker morto entre o
    agendamento e a execucao): o buffer expira em vez de crescer para sempre. Ele
    precisa ser confortavelmente maior que o maior `debounce_seconds` configuravel
    (60s, ver `WhatsAppChannel.debounce_seconds`) — a proxima mensagem da conversa
    reabre o buffer de qualquer jeito.
    """

    def __init__(self, redis: Redis, *, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds
        self._append = redis.register_script(_APPEND_LUA)
        self._drain = redis.register_script(_DRAIN_LUA)

    async def append(self, message: InboundMessage) -> AppendResult:
        raw = await self._append(
            keys=self._keys(message.conversation_id),
            args=[
                message.model_dump_json(),
                str(message.message_id),
                self._ttl,
            ],
        )
        seq, added = int(raw[0]), int(raw[1])
        return AppendResult(seq=seq, buffered=added == 1)

    async def drain(
        self, conversation_id: uuid.UUID, expected_seq: int
    ) -> tuple[InboundMessage, ...] | None:
        """Fecha a rajada, ou devolve `None` se o timer foi reiniciado por outra mensagem."""
        raw = await self._drain(keys=self._keys(conversation_id)[:2], args=[expected_seq])
        if raw is None:
            return None
        return _decode_parts(raw)

    async def pending(self, conversation_id: uuid.UUID) -> int:
        """Quantas partes estao no buffer agora. So para teste e diagnostico."""
        key = BUFFER_KEY.format(conversation_id=conversation_id)
        # `redis-py` tipa os comandos como sincronos ou assincronos conforme o cliente;
        # no cliente async o retorno e sempre awaitable.
        return await cast("Awaitable[int]", self._redis.llen(key))

    @staticmethod
    def _keys(conversation_id: uuid.UUID) -> list[str]:
        cid = str(conversation_id)
        return [
            BUFFER_KEY.format(conversation_id=cid),
            SEQ_KEY.format(conversation_id=cid),
            SEEN_KEY.format(conversation_id=cid),
        ]


def _decode_parts(raw: list[bytes | str]) -> tuple[InboundMessage, ...]:
    """Segunda barreira contra duplicata: mesmo id no buffer entra uma vez so."""
    parts: list[InboundMessage] = []
    seen: set[uuid.UUID] = set()
    for item in raw:
        part = InboundMessage.model_validate_json(item)
        if part.message_id in seen:
            continue
        seen.add(part.message_id)
        parts.append(part)
    return tuple(parts)
