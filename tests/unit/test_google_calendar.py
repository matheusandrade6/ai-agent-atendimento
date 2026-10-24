"""Google Calendar contra um Google falso (secao 14.3, aceite da Sessao S12).

O que importa aqui nao e o formato do JSON — e o **comportamento sob falha**: o insert que
funcionou mas cuja resposta se perdeu nao pode duplicar o evento, o `410 Gone` do
`syncToken` nao pode virar erro, o `403 rateLimitExceeded` retenta e o `403 forbidden` nao.

`FakeGoogle` guarda estado (eventos por `iCalUID`) porque "mesmo iCalUID duas vezes = 1
evento" e uma afirmacao sobre o **estado do calendario**, nao sobre quantas chamadas
saíram.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from app.calendar.base import (
    CalendarAuthError,
    CalendarNotFound,
    CalendarPermanentError,
    CalendarQueryError,
    CalendarSyncPending,
    EventDraft,
    EventPatch,
)
from app.calendar.google import FREEBUSY_MAX_CALENDARS, GoogleCalendarProvider
from app.core.secrets import EnvSecretStore, SecretNotFound, SecretStore

API = "https://calendar.example.test/calendar/v3"
TOKEN_URL = "https://oauth.example.test/token"
CAL = "prof@clinica.example.com"
CAL_URL = f"{API}/calendars/prof%40clinica.example.com/events"
FREEBUSY_URL = f"{API}/freeBusy"

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)  # 09:00 em America/Sao_Paulo
T1 = T0 + timedelta(hours=1)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Secrets:
    def __init__(self, value: str = "rt-secreto") -> None:
        self.value = value
        self.refs: list[str] = []

    async def get(self, ref: str) -> str:
        self.refs.append(ref)
        return self.value


class Harness:
    def __init__(self, provider: GoogleCalendarProvider, clock: Clock, sleeps: list[float]) -> None:
        self.provider = provider
        self.clock = clock
        self.sleeps = sleeps


def _google_error(status: int, reason: str) -> httpx.Response:
    body = {"error": {"code": status, "errors": [{"reason": reason}]}}
    return httpx.Response(status, json=body)


def _make(secrets: SecretStore | None = None, **overrides: Any) -> Harness:
    clock = Clock()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    kwargs: dict[str, Any] = {
        "credentials_ref": "env:GOOGLE_RT_TESTE",
        "secrets": secrets or Secrets(),
        "client_id": "cid",
        "client_secret": "csecret",
        "token_url": TOKEN_URL,
        "base_url": API,
        "max_tries": 4,
        "backoff_base": 1.0,
        "backoff_max": 30.0,
        "sleep": fake_sleep,
        "clock": clock,
        "jitter": lambda: 1.0,  # backoff exato: base * 2**(n-1)
    }
    kwargs.update(overrides)
    return Harness(GoogleCalendarProvider(**kwargs), clock, sleeps)


@pytest.fixture
def google() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        )
        yield router


class FakeGoogle:
    """Calendario falso com estado: `events` indexado por `iCalUID`."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.insert_calls = 0
        self.lose_first_response = False
        router.post(CAL_URL).mock(side_effect=self._insert)
        router.get(CAL_URL).mock(side_effect=self._list)

    def _insert(self, request: httpx.Request) -> httpx.Response:
        self.insert_calls += 1
        body = json.loads(request.content)
        uid = body["iCalUID"]
        if uid in self.events:
            return _google_error(409, "duplicate")
        stored = {**body, "id": f"ev-{uuid.uuid4().hex[:8]}", "status": "confirmed"}
        self.events[uid] = stored
        if self.lose_first_response and self.insert_calls == 1:
            return httpx.Response(503, json={})  # gravou, mas quem chamou nao soube
        return httpx.Response(200, json=stored)

    def _list(self, request: httpx.Request) -> httpx.Response:
        uid = request.url.params.get("iCalUID")
        return httpx.Response(200, json={"items": [self.events[uid]] if uid in self.events else []})


