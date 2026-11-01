"""`confirmation_token` (secao 12.3; aceite da S16: cancelamento em dois turnos).

O que se protege: o modelo nao cancela na primeira mensagem. O token so existe depois de a
tool ter pedido a confirmacao, so vale em turno posterior ao que o emitiu, e nao serve para
outro agendamento, outra conversa nem outro tenant.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.scheduling.confirmation_token import (
    ConfirmationTokenError,
    ConfirmationTokenSigner,
)

SECRET = "segredo-de-teste"
TTL = timedelta(minutes=30)
ASKED_AT = datetime(2026, 11, 10, 13, 0, tzinfo=UTC)
ANSWERED_AT = ASKED_AT + timedelta(minutes=2)

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = uuid.UUID("99999999-9999-9999-9999-999999999999")
CONVERSATION = uuid.UUID("22222222-2222-2222-2222-222222222222")
OTHER_CONVERSATION = uuid.UUID("88888888-8888-8888-8888-888888888888")
APPOINTMENT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER_APPOINTMENT = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.fixture
def signer() -> ConfirmationTokenSigner:
    return ConfirmationTokenSigner(SECRET, ttl=TTL)


def sign(signer: ConfirmationTokenSigner, **overrides: object) -> str:
    params: dict[str, object] = {
        "tenant_id": TENANT,
        "conversation_id": CONVERSATION,
        "now": ASKED_AT,
        **overrides,
    }
    return signer.sign(APPOINTMENT, **params)  # type: ignore[arg-type]


def verify(signer: ConfirmationTokenSigner, token: str, **overrides: object) -> object:
    params: dict[str, object] = {
        "appointment_id": APPOINTMENT,
        "tenant_id": TENANT,
        "conversation_id": CONVERSATION,
        "now": ANSWERED_AT,
        **overrides,
    }
    return signer.verify(token, **params)  # type: ignore[arg-type]


def reason_of(signer: ConfirmationTokenSigner, token: str, **overrides: object) -> str:
    with pytest.raises(ConfirmationTokenError) as caught:
        verify(signer, token, **overrides)
    return caught.value.reason


# ------------------------------------ o caminho certo ------------------------------------


def test_token_do_turno_anterior_vale(signer: ConfirmationTokenSigner) -> None:
    claims = verify(signer, sign(signer))

    assert claims.appointment_id == APPOINTMENT  # type: ignore[attr-defined]


def test_vale_ate_o_ultimo_instante_antes_de_vencer(signer: ConfirmationTokenSigner) -> None:
    token = sign(signer)

    verify(signer, token, now=ASKED_AT + TTL - timedelta(seconds=1))
    assert reason_of(signer, token, now=ASKED_AT + TTL) == "expired"


# --------------------------------- dois turnos, nao um so ---------------------------------


def test_no_mesmo_turno_o_token_nao_vale(signer: ConfirmationTokenSigner) -> None:
    """O modelo pede a confirmacao e ja tenta usar o token: o cliente nao teve como responder."""
    token = sign(signer)

    assert reason_of(signer, token, now=ASKED_AT) == "same_turn"


def test_relogio_que_anda_para_tras_nao_vale(signer: ConfirmationTokenSigner) -> None:
    assert reason_of(signer, sign(signer), now=ASKED_AT - timedelta(seconds=1)) == "same_turn"


def test_um_microssegundo_depois_ja_e_outro_turno(signer: ConfirmationTokenSigner) -> None:
    verify(signer, sign(signer), now=ASKED_AT + timedelta(microseconds=1))


# ------------------------------------ o que prende o token ------------------------------------


def test_token_de_um_agendamento_nao_cancela_outro(signer: ConfirmationTokenSigner) -> None:
    assert reason_of(signer, sign(signer), appointment_id=OTHER_APPOINTMENT) == "wrong_appointment"


def test_token_de_outra_conversa_nao_vale(signer: ConfirmationTokenSigner) -> None:
    assert reason_of(signer, sign(signer), conversation_id=OTHER_CONVERSATION) == "bad_signature"


def test_token_de_outro_tenant_nao_vale(signer: ConfirmationTokenSigner) -> None:
    assert reason_of(signer, sign(signer), tenant_id=OTHER_TENANT) == "bad_signature"


def test_outra_chave_nao_gera_token_valido(signer: ConfirmationTokenSigner) -> None:
    forger = ConfirmationTokenSigner("outro-segredo", ttl=TTL)

    assert reason_of(signer, sign(forger)) == "bad_signature"


def test_o_segredo_do_slot_token_nao_assina_confirmacao() -> None:
    """Mesmo segredo mestre, dominios separados: um formato nao serve de chave para o outro."""
    from app.scheduling.slot_token import SlotClaims, SlotTokenSigner

    slot = SlotTokenSigner(SECRET, ttl=TTL).sign(
        SlotClaims(provider_id=APPOINTMENT, service_id=APPOINTMENT, starts_at=ASKED_AT),
        tenant_id=TENANT,
        conversation_id=CONVERSATION,
        now=ASKED_AT,
    )

    assert reason_of(ConfirmationTokenSigner(SECRET, ttl=TTL), slot) == "bad_signature"


# ------------------------------------ adulteracao e lixo ------------------------------------


def test_corpo_adulterado_e_recusado(signer: ConfirmationTokenSigner) -> None:
    payload, signature = sign(signer).split(".")
    body = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    body["a"] = OTHER_APPOINTMENT.hex
    forged = (
        base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode())
        .rstrip(b"=")
        .decode()
    )

    assert reason_of(signer, f"{forged}.{signature}") == "bad_signature"


def test_validade_estendida_pelo_modelo_e_recusada(signer: ConfirmationTokenSigner) -> None:
    payload, signature = sign(signer).split(".")
    body = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    body["e"] += 10**12
    forged = (
        base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode())
        .rstrip(b"=")
        .decode()
    )

    assert reason_of(signer, f"{forged}.{signature}") == "bad_signature"


@pytest.mark.parametrize(
    "garbage", ["", "x", "sem-ponto", "a.b.c", ".", "abc.", ".abc", "ab cd.ef", "%%%.%%%"]
)
def test_lixo_e_recusado_sem_excecao_estranha(
    signer: ConfirmationTokenSigner, garbage: str
) -> None:
    assert reason_of(signer, garbage) in {"malformed", "bad_signature"}


def test_instante_ingenuo_e_erro_de_programacao(signer: ConfirmationTokenSigner) -> None:
    with pytest.raises(ValueError, match="ingenuo"):
        sign(signer, now=datetime(2026, 11, 10, 13, 0))


def test_segredo_vazio_e_validade_nao_positiva_sao_recusados() -> None:
    with pytest.raises(ValueError, match="vazio"):
        ConfirmationTokenSigner("", ttl=TTL)
    with pytest.raises(ValueError, match="positiva"):
        ConfirmationTokenSigner(SECRET, ttl=timedelta(0))
