"""Rate limit por contato (secao 11.5, guardrail de entrada).

Anti-flood, nao anti-abuso de infraestrutura: o alvo e a pessoa (ou o bot) que manda
rajada atras de rajada e faria o agente gastar LLM em looping. Por isso a contagem e de
**turnos**, depois da agregacao — uma rajada de dez baloes ja e um turno so.

Janela fixa com `INCR` + `EXPIRE`, em Lua para que o primeiro incremento e o TTL
acontecam juntos. Um contador sem TTL, se o processo morresse entre as duas chamadas,
travaria o contato para sempre.

A janela fixa deixa passar ate 2x o limite na virada (fim de uma janela + inicio da
seguinte). Para anti-flood isso e irrelevante e custa um contador em vez de um sorted
set por contato.
"""

from __future__ import annotations

import uuid

from redis.asyncio import Redis

#: Contador por contato — nao por conversa: a mesma pessoa no WhatsApp e no widget
#: continua sendo um contato so.
RATE_KEY = "rl:turn:{tenant_id}:{contact_id}"

_INCR_LUA = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return n
"""


class ContactRateLimiter:
    def __init__(self, redis: Redis, *, max_turns: int, window_seconds: int) -> None:
        self._max_turns = max_turns
        self._window = window_seconds
        self._incr = redis.register_script(_INCR_LUA)

    async def allow(self, *, tenant_id: uuid.UUID, contact_id: uuid.UUID) -> bool:
        key = RATE_KEY.format(tenant_id=tenant_id, contact_id=contact_id)
        used = int(await self._incr(keys=[key], args=[self._window]))
        return used <= self._max_turns