def _draft() -> EventDraft:
    return EventDraft(
        summary="Consulta — Rex",
        start=T0,
        end=T1,
        timezone="America/Sao_Paulo",
        description="Resumo do intake",
        tenant_ref="clinica-exemplo",
    )


# --------------------------------------------------------------------- freebusy


async def test_freebusy_devolve_intervalos_em_utc_por_calendario(
    google: respx.MockRouter,
) -> None:
    route = google.post(FREEBUSY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "calendars": {
                    "a": {
                        "busy": [
                            {"start": "2026-11-03T12:00:00-03:00", "end": "2026-11-03T13:00:00Z"}
                        ]
                    },
                    "b": {"busy": []},
                }
            },
        )
    )
    h = _make()

    busy = await h.provider.get_busy(["a", "b"], T0, T1)

    assert busy["b"] == []
    (interval,) = busy["a"]
    assert interval.start == datetime(2026, 11, 3, 15, 0, tzinfo=UTC)  # -03:00 -> UTC
    assert interval.end == datetime(2026, 11, 3, 13, 0, tzinfo=UTC)
    sent = json.loads(route.calls[0].request.content)
    assert sent["items"] == [{"id": "a"}, {"id": "b"}]
    assert route.calls[0].request.headers["Authorization"] == "Bearer at-1"


async def test_freebusy_agrupa_ate_50_calendarios_por_chamada(google: respx.MockRouter) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        items = json.loads(request.content)["items"]
        return httpx.Response(200, json={"calendars": {i["id"]: {"busy": []} for i in items}})

    route = google.post(FREEBUSY_URL).mock(side_effect=answer)
    ids = [f"cal-{n}@x" for n in range(120)]
    h = _make()

    busy = await h.provider.get_busy(ids, T0, T1)

    sizes = [len(json.loads(c.request.content)["items"]) for c in route.calls]
    assert sizes == [FREEBUSY_MAX_CALENDARS, FREEBUSY_MAX_CALENDARS, 20]
    assert set(busy) == set(ids)


async def test_freebusy_cache_de_60s_evita_a_segunda_chamada(google: respx.MockRouter) -> None:
    route = google.post(FREEBUSY_URL).mock(
        return_value=httpx.Response(200, json={"calendars": {"a": {"busy": []}}})
    )
    h = _make(cache_ttl=60.0)

    await h.provider.get_busy(["a"], T0, T1)
    h.clock.advance(59)
    await h.provider.get_busy(["a"], T0, T1)
    assert route.call_count == 1

    h.clock.advance(2)  # 61s depois da primeira
    await h.provider.get_busy(["a"], T0, T1)
    assert route.call_count == 2


async def test_freebusy_com_cache_parcial_so_busca_o_que_falta(google: respx.MockRouter) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        items = json.loads(request.content)["items"]
        return httpx.Response(200, json={"calendars": {i["id"]: {"busy": []} for i in items}})

    route = google.post(FREEBUSY_URL).mock(side_effect=answer)
    h = _make()

    await h.provider.get_busy(["a"], T0, T1)
    busy = await h.provider.get_busy(["a", "b"], T0, T1)

    assert set(busy) == {"a", "b"}
    assert json.loads(route.calls[1].request.content)["items"] == [{"id": "b"}]


async def test_escrever_no_calendario_invalida_o_cache_dele(google: respx.MockRouter) -> None:
    fake = FakeGoogle(google)
    route = google.post(FREEBUSY_URL).mock(
        return_value=httpx.Response(200, json={"calendars": {CAL: {"busy": []}}})
    )
    h = _make()

    await h.provider.get_busy([CAL], T0, T1)
    await h.provider.create_event(CAL, _draft(), "appt-1")
    await h.provider.get_busy([CAL], T0, T1)  # sem a invalidacao, serviria o "livre" antigo

    assert route.call_count == 2
    assert len(fake.events) == 1


