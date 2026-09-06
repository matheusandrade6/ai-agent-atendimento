"""Parsing e assinatura do WhatsApp Cloud API (secao 14.1.1).

Os modelos aqui sao deliberadamente tolerantes (`extra="ignore"`): o payload da Meta
carrega muito mais campos do que usamos, e um campo novo do lado deles nunca pode
derrubar a ingestao (RNF-04 — responder 200 sempre). Isso contrasta de proposito com
`app.domain.tenant_config`, onde campo desconhecido e erro: aquele e um contrato nosso,
este e um contrato de terceiro.

`WhatsAppSender` fecha o outro lado do canal (secao 14.1.3): envio de texto e marcacao
de leitura. Ele nao decide retentativa — traduz a resposta HTTP para as excecoes de
`app.channels.base` e deixa a politica de backoff com o worker de saida.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterator
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.channels.base import (
    ChannelPermanentError,
    ChannelRateLimited,
    ChannelTransientError,
    SentMessage,
)


def verify_signature(app_secret: str, payload: bytes, signature_header: str | None) -> bool:
    """Valida `X-Hub-Signature-256` (HMAC SHA256) sobre o corpo bruto.

    Chamado ANTES de qualquer parsing do payload (secao 14.1.1) — por isso recebe
    `bytes`, nunca o JSON ja decodificado.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


class WhatsAppProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None


class WhatsAppContact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    wa_id: str
    profile: WhatsAppProfile | None = None


class WhatsAppTextBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    body: str


class WhatsAppMediaRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    mime_type: str | None = None
    caption: str | None = None


class WhatsAppMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    from_: str = Field(alias="from")
    timestamp: str
    type: str
    text: WhatsAppTextBody | None = None
    image: WhatsAppMediaRef | None = None
    audio: WhatsAppMediaRef | None = None
    video: WhatsAppMediaRef | None = None
    document: WhatsAppMediaRef | None = None
    sticker: WhatsAppMediaRef | None = None

    def extract_content(self) -> tuple[str, str | None, str | None]:
        """Devolve `(content_type, content, media_ref)` para persistir em `messages`."""
        if self.type == "text" and self.text is not None:
            return "text", self.text.body, None
        media_by_type: dict[str, WhatsAppMediaRef | None] = {
            "image": self.image,
            "audio": self.audio,
            "video": self.video,
            "document": self.document,
            "sticker": self.sticker,
        }
        media = media_by_type.get(self.type)
        if media is not None:
            return self.type, media.caption, media.id
        return self.type, None, None


class WhatsAppStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    status: str
    timestamp: str
    recipient_id: str | None = None


class WhatsAppValue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    messaging_product: str = "whatsapp"
    metadata: dict[str, Any] = Field(default_factory=dict)
    contacts: list[WhatsAppContact] = Field(default_factory=list)
    messages: list[WhatsAppMessage] = Field(default_factory=list)
    statuses: list[WhatsAppStatus] = Field(default_factory=list)

    @property
    def phone_number_id(self) -> str | None:
        value = self.metadata.get("phone_number_id")
        return str(value) if value is not None else None


class WhatsAppChange(BaseModel):
    model_config = ConfigDict(extra="ignore")

    field: str
    value: WhatsAppValue


class WhatsAppEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    changes: list[WhatsAppChange] = Field(default_factory=list)


class WhatsAppWebhookPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    object: str
    entry: list[WhatsAppEntry] = Field(default_factory=list)

    def iter_values(self) -> Iterator[WhatsAppValue]:
        for entry in self.entry:
            for change in entry.changes:
                yield change.value


class WhatsAppSender:
    """Envio pela Cloud API (secao 14.1.3).

    O token de acesso e global (uma Business Manager, varios WABAs). O que separa um
    tenant do outro no envio e o `phone_number_id`, que vem da config do tenant — nao
    ha `if tenant == 'x'` aqui, so um numero diferente na URL (ver docs/DECISOES.md,
    D-16).
    """

    def __init__(
        self,
        *,
        phone_number_id: str,
        access_token: str,
        api_version: str = "v21.0",
        base_url: str = "https://graph.facebook.com",
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/{api_version}/{phone_number_id}/messages"
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    @property
    def endpoint(self) -> str:
        """URL de envio deste tenant. Publica porque e o que identifica o numero."""
        return self._url

    async def send_text(self, *, to: str, text: str) -> SentMessage:
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }
        data = await self._post(payload)
        messages = data.get("messages") or []
        if not messages or not messages[0].get("id"):
            # 200 sem id e resposta que nao da para conciliar com o callback de status.
            raise ChannelTransientError("resposta de envio sem provider_msg_id")
        return SentMessage(provider_msg_id=str(messages[0]["id"]))

    async def mark_read(self, *, provider_msg_id: str, typing: bool = False) -> None:
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": provider_msg_id,
        }
        if typing:
            payload["typing_indicator"] = {"type": "text"}
        await self._post(payload)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(self._url, json=payload, headers=self._headers)
        except httpx.HTTPError as exc:
            # Timeout e conexao caida sao ambiguos: pode ter chegado. Quem decide
            # retentar e o worker, que carrega a chave de idempotencia.
            raise ChannelTransientError(f"falha de transporte: {exc}") from exc

        if response.status_code == 429:
            raise ChannelRateLimited("429 do WhatsApp", _retry_after(response))
        if response.status_code >= 500:
            raise ChannelTransientError(f"{response.status_code} do WhatsApp")
        if response.status_code >= 400:
            raise ChannelPermanentError(f"{response.status_code}: {response.text[:300]}")

        body: dict[str, Any] = response.json()
        return body


def _retry_after(response: httpx.Response) -> float | None:
    """Le `Retry-After` em segundos. A Meta nem sempre manda; sem ele, backoff normal."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
