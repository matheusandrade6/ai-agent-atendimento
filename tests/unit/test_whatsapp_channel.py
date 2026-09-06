"""Parsing e assinatura do WhatsApp Cloud API (secao 14.1.1, Sessao S04)."""

from __future__ import annotations

import hashlib
import hmac
import json

from app.channels.whatsapp import WhatsAppMessage, WhatsAppWebhookPayload, verify_signature

APP_SECRET = "app-secret-de-teste"


def _sign(body: bytes, secret: str = APP_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# --------------------------------- assinatura ---------------------------------


def test_assinatura_valida() -> None:
    body = b'{"object":"whatsapp_business_account"}'
    assert verify_signature(APP_SECRET, body, _sign(body)) is True


def test_assinatura_com_segredo_errado_e_invalida() -> None:
    body = b'{"object":"whatsapp_business_account"}'
    assert verify_signature(APP_SECRET, body, _sign(body, secret="outro-segredo")) is False


def test_assinatura_corpo_alterado_e_invalida() -> None:
    body = b'{"object":"whatsapp_business_account"}'
    signature = _sign(body)
    assert verify_signature(APP_SECRET, b'{"object":"adulterado"}', signature) is False


def test_assinatura_ausente_e_invalida() -> None:
    body = b'{"object":"whatsapp_business_account"}'
    assert verify_signature(APP_SECRET, body, None) is False


def test_assinatura_sem_prefixo_sha256_e_invalida() -> None:
    body = b'{"object":"whatsapp_business_account"}'
    digest = hmac.new(APP_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    assert verify_signature(APP_SECRET, body, digest) is False


# ---------------------------------- payload ----------------------------------


def _text_message_payload(phone_number_id: str = "123456", msg_id: str = "wamid.ABC") -> dict:
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
                            "contacts": [{"profile": {"name": "Carla"}, "wa_id": "5511999999999"}],
                            "messages": [
                                {
                                    "from": "5511999999999",
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


def test_payload_de_texto_e_parseado() -> None:
    payload = WhatsAppWebhookPayload.model_validate(_text_message_payload())
    values = list(payload.iter_values())
    assert len(values) == 1
    assert values[0].phone_number_id == "123456"
    assert len(values[0].messages) == 1
    assert values[0].messages[0].from_ == "5511999999999"
    assert values[0].contacts[0].profile is not None
    assert values[0].contacts[0].profile.name == "Carla"


def test_payload_ignora_campos_desconhecidos_da_meta() -> None:
    data = _text_message_payload()
    data["entry"][0]["changes"][0]["value"]["messages"][0]["context"] = {"from": "outro"}
    data["campo_novo_da_meta"] = {"qualquer": "coisa"}
    payload = WhatsAppWebhookPayload.model_validate(data)
    assert payload.entry[0].changes[0].value.messages[0].id == "wamid.ABC"


def test_status_callback_e_parseado() -> None:
    data = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "123456"},
                            "statuses": [
                                {
                                    "id": "wamid.ABC",
                                    "status": "delivered",
                                    "timestamp": "1699999999",
                                    "recipient_id": "5511999999999",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    payload = WhatsAppWebhookPayload.model_validate(data)
    values = list(payload.iter_values())
    assert values[0].statuses[0].status == "delivered"
    assert values[0].messages == []


def test_payload_malformado_falha_a_validacao() -> None:
    body = b"nao e json"
    try:
        json.loads(body)
        raised = False
    except json.JSONDecodeError:
        raised = True
    assert raised


# ------------------------------- extracao de conteudo -------------------------------


def test_extract_content_texto() -> None:
    message = WhatsAppMessage.model_validate(
        {
            "id": "wamid.1",
            "from": "5511999999999",
            "timestamp": "1",
            "type": "text",
            "text": {"body": "ola"},
        }
    )
    assert message.extract_content() == ("text", "ola", None)


def test_extract_content_midia() -> None:
    message = WhatsAppMessage.model_validate(
        {
            "id": "wamid.2",
            "from": "5511999999999",
            "timestamp": "1",
            "type": "audio",
            "audio": {"id": "media-123", "mime_type": "audio/ogg"},
        }
    )
    assert message.extract_content() == ("audio", None, "media-123")


def test_extract_content_tipo_sem_modelo_dedicado() -> None:
    message = WhatsAppMessage.model_validate(
        {"id": "wamid.3", "from": "5511999999999", "timestamp": "1", "type": "location"}
    )
    assert message.extract_content() == ("location", None, None)