async def test_calendario_sem_resposta_nao_vira_calendario_livre(google: respx.MockRouter) -> None:
    google.post(FREEBUSY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "calendars": {
                    "ok": {"busy": []},
                    "quebrado": {"errors": [{"domain": "global", "reason": "notFound"}]},
                }
            },
        )
    )
    h = _make()

    with pytest.raises(CalendarQueryError) as exc:
        await h.provider.get_busy(["ok", "quebrado", "sumiu"], T0, T1)

    assert exc.value.failed == {"quebrado": "notFound", "sumiu": "ausente na resposta"}
    assert "ok" in exc.value.partial  # o que respondeu nao se perde


# ----------------------------------------------------------------------- insert


async def test_insert_manda_ical_uid_propriedades_privadas_e_nao_notifica(
    google: respx.MockRouter,
) -> None:
    FakeGoogle(google)
    h = _make()

    event = await h.provider.create_event(CAL, _draft(), "appt-1")

    request = next(
        c.request
        for c in google.calls
        if c.request.method == "POST" and "events" in str(c.request.url)
    )
    body = json.loads(request.content)
    assert body["iCalUID"] == "appt-1"
    assert body["summary"] == "Consulta — Rex"
    assert body["extendedProperties"]["private"] == {
        "datamind_appointment_id": "appt-1",
        "datamind_tenant": "clinica-exemplo",
    }
    assert body["start"] == {
        "dateTime": "2026-11-03T09:00:00-03:00",
        "timeZone": "America/Sao_Paulo",
    }
    assert parse_qs(request.url.query.decode())["sendUpdates"] == ["none"]
    assert event.appointment_id == "appt-1"
    assert event.start == T0


async def test_insert_com_o_mesmo_ical_uid_duas_vezes_deixa_um_evento(
    google: respx.MockRouter,
) -> None:
    fake = FakeGoogle(google)
    h = _make()

    first = await h.provider.create_event(CAL, _draft(), "appt-1")
    second = await h.provider.create_event(CAL, _draft(), "appt-1")

    assert len(fake.events) == 1
    assert fake.insert_calls == 2  # a segunda chegou ao Google e foi recusada (409)
    assert second.id == first.id


async def test_insert_cuja_resposta_se_perdeu_nao_duplica_na_retentativa(
    google: respx.MockRouter,
) -> None:
    fake = FakeGoogle(google)
    fake.lose_first_response = True  # 1a: grava e devolve 503; 2a: 409 -> busca o existente
    h = _make()

    event = await h.provider.create_event(CAL, _draft(), "appt-1")

    assert len(fake.events) == 1
    assert event.id == fake.events["appt-1"]["id"]
    assert h.sleeps == [1.0]  # houve backoff entre as duas tentativas


async def test_insert_409_com_evento_cancelado_e_erro_permanente(google: respx.MockRouter) -> None:
    google.post(CAL_URL).mock(return_value=_google_error(409, "duplicate"))
    google.get(CAL_URL).mock(
        return_value=httpx.Response(200, json={"items": [{"id": "ev-1", "status": "cancelled"}]})
    )
    h = _make()

    with pytest.raises(CalendarPermanentError) as exc:
        await h.provider.create_event(CAL, _draft(), "appt-1")

    assert exc.value.status_code == 409


# ------------------------------------------------------------------ patch/delete


async def test_patch_manda_so_os_campos_alterados(google: respx.MockRouter) -> None:
    route = google.patch(f"{CAL_URL}/ev-1").mock(
        return_value=httpx.Response(200, json={"id": "ev-1", "status": "confirmed"})
    )
    h = _make()

    await h.provider.update_event(
        CAL,
        "ev-1",
        EventPatch(
            start=T0 + timedelta(days=1), end=T1 + timedelta(days=1), timezone="America/Sao_Paulo"
        ),
    )

    body = json.loads(route.calls[0].request.content)
    assert set(body) == {"start", "end"}
    assert body["start"]["dateTime"] == "2026-11-04T09:00:00-03:00"


async def test_patch_de_horario_sem_timezone_e_recusado(google: respx.MockRouter) -> None:
    h = _make()

    with pytest.raises(ValueError, match="timezone"):
        await h.provider.update_event(CAL, "ev-1", EventPatch(start=T0))


