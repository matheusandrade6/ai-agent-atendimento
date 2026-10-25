"""`slot_token`: a prova de que um horario saiu do motor de disponibilidade (secao 11.4).

Por que o token existe
----------------------
O modelo escolhe *entre* os horarios que `check_availability` ofereceu, nunca *qual horario
existe* (invariante 1). Se `hold_slot` aceitasse `provider_id` e `starts_at` como argumento,
bastaria o modelo escrever `2026-09-09T03:00` para tentar reservar uma vaga que ninguem
ofereceu. Em vez disso `hold_slot` recebe so o token, e o token e assinado: o que o modelo
nao consegue e fabricar um.

O que ele carrega e o que o prende
----------------------------------
O corpo leva `provider_id`, `resource_id`, `service_id`, `starts_at` (UTC) e a validade. O
**tenant e a conversa nao vao no corpo**: entram na derivacao da chave de assinatura. O mesmo
corpo assinado para outra conversa — ou para outro tenant — tem outra assinatura, entao um
token copiado de um turno para outro cliente nao vale, e nao ha campo de tenant que alguem
possa adulterar. A consequencia e que o erro e o mesmo para "adulterado" e "de outra
conversa": e de proposito, nao ha o que ajudar quem tenta.

Formato: `base64url(corpo JSON).base64url(HMAC-SHA256)`. A assinatura cobre os bytes do
segmento **como foram transmitidos**, nao o JSON reserializado, entao nao ha ambiguidade de
canonicalizacao. Os dois segmentos so aceitam o alfabeto do base64url: um caractere solto no
meio da assinatura nao pode ser descartado pelo decodificador e continuar valendo.

Quem decide "agora" e quem chama (`now`), nao este modulo: o teste de validade nao depende
do relogio da maquina.
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
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

__all__ = [
    "SlotClaims",
    "SlotTokenError",
    "SlotTokenSigner",
]

_VERSION: Final = 1
_SEGMENT: Final = re.compile(r"[A-Za-z0-9_-]+")
#: Separa o dominio da chave: a mesma chave mestra nao pode assinar outra coisa com este
#: formato por acidente.
_KEY_DOMAIN: Final = b"datamind.slot_token.v1"

Reason = Literal["malformed", "bad_signature", "expired", "unsupported_version"]


class SlotTokenError(Exception):
    """Token recusado. `reason` e para log e metrica; nao vai para o cliente nem para o prompt."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason: Reason = reason


@dataclass(frozen=True, slots=True)
class SlotClaims:
    """O que o motor ofereceu. `starts_at` em UTC."""

    provider_id: uuid.UUID
    service_id: uuid.UUID
    starts_at: datetime
    resource_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if self.starts_at.tzinfo is None:
            raise ValueError("`starts_at` ingenuo: todo instante precisa de timezone")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(segment: str) -> bytes:
    if not _SEGMENT.fullmatch(segment):
        raise SlotTokenError("malformed")
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (binascii.Error, ValueError) as exc:
        raise SlotTokenError("malformed") from exc


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None:
        raise ValueError("`now` ingenuo: use app.core.time.now_in(tz)")


class SlotTokenSigner:
    """Assina e confere tokens. Uma instancia por processo; nao guarda estado."""

    def __init__(self, secret: str, *, ttl: timedelta) -> None:
        if not secret:
            raise ValueError("segredo do slot_token vazio")
        if ttl <= timedelta(0):
            raise ValueError("validade do slot_token precisa ser positiva")
        self._secret = secret.encode("utf-8")
        self._ttl = ttl

    def _key(self, tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> bytes:
        context = b"|".join([_KEY_DOMAIN, tenant_id.bytes, conversation_id.bytes])
        return hmac.new(self._secret, context, hashlib.sha256).digest()

    def _signature(self, tenant_id: uuid.UUID, conversation_id: uuid.UUID, payload: str) -> bytes:
        return hmac.new(
            self._key(tenant_id, conversation_id), payload.encode("ascii"), hashlib.sha256
        ).digest()

    def sign(
        self,
        claims: SlotClaims,
        *,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        now: datetime,
    ) -> str:
        _require_aware(now)
        body = {
            "v": _VERSION,
            "p": claims.provider_id.hex,
            "s": claims.service_id.hex,
            "r": claims.resource_id.hex if claims.resource_id else None,
            "a": claims.starts_at.astimezone(UTC).isoformat(),
            "e": int((now + self._ttl).timestamp()),
        }
        payload = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        signature = self._signature(tenant_id, conversation_id, payload)
        return f"{payload}.{_b64(signature)}"

    def verify(
        self,
        token: str,
        *,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        now: datetime,
    ) -> SlotClaims:
        """Devolve o que o motor ofereceu, ou levanta `SlotTokenError`.

        A assinatura e conferida **antes** de qualquer interpretacao do corpo: quem nao
        tem a chave nao aprende nada sobre o formato pelos erros que provoca.
        """
        _require_aware(now)
        payload, separator, signature = token.strip().partition(".")
        if not separator or not _SEGMENT.fullmatch(payload):
            raise SlotTokenError("malformed")
        received = _unb64(signature)
        expected = self._signature(tenant_id, conversation_id, payload)
        if not hmac.compare_digest(received, expected):
            raise SlotTokenError("bad_signature")

        try:
            body = json.loads(_unb64(payload))
            if body["v"] != _VERSION:
                raise SlotTokenError("unsupported_version")
            claims = SlotClaims(
                provider_id=uuid.UUID(body["p"]),
                service_id=uuid.UUID(body["s"]),
                resource_id=uuid.UUID(body["r"]) if body["r"] else None,
                starts_at=datetime.fromisoformat(body["a"]),
            )
            expires = int(body["e"])
        except SlotTokenError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            # Assinatura valida com corpo ilegivel so acontece com bug nosso, nao com ataque.
            raise SlotTokenError("malformed") from exc

        if now.timestamp() >= expires:
            raise SlotTokenError("expired")
        return claims
