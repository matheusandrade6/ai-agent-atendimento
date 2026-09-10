"""Os quatro perfis de worker sobem com a configuracao certa (Sessao S05).

Este teste existe por causa de uma armadilha real: o arq monta o worker a partir do
`__dict__` da classe de settings, entao atributo herdado nao chega ate ele. Uma
subclasse que so trocasse `functions` subiria apontando para o Redis default
(`localhost:6379`) sem uma linha de erro — e a fila ficaria parada em producao.
"""

from __future__ import annotations

import pytest
from arq.worker import create_worker

from app.core.config import get_settings
from app.workers.settings import (
    CronWorkerSettings,
    InboundWorkerSettings,
    OutboundWorkerSettings,
    WorkerSettings,
)

PERFIS = [WorkerSettings, InboundWorkerSettings, OutboundWorkerSettings, CronWorkerSettings]


@pytest.mark.parametrize("perfil", PERFIS, ids=lambda cls: cls.__name__)
def test_perfil_usa_o_redis_da_aplicacao(perfil: type) -> None:
    settings = get_settings()
    worker = create_worker(perfil)

    assert worker.redis_settings.port == settings.redis_url.port
    assert worker.redis_settings.host == settings.redis_url.host


@pytest.mark.parametrize("perfil", PERFIS, ids=lambda cls: cls.__name__)
def test_perfil_tem_ciclo_de_vida_e_limites(perfil: type) -> None:
    settings = get_settings()
    worker = create_worker(perfil)

    assert worker.on_startup is not None, "sem on_startup o ctx nao tem agregador nem canal"
    assert worker.on_shutdown is not None
    assert worker.max_tries == settings.outbound_max_tries
    assert worker.job_timeout_s == settings.worker_job_timeout_seconds
    assert worker.max_jobs == settings.worker_max_jobs


def test_cada_fila_registra_so_os_seus_jobs() -> None:
    assert set(create_worker(InboundWorkerSettings).functions) == {
        "process_inbound",
        "flush_conversation",
    }
    assert set(create_worker(OutboundWorkerSettings).functions) == {"send_outbound"}
    assert set(create_worker(WorkerSettings).functions) >= {
        "process_inbound",
        "flush_conversation",
        "send_outbound",
    }


def test_cron_sobe_mesmo_sem_job_de_negocio() -> None:
    """O arq recusa worker sem nada registrado; o batimento mantem o cron de pe."""
    worker = create_worker(CronWorkerSettings)

    assert [job.name for job in worker.cron_jobs] == ["cron:heartbeat"]