async def test_patch_em_evento_inexistente_e_not_found(google: respx.MockRouter) -> None:
    google.patch(f"{CAL_URL}/ev-x").mock(return_value=_google_error(404, "notFound"))
    h = _make()

    with pytest.raises(CalendarNotFound):
        await h.provider.update_event(CAL, "ev-x", EventPatch(summary="novo"))


@pytest.mark.parametrize("status", [200, 204, 404, 410])
async def test_delete_de_evento_ja_removido_conta_como_sucesso(
    google: respx.MockRouter, status: int
) -> None:
    google.delete(f"{CAL_URL}/ev-1").mock(return_value=httpx.Response(status))
    h = _make()

    await h.provider.delete_event(CAL, "ev-1")  # nao levanta


# --------------------------------------------------------------------------- sync


def _page(items: list[dict[str, Any]], **extra: Any) -> httpx.Response:
    return httpx.Response(200, json={"items": items, "timeZone": "America/Sao_Paulo", **extra})


async def test_sync_incremental_percorre_paginas_e_devolve_o_novo_token(
    google: respx.MockRouter,
) -> None:
    route = google.get(CAL_URL).mock(
        side_effect=[
            _page(
                [
                    {
                        "id": "e1",
                        "status": "confirmed",
                        "start": {"dateTime": "2026-11-03T09:00:00-03:00"},
                        "end": {"dateTime": "2026-11-03T10:00:00-03:00"},
                        "extendedProperties": {"private": {"datamind_appointment_id": "appt-1"}},
                    }
                ],
                nextPageToken="p2",
            ),
            _page([{"id": "e2", "status": "cancelled"}], nextSyncToken="tok-novo"),
        ]
    )
    h = _make()

    result = await h.provider.sync_changes(CAL, "tok-velho")

    assert result.full_sync is False
    assert result.next_sync_token == "tok-novo"
    assert [e.id for e in result.events] == ["e1", "e2"]
    assert result.events[0].appointment_id == "appt-1"
    assert result.events[0].start == T0
    assert result.events[1].status == "cancelled"
    assert result.events[1].start is None  # removido: o Google so manda id e status
    first, second = (c.request.url.params for c in route.calls)
    assert first["syncToken"] == "tok-velho"
    assert "timeMin" not in first  # incompativel com syncToken
    assert second["pageToken"] == "p2"
    assert second["syncToken"] == "tok-velho"


async def test_sync_com_410_gone_cai_para_full_sync(google: respx.MockRouter) -> None:
    route = google.get(CAL_URL).mock(
        side_effect=[
            _google_error(410, "fullSyncRequired"),
            _page(
                [
                    {
                        "id": "e1",
                        "status": "confirmed",
                        "start": {"dateTime": "2026-11-03T09:00:00-03:00"},
                        "end": {"dateTime": "2026-11-03T10:00:00-03:00"},
                    }
                ],
                nextSyncToken="tok-zero",
            ),
        ]
    )
    h = _make()

    result = await h.provider.sync_changes(CAL, "tok-vencido")

    assert result.full_sync is True  # o consumidor precisa saber que reler foi do zero
    assert result.next_sync_token == "tok-zero"
    assert [e.id for e in result.events] == ["e1"]
    retry_params = route.calls[1].request.url.params
    assert "syncToken" not in retry_params
    assert "timeMin" in retry_params  # full sync com janela, nao a historia inteira
    assert h.sleeps == []  # 410 nao e falha transitoria: nao ha backoff


async def test_sync_sem_token_e_full_sync_com_janela(google: respx.MockRouter) -> None:
    route = google.get(CAL_URL).mock(return_value=_page([], nextSyncToken="tok-1"))
    h = _make()

    result = await h.provider.sync_changes(CAL, None)

    assert result.full_sync is True
    params = route.calls[0].request.url.params
    assert "syncToken" not in params
    assert params["singleEvents"] == "true"


async def test_sync_410_repetido_sem_token_e_erro_permanente(google: respx.MockRouter) -> None:
    google.get(CAL_URL).mock(return_value=_google_error(410, "fullSyncRequired"))
    h = _make()

    with pytest.raises(CalendarPermanentError):
        await h.provider.sync_changes(CAL, "tok-vencido")


