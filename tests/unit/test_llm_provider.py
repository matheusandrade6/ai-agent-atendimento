"""Contabilizacao de custo e traducao para o SDK da Anthropic (secoes 11.1 e 17.2).

O custo por mensagem nao e so metrica: e o que alimenta o disjuntor por conversa da
11.5. Errar para menos deixa o limite do tenant sem efeito.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, ClassVar

import pytest

from app.agent.llm import (
    PRICING,
    AnthropicProvider,
    LLMMessage,
    PromptSegment,
    TextBlock,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
    Usage,
    _blocks_of,
    _message_param,
    _system_param,
    _tool_param,
    cost_of,
    price_for,
)
from app.core.config import Settings

# ---------------------------------- precos ----------------------------------


def test_custo_de_uma_chamada_conhecida() -> None:
    """Sonnet 5: US$ 2 por MTok de entrada, US$ 10 por MTok de saida."""
    custo = cost_of("claude-sonnet-5", Usage(input_tokens=1_000_000, output_tokens=100_000))
    assert custo == Decimal("3.000000")


def test_cache_e_cobrado_diferente_de_entrada_crua() -> None:
    leitura = cost_of("claude-sonnet-5", Usage(cache_read_input_tokens=1_000_000))
    escrita = cost_of("claude-sonnet-5", Usage(cache_creation_input_tokens=1_000_000))
    crua = cost_of("claude-sonnet-5", Usage(input_tokens=1_000_000))

    assert leitura == crua / 10
    assert escrita == crua * Decimal("1.25")


def test_modelo_desconhecido_usa_o_preco_mais_caro() -> None:
    """Errar para cima abre o disjuntor cedo; errar para baixo estoura o orcamento."""
    mais_caro = max(PRICING.values(), key=lambda p: p.output_per_mtok)
    assert price_for("claude-modelo-que-ainda-nao-existe") == mais_caro


def test_id_com_sufixo_de_data_cai_no_preco_da_familia() -> None:
    assert price_for("claude-haiku-4-5-20251001") == PRICING["claude-haiku-4-5"]


def test_custo_respeita_a_precisao_da_coluna() -> None:
    """`messages.cost_usd` e NUMERIC(10,6): arredondar aqui evita divergencia na soma."""
    custo = cost_of("claude-haiku-4-5", Usage(input_tokens=7, output_tokens=3))
    assert custo.as_tuple().exponent == -6


def test_usage_soma_e_total_de_entrada() -> None:
    total = Usage(input_tokens=10, output_tokens=5) + Usage(
        input_tokens=1, cache_read_input_tokens=100, cache_creation_input_tokens=20
    )
    assert total.input_tokens == 11
    assert total.output_tokens == 5
    assert total.total_input_tokens == 131


# ------------------------------ traducao do SDK ------------------------------


def test_system_vira_blocos_com_corte_de_cache() -> None:
    blocos = _system_param(
        (PromptSegment("prefixo estavel", cache_breakpoint=True), PromptSegment("volatil"))
    )
    assert blocos[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocos[1]


def test_segmento_vazio_nao_vira_bloco() -> None:
    assert _system_param((PromptSegment("   "),)) == []


def test_mensagem_com_tool_use_e_tool_result() -> None:
    uso = ToolUseBlock("tu_1", "check_availability", {"n": 1})
    assistente = _message_param(LLMMessage(role="assistant", content=(uso,)))
    assert assistente["role"] == "assistant"
    bloco: Any = next(iter(assistente["content"]))
    assert bloco["type"] == "tool_use"
    assert bloco["input"] == {"n": 1}

    usuario = _message_param(
        LLMMessage(role="user", content=(ToolResultBlock("tu_1", '{"slots": []}'),))
    )
    resultado: Any = next(iter(usuario["content"]))
    assert resultado["type"] == "tool_result"
    assert resultado["tool_use_id"] == "tu_1"


def test_tool_vai_sem_tenant_id_no_schema() -> None:
    """Invariante 3: o que o modelo pode preencher e so o `input_schema`."""
    param = _tool_param(
        ToolSchema(
            name="check_availability",
            description="slots",
            input_schema={"type": "object", "properties": {"service_id": {"type": "string"}}},
        )
    )
    assert "tenant_id" not in str(param["input_schema"])


def test_resposta_do_sdk_vira_blocos_neutros() -> None:
    class _Text:
        type = "text"
        text = "Oi!"

    class _Use:
        type = "tool_use"
        id = "tu_1"
        name = "check_availability"
        input: ClassVar[dict[str, str]] = {"service_id": "s1"}

    class _Thinking:
        type = "thinking"
        thinking = "raciocinio bruto"

    blocos = _blocks_of([_Text(), _Use(), _Thinking()])
    assert blocos == (
        TextBlock("Oi!"),
        ToolUseBlock("tu_1", "check_availability", {"service_id": "s1"}),
    )


def test_bloco_de_pensamento_nao_e_persistido() -> None:
    """Raciocinio bruto em banco de conversa de cliente e risco sem contrapartida."""

    class _Thinking:
        type = "thinking"
        thinking = "detalhe interno"

    assert _blocks_of([_Thinking()]) == ()


# --------------------------------- settings ---------------------------------


def test_provider_le_modelo_e_esforco_das_settings() -> None:
    settings = Settings(
        environment="test",
        anthropic_api_key="sk-teste",
        llm_model="claude-sonnet-5",
        llm_effort="medium",
        llm_thinking=False,
    )
    provider = AnthropicProvider.from_settings(settings)

    assert provider.model == "claude-sonnet-5"
    assert provider.effort == "medium"
    assert provider._thinking_param() == {"type": "disabled"}


def test_pensamento_adaptativo_e_o_padrao() -> None:
    settings = Settings(environment="test", anthropic_api_key="sk-teste")
    assert AnthropicProvider.from_settings(settings)._thinking_param() == {"type": "adaptive"}


@pytest.mark.parametrize("modelo", list(PRICING))
def test_todo_modelo_da_tabela_tem_preco_positivo(modelo: str) -> None:
    preco = PRICING[modelo]
    assert preco.input_per_mtok > 0
    assert preco.output_per_mtok > preco.input_per_mtok
