from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_health() -> None:
    app = create_app(Settings(environment="test"))
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_docs_ocultos_em_producao() -> None:
    settings = Settings(
        environment="production",
        secret_key="segredo-real",
        slot_token_secret="outro-segredo-real",
        anthropic_api_key="sk-real",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 404


def test_producao_recusa_segredo_de_desenvolvimento() -> None:
    settings = Settings(environment="production", anthropic_api_key="sk-real")
    with pytest.raises(RuntimeError, match="segredos de desenvolvimento"):
        create_app(settings)


def test_producao_exige_chave_do_llm() -> None:
    settings = Settings(
        environment="production",
        secret_key="segredo-real",
        slot_token_secret="outro-segredo-real",
    )
    with pytest.raises(RuntimeError, match="anthropic_api_key"):
        create_app(settings)


@pytest.mark.parametrize(
    "entrada",
    [
        "postgresql+asyncpg://u:p@h:5432/d",
        # Como a URL chega do ambiente na maioria dos deploys: sem driver.
        "postgresql://u:p@h:5432/d",
    ],
)
def test_url_sincrona_nomeia_o_driver(entrada: str) -> None:
    """O driver sincrono nao pode depender do default do SQLAlchemy.

    Ele mudou de psycopg2 para psycopg (v3) no 2.1, e a dependencia declarada e
    `psycopg2-binary` — sem o driver escrito na URL, `alembic upgrade head` morre com
    `ModuleNotFoundError` no primeiro ambiente que instalar a versao nova.
    """
    settings = Settings(environment="test", database_url=entrada, database_admin_url=entrada)

    assert settings.sync_database_url == "postgresql+psycopg2://u:p@h:5432/d"
    assert settings.sync_admin_database_url == "postgresql+psycopg2://u:p@h:5432/d"