async def test_sync_le_evento_de_dia_inteiro_na_timezone_da_agenda(
    google: respx.MockRouter,
) -> None:
    google.get(CAL_URL).mock(
        return_value=_page(
            [
                {
                    "id": "feriado",
                    "status": "confirmed",
                    "start": {"date": "2026-11-15"},
                    "end": {"date": "2026-11-16"},
                }
            ],
            nextSyncToken="t",
        )
    )
    h = _make()

    result = await h.provider.sync_changes(CAL, "t0")

    (event,) = result.events
    assert event.all_day is True
    assert event.start == datetime(2026, 11, 15, 3, 0, tzinfo=UTC)  # 00:00 em Sao_Paulo


# ------------------------------------------------------------------------ retry


async def test_403_rate_limit_exceeded_retenta_com_backoff_exponencial(
    google: respx.MockRouter,
) -> None:
    route = google.post(FREEBUSY_URL).mock(
        side_effect=[
            _google_error(403, "rateLimitExceeded"),
            _google_error(403, "userRateLimitExceeded"),
            httpx.Response(200, json={"calendars": {"a": {"busy": []}}}),
        ]
    )
    h = _make()

    busy = await h.provider.get_busy(["a"], T0, T1)

    assert busy == {"a": []}
    assert route.call_count == 3
    assert h.sleeps == [1.0, 2.0]  # base * 2**(n-1)


async def test_403_rate_limit_persistente_vira_sync_pending(google: respx.MockRouter) -> None:
    route = google.post(FREEBUSY_URL).mock(return_value=_google_error(403, "rateLimitExceeded"))
    h = _make(max_tries=4)

    with pytest.raises(CalendarSyncPending) as exc:
        await h.provider.get_busy(["a"], T0, T1)

    assert exc.value.attempts == 4
    assert route.call_count == 4
    assert h.sleeps == [1.0, 2.0, 4.0]  # nao dorme depois da ultima tentativa


async def test_403_que_nao_e_limite_de_taxa_nao_e_retentado(google: respx.MockRouter) -> None:
    route = google.post(FREEBUSY_URL).mock(return_value=_google_error(403, "forbidden"))
    h = _make()

    with pytest.raises(CalendarPermanentError) as exc:
        await h.provider.get_busy(["a"], T0, T1)

    assert exc.value.status_code == 403
    assert route.call_count == 1
    assert h.sleeps == []


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_5xx_e_retentado_ate_passar(google: respx.MockRouter, status: int) -> None:
    route = google.post(FREEBUSY_URL).mock(
        side_effect=[
            httpx.Response(status, json={}),
            httpx.Response(200, json={"calendars": {"a": {"busy": []}}}),
        ]
    )
    h = _make()

    assert await h.provider.get_busy(["a"], T0, T1) == {"a": []}
    assert route.call_count == 2


async def test_retry_after_do_google_tem_precedencia_sobre_o_backoff(
    google: respx.MockRouter,
) -> None:
    google.post(FREEBUSY_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}, json={}),
            httpx.Response(200, json={"calendars": {"a": {"busy": []}}}),
        ]
    )
    h = _make()

    await h.provider.get_busy(["a"], T0, T1)

    assert h.sleeps == [7.0]


async def test_falha_de_transporte_e_retentada(google: respx.MockRouter) -> None:
    route = google.post(FREEBUSY_URL).mock(
        side_effect=[
            httpx.ConnectError("conexao caida"),
            httpx.Response(200, json={"calendars": {"a": {"busy": []}}}),
        ]
    )
    h = _make()

    assert await h.provider.get_busy(["a"], T0, T1) == {"a": []}
    assert route.call_count == 2


async def test_404_em_calendario_inexistente_nao_e_retentado(google: respx.MockRouter) -> None:
    route = google.post(CAL_URL).mock(return_value=_google_error(404, "notFound"))
    h = _make()

    with pytest.raises(CalendarNotFound):
        await h.provider.create_event(CAL, _draft(), "appt-1")

    assert route.call_count == 1


# --------------------------------------------------------------------------- auth


