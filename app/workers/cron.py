"""Worker de cron.

`CRON_JOBS` e o ponto de encaixe das sessoes: cada uma acrescenta uma linha em vez de
descobrir infraestrutura no meio do caminho.

Registrados:

- `heartbeat` — o arq recusa subir um worker sem nenhuma funcao nem cron (`at least one
  function or cron_job must be registered`), e um batimento no log e o sinal mais barato
  de "o cron esta vivo" para o alerta de fila parada da S22.
- `expire_holds_job` (S14) — a cada minuto, marca como `expired` os holds vencidos (13.4,
  camada 1). Limpeza: a ocupacao ja ignora hold vencido sem esperar por ele.

Encaixes previstos:

- **S19** — lembretes (`reminders`, secao 12.5) e templates utility fora da janela de
  24h (14.1.4).
- **S20** — sync reverso do Google Calendar via `events.list` com `syncToken` (13.5).

O cron do arq roda no relogio do worker (UTC). Regra de negocio em horario local — "o
lembrete sai as 18h da unidade" — se resolve dentro do job com `app.core.time`, nunca
mudando o fuso do processo (RNF-06).
"""

from __future__ import annotations

from arq import cron
from arq.cron import CronJob

from app.core.db import bypass_rls_session
from app.core.telemetry import get_logger
from app.core.time import now_utc
from app.scheduling.booking import expire_holds
from app.workers.base import WorkerContext

log = get_logger(__name__)


async def heartbeat(ctx: WorkerContext) -> None:
    """Batimento do cron. Substituivel, mas nunca removivel sem por outro job no lugar."""
    log.info("cron_vivo", job_id=ctx.get("job_id"))


async def expire_holds_job(ctx: WorkerContext) -> int:
    """Expira holds vencidos de todos os tenants (job que atravessa tenants, D-03)."""
    async with bypass_rls_session(ctx.get("settings")) as session:
        expired = await expire_holds(session, now_utc())
    if expired:
        log.info("holds_expirados", total=expired)
    return expired


#: Jobs de negocio da S19 e da S20 entram aqui.
CRON_JOBS: list[CronJob] = [
    cron(heartbeat, minute={0, 15, 30, 45}, run_at_startup=False),
    cron(expire_holds_job, second=0, run_at_startup=False),
]
