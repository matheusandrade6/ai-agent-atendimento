"""Tools de agendamento (S15): contrato, validacao de argumentos e formato do que o modelo le.

Sem banco. O comportamento contra Postgres esta em
`tests/integration/test_tools_scheduling.py`.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.agent.temporal import evidence_from_tool_result
from app.agent.tools.check_availability import CheckAvailabilityArgs, check_availability_tool
from app.agent.tools.common import (
    RECHECK_AVAILABILITY,
    failure,
    human_date,
    human_slot,
    human_time,
    price_line,
)
from app.agent.tools.confirm_appointment import (
    NOTES_KEY,
    ConfirmAppointmentArgs,
    confirm_appointment_tool,
)
from app.agent.tools.find_appointments import find_appointments_tool
from app.agent.tools.hold_slot import HoldSlotArgs, hold_slot_tool
from app.agent.tools.save_contact_info import SaveContactInfoArgs, save_contact_info_tool
from app.calendar.resolver import no_calendars
from app.domain.models import Service

SP = ZoneInfo("America/Sao_Paulo")
TODAY = date(2026, 11, 10)
SERVICE = str(uuid.uuid4())
HOLD = str(uuid.uuid4())


def local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=SP)


# ------------------------------- como o horario e escrito -------------------------------


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (local(11, 14), "quarta-feira, 11 de novembro, às 14h"),
        (local(11, 14, 30), "quarta-feira, 11 de novembro, às 14h30"),
        (local(14, 9, 5), "sábado, 14 de novembro, às 9h05"),
        (local(15, 19), "domingo, 15 de novembro, às 19h"),
        (local(10, 8), "terça-feira, 10 de novembro, às 8h"),
    ],
)
def test_human_slot_escreve_como_gente_fala(moment: datetime, expected: str) -> None:
    assert human_slot(moment) == expected


def test_data_e_hora_separadas_servem_ao_template_de_confirmacao() -> None:
    moment = local(11, 16, 15)

    assert human_date(moment) == "quarta-feira, 11 de novembro"
    assert human_time(moment) == "16h15"


@pytest.mark.parametrize(
    "moment", [local(11, 8), local(11, 12), local(11, 14, 30), local(14, 9, 15)]
)
def test_o_texto_humano_e_lido_de_volta_pela_gramatica_do_guardrail(moment: datetime) -> None:
    """Se a gramatica temporal nao entende o `human`, o guardrail descartaria a resposta certa."""
    evidence = evidence_from_tool_result(
        {"human": human_slot(moment)}, timezone="America/Sao_Paulo", today=TODAY
    )

    assert moment.date() in evidence.dates
    assert moment.time() in evidence.times


def test_preco_em_reais_ou_nota() -> None:
    with_price = Service(price_cents=15050, price_note=None)
    with_note = Service(price_cents=None, price_note="sob avaliacao")
    free = Service(price_cents=None, price_note=None)

    assert price_line(with_price) == "R$ 150,50"
    assert price_line(with_note) == "sob avaliacao"
    assert price_line(free) == ""


def test_falha_recuperavel_e_erro_com_proximo_passo() -> None:
    result = failure("hold_expired", "acabou", RECHECK_AVAILABILITY)

    assert result.is_error
    assert result.content == {
        "status": "hold_expired",
        "message": "acabou",
        "suggested_action": "call check_availability again",
    }


# ----------------------------- o que o modelo nao consegue escrever -----------------------------


def _properties(spec_schema: Any) -> set[str]:
    return set(spec_schema["properties"])


def test_hold_slot_so_aceita_o_token() -> None:
    """Sem campo de horario, profissional ou servico, nao ha onde o modelo escrever um."""
    spec = hold_slot_tool()

    assert _properties(spec.input_schema) == {"slot_token"}
    assert spec.input_schema["required"] == ["slot_token"]
    assert set(HoldSlotArgs.model_fields) == {"slot_token"}


def test_hold_slot_descarta_horario_escrito_a_mao() -> None:
    """O motor passa ao handler so o que o modelo Pydantic declara."""
    validated = HoldSlotArgs.model_validate(
        {
            "slot_token": "x.y",
            "starts_at": "2026-11-11T03:00:00-03:00",
            "provider_id": str(uuid.uuid4()),
            "service_id": SERVICE,
        }
    )

    assert validated.model_dump() == {"slot_token": "x.y"}


def test_hold_slot_sem_token_e_invalido() -> None:
    with pytest.raises(ValidationError):
        HoldSlotArgs.model_validate({"starts_at": "2026-11-11T14:00:00-03:00"})


def test_confirm_appointment_so_conhece_a_reserva_nao_o_horario() -> None:
    spec = confirm_appointment_tool(no_calendars)

    assert _properties(spec.input_schema) == {"hold_id", "subject_name", "intake", "notes"}
    assert spec.input_schema["required"] == ["hold_id"]
    for forbidden in ("starts_at", "provider_id", "service_id", "slot_token"):
        assert forbidden not in spec.input_schema["properties"]


def test_nenhuma_tool_de_agendamento_aceita_tenant_ou_contato_como_argumento() -> None:
    specs = (
        check_availability_tool(no_calendars),
        hold_slot_tool(),
        confirm_appointment_tool(no_calendars),
        find_appointments_tool(),
        save_contact_info_tool(),
    )
    for spec in specs:
        serialized = json.dumps(spec.input_schema)
        assert "tenant_id" not in serialized, spec.name
        assert "contact_id" not in serialized, spec.name
        assert "conversation_id" not in serialized, spec.name


def test_tipo_de_cada_tool_bate_com_a_tabela_da_11_4() -> None:
    assert check_availability_tool(no_calendars).kind == "read"
    assert hold_slot_tool().kind == "write"
    assert confirm_appointment_tool(no_calendars).kind == "write"
    assert find_appointments_tool().kind == "read"
    assert save_contact_info_tool().kind == "write"


# ---------------------------------- check_availability ----------------------------------


def _availability(**overrides: Any) -> dict[str, Any]:
    return {
        "service_id": SERVICE,
        "date_from": "2026-11-11",
        "date_to": "2026-11-18",
        **overrides,
    }


def test_check_availability_aceita_o_contrato_da_11_4() -> None:
    args = CheckAvailabilityArgs.model_validate(
        _availability(preferred_weekdays=["mon", "wed"], preferred_periods=["afternoon"], limit=5)
    )

    assert args.date_from == date(2026, 11, 11)
    assert args.preferred_weekdays == ["mon", "wed"]
    assert args.limit == 5
    assert args.offset == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"date_to": "2026-11-10"},  # antes do inicio
        {"date_to": "2027-03-01"},  # janela maior que o teto
        {"service_id": "nao-e-uuid"},
        {"limit": 0},
        {"limit": 11},
        {"offset": -1},
        {"preferred_weekdays": ["segunda"]},
        {"preferred_periods": ["madrugada"]},
        {"date_from": "amanha"},
    ],
)
def test_check_availability_recusa_argumento_invalido(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CheckAvailabilityArgs.model_validate(_availability(**overrides))


def test_check_availability_de_um_dia_so_e_valido() -> None:
    args = CheckAvailabilityArgs.model_validate(_availability(date_to="2026-11-11"))

    assert args.date_from == args.date_to


# ---------------------------------- confirm_appointment ----------------------------------


def test_confirm_appointment_texto_em_branco_vira_ausente() -> None:
    args = ConfirmAppointmentArgs.model_validate(
        {"hold_id": HOLD, "subject_name": "   ", "notes": ""}
    )

    assert args.subject_name is None
    assert args.notes is None


def test_confirm_appointment_recusa_hold_id_que_nao_e_uuid() -> None:
    with pytest.raises(ValidationError):
        ConfirmAppointmentArgs.model_validate({"hold_id": "inventado"})


def test_confirm_appointment_recusa_intake_exagerado() -> None:
    with pytest.raises(ValidationError, match="campos"):
        ConfirmAppointmentArgs.model_validate(
            {"hold_id": HOLD, "intake": {f"k{n}": n for n in range(41)}}
        )
    with pytest.raises(ValidationError, match="grande"):
        ConfirmAppointmentArgs.model_validate({"hold_id": HOLD, "intake": {"texto": "x" * 5000}})


def test_confirmacao_tem_uma_chave_por_reserva_e_nao_por_argumento() -> None:
    """Observacao diferente na repeticao nao pode confirmar duas vezes (invariante 5)."""
    spec = confirm_appointment_tool(no_calendars)
    ctx: Any = None  # a chave so olha os argumentos

    first = spec.key_for(ctx, {"hold_id": HOLD, "notes": "primeira"})
    second = spec.key_for(ctx, {"hold_id": HOLD, "notes": "segunda", "subject_name": "Mia"})
    other_hold = spec.key_for(ctx, {"hold_id": str(uuid.uuid4())})

    assert first == second
    assert first != other_hold


def test_observacao_fica_em_chave_reservada_do_intake() -> None:
    assert NOTES_KEY.startswith("_")


# ----------------------------------- save_contact_info -----------------------------------


def test_save_contact_info_aceita_o_basico() -> None:
    args = SaveContactInfoArgs.model_validate(
        {
            "contact_name": "  Carla  ",
            "email": "carla@example.com",
            "subject": {"kind": "pet", "name": "Mia", "attributes": {"especie": "gato"}},
            "attributes": {"prefere_manha": True, "idade": 31},
        }
    )

    assert args.contact_name == "Carla"
    assert args.subject is not None
    assert args.subject.attributes == {"especie": "gato"}


def test_save_contact_info_sem_nada_para_salvar_e_invalido() -> None:
    with pytest.raises(ValidationError, match="nada para salvar"):
        SaveContactInfoArgs.model_validate({})
    with pytest.raises(ValidationError, match="nada para salvar"):
        SaveContactInfoArgs.model_validate({"contact_name": "  "})


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "sem-arroba"},
        {"attributes": {"Especie": "gato"}},  # maiuscula
        {"attributes": {"1especie": "gato"}},  # comeca com digito
        {"attributes": {"com espaco": "x"}},
        {"attributes": {"k": "x" * 201}},
        {"attributes": {f"k{n}": n for n in range(21)}},
        {"attributes": {"lista": [1, 2]}},  # so escalar
        {"subject": {"kind": "robo", "name": "R2"}},
        {"subject": {"kind": "pet", "name": "   "}},
        {"subject": {"kind": "pet"}},
    ],
)
def test_save_contact_info_recusa_o_que_foge_da_forma(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SaveContactInfoArgs.model_validate(payload)


def test_save_contact_info_so_le_texto_que_vai_ser_gravado() -> None:
    args = SaveContactInfoArgs.model_validate(
        {
            "contact_name": "Carla",
            "subject": {"kind": "pet", "name": "Mia", "attributes": {"idade": 3, "raca": "SRD"}},
            "attributes": {"cidade": "Sao Paulo", "ativo": True},
        }
    )

    assert sorted(args.texts()) == ["Carla", "Mia", "SRD", "Sao Paulo"]