async def test_refresh_token_vem_do_secret_store_pelo_credentials_ref(
    google: respx.MockRouter,
) -> None:
    google.post(FREEBUSY_URL).mock(
        return_value=httpx.Response(200, json={"calendars": {"a": {"busy": []}}})
    )
    secrets = Secrets("rt-do-cliente")
    h = _make(secrets)

    await h.provider.get_busy(["a"], T0, T1)

    assert secrets.refs == ["env:GOOGLE_RT_TESTE"]
    form = parse_qs(google.calls[0].request.content.decode())
    assert form["grant_type"] == ["refresh_token"]
    assert form["refresh_token"] == ["rt-do-cliente"]
    assert form["client_id"] == ["cid"]


async def test_access_token_e_reaproveitado_ate_perto_de_expirar(google: respx.MockRouter) -> None:
    google.post(FREEBUSY_URL).mock(
        side_effect=lambda _: httpx.Response(200, json={"calendars": {"a": {"busy": []}}})
    )
    h = _make(cache_ttl=0.0)  # desliga o cache de freebusy: so o token esta sob teste
    token_route = google.routes[0]

    await h.provider.get_busy(["a"], T0, T1)
    h.clock.advance(3000)  # 3600s de vida - 60s de margem = 3540s
    await h.provider.get_busy(["a"], T0, T1)
    assert token_route.call_count == 1

    h.clock.advance(600)
    await h.provider.get_busy(["a"], T0, T1)
    assert token_route.call_count == 2


async def test_401_forca_uma_renovacao_e_repete_a_chamada(google: respx.MockRouter) -> None:
    google.routes[0].mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "at-velho", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "at-novo", "expires_in": 3600}),
        ]
    )
    route = google.post(FREEBUSY_URL).mock(
        side_effect=[
            _google_error(401, "authError"),
            httpx.Response(200, json={"calendars": {"a": {"busy": []}}}),
        ]
    )
    h = _make()

    assert await h.provider.get_busy(["a"], T0, T1) == {"a": []}
    assert route.calls[0].request.headers["Authorization"] == "Bearer at-velho"
    assert route.calls[1].request.headers["Authorization"] == "Bearer at-novo"


async def test_401_mesmo_com_token_novo_e_erro_de_autenticacao(google: respx.MockRouter) -> None:
    google.post(FREEBUSY_URL).mock(return_value=_google_error(401, "authError"))
    h = _make()

    with pytest.raises(CalendarAuthError):
        await h.provider.get_busy(["a"], T0, T1)


async def test_invalid_grant_e_erro_de_autenticacao_sem_retentativa(
    google: respx.MockRouter,
) -> None:
    google.routes[0].mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    h = _make()

    with pytest.raises(CalendarAuthError, match="invalid_grant"):
        await h.provider.get_busy(["a"], T0, T1)

    assert google.routes[0].call_count == 1
    assert h.sleeps == []


async def test_endpoint_oauth_fora_do_ar_vira_sync_pending(google: respx.MockRouter) -> None:
    google.routes[0].mock(return_value=httpx.Response(503, json={}))
    h = _make(max_tries=3)

    with pytest.raises(CalendarSyncPending):
        await h.provider.get_busy(["a"], T0, T1)


async def test_segredo_ausente_e_erro_de_autenticacao(google: respx.MockRouter) -> None:
    h = _make(EnvSecretStore({}))

    with pytest.raises(CalendarAuthError, match="env:GOOGLE_RT_TESTE"):
        await h.provider.get_busy(["a"], T0, T1)


# ------------------------------------------------------------------ secret store


async def test_env_secret_store_resolve_o_ponteiro() -> None:
    store = EnvSecretStore({"GOOGLE_RT_X": "valor"})

    assert await store.get("env:GOOGLE_RT_X") == "valor"


@pytest.mark.parametrize("ref", ["env:NAO_EXISTE", "vault:secret/x", "sem-esquema"])
async def test_env_secret_store_recusa_ponteiro_que_nao_resolve(ref: str) -> None:
    with pytest.raises(SecretNotFound):
        await EnvSecretStore({"GOOGLE_RT_X": "valor"}).get(ref)
