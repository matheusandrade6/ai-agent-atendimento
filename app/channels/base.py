"""Contrato minimo de canal de saida (secao 14.1.3 / 14.1.5).

Esta e a fatia de `ChannelProvider` que a Sessao S05 precisa: mandar texto e marcar
leitura. `send_template` (S19, janela fechada) e `download_media` (S06, audio/imagem)
entram nas sessoes que os usam — declarar agora um Protocol que ninguem implementa so
criaria contrato morto.

As tres excecoes existem para o worker de saida decidir **o que fazer com a falha**,
nao para descrever o erro:

- `ChannelRateLimited` — nao enviou, o provedor pediu para esperar. Reagenda honrando
  `retry_after` quando ele vem no cabecalho.
- `ChannelTransientError` — pode ter falhado no meio do caminho. Reagenda com backoff.
- `ChannelPermanentError` — retentar so gasta cota (numero invalido, token errado,
  payload recusado). Marca falha e para.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SentMessage:
    """Recibo de envio. `provider_msg_id` amarra os callbacks de status (14.1.3)."""

    provider_msg_id: str


class ChannelError(Exception):
    """Base das falhas de canal."""


class ChannelRateLimited(ChannelError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ChannelTransientError(ChannelError):
    """Falha que provavelmente passa: 5xx, timeout, conexao caida."""


class ChannelPermanentError(ChannelError):
    """Falha que nao passa com retentativa: 4xx que nao seja 429."""


class OutboundChannel(Protocol):
    async def send_text(self, *, to: str, text: str) -> SentMessage: ...

    async def mark_read(self, *, provider_msg_id: str, typing: bool = False) -> None: ...

    async def aclose(self) -> None: ...
