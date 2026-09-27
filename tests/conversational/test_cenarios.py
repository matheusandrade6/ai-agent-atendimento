"""Um teste por cenario — o relatorio que se le quando algo cai.

O placar (`test_placar.py`) e quem bloqueia o merge; este arquivo existe para dizer
**qual** cenario caiu e **em que turno**, com a transcricao inteira no erro. Cenario
registrado como `known_failure` no placar e pulado aqui: a falha dele ja esta descrita
no placar, e repeti-la em vermelho a cada execucao ensina a ignorar vermelho.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conversational.harness import load_scenario, run_scenario, scenario_paths
from tests.conversational.judge import ToneJudge
from tests.conversational.placar import load_placar


def _ids(paths: list[Path]) -> list[str]:
    return [path.stem for path in paths]


@pytest.mark.parametrize("path", scenario_paths(), ids=_ids(scenario_paths()))
async def test_cenario(path: Path, judge: ToneJudge | None) -> None:
    scenario = load_scenario(path)
    entry = load_placar().get(scenario.scenario)
    if entry is not None and entry.status == "known_failure":
        pytest.skip(f"falha conhecida e registrada no placar: {entry.motivo}")

    result = await run_scenario(scenario, judge=judge)

    assert result.passed, "\n" + result.report()
