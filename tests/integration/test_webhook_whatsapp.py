"""Integracao do webhook WhatsApp (secao 14.1.1 e 19.2, Sessao S04).

Assinatura invalida e payload malformado nao tocam o banco: o guard de assinatura
roda antes de qualquer parsing (14.1.1), e um payload que nao vira JSON valido nunca
chega perto de resolver um tenant. Os demais casos precisam de Postgres real porque
dependem de constraint de dedup e de RLS.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import dispose_engine
from app.domain.tenant_config import load_tenant_config
from app.main import create_app

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_TENANT = REPO_ROOT / "config" / "tenants" / "clinica-exemplo.yaml"

APP_SECRET = "s04-integration-secret"


def _sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class FakeQueue:
    """Substitui `ArqInboundQueue` nos testes: sem Redis, so registra as chamadas."""

    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def enqueue_inbound(self, *, tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
        self.calls.append((tenant_id, conversation_id))

    async def close(self) -> None:
        return None


def _message_payload(phone_number_id: str, msg_id: str) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "16505551111",
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": [{"profile": {"name": "Carla"}, "wa_id": "5511999998888"}],
                            "messages": [
                                {
                                    "from": "5511999998888",
                                    "id": msg_id,
                                    "timestamp": "1699999999",
                                    "type": "text",
                                    "text": {"body": "Oi, quero marcar um horario"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _status_payload(phone_number_id: str, msg_id: str, status: str) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": phone_number_id},
                            "statuses": [
                                {
                                    "id": msg_id,
                                    "status": status,
                                    "timestamp": "1699999999",
                                    "recipient_id": "5511999998888",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


@pytest.fixture
def app_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"whatsapp_app_secret": APP_SECRET, "whatsapp_verify_token": "verify-token"}
    )


@pytest.fixture
def clean_engine() -> Iterator[None]:
    """Isola o engine assincrono global entre testes.

    O `TestClient` abre um event loop proprio a cada uso; conexoes asyncpg sao presas
    ao loop em que nasceram. Sem descartar o engine entre testes, a segunda chamada
    reutilizaria uma conexao aberta no loop do teste anterior e quebraria.
    """
    asyncio.run(dispose_engine())
    yield
    asyncio.run(dispose_engine())


@pytest.fixture
def tenant_with_whatsapp(sync_engine: sa.Engine, clean_engine: None) -> Iterator[dict[str, str]]:
    phone_number_id = f"pnid-{uuid.uuid4().hex[:12]}"
    config = load_tenant_config(EXAMPLE_TENANT).raw_dict()
    config["channels"]["whatsapp"]["phone_number_id"] = phone_number_id
    slug = f"s04-{uuid.uuid4().hex[:8]}"

    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        tenant_id = conn.execute(
            sa.text(
                "INSERT INTO tenants (slug, name, vertical, config) "
                "VALUES (:slug, :slug, 'veterinaria', CAST(:config AS jsonb)) RETURNING id"
            ),
            {"slug": slug, "config": json.dumps(config)},
        ).scalar_one()

    yield {"tenant_id": str(tenant_id), "phone_number_id": phone_number_id}

    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        conn.execute(sa.text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})


# --------------------------- casos sem banco (guard de assinatura) ---------------------------


def test_assinatura_invalida_retorna_403(app_settings: Settings) -> None:
    body = b'{"object":"whatsapp_business_account","entry":[]}'
    app = create_app(app_settings)
    app.state.inbound_queue = FakeQueue()

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
        )

    assert response.status_code == 403


def test_assinatura_ausente_retorna_403(app_settings: Settings) -> None:
    body = b'{"object":"whatsapp_business_account","entry":[]}'
    app = create_app(app_settings)
    app.state.inbound_queue = FakeQueue()

    with TestClient(app) as client:
        response = client.post("/webhooks/whatsapp", content=body)

    assert response.status_code == 403


def test_payload_malformado_retorna_200_sem_crash(app_settings: Settings) -> None:
    body = b"isso nao e json valido {"
    signature = _sign(body, app_settings.whatsapp_app_secret)
    app = create_app(app_settings)
    app.state.inbound_queue = FakeQueue()

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": signature}
        )

    assert response.status_code == 200


def test_verificacao_get_com_token_correto_devolve_challenge(app_settings: Settings) -> None:
    app = create_app(app_settings)
    app.state.inbound_queue = FakeQueue()

    with TestClient(app) as client:
        response = client.get(
            "/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "verify-token",
                "hub.challenge": "12345",
            },
        )

    assert response.status_code == 200
    assert response.text == "12345"


def test_verificacao_get_com_token_errado_retorna_403(app_settings: Settings) -> None:
    app = create_app(app_settings)
    app.state.inbound_queue = FakeQueue()

    with TestClient(app) as client:
        response = client.get(
            "/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "token-errado",
                "hub.challenge": "12345",
            },
        )

    assert response.status_code == 403


# ------------------------------ casos com banco real ------------------------------


def test_payload_duplicado_produz_uma_linha_e_enfileira_uma_vez(
    app_settings: Settings, tenant_with_whatsapp: dict[str, str], sync_engine: sa.Engine
) -> None:
    phone_number_id = tenant_with_whatsapp["phone_number_id"]
    msg_id = f"wamid.{uuid.uuid4().hex}"
    body = json.dumps(_message_payload(phone_number_id, msg_id)).encode("utf-8")
    signature = _sign(body, APP_SECRET)

    app = create_app(app_settings)
    fake_queue = FakeQueue()
    app.state.inbound_queue = fake_queue

    with TestClient(app) as client:
        first = client.post(
            "/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": signature}
        )
        second = client.post(
            "/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": signature}
        )

    assert first.status_code == 200
    assert second.status_code == 200

    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        count = conn.execute(
            sa.text("SELECT count(*) FROM messages WHERE provider_msg_id = :id"), {"id": msg_id}
        ).scalar_one()

    assert count == 1
    assert len(fake_queue.calls) == 1


def test_mensagem_inbound_atualiza_janela_de_servico(
    app_settings: Settings, tenant_with_whatsapp: dict[str, str], sync_engine: sa.Engine
) -> None:
    phone_number_id = tenant_with_whatsapp["phone_number_id"]
    msg_id = f"wamid.{uuid.uuid4().hex}"
    body = json.dumps(_message_payload(phone_number_id, msg_id)).encode("utf-8")
    signature = _sign(body, APP_SECRET)

    app = create_app(app_settings)
    app.state.inbound_queue = FakeQueue()

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": signature}
        )

    assert response.status_code == 200

    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        expires_at = conn.execute(
            sa.text(
                "SELECT c.service_window_expires_at FROM conversations c "
                "JOIN messages m ON m.conversation_id = c.id "
                "WHERE m.provider_msg_id = :id"
            ),
            {"id": msg_id},
        ).scalar_one()

    assert expires_at is not None


def test_status_callback_atualiza_status_da_mensagem(
    app_settings: Settings, tenant_with_whatsapp: dict[str, str], sync_engine: sa.Engine
) -> None:
    phone_number_id = tenant_with_whatsapp["phone_number_id"]
    msg_id = f"wamid.{uuid.uuid4().hex}"
    inbound_body = json.dumps(_message_payload(phone_number_id, msg_id)).encode("utf-8")
    status_body = json.dumps(_status_payload(phone_number_id, msg_id, "delivered")).encode("utf-8")

    app = create_app(app_settings)
    app.state.inbound_queue = FakeQueue()

    with TestClient(app) as client:
        client.post(
            "/webhooks/whatsapp",
            content=inbound_body,
            headers={"X-Hub-Signature-256": _sign(inbound_body, APP_SECRET)},
        )
        response = client.post(
            "/webhooks/whatsapp",
            content=status_body,
            headers={"X-Hub-Signature-256": _sign(status_body, APP_SECRET)},
        )

    assert response.status_code == 200

    with sync_engine.begin() as conn:
        conn.execute(sa.text("SELECT set_config('app.bypass_rls', 'on', false)"))
        status = conn.execute(
            sa.text("SELECT status FROM messages WHERE provider_msg_id = :id"), {"id": msg_id}
        ).scalar_one()

    assert status == "delivered"


def test_status_callback_sem_mensagem_correspondente_nao_falha(
    app_settings: Settings, tenant_with_whatsapp: dict[str, str]
) -> None:
    phone_number_id = tenant_with_whatsapp["phone_number_id"]
    status_body = json.dumps(_status_payload(phone_number_id, "wamid.inexistente", "read")).encode(
        "utf-8"
    )

    app = create_app(app_settings)
    app.state.inbound_queue = FakeQueue()

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp",
            content=status_body,
            headers={"X-Hub-Signature-256": _sign(status_body, APP_SECRET)},
        )

    assert response.status_code == 200


def test_phone_number_id_desconhecido_nao_falha(
    app_settings: Settings, clean_engine: None, require_db: None
) -> None:
    body = json.dumps(_message_payload("pnid-inexistente", "wamid.qualquer")).encode("utf-8")
    signature = _sign(body, APP_SECRET)

    app = create_app(app_settings)
    app.state.inbound_queue = FakeQueue()

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": signature}
        )

    assert response.status_code == 200
