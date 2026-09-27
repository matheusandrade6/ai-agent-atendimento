"""O placar da 19.4: queda em qualquer cenario bloqueia o merge.

Quatro conferencias, cada uma fechando um jeito de conseguir verde sem ter conferido
nada:

1. **Cenario sem linha no placar** — escrever um cenario e esquecer de registra-lo
   deixaria a queda dele invisivel para as demais regras.
2. **Linha sem cenario** — apagar o arquivo de um cenario que incomoda tiraria a
   cobertura sem aparecer no diff como remocao de teste.
3. **Queda** — cenario registrado como `pass` que falha. E o bloqueio de merge da 19.4.
4. **Enfraquecimento** — o cenario continua verde, mas com menos assercoes do que o
   placar registra.

E ainda a quinta, que vale para o outro lado: `known_failure` que passou a funcionar
precisa ser promovido, se nao o placar vira um cemiterio de defeitos ja corrigidos.

O juiz de tom nao entra aqui de proposito (ver `judge.py`): placar que depende de
chamada de rede nao deterministica nao bloqueia merge, atrapalha.
"""

from __future__ import annotations

import pytest

from tests.conversational.harness import ScenarioResult, load_scenario, run_scenario, scenario_paths
from tests.conversational.placar import load_placar, load_placar_file


async def _resultados() -> dict[str, ScenarioResult]:
    resultados: dict[str, ScenarioResult] = {}
    for path in scenario_paths():
        scenario = load_scenario(path)
        assert scenario.scenario == path.stem, (
            f"{path.name}: o campo `scenario` ({scenario.scenario!r}) tem de ser igual ao"
            " nome do arquivo — o placar e indexado por ele"
        )
        resultados[scenario.scenario] = await run_scenario(scenario)
    return resultados


def test_placar_e_os_cenarios_cobrem_o_mesmo_conjunto() -> None:
    placar = load_placar()
    nomes = {path.stem for path in scenario_paths()}

    sem_linha = sorted(nomes - set(placar))
    sem_arquivo = sorted(set(placar) - nomes)

    assert not sem_linha, f"cenarios fora do placar: {sem_linha} — registre-os em placar.yaml"
    assert not sem_arquivo, f"placar cita cenario que nao existe: {sem_arquivo}"


async def test_nenhum_cenario_aprovado_caiu() -> None:
    placar = load_placar()
    resultados = await _resultados()

    quedas = [
        f"{nome}:\n{resultado.report()}"
        for nome, resultado in resultados.items()
        if placar[nome].status == "pass" and not resultado.passed
    ]

    assert not quedas, "queda de cenario aprovado (19.4):\n" + "\n".join(quedas)


async def test_falha_conhecida_que_passou_precisa_ser_promovida() -> None:
    placar = load_placar()
    resultados = await _resultados()

    promover = [
        nome
        for nome, resultado in resultados.items()
        if placar[nome].status == "known_failure" and resultado.passed
    ]

    assert not promover, (
        f"cenarios registrados como known_failure passaram: {promover}."
        " Troque o status para `pass` em placar.yaml."
    )


@pytest.mark.parametrize("path", scenario_paths(), ids=lambda p: p.stem)
def test_cenario_nao_perdeu_assercoes(path) -> None:  # type: ignore[no-untyped-def]
    placar = load_placar()
    scenario = load_scenario(path)
    registrado = placar[scenario.scenario].assercoes
    atual = scenario.assertion_count()

    assert atual == registrado, (
        f"{scenario.scenario}: o placar registra {registrado} assercoes e o cenario tem"
        f" {atual}. Menos e enfraquecimento; mais pede atualizacao do placar."
    )


def test_placar_tem_versao_e_data() -> None:
    placar = load_placar_file()
    assert placar.versao >= 1
    assert placar.atualizado_em
