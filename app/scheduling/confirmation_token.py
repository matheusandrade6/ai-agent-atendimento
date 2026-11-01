"""`confirmation_token`: a prova de que o cancelamento foi pedido e confirmado em turnos distintos.

Por que o token existe
----------------------
A 12.3 manda confirmar o cancelamento — "nunca cancela em uma mensagem so". Um argumento
`confirmed: true` na tool nao cumpre isso: o modelo o preencheria na primeira mensagem. O que
ele nao consegue e fabricar um token que so existe depois de a tool ter pedido a confirmacao.

O primeiro `cancel_appointment` (sem token) nao cancela nada: devolve este token e o modelo
pergunta ao cliente. O segundo, com o token, so vale se vier de **outro turno** — `now`
estritamente depois de quando o token foi emitido. Turno so existe porque uma mensagem do
cliente chegou, entao "outro turno" e "o cliente respondeu". O que o codigo nao le e o
conteudo da resposta: se foi um "sim" ou um "nao" e leitura do modelo, e e esse o unico elo
que sobra fora do codigo.

Formato e chave seguem `app.scheduling.slot_token`: `base64url(corpo).base64url(HMAC)`,
com a chave derivada de (tenant, conversa) — um token copiado para outra conversa ou outro
tenant nao vale — e o dominio da chave separado, para o mesmo segredo nao assinar uma coisa
no lugar da outra. O agendamento vai assinado dentro do corpo: o token de um agendamento nao
cancela outro.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

__all__ = [
    "ConfirmationClaims",
    "ConfirmationTokenError",
    "ConfirmationTokenSigner",
]

_VERSION: Final = 1
_SEGMENT: Final = re.compile(r"[A-Za-z0-9_-]+")
_KEY_DOMAIN: Final = b"datamind.confirmation_token.v1"

Reason = Literal[
    "malformed", "bad_signature", "expired", "unsupported_version", "same_turn", "wrong_appointment"
]


class ConfirmationTokenError(Exception):
    """Token recusado. `reason` e para log e metrica; nao vai para o cliente nem para o prompt."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason: Reason = reason


@dataclass(frozen=True, slots=True)
class ConfirmationClaims:
    appointment_id: uuid.UUID
    #: Instante em que a confirmacao foi pedida (microssegundos desde a epoch).
    issued_at_us: int


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(segment: str) -> bytes:
    if not _SEGMENT.fullmatch(segment):
        raise ConfirmationTokenError("malformed")
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ConfirmationTokenError("malformed") from exc


def _microseconds(moment: datetime) -> int:
    if moment.tzinfo is None:
        raise ValueError("`now` ingenuo: use app.core.time.now_in(tz)")
    return int(moment.timestamp() * 1_000_000)


class ConfirmationTokenSigner:
    """Assina e confere tokens. Uma instancia por uso; nao guarda estado."""

    def __init__(self, secret: str, *, ttl: timedelta) -> None:
        if not secret:
            raise ValueError("segredo do confirmation_token vazio")
        if ttl <= timedelta(0):
            raise ValueError("validade do confirmation_token precisa ser positiva")
        self._secret = secret.encode("utf-8")
        self._ttl = ttl

    def _signature(self, tenant_id: uuid.UUID, conversation_id: uuid.UUID, payload: str) -> bytes:
        context = b"|".join([_KEY_DOMAIN, tenant_id.bytes, conversation_id.bytes])
        key = hmac.new(self._secret, context, hashlib.sha256).digest()
        return hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest()

    def sign(
        self,
        appointment_id: uuid.UUID,
        *,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        now: datetime,
    ) -> str:
        issued = _microseconds(now)
        body = {
            "v": _VERSION,
            "a": appointment_id.hex,
            "i": issued,
            "e": issued + int(self._ttl.total_seconds() * 1_000_000),
        }
        payload = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        return f"{payload}.{_b64(self._signature(tenant_id, conversation_id, payload))}"

    def verify(
        self,
        token: str,
        *,
        appointment_id: uuid.UUID,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        now: datetime,
    ) -> ConfirmationClaims:
        """Devolve o que foi assinado, ou levanta `ConfirmationTokenError`.

        A assinatura e conferida antes de interpretar o corpo, como no `slot_token`.
        """
        current = _microseconds(now)
        payload, separator, signature = token.strip().partition(".")
        if not separator or not _SEGMENT.fullmatch(payload):
            raise ConfirmationTokenError("malformed")
        received = _unb64(signature)
        expected = self._signature(tenant_id, conversation_id, payload)
        if not hmac.compare_digest(received, expected):
            raise ConfirmationTokenError("bad_signature")

        try:
            body = json.loads(_unb64(payload))
            if body["v"] != _VERSION:
                raise ConfirmationTokenError("unsupported_version")
            claims = ConfirmationClaims(
                appointment_id=uuid.UUID(body["a"]), issued_at_us=int(body["i"])
            )
            expires = int(body["e"])
        except ConfirmationTokenError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfirmationTokenError("malformed") from exc

        if claims.appointment_id != appointment_id:
            raise ConfirmationTokenError("wrong_appointment")
        if current >= expires:
            raise ConfirmationTokenError("expired")
        if current <= claims.issued_at_us:
            # Mesmo turno: o cliente ainda nao teve como responder.
            raise ConfirmationTokenError("same_turn")
        return claims
