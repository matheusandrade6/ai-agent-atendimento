"""Worker de cron — a casca, sem nenhum job de negocio ainda.

`CRON_JOBS` existe vazia de proposito: ela e o ponto de encaixe das sessoes que vem, e
ter o processo de cron rodando desde agora significa que a S19 e a S20 acrescentam uma
linha em vez de descobrir infraestrutura no meio do caminho.

Encaixes previstos:

- **S19** — lembretes (`reminders`, secao 12.5), expiracao de hold (13.4, camada 1) e
  templates utility fora da janela de 24h (14.1.4).
- **S20** — sync reverso do Google Calendar via `events.list` com `syncToken` (13.5).

Como registrar, quando chegar a hora::

    CRON_JOBS.append(cron(expirar_holds, minute=None, second=0))

O unico job registrado hoje e o `heartbeat`. Ele existe por dois motivos: o arq recusa
subir um worker sem nenhuma funcao nem cron (`at least one function or cron_job must be
registered`), e um batimento no log e o sinal mais barato de "o cron esta vivo" para o
alerta de fila parada da S22.

O cron do arq roda no relogio do worker (UTC). Regra de negocio em horario local — "o
lembrete sai as 18h da unidade" — se resolve dentro do job com `app.core.time`, nunca
mudando o fuso do processo (RNF-06).
"""

from __future__ import annotations

from arq import cron
from arq.cron import CronJob

from app.core.telemetry import get_logger
from app.workers.base import WorkerContext

log = get_logger(__name__)


async def heartbeat(ctx: WorkerContext) -> None:
    """Batimento do cron. Substituivel, mas nunca removivel sem por outro job no lugar."""
    log.info("cron_vivo", job_id=ctx.get("job_id"))


#: Jobs de negocio entram aqui na S19 e na S20. Hoje, so o batimento.
CRON_JOBS: list[CronJob] = [cron(heartbeat, minute={0, 15, 30, 45}, run_at_startup=False)]
