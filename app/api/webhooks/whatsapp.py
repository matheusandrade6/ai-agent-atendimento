"""Ingestao do WhatsApp Cloud API (secao 14.1.1, Sessao S04).

Contrato inegociavel: validar `X-Hub-Signature-256` ANTES de qualquer parsing,
deduplicar por `provider_msg_id`, e responder 200 em menos de 1s sempre — mesmo com
payload malformado ou erro interno. O turno do agente roda fora, no worker que
consome `app.workers.queue.INBOUND_JOB_NAME` (Sessao S05); aqui so valida, deduplica,
persiste e enfileira (RNF-04).
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.whatsapp import (
    WhatsAppMessage,
    WhatsAppStatus,
    WhatsAppWebhookPayload,
    verify_signature,
)
from app.core.config import Settings
from app.core.db import tenant_session
from app.core.telemetry import get_logger
from app.core.time import now_utc
from app.domain.tenant_registry import resolve_tenant_by_phone_number_id
from app.workers.queue import InboundQueue

log = get_logger(__name__)

router = APIRouter(prefix="/webhooks/whatsapp", tags=["webhooks"])

#: Janela de servico do WhatsApp: gratuita por 24h a partir da ultima mensagem inbound
#: (secao 14.1.4).
SERVICE_WINDOW = timedelta(hours=24)


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _inbound_queue(request: Request) -> InboundQueue:
    queue: InboundQueue = request.app.state.inbound_queue
    return queue


@router.get("")
async def verify_subscription(
    request: Request,
    hub_mode: str = Query(default="", alias="hub.mode"),
    hub_verify_token: str = Query(default="", alias="hub.verify_token"),
    hub_challenge: str = Query(default="", alias="hub.challenge"),
) -> Response:
    """Verificacao de assinatura do webhook, exigida pela Meta na configuracao."""
    settings = _settings(request)
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token:
        return Response(content=hub_challenge, media_type="text/plain", status_code=200)
    return Response(status_code=403)


@router.post("")
async def receive(request: Request) -> Response:
    settings = _settings(request)
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    # A assinatura e validada sobre o corpo BRUTO, antes de qualquer parsing (14.1.1).
    if not verify_signature(settings.whatsapp_app_secret, raw_body, signature):
        log.warning("whatsapp_assinatura_invalida")
        return Response(status_code=403)

    try:
        await _process_payload(raw_body, _inbound_queue(request), settings)
    except Exception:
        # A Meta reentrega em qualquer resposta != 200, o que so multiplicaria o
        # problema. Um payload malformado ou um erro interno nunca pode virar 5xx aqui.
        log.exception("whatsapp_webhook_falhou")

    return Response(status_code=200)


async def _process_payload(raw_body: bytes, queue: InboundQueue, settings: Settings) -> None:
    try:
        data: Any = json.loads(raw_body)
        payload = WhatsAppWebhookPayload.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as exc:
        log.warning("whatsapp_payload_malformado", error=str(exc))
        return

    for value in payload.iter_values():
        phone_number_id = value.phone_number_id
        if not phone_number_id:
            continue

        tenant_id = await resolve_tenant_by_phone_number_id(phone_number_id, settings)
        if tenant_id is None:
            log.warning("whatsapp_tenant_nao_encontrado", phone_number_id=phone_number_id)
            continue

        contact_names = {
            contact.wa_id: contact.profile.name if contact.profile else None
            for contact in value.contacts
        }

        async with tenant_session(tenant_id, settings) as session:
            for message in value.messages:
                await _ingest_message(session, tenant_id, message, contact_names, queue)
            for status in value.statuses:
                await _apply_status(session, tenant_id, status)


async def _ingest_message(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    message: WhatsAppMessage,
    contact_names: dict[str, str | None],
    queue: InboundQueue,
) -> None:
    contact_id = await _upsert_contact(
        session, tenant_id, message.from_, contact_names.get(message.from_)
    )
    conversation_id = await _upsert_conversation(session, tenant_id, contact_id, message.from_)

    content_type, content, media_ref = message.extract_content()
    inserted_id = (
        await session.execute(
            sa.text(
                "INSERT INTO messages "
                "(tenant_id, conversation_id, direction, author, content_type, content, "
                " media_ref, provider_msg_id) "
                "VALUES (:tenant_id, :conversation_id, 'inbound', 'contact', :content_type, "
                " :content, :media_ref, :provider_msg_id) "
                "ON CONFLICT (tenant_id, provider_msg_id) DO NOTHING "
                "RETURNING id"
            ),
            {
                "tenant_id": tenant_id,
                "conversation_id": conversation_id,
                "content_type": content_type,
                "content": content,
                "media_ref": media_ref,
                "provider_msg_id": message.id,
            },
        )
    ).scalar_one_or_none()

    if inserted_id is None:
        # Reentrega da Meta: a mensagem ja foi persistida e ja disparou um turno.
        log.info("whatsapp_mensagem_duplicada", provider_msg_id=message.id)
        return

    now = now_utc()
    await session.execute(
        sa.text(
            "UPDATE conversations SET service_window_expires_at = :expires, "
            "last_message_at = :now WHERE id = :conversation_id"
        ),
        {"expires": now + SERVICE_WINDOW, "now": now, "conversation_id": conversation_id},
    )

    await queue.enqueue_inbound(tenant_id=tenant_id, conversation_id=conversation_id)


async def _apply_status(
    session: AsyncSession, tenant_id: uuid.UUID, status: WhatsAppStatus
) -> None:
    updated_id = (
        await session.execute(
            sa.text(
                "UPDATE messages SET status = :status "
                "WHERE tenant_id = :tenant_id AND provider_msg_id = :provider_msg_id "
                "RETURNING id"
            ),
            {"status": status.status, "tenant_id": tenant_id, "provider_msg_id": status.id},
        )
    ).scalar_one_or_none()
    if updated_id is None:
        log.info("whatsapp_status_sem_mensagem_correspondente", provider_msg_id=status.id)


async def _upsert_contact(
    session: AsyncSession, tenant_id: uuid.UUID, wa_id: str, name: str | None
) -> uuid.UUID:
    phone_e164 = f"+{wa_id}"
    contact_id: uuid.UUID = (
        await session.execute(
            sa.text(
                "INSERT INTO contacts (tenant_id, phone_e164, name) "
                "VALUES (:tenant_id, :phone, :name) "
                "ON CONFLICT (tenant_id, phone_e164) DO UPDATE SET "
                "name = COALESCE(contacts.name, EXCLUDED.name) "
                "RETURNING id"
            ),
            {"tenant_id": tenant_id, "phone": phone_e164, "name": name},
        )
    ).scalar_one()
    return contact_id


async def _upsert_conversation(
    session: AsyncSession, tenant_id: uuid.UUID, contact_id: uuid.UUID, wa_id: str
) -> uuid.UUID:
    conversation_id: uuid.UUID = (
        await session.execute(
            sa.text(
                "INSERT INTO conversations (tenant_id, contact_id, channel, channel_thread) "
                "VALUES (:tenant_id, :contact_id, 'whatsapp', :channel_thread) "
                "ON CONFLICT (tenant_id, channel, channel_thread) DO UPDATE SET "
                "contact_id = EXCLUDED.contact_id "
                "RETURNING id"
            ),
            {"tenant_id": tenant_id, "contact_id": contact_id, "channel_thread": wa_id},
        )
    ).scalar_one()
    return conversation_id
