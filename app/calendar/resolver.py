"""Qual cliente de calendario enxerga a agenda de quem (D-42, D-48).

`find_availability` e a tool de confirmacao recebem um `CalendarResolver`: dado um
profissional ou recurso, devolvem o cliente que fala com o calendario dele — ou `None`,
que o motor le como "nao consegui consultar" (o dono sai da conta, nunca vira "livre").

Uma instancia de `GoogleCalendarProvider` por `credentials_ref`: o cache de `freebusy` e o
access token moram nela, e e o `credentials_ref` que separa uma conta Google da outra.

Recurso com calendario: ainda nao suportado
-------------------------------------------
`resources` tem `calendar_id` mas nao tem `credentials_ref` (09), e a spec nao diz com que
conta o calendario de uma sala e lido. Em vez de escolher uma conta por palpite — ler a
agenda de um recurso com a credencial errada pode devolver "livre" para o que esta ocupado —,
o recurso com `calendar_id` fica **de fora** (falha fechada) e o aviso vai para o log.
Recurso sem `calendar_id` (agenda propria, so o banco conta) funciona normalmente.
"""

from __future__ import annotations

import httpx

from app.calendar.base import CalendarProvider
from app.calendar.google import GoogleCalendarProvider
from app.core.config import Settings
from app.core.secrets import SecretStore
from app.core.telemetry import get_logger
from app.domain.models import Provider, Resource

log = get_logger(__name__)

__all__ = ["GoogleCalendarResolver", "no_calendars"]


def no_calendars(owner: Provider | Resource) -> CalendarProvider | None:
    """Resolver de quem nao tem calendario nenhum: so agenda propria (so o banco) funciona."""
    return None


class GoogleCalendarResolver:
    def __init__(
        self,
        settings: Settings,
        *,
        secrets: SecretStore,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._secrets = secrets
        self._client = client
        self._by_credentials: dict[str, GoogleCalendarProvider] = {}

    def __call__(self, owner: Provider | Resource) -> CalendarProvider | None:
        if owner.calendar_id is None:
            return None
        if isinstance(owner, Resource):
            log.warning(
                "calendario_de_recurso_sem_credencial",
                resource_id=str(owner.id),
                detalhe="resources nao tem credentials_ref; recurso fica fora da oferta",
            )
            return None
        if owner.calendar_provider != "google" or not owner.credentials_ref:
            return None

        client = self._by_credentials.get(owner.credentials_ref)
        if client is None:
            client = GoogleCalendarProvider.from_settings(
                self._settings,
                credentials_ref=owner.credentials_ref,
                secrets=self._secrets,
                client=self._client,
            )
            self._by_credentials[owner.credentials_ref] = client
        return client
