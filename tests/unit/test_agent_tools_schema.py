"""Contrato de schema das tools (secao 11.4, aceite da S08 e da S10).

`tenant_id` nunca pode aparecer num `input_schema`: e a garantia de que o modelo nao
tem como pedir para uma tool ler o banco de outro tenant (invariante 3). O schema
tambem precisa sobreviver a serializacao que vai para a API do provedor.
"""

from __future__ import annotations

import json
from typing import Any

from app.agent.tools.base import ToolSpec
from app.agent.tools.check_availability import check_availability_tool
from app.agent.tools.confirm_appointment import confirm_appointment_tool
from app.agent.tools.escalate_to_human import escalate_to_human_tool
from app.agent.tools.find_appointments import find_appointments_tool
from app.agent.tools.hold_slot import hold_slot_tool
from app.agent.tools.list_services import list_services_tool
from app.agent.tools.save_contact_info import save_contact_info_tool
from app.agent.tools.search_knowledge import search_knowledge_tool
from app.calendar.resolver import no_calendars
from app.knowledge.embeddings import HashingEmbeddingProvider

READ_TOOLS: tuple[ToolSpec, ...] = (
    list_services_tool(),
    search_knowledge_tool(HashingEmbeddingProvider()),
    check_availability_tool(no_calendars),
    find_appointments_tool(),
)
WRITE_TOOLS: tuple[ToolSpec, ...] = (
    escalate_to_human_tool(),
    hold_slot_tool(),
    confirm_appointment_tool(no_calendars),
    save_contact_info_tool(),
)
ALL_TOOLS: tuple[ToolSpec, ...] = (*READ_TOOLS, *WRITE_TOOLS)


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


def test_todas_tem_nome_e_descricao() -> None:
    for tool in ALL_TOOLS:
        assert tool.name
        assert tool.description


def test_tools_de_leitura_sao_kind_read() -> None:
    for tool in READ_TOOLS:
        assert tool.kind == "read"


def test_tools_de_escrita_sao_kind_write() -> None:
    for tool in WRITE_TOOLS:
        assert tool.kind == "write", tool.name


def test_nomes_batem_com_a_tabela_da_11_4() -> None:
    assert {tool.name for tool in ALL_TOOLS} == {
        "list_services",
        "search_knowledge",
        "check_availability",
        "find_appointments",
        "escalate_to_human",
        "hold_slot",
        "confirm_appointment",
        "save_contact_info",
    }


def test_argumentos_obrigatorios_existem_no_modelo_pydantic() -> None:
    """O schema que o modelo le e o modelo que valida nao podem divergir."""
    for tool in ALL_TOOLS:
        if tool.args_model is None:
            continue
        fields = set(tool.args_model.model_fields)
        assert set(tool.input_schema["properties"]) <= fields, tool.name
        assert set(tool.input_schema.get("required", [])) <= fields, tool.name
