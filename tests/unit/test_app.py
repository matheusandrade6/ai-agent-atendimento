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
