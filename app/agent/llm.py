"""Interface com o modelo e contabilizacao de custo (secoes 11.1 e 17.2).

Por que existe uma interface
----------------------------
O motor (`app.agent.engine`) nao importa `anthropic`. Ele fala com `LLMProvider`, que
troca blocos neutros — texto, chamada de tool, resultado de tool. Isso serve a tres
coisas concretas, nao a abstracao pela abstracao:

1. O teste do loop roda com um provider falso, sem rede e sem chave de API.
2. A suite conversacional (S11) grava e repete respostas sem tocar no motor.
3. Trocar de modelo ou de fornecedor nao mexe no loop de tool calling.

Custo por mensagem
------------------
`usage` volta em tokens; `messages.cost_usd` guarda dinheiro. A conversao acontece aqui,
com a tabela de precos versionada junto do codigo, porque o limite por conversa
(`limits.max_llm_cost_usd_per_conversation`) e um disjuntor: se o custo for subestimado,
o disjuntor nao dispara. Modelo desconhecido na tabela usa o **preco mais caro**
conhecido — errar para cima faz o disjuntor abrir cedo demais, que e o lado barato do
erro.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final, Literal, Protocol, Self, cast

from anthropic import AsyncAnthropic, omit
from anthropic.types import (
    MessageParam,
    TextBlockParam,
    ThinkingConfigParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUnionParam,
    ToolUseBlockParam,
)

from app.core.config import Settings
from app.core.telemetry import get_logger

log = get_logger(__name__)

Role = Literal["user", "assistant"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]

#: Teto do que o modelo pode escrever num turno. A quebra em varias mensagens de
#: WhatsApp acontece depois, nos guardrails de saida (11.5).
DEFAULT_MAX_TOKENS: Final[int] = 1024


# ------------------------------ blocos neutros ------------------------------


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False


Block = TextBlock | ToolUseBlock | ToolResultBlock


@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: Role
    content: tuple[Block, ...]

    @classmethod
    def user(cls, text: str) -> LLMMessage:
        return cls(role="user", content=(TextBlock(text),))

    @classmethod
    def assistant(cls, text: str) -> LLMMessage:
        return cls(role="assistant", content=(TextBlock(text),))

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.content if isinstance(b, TextBlock))


@dataclass(frozen=True, slots=True)
class ToolSchema:
    """Contrato de uma tool como o modelo a ve (11.4).

    `tenant_id` nunca aparece aqui: ele vem do contexto de execucao (invariante 3).
    """

    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class PromptSegment:
    """Pedaco do system prompt, com a marca de onde o cache pode cortar.

    O cache do prompt e casamento de prefixo: um byte diferente invalida tudo daquele
    ponto em diante. Por isso o prompt sai daqui em dois segmentos — o estavel (que
    fecha com `cache_breakpoint=True`) e o volatil (data, hora, campos coletados).
    """

    text: str
    cache_breakpoint: bool = False


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens + other.cache_creation_input_tokens
            ),
        )

    @property
    def total_input_tokens(self) -> int:
        """Tudo que entrou, cacheado ou nao. E o que vai para `messages.tokens_in`."""
        return self.input_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens


@dataclass(frozen=True, slots=True)
class LLMResponse:
    blocks: tuple[Block, ...]
    stop_reason: str | None
    model: str
    usage: Usage
    cost_usd: Decimal

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks if isinstance(b, TextBlock)).strip()

    @property
    def tool_calls(self) -> tuple[ToolUseBlock, ...]:
        return tuple(b for b in self.blocks if isinstance(b, ToolUseBlock))

    @property
    def assistant_message(self) -> LLMMessage:
        return LLMMessage(role="assistant", content=self.blocks)


@dataclass(frozen=True, slots=True)
class ExtractionResponse:
    """Saida da chamada barata e separada de extracao (11.7)."""

    data: Mapping[str, Any]
    model: str
    usage: Usage
    cost_usd: Decimal


class LLMProvider(Protocol):
    """O que o motor precisa de um modelo. Nada alem disto."""

    async def complete(
        self,
        *,
        system: Sequence[PromptSegment],
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolSchema] = (),
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...

    async def extract(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        schema: Mapping[str, Any],
        model: str | None = None,
    ) -> ExtractionResponse: ...


# --------------------------------- precos ---------------------------------


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Dolares por milhao de tokens."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal

    @property
    def cache_write_per_mtok(self) -> Decimal:
        """Escrita no cache custa ~1.25x a entrada."""
        return self.input_per_mtok * Decimal("1.25")

    @property
    def cache_read_per_mtok(self) -> Decimal:
        """Leitura do cache custa ~0.1x a entrada."""
        return self.input_per_mtok * Decimal("0.1")


#: Tabela de precos publicada (USD por 1M de tokens). Atualizar junto com o modelo
#: default em `Settings.llm_model`.
PRICING: Final[Mapping[str, ModelPrice]] = {
    "claude-fable-5-1": ModelPrice(Decimal("10.00"), Decimal("50.00")),
    "claude-fable-5": ModelPrice(Decimal("10.00"), Decimal("50.00")),
    "claude-opus-5": ModelPrice(Decimal("5.00"), Decimal("25.00")),
    "claude-opus-4-8": ModelPrice(Decimal("5.00"), Decimal("25.00")),
    "claude-opus-4-7": ModelPrice(Decimal("5.00"), Decimal("25.00")),
    "claude-opus-4-6": ModelPrice(Decimal("5.00"), Decimal("25.00")),
    "claude-sonnet-5": ModelPrice(Decimal("2.00"), Decimal("10.00")),
    "claude-sonnet-4-6": ModelPrice(Decimal("3.00"), Decimal("15.00")),
    "claude-haiku-4-5": ModelPrice(Decimal("1.00"), Decimal("5.00")),
}

_MILLION: Final[Decimal] = Decimal(1_000_000)
#: Precisao de `messages.cost_usd` (NUMERIC(10,6)). Arredondar aqui evita que a soma
#: da conversa acumule casas que o banco vai truncar depois.
_CENTS: Final[Decimal] = Decimal("0.000001")


def price_for(model: str) -> ModelPrice:
    """Preco do modelo. Desconhecido usa o mais caro da tabela, de proposito.

    Um id novo (ou com sufixo de data) nao pode fazer o disjuntor de custo dormir. Errar
    para cima interrompe uma conversa cedo demais; errar para baixo estoura o orcamento
    do tenant sem ninguem ver.
    """
    known = PRICING.get(model)
    if known is not None:
        return known
    for prefix, price in PRICING.items():
        if model.startswith(prefix):
            return price
    log.warning("modelo_sem_preco_conhecido", model=model)
    return max(PRICING.values(), key=lambda p: p.output_per_mtok)


def cost_of(model: str, usage: Usage) -> Decimal:
    """Custo em USD de uma chamada, arredondado para a precisao da coluna."""
    price = price_for(model)
    total = (
        Decimal(usage.input_tokens) * price.input_per_mtok
        + Decimal(usage.output_tokens) * price.output_per_mtok
        + Decimal(usage.cache_read_input_tokens) * price.cache_read_per_mtok
        + Decimal(usage.cache_creation_input_tokens) * price.cache_write_per_mtok
    ) / _MILLION
    return total.quantize(_CENTS)


# ------------------------------ implementacao ------------------------------


@dataclass(slots=True)
class AnthropicProvider:
    """Implementacao de `LLMProvider` sobre o SDK oficial.

    Pensamento e esforco
    --------------------
    O agente decide *o que perguntar* e *quando chamar tool* — decisao que se beneficia
    de raciocinio — mas roda sob um teto de custo por conversa medido em centavos
    (`limits.max_llm_cost_usd_per_conversation`, default 0.15 USD para ate 60 mensagens).
    O ajuste que atende aos dois lados e pensamento adaptativo com `effort` baixo, ambos
    em settings para poder subir por ambiente sem tocar no codigo.
    """

    client: AsyncAnthropic
    model: str
    extraction_model: str
    max_tokens: int = DEFAULT_MAX_TOKENS
    effort: Effort = "low"
    thinking: bool = True
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: Settings, client: AsyncAnthropic | None = None) -> Self:
        return cls(
            client=client or AsyncAnthropic(api_key=settings.anthropic_api_key),
            model=settings.llm_model,
            extraction_model=settings.llm_extraction_model,
            max_tokens=settings.llm_max_tokens,
            effort=settings.llm_effort,
            thinking=settings.llm_thinking,
        )

    async def complete(
        self,
        *,
        system: Sequence[PromptSegment],
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolSchema] = (),
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        chosen = model or self.model
        # `omit` em vez de lista vazia: a chamada de fechamento do loop (sem tools) e a
        # de resumo (sem system) precisam **nao enviar** o campo, nao enviar `[]`.
        system_blocks = _system_param(system)
        tool_params = [_tool_param(t) for t in tools]
        response = await self.client.messages.create(
            model=chosen,
            max_tokens=max_tokens or self.max_tokens,
            system=system_blocks or omit,
            messages=[_message_param(m) for m in messages],
            tools=tool_params or omit,
            thinking=self._thinking_param(),
            output_config={"effort": self.effort},
        )
        usage = _usage_of(response.usage)
        return LLMResponse(
            blocks=_blocks_of(response.content),
            stop_reason=response.stop_reason,
            model=response.model,
            usage=usage,
            cost_usd=cost_of(response.model, usage),
        )

    async def extract(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        schema: Mapping[str, Any],
        model: str | None = None,
    ) -> ExtractionResponse:
        """Extracao estruturada: chamada barata, sem tools e sem pensamento.

        E uma leitura mecanica do que a pessoa ja disse. O que fazer com o resultado —
        inclusive se o intake terminou — e decidido em codigo (11.7).
        """
        chosen = model or self.extraction_model
        response = await self.client.messages.create(
            model=chosen,
            max_tokens=self.max_tokens,
            system=system,
            messages=[_message_param(m) for m in messages],
            output_config={"format": {"type": "json_schema", "schema": dict(schema)}},
        )
        usage = _usage_of(response.usage)
        return ExtractionResponse(
            data=_parse_json_object(response.content),
            model=response.model,
            usage=usage,
            cost_usd=cost_of(response.model, usage),
        )

    def _thinking_param(self) -> ThinkingConfigParam:
        return {"type": "adaptive"} if self.thinking else {"type": "disabled"}


# ------------------------------- traducao -------------------------------


def _system_param(segments: Sequence[PromptSegment]) -> list[TextBlockParam]:
    blocks: list[TextBlockParam] = []
    for segment in segments:
        if not segment.text.strip():
            continue
        block: TextBlockParam = {"type": "text", "text": segment.text}
        if segment.cache_breakpoint:
            block["cache_control"] = {"type": "ephemeral"}
        blocks.append(block)
    return blocks


def _message_param(message: LLMMessage) -> MessageParam:
    content: list[Any] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            text_block: TextBlockParam = {"type": "text", "text": block.text}
            content.append(text_block)
        elif isinstance(block, ToolUseBlock):
            use_block: ToolUseBlockParam = {
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": dict(block.arguments),
            }
            content.append(use_block)
        else:
            result_block: ToolResultBlockParam = {
                "type": "tool_result",
                "tool_use_id": block.tool_use_id,
                "content": block.content,
                "is_error": block.is_error,
            }
            content.append(result_block)
    return {"role": message.role, "content": content}


def _tool_param(tool: ToolSchema) -> ToolUnionParam:
    param: ToolParam = {
        "name": tool.name,
        "description": tool.description,
        "input_schema": dict(tool.input_schema),
    }
    return param


def _blocks_of(content: Sequence[Any]) -> tuple[Block, ...]:
    """Traduz a resposta do SDK para blocos neutros.

    Blocos de pensamento sao descartados de proposito: nada no motor depende deles e
    guardar raciocinio bruto em banco de conversa de cliente e risco sem contrapartida.
    """
    blocks: list[Block] = []
    for item in content:
        kind = getattr(item, "type", None)
        if kind == "text":
            blocks.append(TextBlock(item.text))
        elif kind == "tool_use":
            arguments = item.input if isinstance(item.input, Mapping) else {}
            blocks.append(ToolUseBlock(id=item.id, name=item.name, arguments=dict(arguments)))
    return tuple(blocks)


def _usage_of(usage: Any) -> Usage:
    return Usage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cache_read_input_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        cache_creation_input_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    )


def _parse_json_object(content: Sequence[Any]) -> Mapping[str, Any]:
    for item in content:
        if getattr(item, "type", None) != "text":
            continue
        try:
            parsed = json.loads(item.text)
        except json.JSONDecodeError:
            log.warning("extracao_json_invalido")
            continue
        if isinstance(parsed, Mapping):
            return cast(Mapping[str, Any], parsed)
    return {}
