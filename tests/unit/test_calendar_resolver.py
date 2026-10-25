"""`GoogleCalendarResolver`: quem enxerga a agenda de quem (D-42, D-48).

O que importa e o lado seguro do erro: dono sem credencial resolvida volta `None`, que o motor
de disponibilidade le como "nao consegui consultar" — nunca como "livre".
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest

from app.calendar.google import GoogleCalendarProvider
from app.calendar.resolver import GoogleCalendarResolver, no_calendars
from app.core.config import Settings
from app.domain.models import Provider, Resource


class Secrets:
    async def get(self, ref: str) -> str:
        return "rt"


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def resolver(http: httpx.AsyncClient) -> GoogleCalendarResolver:
    return GoogleCalendarResolver(Settings(environment="test"), secrets=Secrets(), client=http)


def provider(
    *, calendar_id: str | None = "ana@cal", ref: str | None = "env:ANA", kind: str = "google"
) -> Provider:
    return Provider(
        id=uuid.uuid4(),
        name="Ana",
        calendar_id=calendar_id,
        credentials_ref=ref,
        calendar_provider=kind,
    )


def test_profissional_com_credencial_recebe_um_cliente_google(
    resolver: GoogleCalendarResolver,
) -> None:
    assert isinstance(resolver(provider()), GoogleCalendarProvider)


def test_mesma_credencial_reaproveita_o_cliente(resolver: GoogleCalendarResolver) -> None:
    """O cache de freebusy e o token moram na instancia (D-42): uma por `credentials_ref`."""
    assert resolver(provider(ref="env:A")) is resolver(provider(ref="env:A"))


def test_credenciais_diferentes_nao_dividem_cliente(resolver: GoogleCalendarResolver) -> None:
    assert resolver(provider(ref="env:A")) is not resolver(provider(ref="env:B"))


def test_agenda_propria_nao_tem_cliente(resolver: GoogleCalendarResolver) -> None:
    assert resolver(provider(calendar_id=None, ref=None)) is None


def test_calendario_sem_credencial_fica_de_fora(resolver: GoogleCalendarResolver) -> None:
    assert resolver(provider(ref=None)) is None
    assert resolver(provider(ref="")) is None


def test_calendario_que_nao_e_google_fica_de_fora(resolver: GoogleCalendarResolver) -> None:
    assert resolver(provider(kind="outlook")) is None


def test_recurso_com_calendario_fica_de_fora_em_vez_de_adivinhar_a_conta(
    resolver: GoogleCalendarResolver,
) -> None:
    room = Resource(id=uuid.uuid4(), name="Sala", calendar_id="sala@cal")

    assert resolver(room) is None


def test_recurso_sem_calendario_nao_tem_cliente(resolver: GoogleCalendarResolver) -> None:
    assert resolver(Resource(id=uuid.uuid4(), name="Sala", calendar_id=None)) is None


def test_sem_calendarios_ninguem_tem_cliente() -> None:
    assert no_calendars(provider()) is None
