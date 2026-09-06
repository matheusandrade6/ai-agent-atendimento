"""Traducao de HTTP para decisao de retentativa (secao 14.1.3, Sessao S05).

O que importa aqui nao e o formato do JSON da Meta — e o **tipo de excecao**, porque e
ele que o worker de saida usa para escolher entre reagendar e desistir. Trocar
`ChannelTransientError` por `ChannelPermanentError` num 500 significaria descartar
resposta de cliente por causa de um soluco do provedor.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.channels.base import (
    ChannelPermanentError,
    ChannelRateLimited,
    ChannelTransientError,
)
from app.channels.whatsapp import WhatsAppSender

BASE_URL = "https://graph.example.test"
PHONE_NUMBER_ID = "1234567890"
SEND_URL = f"{BASE_URL}/v21.0/{PHONE_NUMBER_ID}/messages"


def _sender() -> WhatsAppSender:
    return WhatsAppSender(
        phone_number_id=PHONE_NUMBER_ID,
        access_token="token-de-teste",
        base_url=BASE_URL,
    )


@respx.mock
async def test_envio_bem_sucedido_devolve_provider_msg_id() -> None:
    route = respx.post(SEND_URL).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.ABC"}]})
    )
    sender = _sender()

    sent = await sender.send_text(to="5511999998888", text="Oi!")
    await sender.aclose()

    assert sent.provider_msg_id == "wamid.ABC"
    body = json.loads(route.calls[0].request.content)
    assert body["to"] == "5511999998888"
    assert body["text"]["body"] == "Oi!"
    assert route.calls[0].request.headers["Authorization"] == "Bearer token-de-teste"


@respx.mock
async def test_429_vira_rate_limited_com_retry_after() -> None:
    respx.post(SEND_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "42"}, json={})
    )
    sender = _sender()

    with pytest.raises(ChannelRateLimited) as exc:
        await sender.send_text(to="5511999998888", text="Oi!")
    await sender.aclose()

    assert exc.value.retry_after == 42.0


@respx.mock
async def test_429_sem_cabecalho_deixa_o_backoff_decidir() -> None:
    respx.post(SEND_URL).mock(return_value=httpx.Response(429, json={}))
    sender = _sender()

    with pytest.raises(ChannelRateLimited) as exc:
        await sender.send_text(to="5511999998888", text="Oi!")
    await sender.aclose()

    assert exc.value.retry_after is None


@respx.mock
async def test_5xx_e_transitorio() -> None:
    respx.post(SEND_URL).mock(return_value=httpx.Response(503, json={}))
    sender = _sender()

    with pytest.raises(ChannelTransientError):
        await sender.send_text(to="5511999998888", text="Oi!")
    await sender.aclose()


@respx.mock
async def test_timeout_e_transitorio() -> None:
    respx.post(SEND_URL).mock(side_effect=httpx.ConnectTimeout("estourou"))
    sender = _sender()

    with pytest.raises(ChannelTransientError):
        await sender.send_text(to="5511999998888", text="Oi!")
    await sender.aclose()


@respx.mock
async def test_4xx_e_permanente() -> None:
    respx.post(SEND_URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": "numero invalido"}})
    )
    sender = _sender()

    with pytest.raises(ChannelPermanentError):
        await sender.send_text(to="5511999998888", text="Oi!")
    await sender.aclose()


@respx.mock
async def test_200_sem_id_e_transitorio() -> None:
    """Sem `provider_msg_id` nao ha como conciliar o callback de status depois."""
    respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"messages": []}))
    sender = _sender()

    with pytest.raises(ChannelTransientError):
        await sender.send_text(to="5511999998888", text="Oi!")
    await sender.aclose()


@respx.mock
async def test_mark_read_com_digitando() -> None:
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"success": True}))
    sender = _sender()

    await sender.mark_read(provider_msg_id="wamid.ABC", typing=True)
    await sender.aclose()

    body = json.loads(route.calls[0].request.content)
    assert body["status"] == "read"
    assert body["message_id"] == "wamid.ABC"
    assert body["typing_indicator"] == {"type": "text"}
