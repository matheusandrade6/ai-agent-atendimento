"""`slot_token` assinado (secao 11.4; aceite da S15: token adulterado e rejeitado).

O que se protege: o modelo nao fabrica um horario. Cada teste de recusa e uma forma de
tentar — mexer no corpo, mexer na assinatura, reaproveitar o token em outra conversa ou
outro tenant, esperar a oferta vencer, assinar com outra chave, mandar lixo.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.scheduling.slot_token import SlotClaims, SlotTokenError, SlotTokenSigner

SECRET = "segredo-de-teste"
TTL = timedelta(minutes=30)
NOW = datetime(2026, 11, 10, 13, 0, tzinfo=UTC)

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = uuid.UUID("99999999-9999-9999-9999-999999999999")
CONVERSATION = uuid.UUID("22222222-2222-2222-2222-222222222222")
OTHER_CONVERSATION = uuid.UUID("88888888-8888-8888-8888-888888888888")

PROVIDER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER_PROVIDER = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
SERVICE = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
RESOURCE = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
STARTS = datetime(2026, 11, 11, 17, 0, tzinfo=UTC)


@pytest.fixture
def signer() -> SlotTokenSigner:
    return SlotTokenSigner(SECRET, ttl=TTL)


def sign(
    signer: SlotTokenSigner,
    *,
    resource: uuid.UUID | None = None,
    tenant: uuid.UUID = TENANT,
    conversation: uuid.UUID = CONVERSATION,
    now: datetime = NOW,
) -> str:
    claims = SlotClaims(
        provider_id=PROVIDER, service_id=SERVICE, starts_at=STARTS, resource_id=resource
    )
    return signer.sign(claims, tenant_id=tenant, conversation_id=conversation, now=now)


def verify(
    signer: SlotTokenSigner,
    token: str,
    *,
    tenant: uuid.UUID = TENANT,
    conversation: uuid.UUID = CONVERSATION,
    now: datetime = NOW,
) -> SlotClaims:
    return signer.verify(token, tenant_id=tenant, conversation_id=conversation, now=now)


def reason_of(signer: SlotTokenSigner, token: str, **kwargs: object) -> str:
    with pytest.raises(SlotTokenError) as excinfo:
        verify(signer, token, **kwargs)  # type: ignore[arg-type]
    return excinfo.value.reason


def body_of(token: str) -> dict[str, object]:
    payload = token.split(".")[0]
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert isinstance(decoded, dict)
    return decoded


def reencode(body: dict[str, object]) -> str:
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# --------------------------------------- ida e volta ---------------------------------------


def test_token_carrega_o_que_o_motor_ofereceu(signer: SlotTokenSigner) -> None:
    claims = verify(signer, sign(signer))

    assert claims.provider_id == PROVIDER
    assert claims.service_id == SERVICE
    assert claims.starts_at == STARTS
    assert claims.resource_id is None


def test_token_carrega_o_recurso_quando_ha(signer: SlotTokenSigner) -> None:
    assert verify(signer, sign(signer, resource=RESOURCE)).resource_id == RESOURCE


def test_horario_volta_em_utc_mesmo_assinado_em_outra_timezone(signer: SlotTokenSigner) -> None:
    sao_paulo = datetime(2026, 11, 11, 14, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))
    claims = SlotClaims(provider_id=PROVIDER, service_id=SERVICE, starts_at=sao_paulo)
    token = signer.sign(claims, tenant_id=TENANT, conversation_id=CONVERSATION, now=NOW)

    assert verify(signer, token).starts_at == STARTS


def test_o_formato_e_corpo_ponto_assinatura_em_base64url(signer: SlotTokenSigner) -> None:
    token = sign(signer)

    assert token.count(".") == 1
    assert all(ch.isalnum() or ch in "-_." for ch in token)
    # O corpo e legivel (JSON) — o que o torna seguro e a assinatura, nao o sigilo.
    assert set(body_of(token)) == {"v", "p", "s", "r", "a", "e"}


def test_espaco_ao_redor_do_token_e_tolerado(signer: SlotTokenSigner) -> None:
    """O modelo copia o token de um JSON; uma quebra de linha na ponta nao pode invalidar."""
    assert verify(signer, f"  {sign(signer)}\n").provider_id == PROVIDER


# ------------------------------------ adulteracao ------------------------------------


def test_corpo_adulterado_e_rejeitado(signer: SlotTokenSigner) -> None:
    token = sign(signer)
    body = body_of(token)
    body["p"] = OTHER_PROVIDER.hex  # trocar o profissional, mantendo a assinatura antiga
    forged = f"{reencode(body)}.{token.split('.')[1]}"

    assert reason_of(signer, forged) == "bad_signature"


def test_horario_adulterado_e_rejeitado(signer: SlotTokenSigner) -> None:
    token = sign(signer)
    body = body_of(token)
    body["a"] = (STARTS + timedelta(hours=1)).isoformat()
    forged = f"{reencode(body)}.{token.split('.')[1]}"

    assert reason_of(signer, forged) == "bad_signature"


def test_validade_estendida_e_rejeitada(signer: SlotTokenSigner) -> None:
    token = sign(signer)
    body = body_of(token)
    body["e"] = int((NOW + timedelta(days=30)).timestamp())
    forged = f"{reencode(body)}.{token.split('.')[1]}"

    assert reason_of(signer, forged) == "bad_signature"


def test_qualquer_caractere_do_corpo_alterado_e_rejeitado(signer: SlotTokenSigner) -> None:
    token = sign(signer)
    payload, signature = token.split(".")
    for index in range(len(payload)):
        swapped = "A" if payload[index] != "A" else "B"
        tampered = f"{payload[:index]}{swapped}{payload[index + 1 :]}.{signature}"
        assert reason_of(signer, tampered) == "bad_signature", index


def test_assinatura_adulterada_e_rejeitada(signer: SlotTokenSigner) -> None:
    payload, signature = sign(signer).split(".")
    swapped = "A" if signature[0] != "A" else "B"

    assert reason_of(signer, f"{payload}.{swapped}{signature[1:]}") == "bad_signature"


def test_assinatura_truncada_e_rejeitada(signer: SlotTokenSigner) -> None:
    payload, signature = sign(signer).split(".")

    assert reason_of(signer, f"{payload}.{signature[:-4]}") == "bad_signature"


def test_caractere_solto_na_assinatura_nao_e_descartado(signer: SlotTokenSigner) -> None:
    """O decodificador do base64 ignora lixo; aceitar isso daria varias formas do mesmo token."""
    payload, signature = sign(signer).split(".")

    assert reason_of(signer, f"{payload}.{signature[:10]}!{signature[10:]}") == "malformed"
    assert reason_of(signer, f"{payload}.{signature}=") == "malformed"


def test_token_sem_assinatura_e_rejeitado(signer: SlotTokenSigner) -> None:
    payload = sign(signer).split(".")[0]

    assert reason_of(signer, payload) == "malformed"
    assert reason_of(signer, f"{payload}.") == "malformed"


def test_token_assinado_com_outra_chave_e_rejeitado(signer: SlotTokenSigner) -> None:
    forged = sign(SlotTokenSigner("outro-segredo", ttl=TTL))

    assert reason_of(signer, forged) == "bad_signature"


def test_token_fabricado_do_zero_e_rejeitado(signer: SlotTokenSigner) -> None:
    """O que o modelo faria: montar o corpo certo, sem a chave."""
    body = {
        "v": 1,
        "p": PROVIDER.hex,
        "s": SERVICE.hex,
        "r": None,
        "a": STARTS.isoformat(),
        "e": int((NOW + TTL).timestamp()),
    }
    fake_signature = base64.urlsafe_b64encode(b"x" * 32).rstrip(b"=").decode()

    assert reason_of(signer, f"{reencode(body)}.{fake_signature}") == "bad_signature"


# ------------------------------- contexto: conversa e tenant -------------------------------


def test_token_de_outra_conversa_e_rejeitado(signer: SlotTokenSigner) -> None:
    token = sign(signer, conversation=OTHER_CONVERSATION)

    assert reason_of(signer, token) == "bad_signature"


def test_token_de_outro_tenant_e_rejeitado(signer: SlotTokenSigner) -> None:
    token = sign(signer, tenant=OTHER_TENANT)

    assert reason_of(signer, token) == "bad_signature"


def test_mesmo_horario_em_conversas_diferentes_gera_tokens_diferentes(
    signer: SlotTokenSigner,
) -> None:
    assert sign(signer) != sign(signer, conversation=OTHER_CONVERSATION)


# ------------------------------------------ validade ------------------------------------------


def test_vale_ate_o_ultimo_instante_antes_do_vencimento(signer: SlotTokenSigner) -> None:
    token = sign(signer)

    assert verify(signer, token, now=NOW + TTL - timedelta(seconds=1)).provider_id == PROVIDER


def test_vencido_no_instante_do_vencimento(signer: SlotTokenSigner) -> None:
    token = sign(signer)

    assert reason_of(signer, token, now=NOW + TTL) == "expired"
    assert reason_of(signer, token, now=NOW + TTL + timedelta(days=1)) == "expired"


def test_validade_e_do_momento_da_oferta_nao_do_horario_do_slot(signer: SlotTokenSigner) -> None:
    """Um slot daqui a 3 semanas nao ganha 3 semanas de validade: a oferta e que envelhece."""
    claims = SlotClaims(
        provider_id=PROVIDER, service_id=SERVICE, starts_at=STARTS + timedelta(days=21)
    )
    token = signer.sign(claims, tenant_id=TENANT, conversation_id=CONVERSATION, now=NOW)

    assert reason_of(signer, token, now=NOW + TTL) == "expired"


def test_versao_desconhecida_e_recusada(
    signer: SlotTokenSigner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.scheduling.slot_token._VERSION", 2)
    token = sign(signer)
    monkeypatch.undo()

    assert reason_of(signer, token) == "unsupported_version"


# ---------------------------------------------- lixo ----------------------------------------------


@pytest.mark.parametrize(
    "garbage",
    [
        "",
        " ",
        ".",
        "..",
        "abc",
        "a.b.c",
        "eyJ...",
        "eyJ....assinatura",
        "token com espaco.no meio",
        "ação.assinatura",
        "a" * 5000,
    ],
)
def test_lixo_e_rejeitado_sem_excecao_inesperada(signer: SlotTokenSigner, garbage: str) -> None:
    assert reason_of(signer, garbage) in {"malformed", "bad_signature"}


# --------------------------------------------- uso ---------------------------------------------


def test_segredo_vazio_e_recusado() -> None:
    with pytest.raises(ValueError, match="segredo"):
        SlotTokenSigner("", ttl=TTL)


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(minutes=-1)])
def test_validade_nao_positiva_e_recusada(ttl: timedelta) -> None:
    with pytest.raises(ValueError, match="validade"):
        SlotTokenSigner(SECRET, ttl=ttl)


def test_instante_ingenuo_e_recusado(signer: SlotTokenSigner) -> None:
    naive = datetime(2026, 11, 10, 13, 0)
    with pytest.raises(ValueError, match="ingenuo"):
        signer.sign(
            SlotClaims(provider_id=PROVIDER, service_id=SERVICE, starts_at=STARTS),
            tenant_id=TENANT,
            conversation_id=CONVERSATION,
            now=naive,
        )
    with pytest.raises(ValueError, match="ingenuo"):
        verify(signer, sign(signer), now=naive)
    with pytest.raises(ValueError, match="ingenuo"):
        SlotClaims(provider_id=PROVIDER, service_id=SERVICE, starts_at=naive)
