"""Fixtures da suite conversacional.

Marcador `conversational` em tudo que esta abaixo deste diretorio: a 19.4 manda rodar a
suite inteira a cada mudanca de prompt (`pytest -m conversational`), e isso so e um
comando se a marca vier do diretorio, e nao de cada arquivo lembrar de declara-la.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from tests.conversational.harness import TENANTS_DIR
from tests.conversational.judge import ToneJudge, build_judge

HERE = Path(__file__).resolve().parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Marca so o que esta abaixo deste diretorio.

    O hook e da sessao inteira, nao do diretorio onde o conftest mora: sem o filtro de
    caminho, a marca cairia em todos os testes do projeto e `-m conversational` passaria
    a rodar a suite inteira — exatamente o contrario do que a marca serve.
    """
    for item in items:
        caminho = getattr(item, "path", None)
        if caminho is not None and HERE in Path(caminho).parents:
            item.add_marker(pytest.mark.conversational)


@pytest.fixture(scope="session")
def conversational_settings() -> Settings:
    return Settings(environment="test", tenants_config_dir=str(TENANTS_DIR))


@pytest.fixture(scope="session")
def judge(conversational_settings: Settings) -> ToneJudge | None:
    """Juiz de tom, ou `None`. Sem ele, as assercoes `tone` ficam sem conferir."""
    return build_judge(conversational_settings)
