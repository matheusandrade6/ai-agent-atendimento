"""Google Calendar (secao 14.3) — unico lugar do codigo que sabe que o Google existe.

HTTP direto com `httpx`, sem o SDK do Google: o SDK e sincrono, puxa uma arvore de
dependencias grande e esconde o que mais importa aqui — o status e o `reason` de cada
resposta, que e o que decide entre retentar, desistir ou tratar como sucesso.

Tres decisoes que o resto do arquivo obedece:

1. **Idempotencia pela chave natural (invariante 5).** `events.insert` leva
   `iCalUID = appointment_id`. O Google recusa um segundo insert com o mesmo `iCalUID`
   (`409`); nesse caso procuramos o evento existente e o devolvemos. E isso que torna
   seguro retentar um insert cuja resposta se perdeu — o caso em que a primeira
   tentativa *funcionou*.
2. **Nunca tratar silencio como "livre" (invariante 1).** Calendario que `freebusy`
   nao respondeu levanta `CalendarQueryError`; nao some do resultado.
3. **Falha transitoria persistente vira `CalendarSyncPending`**, nao um erro opaco: quem
   chama marca o agendamento `pending_sync` (`app.calendar.sync_state`).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import date, datetime, timedelta
from typing import Any, Final
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from app.calendar.base import (
    CalendarAuthError,
    CalendarNotFound,
    CalendarPermanentError,
    CalendarQueryError,
    CalendarSyncPending,
    EventDraft,
    EventPatch,
    EventStatus,
    ExternalEvent,
    Interval,
    SyncResult,
)
from app.core.config import Settings
from app.core.secrets import SecretNotFound, SecretStore
from app.core.time import as_utc, now_utc

#: Teto de `items` por chamada de `freebusy` (`calendarExpansionMax`).
FREEBUSY_MAX_CALENDARS: Final = 50

_RETRYABLE_403_REASONS: Final = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
_EVENTS_PAGE_SIZE: Final = 250
#: Margem para nao usar um access token que expira no meio da chamada.
_TOKEN_SKEW_SECONDS: Final = 60.0

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class GoogleCalendarProvider:
    """`CalendarProvider` sobre a Calendar API v3, para **um** `credentials_ref`.

    Uma instancia por conjunto de credenciais: o cache de `freebusy` e o access token
    vivem nela, e misturar credenciais de tenants diferentes na mesma instancia
    misturaria os dois.
    """

    def __init__(
        self,
        *,
        credentials_ref: str,
        secrets: SecretStore,
        client_id: str,
        client_secret: str,
        token_url: str = "https://oauth2.googleapis.com/token",
        base_url: str = "https://www.googleapis.com/calendar/v3",
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        max_tries: int = 5,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
        cache_ttl: float = 60.0,
        full_sync_days_back: int = 30,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._credentials_ref = credentials_ref
        self._secrets = secrets
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = token_url
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self._max_tries = max_tries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._cache_ttl = cache_ttl
        self._full_sync_days_back = full_sync_days_back
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter

        self._access_token: str | None = None
        self._access_token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        # (calendar_id, start, end) -> (expira_em, intervalos)
        self._busy_cache: dict[tuple[str, datetime, datetime], tuple[float, list[Interval]]] = {}

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        credentials_ref: str,
        secrets: SecretStore,
        client: httpx.AsyncClient | None = None,
    ) -> GoogleCalendarProvider:
        return cls(
            credentials_ref=credentials_ref,
            secrets=secrets,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            token_url=settings.google_token_url,
            base_url=settings.google_calendar_base_url,
            client=client,
            max_tries=settings.google_max_tries,
            backoff_base=settings.google_backoff_base_seconds,
            backoff_max=settings.google_backoff_max_seconds,
            cache_ttl=settings.google_freebusy_cache_seconds,
            full_sync_days_back=settings.google_full_sync_days_back,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ leitura

    async def get_busy(
        self, calendar_ids: list[str], start: datetime, end: datetime
    ) -> dict[str, list[Interval]]:
        """Ocupacao por calendario em `[start, end)`.

        Calendarios com cache valido (60s, por calendario e janela) nao vao a rede; os
        demais seguem em lotes de ate 50 por chamada.
        """
        start_utc, end_utc = as_utc(start), as_utc(end)
        now = self._clock()
        result: dict[str, list[Interval]] = {}
        missing: list[str] = []
        for calendar_id in dict.fromkeys(calendar_ids):  # dedupe preservando ordem
            cached = self._busy_cache.get((calendar_id, start_utc, end_utc))
            if cached is not None and cached[0] > now:
                result[calendar_id] = list(cached[1])
            else:
                missing.append(calendar_id)

        failed: dict[str, str] = {}
        for offset in range(0, len(missing), FREEBUSY_MAX_CALENDARS):
            batch = missing[offset : offset + FREEBUSY_MAX_CALENDARS]
            answered, errors = await self._freebusy(batch, start_utc, end_utc)
            expires_at = self._clock() + self._cache_ttl
            for calendar_id, intervals in answered.items():
                self._busy_cache[(calendar_id, start_utc, end_utc)] = (expires_at, intervals)
                result[calendar_id] = list(intervals)
            failed.update(errors)

        if failed:
            raise CalendarQueryError(failed, result)
        return result

    async def _freebusy(
        self, calendar_ids: Sequence[str], start: datetime, end: datetime
    ) -> tuple[dict[str, list[Interval]], dict[str, str]]:
        response = await self._request(
            "POST",
            "/freeBusy",
            json={
                "timeMin": _iso(start),
                "timeMax": _iso(end),
                "items": [{"id": calendar_id} for calendar_id in calendar_ids],
            },
        )
        calendars: dict[str, Any] = response.json().get("calendars", {})
        answered: dict[str, list[Interval]] = {}
        failed: dict[str, str] = {}
        for calendar_id in calendar_ids:
            entry = calendars.get(calendar_id)
            if entry is None:
                failed[calendar_id] = "ausente na resposta"
            elif entry.get("errors"):
                failed[calendar_id] = str(entry["errors"][0].get("reason", "erro desconhecido"))
            else:
                answered[calendar_id] = [
                    Interval(start=_parse_instant(b["start"]), end=_parse_instant(b["end"]))
                    for b in entry.get("busy", [])
                ]
        return answered, failed

    # ------------------------------------------------------------------ escrita

    async def create_event(
        self, calendar_id: str, event: EventDraft, idempotency_key: str
    ) -> ExternalEvent:
        """Cria o evento com `iCalUID = idempotency_key`; repetir devolve o mesmo evento."""
        response = await self._request(
            "POST",
            f"/calendars/{_q(calendar_id)}/events",
            params={"sendUpdates": "none"},
            json=_insert_body(event, idempotency_key),
            tolerate={409},
        )
        self._invalidate(calendar_id)
        if response.status_code == 409:
            return await self._find_by_ical_uid(calendar_id, idempotency_key)
        return _parse_event(response.json())

    async def _find_by_ical_uid(self, calendar_id: str, ical_uid: str) -> ExternalEvent:
        response = await self._request(
            "GET",
            f"/calendars/{_q(calendar_id)}/events",
            params={"iCalUID": ical_uid, "showDeleted": "true"},
        )
        for item in response.json().get("items", []):
            existing = _parse_event(item)
            if existing.status != "cancelled":
                return existing
        # 409 com o evento cancelado: o iCalUID continua reservado, e ressuscitar um
        # evento apagado por outra pessoa nao e decisao deste modulo.
        raise CalendarPermanentError(
            f"iCalUID {ical_uid!r} ja existe em {calendar_id!r} mas nao ha evento ativo",
            status_code=409,
        )

    async def update_event(
        self, calendar_id: str, event_id: str, patch: EventPatch
    ) -> ExternalEvent:
        response = await self._request(
            "PATCH",
            f"/calendars/{_q(calendar_id)}/events/{_q(event_id)}",
            params={"sendUpdates": "none"},
            json=_patch_body(patch),
        )
        self._invalidate(calendar_id)
        return _parse_event(response.json())

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        """Remove o evento. Ja removido (`404`/`410`) conta como sucesso: o estado
        desejado — evento inexistente — e o que existe."""
        await self._request(
            "DELETE",
            f"/calendars/{_q(calendar_id)}/events/{_q(event_id)}",
            params={"sendUpdates": "none"},
            tolerate={404, 410},
        )
        self._invalidate(calendar_id)

    # --------------------------------------------------------------------- sync

    async def sync_changes(self, calendar_id: str, sync_token: str | None) -> SyncResult:
        """Mudancas desde `sync_token`; sem token (ou com token expirado), leitura cheia.

        `410 Gone` e o Google dizendo que o token venceu (13.5): descartamos o token e
        relemos do zero, sinalizando `full_sync=True` para o consumidor.
        """
        full_sync = sync_token is None
        token = sync_token
        for _ in range(2):
            outcome = await self._list_all(calendar_id, token)
            if outcome is not None:
                events, next_token = outcome
                return SyncResult(events=events, next_sync_token=next_token, full_sync=full_sync)
            token, full_sync = None, True
        raise CalendarPermanentError(
            f"{calendar_id!r}: Google devolveu 410 mesmo sem syncToken", status_code=410
        )

    async def _list_all(
        self, calendar_id: str, sync_token: str | None
    ) -> tuple[list[ExternalEvent], str] | None:
        """Percorre todas as paginas. `None` = `410 Gone` (token vencido)."""
        events: list[ExternalEvent] = []
        page_token: str | None = None
        calendar_tz = "UTC"
        while True:
            params: dict[str, str] = {
                "singleEvents": "true",
                "showDeleted": "true",
                "maxResults": str(_EVENTS_PAGE_SIZE),
            }
            if sync_token is not None:
                params["syncToken"] = sync_token
            else:
                since = now_utc() - timedelta(days=self._full_sync_days_back)
                params["timeMin"] = _iso(since)
            if page_token is not None:
                params["pageToken"] = page_token

            response = await self._request(
                "GET", f"/calendars/{_q(calendar_id)}/events", params=params, tolerate={410}
            )
            if response.status_code == 410:
                return None
            body: dict[str, Any] = response.json()
            calendar_tz = str(body.get("timeZone", calendar_tz))
            events.extend(_parse_event(item, default_tz=calendar_tz) for item in body["items"])
            page_token = body.get("nextPageToken")
            if page_token is None:
                return events, str(body["nextSyncToken"])

    # --------------------------------------------------------------------- http

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        tolerate: set[int] | None = None,
    ) -> httpx.Response:
        """Uma chamada autenticada, com a politica de retentativa da 14.3.

        Retenta com backoff exponencial (e `Retry-After` quando vem) em `5xx`, `429`,
        `403 rateLimitExceeded` e falha de transporte. `401` forca uma renovacao do
        access token, uma unica vez. Esgotadas as tentativas: `CalendarSyncPending`.
        """
        tolerated = tolerate or set()
        refreshed_after_401 = False
        last_error = "sem tentativa"
        for attempt in range(1, self._max_tries + 1):
            token = await self._get_access_token()
            try:
                response = await self._client.request(
                    method,
                    f"{self._base_url}{path}",
                    params=params,
                    json=json,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                last_error = f"falha de transporte: {exc}"
                await self._backoff(attempt, retry_after=None)
                continue

            status = response.status_code
            if status < 300 or status in tolerated:
                return response
            if status == 401 and not refreshed_after_401:
                refreshed_after_401 = True
                self._access_token = None
                continue
            if status == 401:
                raise CalendarAuthError("401 do Google mesmo com access token novo")
            if _is_retryable(response):
                last_error = f"{status} {_error_reason(response)}"
                await self._backoff(attempt, retry_after=_retry_after(response))
                continue
            raise _permanent_error(response)

        raise CalendarSyncPending(
            f"Google Calendar indisponivel apos {self._max_tries} tentativas: {last_error}",
            attempts=self._max_tries,
        )

    async def _backoff(self, attempt: int, *, retry_after: float | None) -> None:
        if attempt >= self._max_tries:
            return  # ultima tentativa: nao dormir antes de desistir
        delay = retry_after
        if delay is None:
            exponential = min(self._backoff_base * 2 ** (attempt - 1), self._backoff_max)
            delay = exponential * (0.5 + self._jitter() / 2)  # jitter em [50%, 100%]
        await self._sleep(min(delay, self._backoff_max))

    # --------------------------------------------------------------------- auth

    async def _get_access_token(self) -> str:
        async with self._token_lock:
            if self._access_token is not None and self._clock() < self._access_token_expires_at:
                return self._access_token
            return await self._refresh_access_token()

    async def _refresh_access_token(self) -> str:
        try:
            refresh_token = await self._secrets.get(self._credentials_ref)
        except SecretNotFound as exc:
            raise CalendarAuthError(str(exc)) from exc

        for attempt in range(1, self._max_tries + 1):
            try:
                response = await self._client.post(
                    self._token_url,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                    },
                )
            except httpx.HTTPError:
                await self._backoff(attempt, retry_after=None)
                continue
            if response.status_code >= 500 or response.status_code == 429:
                await self._backoff(attempt, retry_after=_retry_after(response))
                continue
            if response.status_code >= 400:
                # `invalid_grant`: token revogado/expirado. So o cliente refazendo o
                # consentimento resolve — retentar nao.
                raise CalendarAuthError(
                    f"renovacao recusada ({response.status_code}): {_oauth_error(response)}"
                )
            body: dict[str, Any] = response.json()
            self._access_token = str(body["access_token"])
            lifetime = float(body.get("expires_in", 3600))
            self._access_token_expires_at = self._clock() + max(lifetime - _TOKEN_SKEW_SECONDS, 0.0)
            return self._access_token
        raise CalendarSyncPending(
            f"endpoint OAuth indisponivel apos {self._max_tries} tentativas",
            attempts=self._max_tries,
        )

    # -------------------------------------------------------------------- cache

    def _invalidate(self, calendar_id: str) -> None:
        """Escrita nossa torna o `freebusy` em cache mentiroso: descarta o calendario."""
        for key in [k for k in self._busy_cache if k[0] == calendar_id]:
            del self._busy_cache[key]


# ---------------------------------------------------------------------- helpers


def _q(segment: str) -> str:
    """Escapa um id para o caminho da URL (ids de calendario trazem `@` e `#`)."""
    return quote(segment, safe="")


def _iso(moment: datetime) -> str:
    return as_utc(moment).isoformat().replace("+00:00", "Z")


def _parse_instant(raw: str) -> datetime:
    return as_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))


def _error_reason(response: httpx.Response) -> str:
    try:
        errors = response.json()["error"]["errors"]
        return str(errors[0]["reason"])
    except (ValueError, KeyError, IndexError, TypeError):
        return ""


def _is_retryable(response: httpx.Response) -> bool:
    status = response.status_code
    if status >= 500 or status == 429:
        return True
    return status == 403 and _error_reason(response) in _RETRYABLE_403_REASONS


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return None


def _oauth_error(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", "desconhecido"))
    except ValueError:
        return "desconhecido"


def _permanent_error(response: httpx.Response) -> CalendarPermanentError:
    status = response.status_code
    reason = _error_reason(response)
    message = f"{status} do Google Calendar" + (f" ({reason})" if reason else "")
    if status == 404:
        return CalendarNotFound(message, status_code=status)
    return CalendarPermanentError(message, status_code=status)


def _time_field(moment: datetime, timezone: str) -> dict[str, str]:
    local = moment.astimezone(ZoneInfo(timezone))
    return {"dateTime": local.isoformat(), "timeZone": timezone}


def _insert_body(event: EventDraft, appointment_id: str) -> dict[str, Any]:
    private = {"datamind_appointment_id": appointment_id}
    if event.tenant_ref is not None:
        private["datamind_tenant"] = event.tenant_ref
    body: dict[str, Any] = {
        "iCalUID": appointment_id,
        "summary": event.summary,
        "start": _time_field(event.start, event.timezone),
        "end": _time_field(event.end, event.timezone),
        "extendedProperties": {"private": private},
    }
    if event.description is not None:
        body["description"] = event.description
    if event.attendee_emails:
        body["attendees"] = [{"email": email} for email in event.attendee_emails]
    return body


def _patch_body(patch: EventPatch) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if patch.summary is not None:
        body["summary"] = patch.summary
    if patch.description is not None:
        body["description"] = patch.description
    if patch.status is not None:
        body["status"] = patch.status
    if patch.start is not None or patch.end is not None:
        if patch.timezone is None:
            raise ValueError("EventPatch com start/end exige timezone")
        if patch.start is not None:
            body["start"] = _time_field(patch.start, patch.timezone)
        if patch.end is not None:
            body["end"] = _time_field(patch.end, patch.timezone)
    return body


def _parse_event(item: dict[str, Any], *, default_tz: str = "UTC") -> ExternalEvent:
    start, all_day = _parse_when(item.get("start"), default_tz)
    end, _ = _parse_when(item.get("end"), default_tz)
    private = item.get("extendedProperties", {}).get("private", {})
    status: EventStatus = item.get("status", "confirmed")
    return ExternalEvent(
        id=str(item["id"]),
        status=status,
        start=start,
        end=end,
        ical_uid=item.get("iCalUID"),
        summary=item.get("summary"),
        appointment_id=private.get("datamind_appointment_id"),
        all_day=all_day,
    )


def _parse_when(raw: dict[str, Any] | None, default_tz: str) -> tuple[datetime | None, bool]:
    if not raw:
        return None, False
    if "dateTime" in raw:
        return _parse_instant(raw["dateTime"]), False
    # Evento de dia inteiro: `date` sem hora, na timezone do evento (ou da agenda).
    day = date.fromisoformat(raw["date"])
    tz = ZoneInfo(raw.get("timeZone", default_tz))
    return as_utc(datetime(day.year, day.month, day.day, tzinfo=tz)), True
