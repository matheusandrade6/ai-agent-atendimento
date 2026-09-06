"""Parsing e assinatura do WhatsApp Cloud API (secao 14.1.1).

Os modelos aqui sao deliberadamente tolerantes (`extra="ignore"`): o payload da Meta
carrega muito mais campos do que usamos, e um campo novo do lado deles nunca pode
derrubar a ingestao (RNF-04 — responder 200 sempre). Isso contrasta de proposito com
`app.domain.tenant_config`, onde campo desconhecido e erro: aquele e um contrato nosso,
este e um contrato de terceiro.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
