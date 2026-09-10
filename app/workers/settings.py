"""Configuracoes de worker do arq — o que o `arq` recebe na linha de comando.

Quatro perfis, uma decisao:

- `WorkerSettings` roda tudo num processo. E o que o `docker compose` e o
  `tasks.ps1 worker` usam: em desenvolvimento, um processo a menos para lembrar.
- `InboundWorkerSettings`, `OutboundWorkerSettings` e `CronWorkerSettings` separam as
  filas em producao. Entrada e saida tem perfis de falha diferentes — uma rajada de 429
  no envio nao pode segurar o processamento de mensagens recebidas — e o cron nao
  disputa worker com nenhum dos dois.

`arq app.workers.settings.WorkerSettings` sobe o worker unico;
`arq app.workers.settings.OutboundWorkerSettings` sobe so a saida, e assim por diante.

**Por que um decorador em vez de heranca.** O arq monta o worker a partir do
`__dict__` da classe, nao dos atributos herdados. Uma subclasse que so trocasse
`functions` perderia `redis_settings` em silencio e o worker subiria apontando para o
Redis default (`localhost:6379`) — falha que aparece em producao, nao no import.
`_com_padroes` grava os atributos comuns em cada classe. `tests/unit/test_worker_settings.py`
guarda essa armadilha.
"""

from __future__ import annotations

from typing import Any, ClassVar

from app.core.config import get_settings
from app.workers.base import on_shutdown, on_startup
from app.workers.cron import CRON_JOBS
from app.workers.inbound import flush_conversation, process_inbound
from app.workers.outbound import send_outbound
from app.workers.queue import redis_settings_for

_settings = get_settings()

INBOUND_FUNCTIONS: list[Any] = [process_inbound, flush_conversation]
OUTBOUND_FUNCTIONS: list[Any] = [send_outbound]

_PADROES: dict[str, Any] = {
    "redis_settings": redis_settings_for(_settings),
    "on_startup": staticmethod(on_startup),
    "on_shutdown": staticmethod(on_shutdown),
    "max_jobs": _settings.worker_max_jobs,
    "job_timeout": _settings.worker_job_timeout_seconds,
    "keep_result": _settings.worker_keep_result_seconds,
    # O teto de tentativas e o do envio: quem decide desistir e `send_outbound`, que
    # marca a mensagem como falha antes de o arq abandonar o job em silencio.
    "max_tries": _settings.outbound_max_tries,
}


def _com_padroes[T: type](cls: T) -> T:
    """Grava os atributos comuns no proprio `__dict__` da classe (ver docstring)."""
    for name, value in _PADROES.items():
        if name not in cls.__dict__:
            setattr(cls, name, value)
    return cls


@_com_padroes
class WorkerSettings:
    """Worker unico: entrada, saida e cron no mesmo processo."""

    functions: ClassVar[list[Any]] = INBOUND_FUNCTIONS + OUTBOUND_FUNCTIONS
    cron_jobs: ClassVar[list[Any]] = CRON_JOBS


@_com_padroes
class InboundWorkerSettings:
    """So a fila de entrada: agregacao e disparo do turno."""

    functions: ClassVar[list[Any]] = INBOUND_FUNCTIONS
    cron_jobs: ClassVar[list[Any]] = []


@_com_padroes
class OutboundWorkerSettings:
    """So a fila de saida: entrega, backoff e 429."""

    functions: ClassVar[list[Any]] = OUTBOUND_FUNCTIONS
    cron_jobs: ClassVar[list[Any]] = []


@_com_padroes
class CronWorkerSettings:
    """So o cron. Hoje carrega apenas o batimento (ver `app.workers.cron`)."""

    functions: ClassVar[list[Any]] = []
    cron_jobs: ClassVar[list[Any]] = CRON_JOBS
