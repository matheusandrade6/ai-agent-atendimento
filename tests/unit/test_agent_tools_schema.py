"""Contrato de schema das tools de leitura (secao 11.4, aceite da S08).

`tenant_id` nunca pode aparecer num `input_schema`: e a garantia de que o modelo nao
tem como pedir para uma tool ler o banco de outro tenant (invariante 3). O schema
tambem precisa sobreviver a serializacao que vai para a API do provedor.
"""

from __future__ import annotations

import json
from typing import Any

from app.agent.tools.base import ToolSpec
from app.agent.tools.list_services import list_services_tool
from app.agent.tools.search_knowledge import search_knowledge_tool
from app.knowledge.embeddings import HashingEmbeddingProvider

ALL_TOOLS: tuple[ToolSpec, ...] = (
    list_services_tool(),
    search_knowledge_tool(HashingEmbeddingProvider()),
)


def _assert_valid_json_schema(schema: dict[str, Any]) -> None:
    round_tripped = json.loads(json.dumps(schema))
    assert round_tripped == schema

    assert schema["type"] == "object"
    properties = schema["properties"]
    assert isinstance(properties, dict)
    for name, prop in properties.items():
        assert isinstance(name, str)
        assert isinstance(prop, dict)
        assert "type" in prop

    required = schema.get("required", [])
    assert isinstance(required, list)
    for key in required:
        assert key in properties


def test_todo_schema_e_json_valido() -> None:
    for tool in ALL_TOOLS:
        _assert_valid_json_schema(dict(tool.input_schema))


def test_tenant_id_nunca_aparece_em_input_schema() -> None:
    for tool in ALL_TOOLS:
        serialized = json.dumps(tool.input_schema)
        assert "tenant_id" not in serialized, tool.name


def test_todas_sao_tools_de_leitura_com_nome_e_descricao() -> None:
    for tool in ALL_TOOLS:
        assert tool.name
        assert tool.description
        assert tool.kind == "read"


def test_nomes_batem_com_a_tabela_da_11_4() -> None:
    assert {tool.name for tool in ALL_TOOLS} == {"list_services", "search_knowledge"}
